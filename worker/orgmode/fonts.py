"""한글 폰트 탐색 (보고서 PDF·워터마크).

우선순위: NURIBLUR_FONT 환경변수 → packaging/fonts/(배포 시 Noto Sans KR 등 OFL 폰트 번들) → Windows 맑은 고딕.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = [
    ROOT / "packaging" / "fonts" / "NotoSansKR-Regular.ttf",
    ROOT / "packaging" / "fonts" / "Pretendard-Regular.ttf",
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgun.ttf",
    Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
]


@lru_cache(maxsize=1)
def korean_font_path() -> Path | None:
    env = os.environ.get("NURIBLUR_FONT")
    if env and Path(env).exists():
        return Path(env)
    for p in CANDIDATES:
        if p.exists():
            return p
    return None


def load_pil_font(size: int):
    from PIL import ImageFont

    p = korean_font_path()
    if p:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            pass
    return ImageFont.load_default()
