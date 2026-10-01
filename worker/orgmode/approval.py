"""결재선 설정·결재 흐름 (G3-01, G3-02) — docs/08.

흐름: REVIEWING ─검수 완료(1단계)→ PENDING_APPROVAL ─…─ 마지막 단계 승인→ APPROVED ─출력 제공→ DELIVERED
                          └ 어느 단계든 반려(사유 필수) → REVIEWING, 모든 결재 단계 초기화
- 결재선은 처리 건 생성 시 approval_line_snapshot 으로 복사되어, 이후 설정 변경의 영향을 받지 않는다.
- 결재선 저장은 관리자만. 단계 2~6.
- 검수 완료(승인 요청)는 최근 렌더링의 노출 재검사가 0건(AUDITED)일 때만 가능.
- 출력본 제공은 마지막 단계 승인 후에만.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from worker.orgmode import auditlog as A
from worker.orgmode.db import OrgDB, now_iso

PRESETS: dict[int, list[str]] = {
    2: ["담당자 검수", "승인자"],
    3: ["담당자 검수", "팀장", "부서장"],
    4: ["담당자 검수", "팀장", "부서장", "개인정보보호책임자"],
}
MIN_STEPS, MAX_STEPS = 2, 6


class ApprovalError(Exception):
    pass


@dataclass
class Step:
    step_no: int
    role: str
    user: str
    decision: str
    comment: str | None
    decided_at: str | None


class Approval:
    def __init__(self, db: OrgDB):
        self.db = db
        self.log = A.AuditLog(db)

    # ---------- 결재선 설정 ----------
    def default_line(self) -> list[dict[str, str]]:
        r = self.db.conn.execute("SELECT steps FROM org_approval_line WHERE is_default=1 ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            return json.loads(r[0])
        return [{"role": role, "user": ""} for role in PRESETS[3]]

    def save_line(self, steps: list[dict[str, str]], actor: str, name: str = "기본") -> None:
        if not self.db.is_admin(actor):
            raise PermissionError("결재선 설정은 기관 관리자만 저장할 수 있습니다")
        steps = [{"role": (s.get("role") or "").strip(), "user": (s.get("user") or "").strip()} for s in steps]
        if not (MIN_STEPS <= len(steps) <= MAX_STEPS):
            raise ApprovalError(f"결재 단계는 {MIN_STEPS}~{MAX_STEPS}단이어야 합니다")
        if any(not s["role"] for s in steps):
            raise ApprovalError("모든 단계에 역할명이 필요합니다")
        with self.db.lock:
            self.db.conn.execute("UPDATE org_approval_line SET is_default=0")
            self.db.conn.execute(
                "INSERT INTO org_approval_line(name,steps,is_default,updated_by,updated_at) VALUES(?,?,1,?,?)",
                (name, json.dumps(steps, ensure_ascii=False), actor, now_iso()))
        self.log.append(actor, A.LINE_CHANGED, name, {"steps": [f"{s['role']}:{s['user']}" for s in steps]})

    @staticmethod
    def preset(n: int, users: list[str] | None = None) -> list[dict[str, str]]:
        users = users or []
        return [{"role": r, "user": users[i] if i < len(users) else ""} for i, r in enumerate(PRESETS[n])]

    # ---------- 처리 건 ----------
    def create_case(self, receipt_no: str, legal_basis: str, requester_name: str, project_path: str,
                    actor: str) -> str:
        cid = "C-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6]
        line = self.default_line()
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO case_file(id,receipt_no,legal_basis,requester_name,project_path,approval_line_snapshot,"
                "status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, receipt_no, legal_basis, requester_name, project_path, json.dumps(line, ensure_ascii=False),
                 "REVIEWING", actor, now_iso()))
            for i, s in enumerate(line, 1):
                self.db.conn.execute(
                    "INSERT INTO approval_step(case_id,step_no,role,user,decision) VALUES(?,?,?,?,'PENDING')",
                    (cid, i, s["role"], s["user"]))
        self.log.append(actor, A.CASE_CREATED, receipt_no, {"legal_basis": legal_basis,
                                                            "line": [s["role"] for s in line]}, cid)
        return cid

    def update_case(self, cid: str, actor: str, receipt_no: str, legal_basis: str, requester_name: str) -> None:
        with self.db.lock:
            self.db.conn.execute("UPDATE case_file SET receipt_no=?, legal_basis=?, requester_name=? WHERE id=?",
                                 (receipt_no, legal_basis, requester_name, cid))
        self.log.append(actor, A.CASE_UPDATED, receipt_no, {"legal_basis": legal_basis}, cid)

    def case(self, cid: str) -> dict[str, Any]:
        r = self.db.conn.execute("SELECT * FROM case_file WHERE id=?", (cid,)).fetchone()
        if r is None:
            raise ApprovalError(f"처리 건 없음: {cid}")
        return dict(r)

    def cases(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.conn.execute("SELECT * FROM case_file ORDER BY created_at DESC")]

    def case_by_project(self, project_path: str) -> dict[str, Any] | None:
        r = self.db.conn.execute("SELECT * FROM case_file WHERE project_path=? ORDER BY created_at DESC LIMIT 1",
                                 (project_path,)).fetchone()
        return dict(r) if r else None

    def steps(self, cid: str) -> list[Step]:
        return [Step(r["step_no"], r["role"], r["user"], r["decision"], r["comment"], r["decided_at"])
                for r in self.db.conn.execute("SELECT * FROM approval_step WHERE case_id=? ORDER BY step_no", (cid,))]

    def current_step(self, cid: str) -> Step | None:
        return next((s for s in self.steps(cid) if s.decision == "PENDING"), None)

    def _status(self, cid: str, status: str) -> None:
        with self.db.lock:
            self.db.conn.execute("UPDATE case_file SET status=? WHERE id=?", (status, cid))

    def log_event(self, cid: str, actor: str, action: str, target: str = "", detail: Any = None) -> None:
        self.log.append(actor, action, target, detail, cid)

    def record_render(self, cid: str, actor: str, output_path: str, sha256: str, exposures: int) -> None:
        with self.db.lock:
            self.db.conn.execute("UPDATE case_file SET output_path=?, output_sha256=?, audit_exposures=? WHERE id=?",
                                 (output_path, sha256, exposures, cid))
        self.log.append(actor, A.RENDERED, output_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                        {"sha256": sha256, "audit_exposures": exposures}, cid)

    # ---------- 결재 ----------
    def submit_review(self, cid: str, actor: str, comment: str = "") -> None:
        """1단계(담당자 검수) 완료 = 승인 요청."""
        c = self.case(cid)
        if c["status"] != "REVIEWING":
            raise ApprovalError(f"검수 단계가 아닙니다 ({c['status']})")
        if c["audit_exposures"] is None or int(c["audit_exposures"]) != 0:
            raise ApprovalError("노출 재검사를 통과한(0건) 출력본이 있어야 승인 요청할 수 있습니다")
        first = self.steps(cid)[0]
        self._decide(cid, first.step_no, "APPROVED", comment)
        self._status(cid, "PENDING_APPROVAL")
        nxt = self.current_step(cid)
        self.log.append(actor, A.REVIEW_DONE, first.role,
                        {"comment": comment, "next": f"{nxt.role}:{nxt.user}" if nxt else ""}, cid)
        if nxt is None:
            self._status(cid, "APPROVED")

    def approve(self, cid: str, actor: str, comment: str = "") -> Step | None:
        c = self.case(cid)
        if c["status"] != "PENDING_APPROVAL":
            raise ApprovalError(f"결재 대기 상태가 아닙니다 ({c['status']})")
        cur = self.current_step(cid)
        if cur is None:
            raise ApprovalError("결재할 단계가 없습니다")
        self._decide(cid, cur.step_no, "APPROVED", comment)
        nxt = self.current_step(cid)
        self.log.append(actor, A.APPROVED, f"{cur.role}:{cur.user}",
                        {"comment": comment, "next": f"{nxt.role}:{nxt.user}" if nxt else "출력 제공 가능"}, cid)
        if nxt is None:
            self._status(cid, "APPROVED")
        return nxt

    def reject(self, cid: str, actor: str, reason: str) -> None:
        if not reason.strip():
            raise ApprovalError("반려 사유를 입력해야 합니다")
        c = self.case(cid)
        if c["status"] not in ("PENDING_APPROVAL", "APPROVED"):
            raise ApprovalError(f"반려할 수 없는 상태입니다 ({c['status']})")
        cur = self.current_step(cid) or self.steps(cid)[-1]
        with self.db.lock:
            self.db.conn.execute("UPDATE approval_step SET decision='PENDING', comment=NULL, decided_at=NULL "
                                 "WHERE case_id=?", (cid,))
        self._status(cid, "REVIEWING")
        self.log.append(actor, A.REJECTED, f"{cur.role}:{cur.user}", {"reason": reason, "back_to": "REVIEWING"}, cid)

    def _decide(self, cid: str, step_no: int, decision: str, comment: str) -> None:
        with self.db.lock:
            self.db.conn.execute("UPDATE approval_step SET decision=?, comment=?, decided_at=? WHERE case_id=? AND step_no=?",
                                 (decision, comment, now_iso(), cid, step_no))

    def can_deliver(self, cid: str) -> bool:
        return self.case(cid)["status"] == "APPROVED"

    def deliver(self, cid: str, actor: str, retention_days: int | None = None) -> str:
        c = self.case(cid)
        if c["status"] != "APPROVED":
            raise ApprovalError("최종 승인 후에만 출력본을 제공할 수 있습니다")
        days = int(retention_days if retention_days is not None else self.db.get("retention_days") or 90)
        until = (datetime.now().astimezone() + timedelta(days=days)).isoformat(timespec="seconds") if days > 0 else ""
        with self.db.lock:
            self.db.conn.execute("UPDATE case_file SET status='DELIVERED', retention_until=? WHERE id=?", (until, cid))
        self.log.append(actor, A.DELIVERED, (c["output_path"] or "").replace("\\", "/").rsplit("/", 1)[-1],
                        {"sha256": c["output_sha256"], "retention_days": days, "retention_until": until}, cid)
        return until

    def set_report(self, cid: str, actor: str, path: str) -> None:
        with self.db.lock:
            self.db.conn.execute("UPDATE case_file SET report_path=? WHERE id=?", (path, cid))
        self.log.append(actor, A.REPORT, path.replace("\\", "/").rsplit("/", 1)[-1], None, cid)
