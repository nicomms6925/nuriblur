from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DATA = ROOT / "tests" / "data"


@pytest.fixture(scope="session")
def street_clip() -> Path:
    """6초 합성 회귀 클립 (+GT). 없으면 생성."""
    from make_test_clips import make_street

    p = DATA / "synthetic" / "street_faces.mp4"
    if not p.exists() or not p.with_suffix(".gt.json").exists():
        make_street(p, 6.0)
    return p


@pytest.fixture(scope="session")
def short_clip(tmp_path_factory) -> Path:
    from make_test_clips import make_street

    p = tmp_path_factory.mktemp("clips") / "short.mp4"
    make_street(p, 1.5)
    return p


@pytest.fixture(scope="session")
def gt(street_clip) -> dict:
    return json.loads(street_clip.with_suffix(".gt.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def analyzed(street_clip, tmp_path_factory) -> Path:
    """세션당 한 번 분석한 프로젝트 (테스트는 복사본을 쓴다)."""
    from worker.jobs import JobControl
    from worker.pipeline.analyze import analyze

    out = tmp_path_factory.mktemp("proj") / "street.nbproj"
    analyze(street_clip, out, "t-analyze", JobControl("t"), lambda e: None, profile="cpu", detect_interval=2)
    return out


@pytest.fixture
def project_copy(analyzed, tmp_path) -> Path:
    dst = tmp_path / "case.nbproj"
    shutil.copy(analyzed, dst)
    return dst


@pytest.fixture
def events():
    class Sink(list):
        def __call__(self, e):
            self.append(e)

        def of(self, t):
            return [e for e in self if e["type"] == t]

    return Sink()
