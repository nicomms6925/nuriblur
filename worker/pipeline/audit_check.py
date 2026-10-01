"""audit_check (G1-10) — 노출 재검사.

출력본을 검출기(conf 0.2)로 다시 훑어 보호대상이 아닌 얼굴이 보이면 노출로 센다. 두 가지를 검사한다.

1. 노출 검사(합의): 출력 프레임 그대로(A)와, 마스크 영역을 회색으로 덮은 프레임(B)에 각각 검출기를 돌려
   B의 검출 중 A에도 같은 자리에 검출이 있는 것만 노출로 본다.
   - A만 쓰면 검출기가 모자이크 자체를 저신뢰 얼굴로 다시 잡아 마스크를 더해도 끝나지 않는 오탐 루프가 생긴다.
   - B만 쓰면 회색 경계 모서리가 작은 오검출을 만든다(평균색으로 덮으면 피부색 덩어리가 얼굴처럼 잡힌다).
   - 실제로 보이는 얼굴은 A·B 모두에서 잡힌다. (docs/11 §3) 보호 트랙 박스(±pad_frames)와 IoU ≥0.3 이거나,
   검출 박스의 70% 이상이 보호 박스(×1.25) 안에 있으면(보호 얼굴의 일부가 이웃 마스크에 가려 따로 잡힌 경우) 제외.
   (모자이크를 그대로 두고 검출하면 검출기가 모자이크 자체를 저신뢰 얼굴로 다시 잡아,
    마스크를 더해도 끝나지 않는 오탐 루프가 생긴다 — docs/11 §3)
2. 무결성 검사: 원본을 함께 디코딩해 같은 계획으로 '기대 출력'을 만들고, 마스크 영역마다
   출력이 기대 출력보다 원본에 더 가까우면(=마스킹이 빠짐) 'mask_missing' 노출로 센다.
   마스킹해도 거의 변하지 않는 평탄한 영역(패딩 프레임 등)은 판정에서 제외한다.
   1번에서 마스크 영역을 덮어 버리므로, 인코딩·프레임 정렬 오류로 마스크가 빠진 경우를 여기서 잡는다.

번호판은 audit_capable 번호판 모델(프로파일의 plate)이 있을 때 재검사한다. 단 회색 덮기 방식은 쓰지 않는다
— 번호판 검출기는 '사각형'을 보므로 회색으로 덮은 영역 자체를 번호판으로 잡는다. 대신 출력 그대로 검출해,
검출 박스가 마스크 영역에 80% 이상(또는 중심부 50%×50%가 90% 이상) 덮이면 가려진 것으로 본다.
번호판은 강체 직사각형이라 모자이크 재검출 박스가 마스크와 거의 일치한다(얼굴과 다름).
또한 1차 번호판 모델은 무늬 없는 사각형(창문·간판·모자이크)도 잡으므로, 박스 안에 글자 획처럼
고대비 세로 전이가 있어야만(text_like) 노출로 센다 — 글자가 보이지 않는 번호판은 식별 불가.
CPU 비용을 줄이려고 번호판 마스크가 있는 프레임(그 영역)과 PLATE_FULL_EVERY 프레임마다 전체 화면만 본다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from worker.jobs import Emit, JobControl, Throttle, event
from worker.models.registry import Registry, default_registry
from worker.pipeline.decode import iter_frames
from worker.pipeline.detect import CONF, RtdetrPlateDetector, YunetDetector
from worker.pipeline.mask import MaskPlan, Region, RenderProfile, apply_masks

AUDIT_CONF = 0.2
PROTECT_IOU = 0.3
PROTECT_IOA = 0.7
PROTECT_GROW = 1.25
MIN_CHANGE = 4.0
MISSING_RATIO = 0.4
PLATE_AUDIT_CONF = 0.2   # 보정 점수 기준(원점수 0.04), 분석 0.25보다 민감
PLATE_FULL_EVERY = 30
NEUTRAL = 128


def _iou(a: tuple, b: tuple) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _ioa(det: tuple, box: tuple) -> float:
    ix = max(0.0, min(det[2], box[2]) - max(det[0], box[0]))
    iy = max(0.0, min(det[3], box[3]) - max(det[1], box[1]))
    a = (det[2] - det[0]) * (det[3] - det[1])
    return ix * iy / a if a > 0 else 0.0


def _grow(b: tuple, k: float) -> tuple:
    cx, cy, w, h = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, (b[2] - b[0]) * k, (b[3] - b[1]) * k
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def is_protected(det: tuple, protected: list[tuple]) -> bool:
    return any(_iou(det, p) >= PROTECT_IOU or _ioa(det, _grow(p, PROTECT_GROW)) >= PROTECT_IOA for p in protected)


def _clip(img: np.ndarray, r: Region) -> tuple[int, int, int, int] | None:
    H, W = img.shape[:2]
    x1, y1 = max(0, int(np.floor(r.x1))), max(0, int(np.floor(r.y1)))
    x2, y2 = min(W, int(np.ceil(r.x2))), min(H, int(np.ceil(r.y2)))
    return (x1, y1, x2, y2) if x2 - x1 >= 2 and y2 - y1 >= 2 else None


def neutralize(img: np.ndarray, regions: list[Region]) -> np.ndarray:
    out = img.copy()
    for r in regions:
        c = _clip(out, r)
        if c:
            out[c[1]:c[3], c[0]:c[2]] = NEUTRAL
    return out


def agrees(b: tuple, raw: list[tuple]) -> bool:
    return any(_iou(b, a) >= 0.3 or _ioa(b, a) >= 0.5 for a in raw)


def unchanged_regions(out: np.ndarray, src: np.ndarray, regions: list[Region],
                      profile: RenderProfile) -> list[Region]:
    if out.shape != src.shape:
        h, w = min(out.shape[0], src.shape[0]), min(out.shape[1], src.shape[1])
        out, src = out[:h, :w], src[:h, :w]
    expected = apply_masks(src.copy(), regions, profile)
    bad = []
    for r in regions:
        c = _clip(out, r)
        if not c or min(c[2] - c[0], c[3] - c[1]) < 16:  # 블록 1~2개짜리 작은 영역은 압축 잡음이 커서 판정 불가
            continue
        sl = (slice(c[1], c[3]), slice(c[0], c[2]))
        o = out[sl].astype(np.int16)
        e = expected[sl].astype(np.int16)
        s_ = src[sl].astype(np.int16)
        if float(np.abs(e - s_).mean()) < MIN_CHANGE:
            continue  # 마스킹해도 변화가 거의 없는 평탄 영역
        # 마스크가 빠졌다면 출력≈원본(차이는 압축 잡음 수준)이고, 들어갔다면 출력-원본 차이≈기대-원본 차이.
        # 압축이 심한 작은 영역에서 오탐이 없도록 '기대 변화량의 40% 미만'일 때만 빠진 것으로 본다.
        if float(np.abs(o - s_).mean()) < MISSING_RATIO * float(np.abs(e - s_).mean()):
            bad.append(r)
    return bad


TEXT_MIN_CONTRAST = 60.0
TEXT_MIN_TRANSITIONS = 6.0


def text_like(crop: np.ndarray) -> bool:
    """번호판 글자 획 판별: 높이 32로 맞춘 뒤 가운데 띠에서 줄당 강한 명암 전이 수.

    실측(합성 신형 번호판): 실제 17.4 / 모자이크된 번호판 4.0 / 단색 사각형 3.0 / 모자이크 얼굴 0.7
    """
    import cv2

    if crop.size == 0 or min(crop.shape[:2]) < 6:
        return False
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (max(8, int(32 * g.shape[1] / g.shape[0])), 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    band = g[8:24]
    rng = float(np.percentile(band, 95) - np.percentile(band, 5))
    if rng < TEXT_MIN_CONTRAST:
        return False
    trans = float((np.abs(np.diff(band, axis=1)) / rng > 0.35).sum(1).mean())
    return trans >= TEXT_MIN_TRANSITIONS


def covered(box: tuple[float, float, float, float], regions: list[Region]) -> bool:
    from worker.pipeline.mask import coverage

    if not regions:
        return False
    if coverage(box, regions) >= 0.8:
        return True
    # 마스크 영역을 거의 통째로 품은 검출(모자이크 + 범퍼·바퀴 등 주변까지 잡은 박스)
    area = max((box[2] - box[0]) * (box[3] - box[1]), 1e-6)
    for r in regions:
        ra = max((r.x2 - r.x1) * (r.y2 - r.y1), 1e-6)
        ix = max(0.0, min(box[2], r.x2) - max(box[0], r.x1))
        iy = max(0.0, min(box[3], r.y2) - max(box[1], r.y1))
        if ix * iy / ra >= 0.8 and ra / area >= 0.15:
            return True
    x1, y1, x2, y2 = box
    mx, my = (x2 - x1) * 0.25, (y2 - y1) * 0.25
    return coverage((x1 + mx, y1 + my, x2 - mx, y2 - my), regions) >= 0.9


def _audit_plates(plate, p_scale: float, fr, regions: list[Region], plan: MaskPlan, pad: int,
                  stride: int = 1) -> list[dict]:
    H, W = fr.bgr.shape[:2]
    rois = []
    pr = [r for r in regions if r.kind in ("plate", "manual")]
    if pr:
        x1, y1 = min(r.x1 for r in pr), min(r.y1 for r in pr)
        x2, y2 = max(r.x2 for r in pr), max(r.y2 for r in pr)
        px, py = (x2 - x1) * 0.5 + 20, (y2 - y1) * 0.5 + 20
        rois.append((max(0, int(x1 - px)), max(0, int(y1 - py)), min(W, int(x2 + px)), min(H, int(y2 + py))))
    if fr.index % PLATE_FULL_EVERY < stride:  # 30프레임 창마다 재검사 프레임 1장은 전체 화면
        rois.append((0, 0, W, H))
    out = []
    for roi in rois:
        for d in plate(fr.bgr, p_scale, roi=roi):
            box = (d.x1, d.y1, d.x2, d.y2)
            if is_protected(box, protected_near(plan, fr.index, pad)) or plan.is_excluded(fr.index, box):
                continue
            if covered(box, regions):
                continue
            if not text_like(fr.bgr[int(d.y1):int(d.y2), int(d.x1):int(d.x2)]):
                continue
            if any(abs(e["x"] - d.x1) < 2 and abs(e["y"] - d.y1) < 2 for e in out):
                continue
            out.append({"frame": fr.index, "cls": "plate", "x": d.x1, "y": d.y1, "w": d.x2 - d.x1,
                        "h": d.y2 - d.y1, "conf": d.conf})
    return out


def protected_near(plan: MaskPlan, frame: int, window: int) -> list[tuple[float, float, float, float]]:
    out = []
    for f in range(frame - window, frame + window + 1):
        out += plan.protected.get(f, [])
    return out


def audit(output_path: str | Path, plan: MaskPlan, job_id: str = "", ctl: JobControl | None = None,
          emit: Emit | None = None, total: int = 0, stride: int = 1, face_long_side: int = 960,
          pad_frames: int = 5, src_path: str | Path | None = None, profile: RenderProfile | None = None,
          plate_model: str = "", registry: Registry | None = None) -> list[dict[str, Any]]:
    reg = registry or default_registry()
    face = YunetDetector(reg.get("face_yunet_2023mar"), face_long_side)
    plate = None
    if plate_model:
        spec = reg.get(plate_model)
        if spec.audit_capable and spec.arch == "rtdetr":
            plate = RtdetrPlateDetector(spec)
    p_scale = PLATE_AUDIT_CONF / CONF["plate"]
    scale = AUDIT_CONF / CONF["face"]
    exposures: list[dict[str, Any]] = []
    prog = Throttle(0.5)
    stride = max(1, stride)
    src_iter = iter_frames(src_path) if src_path else None
    n_out = 0
    for fr in iter_frames(output_path):
        if ctl:
            ctl.check()
        n_out += 1
        src = next(src_iter, None) if src_iter is not None else None
        regions = plan.at(fr.index)
        if src is not None and regions:
            for r in unchanged_regions(fr.bgr, src.bgr, regions, profile or RenderProfile()):
                exposures.append({"frame": fr.index, "cls": "mask_missing", "x": r.x1, "y": r.y1,
                                  "w": r.x2 - r.x1, "h": r.y2 - r.y1, "conf": 1.0})
        # 분석 검출 프레임 '사이'(보간 프레임)를 본다 — 누출은 주로 보간·이동 구간에서 생긴다
        if (fr.index + stride // 2) % stride == 0:
            cands = face(neutralize(fr.bgr, regions), conf_scale=scale) if regions else None
            raw = [(d.x1, d.y1, d.x2, d.y2) for d in face(fr.bgr, conf_scale=scale)]
            for d in (cands if cands is not None else face(fr.bgr, conf_scale=scale)):
                box = (d.x1, d.y1, d.x2, d.y2)
                if is_protected(box, protected_near(plan, fr.index, pad_frames)):
                    continue
                if plan.is_excluded(fr.index, box):
                    continue
                if cands is not None and not agrees(box, raw):
                    continue
                exposures.append({"frame": fr.index, "cls": "face", "x": d.x1, "y": d.y1,
                                  "w": d.x2 - d.x1, "h": d.y2 - d.y1, "conf": d.conf})
            if plate is not None:
                exposures += _audit_plates(plate, p_scale, fr, regions, plan, pad_frames, stride)
        if emit and prog.ready():
            emit(event("progress", job_id, stage="audit", frame=fr.index + 1, total=total))
    if total and n_out != total:
        exposures.append({"frame": n_out, "cls": "frame_count", "x": 0, "y": 0, "w": 0, "h": 0,
                          "conf": 1.0, "detail": f"출력 프레임 수 {n_out} ≠ 원본 {total}"})
    return exposures
