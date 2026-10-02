"""mask (G1-08) — 렌더링 계획 생성과 마스킹 적용.

계획: frame -> [Region]. 판정이 '보호'가 아닌 얼굴·번호판 트랙과 수동 박스, (옵션) 전신/머리 영역.
- 박스 ×pad_ratio(기본 1.25) 확장, 좌표 EMA α=0.6 — 단, EMA 지연으로 노출되지 않도록 원 박스와 **합집합**.
- 트랙 앞뒤 pad_frames(기본 5) 프레임 연장.
- 번호판 장변 <20px → 박스 ×3 폴백.
- 얼굴이 검출되지 않은 비보호 전신은 머리 영역(상단 25%)을 보수적으로 마스킹 (mask_head_when_no_face, 기본 on).
- region(exclude 다각형)·timerange 규칙은 해당 영역/구간의 마스크를 뺀다(사용자 명시 행위).

스타일 하한: pixelate block ≥8px, gaussian σ ≥15.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from worker.io.project import Project, TrackRow

EMA_ALPHA = 0.6
MIN_BLOCK = 8
MIN_SIGMA = 15.0
PLATE_MIN_PX = 20
HEAD_FRAC = 0.25


@dataclass
class RenderProfile:
    style: str = "pixelate"          # pixelate | gaussian | solid | segment(V2)
    strength: float = 8.0            # 얼굴 폭 / strength = 블록 크기 (σ = 폭×2/strength)
    pad_ratio: float = 1.25
    pad_frames: int = 5
    codec: str = "source"            # source | h264 | hevc
    quality: str = "source"          # source | high | normal
    strip_meta: bool = True
    watermark: str = ""
    mask_body_when_face_masked: bool = False
    mask_head_when_no_face: bool = True
    keep_audio: bool = True
    mask_all_unprotected: bool = False   # 보호대상 외 전체 가리기: 비보호 사람 전신 + 비보호 차량 전체

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> RenderProfile:
        p = cls()
        for k, v in (d or {}).items():
            if hasattr(p, k) and v is not None and v != "":
                setattr(p, k, type(getattr(p, k))(v))
        if p.style == "segment":  # V2 — 지원 전까지 픽셀화로 대체
            p.style = "pixelate"
        p.strength = float(min(max(p.strength, 2.0), 32.0))
        p.pad_ratio = float(min(max(p.pad_ratio, 1.0), 2.0))
        p.pad_frames = int(min(max(p.pad_frames, 0), 60))
        return p


@dataclass
class Region:
    x1: float
    y1: float
    x2: float
    y2: float
    kind: str          # face | plate | person | head | manual
    track_id: int = 0

    def xyxy(self) -> tuple[float, float, float, float]:
        return self.x1, self.y1, self.x2, self.y2


@dataclass
class MaskPlan:
    regions: dict[int, list[Region]] = field(default_factory=dict)
    protected: dict[int, list[tuple[float, float, float, float]]] = field(default_factory=dict)
    # 사용자 제외 규칙(region/timerange) — 재검사도 보호와 같이 취급
    excluded_polys: list[list[list[float]]] = field(default_factory=list)
    excluded_ranges: list[tuple[int, int]] = field(default_factory=list)

    def is_excluded(self, frame: int, box: tuple[float, float, float, float]) -> bool:
        if any(a <= frame <= b for a, b in self.excluded_ranges):
            return True
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return any(_point_in_poly(cx, cy, poly) for poly in self.excluded_polys)

    def at(self, frame: int) -> list[Region]:
        return self.regions.get(frame, [])

    def add(self, frame: int, r: Region) -> None:
        self.regions.setdefault(frame, []).append(r)

    def count_tracks(self) -> dict[str, int]:
        seen: dict[str, set[int]] = {}
        for rs in self.regions.values():
            for r in rs:
                seen.setdefault(r.kind, set()).add(r.track_id)
        return {k: len(v) for k, v in seen.items()}


def _expand(x: float, y: float, w: float, h: float, ratio: float) -> tuple[float, float, float, float]:
    cx, cy = x + w / 2, y + h / 2
    w2, h2 = w * ratio / 2, h * ratio / 2
    return cx - w2, cy - h2, cx + w2, cy + h2


def _point_in_poly(px: float, py: float, poly: list[list[float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > py) != (y2 > py) and px < (x2 - x1) * (py - y1) / ((y2 - y1) or 1e-9) + x1:
            inside = not inside
    return inside


def _velocity(boxes: dict, frames: list[int]) -> tuple[float, float]:
    """프레임당 중심 이동량. 관측이 1개뿐이면 0."""
    if len(frames) < 2 or frames[-1] == frames[0]:
        return 0.0, 0.0
    a, b = boxes[frames[0]], boxes[frames[-1]]
    n = frames[-1] - frames[0]
    return ((b[0] + b[2] / 2) - (a[0] + a[2] / 2)) / n, ((b[1] + b[3] / 2) - (a[1] + a[3] / 2)) / n


def _padded(b: tuple, v: tuple[float, float], k: int, ratio: float, kind: str, tid: int) -> Region:
    sx1, sy1, sx2, sy2 = _expand(*b[:4], ratio)
    dx, dy = v[0] * k, v[1] * k
    return Region(min(sx1, sx1 + dx), min(sy1, sy1 + dy), max(sx2, sx2 + dx), max(sy2, sy2 + dy),
                  kind=kind, track_id=tid)


def interpolate_keyframes(frames: list[list[float]]) -> dict[int, tuple[float, float, float, float]]:
    """수동 박스 키프레임 [[f,x,y,w,h],...] → 사이 프레임 선형 보간."""
    kf = sorted(([int(f[0])] + [float(v) for v in f[1:5]] for f in frames), key=lambda r: r[0])
    out: dict[int, tuple[float, float, float, float]] = {}
    for a, b in zip(kf, kf[1:] + [None], strict=True):
        out[a[0]] = (a[1], a[2], a[3], a[4])
        if b is None:
            continue
        for f in range(a[0] + 1, b[0]):
            t = (f - a[0]) / (b[0] - a[0])
            out[f] = tuple(a[i] + (b[i] - a[i]) * t for i in range(1, 5))  # type: ignore[assignment]
    return out


def build_plan(tracks: list[TrackRow], decisions: dict[int, dict[str, Any]], rules: list[dict[str, Any]],
               profile: RenderProfile, n_frames: int, fps: float, plate_stride: int = 0) -> MaskPlan:
    plan = MaskPlan()
    masked = {t.id for t in tracks if not (decisions.get(t.id) or {}).get("protected")}
    from worker.pipeline.rules import not_object_ids

    # '객체 아님'은 가리지 않지만 재검사 보호 영역에도 넣지 않는다 — 실제 얼굴·번호판이면 재검사가 잡는다
    protected_ids = {t.id for t in tracks} - masked - not_object_ids(tracks, rules)
    masked_faces = {t.id for t in tracks if t.cls == "face" and t.id in masked}
    persons_with_masked_face = {t.linked_person_id for t in tracks if t.id in masked_faces and t.linked_person_id}
    last = max(n_frames - 1, 0)

    face_boxes_at: dict[int, list[tuple[float, float, float, float]]] = {}
    for t in tracks:
        if t.cls == "face":
            for f, b in t.boxes.items():
                face_boxes_at.setdefault(f, []).append((b[0], b[1], b[2], b[3]))
        if t.id in protected_ids and t.cls != "vehicle":  # 차량 박스는 커서 운전자 얼굴 재검사까지 빼 버린다
            for f, b in t.boxes.items():
                plan.protected.setdefault(f, []).append((b[0], b[1], b[0] + b[2], b[1] + b[3]))

    for t in tracks:
        if t.id not in masked or not t.boxes:
            continue
        body = t.cls == "person" and (profile.mask_all_unprotected or
                                      (profile.mask_body_when_face_masked and t.id in persons_with_masked_face))
        if t.cls == "vehicle" and not profile.mask_all_unprotected:
            continue  # 기본은 번호판만 가린다
        if t.cls == "person" and not (body or profile.mask_head_when_no_face):
            continue
        frames = sorted(t.boxes)
        ema = None
        for f in frames:
            x, y, w, h = t.boxes[f][:4]
            if t.cls == "plate" and max(w, h) < PLATE_MIN_PX:
                x, y, w, h = x - w, y - h, w * 3, h * 3
            if t.cls == "person":
                if body:
                    kind = "person"
                else:
                    # 머리 영역에 얼굴(마스킹이든 보호든)이 있으면 생략
                    hx1, hy1, hx2, hy2 = x, y, x + w, y + h * 0.4
                    has_face = False
                    for fx, fy, fw, fh in face_boxes_at.get(f, []):
                        ix = max(0.0, min(fx + fw, hx2) - max(fx, hx1))
                        iy = max(0.0, min(fy + fh, hy2) - max(fy, hy1))
                        if ix * iy >= 0.5 * fw * fh:
                            has_face = True
                            break
                    if has_face:
                        ema = None
                        continue
                    kind = "head"
                    x, y, w, h = x + w * 0.1, y, w * 0.8, h * HEAD_FRAC
            else:
                kind = t.cls
            raw = _expand(x, y, w, h, profile.pad_ratio)
            ema = raw if ema is None else tuple(EMA_ALPHA * r + (1 - EMA_ALPHA) * e for r, e in zip(raw, ema, strict=True))
            box = (min(raw[0], ema[0]), min(raw[1], ema[1]), max(raw[2], ema[2]), max(raw[3], ema[3]))
            plan.add(f, Region(*box, kind=kind, track_id=t.id))
        # 앞뒤 패딩 (얼굴·번호판 트랙): 제자리 박스 ∪ 트랙 속도로 외삽한 박스
        # (빠르게 들어오거나 나가는 객체, 짧게 끊긴 트랙에서 마스크가 뒤처지지 않도록)
        # 번호판은 번호판 모델 실행 주기(plate_stride)만큼 시작·끝이 불확실하다 → 패딩을 그 이상으로
        pad = max(profile.pad_frames, plate_stride) if t.cls == "plate" else profile.pad_frames
        if t.cls != "person" and pad > 0:
            first, lastf = frames[0], frames[-1]
            fb = t.boxes[first]
            lb = t.boxes[lastf]
            v_in = _velocity(t.boxes, frames[: min(len(frames), 5)])
            v_out = _velocity(t.boxes, frames[-min(len(frames), 5):])
            for k in range(1, pad + 1):
                if first - k >= 0 and first - k not in t.boxes:
                    plan.add(first - k, _padded(fb, v_in, -k, profile.pad_ratio, t.cls, t.id))
                if lastf + k <= last and lastf + k not in t.boxes:
                    plan.add(lastf + k, _padded(lb, v_out, k, profile.pad_ratio, t.cls, t.id))

    # 수동 박스 (항상 마스킹). payload.track_id 가 있으면 그 트랙 전 구간(예: 이 구간 전신 마스킹)
    by_id = {t.id: t for t in tracks}
    for r in rules:
        if r["kind"] == "manual_box" and r["payload"].get("track_id") in by_id:
            t = by_id[r["payload"]["track_id"]]
            for f, b in t.boxes.items():
                plan.add(f, Region(*_expand(*b[:4], profile.pad_ratio), kind="manual", track_id=-int(r["id"] or 0)))
        elif r["kind"] == "manual_box":
            for f, (x, y, w, h) in interpolate_keyframes(r["payload"].get("frames", [])).items():
                if 0 <= f <= last:
                    plan.add(f, Region(*_expand(x, y, w, h, profile.pad_ratio), kind="manual", track_id=-int(r["id"] or 0)))

    # 제외 규칙
    for r in rules:
        p = r.get("payload") or {}
        if r["kind"] == "region" and p.get("mode", "exclude") == "exclude" and len(p.get("polygon", [])) >= 3:
            poly = p["polygon"]
            plan.excluded_polys.append(poly)
            for f in list(plan.regions):
                plan.regions[f] = [g for g in plan.regions[f]
                                   if g.kind == "manual" or not _point_in_poly((g.x1 + g.x2) / 2, (g.y1 + g.y2) / 2, poly)]
        elif r["kind"] == "timerange":
            a = int(float(p.get("start_ms", 0)) / 1000 * fps)
            b = int(float(p.get("end_ms", 0)) / 1000 * fps)
            plan.excluded_ranges.append((a, b))
            for f in range(max(a, 0), min(b, last) + 1):
                plan.regions[f] = [g for g in plan.regions.get(f, []) if g.kind == "manual"]
    return plan


def plan_for_project(project: Project, profile: RenderProfile) -> MaskPlan:
    media = project.media()
    n = project.frame_count() or int(media.get("frames") or 0)
    return build_plan(project.tracks(with_boxes=True), project.decisions(), project.rules(), profile,
                      n, float(media.get("fps") or 30.0), plate_stride=int(project.get_meta("plate_stride") or 0))


# ---------------- 적용 ----------------
def _clip(img: np.ndarray, r: Region) -> tuple[int, int, int, int] | None:
    H, W = img.shape[:2]
    x1, y1 = max(0, int(np.floor(r.x1))), max(0, int(np.floor(r.y1)))
    x2, y2 = min(W, int(np.ceil(r.x2))), min(H, int(np.ceil(r.y2)))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return x1, y1, x2, y2


def apply_masks(img: np.ndarray, regions: list[Region], profile: RenderProfile) -> np.ndarray:
    for r in regions:
        c = _clip(img, r)
        if c is None:
            continue
        x1, y1, x2, y2 = c
        roi = img[y1:y2, x1:x2]
        w, h = x2 - x1, y2 - y1
        ref = max(r.x2 - r.x1, 1.0)
        if profile.style == "solid":
            roi[:] = 0
        elif profile.style == "gaussian":
            sigma = max(MIN_SIGMA, ref * 2.0 / profile.strength)
            k = int(sigma * 3) | 1
            roi[:] = cv2.GaussianBlur(roi, (k, k), sigma, borderType=cv2.BORDER_REPLICATE)
        else:
            block = max(MIN_BLOCK, int(round(ref / profile.strength)))
            sw, sh = max(1, w // block), max(1, h // block)
            small = cv2.resize(roi, (sw, sh), interpolation=cv2.INTER_AREA)
            roi[:] = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    return img


class Watermark:
    """열람 전용 워터마크(반투명, 한글). Pillow로 한 번 그려 매 프레임 합성."""

    def __init__(self, text: str, width: int, height: int):
        from PIL import Image, ImageDraw

        from worker.orgmode.fonts import load_pil_font

        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        size = max(14, height // 30)
        font = load_pil_font(size)
        pad = size // 2
        bbox = d.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y = width - tw - pad * 2, height - th - pad * 2
        d.rectangle([x - pad, y - pad, x + tw + pad, y + th + pad * 1.5], fill=(0, 0, 0, 90))
        d.text((x, y), text, font=font, fill=(255, 255, 255, 170))
        rgba = np.array(layer)
        self.alpha = (rgba[:, :, 3:4].astype(np.float32) / 255.0)
        self.color = rgba[:, :, 2::-1].astype(np.float32)  # RGB→BGR
        ys, xs = np.where(rgba[:, :, 3] > 0)
        self.box = (ys.min(), ys.max() + 1, xs.min(), xs.max() + 1) if len(ys) else None

    def apply(self, img: np.ndarray) -> np.ndarray:
        if self.box is None:
            return img
        a, b, c, d = self.box
        roi = img[a:b, c:d].astype(np.float32)
        al = self.alpha[a:b, c:d]
        img[a:b, c:d] = (roi * (1 - al) + self.color[a:b, c:d] * al).astype(np.uint8)
        return img


def coverage(box: tuple[float, float, float, float], regions: list[Region], grid: int = 24) -> float:
    """box(xyxy) 면적 중 regions 합집합이 덮는 비율 (격자 샘플링). 누락률 측정용."""
    x1, y1, x2, y2 = box
    if x2 <= x1 or y2 <= y1 or not regions:
        return 0.0
    xs = np.linspace(x1, x2, grid, endpoint=False) + (x2 - x1) / grid / 2
    ys = np.linspace(y1, y2, grid, endpoint=False) + (y2 - y1) / grid / 2
    gx, gy = np.meshgrid(xs, ys)
    covered = np.zeros_like(gx, dtype=bool)
    for r in regions:
        covered |= (gx >= r.x1) & (gx <= r.x2) & (gy >= r.y1) & (gy <= r.y2)
    return float(covered.mean())
