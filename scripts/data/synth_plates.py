"""합성 국내 번호판 생성 (docs/07 '합성 데이터') — 회귀 클립·학습 보강용.

유형: new_white(신형 8자리 흰색) · old_white(구형 7자리) · old_green(구형 녹색) · commercial_yellow(영업용)
      · ev_blue(전기차 하늘색) · two_line(2단형)

  python scripts/data/synth_plates.py --out .scratch/plates --n 200      # 이미지 + labels.json(텍스트·유형)
"""
from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HANGUL_USE = "가나다라마거너더러머버서어저고노도로모보소오조구누두루무부수우주하허호"
HANGUL_COMMERCIAL = "바사아자배"
REGIONS = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]

STYLES = {
    "new_white": {"bg": (255, 255, 255), "fg": (0, 0, 0), "size": (520, 110)},
    "old_white": {"bg": (255, 255, 255), "fg": (0, 0, 0), "size": (335, 155)},
    "old_green": {"bg": (36, 120, 70), "fg": (255, 255, 255), "size": (335, 170)},
    "commercial_yellow": {"bg": (240, 200, 40), "fg": (0, 0, 0), "size": (335, 170)},
    "ev_blue": {"bg": (95, 170, 230), "fg": (0, 0, 0), "size": (520, 110)},
    "two_line": {"bg": (240, 200, 40), "fg": (0, 0, 0), "size": (335, 170)},
}


def _font(size: int):
    for p in (os.environ.get("NURIBLUR_FONT", ""), "C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/malgun.ttf",
              "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"):
        if p and Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def plate_text(kind: str, rng: random.Random) -> str:
    h = rng.choice(HANGUL_COMMERCIAL if kind in ("commercial_yellow", "two_line") else HANGUL_USE)
    if kind in ("new_white", "ev_blue"):
        return f"{rng.randint(100, 399)}{h}{rng.randint(1000, 9999)}"
    return f"{rng.randint(10, 99)}{h}{rng.randint(1000, 9999)}"


def render_plate(text: str, kind: str = "new_white", rng: random.Random | None = None) -> np.ndarray:
    """RGB uint8 배열. 2단형은 상단에 지역명."""
    rng = rng or random.Random(0)
    st = STYLES[kind]
    w, h = st["size"]
    img = Image.new("RGB", (w, h), st["bg"])
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, w - 3, h - 3], radius=10, outline=st["fg"], width=4)
    if kind in ("new_white", "ev_blue"):
        f = _font(int(h * 0.78))
        tb = d.textbbox((0, 0), text, font=f)
        d.text(((w - (tb[2] - tb[0])) / 2 - tb[0], (h - (tb[3] - tb[1])) / 2 - tb[1]), text, font=f, fill=st["fg"])
    else:
        top = rng.choice(REGIONS) + " " + text[:2] if kind == "two_line" else text[:2]
        f1, f2 = _font(int(h * 0.36)), _font(int(h * 0.5))
        tb = d.textbbox((0, 0), top, font=f1)
        d.text(((w - (tb[2] - tb[0])) / 2 - tb[0], h * 0.06 - tb[1]), top, font=f1, fill=st["fg"])
        bottom = text[2:]
        tb = d.textbbox((0, 0), bottom, font=f2)
        d.text(((w - (tb[2] - tb[0])) / 2 - tb[0], h * 0.42 - tb[1]), bottom, font=f2, fill=st["fg"])
    return np.array(img)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    labels = []
    for i in range(a.n):
        kind = rng.choice(list(STYLES))
        text = plate_text(kind, rng)
        Image.fromarray(render_plate(text, kind, rng)).save(out / f"{i:05d}.png")
        labels.append({"file": f"{i:05d}.png", "text": text, "kind": kind})
    (out / "labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{a.n}장 생성: {out}")


if __name__ == "__main__":
    main()
