"""관리자 권한 실행 시 끌어다 놓기(WM_DROPFILES) — 실제 Windows 창에 탐색기와 같은 메시지를 보내 받는지 확인."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ui, pytest.mark.skipif(sys.platform != "win32", reason="Windows 전용")]

SCRIPT = r'''
import sys, ctypes, os
from ctypes import wintypes
from pathlib import Path
sys.path.insert(0, ".")
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import QTimer
app = QApplication([])
from app.win_drop import enable_dropfiles, WM_DROPFILES
w = QWidget(); w.resize(200, 100); w.show()
got = []
f = enable_dropfiles(int(w.winId()), got.append)
files = [os.path.abspath("README.md"), "C:\경로\한글 영상.mp4"]
payload = ("\0".join(files) + "\0\0").encode("utf-16-le")
k32 = ctypes.windll.kernel32
k32.GlobalAlloc.restype = ctypes.c_void_p; k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]; k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
h = k32.GlobalAlloc(0x0042, 20 + len(payload))
p = k32.GlobalLock(h)
ctypes.memmove(p, (ctypes.c_uint32 * 5)(20, 0, 0, 0, 1), 20)
ctypes.memmove(p + 20, payload, len(payload)); k32.GlobalUnlock(h)
u32 = ctypes.windll.user32
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
u32.PostMessageW(int(w.winId()), WM_DROPFILES, h, None)
QTimer.singleShot(500, app.quit); app.exec()
print("OK" if got and [str(a) for a in got[0]] == [str(Path(x)) for x in files] else f"FAIL {got}")
'''


def test_wm_dropfiles_received():
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"} | {"PYTHONUTF8": "1"}
    r = subprocess.run([sys.executable, "-c", SCRIPT], cwd=Path(__file__).resolve().parents[1], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.stdout.strip().endswith("OK"), r.stdout + r.stderr
