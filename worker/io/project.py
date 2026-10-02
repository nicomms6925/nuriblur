""".nbproj 읽기/쓰기 (G1-06, docs/04).

.nbproj = ZIP { project.sqlite, thumbs/{track_id}.jpg, manifest.json }.
원본 영상은 포함하지 않고 경로·SHA-256만 저장한다.

작업 방식: zip을 임시 작업 폴더에 풀어 SQLite로 직접 다루고, save() 때 원자적으로 다시 묶는다.
작업 폴더는 close() 때 즉시 삭제한다(임시 파일 즉시 삭제 원칙).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from worker import __version__
from worker.errors import DiskError, NBError

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT, codec TEXT, width INT, height INT,
  fps REAL, is_vfr INT, frames INT, duration_ms INT, rotation INT, audio_codec TEXT);

CREATE TABLE IF NOT EXISTS track (
  id INTEGER PRIMARY KEY, media_id INT, cls TEXT CHECK(cls IN ('face','person','plate','vehicle')),
  start_f INT, end_f INT, conf_avg REAL, embedding BLOB, plate_text TEXT, plate_conf REAL,
  linked_person_id INT, merged_into INT, thumb TEXT);

CREATE TABLE IF NOT EXISTS track_box (
  track_id INT, frame INT, x REAL, y REAL, w REAL, h REAL, conf REAL, interpolated INT,
  PRIMARY KEY(track_id, frame));
CREATE INDEX IF NOT EXISTS idx_track_box_frame ON track_box(frame);

CREATE TABLE IF NOT EXISTS protect_rule (
  id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN ('click','ref_face','plate_text','region','timerange','manual_box')),
  payload TEXT, created_at TEXT, created_by TEXT);

CREATE TABLE IF NOT EXISTS track_decision (
  track_id INT PRIMARY KEY, protected INT, source_rule_id INT, confidence REAL,
  flag TEXT CHECK(flag IN ('OK','REVIEW')), reviewed INT, reviewed_by TEXT, reviewed_at TEXT);

CREATE TABLE IF NOT EXISTS render_profile (
  id INTEGER PRIMARY KEY, style TEXT, strength REAL, pad_ratio REAL, pad_frames INT,
  codec TEXT, quality TEXT, strip_meta INT, watermark TEXT);

CREATE TABLE IF NOT EXISTS job (
  id TEXT PRIMARY KEY, kind TEXT CHECK(kind IN ('analyze','render')), status TEXT,
  checkpoint_frame INT, started_at TEXT, ended_at TEXT, stats TEXT, log_path TEXT,
  output_path TEXT, output_sha256 TEXT, audit_exposures INT);

-- 확장: GetFrame 랜덤 접근용 프레임 PTS 표, 프로젝트 메타
CREATE TABLE IF NOT EXISTS frame_pts (frame INTEGER PRIMARY KEY, pts INT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class TrackRow:
    id: int
    cls: str
    start_f: int
    end_f: int
    conf_avg: float = 0.0
    embedding: bytes | None = None
    plate_text: str | None = None
    plate_conf: float | None = None
    linked_person_id: int | None = None
    merged_into: int | None = None
    thumb: str | None = None
    # frame -> (x, y, w, h, conf, interpolated)
    boxes: dict[int, tuple[float, float, float, float, float, int]] = field(default_factory=dict)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Project:
    """열린 .nbproj 하나. 스레드 안전(내부 락)."""

    def __init__(self, path: Path, workdir: Path, thumbs: dict[int, bytes] | None = None):
        self.path = Path(path)
        self.workdir = workdir
        self.db_path = workdir / "project.sqlite"
        # 썸네일은 메모리에 두고 save() 때 zip에 바로 쓴다 — 군중 영상은 트랙이 수천 개라
        # 작업 폴더에 개별 파일로 쓰면(백신 검사 포함) 분석보다 오래 걸린다
        self.thumbs: dict[int, bytes] = thumbs or {}
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute("PRAGMA journal_mode=MEMORY")
        self.conn.execute("PRAGMA synchronous=OFF")

    def _migrate(self) -> None:
        """구버전 프로젝트: track.cls CHECK에 'vehicle'이 없으면 표를 다시 만든다(데이터 보존)."""
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='track'").fetchone()
        if row is None or "'vehicle'" in row[0]:
            return
        new_sql = row[0].replace("'face','person','plate')", "'face','person','plate','vehicle')")
        self.conn.executescript(f"""
            ALTER TABLE track RENAME TO _track_old;
            {new_sql};
            INSERT INTO track SELECT * FROM _track_old;
            DROP TABLE _track_old;
        """)
        self.conn.commit()

    # ---------- 생성/열기/저장 ----------
    @classmethod
    def create(cls, path: str | Path) -> Project:
        workdir = Path(tempfile.mkdtemp(prefix="nbproj_"))
        return cls(Path(path), workdir)

    @classmethod
    def open(cls, path: str | Path) -> Project:
        path = Path(path)
        if not path.exists():
            raise NBError(f"프로젝트 파일 없음: {path}")
        workdir = Path(tempfile.mkdtemp(prefix="nbproj_"))
        thumbs: dict[int, bytes] = {}
        try:
            with zipfile.ZipFile(path) as z:
                files = []
                for name in z.namelist():
                    # zip slip 방지
                    target = (workdir / name).resolve()
                    if not str(target).startswith(str(workdir.resolve())):
                        raise NBError(f"잘못된 프로젝트 항목: {name}")
                    stem = name[len("thumbs/"):-len(".jpg")] if name.startswith("thumbs/") and name.endswith(".jpg") else ""
                    if stem.isdigit():
                        thumbs[int(stem)] = z.read(name)
                    elif not name.endswith("/"):
                        files.append(name)
                z.extractall(workdir, members=files)
        except zipfile.BadZipFile as e:
            shutil.rmtree(workdir, ignore_errors=True)
            raise NBError(f"손상된 프로젝트 파일: {path}") from e
        return cls(path, workdir, thumbs)

    def save(self, model_hashes: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.conn.commit()
            # 일관된 스냅샷을 위해 백업 API로 복사
            snap = self.workdir / "_snapshot.sqlite"
            if snap.exists():
                snap.unlink()
            dst = sqlite3.connect(snap)
            self.conn.backup(dst)
            dst.close()
            manifest = {
                "format": "nbproj",
                "version": 1,
                "app": f"nuriblur {__version__}",
                "saved_at": _now(),
                "models": model_hashes if model_hashes is not None else json.loads(self.get_meta("models") or "{}"),
            }
            if model_hashes is not None:
                self.set_meta("models", json.dumps(model_hashes))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                    z.write(snap, "project.sqlite")
                    z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    for tid in sorted(self.thumbs):
                        z.writestr(f"thumbs/{tid}.jpg", self.thumbs[tid], compress_type=zipfile.ZIP_STORED)
                os.replace(tmp, self.path)
            except OSError as e:
                raise DiskError(f"프로젝트 저장 실패: {e}") from e
            finally:
                snap.unlink(missing_ok=True)
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.close()
            finally:
                shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> Project:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- meta ----------
    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    # ---------- media ----------
    def set_media(self, info: dict[str, Any]) -> None:
        cols = ["path", "sha256", "codec", "width", "height", "fps", "is_vfr", "frames",
                "duration_ms", "rotation", "audio_codec"]
        with self.lock:
            self.conn.execute("DELETE FROM media")
            self.conn.execute(
                f"INSERT INTO media(id,{','.join(cols)}) VALUES(1,{','.join('?' * len(cols))})",
                [info.get(c) for c in cols])
            self.conn.commit()

    def media(self) -> dict[str, Any]:
        with self.lock:
            r = self.conn.execute("SELECT * FROM media WHERE id=1").fetchone()
        if r is None:
            raise NBError("프로젝트에 미디어 정보가 없습니다")
        return dict(r)

    # ---------- frame pts ----------
    def add_frame_pts(self, rows: list[tuple[int, int]]) -> None:
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO frame_pts(frame,pts) VALUES(?,?)", rows)

    def frame_pts(self, frame: int) -> int | None:
        with self.lock:
            r = self.conn.execute("SELECT pts FROM frame_pts WHERE frame=?", (frame,)).fetchone()
        return r[0] if r else None

    def frame_count(self) -> int:
        with self.lock:
            r = self.conn.execute("SELECT COUNT(*) FROM frame_pts").fetchone()
        return int(r[0])

    # ---------- tracks ----------
    def next_track_id(self) -> int:
        with self.lock:
            r = self.conn.execute("SELECT COALESCE(MAX(id),0) FROM track").fetchone()
        return int(r[0]) + 1

    def write_tracks(self, tracks: list[TrackRow]) -> None:
        with self.lock:
            for t in tracks:
                self.conn.execute(
                    "INSERT OR REPLACE INTO track(id,media_id,cls,start_f,end_f,conf_avg,embedding,plate_text,"
                    "plate_conf,linked_person_id,merged_into,thumb) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t.id, 1, t.cls, t.start_f, t.end_f, t.conf_avg, t.embedding, t.plate_text, t.plate_conf,
                     t.linked_person_id, t.merged_into, t.thumb))
                self.conn.execute("DELETE FROM track_box WHERE track_id=?", (t.id,))
                self.conn.executemany(
                    "INSERT INTO track_box(track_id,frame,x,y,w,h,conf,interpolated) VALUES(?,?,?,?,?,?,?,?)",
                    [(t.id, f, *b) for f, b in t.boxes.items()])
            self.conn.commit()

    def update_track_fields(self, track_id: int, **fields: Any) -> None:
        if not fields:
            return
        keys = list(fields)
        with self.lock:
            self.conn.execute(f"UPDATE track SET {','.join(k + '=?' for k in keys)} WHERE id=?",
                              [fields[k] for k in keys] + [track_id])
            self.conn.commit()

    def tracks(self, with_boxes: bool = False, at_frame: int | None = None) -> list[TrackRow]:
        with self.lock:
            if at_frame is not None and at_frame >= 0:
                rows = self.conn.execute(
                    "SELECT t.* FROM track t JOIN track_box b ON b.track_id=t.id WHERE b.frame=? ORDER BY t.id",
                    (at_frame,)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM track ORDER BY id").fetchall()
            out = []
            for r in rows:
                t = TrackRow(id=r["id"], cls=r["cls"], start_f=r["start_f"], end_f=r["end_f"],
                             conf_avg=r["conf_avg"] or 0.0, embedding=r["embedding"], plate_text=r["plate_text"],
                             plate_conf=r["plate_conf"], linked_person_id=r["linked_person_id"],
                             merged_into=r["merged_into"], thumb=r["thumb"])
                if with_boxes:
                    for b in self.conn.execute(
                            "SELECT frame,x,y,w,h,conf,interpolated FROM track_box WHERE track_id=? ORDER BY frame",
                            (t.id,)):
                        t.boxes[b[0]] = (b[1], b[2], b[3], b[4], b[5], b[6])
                out.append(t)
        return out

    def track(self, track_id: int, with_boxes: bool = True) -> TrackRow | None:
        for t in self.tracks(with_boxes=False):
            if t.id == track_id:
                if with_boxes:
                    with self.lock:
                        for b in self.conn.execute(
                                "SELECT frame,x,y,w,h,conf,interpolated FROM track_box WHERE track_id=? "
                                "ORDER BY frame", (t.id,)):
                            t.boxes[b[0]] = (b[1], b[2], b[3], b[4], b[5], b[6])
                return t
        return None

    def boxes_by_frame(self) -> dict[int, list[tuple[int, float, float, float, float]]]:
        """frame -> [(track_id, x, y, w, h)] 전체 색인 (렌더링용)."""
        out: dict[int, list[tuple[int, float, float, float, float]]] = {}
        with self.lock:
            for r in self.conn.execute("SELECT track_id,frame,x,y,w,h FROM track_box ORDER BY frame"):
                out.setdefault(r[1], []).append((r[0], r[2], r[3], r[4], r[5]))
        return out

    def delete_tracks_from(self, frame: int) -> None:
        """체크포인트 재개 시: frame 이후 박스를 지우고 빈 트랙 제거."""
        with self.lock:
            self.conn.execute("DELETE FROM track_box WHERE frame>=?", (frame,))
            self.conn.execute("DELETE FROM frame_pts WHERE frame>=?", (frame,))
            self.conn.execute("DELETE FROM track WHERE id NOT IN (SELECT DISTINCT track_id FROM track_box)")
            self.conn.execute(
                "UPDATE track SET end_f=(SELECT MAX(frame) FROM track_box WHERE track_id=track.id)")
            self.conn.commit()

    def write_thumb(self, track_id: int, jpeg: bytes) -> str:
        with self.lock:
            self.thumbs[int(track_id)] = jpeg
        return f"{track_id}.jpg"

    def write_thumbs(self, items: list[tuple[int, bytes]]) -> list[str]:
        return [self.write_thumb(tid, j) for tid, j in items]

    def read_thumb(self, track_id: int) -> bytes:
        with self.lock:
            return self.thumbs.get(int(track_id), b"")

    # ---------- rules / decisions ----------
    def replace_rules(self, rules: list[dict[str, Any]], actor: str = "") -> list[dict[str, Any]]:
        """규칙 전체 교체. id가 있으면 유지, 없으면 새로 매긴다."""
        with self.lock:
            existing = {r["id"]: r for r in self.rules()}
            self.conn.execute("DELETE FROM protect_rule")
            out = []
            for r in rules:
                rid = r.get("id") or None
                created = existing.get(rid, {}).get("created_at") or _now()
                by = existing.get(rid, {}).get("created_by") or actor
                cur = self.conn.execute(
                    "INSERT INTO protect_rule(id,kind,payload,created_at,created_by) VALUES(?,?,?,?,?)",
                    (rid, r["kind"], json.dumps(r.get("payload") or {}, ensure_ascii=False), created, by))
                out.append({"id": cur.lastrowid, "kind": r["kind"], "payload": r.get("payload") or {},
                            "created_at": created, "created_by": by})
            self.conn.commit()
            return out

    def rules(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM protect_rule ORDER BY id").fetchall()
        return [{"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"] or "{}"),
                 "created_at": r["created_at"], "created_by": r["created_by"]} for r in rows]

    def write_decisions(self, decisions: list[dict[str, Any]]) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM track_decision")
            self.conn.executemany(
                "INSERT INTO track_decision(track_id,protected,source_rule_id,confidence,flag,reviewed) "
                "VALUES(?,?,?,?,?,0)",
                [(d["track_id"], int(d["protected"]), d.get("source_rule_id"), d.get("confidence", 0.0),
                  d.get("flag", "OK")) for d in decisions])
            self.conn.commit()

    def decisions(self) -> dict[int, dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM track_decision").fetchall()
        return {r["track_id"]: dict(r) for r in rows}

    # ---------- render profile / job ----------
    def save_render_profile(self, p: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM render_profile")
            self.conn.execute(
                "INSERT INTO render_profile(id,style,strength,pad_ratio,pad_frames,codec,quality,strip_meta,watermark)"
                " VALUES(1,?,?,?,?,?,?,?,?)",
                (p.get("style"), p.get("strength"), p.get("pad_ratio"), p.get("pad_frames"), p.get("codec"),
                 p.get("quality"), int(bool(p.get("strip_meta", True))), p.get("watermark")))
            self.conn.commit()

    def upsert_job(self, job_id: str, kind: str, **fields: Any) -> None:
        with self.lock:
            r = self.conn.execute("SELECT id FROM job WHERE id=?", (job_id,)).fetchone()
            if r is None:
                self.conn.execute("INSERT INTO job(id,kind,status,checkpoint_frame,started_at) VALUES(?,?,?,?,?)",
                                  (job_id, kind, fields.pop("status", "CREATED"),
                                   fields.pop("checkpoint_frame", 0), _now()))
            if fields:
                if "stats" in fields and not isinstance(fields["stats"], str):
                    fields["stats"] = json.dumps(fields["stats"], ensure_ascii=False)
                keys = list(fields)
                self.conn.execute(f"UPDATE job SET {','.join(k + '=?' for k in keys)} WHERE id=?",
                                  [fields[k] for k in keys] + [job_id])
            self.conn.commit()

    def jobs(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            q = "SELECT * FROM job" + (" WHERE kind=?" if kind else "") + " ORDER BY started_at"
            rows = self.conn.execute(q, (kind,) if kind else ()).fetchall()
        return [dict(r) for r in rows]

    def analyze_checkpoint(self) -> int:
        """마지막 분석 작업의 checkpoint_frame (완료면 -1)."""
        js = self.jobs("analyze")
        if not js:
            return 0
        last = js[-1]
        if last["status"] == "ANALYZED":
            return -1
        return int(last["checkpoint_frame"] or 0)
