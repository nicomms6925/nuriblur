"""작업 제어(pause/resume/cancel)와 이벤트 발행."""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from worker.errors import Cancelled

Emit = Callable[[dict[str, Any]], None]


def new_job_id() -> str:
    return "j-" + uuid.uuid4().hex[:12]


class JobControl:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self._run = threading.Event()
        self._run.set()
        self._cancel = threading.Event()

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._run.set()

    @property
    def paused(self) -> bool:
        return not self._run.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self, on_pause: Callable[[], None] | None = None) -> None:
        """루프마다 호출: 취소면 예외, 일시정지면 재개까지 대기."""
        if self._cancel.is_set():
            raise Cancelled("사용자가 취소했습니다")
        if not self._run.is_set():
            if on_pause:
                on_pause()
            while not self._run.wait(0.2):
                pass
            if self._cancel.is_set():
                raise Cancelled("사용자가 취소했습니다")


class Throttle:
    def __init__(self, interval: float):
        self.interval = interval
        self.t = 0.0

    def ready(self) -> bool:
        now = time.monotonic()
        if now - self.t >= self.interval:
            self.t = now
            return True
        return False


def event(type_: str, job_id: str, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"type": type_, "job_id": job_id}
    d.update(kw)
    return d
