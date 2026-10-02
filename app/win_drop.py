"""관리자 권한으로 실행할 때의 파일 끌어다 놓기 (Windows).

Windows는 권한이 다른 프로그램 사이의 OLE 끌어다 놓기를 막는다(UIPI) — 일반 권한 탐색기에서 관리자 권한으로 실행한
NuriBlur 창에 영상을 놓으면 아무 일도 일어나지 않는다. 이때는 Qt의 OLE 놓기 대상을 해제하고, 예전 방식의
WM_DROPFILES를 허용(ChangeWindowMessageFilterEx)해 받는다.
"""
from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

WM_DROPFILES = 0x0233
WM_COPYDATA = 0x004A
WM_COPYGLOBALDATA = 0x0049
MSGFLT_ALLOW = 1


def is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _shell32():
    s = ctypes.windll.shell32
    s.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    s.DragQueryFileW.restype = wintypes.UINT
    s.DragFinish.argtypes = [ctypes.c_void_p]
    s.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    return s


def dropped_paths(hdrop: int) -> list[Path]:
    s = _shell32()
    n = s.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    out = []
    for i in range(n):
        ln = s.DragQueryFileW(hdrop, i, None, 0)
        buf = ctypes.create_unicode_buffer(ln + 1)
        s.DragQueryFileW(hdrop, i, buf, ln + 1)
        out.append(Path(buf.value))
    return out


class DropFilesFilter(QAbstractNativeEventFilter):
    def __init__(self, hwnd: int, on_files: Callable[[list[Path]], None]):
        super().__init__()
        self.hwnd = hwnd
        self.on_files = on_files

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt API)
        if bytes(event_type) != b"windows_generic_MSG":
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.message != WM_DROPFILES:
            return False, 0
        hdrop = msg.wParam
        try:
            paths = dropped_paths(hdrop)
        finally:
            _shell32().DragFinish(hdrop)
        if paths:
            self.on_files(paths)
        return True, 0


def enable_dropfiles(hwnd: int, on_files: Callable[[list[Path]], None]) -> DropFilesFilter | None:
    """hwnd 창에 WM_DROPFILES 받기를 켠다. 반환된 필터는 호출한 쪽이 참조를 유지해야 한다."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
    for m in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):
        user32.ChangeWindowMessageFilterEx(hwnd, m, MSGFLT_ALLOW, None)
    ctypes.windll.ole32.RevokeDragDrop(wintypes.HWND(hwnd))  # OLE 놓기 대상이 있으면 탐색기가 WM_DROPFILES를 보내지 않는다
    _shell32().DragAcceptFiles(hwnd, True)
    f = DropFilesFilter(hwnd, on_files)
    QCoreApplication.instance().installNativeEventFilter(f)
    return f
