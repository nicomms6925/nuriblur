"""기관 DB (org.sqlite, 설치 단위) — docs/04 기관 DB.

추론 코드가 없으므로 UI 프로세스가 직접 사용한다(docs/11 §3-4).
확장 컬럼: case_file.output_path / output_sha256 / audit_exposures / report_path (출력 제공·보고서 연계)
"""
from __future__ import annotations

import getpass
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS org_settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS org_approval_line (
  id INTEGER PRIMARY KEY, name TEXT, steps TEXT, is_default INT, updated_by TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS case_file (
  id TEXT PRIMARY KEY, receipt_no TEXT, legal_basis TEXT, requester_name TEXT,
  project_path TEXT, approval_line_snapshot TEXT,
  status TEXT, retention_until TEXT, created_by TEXT, created_at TEXT,
  output_path TEXT, output_sha256 TEXT, audit_exposures INT, report_path TEXT);

CREATE TABLE IF NOT EXISTS approval_step (
  id INTEGER PRIMARY KEY, case_id TEXT, step_no INT, role TEXT, user TEXT,
  decision TEXT CHECK(decision IN ('PENDING','APPROVED','REJECTED')), comment TEXT, decided_at TEXT);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, case_id TEXT, action TEXT, target TEXT, detail TEXT,
  prev_hash TEXT, hash TEXT);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

DEFAULTS = {
    "org_name": "",
    "retention_days": "90",
    "watermark_default": "0",
    "offline_mode": "1",
    "admins": "[]",
}


def default_path() -> Path:
    env = os.environ.get("NURIBLUR_ORG_DB")
    if env:
        return Path(env)
    base = Path(os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") or Path.home()) / "NuriBlur"
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".w"
        probe.write_text("")
        probe.unlink()
    except OSError:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "NuriBlur"
        base.mkdir(parents=True, exist_ok=True)
    return base / "org.sqlite"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


class OrgDB:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or default_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for k, v in DEFAULTS.items():
            self.conn.execute("INSERT OR IGNORE INTO org_settings(key,value) VALUES(?,?)", (k, v))
        # 첫 설치: 설치한 Windows 계정을 관리자로
        if json.loads(self.get("admins") or "[]") == []:
            self.set("admins", json.dumps([current_user()], ensure_ascii=False))

    def get(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM org_settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO org_settings(key,value) VALUES(?,?)", (key, value))

    def is_admin(self, user: str) -> bool:
        return user in json.loads(self.get("admins") or "[]")

    def close(self) -> None:
        self.conn.close()
