"""감사 로그 (G3-03) — append-only 해시 체인.

hash = sha256(prev_hash || ts || actor || action || target || detail)
수정·삭제는 DB 트리거가 막는다. verify()로 체인 무결성을 검사한다(월 1회 리포트용).
로그에는 민감정보(얼굴 이미지·임베딩·영상 내용)를 남기지 않는다.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from worker.orgmode.db import OrgDB, now_iso

GENESIS = "0" * 64

# 감사 이벤트 (docs/08)
CASE_CREATED = "작업 생성"
CASE_UPDATED = "처리 건 정보 수정"
ANALYZED = "분석 완료"
PROTECT_SET = "보호대상 지정"
PROTECT_UNSET = "보호대상 해제"
MERGED = "병합"
MANUAL_BOX = "수동 박스 추가"
REVIEW_DONE = "검수 완료"
LINE_CHANGED = "결재선 변경"
APPROVED = "승인"
REJECTED = "반려"
RENDERED = "렌더링 완료"
REPORT = "보고서 생성"
DELIVERED = "출력본 제공"
PURGED = "보관 만료 삭제"


def _digest(prev: str, ts: str, actor: str, action: str, target: str, detail: str) -> str:
    return hashlib.sha256("||".join([prev, ts, actor, action, target, detail]).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, db: OrgDB):
        self.db = db

    def append(self, actor: str, action: str, target: str = "", detail: str | dict | None = None,
               case_id: str | None = None) -> int:
        d = detail if isinstance(detail, str) else json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
        with self.db.lock:
            c = self.db.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
                prev = r[0] if r else GENESIS
                ts = now_iso()
                h = _digest(prev, ts, actor, action, target, d)
                cur = c.execute(
                    "INSERT INTO audit_log(ts,actor,case_id,action,target,detail,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
                    (ts, actor, case_id, action, target, d, prev, h))
                c.execute("COMMIT")
                return int(cur.lastrowid)
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def entries(self, case_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM audit_log" + (" WHERE case_id=?" if case_id else "") + " ORDER BY id"
        rows = self.db.conn.execute(q, (case_id,) if case_id else ()).fetchall()
        out = [dict(r) for r in rows]
        return out[-limit:] if limit else out

    def verify(self) -> tuple[bool, int | None]:
        """(체인 정상 여부, 처음 깨진 id)"""
        prev = GENESIS
        for r in self.db.conn.execute("SELECT * FROM audit_log ORDER BY id"):
            if r["prev_hash"] != prev or _digest(prev, r["ts"], r["actor"], r["action"], r["target"], r["detail"]) != r["hash"]:
                return False, int(r["id"])
            prev = r["hash"]
        return True, None

    def export_json(self, path: str | Path, case_id: str | None = None) -> Path:
        path = Path(path)
        ok, bad = self.verify()
        path.write_text(json.dumps({"chain_ok": ok, "first_bad_id": bad, "entries": self.entries(case_id)},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    def export_csv(self, path: str | Path, case_id: str | None = None) -> Path:
        path = Path(path)
        rows = self.entries(case_id)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["id", "ts", "actor", "case_id", "action", "target", "detail",
                                              "prev_hash", "hash"])
            w.writeheader()
            w.writerows(rows)
        return path
