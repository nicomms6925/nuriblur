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
    pins = {"김형남": "111111", "박문화": "222222", "이규홍": "333333"}
    for n, p in pins.items():
        win._register_user(n, p)
    win._save_line(3, "김형남, 박문화, 이규홍")
    win._case_info("2026-TEST-1", "개인정보보호법 §35 열람", "홍○○")
    # 결재선은 처리 건 생성(분석 시작) 시점 스냅샷 → 새 결재선으로 새 처리 건을 연결
    win.job.case_id = win.approval.create_case("2026-TEST-1", "개인정보보호법 §35 열람", "홍○○",
                                               str(win.job.project_path), win.actor())
    win.approval.record_render(win.job.case_id, win.actor(), str(win.job.output_path), win.job.last_output_sha, 0)
    win.ask_pin = lambda role, user: "000000"   # 틀린 PIN → 진행 안 됨
    win._approve("검수 완료")
    assert win.approval.case(win.job.case_id)["status"] == "REVIEWING"
    win.ask_pin = lambda role, user: pins.get(user)
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
    # 결재선을 바꾼 뒤 새로 만든 처리 건이므로 '분석 완료'·'보호대상 지정'은 이전 처리 건에 있다
    for a in ("작업 생성", "렌더링 완료", "본인 확인 실패", "검수 완료", "승인", "보고서 생성", "출력본 제공"):
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


def test_viewer_controls_and_mask_target(win, qtbot, clip):
    """확대·축소·맞춤·원본 크기, 프레임 번호 이동, ±초 이동, 배속 재생·정지, 마스킹 대상 지정(자동 추적)."""
    import time

    from PySide6.QtCore import QRectF

    win.add_files([clip])
    qtbot.waitUntil(lambda: win.job is not None and win.job.media is not None, timeout=60_000)
    win.input._start()
    qtbot.waitUntil(lambda: win.step == 3, timeout=300_000)
    cv = win.stage.canvas
    fit = cv.zoom()
    cv.zoom_in()
    assert cv.zoom() > fit and not cv.fit_mode
    cv.actual_size()
    assert abs(cv.zoom() * cv.devicePixelRatioF() - 1.0) < 1e-6
    cv.fit()
    assert cv.fit_mode and abs(cv.zoom() - fit) < 1e-6
    win.stage.frame_box.setValue(20)
    assert win.frame == 20
    win.stage.step_seconds.emit(-10)
    assert win.frame == 0
    win.stage.speed.setCurrentIndex(win.stage.speed.findData(2.0))
    win.stage._toggle_play()
    t0 = time.monotonic()
    qtbot.waitUntil(lambda: win.frame >= 20 or time.monotonic() - t0 > 20, timeout=30_000)
    win.stage._stop()
    assert win.frame == 0 and not win.play_timer.isActive()
    # 놓친 얼굴 지정 → 앞뒤 자동 추적 → manual_box 규칙
    win.seek(10)
    face = next(t for t in win.job.tracks.values() if t.cls == "face" and t.start_f <= 10 <= t.end_f)
    qtbot.waitUntil(lambda: not win.fetcher.busy, timeout=30_000)
    n_rules = len(win.job.rules)
    win.stage.set_mask_tool(True)
    at = win.worker.rpc_sync("ListTracks", __import__("app.pb.nuriblur_pb2", fromlist=["x"]).ListTracksRequest(
        project_path=str(win.job.project_path), at_frame=10))
    b = next(t.boxes[0] for t in at.tracks if t.id == face.id)
    win.apply_mask_target(QRectF(b.x, b.y, b.w, b.h), "face", True)
    qtbot.waitUntil(lambda: len(win.job.rules) > n_rules, timeout=120_000)
    rule = win.job.rules[-1]
    assert rule["kind"] == "manual_box" and rule["payload"]["cls"] == "face"
    assert len(rule["payload"]["frames"]) > 10
