"""G2-09 pytest-qt 시나리오: 실제 워커 프로세스로 1→6단계, 취소·재개, 워커 크래시 복구."""
from __future__ import annotations

import os
import shutil

import pytest

pytestmark = [pytest.mark.ui, pytest.mark.slow]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("NURIBLUR_ORG_DB", str(tmp_path / "org.sqlite"))
    from app import theme
    from app.views.main_window import MainWindow
    from app.worker_client import WorkerClient

    qtbot_app = qtbot  # noqa: F841
    from PySide6.QtWidgets import QApplication

    QApplication.instance().setStyleSheet(theme.qss())
    w = WorkerClient()
    m = MainWindow(w, org_mode=True)
    qtbot.addWidget(m)
    w.start()
    m.show()
    qtbot.waitUntil(lambda: "ONNX" in m.top.ep.text(), timeout=120_000)
    yield m
    for t in m.streams.values():
        t.abort()
        t.wait(5000)
    w.stop()


@pytest.fixture
def clip(tmp_path, short_clip):
    dst = tmp_path / "clip.mp4"
    shutil.copy(short_clip, dst)
    return dst


def test_full_flow_to_delivery(win, qtbot, clip):
    from app.state.machine import S

    win.add_files([clip])
    qtbot.waitUntil(lambda: win.job is not None and win.job.media is not None, timeout=60_000)
    assert win.step == 1 and win.max_step() == 1
    win.input._start()
    qtbot.waitUntil(lambda: win.step == 3, timeout=300_000)
    assert win.job.s == S.RULES_APPLIED and win.job.tracks
    win.seek(2)
    qtbot.waitUntil(lambda: bool(win.stage.canvas.boxes) and not win.fetcher.busy, timeout=60_000)
    face = next(b.tid for b in win.stage.canvas.boxes if win.job.tracks[b.tid].cls == "face")
    win.toggle_track(face)
    qtbot.waitUntil(lambda: win.job.status_of(face) == "protect", timeout=60_000)
    assert win.job.counters()[0] >= 1
    win.go(4)
    win.go(5)
    win.export._start()
    qtbot.waitUntil(lambda: win.job.s in (S.AUDITED, S.REVIEWING), timeout=600_000)
    assert win.job.s == S.AUDITED, win.job.exposures[:3]
    qtbot.waitUntil(lambda: win.step == 6, timeout=10_000)
    win._case_info("2026-TEST-1", "개인정보보호법 §35 열람", "홍○○")
    win._approve("검수 완료")        # 1단계(담당자 검수) → 승인 요청
    assert win.job.s == S.PENDING_APPROVAL
    win._reject("")                  # 사유 없으면 거부
    assert win.approval.case(win.job.case_id)["status"] == "PENDING_APPROVAL"
    win._approve("팀장 확인")
    win._approve("부서장 확인")
    assert win.job.s == S.APPROVED and win.orgp.out_b.isEnabled()
    win._deliver(90)
    assert win.job.s == S.DELIVERED
    case = win.approval.case(win.job.case_id)
    assert case["status"] == "DELIVERED" and case["report_path"] and os.path.exists(case["report_path"])
    from worker.orgmode.auditlog import AuditLog

    actions = [e["action"] for e in AuditLog(win.org).entries(win.job.case_id)]
    for a in ("작업 생성", "분석 완료", "보호대상 지정", "렌더링 완료", "검수 완료", "승인", "보고서 생성", "출력본 제공"):
        assert a in actions, (a, actions)
    assert AuditLog(win.org).verify()[0]


def test_cancel_then_resume(win, qtbot, clip):
    from app.state.machine import S

    win.add_files([clip])
    qtbot.waitUntil(lambda: win.job is not None and win.job.media is not None, timeout=60_000)
    win.input.profile.set_value("cpu")
    win.input._start()
    qtbot.waitUntil(lambda: win.job.progress > 5, timeout=120_000)
    win._cancel()
    qtbot.waitUntil(lambda: win.job.s == S.CANCELLED, timeout=60_000)
    assert win.max_step() == 2
    win.input._start()
    qtbot.waitUntil(lambda: win.step == 3, timeout=300_000)
    assert win.job.tracks


def test_worker_crash_recovery(win, qtbot, clip):
    """분석 중 워커가 죽으면 UI가 재기동하고 체크포인트부터 이어서 분석을 끝낸다."""
    win.add_files([clip])
    qtbot.waitUntil(lambda: win.job is not None and win.job.media is not None, timeout=60_000)
    win.input._start()
    qtbot.waitUntil(lambda: win.job.progress > 10, timeout=120_000)
    win.worker.kill_for_test()
    qtbot.waitUntil(lambda: win.worker.restarts >= 1, timeout=30_000)
    qtbot.waitUntil(lambda: win.step == 3, timeout=300_000)
    assert win.job.tracks
    assert "체크포인트" in win.monitor.log.toPlainText()
