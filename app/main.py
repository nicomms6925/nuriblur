"""nuriblur-app 진입점.

  python -m app.main [파일 ...]        UI 실행 (워커 프로세스를 자동으로 띄운다)
  nuriblur-app.exe --worker ...        (PyInstaller 빌드) 같은 실행 파일을 워커 모드로

환경변수: NURIBLUR_EDITION=org|b2c (기본 org — 결재·리포트 단계 표시, URL 입력 숨김)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--worker":
        from worker.server import main as worker_main

        return worker_main(argv[1:])
    if len(argv) == 2 and argv[0] == "--probe-encoder":  # 인코더 시험(크래시 격리용 별도 프로세스)
        from worker.pipeline.encode import _probe_main

        return _probe_main(argv[1])

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from app import theme
    from app.i18n import tr
    from app.views.main_window import MainWindow
    from app.worker_client import WorkerClient
    from worker.errors import NBError

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName(tr("app.name"))
    app.setStyle("Fusion")
    app.setStyleSheet(theme.qss())
    try:
        app.styleHints().colorSchemeChanged.connect(lambda *_: app.setStyleSheet(theme.qss()))
    except AttributeError:
        pass
    org_mode = os.environ.get("NURIBLUR_EDITION", "org") != "b2c"
    worker = WorkerClient()
    try:
        win = MainWindow(worker, org_mode=org_mode)
    except NBError as e:  # 예: 기관 DB 권한 — 원인·해결 방법을 보여 주고 종료
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(QMessageBox.Critical, tr("app.name"), tr("dlg.start_failed"))
        box.setInformativeText(str(e))
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.exec()
        return 1
    worker.start()
    win.show()
    files = [Path(a) for a in argv if Path(a).exists()]
    if files:
        win.add_files(files)
    rc = app.exec()
    worker.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
