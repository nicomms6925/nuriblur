"""gRPC 워커 서버 (G1-11).

- 127.0.0.1 바인딩. 포트·토큰은 UI가 환경변수 NURIBLUR_PORT / NURIBLUR_TOKEN 으로 전달.
- 모든 호출은 메타데이터 x-nb-token 필수 (불일치 → UNAUTHENTICATED).
- 파일 경로는 AllowRoot로 등록된 폴더 하위만 허용 (경로 탈출 검사).
- Analyze/Render는 작업 스레드에서 돌고 이벤트를 스트림으로 보낸다. 클라이언트가 끊기면 작업을 취소한다
  (분석은 체크포인트까지 저장되어 재개 가능).
"""
from __future__ import annotations

import argparse
import ctypes
import hmac
import json
import os
import queue
import sys
import threading
import time
from concurrent import futures
from pathlib import Path
from typing import Any

import grpc

from worker import __version__
from worker.errors import Cancelled, NBError, PathNotAllowed
from worker.io.project import Project
from worker.jobs import JobControl, new_job_id
from worker.pb import nuriblur_pb2 as pb
from worker.pb import nuriblur_pb2_grpc as pbg

TOKEN_HEADER = "x-nb-token"
MAX_MSG = 256 * 1024 * 1024


# ---------------- 보안 ----------------
class TokenInterceptor(grpc.ServerInterceptor):
    def __init__(self, token: str):
        self.token = token.encode()

        def deny(request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid token")

        self._deny = grpc.unary_unary_rpc_method_handler(deny)
        self._deny_stream = grpc.unary_stream_rpc_method_handler(deny)

    def intercept_service(self, continuation, handler_call_details):
        md = dict(handler_call_details.invocation_metadata or ())
        tok = (md.get(TOKEN_HEADER) or "").encode()
        if hmac.compare_digest(tok, self.token):
            return continuation(handler_call_details)
        m = handler_call_details.method or ""
        return self._deny_stream if m.endswith(("/Analyze", "/Render", "/SegmentTrack")) else self._deny


class PathGuard:
    def __init__(self, roots: list[str] | None = None):
        self.roots: list[Path] = []
        self.lock = threading.Lock()
        for r in roots or []:
            self.allow(r)

    def allow(self, root: str) -> Path:
        p = Path(root).expanduser().resolve()
        if p.is_file():
            p = p.parent
        with self.lock:
            if p not in self.roots:
                self.roots.append(p)
        return p

    def check(self, path: str) -> Path:
        if not path:
            raise PathNotAllowed("경로가 비어 있습니다")
        p = Path(path).expanduser().resolve()
        with self.lock:
            roots = list(self.roots)
        if not any(p == r or p.is_relative_to(r) for r in roots):
            raise PathNotAllowed(f"허용되지 않은 경로: {p}")
        return p


# ---------------- 변환 ----------------
def ev_to_pb(e: dict[str, Any]) -> pb.Event:
    m = pb.Event(type=e.get("type", ""), job_id=e.get("job_id", ""), stage=e.get("stage", ""),
                 frame=int(e.get("frame") or 0), total=int(e.get("total") or 0), fps=float(e.get("fps") or 0.0),
                 eta_s=int(e.get("eta_s") or 0), gpu_util=float(e.get("gpu_util") or 0.0),
                 vram_mb=int(e.get("vram_mb") or 0), code=e.get("code", ""), message=e.get("message", ""),
                 output_path=e.get("output_path", ""), output_sha256=e.get("output_sha256", ""),
                 audit_exposures=int(e.get("audit_exposures") or 0))
    for k, v in (e.get("counts") or {}).items():
        if isinstance(v, (int, float)):
            m.counts[k] = int(v)
    if e.get("preview_jpeg"):
        m.preview_jpeg = e["preview_jpeg"]
    return m


def profile_from_pb(p: pb.RenderProfile) -> dict[str, Any]:
    d: dict[str, Any] = {}
    if p.style:
        d["style"] = p.style
    if p.strength:
        d["strength"] = p.strength
    if p.pad_ratio:
        d["pad_ratio"] = p.pad_ratio
    if p.pad_frames:
        d["pad_frames"] = p.pad_frames
    if p.codec:
        d["codec"] = p.codec
    if p.quality:
        d["quality"] = p.quality
    d["strip_meta"] = p.strip_meta
    d["watermark"] = p.watermark
    d["mask_body_when_face_masked"] = p.mask_body_when_face_masked
    d["keep_audio"] = p.keep_audio
    d["mask_head_when_no_face"] = not p.no_head_fallback
    return d


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------- 서비스 ----------------
class Service(pbg.NuriBlurWorkerServicer):
    def __init__(self, guard: PathGuard):
        from worker.pipeline.frames import ReaderCache

        self.guard = guard
        self.jobs: dict[str, JobControl] = {}
        self.busy: dict[str, str] = {}          # project_path -> job_id
        self.projects: dict[str, Project] = {}  # 열린 프로젝트 캐시 (규칙·트랙 조회용)
        self.plans: dict[str, Any] = {}
        self.lock = threading.RLock()
        self.readers = ReaderCache()

    # ---- 공통 ----
    def _fail(self, context, e: Exception):
        if isinstance(e, PathNotAllowed):
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"{e.code}: {e}")
        if isinstance(e, NBError):
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, f"{e.code}: {e}")
        context.abort(grpc.StatusCode.INTERNAL, f"E_INTERNAL: {e}")

    def _project(self, path: str) -> Project:
        key = str(self.guard.check(path))
        with self.lock:
            if key in self.busy:
                raise NBError("이 프로젝트는 작업 중입니다", code="E_BUSY")
            p = self.projects.get(key)
            if p is None:
                p = Project.open(key)
                self.projects[key] = p
            return p

    def _drop(self, path: str) -> None:
        key = str(Path(path).resolve())
        with self.lock:
            p = self.projects.pop(key, None)
            self.plans.pop(key, None)
            if p:
                p.close()

    def _run_job(self, kind: str, job_id: str, project_path: str, fn, context):
        """작업을 스레드에서 실행하고 이벤트를 스트림으로 내보낸다."""
        key = str(Path(project_path).resolve())
        with self.lock:
            if key in self.busy:
                yield ev_to_pb({"type": "error", "job_id": job_id, "stage": kind, "code": "E_BUSY",
                                "message": "같은 프로젝트에서 다른 작업이 진행 중입니다"})
                return
            self.busy[key] = job_id
        self._drop(project_path)
        ctl = JobControl(job_id)
        self.jobs[job_id] = ctl
        q: queue.Queue = queue.Queue()
        END = object()

        def emit(e: dict[str, Any]) -> None:
            q.put(e)

        def target():
            try:
                fn(ctl, emit)
            except Cancelled:
                q.put({"type": "error", "job_id": job_id, "stage": kind, "code": "E_CANCELLED", "message": "취소됨"})
            except NBError as e:
                q.put({"type": "error", "job_id": job_id, "stage": kind, "code": e.code, "message": str(e)})
            except Exception as e:  # noqa: BLE001
                q.put({"type": "error", "job_id": job_id, "stage": kind, "code": "E_INTERNAL", "message": repr(e)})
            finally:
                with self.lock:
                    self.busy.pop(key, None)
                self.jobs.pop(job_id, None)
                q.put(END)

        context.add_callback(lambda: ctl.cancel())
        threading.Thread(target=target, name=f"nb-{kind}-{job_id}", daemon=True).start()
        done = None
        while True:
            e = q.get()
            if e is END:
                # done은 작업 스레드가 프로젝트를 닫고 busy를 푼 뒤에 보낸다
                # (클라이언트가 done 직후 ListTracks 등을 호출해도 E_BUSY가 나지 않도록)
                if done is not None:
                    yield ev_to_pb(done)
                return
            if e.get("type") == "done":
                done = e
                continue
            yield ev_to_pb(e)

    # ---- RPC ----
    def Health(self, request, context):
        from worker.models.registry import available_providers, ep_label
        from worker.pipeline.encode import working_encoders

        try:
            enc = [k for k, v in working_encoders().items() if v]
        except Exception:  # noqa: BLE001
            enc = []
        return pb.WorkerInfo(version=__version__, ep=ep_label(), providers=available_providers(), encoders=enc)

    def AllowRoot(self, request, context):
        p = self.guard.allow(request.path)
        return pb.Ack(ok=True, message=str(p))

    def Probe(self, request, context):
        from worker.pipeline.probe import probe

        try:
            m = probe(self.guard.check(request.path))
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)
        return pb.MediaInfo(path=m["path"], sha256=m["sha256"], codec=m["codec"], width=m["width"],
                            height=m["height"], fps=m["fps"], is_vfr=bool(m["is_vfr"]), frames=m["frames"],
                            duration_ms=m["duration_ms"], rotation=m["rotation"], audio_codec=m["audio_codec"])

    def Analyze(self, request, context):
        from worker.pipeline.analyze import analyze

        try:
            src = self.guard.check(request.path)
            proj = self.guard.check(request.project_path)
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)
        job_id = request.job_id or new_job_id()
        classes = tuple(request.classes) or ("face", "person", "plate")

        def fn(ctl, emit):
            analyze(src, proj, job_id, ctl, emit, classes=classes, profile=request.profile or "cpu",
                    detect_interval=request.detect_interval, resume_from_frame=request.resume_from_frame)

        yield from self._run_job("analyze", job_id, str(proj), fn, context)

    def Render(self, request, context):
        from worker.pipeline.render import render

        try:
            proj = self.guard.check(request.project_path)
            out = self.guard.check(request.output_path)
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)
        job_id = request.job_id or new_job_id()
        prof = profile_from_pb(request.profile)

        def fn(ctl, emit):
            render(proj, out, prof, job_id, ctl, emit, run_audit=request.run_audit)

        yield from self._run_job("render", job_id, str(proj), fn, context)

    def Control(self, request, context):
        ctl = self.jobs.get(request.job_id)
        if ctl is None:
            return pb.Ack(ok=False, message="작업 없음")
        {"pause": ctl.pause, "resume": ctl.resume, "cancel": ctl.cancel}.get(request.action, lambda: None)()
        return pb.Ack(ok=request.action in ("pause", "resume", "cancel"), message=request.action)

    def ListTracks(self, request, context):
        from worker.pipeline.link import merge_suggestions

        try:
            p = self._project(request.project_path)
            with p.lock:
                at = request.at_frame
                tracks = p.tracks(at_frame=at if at >= 0 else None)
                out = pb.TrackList()
                if at >= 0:
                    rows = p.conn.execute(
                        "SELECT track_id,frame,x,y,w,h,conf,interpolated FROM track_box WHERE frame=?", (at,)).fetchall()
                    bx = {r[0]: r for r in rows}
                for t in tracks:
                    m = out.tracks.add(id=t.id, cls=t.cls, start_f=t.start_f, end_f=t.end_f, conf_avg=t.conf_avg,
                                       plate_text=t.plate_text or "", plate_conf=t.plate_conf or 0.0,
                                       linked_person_id=t.linked_person_id or 0, merged_into=t.merged_into or 0,
                                       thumb_jpeg=p.read_thumb(t.id))
                    if at >= 0 and t.id in bx:
                        r = bx[t.id]
                        m.boxes.add(frame=r[1], x=r[2], y=r[3], w=r[4], h=r[5], conf=r[6], interpolated=bool(r[7]))
                if at < 0:
                    full = p.tracks(with_boxes=True)
                    for s in merge_suggestions(full, p.media().get("fps") or 30.0):
                        out.suggestions.add(from_id=s.from_id, to_id=s.to_id, similarity=s.similarity, reason=s.reason)
            return out
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)

    def ApplyRules(self, request, context):
        from worker.pipeline import rules as rules_mod

        try:
            p = self._project(request.project_path)
            with p.lock:
                rules = None
                if not request.keep_stored:
                    rules = [{"id": r.id or None, "kind": r.kind, "payload": json.loads(r.payload_json or "{}")}
                             for r in request.rules]
                dec = rules_mod.apply(p, rules, threshold=request.face_threshold or rules_mod.DEFAULT_THRESHOLD,
                                      actor=request.actor)
                p.save()
                self.plans.pop(str(p.path.resolve()), None)
                out = pb.DecisionList()
                for d in dec:
                    out.decisions.add(track_id=d["track_id"], protected=bool(d["protected"]),
                                      source_rule_id=d["source_rule_id"] or 0, confidence=d["confidence"],
                                      flag=d["flag"])
                for r in p.rules():
                    out.rules.add(id=r["id"], kind=r["kind"], payload_json=json.dumps(r["payload"], ensure_ascii=False))
            return out
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)

    def MergeTracks(self, request, context):
        from worker.pipeline import rules as rules_mod

        try:
            p = self._project(request.project_path)
            with p.lock:
                if request.to_id and request.from_id == request.to_id:
                    return pb.Ack(ok=False, message="같은 트랙")
                ids = {t.id: t for t in p.tracks()}
                if request.from_id not in ids or (request.to_id and request.to_id not in ids):
                    return pb.Ack(ok=False, message="트랙 없음")
                if request.to_id and ids[request.from_id].cls != ids[request.to_id].cls:
                    return pb.Ack(ok=False, message="클래스가 다른 트랙은 병합할 수 없습니다")
                p.update_track_fields(request.from_id, merged_into=request.to_id or None)
                rules_mod.apply(p)
                p.save()
                self.plans.pop(str(p.path.resolve()), None)
            return pb.Ack(ok=True, message=f"{request.from_id}->{request.to_id}")
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)

    def AuditCheck(self, request, context):
        from worker.pipeline.render import audit_only

        try:
            proj = self.guard.check(request.project_path)
            out = self.guard.check(request.output_path)
            self._drop(str(proj))
            ex = audit_only(proj, out)
            res = pb.AuditResult(exposures=len(ex))
            for e in ex[:1000]:
                res.exposure_boxes.add(frame=e["frame"], x=e["x"], y=e["y"], w=e["w"], h=e["h"], conf=e["conf"])
            return res
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)

    def GetFrame(self, request, context):
        from worker.pipeline.frames import to_jpeg
        from worker.pipeline.mask import RenderProfile, apply_masks, plan_for_project

        try:
            p = self._project(request.project_path)
            media = p.media()
            pts = p.frame_pts(request.frame)
            img = self.readers.get(media["path"]).get(pts, int(request.frame))
            if request.masked:
                key = str(p.path.resolve())
                prof_d = profile_from_pb(request.profile) if request.profile.style else \
                    json.loads(p.get_meta("render_profile") or "{}")
                prof = RenderProfile.from_dict(prof_d)
                pk = (json.dumps(prof.__dict__, sort_keys=True))
                cached = self.plans.get(key)
                if cached is None or cached[0] != pk:
                    with p.lock:
                        cached = (pk, plan_for_project(p, prof))
                    self.plans[key] = cached
                img = apply_masks(img, cached[1].at(int(request.frame)), prof)
            return pb.FrameJpeg(jpeg=to_jpeg(img, request.max_width or 0))
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)

    def TrackObject(self, request, context):
        from worker.pipeline.assist import track_object

        try:
            p = self._project(request.project_path)
            b = request.box
            with p.lock:
                r = track_object(p, int(request.frame), (b.x, b.y, b.w, b.h), request.cls or "other",
                                 max_frames=request.max_frames or 300, both=request.both_directions)
            res = pb.TrackObjectResult(start_f=r["start_f"], end_f=r["end_f"],
                                       method=",".join(f"{k}:{v}" for k, v in r["methods"].items()))
            for f, x, y, w, h in r["boxes"]:
                res.boxes.add(frame=f, x=x, y=y, w=w, h=h, conf=1.0)
            return res
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)

    def DismissExposures(self, request, context):
        from worker.pipeline.dismiss import dismiss

        try:
            p = self._project(request.project_path)
            out = self.guard.check(request.output_path)
            items = [{"frame": b.frame, "cls": c, "x": b.x, "y": b.y, "w": b.w, "h": b.h}
                     for b, c in zip(request.items, request.classes, strict=True)]
            with p.lock:
                r = dismiss(p, out, items, request.actor, request.reason)
            res = pb.DismissResult(accepted=len(r["accepted"]), refused=len(r["refused"]), remaining=r["remaining"])
            for a in r["accepted"]:
                res.accepted_items.add(frame=a["frame"], x=a["x"], y=a["y"], w=a["w"], h=a["h"], conf=a["conf"])
            return res
        except ValueError as e:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))
        except Exception as e:  # noqa: BLE001
            self._fail(context, e)

    def MatchReference(self, request, context):
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "참조 사진 매칭은 V1(G4-02, 자체 학습 ArcFace) 이후 지원")

    def SegmentTrack(self, request, context):
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "세그먼트 마스크는 V2(G5-01)")

    def shutdown(self) -> None:
        for c in list(self.jobs.values()):
            c.cancel()
        with self.lock:
            for p in self.projects.values():
                p.close()
            self.projects.clear()
        self.readers.clear()


def serve(port: int, token: str, roots: list[str], parent_pid: int = 0, block: bool = True):
    guard = PathGuard(roots)
    svc = Service(guard)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16),
                         interceptors=[TokenInterceptor(token)],
                         options=[("grpc.max_send_message_length", MAX_MSG),
                                  ("grpc.max_receive_message_length", MAX_MSG)])
    pbg.add_NuriBlurWorkerServicer_to_server(svc, server)
    bound = server.add_insecure_port(f"127.0.0.1:{port}")
    if not bound:
        raise RuntimeError(f"포트 {port} 바인딩 실패")
    server.start()
    print(f"NURIBLUR_WORKER_READY port={bound}", flush=True)
    if parent_pid:
        def watch():
            while _pid_alive(parent_pid):
                time.sleep(2)
            svc.shutdown()
            server.stop(0)
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
    if block:
        try:
            server.wait_for_termination()
        finally:
            svc.shutdown()
    return server, svc, bound


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nuriblur-worker")
    ap.add_argument("--port", type=int, default=int(os.environ.get("NURIBLUR_PORT", "0")))
    ap.add_argument("--allowed-root", action="append", default=[])
    ap.add_argument("--parent-pid", type=int, default=0)
    a = ap.parse_args(argv)
    token = os.environ.get("NURIBLUR_TOKEN", "")
    if len(token) < 32:
        print("NURIBLUR_TOKEN(32자 이상) 환경변수가 필요합니다", file=sys.stderr)
        return 2
    roots = a.allowed_root + [r for r in os.environ.get("NURIBLUR_ALLOWED_ROOTS", "").split(os.pathsep) if r]
    serve(a.port, token, roots, a.parent_pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
