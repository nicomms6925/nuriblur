"""MainWindow — 레이아웃 + 6단계 흐름 컨트롤러 (G2-01~08, G3 UI).

레이아웃: TopBar / QSplitter(Queue 260 · Stage · Side 320) / 하단 Timeline(160)
모든 RPC는 WorkerClient가 스레드에서 실행하고, 결과는 시그널 콜백으로 받는다.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.i18n import tr
from app.pb import nuriblur_pb2 as pb
from app.session import JobItem
from app.state.machine import STEP_OF, S
from app.views.queue_panel import QueuePanel
from app.views.side_panels import ExportPanel, InputPanel, MonitorPanel, OrgPanel, ProtectPanel, ReviewPanel
from app.views.stage_view import StageView, timecode
from app.views.timeline import Lane, TimelinePanel
from app.views.topbar import TopBar
from app.widgets.common import Toast, TrackRowW, button
from app.worker_client import WorkerClient
from worker.orgmode import auditlog as A

CLS_LABEL = {"face": "cls.face", "person": "cls.person", "plate": "cls.plate"}


class FrameFetcher:
    """스크러빙 요청 합치기: 진행 중이면 마지막 요청만 남겨 두었다가 이어서 보낸다."""

    def __init__(self, win: MainWindow):
        self.w = win
        self.busy = False
        self.pending: tuple | None = None

    def request(self, *args) -> None:
        if self.busy:
            self.pending = args
            return
        self._go(*args)

    def _go(self, job: JobItem, frame: int, mode: str, profile: pb.RenderProfile | None, with_boxes: bool) -> None:
        self.busy = True
        stub, md = self.w.worker.stub, self.w.worker.md
        proj = str(job.project_path)
        # 화면 확대가 크면 원본 해상도로 받아 선명하게
        z = self.w.stage.canvas.zoom() * self.w.stage.canvas.devicePixelRatioF()
        max_w = 0 if (job.media and z * job.media.width > 1300) else 1280

        def fn():
            def get(masked: bool) -> bytes:
                req = pb.FrameRequest(project_path=proj, frame=frame, masked=masked, max_width=max_w)
                if masked and profile is not None:
                    req.profile.CopyFrom(profile)
                return stub.GetFrame(req, metadata=md, timeout=30).jpeg

            imgs = [get(False)] if mode == "orig" else [get(True)] if mode == "mask" else [get(False), get(True)]
            tl = stub.ListTracks(pb.ListTracksRequest(project_path=proj, at_frame=frame), metadata=md,
                                 timeout=30) if with_boxes else None
            return job, frame, mode, imgs, tl

        self.w.worker.call(fn, self._done, self._fail)

    def _done(self, res) -> None:
        self.busy = False
        self.w.on_frame(*res)
        self._next()

    def _fail(self, msg: str) -> None:
        self.busy = False
        self.w.monitor.append_log("WARN", tr("log.frame_fail", msg=msg))
        self._next()

    def _next(self) -> None:
        if self.pending:
            args, self.pending = self.pending, None
            self._go(*args)


class MainWindow(QMainWindow):
    def __init__(self, worker: WorkerClient, org_mode: bool = True, org_db: str | None = None):
        super().__init__()
        self.worker = worker
        self.org_mode = org_mode
        self.jobs: list[JobItem] = []
        self.cur = -1
        self.step = 1
        self.frame = 0
        self.view_mode = "orig"
        self.streams: dict[int, Any] = {}
        self.manual_kf: tuple[int, QRectF] | None = None
        self.selected_tid = -1
        self.gpu = False
        self.setWindowTitle(tr("app.title"))
        self.resize(1440, 900)
        self._build()
        self.fetcher = FrameFetcher(self)
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._play_tick)
        self.play_t0 = 0.0
        self.play_f0 = 0
        self._zoom_refetch = QTimer(self)
        self._zoom_refetch.setSingleShot(True)
        self._zoom_refetch.setInterval(250)
        self._zoom_refetch.timeout.connect(self.refresh_frame)
        self.org = self.approval = None
        if org_mode:
            from worker.orgmode.approval import Approval
            from worker.orgmode.db import OrgDB

            self.org = OrgDB(org_db)
            self.approval = Approval(self.org)
            self._purge()
            self.purge_timer = QTimer(self)
            self.purge_timer.timeout.connect(self._purge)
            self.purge_timer.start(24 * 3600 * 1000)
            self._fill_line_names()
        worker.ready.connect(self._worker_ready)
        worker.crashed.connect(self._worker_crashed)
        self.go(1)

    # ------------------------------------------------------------------ 레이아웃
    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.top = TopBar(self.org_mode)
        self.top.step_clicked.connect(self.go)
        v.addWidget(self.top)
        vs = QSplitter(Qt.Vertical)
        hs = QSplitter(Qt.Horizontal)
        self.queue = QueuePanel(self.org_mode)
        self.queue.files_added.connect(self.add_files)
        self.queue.selected.connect(self.select)
        self.stage = StageView()
        self.stage.canvas.box_clicked.connect(self.toggle_track)
        self.stage.canvas.rect_drawn.connect(self._on_rect)
        self.stage.seek.connect(self.seek)
        self.stage.step_frame.connect(lambda d: self.seek(self.frame + d))
        self.stage.step_seconds.connect(lambda sec: self.seek(self.frame + int(round(sec * (self.job.fps() if self.job else 30)))))
        self.stage.play_toggled.connect(self._play)
        self.stage.stop_clicked.connect(self._stop)
        self.stage.speed_changed.connect(lambda _: self._play(True) if self.stage.playing else None)
        self.stage.mask_tool.connect(self._mask_tool_toggled)
        self.stage.canvas.zoom_changed.connect(lambda _: self._zoom_refetch.start())
        side_frame = QFrame()
        side_frame.setObjectName("side")
        sl = QVBoxLayout(side_frame)
        sl.setContentsMargins(0, 0, 0, 0)
        self.side = QStackedWidget()
        self.input = InputPanel()
        self.monitor = MonitorPanel()
        self.protect = ProtectPanel()
        self.review = ReviewPanel()
        self.export = ExportPanel(self.org_mode)
        self.orgp = OrgPanel()
        for p in (self.input, self.monitor, self.protect, self.review, self.export, self.orgp):
            sc = QScrollArea()
            sc.setObjectName("side")
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            sc.setWidget(p)
            self.side.addWidget(sc)
        sl.addWidget(self.side)
        hs.addWidget(self.queue)
        hs.addWidget(self.stage)
        hs.addWidget(side_frame)
        hs.setSizes([260, 860, 320])
        hs.setStretchFactor(1, 1)
        self.timeline = TimelinePanel()
        self.timeline.seek.connect(self.seek)
        self.timeline.track_selected.connect(self._select_track)
        vs.addWidget(hs)
        vs.addWidget(self.timeline)
        vs.setSizes([700, 170])
        vs.setStretchFactor(0, 1)
        v.addWidget(vs, 1)
        self.setCentralWidget(root)
        self.toast = Toast(root)
        # 시그널
        self.input.start.connect(self.start_analysis)
        self.monitor.pause.connect(self._pause)
        self.monitor.cancel.connect(self._cancel)
        self.protect.toggle.connect(lambda tid, on: self.toggle_track(tid, on))
        self.protect.focus.connect(self._select_track)
        self.protect.add_plate.connect(self._add_plate)
        self.review.merge.connect(self._merge)
        self.review.manual_box.connect(self._manual_mode)
        self.review.body_mask.connect(self._body_mask)
        self.review.preview_mode.connect(self._preview_mode)
        self.review.done.connect(lambda: self.go(5))
        self.export.start.connect(self.start_render)
        self.export.style_changed.connect(lambda _: self.refresh_frame())
        self.orgp.create_case.connect(self._case_info)
        self.orgp.save_line.connect(self._save_line)
        self.orgp.preset.currentIndexChanged.connect(lambda _: self._fill_line_names())
        self.orgp.approve.connect(self._approve)
        self.orgp.reject.connect(self._reject)
        self.orgp.make_pdf.connect(self._make_pdf)
        self.orgp.deliver.connect(self._deliver)
        self.orgp.export_log.connect(self._export_log)
        self.orgp.register_user.connect(self._register_user)
        fps = lambda: self.job.fps() if self.job else 30.0  # noqa: E731
        for keys, fn in (
            ("Left", lambda: self.seek(self.frame - 1)), ("Right", lambda: self.seek(self.frame + 1)),
            ("Shift+Left", lambda: self.seek(self.frame - int(fps()))), ("Shift+Right", lambda: self.seek(self.frame + int(fps()))),
            ("Ctrl+Left", lambda: self.seek(self.frame - int(10 * fps()))),
            ("Ctrl+Right", lambda: self.seek(self.frame + int(10 * fps()))),
            ("Home", lambda: self.seek(0)), ("End", lambda: self.seek(10 ** 9)),
            ("Space", lambda: self.stage._toggle_play()),
            ("+", lambda: self.stage.canvas.zoom_in()), ("=", lambda: self.stage.canvas.zoom_in()),
            ("-", lambda: self.stage.canvas.zoom_out()), ("0", lambda: self.stage.canvas.fit()),
            ("1", lambda: self.stage.canvas.actual_size()),
            ("Escape", lambda: self.stage.set_mask_tool(False, self.step in (3, 4))),
        ):
            QShortcut(QKeySequence(keys), self, fn)

    # ------------------------------------------------------------------ 공통
    @property
    def job(self) -> JobItem | None:
        return self.jobs[self.cur] if 0 <= self.cur < len(self.jobs) else None

    def say(self, msg: str) -> None:
        self.toast.say(msg)

    def actor(self) -> str:
        from worker.orgmode.db import current_user

        return current_user()

    def _refresh_queue(self, job: JobItem) -> None:
        if job in self.jobs:
            self.queue.refresh(self.jobs.index(job), job)

    def max_step(self) -> int:
        j = self.job
        if j is None:
            return 1
        if j.tracks:
            return 6 if self.org_mode else 5
        return 2 if S.ANALYZING in j.state.history else 1

    def go(self, n: int) -> None:
        n = max(1, min(n, self.max_step() if self.job else 1))
        self.step = n
        self.top.set_step(n, self.max_step())
        self.side.setCurrentIndex(n - 1)
        self.stage.show_legend(n in (3, 4))
        self.stage.canvas.draw_mode = False
        self.review.manual_b.setChecked(False)
        self.stage.set_mask_tool(False, n in (3, 4))
        j = self.job
        if n == 6 and self.org_mode:
            self._refresh_org()
        else:
            self.stage.show_report(None)
        if n == 5 and j:
            if not self.export.out.text() or Path(self.export.out.text()).stem.replace("_blur", "") != j.path.stem:
                self.export.out.setText(str(j.output_path))
            p, m, _ = j.counters()
            self.export.set_summary(p, m)
        if n == 4:
            self._refresh_review()
        if n == 3:
            self._refresh_protect_counts()
        self.view_mode = {5: "mask", 6: "mask"}.get(n, self.review.seg.value() if n == 4 else "orig")
        self.stage.mode.setText({"orig": tr("view.original"), "mask": tr("view.mask_preview"),
                                 "split": tr("view.split")}[self.view_mode])
        if n != 2 and j and j.tracks:
            self.refresh_frame()

    # ------------------------------------------------------------------ 워커
    def _worker_ready(self, info: pb.WorkerInfo) -> None:
        self.gpu = info.ep in ("CUDA", "DirectML", "CoreML")
        self.top.set_ep(f"{info.ep} · ONNX Runtime" + ("" if self.gpu else f" · {tr('top.cpu_note')}"), self.gpu)
        self.input.set_runtime(self.gpu)
        self.monitor.append_log("INFO", tr("log.worker_ready", ep=info.ep, enc=", ".join(info.encoders[:3]) or "-"))
        for j in self.jobs:  # 크래시 복구: 분석 중이던 작업은 체크포인트부터 재개
            if j.analysis_running:
                self.monitor.append_log("WARN", tr("log.resume_after_crash", name=j.name))
                self._run_analysis(j, j.classes, j.profile)

    def _worker_crashed(self, msg: str) -> None:
        self.monitor.append_log("ERROR", tr("log.worker_crashed", msg=msg))
        for j in self.jobs:
            if j.s == S.RENDERING:
                j.state.go(S.FAILED)
                j.error = msg
                self._refresh_queue(j)

    # ------------------------------------------------------------------ 1 입력
    def add_files(self, paths: list[Path]) -> None:
        for p in paths:
            p = Path(p)
            proj = None
            if p.suffix.lower() == ".nbproj":
                try:
                    from worker.io.project import Project

                    with Project.open(p) as pr:
                        video = Path(pr.media()["path"])
                    proj, p = p, video
                except Exception as e:  # noqa: BLE001
                    self.say(tr("toast.proj_open_fail", msg=str(e)))
                    continue
            if any(j.path == p for j in self.jobs):
                continue
            job = JobItem(p, project_path=proj)
            self.jobs.append(job)
            self.queue.add(job)
            self.worker.allow(p)
            self.worker.allow(job.project_path)
            self.worker.rpc("Probe", pb.ProbeRequest(path=str(p)), lambda m, j=job: self._probed(j, m),
                            lambda e, j=job: self._failed(j, e))
        if self.cur < 0 and self.jobs:
            self.queue.select(0)

    def _probed(self, job: JobItem, m: pb.MediaInfo) -> None:
        job.media = m
        job.state.go(S.PROBED)
        self._refresh_queue(job)
        if job.project_path.exists():  # 기존 프로젝트 → 바로 불러오기
            self._load_tracks(job, after=lambda: self._loaded_existing(job))
        if self.job is job:
            self.select(self.cur)

    def _loaded_existing(self, job: JobItem) -> None:
        if job.tracks:
            if job.s == S.PROBED:
                job.state.go(S.ANALYZED)
                job.state.go(S.RULES_APPLIED)
            self._refresh_queue(job)
            if self.job is job:
                self.select(self.cur)

    def _failed(self, job: JobItem, msg: str) -> None:
        job.error = msg
        self._refresh_queue(job)
        self.monitor.append_log("ERROR", f"{job.name}: {msg}")
        self.say(tr("toast.error", msg=msg[:120]))

    def select(self, idx: int) -> None:
        if not (0 <= idx < len(self.jobs)):
            return
        self.cur = idx
        j = self.jobs[idx]
        self.input.set_media(j.name, j.media)
        self.frame = 0
        if j.media:
            self.stage.set_media(j.frames(), j.fps(), j.media.width, j.media.height)
        self._rebuild_timeline()
        target = STEP_OF.get(j.s, 1)
        self.go(target if j.s not in (S.ANALYZING, S.PAUSED, S.RENDERING) else 2)
        if j.tracks:
            self.refresh_frame()

    # ------------------------------------------------------------------ 2 분석
    def start_analysis(self, classes: list[str], profile: str) -> None:
        j = self.job
        if j is None or j.media is None:
            self.say(tr("toast.no_video"))
            return
        if not classes:
            self.say(tr("toast.no_classes"))
            return
        if self.org_mode and not j.case_id:
            existing = self.approval.case_by_project(str(j.project_path))
            j.case_id = existing["id"] if existing else self.approval.create_case(
                "", tr("basis.35"), "", str(j.project_path), self.actor())
        self._run_analysis(j, classes, profile)

    def _run_analysis(self, j: JobItem, classes: list[str], profile: str) -> None:
        if j.s not in (S.ANALYZING,):
            j.state.go(S.ANALYZING)
        j.analysis_running = True
        j.classes, j.profile = classes, profile
        j.job_id = "j-" + datetime.now().strftime("%H%M%S%f")
        self.monitor.set_mode("analyze")
        self.monitor.append_log("INFO", tr("log.analyze_start", name=j.name, profile=tr(f"profile.{profile}")))
        req = pb.AnalyzeRequest(job_id=j.job_id, path=str(j.path), project_path=str(j.project_path),
                                classes=classes, profile=profile, detect_interval=0, resume_from_frame=-1)
        t = self.worker.stream("Analyze", req, self)
        t.event.connect(lambda e, jj=j: self._on_event(jj, e, "analyze"))
        t.failed.connect(lambda m, jj=j: self._stream_failed(jj, m))
        self.streams[id(j)] = t
        t.start()
        self._refresh_queue(j)
        if self.job is j:
            self.go(2)

    def _pause(self) -> None:
        j = self.job
        if j is None or not j.job_id:
            return
        if j.s == S.PAUSED:
            self.worker.control(j.job_id, "resume")
            j.state.resume()
            self.monitor.pause_b.setText(tr("mon.pause"))
            self.say(tr("toast.resumed"))
        elif j.s in (S.ANALYZING, S.RENDERING):
            self.worker.control(j.job_id, "pause")
            j.state.go(S.PAUSED)
            self.monitor.pause_b.setText(tr("mon.resume"))
            self.say(tr("toast.paused"))
        self._refresh_queue(j)

    def _cancel(self) -> None:
        j = self.job
        if j and j.job_id and j.s in (S.ANALYZING, S.RENDERING, S.PAUSED):
            self.worker.control(j.job_id, "cancel")

    def _stream_failed(self, j: JobItem, msg: str) -> None:
        if msg.startswith("UNAVAILABLE") and j.analysis_running:
            # 워커 크래시: 재기동 후 _worker_ready 에서 체크포인트부터 재개
            self.monitor.append_log("WARN", tr("log.worker_crashed", msg=msg))
            return
        j.analysis_running = False
        if j.s in (S.ANALYZING, S.RENDERING, S.PAUSED):
            j.state.go(S.FAILED)
        j.error = msg
        self._refresh_queue(j)
        self.monitor.append_log("ERROR", msg)

    def _on_event(self, j: JobItem, e: pb.Event, kind: str) -> None:
        mine = self.job is j
        if e.type == "progress":
            if e.total:
                j.progress = 100.0 * e.frame / e.total
            if mine:
                self.monitor.bar.setValue(int(10 * j.progress))
                self.monitor.pct.setText(f"{int(j.progress)}%")
                self.monitor.stage.setText(tr(f"stagel.{e.stage}"))
                self.monitor.mark(e.stage)
                if e.eta_s:
                    self.monitor.eta.setText(f"{e.eta_s // 60:02d}:{e.eta_s % 60:02d}")
                if e.fps:
                    self.monitor.g_fps.set(f"{e.fps:.1f}")
                self.monitor.g_gpu.set("—" if e.gpu_util < 0 else f"{int(e.gpu_util * 100)}%")
                self.monitor.g_vram.set("—" if e.vram_mb < 0 else f"{e.vram_mb / 1024:.1f} GB")
                if e.frame and self.step == 2:
                    self.stage.set_position(min(e.frame, max(j.frames() - 1, 0)), j.frames())
            self._refresh_queue(j)
        elif e.type == "stats" and mine:
            self.monitor.c_face.set(str(e.counts.get("faces", 0)))
            self.monitor.c_person.set(str(e.counts.get("persons", 0)))
            self.monitor.c_plate.set(str(e.counts.get("plates", 0)))
        elif e.type == "preview" and mine and self.step == 2:
            pm = QPixmap()
            if pm.loadFromData(e.preview_jpeg):
                self.stage.canvas.set_boxes([])
                self.stage.canvas.set_frame(pm)
        elif e.type in ("log", "warning"):
            if mine or e.type == "warning":
                self.monitor.append_log("INFO" if e.type == "log" else "WARN",
                                        (f"{e.code} " if e.code else "") + e.message)
        elif e.type == "exposure":
            d = json.loads(e.message or "{}")
            j.exposures.append({"frame": e.frame, "cls": e.code, **d})
        elif e.type == "error":
            self.monitor.append_log("ERROR", f"{e.code} {e.message}")
            if e.code == "E_AUDIT_EXPOSURE":
                return
            j.analysis_running = False
            if e.code == "E_CANCELLED":
                j.state.go(S.CANCELLED)
                self.say(tr("toast.cancelled"))
            else:
                j.state.go(S.FAILED)
                j.error = f"{e.code} {e.message}"
                self.say(tr("toast.error", msg=e.message[:120]))
            self._refresh_queue(j)
            self.top.set_step(self.step, self.max_step())
        elif e.type == "done":
            if kind == "analyze":
                self._analysis_done(j, e)
            else:
                self._render_done(j, e)

    def _analysis_done(self, j: JobItem, e: pb.Event) -> None:
        j.analysis_running = False
        j.progress = 100
        if j.s == S.PAUSED:
            j.state.resume()
        j.state.go(S.ANALYZED)
        self.monitor.mark("", done_all=True)
        self.monitor.stage.setText(tr("mon.analyze_done"))
        self.monitor.pause_b.setEnabled(False)
        self.monitor.cancel_b.setEnabled(False)
        if self.approval and j.case_id:
            self.approval.log_event(j.case_id, tr("audit.system"), A.ANALYZED,
                                    f"F{e.counts.get('faces', 0)} P{e.counts.get('persons', 0)} LP{e.counts.get('plates', 0)}")

        def after():
            j.state.go(S.RULES_APPLIED)
            self._refresh_queue(j)
            if self.job is j:
                self.say(tr("toast.analyze_done"))
                QTimer.singleShot(700, lambda: self.go(3))

        self._load_tracks(j, after)

    # ------------------------------------------------------------------ 트랙·판정
    def _load_tracks(self, j: JobItem, after=None) -> None:
        proj = str(j.project_path)
        stub, md = self.worker.stub, self.worker.md

        def fn():
            tl = stub.ListTracks(pb.ListTracksRequest(project_path=proj, at_frame=-1), metadata=md, timeout=120)
            dl = stub.ApplyRules(pb.ApplyRulesRequest(project_path=proj, keep_stored=True), metadata=md, timeout=120)
            return tl, dl

        def done(res):
            tl, dl = res
            j.tracks = {t.id: t for t in tl.tracks}
            j.suggestions = list(tl.suggestions)
            j.load_decisions(dl)
            if self.job is j:
                self._rebuild_timeline()
                self._refresh_protect_counts()
                self.top.set_step(self.step, self.max_step())
            self._fetch_thumb(j)
            if after:
                after()

        self.worker.call(fn, done, lambda m: self._failed(j, m))

    def _fetch_thumb(self, j: JobItem) -> None:
        def done(r):
            if j in self.jobs:
                self.queue.rows[self.jobs.index(j)].set_thumb(r.jpeg)

        self.worker.rpc("GetFrame", pb.FrameRequest(project_path=str(j.project_path), frame=0, max_width=96), done)

    def _apply_rules(self, j: JobItem, after=None) -> None:
        req = pb.ApplyRulesRequest(project_path=str(j.project_path), rules=j.rules_pb(), actor=self.actor())

        def done(dl):
            j.load_decisions(dl)
            if j.s in (S.ANALYZED, S.AUDITED, S.CANCELLED, S.FAILED):
                j.state.go(S.RULES_APPLIED)
            self._rebuild_timeline()
            self._refresh_protect_counts()
            self.refresh_frame()
            if self.step == 4:
                self._refresh_review()
            if after:
                after()

        self.worker.rpc("ApplyRules", req, done, lambda m: self._failed(j, m))

    def toggle_track(self, tid: int, on: bool | None = None) -> None:
        j = self.job
        if j is None or self.step not in (3, 4) or tid not in j.tracks:
            return
        if self.org_mode and j.s in (S.PENDING_APPROVAL, S.APPROVED, S.DELIVERED):
            self.say(tr("toast.locked"))
            return
        protect = (j.status_of(tid) != "protect") if on is None else on
        j.set_click(tid, protect)
        t = j.tracks[tid]
        if self.approval and j.case_id:
            self.approval.log_event(j.case_id, self.actor(), A.PROTECT_SET if protect else A.PROTECT_UNSET,
                                    tr("audit.click", tag=j.tag(t)))
        self._apply_rules(j)
        self.say(tr("toast.protected" if protect else "toast.unprotected", tag=j.tag(t)))

    def _add_plate(self, text: str) -> None:
        j = self.job
        plates = [p.strip() for p in text.split(",") if p.strip()]
        if j is None or not plates:
            return
        j.rules.append({"kind": "plate_text", "payload": {"plates": plates, "max_edit": 1}})
        self._apply_rules(j)
        self.say(tr("toast.plate_added", n=len(plates)))

    def _refresh_protect_counts(self) -> None:
        j = self.job
        if j:
            self.protect.set_counts(*j.counters())

    # ------------------------------------------------------------------ 프레임·캔버스
    def seek(self, f: int) -> None:
        j = self.job
        if j is None or not j.media:
            return
        self.frame = max(0, min(int(f), j.frames() - 1))
        self.stage.set_position(self.frame, j.frames())
        self.timeline.set_playhead(self.frame)
        if j.tracks and self.step != 2:
            self.refresh_frame()

    def refresh_frame(self) -> None:
        j = self.job
        if j is None or not j.tracks or self.step == 2:
            return
        prof = None
        if self.view_mode != "orig":
            prof = self._profile_pb(self.export.profile())
        self.fetcher.request(j, self.frame, self.view_mode, prof, self.step in (3, 4))
        self.stage.set_position(self.frame, j.frames())
        self.timeline.set_playhead(self.frame)

    def on_frame(self, j: JobItem, frame: int, mode: str, imgs: list[bytes], tl) -> None:
        if j is not self.job or self.step == 2:
            return
        if mode == "split":
            a, b = QImage.fromData(imgs[0]), QImage.fromData(imgs[1])
            out = QImage(a.size(), QImage.Format_RGB32)
            p = QPainter(out)
            p.drawImage(0, 0, a)
            half = a.width() // 2
            p.drawImage(half, 0, b, half, 0, a.width() - half, a.height())
            p.setPen(Qt.white)
            p.drawLine(half, 0, half, a.height())
            p.end()
            pm = QPixmap.fromImage(out)
        else:
            pm = QPixmap()
            pm.loadFromData(imgs[0])
        self.stage.canvas.set_frame(pm)
        if tl is not None and self.step in (3, 4) and mode == "orig":
            items = []
            rows = []
            for t in tl.tracks:
                if not t.boxes or t.id not in j.tracks:
                    continue
                b = t.boxes[0]
                st = j.status_of(t.id)
                items.append((t.id, j.tag(t), st, QRectF(b.x, b.y, b.w, b.h)))
                rows.append(self._track_row(j, j.tracks[t.id], st))
            self.stage.canvas.set_boxes(items)
            self.protect.set_rows(rows)
        else:
            self.stage.canvas.set_boxes([])

    def _track_row(self, j: JobItem, t: pb.Track, st: str) -> TrackRowW:
        fps = j.fps()
        span = f"{timecode(t.start_f, fps, False)[3:]}–{timecode(t.end_f, fps, False)[3:]}"
        d = j.decisions.get(t.id)
        if st == "protect":
            why = tr("why.protect", c=d.confidence if d else 1.0)
        elif st == "review":
            why = tr("why.review")
        else:
            why = tr("why.mask")
        extra = f" <span style='color:#E8C547'>⚠ {tr('rev.gap')}</span>" if any(s.from_id == t.id for s in j.suggestions) else ""
        r = TrackRowW(t.id, f"{j.tag(t)} · {tr(CLS_LABEL[t.cls])}{extra}", f"{span} · {why}", t.thumb_jpeg,
                      toggle_state=st)
        r.setProperty("sel", t.id == self.selected_tid)
        return r

    def _select_track(self, tid: int) -> None:
        j = self.job
        if j is None or tid not in j.tracks:
            return
        self.selected_tid = tid
        self.timeline.select(tid)
        t = j.tracks[tid]
        if not (t.start_f <= self.frame <= t.end_f):
            self.seek(t.start_f)

    def _play(self, on: bool) -> None:
        """재생 시계: 배속에 맞춰 목표 프레임을 계산하고, 프레임 가져오기가 밀리면 건너뛴다."""
        import time

        if on and self.job:
            if self.frame >= self.job.frames() - 1:
                self.seek(0)
            if not self.play_timer.isActive():
                self.play_start = self.frame
            self.play_t0 = time.monotonic()
            self.play_f0 = self.frame
            self.play_timer.start(max(10, int(1000 / max(self.job.fps() * self.stage.speed_value(), 1))))
        else:
            self.play_timer.stop()

    def _play_tick(self) -> None:
        import time

        j = self.job
        if j is None:
            return
        target = self.play_f0 + int((time.monotonic() - self.play_t0) * j.fps() * self.stage.speed_value())
        if target >= j.frames() - 1:
            self.seek(j.frames() - 1)
            self.stage.set_playing(False)
            self.play_timer.stop()
            return
        if not self.fetcher.busy and target != self.frame:
            self.seek(target)

    def _stop(self) -> None:
        """정지: 재생을 멈추고 재생을 시작했던 프레임으로 돌아간다."""
        self.play_timer.stop()
        self.seek(getattr(self, "play_start", 0))

    def _rebuild_timeline(self) -> None:
        j = self.job
        if j is None:
            self.timeline.set_data([], 0, 30)
            return
        merged: dict[int, list[pb.Track]] = {}
        for t in j.tracks.values():
            if t.merged_into:
                merged.setdefault(t.merged_into, []).append(t)
        gaps: dict[int, list[tuple[int, int]]] = {}
        for s in j.suggestions:
            a, b = j.tracks.get(s.to_id), j.tracks.get(s.from_id)
            if a and b and not b.merged_into:
                gaps.setdefault(a.id, []).append((a.end_f, b.start_f))
        order = {"protect": 0, "review": 1, "mask": 2}
        cls_order = {"face": 0, "plate": 1, "person": 2}
        lanes = []
        for t in sorted((t for t in j.tracks.values() if not t.merged_into),
                        key=lambda t: (order[j.status_of(t.id)], cls_order.get(t.cls, 3), t.start_f)):
            segs = [(t.start_f, t.end_f)]
            g = list(gaps.get(t.id, []))
            prev_end = t.end_f
            for c in sorted(merged.get(t.id, []), key=lambda x: x.start_f):
                segs.append((c.start_f, c.end_f))
                g.append((prev_end, c.start_f))
                prev_end = c.end_f
            st = j.status_of(t.id)
            lanes.append(Lane(t.id, f"{j.tag(t)} {tr('lane.' + st)}", st, segs, g))
        self.timeline.set_data(lanes, j.frames(), j.fps())
        self.timeline.set_playhead(self.frame)

    # ------------------------------------------------------------------ 4 검수
    def _refresh_review(self) -> None:
        j = self.job
        if j is None:
            return
        items = []
        if j.exposures:
            b = button(tr("rev.mask_all"), "", lambda: self._mask_exposure(-1))
            items.append((f"<span style='color:#E5534B'>{tr('rev.exposures', n=len(j.exposures))}</span>",
                          tr("rev.exposures_desc"), b"", b))
            for i, e in enumerate(j.exposures[:30]):
                bb = button(tr("rev.mask"), "", lambda _=False, k=i: self._mask_exposure(k))
                kind = tr("rev.exp_missing") if e["cls"] == "mask_missing" else tr("rev.exp_face", c=e.get("conf", 0))
                items.append((f"{tr('rev.exposure')} · f{e['frame']}", kind, b"", bb))
        for s in j.suggestions:
            a, b = j.tracks.get(s.to_id), j.tracks.get(s.from_id)
            if not a or not b or b.merged_into:
                continue
            btn = button(tr("rev.merge"), "", lambda _=False, f=s.from_id, t=s.to_id: self._merge(f, t))
            hint = tr("rev.merge_protect") if j.status_of(a.id) == "protect" else ""
            items.append((f"{j.tag(b)} · {tr('rev.gap')}", tr("rev.same_as", tag=j.tag(a), sim=s.similarity) + hint,
                          b.thumb_jpeg, btn))
        sugg_from = {s.from_id for s in j.suggestions}
        for tid, d in j.decisions.items():
            t = j.tracks.get(tid)
            if t and d.flag == "REVIEW" and tid not in sugg_from:
                btn = button(tr("rev.view"), "", lambda _=False, f=t.start_f, k=tid: (self.seek(f), self._select_track(k)))
                items.append((f"{j.tag(t)} · {tr('rev.flag')}", tr("rev.flag_desc", c=d.confidence), t.thumb_jpeg, btn))
        linked = {t.linked_person_id for t in j.tracks.values() if t.cls == "face" and t.linked_person_id}
        for t in j.tracks.values():
            if t.cls == "person" and t.id not in linked and not t.merged_into and j.status_of(t.id) != "protect":
                btn = button(tr("rev.view"), "", lambda _=False, f=t.start_f, k=t.id: (self.seek(f), self._select_track(k)))
                fps = j.fps()
                items.append((f"{tr('rev.missing')} · {timecode(t.start_f, fps, False)[3:]}–{timecode(t.end_f, fps, False)[3:]}",
                              tr("rev.missing_desc", tag=j.tag(t)), t.thumb_jpeg, btn))
        self.review.set_items(items)

    def _merge(self, from_id: int, to_id: int) -> None:
        j = self.job
        if j is None:
            return

        def done(ack):
            if not ack.ok:
                self.say(ack.message)
                return
            if self.approval and j.case_id:
                self.approval.log_event(j.case_id, self.actor(), A.MERGED,
                                        f"{j.tag(j.tracks[from_id])} → {j.tag(j.tracks[to_id])}")
            self.say(tr("toast.merged", a=j.tag(j.tracks[from_id]), b=j.tag(j.tracks[to_id])))
            self._load_tracks(j, after=lambda: (self._refresh_review(), self.refresh_frame()))

        self.worker.rpc("MergeTracks", pb.MergeRequest(project_path=str(j.project_path), from_id=from_id, to_id=to_id,
                                                       actor=self.actor()), done, lambda m: self._failed(j, m))

    def _mask_exposure(self, idx: int) -> None:
        j = self.job
        if j is None:
            return
        exps = j.exposures if idx < 0 else [j.exposures[idx]]
        span = 4
        for e in exps:
            if e["cls"] == "frame_count":
                continue
            cx, cy = e["x"] + e["w"] / 2, e["y"] + e["h"] / 2
            w, h = e["w"] * 1.2, e["h"] * 1.2
            box = [cx - w / 2, cy - h / 2, w, h]
            j.rules.append({"kind": "manual_box", "payload": {
                "frames": [[max(0, e["frame"] - span), *box], [e["frame"] + span, *box]], "cls": "face", "source": "audit"}})
        j.exposures = [] if idx < 0 else [x for k, x in enumerate(j.exposures) if k != idx]
        if self.approval and j.case_id:
            self.approval.log_event(j.case_id, self.actor(), A.MANUAL_BOX, tr("log.exposure_masked", n=len(exps)))
        self._apply_rules(j)
        self.say(tr("toast.exposure_masked", n=len(exps)))

    def _on_rect(self, r: QRectF) -> None:
        if self.stage.mask_b.isChecked():
            self._mask_target(r)
        else:
            self._manual_rect(r)

    def _mask_tool_toggled(self, on: bool) -> None:
        if on:
            self.review.manual_b.setChecked(False)
            self.manual_kf = None
            if self.view_mode != "orig" and self.step == 4:
                self.review.seg.set_value("orig")
                self._preview_mode("orig")
            self.say(tr("toast.mask_tool_on"))

    def _mask_target(self, r: QRectF) -> None:
        """놓친 객체 지정: 종류를 고르면 워커가 앞뒤로 자동 추적해 전 구간 마스킹 규칙을 만든다."""
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu

        j = self.job
        if j is None:
            return
        menu = QMenu(self)
        acts = {}
        for key in ("face", "plate", "other"):
            acts[menu.addAction(tr(f"tool.track_{key}"))] = (key, True)
        menu.addSeparator()
        for key in ("face", "plate"):
            acts[menu.addAction(tr(f"tool.once_{key}"))] = (key, False)
        chosen = menu.exec(QCursor.pos())
        if chosen is None or chosen not in acts:
            return
        self.apply_mask_target(r, *acts[chosen])

    def apply_mask_target(self, r: QRectF, cls: str, track: bool) -> None:
        j = self.job
        if j is None:
            return
        f = self.frame
        box = [r.x(), r.y(), r.width(), r.height()]
        if not track:
            span = 5
            self._add_mask_rule(j, cls, [[max(0, f - span), *box], [f + span, *box]], f, f)
            return
        self.say(tr("toast.tracking"))
        req = pb.TrackObjectRequest(project_path=str(j.project_path), frame=f,
                                    box=pb.Box(frame=f, x=box[0], y=box[1], w=box[2], h=box[3]),
                                    cls=cls, max_frames=int(j.fps() * 20), both_directions=True)

        def done(res):
            frames = [[b.frame, b.x, b.y, b.w, b.h] for b in res.boxes]
            self._add_mask_rule(j, cls, frames, res.start_f, res.end_f)

        self.worker.rpc("TrackObject", req, done, lambda m: self.say(tr("toast.error", msg=m[:120])), timeout=600)

    def _add_mask_rule(self, j: JobItem, cls: str, frames: list, a: int, b: int) -> None:
        j.rules.append({"kind": "manual_box", "payload": {"frames": frames, "cls": cls, "source": "assist"}})
        if self.approval and j.case_id:
            self.approval.log_event(j.case_id, self.actor(), A.MANUAL_BOX,
                                    tr("audit.mask_target", cls=tr(f"cls.{cls}") if cls != "other" else tr("cls.other"),
                                       a=a, b=b))
        self._apply_rules(j)
        self.say(tr("toast.mask_target_added", a=a, b=b, n=len(frames)))

    def _manual_mode(self) -> None:
        on = self.review.manual_b.isChecked()
        if on:
            self.stage.set_mask_tool(False)
        self.stage.canvas.draw_mode = on
        self.manual_kf = None
        self.review.manual_note.setText(tr("rev.manual_draw1") if on else tr("rev.manual_note"))
        if on and self.view_mode != "orig":
            self.review.seg.set_value("orig")
            self._preview_mode("orig")

    def _manual_rect(self, r: QRectF) -> None:
        j = self.job
        if j is None:
            return
        if self.manual_kf is None:
            self.manual_kf = (self.frame, r)
            self.review.manual_note.setText(tr("rev.manual_draw2", f=self.frame))
            return
        f1, r1 = self.manual_kf
        f2, r2 = self.frame, r
        if f2 < f1:
            f1, r1, f2, r2 = f2, r2, f1, r1
        frames = [[f1, r1.x(), r1.y(), r1.width(), r1.height()]]
        frames.append([f2 if f2 != f1 else f1 + 1, r2.x(), r2.y(), r2.width(), r2.height()])
        j.rules.append({"kind": "manual_box", "payload": {"frames": frames, "cls": "face"}})
        if self.approval and j.case_id:
            self.approval.log_event(j.case_id, self.actor(), A.MANUAL_BOX,
                                    f"{timecode(f1, j.fps(), False)}–{timecode(f2, j.fps(), False)}")
        self.manual_kf = None
        self.review.manual_b.setChecked(False)
        self.stage.canvas.draw_mode = False
        self.review.manual_note.setText(tr("rev.manual_note"))
        self._apply_rules(j)
        self.say(tr("toast.manual_added", a=f1, b=f2))

    def _body_mask(self) -> None:
        j = self.job
        if j is None:
            return
        t = j.tracks.get(self.selected_tid)
        if t is None or t.cls != "person":
            self.say(tr("toast.select_person"))
            return
        j.rules.append({"kind": "manual_box", "payload": {"track_id": t.id, "cls": "person"}})
        if self.approval and j.case_id:
            self.approval.log_event(j.case_id, self.actor(), A.MANUAL_BOX, tr("audit.body_mask", tag=j.tag(t)))
        self._apply_rules(j)
        self.say(tr("toast.body_masked", tag=j.tag(t)))

    def _preview_mode(self, mode: str) -> None:
        self.view_mode = mode
        self.stage.mode.setText({"orig": tr("view.original"), "mask": tr("view.masked"), "split": tr("view.split")}[mode])
        self.refresh_frame()

    # ------------------------------------------------------------------ 5 내보내기
    @staticmethod
    def _profile_pb(p: dict) -> pb.RenderProfile:
        return pb.RenderProfile(style=p["style"], strength=p["strength"], pad_ratio=p["pad_ratio"],
                                pad_frames=p["pad_frames"], codec=p["codec"], quality=p["quality"],
                                strip_meta=p["strip_meta"], keep_audio=p["keep_audio"],
                                no_head_fallback=p["no_head_fallback"], watermark=p.get("watermark", ""))

    def start_render(self, prof: dict, out: str) -> None:
        j = self.job
        if j is None or not j.tracks:
            return
        if j.s in (S.ANALYZING, S.RENDERING, S.PAUSED):
            self.say(tr("toast.busy"))
            return
        outp = Path(out) if out else j.output_path
        if outp.suffix.lower() not in (".mp4", ".mkv", ".mov"):
            outp = outp.with_suffix(".mp4")
        if outp.resolve() == j.path.resolve():
            self.say(tr("toast.same_output"))
            return
        if outp.exists() and QMessageBox.question(self, tr("app.name"), tr("dlg.overwrite", name=outp.name)) != QMessageBox.Yes:
            return
        j.output_path = outp
        self.worker.allow(outp)
        if prof.get("watermark_on") and self.approval and j.case_id:
            c = self.approval.case(j.case_id)
            prof["watermark"] = tr("wm.text", receipt=c["receipt_no"] or "-", date=datetime.now().strftime("%Y-%m-%d"))
        j.audit_json = bool(prof.get("audit_json", False))
        j.exposures = []
        if j.s == S.PAUSED:
            return
        j.state.go(S.RENDERING)
        j.job_id = "r-" + datetime.now().strftime("%H%M%S%f")
        self.monitor.set_mode("render")
        self.monitor.append_log("INFO", tr("log.render_start", name=outp.name))
        req = pb.RenderRequest(job_id=j.job_id, project_path=str(j.project_path), output_path=str(outp),
                               profile=self._profile_pb(prof), run_audit=True)
        t = self.worker.stream("Render", req, self)
        t.event.connect(lambda e, jj=j: self._on_event(jj, e, "render"))
        t.failed.connect(lambda m, jj=j: self._stream_failed(jj, m))
        self.streams[id(j)] = t
        t.start()
        self._refresh_queue(j)
        self.go(2)
        self.say(tr("toast.render_start"))

    def _render_done(self, j: JobItem, e: pb.Event) -> None:
        if j.s == S.PAUSED:
            j.state.resume()
        j.last_output_sha = e.output_sha256
        j.last_exposures = e.audit_exposures
        self.monitor.mark("", done_all=True)
        self.monitor.pause_b.setEnabled(False)
        self.monitor.cancel_b.setEnabled(False)
        if self.approval and j.case_id:
            self.approval.record_render(j.case_id, self.actor(), e.output_path, e.output_sha256, e.audit_exposures)
        if j.audit_json:
            self._write_audit_json(j, e)
        if e.audit_exposures == 0:
            j.state.go(S.AUDITED)
            self.monitor.stage.setText(tr("mon.audit_pass"))
            self._refresh_queue(j)
            if self.org_mode:
                self.say(tr("toast.render_done_org"))
                QTimer.singleShot(900, lambda: self.go(6))
            else:
                self.say(tr("toast.render_done"))
        else:
            j.state.go(S.REVIEWING)
            self.monitor.stage.setText(tr("mon.audit_fail", n=e.audit_exposures))
            self._refresh_queue(j)
            self.say(tr("toast.audit_fail", n=e.audit_exposures))
            QTimer.singleShot(900, lambda: self.go(4))

    def _write_audit_json(self, j: JobItem, e: pb.Event) -> None:
        data = {"output": e.output_path, "sha256": e.output_sha256, "audit_exposures": e.audit_exposures,
                "source": str(j.path), "source_sha256": j.media.sha256 if j.media else "",
                "rules": j.rules, "protected_tracks": [t for t, d in j.decisions.items() if d.protected],
                "created_at": datetime.now().isoformat(timespec="seconds")}
        if self.approval and j.case_id:
            from worker.orgmode.auditlog import AuditLog

            data["audit_log"] = AuditLog(self.org).entries(j.case_id)
        Path(e.output_path).with_suffix(".audit.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                                                  encoding="utf-8")

    # ------------------------------------------------------------------ 6 기관 모드
    def _purge(self) -> None:
        from worker.orgmode.retention import purge_expired

        n = purge_expired(self.org)
        if n:
            self.monitor.append_log("INFO", tr("log.purged", n=len(n)))

    def _fill_line_names(self) -> None:
        if not self.approval:
            return
        n = self.orgp.preset.currentData()
        line = self.approval.default_line()
        names = [s["user"] for s in line]
        self.orgp.names.setText(", ".join((names + [""] * n)[:n]).strip(", "))

    def _save_line(self, n: int, names: str) -> None:
        from worker.orgmode.approval import Approval, ApprovalError

        users = [x.strip() for x in names.split(",")]
        try:
            self.approval.save_line(Approval.preset(n, users), actor=self.actor())
        except PermissionError:
            self.say(tr("toast.admin_only"))
            return
        except ApprovalError as e:
            self.say(str(e))
            return
        self.say(tr("toast.line_saved", n=n))
        self._refresh_org()

    def _case_info(self, receipt: str, basis: str, requester: str) -> None:
        j = self.job
        if j is None or not self.approval:
            return
        if not receipt.strip():
            self.say(tr("toast.need_receipt"))
            return
        if not j.case_id:
            j.case_id = self.approval.create_case(receipt, basis, requester, str(j.project_path), self.actor())
            if j.last_output_sha:
                self.approval.record_render(j.case_id, self.actor(), str(j.output_path), j.last_output_sha, j.last_exposures)
        else:
            self.approval.update_case(j.case_id, self.actor(), receipt, basis, requester)
        self._refresh_org()

    def _refresh_org(self) -> None:
        j = self.job
        if j is None or not self.approval:
            return
        if not j.case_id:
            c = self.approval.case_by_project(str(j.project_path))
            j.case_id = c["id"] if c else ""
        case = self.approval.case(j.case_id) if j.case_id else None
        if case and not case["receipt_no"]:
            self.orgp.set_case(None)
        else:
            steps = self.approval.steps(j.case_id) if case else []
            people = " · ".join(s.user for s in steps if s.user)
            self.orgp.set_case(case, people)
        if case is None:
            self.orgp.set_steps([])
            self.orgp.approve_b.setEnabled(False)
            self.orgp.reject_b.setEnabled(False)
            self.orgp.out_b.setEnabled(False)
            self.orgp.pdf_b.setEnabled(False)
            self.orgp.audit.setPlainText("")
            self.stage.show_report(None)
            return
        steps = self.approval.steps(j.case_id)
        cur = self.approval.current_step(j.case_id)
        st = case["status"]
        prot, _, _ = j.counters()
        rows = [("done" if prot else "run", tr("org.step_requester"))]
        for s in steps:
            done = s.decision == "APPROVED"
            run = (cur is not None and s.step_no == cur.step_no)
            rows.append(("done" if done else "run" if run else "todo",
                         f"{s.role} · {s.user or tr('org.unassigned')}{' ✓' if done else ''}"))
        delivered = st in ("DELIVERED", "RETAINED", "PURGED")
        rows.append(("done" if delivered else "run" if st == "APPROVED" else "todo", tr("org.step_deliver")))
        self.orgp.set_steps(rows)
        if st == "REVIEWING" and cur:
            self.orgp.approve_b.setText(tr("org.submit", role=cur.role, user=cur.user or "-"))
            ok = case["audit_exposures"] == 0
            self.orgp.approve_b.setEnabled(ok)
            self.orgp.approve_b.setToolTip("" if ok else tr("org.need_audit"))
        elif st == "PENDING_APPROVAL" and cur:
            self.orgp.approve_b.setText(tr("org.approve_as", role=cur.role, user=cur.user or "-"))
            self.orgp.approve_b.setEnabled(True)
        else:
            self.orgp.approve_b.setText(tr("org.approved_all"))
            self.orgp.approve_b.setEnabled(False)
        self.orgp.reject_b.setEnabled(st in ("PENDING_APPROVAL", "APPROVED"))
        self.orgp.out_b.setEnabled(st == "APPROVED")
        self.orgp.pdf_b.setEnabled(bool(case["output_sha256"]))
        self.orgp.c_wm.setChecked(self.export.watermark.isChecked())
        self.orgp.audit.setPlainText(self._audit_text(j.case_id))
        users = self.approval.users.list()
        self.orgp.users_l.setText(tr("org.users_list", names=", ".join(u.name for u in users if u.active) or "-"))
        sb = self.orgp.audit.verticalScrollBar()
        sb.setValue(sb.maximum())
        self.stage.show_report(self._report_html(case, steps))

    def _audit_text(self, cid: str) -> str:
        from worker.orgmode.auditlog import AuditLog

        out = []
        for e in AuditLog(self.org).entries(cid):
            detail = e["detail"]
            try:
                d = json.loads(detail)
                detail = " · ".join(f"{v}" for v in d.values() if v not in ("", None, [])) if isinstance(d, dict) else detail
            except (ValueError, TypeError):
                pass
            out.append(f"[{e['ts'][5:10]} {e['ts'][11:19]}] {e['actor']:<8} {e['action']} · {e['target']} {detail}".rstrip())
        return "\n".join(out)

    def _report_html(self, case: dict, steps) -> str:
        j = self.job
        ex = case["audit_exposures"]
        badge = tr("report.pass") if ex == 0 else tr("report.fail", n=ex if ex is not None else "-")
        prot = [j.tag(j.tracks[t]) for t, d in j.decisions.items() if d.protected and t in j.tracks] if j else []
        cells = "".join(
            f"<td style='border:1px solid #d9dee5;padding:8px'><span style='color:#6f7785;font-size:10px'>{s.role}</span><br>"
            f"<b>{(s.user or '-') + ' · ' + (s.decided_at or '')[5:16].replace('T', ' ') if s.decision == 'APPROVED' else tr('org.waiting')}</b></td>"
            for s in steps)
        delivered = case["status"] in ("DELIVERED", "RETAINED", "PURGED")
        cells += (f"<td style='border:1px solid #d9dee5;padding:8px'><span style='color:#6f7785;font-size:10px'>"
                  f"{tr('org.step_deliver_short')}</span><br><b>{tr('report.delivered') if delivered else '—'}</b></td>")
        name = j.name if j else ""
        sha = case["output_sha256"] or ""
        return (f"<table width='100%'><tr><td><b style='font-size:16px'>{tr('report.title')}</b><br>"
                f"<span style='color:#6f7785;font-size:10.5px'>{case['receipt_no'] or '-'} · {name}</span></td>"
                f"<td align='right'><span style='color:#0E7A57;font-weight:600'>{badge}</span></td></tr></table>"
                f"<hr style='background:#0E7A57;height:2px'>"
                f"<table width='100%' cellspacing='6'><tr><td width='50%'><span style='color:#6f7785;font-size:10px'>{tr('report.basis')}</span><br>{case['legal_basis'] or '-'}</td>"
                f"<td><span style='color:#6f7785;font-size:10px'>{tr('report.protected')}</span><br>{case['requester_name'] or '-'} · {', '.join(prot) or '-'}</td></tr>"
                f"<tr><td><span style='color:#6f7785;font-size:10px'>{tr('report.audit')}</span><br>{tr('report.audit_desc', n=ex if ex is not None else '-')}</td>"
                f"<td><span style='color:#6f7785;font-size:10px'>{tr('report.output')}</span><br>{Path(case['output_path'] or '').name} · SHA-256 {sha[:4]}…{sha[-4:]}</td></tr></table>"
                f"<table width='100%' cellspacing='0' style='margin-top:10px'><tr>{cells}</tr></table>")

    def ask_pin(self, role: str, user: str) -> str | None:
        """현재 단계 결재자 본인 확인 (테스트에서 교체 가능)."""
        from PySide6.QtWidgets import QInputDialog, QLineEdit

        pin, ok = QInputDialog.getText(self, tr("org.pin_title"), tr("org.pin_prompt", role=role, user=user or "-"),
                                       QLineEdit.Password)
        return pin if ok else None

    def _register_user(self, name: str, pin: str) -> None:
        from worker.orgmode.users import AuthError

        try:
            self.approval.users.register(name, pin, actor=self.actor())
        except PermissionError:
            self.say(tr("toast.admin_only"))
            return
        except AuthError as e:
            self.say(str(e))
            return
        self.orgp.user_name.clear()
        self.orgp.user_pin.clear()
        self.say(tr("toast.user_registered", name=name))
        self._refresh_org()

    def _approve(self, comment: str) -> None:
        from worker.orgmode.approval import ApprovalError

        j = self.job
        if j is None or not j.case_id:
            return
        try:
            c = self.approval.case(j.case_id)
            step = self.approval.current_step(j.case_id)
            pin = ""
            if self.approval.users.required() and step is not None:
                got = self.ask_pin(step.role, step.user)
                if got is None:
                    return
                pin = got
            if c["status"] == "REVIEWING":
                self.approval.submit_review(j.case_id, self.actor(), comment, pin=pin)
                if j.s == S.AUDITED:
                    j.state.go(S.PENDING_APPROVAL)
                self.say(tr("toast.submitted"))
            else:
                cur = step
                nxt = self.approval.approve(j.case_id, self.actor(), comment, pin=pin)
                if nxt is None:
                    if j.s == S.PENDING_APPROVAL:
                        j.state.go(S.APPROVED)
                    self.say(tr("toast.final_approved"))
                else:
                    self.say(tr("toast.approved_next", role=cur.role, nrole=nxt.role, nuser=nxt.user or "-"))
        except ApprovalError as e:
            self.say(str(e))
        self.orgp.comment.clear()
        self._refresh_queue(j)
        self._refresh_org()

    def _reject(self, reason: str) -> None:
        from worker.orgmode.approval import ApprovalError

        j = self.job
        if j is None or not j.case_id:
            return
        cur = self.approval.current_step(j.case_id) or self.approval.steps(j.case_id)[-1]
        if not reason.strip():
            self.say(tr("toast.need_reason"))
            return
        pin = ""
        if self.approval.users.required():
            got = self.ask_pin(cur.role, cur.user)
            if got is None:
                return
            pin = got
        try:
            self.approval.reject(j.case_id, self.actor(), reason, pin=pin)
        except ApprovalError as e:
            self.say(str(e))
            return
        if j.s in (S.PENDING_APPROVAL, S.APPROVED, S.AUDITED):
            j.state.go(S.REVIEWING)
        self.orgp.comment.clear()
        self._refresh_queue(j)
        self.say(tr("toast.rejected"))
        QTimer.singleShot(900, lambda: self.go(4))

    def _make_pdf(self) -> Path | None:
        from worker.orgmode.report import generate

        j = self.job
        if j is None or not j.case_id:
            return None
        c = self.approval.case(j.case_id)
        name = (c["receipt_no"] or j.path.stem).replace("/", "-") + "_report.pdf"
        out = Path(c["output_path"] or j.output_path).with_name(name)
        try:
            generate(self.approval, j.case_id, out, operator=self.actor())
        except Exception as e:  # noqa: BLE001
            self.say(tr("toast.pdf_fail", msg=str(e)))
            return None
        self.say(tr("toast.pdf_saved", name=out.name))
        self._refresh_org()
        return out

    def _deliver(self, days: int) -> None:
        from worker.orgmode.approval import ApprovalError

        j = self.job
        if j is None or not j.case_id:
            return
        try:
            if self.orgp.c_pdf.isChecked() and not self.approval.case(j.case_id)["report_path"]:
                self._make_pdf()
            self.approval.deliver(j.case_id, self.actor(), days)
        except ApprovalError as e:
            self.say(str(e))
            return
        if j.s == S.APPROVED:
            j.state.go(S.DELIVERED)
        self._refresh_queue(j)
        self._refresh_org()
        self.say(tr("toast.delivered", days=days if days else tr("org.forever")))

    def _export_log(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from worker.orgmode.auditlog import AuditLog

        j = self.job
        f, _ = QFileDialog.getSaveFileName(self, tr("org.audit_export"), "audit_log.json", "JSON (*.json);;CSV (*.csv)")
        if not f:
            return
        log = AuditLog(self.org)
        cid = j.case_id if j else None
        (log.export_csv if f.lower().endswith(".csv") else log.export_json)(f, cid)
        self.say(tr("toast.log_exported"))

    # ------------------------------------------------------------------ 종료
    def closeEvent(self, e) -> None:
        running = [j for j in self.jobs if j.s in (S.ANALYZING, S.RENDERING, S.PAUSED)]
        if running and QMessageBox.question(self, tr("app.name"), tr("dlg.quit_running")) != QMessageBox.Yes:
            e.ignore()
            return
        for t in self.streams.values():
            t.abort()
        for t in self.streams.values():
            t.wait(3000)
        if self.org:
            self.org.close()
        super().closeEvent(e)
