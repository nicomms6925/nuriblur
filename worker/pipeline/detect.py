"""ONNX 검출기 (G1-03).

클래스: face / person / plate (+ 내부용 vehicle).
- 얼굴: YuNet(640×640 고정 입력) — 전체 프레임 1회 + 고해상도 타일(작은 얼굴용)
- 전신·차량: YOLOX(COCO) — 레터박스
- 번호판: ONNX 번호판 모델(RT-DETR)을 plate_every 검출마다 전체 화면과 차량 영역(차량이 있고, 차량 영역이
  화면의 ROI_MAX_AREA 미만이라 확대 효과가 있을 때)에 실행. 차량 검출과 무관하게 전체 화면도 돌린다 —
  작은 차량은 YOLOX-nano가 놓치기 때문. 번호판을 찾지 못한 차량은 차량 박스 하단 영역(규칙)으로
  보수적 마스킹(deny-by-default). 번호판 모델을 건너뛴 검출에서는 번호판을 내지 않는다
  (트래커가 앞뒤 번호판 검출을 이어 보간 — CPU 프로파일 plate_every=2, RT-DETR CPU 1회 ≈0.3초).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from worker.models.registry import ModelSpec, Profile, Registry, create_session, default_registry

# face: 스펙 0.30 → 0.15. 재검사(0.20)보다 낮게 두어 임계 부근에서 깜빡이는 작은 얼굴에 여유(히스테리시스)를 준다
# (군중 실영상에서 0.2~0.3 얼굴이 대량 노출 — docs/10 열린 질문)
CONF = {"face": 0.15, "person": 0.35, "plate": 0.25, "vehicle": 0.35}
NMS_IOU = 0.5
ROI_MAX_AREA = 0.6
EDGE_MARGIN = 0.02   # 화면 가장자리에 걸친 차량 판정(장변 대비)   # 차량 영역이 화면의 60% 이상이면 전체 화면 검출과 해상도 차이가 거의 없어 생략


@dataclass
class Det:
    cls: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def xywh(self) -> tuple[float, float, float, float]:
        return self.x1, self.y1, self.x2 - self.x1, self.y2 - self.y1


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][ovr <= iou]
    return keep


def letterbox(img: np.ndarray, size: int) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    out = np.full((size, size, 3), 114, dtype=np.uint8)
    out[:nh, :nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return out, r


class YoloxDetector:
    """YOLOX COCO → person / vehicle."""

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.sess = create_session(spec)
        self.input_name = self.sess.get_inputs()[0].name
        self.size = spec.input_size
        grids, strides = [], []
        for s in (8, 16, 32):
            n = self.size // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        self.grids = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)

    def __call__(self, bgr: np.ndarray, conf_scale: float = 1.0) -> list[Det]:
        img, r = letterbox(bgr, self.size)
        blob = img.transpose(2, 0, 1)[None].astype(np.float32)
        out = self.sess.run(None, {self.input_name: blob})[0][0]
        xy = (out[:, :2] + self.grids) * self.strides
        wh = np.exp(out[:, 2:4]) * self.strides
        obj = out[:, 4:5]
        cls_scores = out[:, 5:] * obj
        dets: list[Det] = []
        for coco_id, name in self.spec.classes.items():
            sc = cls_scores[:, coco_id]
            m = sc >= CONF[name] * conf_scale
            if not m.any():
                continue
            c = xy[m]
            s = wh[m]
            boxes = np.concatenate([c - s / 2, c + s / 2], 1) / r
            scores = sc[m]
            for i in nms(boxes, scores, NMS_IOU):
                b = boxes[i]
                dets.append(Det(name, float(scores[i]), *map(float, b)))
        # vehicle은 COCO 여러 클래스 → 클래스 무관 NMS 한 번 더
        veh = [d for d in dets if d.cls == "vehicle"]
        if len(veh) > 1:
            b = np.array([[d.x1, d.y1, d.x2, d.y2] for d in veh])
            keep = set(nms(b, np.array([d.conf for d in veh]), NMS_IOU))
            dets = [d for d in dets if d.cls != "vehicle"] + [veh[i] for i in sorted(keep)]
        return dets


class YunetDetector:
    """YuNet 얼굴 검출 (입력 640×640 고정). 큰 프레임은 타일링."""

    TILE = 640
    OVERLAP = 96

    def __init__(self, spec: ModelSpec, long_side: int = 960):
        self.spec = spec
        self.sess = create_session(spec)
        self.input_name = self.sess.get_inputs()[0].name
        self.out_names = [o.name for o in self.sess.get_outputs()]
        self.long_side = long_side
        self.size = spec.input_size
        self.anchor = {}
        for s in (8, 16, 32):
            n = self.size // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            self.anchor[s] = np.stack((xv.ravel(), yv.ravel()), 1).astype(np.float32)

    def _run(self, tile: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        blob = tile.transpose(2, 0, 1)[None].astype(np.float32)
        outs = dict(zip(self.out_names, self.sess.run(None, {self.input_name: blob}), strict=True))
        boxes, scores = [], []
        for s in (8, 16, 32):
            cls = np.clip(outs[f"cls_{s}"][0, :, 0], 0, 1)
            obj = np.clip(outs[f"obj_{s}"][0, :, 0], 0, 1)
            sc = np.sqrt(cls * obj)
            m = sc > 0.05
            if not m.any():
                continue
            bb = outs[f"bbox_{s}"][0][m]
            a = self.anchor[s][m]
            cx = (a[:, 0] + bb[:, 0]) * s
            cy = (a[:, 1] + bb[:, 1]) * s
            w = np.exp(bb[:, 2]) * s
            h = np.exp(bb[:, 3]) * s
            boxes.append(np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 1))
            scores.append(sc[m])
        if not boxes:
            return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)
        return np.concatenate(boxes), np.concatenate(scores)

    def __call__(self, bgr: np.ndarray, conf_scale: float = 1.0) -> list[Det]:
        H, W = bgr.shape[:2]
        all_b, all_s = [], []
        # 1) 전체 프레임 (큰 얼굴)
        img, r = letterbox(bgr, self.size)
        b, s = self._run(img)
        all_b.append(b / r)
        all_s.append(s)
        # 2) 고해상도 타일 (작은 얼굴). 원본보다 줄이지 않는다(군중 속 작은 얼굴 보존) — 단 4K 초과는 2560으로
        native_cap = min(1.0, 2560 / max(H, W))
        scale = min(max(self.long_side / max(H, W), native_cap), 2.0) if self.long_side else 0.0
        if max(H, W) * scale > self.size * 1.15:
            big = cv2.resize(bgr, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_LINEAR)
            bh, bw = big.shape[:2]
            step = self.TILE - self.OVERLAP
            ys = list(range(0, max(1, bh - self.OVERLAP), step)) or [0]
            xs = list(range(0, max(1, bw - self.OVERLAP), step)) or [0]
            for y0 in ys:
                for x0 in xs:
                    tile = np.full((self.TILE, self.TILE, 3), 0, np.uint8)
                    crop = big[y0:y0 + self.TILE, x0:x0 + self.TILE]
                    tile[:crop.shape[0], :crop.shape[1]] = crop
                    b, s = self._run(tile)
                    if len(b):
                        b = (b + np.array([x0, y0, x0, y0], np.float32)) / scale
                        all_b.append(b)
                        all_s.append(s)
        boxes = np.concatenate(all_b)
        scores = np.concatenate(all_s)
        thr = CONF["face"] * conf_scale
        m = scores >= thr
        boxes, scores = boxes[m], scores[m]
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, W)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, H)
        ok = ((boxes[:, 2] - boxes[:, 0]) >= 4) & ((boxes[:, 3] - boxes[:, 1]) >= 4)
        boxes, scores = boxes[ok], scores[ok]
        return [Det("face", float(scores[i]), *map(float, boxes[i])) for i in nms(boxes, scores, 0.4)]


class RtdetrPlateDetector:
    """RT-DETR 번호판 검출 (입력 640×640 고정, RGB/255, NMS 불필요).

    점수가 압축된 단일 클래스 모델이라 manifest의 score_scale로 보정한다.
    """

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.sess = create_session(spec)
        self.input_name = self.sess.get_inputs()[0].name
        self.size = spec.input_size
        self.scale = float(spec.extra.get("score_scale", 1.0))

    def __call__(self, bgr: np.ndarray, conf_scale: float = 1.0,
                 roi: tuple[int, int, int, int] | None = None) -> list[Det]:
        H, W = bgr.shape[:2]
        x0, y0, x1, y1 = roi or (0, 0, W, H)
        crop = bgr[y0:y1, x0:x1]
        if crop.size == 0:
            return []
        ch, cw = crop.shape[:2]
        x = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), (self.size, self.size), interpolation=cv2.INTER_LINEAR)
        x = (x.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
        logits, boxes = self.sess.run(None, {self.input_name: x})
        sc = np.clip(1.0 / (1.0 + np.exp(-logits[0, :, 0])) * self.scale, 0, 1)
        m = sc >= CONF["plate"] * conf_scale
        if not m.any():
            return []
        b = boxes[0][m]
        xyxy = np.stack([(b[:, 0] - b[:, 2] / 2) * cw + x0, (b[:, 1] - b[:, 3] / 2) * ch + y0,
                         (b[:, 0] + b[:, 2] / 2) * cw + x0, (b[:, 1] + b[:, 3] / 2) * ch + y0], 1)
        s = sc[m]
        out = []
        for i in nms(xyxy, s, 0.5):
            x1, y1, x2, y2 = map(float, xyxy[i])
            w, h = x2 - x1, y2 - y1
            # RT-DETR이 가끔 내는 화면 크기 박스 제거: 번호판일 수 없는 크기·종횡비
            # 국내 번호판 종횡비: 신형 4.7 · 구형/2단 약 2 · 이륜차 약 1.5 → 1.2 미만(정사각형에 가까움)은 제외
            if w <= 2 or h <= 2 or w > 0.3 * W or h > 0.3 * H or not (1.2 <= w / h <= 8.0):
                continue
            out.append(Det("plate", float(s[i]), max(0.0, x1), max(0.0, y1), min(float(W), x2), min(float(H), y2)))
        return out


def vehicle_roi(vehicles: list[Det], W: int, H: int, pad: float = 0.1) -> tuple[int, int, int, int]:
    x1 = min(v.x1 for v in vehicles)
    y1 = min(v.y1 for v in vehicles)
    x2 = max(v.x2 for v in vehicles)
    y2 = max(v.y2 for v in vehicles)
    px, py = (x2 - x1) * pad, (y2 - y1) * pad
    return max(0, int(x1 - px)), max(0, int(y1 - py)), min(W, int(x2 + px)), min(H, int(y2 + py))


def _inside(p: Det, v: Det) -> bool:
    ix = max(0.0, min(p.x2, v.x2) - max(p.x1, v.x1))
    iy = max(0.0, min(p.y2, v.y2) - max(p.y1, v.y1))
    return ix * iy >= 0.6 * max((p.x2 - p.x1) * (p.y2 - p.y1), 1e-6)


def plates_from_vehicles(vehicles: list[Det]) -> list[Det]:
    """번호판 전용 모델 부재 시 규칙: 차량 박스 하단 중앙 영역을 번호판 후보로.

    전·후면/사선 모두 덮도록 가로 80%, 세로 하단 45%를 잡는다(과마스킹은 안전한 쪽).
    """
    out = []
    for v in vehicles:
        w, h = v.x2 - v.x1, v.y2 - v.y1
        x1 = v.x1 + 0.10 * w
        x2 = v.x2 - 0.10 * w
        y1 = v.y1 + 0.55 * h
        y2 = v.y2
        out.append(Det("plate", v.conf, x1, y1, x2, y2))
    return out


class DetectorSet:
    """프로파일 하나에 해당하는 검출기 묶음."""

    def __init__(self, profile: Profile, classes: tuple[str, ...] = ("face", "person", "plate"),
                 registry: Registry | None = None):
        reg = registry or default_registry()
        self.profile = profile
        self.classes = set(classes)
        self.face_spec = reg.get(profile.face)
        self.object_spec = reg.get(profile.object)
        self.plate_spec = reg.get(profile.plate)
        self.face = YunetDetector(self.face_spec, profile.face_long_side) if "face" in self.classes else None
        need_obj = bool({"person", "plate"} & self.classes)
        self.obj = YoloxDetector(self.object_spec) if need_obj else None
        self.plate = None
        if "plate" in self.classes and self.plate_spec.arch == "rtdetr":
            self.plate = RtdetrPlateDetector(self.plate_spec)
            if profile.plate_fallback:
                reg.get(profile.plate_fallback)  # 라이선스 검증
        elif "plate" in self.classes and self.plate_spec.arch != "rule_vehicle_lower":
            raise NotImplementedError(f"지원하지 않는 번호판 모델 구조: {self.plate_spec.arch}")
        self.fallback = self.plate is None or bool(profile.plate_fallback)
        self.plate_every = max(1, int(profile.plate_every or 1))
        self._calls = 0

    @property
    def plate_audit_capable(self) -> bool:
        return self.plate_spec.audit_capable

    def __call__(self, bgr: np.ndarray, conf_scale: float = 1.0) -> list[Det]:
        self._calls += 1
        dets: list[Det] = []
        if self.face is not None:
            dets += self.face(bgr, conf_scale)
        if self.obj is not None:
            od = self.obj(bgr, conf_scale)
            if "person" in self.classes:
                dets += [d for d in od if d.cls == "person"]
            if "plate" in self.classes:
                vehicles = [d for d in od if d.cls == "vehicle"]
                dets += vehicles  # 차량 트랙(보호대상 지정·'보호대상 외 전체 가리기'용)
                plates = self.plates(bgr, vehicles, conf_scale)
                # 1차 번호판 모델은 얼굴도 번호판으로 잡는다 → 얼굴 검출과 겹치는 번호판은 버린다
                # (그 얼굴은 얼굴 트랙으로 마스킹되고, 보호된 얼굴이 가짜 번호판 마스크에 가려지지 않게)
                faces = [d for d in dets if d.cls == "face"]
                if faces:
                    fb = np.array([[f.x1, f.y1, f.x2, f.y2] for f in faces])
                    pb = np.array([[q.x1, q.y1, q.x2, q.y2] for q in plates]).reshape(-1, 4)
                    from worker.pipeline.track import iou_matrix

                    ov = iou_matrix(pb, fb).max(1) if len(pb) else np.zeros(0)

                    def holds_face(q) -> bool:  # 번호판 박스가 얼굴의 절반 이상을 품음
                        for f in faces:
                            ix = max(0.0, min(q.x2, f.x2) - max(q.x1, f.x1))
                            iy = max(0.0, min(q.y2, f.y2) - max(q.y1, f.y1))
                            if ix * iy >= 0.5 * (f.x2 - f.x1) * (f.y2 - f.y1):
                                return True
                        return False

                    plates = [q for q, o in zip(plates, ov, strict=True) if o < 0.3 and not holds_face(q)]
                dets += plates
        return dets

    def plates(self, bgr: np.ndarray, vehicles: list[Det], conf_scale: float = 1.0, full: bool | None = None) -> list[Det]:
        if self.plate is None:
            return plates_from_vehicles(vehicles)
        H, W = bgr.shape[:2]
        if not (full if full is not None else (self._calls - 1) % self.plate_every == 0):
            # 번호판 모델을 건너뛴 검출: 화면 안쪽 번호판은 트랙 보간으로 이어지지만, 화면 가장자리에 걸친 차량은
            # 실행 주기 사이에 번호판이 들어오거나 나간다 → 그 차량만 하단 영역 규칙으로 가린다
            if not self.fallback:
                return []
            m = EDGE_MARGIN * max(W, H)
            return plates_from_vehicles([v for v in vehicles
                                         if v.x1 <= m or v.y1 <= m or v.x2 >= W - m or v.y2 >= H - m])
        found: list[Det] = self.plate(bgr, conf_scale)
        if vehicles:
            roi = vehicle_roi(vehicles, W, H)
            if (roi[2] - roi[0]) * (roi[3] - roi[1]) < ROI_MAX_AREA * W * H:
                found += self.plate(bgr, conf_scale, roi=roi)
        if len(found) > 1:
            b = np.array([[d.x1, d.y1, d.x2, d.y2] for d in found])
            found = [found[i] for i in nms(b, np.array([d.conf for d in found]), 0.5)]
        if not self.fallback:
            return found
        missing = [v for v in vehicles if not any(_inside(p, v) for p in found)]
        return found + plates_from_vehicles(missing)
