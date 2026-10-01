"""렌더링 패스 (docs/03 패스 2): rules → 마스크 계획 → 마스킹 → 인코딩 → 노출 재검사.

트랙 DB와 규칙만으로 출력하며 분석을 다시 하지 않는다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from worker.errors import Cancelled, CodecError
from worker.io.project import Project
from worker.jobs import Emit, JobControl, Throttle, event
from worker.models.registry import Registry, default_registry
from worker.pipeline import rules as rules_mod
from worker.pipeline.analyze import preview_jpeg
from worker.pipeline.audit_check import audit
from worker.pipeline.decode import DecoderThread
from worker.pipeline.encode import VideoWriter
from worker.pipeline.mask import RenderProfile, Watermark, apply_masks, plan_for_project
from worker.pipeline.probe import sha256_file


def render(project_path: str | Path, output_path: str | Path, profile: dict[str, Any] | RenderProfile,
           job_id: str, ctl: JobControl, emit: Emit, run_audit: bool = True,
           registry: Registry | None = None) -> dict[str, Any]:
    reg = registry or default_registry()
    prof = profile if isinstance(profile, RenderProfile) else RenderProfile.from_dict(profile)
    output_path = Path(output_path)
    project = Project.open(project_path)
    try:
        media = project.media()
        src = Path(media["path"])
        emit(event("progress", job_id, stage="ingest", frame=0, total=media["frames"]))
        if not src.exists():
            raise CodecError(f"원본 영상을 찾을 수 없습니다: {src}")
        if media.get("sha256") and sha256_file(src) != media["sha256"]:
            raise CodecError("원본 영상이 분석 이후 변경되었습니다 (SHA-256 불일치) — 다시 분석하세요")
        if output_path.resolve() == src.resolve():
            raise CodecError("출력 경로가 원본과 같습니다")
        if not project.decisions():
            rules_mod.apply(project)
        project.save_render_profile(prof.__dict__)
        project.set_meta("render_profile", json.dumps(prof.__dict__, ensure_ascii=False))
        project.upsert_job(job_id, "render", status="RENDERING")
        plan = plan_for_project(project, prof)
        total = project.frame_count() or int(media["frames"])
        counts = plan.count_tracks()
        emit(event("log", job_id, stage="render",
                   message=f"마스킹 트랙: 얼굴 {counts.get('face', 0)} · 번호판 {counts.get('plate', 0)} · "
                           f"머리 {counts.get('head', 0)} · 전신 {counts.get('person', 0)} · 수동 {counts.get('manual', 0)}"))

        def warn(code: str, msg: str) -> None:
            emit(event("warning", job_id, stage="encode", code=code, message=msg))

        writer = VideoWriter(output_path, media, prof.codec, prof.quality, prof.strip_meta, prof.keep_audio, warn)
        emit(event("log", job_id, stage="encode", message=f"encoder: {writer.encoder} · {writer.vs.bit_rate // 1000} kbps"))
        wm = Watermark(prof.watermark, writer.w, writer.h) if prof.watermark else None
        prog, prev = Throttle(0.5), Throttle(0.5)
        t0 = time.monotonic()
        n = 0
        try:
            for fr in DecoderThread(src, warn=lambda c, m: emit(event("warning", job_id, stage="decode", code=c, message=m))):
                ctl.check()
                img = apply_masks(fr.bgr, plan.at(fr.index), prof)
                if wm:
                    img = wm.apply(img)
                writer.write(img, fr.pts)
                n += 1
                if prog.ready():
                    el = time.monotonic() - t0
                    fps = n / el if el > 0 else 0.0
                    emit(event("progress", job_id, stage="render", frame=n, total=total, fps=fps,
                               eta_s=int((total - n) / fps) if fps > 0 else 0, gpu_util=-1.0, vram_mb=-1))
                if prev.ready():
                    emit(event("preview", job_id, frame=fr.index, preview_jpeg=preview_jpeg(img)))
            writer.close()
        except Cancelled:
            writer.abort()
            project.upsert_job(job_id, "render", status="CANCELLED")
            project.save()
            raise
        except BaseException:
            writer.abort()
            raise
        render_fps = n / max(time.monotonic() - t0, 1e-6)
        digest = sha256_file(output_path)
        exposures: list[dict[str, Any]] = []
        audited = False
        if run_audit:
            emit(event("progress", job_id, stage="audit", frame=0, total=total))
            stride = int(project.get_meta("detect_interval") or 1)
            face_ls = reg.profile(project.get_meta("profile") or "cpu").face_long_side
            exposures = audit(output_path, plan, job_id, ctl, emit, total=total, stride=stride,
                              face_long_side=face_ls, pad_frames=prof.pad_frames, src_path=src, profile=prof, registry=reg)
            audited = True
        status = ("AUDITED" if not exposures else "REVIEWING") if audited else "RENDERED"
        project.upsert_job(job_id, "render", status=status, output_path=str(output_path), output_sha256=digest,
                           audit_exposures=len(exposures) if audited else -1,
                           ended_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                           stats={"render_fps": round(render_fps, 2), "frames": n, "encoder": writer.encoder,
                                  "mask_tracks": counts})
        project.set_meta("last_audit", json.dumps({"output": str(output_path), "exposures": exposures[:500]},
                                                  ensure_ascii=False))
        project.save(reg.model_hashes())
        for e in exposures[:200]:  # 검수 목록용 (UI)
            emit(event("exposure", job_id, stage="audit", frame=e["frame"], code=e["cls"],
                       message=json.dumps({k: e[k] for k in ("x", "y", "w", "h", "conf")})))
        if exposures:
            emit(event("error", job_id, stage="audit", code="E_AUDIT_EXPOSURE",
                       message=f"노출 재검사에서 보호대상 외 얼굴 {len(exposures)}건이 검출되었습니다 — 검수가 필요합니다"))
        result = {"output_path": str(output_path), "sha256": digest, "exposures": exposures, "audited": audited,
                  "render_fps": render_fps, "frames": n, "encoder": writer.encoder, "mask_tracks": counts}
        emit(event("done", job_id, stage="render", frame=n, total=total, fps=render_fps, output_path=str(output_path),
                   output_sha256=digest, audit_exposures=len(exposures) if audited else -1))
        return result
    finally:
        project.close()


def audit_only(project_path: str | Path, output_path: str | Path, job_id: str = "audit",
               ctl: JobControl | None = None, emit: Emit | None = None,
               registry: Registry | None = None) -> list[dict[str, Any]]:
    """기관 모드 재검증: 저장된 렌더 프로파일로 계획을 다시 만들어 재검사만 수행."""
    reg = registry or default_registry()
    project = Project.open(project_path)
    try:
        prof = RenderProfile.from_dict(json.loads(project.get_meta("render_profile") or "{}"))
        plan = plan_for_project(project, prof)
        stride = int(project.get_meta("detect_interval") or 1)
        face_ls = reg.profile(project.get_meta("profile") or "cpu").face_long_side
        return audit(output_path, plan, job_id, ctl, emit, total=project.frame_count(), stride=stride,
                     face_long_side=face_ls, pad_frames=prof.pad_frames, src_path=project.media()["path"],
                     profile=prof, registry=reg)
    finally:
        project.close()
