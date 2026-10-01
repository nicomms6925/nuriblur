"""분석 패스 (docs/03 패스 1): probe → decode → detect → track → identify → link → save.

결과는 .nbproj(트랙 DB)에 저장되며, 이후 보호대상·마스킹 설정을 바꿔도 다시 분석하지 않는다.
체크포인트(기본 15초마다)에 지금까지의 트랙과 checkpoint_frame을 저장해, 중단·크래시 후 이어서 분석한다.

적응형 검출 간격: 검출 때마다 확실한(conf≥0.5) 얼굴·전신 중 기존 트랙에 이어지지 않은 비율을 보고,
절반을 넘으면(타임랩스·빠른 이동 — 검출 사이 보간으로 따라갈 수 없음) FAST_HOLD_S 동안 매 프레임 검출한다.
"""
from __future__ import annotations

import functools
import itertools
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from worker.errors import Cancelled, NBError
from worker.gpu import GpuMonitor
from worker.io.project import Project, TrackRow
from worker.jobs import Emit, JobControl, Throttle, event
from worker.models.registry import Registry, default_registry
from worker.pipeline import rules as rules_mod
from worker.pipeline.decode import DecoderThread
from worker.pipeline.detect import Det, DetectorSet
from worker.pipeline.identify import TrackSamples
from worker.pipeline.link import link_faces_to_persons
from worker.pipeline.probe import probe
from worker.pipeline.track import ByteTracker, STrack, densify

CLASSES = ("face", "person", "plate")
CHECKPOINT_S = 15.0
FAST_NEW_RATIO = 0.5
FAST_MIN_DETS = 5
FAST_HOLD_S = 2.0
COLORS = {"face": (56, 140, 242), "person": (200, 200, 200), "plate": (56, 140, 242)}


def preview_jpeg(img: np.ndarray, dets: list[Det] | None = None, max_w: int = 854) -> bytes:
    h, w = img.shape[:2]
    s = min(1.0, max_w / w)
    small = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else img.copy()
    for d in dets or []:
        cv2.rectangle(small, (int(d.x1 * s), int(d.y1 * s)), (int(d.x2 * s), int(d.y2 * s)), COLORS.get(d.cls, (0, 255, 0)), 2)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes() if ok else b""


def _to_row(t: STrack, cls: str, samples: TrackSamples | None) -> TrackRow:
    boxes = densify(t.obs)
    confs = [o[4] for o in t.obs.values()]
    return TrackRow(id=t.tid, cls=cls, start_f=min(boxes), end_f=max(boxes),
                    conf_avg=float(np.mean(confs)) if confs else 0.0,
                    embedding=samples.embedding() if samples else None, boxes=boxes)


def analyze(path: str | Path, project_path: str | Path, job_id: str, ctl: JobControl, emit: Emit,
            classes: tuple[str, ...] = CLASSES, profile: str = "cpu", detect_interval: int = 0,
            resume_from_frame: int = 0, registry: Registry | None = None) -> dict[str, Any]:
    reg = registry or default_registry()
    prof = reg.profile(profile)
    interval = detect_interval if detect_interval and detect_interval > 0 else prof.detect_interval
    classes = tuple(c for c in classes if c in CLASSES) or CLASSES

    def warn(code: str, msg: str) -> None:
        emit(event("warning", job_id, stage="decode", code=code, message=msg))

    emit(event("progress", job_id, stage="ingest", frame=0, total=0))
    media = probe(path)
    total = int(media["frames"])
    project_path = Path(project_path)

    # 재개 여부: resume_from_frame>0 지정 또는 -1(자동) 이면서 같은 원본의 미완료 프로젝트
    start = 0
    project: Project | None = None
    if resume_from_frame != 0 and project_path.exists():
        try:
            project = Project.open(project_path)
            same = project.media().get("sha256") == media["sha256"]
            cp = project.analyze_checkpoint()
            if not same:
                project.close()
                project = None
            elif resume_from_frame > 0:
                start = resume_from_frame
            elif cp > 0:
                start = cp
            elif cp < 0:  # 이미 분석 완료
                start = -1
        except NBError:
            project = None
    if project is None:
        project = Project.create(project_path)
        start = 0
    try:
        if start == -1:
            emit(event("log", job_id, stage="save", message="이미 분석된 프로젝트입니다 — 재분석하지 않습니다"))
            stats = _stats(project)
            emit(event("done", job_id, stage="analyze", frame=total, total=total, counts=stats,
                       output_path=str(project_path)))
            return stats
        project.set_media(media)
        project.set_meta("profile", profile)
        project.set_meta("classes", ",".join(classes))
        project.set_meta("detect_interval", str(interval))
        project.set_meta("plate_stride", str(interval * max(1, int(prof.plate_every or 1))))  # 번호판 모델 실행 간격(프레임)
        if start > 0:
            project.delete_tracks_from(start)
            emit(event("log", job_id, stage="decode", message=f"체크포인트 프레임 {start}부터 이어서 분석합니다"))
        project.upsert_job(job_id, "analyze", status="ANALYZING", checkpoint_frame=start)

        emit(event("log", job_id, stage="ingest",
                   message=f"probe: {media['codec']} {media['width']}x{media['height']} {media['fps']:.2f}"
                           f"{' VFR' if media['is_vfr'] else ' CFR'} rot={media['rotation']} frames={total}"))
        det = DetectorSet(prof, classes, reg)
        emit(event("log", job_id, stage="detect",
                   message=f"model: {det.object_spec.id} + {det.face_spec.id} + {det.plate_spec.id} · 간격 {interval}"))

        ids = itertools.count(project.next_track_id())
        trackers = {c: ByteTracker(interval, media["fps"] or 30.0, id_alloc=ids.__next__, dist_assoc=(c == "plate"),
                                   max_lost_steps=None if c == "plate" else 3)
                    for c in CLASSES}
        samples: dict[int, TrackSamples] = {}
        cls_of: dict[int, str] = {}
        dirty: set[int] = set()
        pts_rows: list[tuple[int, int]] = []
        last_dets: list[Det] = []
        prog_t, prev_t = Throttle(0.5), Throttle(0.5)
        ck_t0 = time.monotonic()
        t0 = time.monotonic()
        done_frames = 0
        last_idx = start - 1

        def checkpoint(next_frame: int) -> None:
            nonlocal pts_rows, dirty
            project.add_frame_pts(pts_rows)
            pts_rows = []
            rows = []
            for c, tr in trackers.items():
                for t in tr.active():
                    if t.tid in dirty and t.obs:
                        rows.append(_to_row(t, c, samples.get(t.tid)))
            project.write_tracks(rows)
            dirty = set()
            project.upsert_job(job_id, "analyze", checkpoint_frame=next_frame)
            project.save(reg.model_hashes())

        emit(event("progress", job_id, stage="decode", frame=start, total=total))
        gpu = GpuMonitor().start()
        cur_interval, next_det, fast_until, fast_frames = interval, start, -1, 0
        hold = int(FAST_HOLD_S * (media["fps"] or 30))
        try:
            for fr in DecoderThread(path, start=start, warn=warn, stop=None):
                ctl.check(on_pause=functools.partial(checkpoint, last_idx + 1))
                idx = fr.index
                pts_rows.append((idx, fr.pts if fr.pts is not None else idx))
                if idx >= next_det:
                    dets = det(fr.bgr)
                    last_dets = dets
                    n_conf = n_new = 0
                    for c in CLASSES:
                        cd = [d for d in dets if d.cls == c]
                        arr = np.array([[d.x1, d.y1, d.x2, d.y2, d.conf] for d in cd]).reshape(-1, 5)
                        for di, tid in trackers[c].update(idx, arr):
                            d = cd[di]
                            if c in ("face", "person") and d.conf >= 0.5:
                                n_conf += 1
                                n_new += tid not in cls_of
                            cls_of[tid] = c
                            dirty.add(tid)
                            samples.setdefault(tid, TrackSamples()).add(fr.bgr, (d.x1, d.y1, d.x2, d.y2), d.conf)
                    fast = idx > start and n_conf >= FAST_MIN_DETS and n_new / n_conf > FAST_NEW_RATIO
                    if fast:
                        if cur_interval > 1:
                            emit(event("warning", job_id, stage="detect", code="W_FAST_MOTION",
                                       message=f"f{idx}: 빠른 움직임(새 트랙 {n_new}/{n_conf}) — 매 프레임 검출로 전환"))
                        cur_interval, fast_until = 1, idx + hold
                    elif cur_interval == 1 and idx > fast_until:
                        cur_interval = interval
                    next_det = idx + cur_interval
                if cur_interval == 1 and interval > 1:
                    fast_frames += 1
                last_idx = idx
                done_frames += 1
                if prog_t.ready():
                    el = time.monotonic() - t0
                    fps = done_frames / el if el > 0 else 0.0
                    eta = int((total - idx - 1) / fps) if fps > 0 and total else 0
                    emit(event("progress", job_id, stage="detect", frame=idx + 1, total=total, fps=fps, eta_s=eta,
                               gpu_util=gpu.util(), vram_mb=gpu.vram_mb()))
                    counts = {c: 0 for c in CLASSES}
                    for c in cls_of.values():
                        counts[c] += 1
                    emit(event("stats", job_id, counts={"faces": counts["face"], "persons": counts["person"],
                                                        "plates": counts["plate"]}))
                if prev_t.ready():
                    emit(event("preview", job_id, frame=idx, preview_jpeg=preview_jpeg(fr.bgr, last_dets)))
                if time.monotonic() - ck_t0 >= CHECKPOINT_S:
                    checkpoint(idx + 1)
                    ck_t0 = time.monotonic()
        except Cancelled:
            checkpoint(last_idx + 1)
            project.upsert_job(job_id, "analyze", status="CANCELLED")
            project.save(reg.model_hashes())
            raise
        finally:
            gpu.stop()

        # identify / link / save
        emit(event("progress", job_id, stage="identify", frame=total, total=total))
        project.add_frame_pts(pts_rows)
        rows: list[TrackRow] = []
        for c, tr in trackers.items():
            for t in tr.flush():
                if t.obs:
                    rows.append(_to_row(t, c, samples.get(t.tid)))
        # 재개 시 이전 구간 트랙도 포함해 링크 계산
        new_ids = {r.id for r in rows}
        prev = [t for t in project.tracks(with_boxes=True) if t.id not in new_ids]
        allrows = prev + rows
        links = link_faces_to_persons([t for t in allrows if t.cls == "face"], [t for t in allrows if t.cls == "person"])
        for r in allrows:
            if r.cls == "face":
                r.linked_person_id = links.get(r.id)
        emit(event("progress", job_id, stage="save", frame=total, total=total))
        thumbs = [(r, samples[r.id].thumb) for r in rows if r.id in samples and samples[r.id].thumb]
        for (r, _), name in zip(thumbs, project.write_thumbs([(r.id, j) for r, j in thumbs]), strict=True):
            r.thumb = name
        project.write_tracks(rows)
        for r in prev:
            if r.cls == "face":
                project.update_track_fields(r.id, linked_person_id=r.linked_person_id)
        rules_mod.apply(project)  # deny-by-default 초기 판정
        stats = _stats(project)
        el = time.monotonic() - t0
        stats["analyze_fps"] = round(done_frames / el, 2) if el > 0 else 0.0
        stats["fast_motion_frames"] = fast_frames
        if gpu.last:
            stats["gpu"] = {"name": gpu.last["name"], "peak_util": round(gpu.peak_util, 2), "peak_vram_mb": gpu.peak_vram}
        project.upsert_job(job_id, "analyze", status="ANALYZED", checkpoint_frame=last_idx + 1,
                           ended_at=time.strftime("%Y-%m-%dT%H:%M:%S"), stats=stats)
        project.save(reg.model_hashes())
        emit(event("log", job_id, stage="save",
                   message=f"tracks: F{stats['faces']} P{stats['persons']} LP{stats['plates']} · 병합 제안 {stats['merge_suggestions']}"))
        emit(event("done", job_id, stage="analyze", frame=total, total=total, fps=stats["analyze_fps"],
                   counts={k: v for k, v in stats.items() if isinstance(v, int) and not isinstance(v, bool)},
                   output_path=str(project_path)))
        return stats
    finally:
        project.close()


def _stats(project: Project) -> dict[str, Any]:
    from worker.pipeline.link import merge_suggestions

    tracks = project.tracks(with_boxes=True)
    dec = project.decisions()
    return {
        "faces": sum(t.cls == "face" for t in tracks),
        "persons": sum(t.cls == "person" for t in tracks),
        "plates": sum(t.cls == "plate" for t in tracks),
        "protected": sum(1 for d in dec.values() if d["protected"]),
        "review": sum(1 for d in dec.values() if d["flag"] == "REVIEW"),
        "merge_suggestions": len(merge_suggestions(tracks, project.media().get("fps") or 30.0)),
    }
