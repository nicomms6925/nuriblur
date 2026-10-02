"""기관 DB (org.sqlite, 설치 단위) — docs/04 기관 DB.

추론 코드가 없으므로 UI 프로세스가 직접 사용한다(docs/11 §3-4).
확장 컬럼: case_file.output_path / output_sha256 / audit_exposures / report_path (출력 제공·보고서 연계)
"""
from __future__ import annotations

import getpass
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

from worker.errors import NBError

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
    "require_pin": "1",
}


USERS_SID = "*S-1-5-32-545"  # BUILTIN\\Users


class OrgDBReadOnly(NBError):
    code = "E_ORG_DB_READONLY"


def _elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _share_with_users(base: Path) -> None:
    """관리자 권한으로 실행 중이면 공용 데이터 폴더를 일반 사용자도 쓸 수 있게(설치 프로그램의 users-modify와 같음).
    그러지 않으면 관리자 권한 실행이 만든 org.sqlite를 이후 일반 권한 실행에서 쓸 수 없다."""
    if not _elevated():
        return
    import subprocess

    subprocess.run(["icacls", str(base), "/grant", f"{USERS_SID}:(OI)(CI)M", "/T", "/Q"],
                   capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def check_writable(path: Path) -> None:
    """기존 DB 파일(+WAL/SHM)에 쓸 수 있는지. 없으면 원인과 해결 방법을 담은 오류."""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if not p.exists():
            continue
        try:
            with open(p, "ab"):
                pass
        except PermissionError as e:
            raise OrgDBReadOnly(
                f"기관 모드 DB에 쓸 수 없습니다: {path}\n"
                "관리자 권한으로 실행했을 때 만들어진 파일이라 일반 권한에서는 읽기 전용입니다.\n"
                "해결: 관리자 권한 PowerShell에서 아래 명령을 한 번 실행하세요.\n"
                f'icacls "{path.parent}" /grant "{USERS_SID}:(OI)(CI)M" /T') from e


def default_path() -> Path:
    env = os.environ.get("NURIBLUR_ORG_DB")
    if env:
        return Path(env)
    base = Path(os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") or Path.home()) / "NuriBlur"
    try:
        base.mkdir(parents=True, exist_ok=True)
        _share_with_users(base)
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
        check_writable(self.path)
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
