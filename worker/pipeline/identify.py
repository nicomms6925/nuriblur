"""identify (G1-05 일부) — 트랙 썸네일과 외형 특징.

ArcFace 자체 학습(G4-02) 전까지는 신원 임베딩이 아니라 **외형 특징**(색 히스토그램 + 저해상도 명암)을
병합 제안에만 쓴다. 보호 판정(신원 매칭)에는 쓰지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

THUMB = 96
TOP_K = 3


def sharpness(crop: np.ndarray) -> float:
    if crop.size == 0:
        return 0.0
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def crop_box(img: np.ndarray, x1: float, y1: float, x2: float, y2: float, pad: float = 0.0) -> np.ndarray:
    H, W = img.shape[:2]
    w, h = x2 - x1, y2 - y1
    a = max(0, int(x1 - w * pad))
    b = max(0, int(y1 - h * pad))
    c = min(W, int(x2 + w * pad))
    d = min(H, int(y2 + h * pad))
    return img[b:d, a:c]


def appearance(crop: np.ndarray) -> np.ndarray:
    if crop.size == 0:
        return np.zeros(16 * 4 + 16 * 16, np.float32)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 4], [0, 180, 0, 256]).ravel()
    hist /= hist.sum() + 1e-9
    g = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32)
    g = (g - g.mean()) / (g.std() + 1e-6) / 16.0
    v = np.concatenate([np.sqrt(hist), g.ravel()]).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def thumb_jpeg(crop: np.ndarray) -> bytes:
    if crop.size == 0:
        return b""
    h, w = crop.shape[:2]
    s = THUMB / max(h, w)
    t = cv2.resize(crop, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    canvas = np.full((THUMB, THUMB, 3), 32, np.uint8)
    y0, x0 = (THUMB - t.shape[0]) // 2, (THUMB - t.shape[1]) // 2
    canvas[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = t
    ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else b""


@dataclass
class TrackSamples:
    """트랙별로 선명도 상위 K개 크롭과 최고 신뢰도 썸네일을 유지."""

    best: list[tuple[float, np.ndarray]] = field(default_factory=list)  # (sharpness, feature)
    thumb_conf: float = -1.0
    thumb: bytes = b""

    def add(self, img: np.ndarray, box: tuple[float, float, float, float], conf: float) -> None:
        crop = crop_box(img, *box)
        if crop.size == 0 or min(crop.shape[:2]) < 6:
            return
        s = sharpness(crop)
        if len(self.best) < TOP_K or s > self.best[-1][0]:
            self.best.append((s, appearance(crop)))
            self.best.sort(key=lambda x: -x[0])
            del self.best[TOP_K:]
        if conf > self.thumb_conf:
            self.thumb_conf = conf
            self.thumb = thumb_jpeg(crop_box(img, *box, pad=0.15))

    def embedding(self) -> bytes | None:
        if not self.best:
            return None
        v = np.mean([f for _, f in self.best], axis=0)
        v = v / (np.linalg.norm(v) + 1e-9)
        return v.astype(np.float32).tobytes()


def cosine(a: bytes | None, b: bytes | None) -> float:
    if not a or not b:
        return 0.0
    va = np.frombuffer(a, np.float32)
    vb = np.frombuffer(b, np.float32)
    if va.shape != vb.shape:
        return 0.0
    return float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-9))
