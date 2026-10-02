"""재검사 오탐 확인('노출 아님') 정책과 식별 불가 소형 얼굴 제외 (결정 2026-10-02, docs/10)."""
from __future__ import annotations

import json

import pytest

from worker.io.project import Project
from worker.pipeline import audit_check
from worker.pipeline.dismiss import DISMISS_MAX_CONF, DismissIndex, dismiss, dismissals


def _exp(frame, conf, cls="face", x=100.0):
    return {"frame": frame, "cls": cls, "x": x, "y": 50.0, "w": 30.0, "h": 36.0, "conf": conf}


@pytest.fixture
def proj(tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x")
    p = Project.create(tmp_path / "c.nbproj")
    last = [_exp(10, 0.31), _exp(20, 0.72), _exp(30, 1.0, "mask_missing"), _exp(40, 0.25, "plate")]
    p.set_meta("last_audit", json.dumps({"output": str(out), "exposures": last, "count": len(last)}))
    p.upsert_job("r1", "render", status="REVIEWING", output_path=str(out), output_sha256="ab" * 32, audit_exposures=4)
    yield p, out, last
    p.close()


def test_dismiss_policy(proj):
    p, out, last = proj
    r = dismiss(p, out, [last[0], last[1], last[2], _exp(99, 0.2)], "검수자", "노면 반사")
    assert [a["frame"] for a in r["accepted"]] == [10]
    assert {x["frame"]: x["why"] for x in r["refused"]} == {20: "high_conf", 30: "integrity", 99: "not_found"}
    assert r["remaining"] == 3
    d = dismissals(p)
    assert d[0]["actor"] == "검수자" and d[0]["reason"] == "노면 반사" and d[0]["output_sha256"] == "ab" * 32
    assert p.jobs("render")[-1]["status"] == "REVIEWING"
    # 같은 항목을 두 번 확인할 수 없다(목록에서 빠짐)
    assert dismiss(p, out, [last[0]], "검수자", "중복")["refused"][0]["why"] == "not_found"
    assert last[1]["conf"] >= DISMISS_MAX_CONF


def test_dismiss_to_zero_marks_audited(tmp_path):
    out = tmp_path / "o.mp4"
    out.write_bytes(b"x")
    p = Project.create(tmp_path / "c.nbproj")
    try:
        last = [_exp(5, 0.21), _exp(9, 0.33, "plate")]
        p.set_meta("last_audit", json.dumps({"output": str(out), "exposures": last, "count": 2}))
        p.upsert_job("r1", "render", status="REVIEWING", output_path=str(out), audit_exposures=2)
        assert dismiss(p, out, last, "a", "간판 그림")["remaining"] == 0
        assert p.jobs("render")[-1]["status"] == "AUDITED" and p.jobs("render")[-1]["audit_exposures"] == 0
    finally:
        p.close()


def test_dismiss_requires_reason_and_current_output(proj, tmp_path):
    p, out, last = proj
    with pytest.raises(ValueError):
        dismiss(p, out, [last[0]], "a", "  ")
    with pytest.raises(ValueError):
        dismiss(p, tmp_path / "other.mp4", [last[0]], "a", "사유")


def test_dismiss_index_matches_same_place_only():
    idx = DismissIndex([_exp(10, 0.3)])
    assert idx.match(10, "face", (102, 52, 132, 88))          # 같은 자리 재검출
    assert not idx.match(11, "face", (100, 50, 130, 86))      # 다른 프레임
    assert not idx.match(10, "plate", (100, 50, 130, 86))     # 다른 종류
    assert not idx.match(10, "face", (300, 50, 330, 86))      # 다른 자리
    assert idx.hits == 1


@pytest.mark.slow
def test_audit_honors_dismissals_and_min_face(street_clip, monkeypatch):
    from worker.pipeline.mask import MaskPlan

    base = audit_check.audit(street_clip, MaskPlan(), stride=10)
    faces = [e for e in base if e["cls"] == "face"]
    assert faces
    # 확인된 오탐은 같은 자리 재검출만 빠진다
    st: dict = {}
    again = audit_check.audit(street_clip, MaskPlan(), stride=10, dismissed=DismissIndex(faces[:1]), stats=st)
    assert len(again) == len(base) - 1 and st["dismissed"] == 1
    # 폭 기준을 크게 잡으면 모든 얼굴이 '식별 불가 소형'으로 빠지고 건수가 보고된다
    monkeypatch.setattr(audit_check, "AUDIT_MIN_FACE_PX", 10_000)
    st = {}
    none = audit_check.audit(street_clip, MaskPlan(), stride=10, stats=st)
    assert not [e for e in none if e["cls"] == "face"] and st["small_faces"] >= len(faces)


def test_cli_dismiss(proj, capsys):
    from worker.cli import main

    p, out, last = proj
    p.save()
    rc = main(["dismiss", str(p.path), "-o", str(out), "--all", "--reason", "반사", "--actor", "cli"])
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert res["accepted"] == 2 and res["remaining"] == 2 and rc == 3  # 0.72 얼굴·mask_missing은 남음
