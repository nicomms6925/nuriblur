"""GPU 사용률·VRAM 모니터 (UI 게이지·bench용).

NVIDIA: `nvidia-smi` 를 1초마다 조회(드라이버에 포함, 추가 의존성 없음). 없으면 -1(UI는 '—').
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

_QUERY = ["--query-gpu=name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]


def nvidia_smi() -> str | None:
    p = shutil.which("nvidia-smi")
    if p:
        return p
    for c in (r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.exists(c):
            return c
    return None


def query() -> dict | None:
    exe = nvidia_smi()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *_QUERY], capture_output=True, text=True, timeout=3,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = r.stdout.strip().splitlines()[0]
        name, util, used, total = [x.strip() for x in line.split(",")]
        return {"name": name, "util": float(util) / 100.0, "vram_mb": int(float(used)), "vram_total_mb": int(float(total))}
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


class GpuMonitor:
    """백그라운드에서 GPU 상태를 갱신하고 최대값을 기록."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.last: dict | None = None
        self.peak_util = 0.0
        self.peak_vram = 0
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.available = nvidia_smi() is not None

    def start(self) -> GpuMonitor:
        if self.available:
            self._t = threading.Thread(target=self._run, name="nb-gpu", daemon=True)
            self._t.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            q = query()
            if q:
                self.last = q
                self.peak_util = max(self.peak_util, q["util"])
                self.peak_vram = max(self.peak_vram, q["vram_mb"])
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()

    def util(self) -> float:
        return self.last["util"] if self.last else -1.0

    def vram_mb(self) -> int:
        return self.last["vram_mb"] if self.last else -1

    def __enter__(self) -> GpuMonitor:
        return self.start()

    def __exit__(self, *a) -> None:
        self.stop()


def wait_first(mon: GpuMonitor, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while mon.available and mon.last is None and time.monotonic() < end:
        time.sleep(0.05)
