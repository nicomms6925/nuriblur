"""ONNX 검출기 (G1-03).

클래스: face / person / plate (+ 내부용 vehicle).
- 얼굴: YuNet(640×640 고정 입력) — 전체 프레임 1회 + 고해상도 타일(작은 얼굴용)
- 전신·차량: YOLOX(COCO) — 레터박스
- 번호판: 전용 모델이 없으면 차량 박스 하단 영역(규칙) → 보수적 마스킹
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from worker.models.registry import ModelSpec, Profile, Registry, create_session, default_registry

CONF = {"face": 0.30, "person": 0.35, "plate": 0.25, "vehicle": 0.35}
NMS_IOU = 0.5


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
        # 2) 고해상도 타일 (작은 얼굴)
        scale = min(self.long_side / max(H, W), 2.0)
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
        if self.plate_spec.arch != "rule_vehicle_lower":
            raise NotImplementedError("번호판 ONNX 모델 연결은 G4-01에서 구현")

    @property
    def plate_audit_capable(self) -> bool:
        return self.plate_spec.audit_capable

    def __call__(self, bgr: np.ndarray, conf_scale: float = 1.0) -> list[Det]:
        dets: list[Det] = []
        if self.face is not None:
            dets += self.face(bgr, conf_scale)
        if self.obj is not None:
            od = self.obj(bgr, conf_scale)
            if "person" in self.classes:
                dets += [d for d in od if d.cls == "person"]
            if "plate" in self.classes:
                dets += plates_from_vehicles([d for d in od if d.cls == "vehicle"])
        return dets
