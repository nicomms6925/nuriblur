"""사용자 노출 문구는 ko.json 에만 둔다 (CLAUDE.md)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=4)
def _table(lang: str = "ko") -> dict[str, str]:
    return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def tr(key: str, **kw) -> str:
    s = _table().get(key)
    if s is None:
        return key  # 누락 키는 테스트(test_i18n)가 잡는다
    return s.format(**kw) if kw else s
