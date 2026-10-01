import sqlite3
from datetime import datetime, timedelta

import pytest

from worker.orgmode import auditlog as A
from worker.orgmode.approval import Approval, ApprovalError
from worker.orgmode.db import OrgDB, current_user
from worker.orgmode.retention import purge_expired

PINS = {"김형남": "111111", "박문화": "222222", "이규홍": "333333", "a": "444444", "b": "555555",
        "c": "666666", "d": "777777"}


@pytest.fixture
def org(tmp_path):
    db = OrgDB(tmp_path / "org.sqlite")
    from worker.orgmode.users import Users

    u = Users(db)
    for name, pin in PINS.items():
        u.register(name, pin, actor=current_user())
    yield db
    db.close()


def test_auditlog_append_only_and_chain(org):
    log = A.AuditLog(org)
    for i in range(5):
        log.append("김형남", A.PROTECT_SET, f"F#{i}", {"k": i}, "C-1")
    assert log.verify() == (True, None)
    with pytest.raises(sqlite3.DatabaseError):
        org.conn.execute("UPDATE audit_log SET actor='x' WHERE id=2")
    with pytest.raises(sqlite3.DatabaseError):
        org.conn.execute("DELETE FROM audit_log WHERE id=2")
    # 트리거를 우회해 변조해도 체인 검증이 잡아낸다
    org.conn.execute("DROP TRIGGER audit_log_no_update")
    org.conn.execute("UPDATE audit_log SET detail='{}' WHERE id=3")
    assert log.verify() == (False, 3)


def test_auditlog_export(org, tmp_path):
    log = A.AuditLog(org)
    log.append("a", A.CASE_CREATED, "x", None, "C-1")
    assert '"chain_ok": true' in log.export_json(tmp_path / "a.json").read_text(encoding="utf-8")
    assert "작업 생성" in log.export_csv(tmp_path / "a.csv").read_text(encoding="utf-8-sig")


def test_line_admin_only_and_snapshot(org):
    ap = Approval(org)
    with pytest.raises(PermissionError):
        ap.save_line(Approval.preset(3, ["김", "박", "이"]), actor="not-admin")
    me = current_user()
    with pytest.raises(ApprovalError):  # 등록되지 않은 결재자
        ap.save_line(Approval.preset(3, ["김형남", "박문화", "홍길동"]), actor=me)
    ap.save_line(Approval.preset(3, ["김형남", "박문화", "이규홍"]), actor=me)
    cid = ap.create_case("2026-0928-113", "개인정보보호법 §35", "홍○○", "p.nbproj", me)
    ap.save_line(Approval.preset(4, ["a", "b", "c", "d"]), actor=me)  # 설정 변경
    assert [s.role for s in ap.steps(cid)] == ["담당자 검수", "팀장", "부서장"], "진행 중인 건은 스냅샷 유지"
    with pytest.raises(ApprovalError):
        ap.save_line([{"role": "only", "user": ""}], actor=me)
    with pytest.raises(ApprovalError):
        ap.save_line([{"role": f"r{i}", "user": ""} for i in range(7)], actor=me)


def test_full_approval_flow(org, tmp_path):
    me = current_user()
    ap = Approval(org)
    ap.save_line(Approval.preset(3, ["김형남", "박문화", "이규홍"]), actor=me)
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x")
    proj = tmp_path / "p.nbproj"
    proj.write_bytes(b"x")
    cid = ap.create_case("R-1", "§35", "홍", str(proj), me)
    with pytest.raises(ApprovalError):
        ap.submit_review(cid, me)  # 재검사 전
    ap.record_render(cid, me, str(out), "ab" * 32, exposures=2)
    with pytest.raises(ApprovalError):
        ap.submit_review(cid, me)  # 노출 있음
    ap.record_render(cid, me, str(out), "ab" * 32, exposures=0)
    with pytest.raises(ApprovalError):  # 1단계 결재자(김형남) PIN이 아님
        ap.submit_review(cid, me, "검수 완료", pin=PINS["박문화"])
    ap.submit_review(cid, me, "검수 완료", pin=PINS["김형남"])
    assert ap.case(cid)["status"] == "PENDING_APPROVAL" and ap.current_step(cid).role == "팀장"
    with pytest.raises(ApprovalError):
        ap.deliver(cid, me)
    with pytest.raises(ApprovalError):  # 다음 단계 결재자가 대신 승인할 수 없다
        ap.approve(cid, me, "확인", pin=PINS["이규홍"])
    ap.approve(cid, me, "확인", pin=PINS["박문화"])
    with pytest.raises(ApprovalError):
        ap.reject(cid, me, "  ", pin=PINS["이규홍"])  # 사유 필수
    ap.reject(cid, me, "P#9 OCR 재확인", pin=PINS["이규홍"])
    assert ap.case(cid)["status"] == "REVIEWING"
    assert all(s.decision == "PENDING" for s in ap.steps(cid))
    ap.submit_review(cid, me, pin=PINS["김형남"])
    ap.approve(cid, me, pin=PINS["박문화"])
    assert ap.approve(cid, me, pin=PINS["이규홍"]) is None
    log_actors = [(e["actor"], e["action"]) for e in A.AuditLog(org).entries(cid)]
    assert ("박문화", A.APPROVED) in log_actors and ("이규홍", A.REJECTED) in log_actors
    assert ("박문화", "본인 확인 실패") in log_actors  # 박문화 단계에 다른 사람 PIN → 실패 기록

    assert ap.case(cid)["status"] == "APPROVED"
    until = ap.deliver(cid, me, retention_days=90)
    assert ap.case(cid)["status"] == "DELIVERED" and until
    actions = [e["action"] for e in A.AuditLog(org).entries(cid)]
    assert actions[0] == A.CASE_CREATED and A.REJECTED in actions and actions[-1] == A.DELIVERED
    # 보관 만료 → 출력본·프로젝트 삭제, 로그 보존
    assert purge_expired(org, datetime.now().astimezone() + timedelta(days=91)) == [cid]
    assert not out.exists() and not proj.exists()
    assert ap.case(cid)["status"] == "PURGED"
    assert A.AuditLog(org).entries(cid)[-1]["action"] == A.PURGED
    assert A.AuditLog(org).verify()[0]


@pytest.mark.slow
def test_report_pdf(org, tmp_path, project_copy):
    from worker.orgmode.fonts import korean_font_path
    from worker.orgmode.report import generate

    if korean_font_path() is None:
        pytest.skip("한글 폰트 없음")
    me = current_user()
    ap = Approval(org)
    ap.save_line(Approval.preset(2, ["김형남", "이규홍"]), actor=me)
    cid = ap.create_case("R-2", "개인정보보호법 제35조", "홍○○", str(project_copy), me)
    ap.record_render(cid, me, str(tmp_path / "o.mp4"), "cd" * 32, 0)
    pdf = generate(ap, cid, tmp_path / "r.pdf", operator=me)
    data = pdf.read_bytes()
    assert data[:4] == b"%PDF" and len(data) > 5000
    assert ap.case(cid)["report_path"] == str(pdf)


def test_pin_rules_and_lockout(org):
    from worker.orgmode.users import AuthError, Users

    u = Users(org)
    with pytest.raises(PermissionError):
        u.register("x", "123456", actor="not-admin")
    with pytest.raises(AuthError):
        u.register("x", "12ab", actor=current_user())
    assert u.verify("김형남", PINS["김형남"])
    for _ in range(5):
        with pytest.raises(AuthError):
            u.verify("김형남", "000000")
    with pytest.raises(AuthError, match="잠겼"):
        u.verify("김형남", PINS["김형남"])  # 잠금 중에는 맞는 PIN도 거부
    u.change_pin("박문화", PINS["박문화"], "999999", actor="박문화")
    assert u.verify("박문화", "999999")
    row = org.conn.execute("SELECT pin_hash, salt FROM org_user WHERE name='박문화'").fetchone()
    assert "999999" not in row[0] and len(row[1]) == 32
    assert all("999999" not in (e["detail"] or "") for e in A.AuditLog(org).entries())


def test_submit_requires_assigned_approver(org, tmp_path):
    me = current_user()
    ap = Approval(org)
    cid = ap.create_case("R-9", "§35", "홍", str(tmp_path / "p"), me)  # 기본 결재선(결재자 미지정)
    ap.record_render(cid, me, "o.mp4", "ab" * 32, 0)
    with pytest.raises(ApprovalError, match="지정되지 않았"):
        ap.submit_review(cid, me, pin="111111")


def test_dismissal_logged_and_unlocks_review(org, tmp_path):
    """재검사 오탐 확인 → 감사 로그(사유·위치) + 남은 노출 0이면 검수 완료 가능."""
    me = current_user()
    ap = Approval(org)
    ap.save_line(Approval.preset(3, ["김형남", "박문화", "이규홍"]), actor=me)
    cid = ap.create_case("R-2", "§35", "홍", str(tmp_path / "p.nbproj"), me)
    ap.record_render(cid, me, str(tmp_path / "o.mp4"), "cd" * 32, exposures=2)
    items = [{"frame": 3, "cls": "face", "x": 1, "y": 2, "w": 10, "h": 12, "conf": 0.3}]
    ap.record_dismissal(cid, me, items, "노면 반사", remaining=1)
    with pytest.raises(ApprovalError):
        ap.submit_review(cid, me, pin=PINS["김형남"])
    ap.record_dismissal(cid, me, items, "간판", remaining=0)
    ap.submit_review(cid, me, pin=PINS["김형남"])
    e = [x for x in A.AuditLog(org).entries(cid) if x["action"] == A.EXPOSURE_DISMISSED]
    assert len(e) == 2 and "노면 반사" in e[0]["detail"] and A.AuditLog(org).verify()[0]
