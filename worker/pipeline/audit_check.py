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

번호판은 audit_capable 번호판 모델이 manifest에 있을 때만 재검사한다(규칙 기반 폴백은 재검사 불가).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from worker.jobs import Emit, JobControl, Throttle, event
from worker.models.registry import Registry, default_registry
from worker.pipeline.decode import iter_frames
from worker.pipeline.detect import CONF, YunetDetector
from worker.pipeline.mask import MaskPlan, Region, RenderProfile, apply_masks

AUDIT_CONF = 0.2
PROTECT_IOU = 0.3
PROTECT_IOA = 0.7
PROTECT_GROW = 1.25
MIN_CHANGE = 4.0
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
        if not c or (c[2] - c[0]) * (c[3] - c[1]) < 64:
            continue
        sl = (slice(c[1], c[3]), slice(c[0], c[2]))
        o = out[sl].astype(np.int16)
        e = expected[sl].astype(np.int16)
        s_ = src[sl].astype(np.int16)
        if float(np.abs(e - s_).mean()) < MIN_CHANGE:
            continue  # 마스킹해도 변화가 거의 없는 평탄 영역
        if float(np.abs(o - s_).mean()) < float(np.abs(o - e).mean()):
            bad.append(r)
    return bad


def protected_near(plan: MaskPlan, frame: int, window: int) -> list[tuple[float, float, float, float]]:
    out = []
    for f in range(frame - window, frame + window + 1):
        out += plan.protected.get(f, [])
    return out


def audit(output_path: str | Path, plan: MaskPlan, job_id: str = "", ctl: JobControl | None = None,
          emit: Emit | None = None, total: int = 0, stride: int = 1, face_long_side: int = 960,
          pad_frames: int = 5, src_path: str | Path | None = None, profile: RenderProfile | None = None,
          registry: Registry | None = None) -> list[dict[str, Any]]:
    reg = registry or default_registry()
    face = YunetDetector(reg.get("face_yunet_2023mar"), face_long_side)
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
        if fr.index % stride == 0:
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
        if emit and prog.ready():
            emit(event("progress", job_id, stage="audit", frame=fr.index + 1, total=total))
    if total and n_out != total:
        exposures.append({"frame": n_out, "cls": "frame_count", "x": 0, "y": 0, "w": 0, "h": 0,
                          "conf": 1.0, "detail": f"출력 프레임 수 {n_out} ≠ 원본 {total}"})
    return exposures
