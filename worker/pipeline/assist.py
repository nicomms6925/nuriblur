"""검수 보조 — 사용자가 한 프레임에서 지정한 객체를 앞뒤로 자동 추적 (놓친 얼굴·번호판 수동 마스킹).

방법(프레임마다):
1. 직전 박스 주변 ROI(×3)에서 해당 종류 검출기를 낮은 임계로 실행 → 크기가 비슷하고 가장 많이 겹치는 검출 채택
   (face: YuNet, plate: 번호판 모델, other: 검출기 없음)
2. 검출이 없으면 마지막으로 확실했던 모습을 템플릿으로 ROI에서 정규화 상관 매칭(≥0.55)
3. 둘 다 실패가 연속 LOST_LIMIT 프레임이면 그 방향 추적 종료
결과는 manual_box 규칙의 프레임별 박스로 저장되어 렌더링 때 항상 마스킹된다.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import av
import cv2
import numpy as np

from worker.io.project import Project
from worker.models.registry import Registry, default_registry
from worker.pipeline.decode import upright

LOST_LIMIT = 6
TEMPLATE_MIN = 0.55


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / u if u > 0 else 0.0


def _frames(project: Project, a: int, b: int) -> Iterator[tuple[int, np.ndarray]]:
    """프레임 a..b(포함)를 순서대로. frame_pts 표로 키프레임 seek 후 디코딩."""
    media = project.media()
    c = av.open(media["path"])
    try:
        vs = c.streams.video[0]
        pts_a = project.frame_pts(a)
        if pts_a is not None:
            c.seek(pts_a, stream=vs, backward=True, any_frame=False)
            pts_to_idx = {project.frame_pts(i): i for i in range(a, b + 1)}
            for fr in c.decode(vs):
                i = pts_to_idx.get(fr.pts)
                if fr.pts is not None and fr.pts > max(k for k in pts_to_idx if k is not None):
                    return
                if i is not None:
                    yield i, upright(fr.to_ndarray(format="bgr24"), int(round(fr.rotation or 0)) % 360)
        else:
            for i, fr in enumerate(c.decode(vs)):
                if i > b:
                    return
                if i >= a:
                    yield i, upright(fr.to_ndarray(format="bgr24"), int(round(fr.rotation or 0)) % 360)
    finally:
        c.close()


class _Stepper:
    def __init__(self, cls: str, reg: Registry, profile_name: str):
        self.cls = cls
        self.det: Callable[[np.ndarray, tuple], list] | None = None
        if cls == "face":
            from worker.pipeline.detect import YunetDetector

            d = YunetDetector(reg.get("face_yunet_2023mar"), long_side=0)

            def run(img, roi):
                x0, y0, x1, y1 = roi
                crop = img[y0:y1, x0:x1]
                return [(r.x1 + x0, r.y1 + y0, r.x2 + x0, r.y2 + y0, r.conf) for r in d(crop, conf_scale=0.5)]

            self.det = run
        elif cls == "plate":
            from worker.pipeline.detect import RtdetrPlateDetector

            spec = reg.get(reg.profile(profile_name).plate)
            if spec.arch == "rtdetr":
                p = RtdetrPlateDetector(spec)
                self.det = lambda img, roi: [(r.x1, r.y1, r.x2, r.y2, r.conf) for r in p(img, 0.5, roi=roi)]
        self.template: np.ndarray | None = None

    def roi(self, box, W, H, k: float = 1.5):
        w, h = box[2] - box[0], box[3] - box[1]
        return (max(0, int(box[0] - k * w)), max(0, int(box[1] - k * h)),
                min(W, int(box[2] + k * w)), min(H, int(box[3] + k * h)))

    def set_template(self, img, box) -> None:
        x1, y1, x2, y2 = (int(round(v)) for v in box)
        t = img[max(0, y1):y2, max(0, x1):x2]
        if t.size and min(t.shape[:2]) >= 4:
            self.template = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)

    def step(self, img, box) -> tuple[tuple, str] | None:
        H, W = img.shape[:2]
        roi = self.roi(box, W, H)
        bw, bh = box[2] - box[0], box[3] - box[1]
        if self.det is not None:
            best, score = None, 0.0
            for d in self.det(img, roi):
                dw, dh = d[2] - d[0], d[3] - d[1]
                if not (0.5 <= dw / max(bw, 1) <= 2.0 and 0.5 <= dh / max(bh, 1) <= 2.0):
                    continue
                cx, cy = (d[0] + d[2]) / 2, (d[1] + d[3]) / 2
                dist = np.hypot(cx - (box[0] + box[2]) / 2, cy - (box[1] + box[3]) / 2) / max(bw, bh, 1)
                s = _iou(d, box) + max(0.0, 1.0 - dist) * 0.5
                if s > score and dist < 1.5:
                    best, score = d, s
            if best is not None:
                b = best[:4]
                self.set_template(img, b)
                return b, "detector"
        if self.template is not None:
            x0, y0, x1, y1 = roi
            area = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            th, tw = self.template.shape[:2]
            if area.shape[0] > th and area.shape[1] > tw:
                res = cv2.matchTemplate(area, self.template, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(res)
                if mx >= TEMPLATE_MIN:
                    nb = (x0 + loc[0], y0 + loc[1], x0 + loc[0] + tw, y0 + loc[1] + th)
                    return nb, "template"
        return None


def track_object(project: Project, frame: int, box_xywh: tuple[float, float, float, float], cls: str,
                 max_frames: int = 300, both: bool = True, registry: Registry | None = None,
                 profile_name: str | None = None) -> dict[str, Any]:
    reg = registry or default_registry()
    prof = profile_name or project.get_meta("profile") or "cpu"
    n = project.frame_count() or int(project.media()["frames"])
    x, y, w, h = box_xywh
    start = (x, y, x + w, y + h)
    out: dict[int, tuple] = {frame: start}
    methods: dict[str, int] = {}

    def run(order: list[int]) -> None:
        st = _Stepper(cls, reg, prof)
        box, lost = start, 0
        first = True
        frames = dict(_frames(project, min(order), max(order)))
        for i in order:
            img = frames.get(i)
            if img is None:
                break
            if first:
                st.set_template(img, box)
                first = False
                continue
            r = st.step(img, box)
            if r is None:
                lost += 1
                if lost >= LOST_LIMIT:
                    break
                continue
            lost = 0
            box, m = r
            methods[m] = methods.get(m, 0) + 1
            out[i] = tuple(float(v) for v in box)

    fwd = list(range(frame, min(n - 1, frame + max_frames) + 1))
    run(fwd)
    if both and frame > 0:
        back = list(range(frame, max(0, frame - max_frames) - 1, -1))
        run(back)
    frames_sorted = sorted(out)
    return {"start_f": frames_sorted[0], "end_f": frames_sorted[-1],
            "boxes": [[f, out[f][0], out[f][1], out[f][2] - out[f][0], out[f][3] - out[f][1]] for f in frames_sorted],
            "methods": methods}
