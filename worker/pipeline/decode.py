"""decode (G1-02) — 디코더 스레드 + 링버퍼.

프레임 인덱스 = 디코드(표시) 순서. 회전 메타를 적용해 바로 선 BGR 프레임을 낸다.
HW 디코드는 GPU EP가 있을 때만 시도하고, 열기에 실패하면 SW로 폴백하며 경고 콜백을 부른다.
"""
from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np

from worker.errors import Cancelled, CodecError

RING = 64


@dataclass
class Frame:
    index: int
    pts: int | None
    bgr: np.ndarray


def upright(img: np.ndarray, rotation: int) -> np.ndarray:
    k = (rotation // 90) % 4
    return np.ascontiguousarray(np.rot90(img, k)) if k else img


def _hw_types() -> list[str]:
    if os.environ.get("NURIBLUR_HWDEC", "auto") == "0":
        return []
    try:
        from worker.models.registry import available_providers

        provs = available_providers()
    except Exception:
        return []
    if "CUDAExecutionProvider" in provs:
        return ["cuda", "d3d11va"]
    if "DmlExecutionProvider" in provs:
        return ["d3d11va", "qsv"]
    return []


def open_input(path: str | Path, warn: Callable[[str, str], None] | None = None) -> av.container.InputContainer:
    tried = []
    for t in _hw_types():
        try:
            from av.codec.hwaccel import HWAccel

            return av.open(str(path), hwaccel=HWAccel(device_type=t, allow_software_fallback=True))
        except Exception as e:  # noqa: BLE001
            tried.append(f"{t}: {e}")
    if tried and warn:
        warn("W_HWDEC_FALLBACK", "HW 디코딩을 사용할 수 없어 SW 디코딩으로 전환합니다 (" + "; ".join(tried) + ")")
    try:
        return av.open(str(path))
    except av.FFmpegError as e:
        raise CodecError(f"영상을 열 수 없습니다: {e}") from e


def iter_frames(path: str | Path, start: int = 0, warn: Callable[[str, str], None] | None = None
                ) -> Iterator[Frame]:
    """동기 프레임 이터레이터 (start 이전 프레임은 디코딩 후 버림)."""
    c = open_input(path, warn)
    try:
        vs = c.streams.video[0]
        vs.thread_type = "AUTO"
        idx = 0
        for fr in c.decode(vs):
            if idx >= start:
                rot = int(round(fr.rotation or 0)) % 360
                img = fr.to_ndarray(format="bgr24")
                yield Frame(idx, fr.pts, upright(img, rot))
            idx += 1
    except av.FFmpegError as e:
        raise CodecError(f"디코딩 오류(프레임 {idx}): {e}") from e
    finally:
        c.close()


class DecoderThread:
    """백그라운드 디코더. 링버퍼(64)가 차면 디코더가 기다린다."""

    _END = object()

    def __init__(self, path: str | Path, start: int = 0, warn: Callable[[str, str], None] | None = None,
                 stop: threading.Event | None = None):
        self.q: queue.Queue = queue.Queue(maxsize=RING)
        self.path, self.start, self.warn = path, start, warn
        self.stop = stop or threading.Event()
        self.error: BaseException | None = None
        self.t = threading.Thread(target=self._run, name="nb-decode", daemon=True)

    def _run(self) -> None:
        try:
            for f in iter_frames(self.path, self.start, self.warn):
                while not self.stop.is_set():
                    try:
                        self.q.put(f, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if self.stop.is_set():
                    return
        except BaseException as e:  # noqa: BLE001
            self.error = e
        finally:
            while True:
                try:
                    self.q.put(self._END, timeout=0.2)
                    break
                except queue.Full:
                    if self.stop.is_set():
                        break

    def __iter__(self) -> Iterator[Frame]:
        self.t.start()
        try:
            while True:
                item = self.q.get()
                if item is self._END:
                    if self.error and not isinstance(self.error, Cancelled):
                        raise self.error
                    return
                yield item
        finally:
            self.stop.set()
