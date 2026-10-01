"""라이선스 CI 검사 (G0-04, docs/09).

1. pip-licenses로 설치 의존성 라이선스 수집 → AGPL / SSPL / Commons Clause 포함 시 실패
2. models/manifest.json 각 모델: 허용 라이선스 + SHA-256 일치 (registry.verify_all)
3. 소스에서 `import ultralytics` / `from ultralytics` / insightface 사전학습 가중치 이름 발견 시 실패
4. --packaging: 배포용 FFmpeg에 GPL 구성요소(libx264/libx265 등)가 링크되어 있으면 실패
   (PyPI의 av 휠은 GPL 빌드를 포함하므로 배포 전 LGPL 빌드로 교체해야 한다 — docs/10 열린 질문)

  python scripts/ci/check_license.py [--packaging]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FORBIDDEN_DEP = re.compile(r"AGPL|Affero|SSPL|Server Side Public|Commons Clause", re.I)
FORBIDDEN_SRC = [
    re.compile(r"^\s*(import|from)\s+ultralytics\b", re.M),
    re.compile(r"buffalo_[lsm]|antelopev2|insightface\.app", re.I),
]
SRC_DIRS = ["worker", "app", "scripts", "tests"]
SELF = Path(__file__).resolve()


def check_deps() -> list[str]:
    try:
        r = subprocess.run([sys.executable, "-m", "piplicenses", "--format=json", "--with-system"],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        pkgs = json.loads(r.stdout or "[]")
    except Exception as e:  # noqa: BLE001
        return [f"pip-licenses 실행 실패: {e}"]
    return [f"금지 라이선스 의존성: {p['Name']} {p['Version']} ({p['License']})"
            for p in pkgs if FORBIDDEN_DEP.search(p.get("License", ""))]


def check_models() -> list[str]:
    from worker.models.registry import Registry

    return Registry(ROOT / "models" / "manifest.json").verify_all()


def check_sources() -> list[str]:
    errs = []
    for d in SRC_DIRS:
        for f in (ROOT / d).rglob("*.py"):
            if f.resolve() == SELF or "pb" in f.parts:
                continue
            txt = f.read_text(encoding="utf-8", errors="ignore")
            for pat in FORBIDDEN_SRC:
                if pat.search(txt):
                    errs.append(f"금지 import/가중치: {f.relative_to(ROOT)} ({pat.pattern})")
    return errs


def check_packaging() -> list[str]:
    import av

    from worker.pipeline.encode import GPL_ENCODERS

    present = sorted(n for n in GPL_ENCODERS if n in av.codecs_available)
    return [f"배포 FFmpeg에 GPL 구성요소 포함: {', '.join(present)} — LGPL 빌드 PyAV로 교체 필요"] if present else []


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--packaging", action="store_true")
    a = ap.parse_args()
    errs = check_deps() + check_models() + check_sources()
    if a.packaging:
        errs += check_packaging()
    for e in errs:
        print("FAIL", e)
    print("라이선스 검사:", "실패" if errs else "통과")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
