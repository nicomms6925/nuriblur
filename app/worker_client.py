"""워커 프로세스 관리 + gRPC 클라이언트 (G2-02).

- UI가 랜덤 포트·32바이트 토큰을 만들어 환경변수로 워커에 전달하고 127.0.0.1로 접속한다.
- 모든 RPC는 UI 스레드 밖에서 실행한다(긴 작업 중 UI 프리즈 금지). 결과는 Qt 시그널로 돌아온다.
- 워커가 비정상 종료되면 재기동하고 `crashed` 시그널을 보낸다(화면은 체크포인트 재개를 요청).
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import grpc
from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, QTimer, Signal

from app.i18n import tr
from app.pb import nuriblur_pb2 as pb
from app.pb import nuriblur_pb2_grpc as pbg

ROOT = Path(__file__).resolve().parents[1]
MAX_MSG = 256 * 1024 * 1024


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):  # PyInstaller: 같은 exe를 --worker 모드로
        return [sys.executable, "--worker"]
    return [sys.executable, "-m", "worker.server"]


class _CallSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Call(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _CallSignals()

    def run(self) -> None:
        try:
            r = self.fn()
        except grpc.RpcError as e:
            self.signals.failed.emit(e.details() or str(e.code()))
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(repr(e))
        else:
            self.signals.done.emit(r)


class StreamThread(QThread):
    """Analyze/Render 이벤트 스트림을 받아 시그널로 전달."""

    event = Signal(object)
    failed = Signal(str)

    def __init__(self, open_stream: Callable[[], Any], parent=None):
        super().__init__(parent)
        self.open_stream = open_stream
        self.call = None

    def run(self) -> None:
        try:
            self.call = self.open_stream()
            for e in self.call:
                self.event.emit(e)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.CANCELLED:
                self.failed.emit(f"{e.code().name}: {e.details() or ''}")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(repr(e))

    def abort(self) -> None:
        if self.call is not None:
            self.call.cancel()


class WorkerClient(QObject):
    ready = Signal(object)        # pb.WorkerInfo
    crashed = Signal(str)
    log = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: subprocess.Popen | None = None
        self.channel: grpc.Channel | None = None
        self.stub: pbg.NuriBlurWorkerStub | None = None
        self.token = ""
        self.port = 0
        self.roots: set[str] = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self._stopping = False
        self._calls: set[_Call] = set()
        self.restarts = 0
        self.watch = QTimer(self)
        self.watch.setInterval(1500)
        self.watch.timeout.connect(self._check)

    # ---------- 프로세스 ----------
    def start(self) -> None:
        self._stopping = False
        self.token = secrets.token_hex(32)
        self.port = _free_port()
        env = dict(os.environ, NURIBLUR_PORT=str(self.port), NURIBLUR_TOKEN=self.token, PYTHONIOENCODING="utf-8")
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(worker_command() + ["--parent-pid", str(os.getpid())], cwd=str(ROOT), env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags,
                                     text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self.channel = grpc.insecure_channel(f"127.0.0.1:{self.port}", options=[
            ("grpc.max_send_message_length", MAX_MSG), ("grpc.max_receive_message_length", MAX_MSG)])
        self.stub = pbg.NuriBlurWorkerStub(self.channel)

        def wait_ready():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    grpc.channel_ready_future(self.channel).result(timeout=1)
                    info = self.stub.Health(pb.Empty(), metadata=self.md, timeout=60)
                    for r in list(self.roots):
                        self.stub.AllowRoot(pb.AllowRootRequest(path=r), metadata=self.md)
                    return info
                except grpc.FutureTimeoutError:
                    if self.proc and self.proc.poll() is not None:
                        raise RuntimeError(tr("err.worker_exit")) from None
            raise TimeoutError(tr("err.worker_timeout"))

        self.call(wait_ready, self.ready.emit, lambda m: self.crashed.emit(m))
        self.watch.start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout or []:
            self.log.emit(line.rstrip())

    def _check(self) -> None:
        if self._stopping or self.proc is None:
            return
        rc = self.proc.poll()
        if rc is not None:
            self.watch.stop()
            self.restarts += 1
            self.crashed.emit(f"worker exit {rc}")
            if self.restarts <= 5:
                self.start()

    def stop(self) -> None:
        self._stopping = True
        self.watch.stop()
        if self.channel:
            self.channel.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.pool.waitForDone(3000)

    def kill_for_test(self) -> None:
        if self.proc:
            self.proc.kill()

    @property
    def md(self) -> tuple[tuple[str, str], ...]:
        return (("x-nb-token", self.token),)

    # ---------- 호출 ----------
    def call(self, fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
             on_fail: Callable[[str], None] | None = None) -> None:
        c = _Call(fn)
        self._calls.add(c)
        if on_done:
            c.signals.done.connect(on_done)
        if on_fail:
            c.signals.failed.connect(on_fail)
        c.signals.done.connect(lambda *_: self._calls.discard(c))
        c.signals.failed.connect(lambda *_: self._calls.discard(c))
        c.setAutoDelete(True)
        self.pool.start(c)

    def rpc(self, name: str, req, on_done=None, on_fail=None, timeout: float = 120) -> None:
        def fn():
            return getattr(self.stub, name)(req, metadata=self.md, timeout=timeout, wait_for_ready=True)

        self.call(fn, on_done, on_fail)

    def rpc_sync(self, name: str, req, timeout: float = 60):
        return getattr(self.stub, name)(req, metadata=self.md, timeout=timeout)

    def allow(self, path: str | Path) -> None:
        p = Path(path)
        root = str((p if p.is_dir() else p.parent).resolve())
        if root in self.roots:
            return
        self.roots.add(root)
        if self.stub:
            self.rpc("AllowRoot", pb.AllowRootRequest(path=root))

    def stream(self, name: str, req, parent=None) -> StreamThread:
        t = StreamThread(lambda: getattr(self.stub, name)(req, metadata=self.md), parent)
        return t

    def control(self, job_id: str, action: str) -> None:
        self.rpc("Control", pb.ControlRequest(job_id=job_id, action=action))
