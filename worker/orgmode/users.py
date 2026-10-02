"""결재자 본인 확인 (docs/08 '사용자 식별: Windows 로그인 계정 + 앱 내 PIN').

- 결재자 등록·PIN 초기화는 기관 관리자만 (Windows 계정이 org_settings.admins 에 있거나 role=admin + PIN 확인).
- PIN은 6~12자리 숫자, PBKDF2-HMAC-SHA256(200,000회) + 사용자별 salt 로만 저장.
- 5회 연속 실패 시 10분 잠금. 성공·실패·잠금·등록·PIN 변경은 모두 감사 로그에 남는다(PIN 자체는 남기지 않음).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from worker.orgmode import auditlog as A
from worker.orgmode.db import OrgDB, now_iso

ITER = 200_000
MAX_FAIL = 5
LOCK_MIN = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS org_user (
  name TEXT PRIMARY KEY, role TEXT CHECK(role IN ('admin','approver')), pin_hash TEXT, salt TEXT,
  windows_account TEXT, failed INT DEFAULT 0, locked_until TEXT, active INT DEFAULT 1,
  created_by TEXT, created_at TEXT, pin_changed_at TEXT);
"""


class AuthError(Exception):
    pass


@dataclass
class User:
    name: str
    role: str
    windows_account: str
    active: bool
    locked_until: str | None


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), ITER).hex()


def _check_pin_format(pin: str) -> None:
    if not (pin.isdigit() and 6 <= len(pin) <= 12):
        raise AuthError("PIN은 6~12자리 숫자여야 합니다")


class Users:
    def __init__(self, db: OrgDB):
        self.db = db
        self.log = A.AuditLog(db)
        db.conn.executescript(SCHEMA)

    def required(self) -> bool:
        return (self.db.get("require_pin") or "1") == "1"

    def get(self, name: str) -> User | None:
        r = self.db.conn.execute("SELECT * FROM org_user WHERE name=?", (name,)).fetchone()
        if r is None:
            return None
        return User(r["name"], r["role"], r["windows_account"] or "", bool(r["active"]), r["locked_until"])

    def list(self) -> list[User]:
        return [User(r["name"], r["role"], r["windows_account"] or "", bool(r["active"]), r["locked_until"])
                for r in self.db.conn.execute("SELECT * FROM org_user ORDER BY name")]

    def _is_admin_actor(self, actor: str, admin_name: str = "", admin_pin: str = "") -> bool:
        if self.db.is_admin(actor):
            return True
        u = self.get(admin_name) if admin_name else None
        return bool(u and u.role == "admin" and self.verify(admin_name, admin_pin, purpose="관리자 확인", raise_=False))

    def register(self, name: str, pin: str, actor: str, role: str = "approver", windows_account: str = "",
                 admin_name: str = "", admin_pin: str = "") -> None:
        name = name.strip()
        if not name:
            raise AuthError("이름이 필요합니다")
        if not self._is_admin_actor(actor, admin_name, admin_pin):
            raise PermissionError("결재자 등록은 기관 관리자만 할 수 있습니다")
        _check_pin_format(pin)
        salt = secrets.token_hex(16)
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO org_user(name,role,pin_hash,salt,windows_account,failed,locked_until,active,created_by,"
                "created_at,pin_changed_at) VALUES(?,?,?,?,?,0,NULL,1,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET role=excluded.role, pin_hash=excluded.pin_hash, salt=excluded.salt,"
                " windows_account=excluded.windows_account, failed=0, locked_until=NULL, active=1,"
                " pin_changed_at=excluded.pin_changed_at",
                (name, role, _hash(pin, salt), salt, windows_account, actor, now_iso(), now_iso()))
        self.log.append(actor, "결재자 등록", name, {"role": role, "windows_account": windows_account})

    def deactivate(self, name: str, actor: str) -> None:
        if not self.db.is_admin(actor):
            raise PermissionError("기관 관리자만 할 수 있습니다")
        with self.db.lock:
            self.db.conn.execute("UPDATE org_user SET active=0 WHERE name=?", (name,))
        self.log.append(actor, "결재자 비활성화", name, None)

    def change_pin(self, name: str, old: str, new: str, actor: str) -> None:
        self.verify(name, old, purpose="PIN 변경")
        _check_pin_format(new)
        salt = secrets.token_hex(16)
        with self.db.lock:
            self.db.conn.execute("UPDATE org_user SET pin_hash=?, salt=?, pin_changed_at=? WHERE name=?",
                                 (_hash(new, salt), salt, now_iso(), name))
        self.log.append(actor, "PIN 변경", name, None)

    def verify(self, name: str, pin: str, purpose: str = "결재", case_id: str | None = None,
               raise_: bool = True) -> bool:
        r = self.db.conn.execute("SELECT * FROM org_user WHERE name=?", (name,)).fetchone()

        def fail(msg: str) -> bool:
            if raise_:
                raise AuthError(msg)
            return False

        if r is None or not r["active"]:
            return fail(f"등록된 결재자가 아닙니다: {name}")
        now = datetime.now().astimezone()
        if r["locked_until"] and datetime.fromisoformat(r["locked_until"]) > now:
            return fail(f"{name}: PIN 오류가 많아 잠겼습니다 ({r['locked_until'][11:16]}까지)")
        ok = hmac.compare_digest(_hash(pin or "", r["salt"]), r["pin_hash"])
        with self.db.lock:
            if ok:
                self.db.conn.execute("UPDATE org_user SET failed=0, locked_until=NULL WHERE name=?", (name,))
            else:
                failed = int(r["failed"] or 0) + 1
                locked = (now + timedelta(minutes=LOCK_MIN)).isoformat(timespec="seconds") if failed >= MAX_FAIL else None
                self.db.conn.execute("UPDATE org_user SET failed=?, locked_until=? WHERE name=?",
                                     (0 if locked else failed, locked, name))
        if not ok:
            self.log.append(name, "본인 확인 실패", purpose, {"locked": failed >= MAX_FAIL}, case_id)
            return fail("PIN이 일치하지 않습니다")
        return True
