"""상태 머신·i18n (UI 프로세스 규칙) — 빠른 테스트."""
import ast
import json
import re
from pathlib import Path

import pytest

from app.state.machine import InvalidTransition, JobState, S

ROOT = Path(__file__).resolve().parents[1]


def test_happy_path_org():
    j = JobState()
    for s in (S.PROBED, S.ANALYZING, S.PAUSED):
        j.go(s)
    j.resume()
    for s in (S.ANALYZED, S.RULES_APPLIED, S.REVIEWING, S.RENDERING, S.AUDITED, S.PENDING_APPROVAL,
              S.APPROVED, S.DELIVERED, S.RETAINED, S.PURGED):
        j.go(s)
    assert j.state == S.PURGED


def test_reject_goes_back_to_review_and_rerender():
    j = JobState(S.PENDING_APPROVAL)
    j.go(S.REVIEWING)
    j.go(S.RENDERING)
    j.go(S.REVIEWING)  # 재검사 노출 → 검수


def test_audited_only_from_rendering():
    j = JobState(S.REVIEWING)
    with pytest.raises(InvalidTransition):
        j.go(S.AUDITED)
    with pytest.raises(InvalidTransition):
        JobState(S.ANALYZED).go(S.PENDING_APPROVAL)


def test_paused_resumes_to_origin_only():
    j = JobState(S.RENDERING)
    j.go(S.PAUSED)
    with pytest.raises(InvalidTransition):
        j.go(S.ANALYZING)
    assert j.resume() == S.RENDERING


def test_cancel_failed_from_anywhere():
    for s in S:
        JobState(s).go(S.FAILED)


def _keys_in_source() -> set[str]:
    keys = set()
    for f in (ROOT / "app").rglob("*.py"):
        if "pb" in f.parts:
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "tr" and node.args:
                a = node.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    keys.add(a.value)
    return keys


def test_all_i18n_keys_exist():
    table = json.loads((ROOT / "app" / "i18n" / "ko.json").read_text(encoding="utf-8"))
    missing = sorted(k for k in _keys_in_source() if k not in table)
    # 동적 키
    from app.views.topbar import STEPS

    dyn = list(STEPS)
    dyn += [f"badge.{s}" for s in ("audited", "pending_approval", "approved")]
    dyn += [f"case.{s}" for s in ("REVIEWING", "PENDING_APPROVAL", "APPROVED", "DELIVERED", "RETAINED", "PURGED")]
    dyn += [f"codec.{k}" for k in ("source", "h264", "hevc")] + [f"quality.{k}" for k in ("source", "high", "normal")]
    dyn += [f"input.{k}" for k in ("file", "codec", "res", "len", "audio")]
    dyn += [f"input.cls_{k}" for k in ("face", "person", "plate", "sign")]
    dyn += [f"org.preset{n}" for n in (2, 3, 4)] + [f"org.{k}" for k in ("receipt", "basis", "requester", "people", "status")]
    dyn += [f"profile.{k}" for k in ("gpu_precise", "gpu_fast", "cpu")]
    dyn += [f"stage.{k}" for k in ("ingest", "decode", "detect", "identify", "save", "render", "audit")]
    dyn += [f"stagel.{k}" for k in ("ingest", "decode", "detect", "identify", "save", "render", "audit")]
    dyn += [f"lane.{k}" for k in ("protect", "mask", "review")] + [f"cls.{k}" for k in ("face", "person", "plate")]
    missing += [k for k in dyn if k not in table]
    assert not missing, missing


def test_no_hardcoded_hangul_in_ui():
    """사용자 노출 문구는 ko.json에만 (주석·docstring·로그 action 상수 제외)."""
    bad = []
    hangul = re.compile(r"[가-힣]")
    allowed_files: set[str] = set()
    for f in (ROOT / "app").rglob("*.py"):
        if "pb" in f.parts:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and hangul.search(node.value):
                if id(node) in docstrings or f.name in allowed_files:
                    continue
                bad.append(f"{f.name}:{node.lineno} {node.value[:30]}")
    assert not bad, bad


def test_ui_does_not_import_inference():
    """UI 프로세스에 추론 코드 금지: app/ 은 onnxruntime·cv2·worker.pipeline 을 import 하지 않는다."""
    bad = []
    for f in (ROOT / "app").rglob("*.py"):
        if "pb" in f.parts:
            continue
        src = f.read_text(encoding="utf-8")
        for pat in (r"^\s*(import|from)\s+onnxruntime", r"^\s*(import|from)\s+cv2",
                    r"^\s*(import|from)\s+worker\.(pipeline|models)", r"^\s*(import|from)\s+worker\.server"):
            # main.py 는 같은 exe를 별도 프로세스(--worker / --probe-encoder)로 띄울 때의 진입점
            if re.search(pat, src, re.M) and not (f.name == "main.py" and "worker" in pat):
                bad.append(f"{f.name}: {pat}")
    assert not bad, bad
