"""보관 기간 만료 삭제 (G3-04).

만료(retention_until < 지금)된 DELIVERED 처리 건: 출력본·프로젝트(.nbproj) 삭제, 상태 PURGED.
감사 로그와 보고서 PDF는 보존한다. UI 시작 시와 하루 1회 실행.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from worker.orgmode import auditlog as A
from worker.orgmode.db import OrgDB


def purge_expired(db: OrgDB, now: datetime | None = None, actor: str = "시스템") -> list[str]:
    now = now or datetime.now().astimezone()
    log = A.AuditLog(db)
    purged = []
    rows = db.conn.execute("SELECT * FROM case_file WHERE status='DELIVERED' AND retention_until IS NOT NULL "
                           "AND retention_until != ''").fetchall()
    for r in rows:
        try:
            until = datetime.fromisoformat(r["retention_until"])
        except ValueError:
            continue
        if until > now:
            continue
        removed = []
        for key in ("output_path", "project_path"):
            p = r[key]
            if p and Path(p).exists():
                try:
                    Path(p).unlink()
                    removed.append(Path(p).name)
                except OSError:
                    pass
        with db.lock:
            db.conn.execute("UPDATE case_file SET status='PURGED' WHERE id=?", (r["id"],))
        log.append(actor, A.PURGED, r["receipt_no"] or r["id"], {"removed": removed, "kept": ["audit_log", "report"]},
                   r["id"])
        purged.append(r["id"])
    return purged
