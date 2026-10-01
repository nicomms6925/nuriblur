import sqlite3
from datetime import datetime, timedelta

import pytest

from worker.orgmode import auditlog as A
from worker.orgmode.approval import Approval, ApprovalError
from worker.orgmode.db import OrgDB, current_user
from worker.orgmode.retention import purge_expired


@pytest.fixture
def org(tmp_path):
    db = OrgDB(tmp_path / "org.sqlite")
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
    ap.submit_review(cid, me, "검수 완료")
    assert ap.case(cid)["status"] == "PENDING_APPROVAL" and ap.current_step(cid).role == "팀장"
    with pytest.raises(ApprovalError):
        ap.deliver(cid, me)
    ap.approve(cid, "박문화", "확인")
    with pytest.raises(ApprovalError):
        ap.reject(cid, "이규홍", "  ")  # 사유 필수
    ap.reject(cid, "이규홍", "P#9 OCR 재확인")
    assert ap.case(cid)["status"] == "REVIEWING"
    assert all(s.decision == "PENDING" for s in ap.steps(cid))
    ap.submit_review(cid, me)
    ap.approve(cid, "박문화")
    assert ap.approve(cid, "이규홍") is None
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
