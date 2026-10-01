"""합성 회귀 클립 생성 (G0-06).

tests/data/assets/messi5.jpg 의 얼굴과 dog.jpg 의 차량을 잘라 배경 위에서 움직이는 영상을 만든다.
각 클립 옆에 GT(.gt.json: 프레임별 얼굴 박스)를 저장한다.

  python scripts/make_test_clips.py [--out tests/data/synthetic] [--seconds 6]

규약: tests/data/{genre}/{clip}.mp4 + {clip}.gt.json  (docs/10 G0-06)
GT 형식: {"fps":30,"frames":N,"width":W,"height":H,"objects":[{"id":1,"cls":"face","boxes":{"f":[x,y,w,h]}}]}
  - face 박스는 패치 안의 실제 얼굴 영역, 다른 패치에 50% 이상 가려진 프레임은 제외
"""
from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "tests" / "data" / "assets"


def _face_patch() -> np.ndarray:
    img = cv2.imread(str(ASSETS / "messi5.jpg"))
    return img[72:134, 205:276].copy()  # 얼굴 + 머리 주변 (유니폼 엠블럼 제외 — 0.2 재검사 오탐 방지)


# messi5.jpg 에서 얼굴 영역(225,93)-(255,131) → 패치 원점(205,72) 기준 오프셋
FACE_IN_PATCH = (20, 21, 30, 38)


def _car_patch() -> np.ndarray:
    img = cv2.imread(str(ASSETS / "dog.jpg"))
    return img[78:171, 466:692].copy()


def background(w: int, h: int) -> np.ndarray:
    bg = np.zeros((h, w, 3), np.uint8)
    for y in range(h):
        v = 70 + int(60 * y / h)
        bg[y] = (v + 10, v, v - 10)
    rng = np.random.default_rng(7)
    for _ in range(14):
        x, y = int(rng.integers(0, w - 120)), int(rng.integers(0, int(h * 0.55)))
        bw, bh = int(rng.integers(60, 200)), int(rng.integers(80, 260))
        c = tuple(int(v) for v in rng.integers(60, 140, 3))
        cv2.rectangle(bg, (x, y), (x + bw, y + bh), c, -1)
    cv2.rectangle(bg, (0, int(h * 0.72)), (w, h), (60, 62, 66), -1)
    return bg


def paste(dst: np.ndarray, patch: np.ndarray, x: int, y: int) -> tuple[int, int, int, int] | None:
    H, W = dst.shape[:2]
    ph, pw = patch.shape[:2]
    x1, y1, x2, y2 = max(0, x), max(0, y), min(W, x + pw), min(H, y + ph)
    if x2 <= x1 or y2 <= y1:
        return None
    dst[y1:y2, x1:x2] = patch[y1 - y:y2 - y, x1 - x:x2 - x]
    return x1, y1, x2 - x1, y2 - y1


def make_street(path: Path, seconds: float = 6.0, w: int = 1280, h: int = 720, fps: int = 30,
                rotation: int = 0, codec: str = "") -> dict:
    if not codec:
        from worker.pipeline.encode import select_encoder

        codec = select_encoder("h264", "h264")
    face = _face_patch()
    car = _car_patch()
    bg = background(w, h)
    n = int(seconds * fps)
    # (id, scale, x0, y0, vx, vy, t_in, t_out)
    actors = [
        (1, 1.6, 120, 200, 3.0, 0.3, 0, n),          # 보호 대상 후보 (전 구간)
        (2, 1.2, 900, 160, -2.5, 0.5, 0, n),
        (3, 1.0, 560, 330, 0.0, -0.4, int(n * 0.3), int(n * 0.7)),  # 중간에 잠깐 등장
        (4, 2.2, 700, 380, -1.2, -0.6, int(n * 0.5), n),
    ]
    gt = {"fps": fps, "frames": n, "width": w, "height": h, "objects": []}
    objs = {a[0]: {"id": a[0], "cls": "face", "boxes": {}} for a in actors}
    objs[100] = {"id": 100, "cls": "vehicle", "boxes": {}}
    path.parent.mkdir(parents=True, exist_ok=True)
    out = av.open(str(path), "w")
    vs = out.add_stream(codec, rate=fps)
    vs.width, vs.height = (h, w) if rotation in (90, 270) else (w, h)
    vs.pix_fmt = "nv12" if codec.endswith("_mf") else "yuv420p"
    vs.bit_rate = 6_000_000
    if rotation:
        vs.set_display_rotation(rotation)
    for i in range(n):
        fr = bg.copy()
        zbuf = np.zeros((h, w), np.int32)
        cx = int(-260 + (w + 300) * i / n)
        paste(fr, car, cx, int(h * 0.68))
        objs[100]["boxes"][str(i)] = [cx, int(h * 0.68), car.shape[1], car.shape[0]]
        for aid, sc, x0, y0, vx, vy, tin, tout in actors:
            if not (tin <= i < tout):
                continue
            p = cv2.resize(face, None, fx=sc, fy=sc, interpolation=cv2.INTER_LINEAR)
            x = int(x0 + vx * (i - tin))
            y = int(y0 + vy * (i - tin))
            b = paste(fr, p, x, y)
            if b:
                zbuf[b[1]:b[1] + b[3], b[0]:b[0] + b[2]] = aid
                fx, fy, fw, fh = (v * sc for v in FACE_IN_PATCH)
                objs[aid]["boxes"][str(i)] = [x + fx, y + fy, fw, fh]
        for aid, *_ in actors:  # 가려짐·화면 밖 처리
            bb = objs[aid]["boxes"].get(str(i))
            if bb is None:
                continue
            x1, y1 = int(max(0, bb[0])), int(max(0, bb[1]))
            x2, y2 = int(min(w, bb[0] + bb[2])), int(min(h, bb[1] + bb[3]))
            vis = (zbuf[y1:y2, x1:x2] == aid).sum() / max(bb[2] * bb[3], 1) if x2 > x1 and y2 > y1 else 0
            if vis < 0.5:
                del objs[aid]["boxes"][str(i)]
        img = fr
        if rotation:  # 저장은 회전 전 방향으로 → 플레이어가 rotation만큼 반시계 회전해 바로 봄
            img = np.ascontiguousarray(np.rot90(fr, -(rotation // 90)))
        vf = av.VideoFrame.from_ndarray(img, format="bgr24").reformat(format=vs.pix_fmt)
        vf.pts = i
        vf.time_base = Fraction(1, fps)
        for pkt in vs.encode(vf):
            out.mux(pkt)
    for pkt in vs.encode(None):
        out.mux(pkt)
    out.close()
    gt["objects"] = list(objs.values())
    path.with_suffix(".gt.json").write_text(json.dumps(gt), encoding="utf-8")
    return gt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tests" / "data" / "synthetic"))
    ap.add_argument("--seconds", type=float, default=6.0)
    a = ap.parse_args()
    out = Path(a.out)
    make_street(out / "street_faces.mp4", a.seconds)
    print("생성:", out / "street_faces.mp4")


if __name__ == "__main__":
    main()
