"""SidePanel(320): 단계별 패널 1~6 (docs/06). 동작 로직은 MainWindow가 시그널로 받는다."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app import theme
from app.i18n import tr
from app.widgets.common import Section, Seg, Stat, TrackRowW, button, clear_layout, kv_row, label


class Panel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sideInner")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 16)
        self.v.setSpacing(0)

    def sec(self, title: str = "") -> Section:
        s = Section(title)
        self.v.addWidget(s)
        return s

    def finish(self) -> None:
        self.v.addStretch(1)


def combo() -> QComboBox:
    c = QComboBox()
    c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    c.setMinimumContentsLength(8)
    return c


def btn_row(*buttons: QPushButton) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 8, 0, 0)
    h.setSpacing(8)
    for b in buttons:
        if len(buttons) > 1:  # 목업처럼 같은 폭으로 나누고, 좁으면 줄어들게
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            b.setMinimumWidth(0)
            b.setToolTip(b.text())
        h.addWidget(b, 1)
    return w


# ---------------- 1 입력 ----------------
class InputPanel(Panel):
    start = Signal(list, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        s = self.sec(tr("input.selected"))
        self.rows = {}
        for k in ("file", "codec", "res", "len", "audio"):
            w, v = kv_row(tr(f"input.{k}"), "—")
            self.rows[k] = v
            s.add(w)
        s = self.sec(tr("input.targets"))
        self.chk = {}
        for k, on, en in (("face", True, True), ("person", True, True), ("plate", True, True), ("sign", False, False)):
            c = QCheckBox(tr(f"input.cls_{k}"))
            c.setChecked(on)
            c.setEnabled(en)
            self.chk[k] = c
            s.add(c)
        s = self.sec(tr("input.profile"))
        self.profile = Seg([("gpu_precise", tr("profile.gpu_precise")), ("gpu_fast", tr("profile.gpu_fast")),
                            ("cpu", tr("profile.cpu"))])
        self.profile.changed.connect(lambda _: self._update_note())
        s.add(self.profile)
        self.note = label("", "note", wrap=True)
        s.add(self.note)
        self.start_b = button(tr("input.start"), "pri", self._start)
        s.add(btn_row(self.start_b))
        self.finish()
        self.frames = 0
        self.gpu = False

    def set_runtime(self, gpu: bool) -> None:
        self.gpu = gpu
        self.profile.set_value("gpu_precise" if gpu else "cpu")
        self._update_note()

    def set_media(self, name: str, m) -> None:
        self.rows["file"].setText(name)
        if m is None:
            for k in ("codec", "res", "len", "audio"):
                self.rows[k].setText("—")
            return
        self.rows["codec"].setText(m.codec)
        self.rows["res"].setText(f"{m.width}×{m.height} · {m.fps:.2f} ({'VFR' if m.is_vfr else 'CFR'})"
                                 + (f" · {tr('input.rot')} {m.rotation}°" if m.rotation else ""))
        s = m.duration_ms // 1000
        self.rows["len"].setText(f"{s // 60:02d}:{s % 60:02d} · {m.frames:,}")
        self.rows["audio"].setText((m.audio_codec.upper() + " · " + tr("input.audio_keep")) if m.audio_codec else tr("input.no_audio"))
        self.frames = m.frames
        self._update_note()

    def _update_note(self) -> None:
        p = self.profile.value()
        fps = {"gpu_precise": 60, "gpu_fast": 90, "cpu": 6}[p] if self.gpu or p == "cpu" else 6
        eta = int(self.frames / max(fps, 1) / 60) + 1 if self.frames else 0
        model = tr(f"input.model_{p}")
        self.note.setText(tr("input.note", model=model, eta=eta))

    def _start(self) -> None:
        self.start.emit([k for k in ("face", "person", "plate") if self.chk[k].isChecked()], self.profile.value())


# ---------------- 2 분석/렌더 모니터 ----------------
class MonitorPanel(Panel):
    pause = Signal()
    cancel = Signal()

    ANALYZE_STAGES = ["ingest", "decode", "detect", "identify", "save"]
    RENDER_STAGES = ["ingest", "render", "audit"]

    def __init__(self, parent=None):
        super().__init__(parent)
        s = self.sec(tr("mon.progress"))
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self.stage = label(tr("mon.waiting"), "k")
        self.pct = label("0%", "v")
        h.addWidget(self.stage)
        h.addStretch(1)
        h.addWidget(self.pct)
        s.add(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        s.add(self.bar)
        w, self.eta = kv_row(tr("mon.eta"), "—")
        s.add(w)
        self.stage_box = QWidget()
        self.stage_lay = QVBoxLayout(self.stage_box)
        self.stage_lay.setContentsMargins(0, 6, 0, 0)
        self.stage_lay.setSpacing(4)
        s.add(self.stage_box)
        self.pause_b = button(tr("mon.pause"), "", self.pause.emit)
        self.cancel_b = button(tr("mon.cancel"), "danger", self.cancel.emit)
        s.add(btn_row(self.pause_b, self.cancel_b))
        s = self.sec(tr("mon.perf"))
        g = QWidget()
        gl = QGridLayout(g)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(8)
        self.g_fps, self.g_gpu, self.g_vram = Stat(tr("mon.fps")), Stat(tr("mon.gpu")), Stat(tr("mon.vram"))
        for i, w in enumerate((self.g_fps, self.g_gpu, self.g_vram)):
            gl.addWidget(w, 0, i)
        s.add(g)
        s = self.sec(tr("mon.detections"))
        c = QWidget()
        cl = QGridLayout(c)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        self.c_face, self.c_person, self.c_plate = (Stat(tr("mon.c_face"), "count"), Stat(tr("mon.c_person"), "count"),
                                                    Stat(tr("mon.c_plate"), "count"))
        for i, w in enumerate((self.c_face, self.c_person, self.c_plate)):
            cl.addWidget(w, 0, i)
        s.add(c)
        s = self.sec(tr("mon.log"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(150)
        self.log.setMaximumBlockCount(2000)
        s.add(self.log)
        self.finish()
        self.stages: list[str] = []
        self.dots: dict[str, QLabel] = {}
        self.set_mode("analyze")

    def set_mode(self, mode: str) -> None:
        clear_layout(self.stage_lay)
        self.dots = {}
        self.stages = self.ANALYZE_STAGES if mode == "analyze" else self.RENDER_STAGES
        for st in self.stages:
            lb = QLabel()
            lb.setTextFormat(Qt.RichText)
            self.stage_lay.addWidget(lb)
            self.dots[st] = lb
        self.mark("")
        self.bar.setValue(0)
        self.pct.setText("0%")
        self.eta.setText("—")
        self.stage.setText(tr("mon.waiting"))
        self.pause_b.setText(tr("mon.pause"))
        self.pause_b.setEnabled(True)
        self.cancel_b.setEnabled(True)

    def mark(self, cur: str, done_all: bool = False) -> None:
        pal = theme.palette()
        idx = self.stages.index(cur) if cur in self.stages else (len(self.stages) if done_all else -1)
        for i, st in enumerate(self.stages):
            if i < idx:
                c, dot, tc = theme.BRAND, "●", pal.ink2
            elif i == idx:
                c, dot, tc = theme.BRAND2, "◉", pal.ink
            else:
                c, dot, tc = pal.line2, "●", pal.ink3
            self.dots[st].setText(f"<span style='color:{c}'>{dot}</span> <span style='color:{tc}'>{tr('stage.' + st)}</span>")

    def append_log(self, level: str, msg: str) -> None:
        from datetime import datetime

        color = {"INFO": theme.BRAND2, "WARN": theme.REVIEW, "ERROR": theme.DANGER}.get(level, "#b8c0cc")
        cur = self.log.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#b8c0cc"))
        cur.insertText(f"[{datetime.now():%H:%M:%S}] ", fmt)
        fmt.setForeground(QColor(color))
        cur.insertText(f"{level:<5} ", fmt)
        fmt.setForeground(QColor("#b8c0cc"))
        cur.insertText(msg + "\n", fmt)
        self.log.setTextCursor(cur)
        self.log.ensureCursorVisible()


# ---------------- 3 보호대상 선택 ----------------
class ProtectPanel(Panel):
    toggle = Signal(int, bool)
    focus = Signal(int)
    add_plate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        s = self.sec(tr("prot.title"))
        s.add(label(tr("prot.note"), "note", wrap=True))
        c = QWidget()
        cl = QGridLayout(c)
        cl.setContentsMargins(0, 6, 0, 0)
        cl.setSpacing(8)
        self.n_prot = Stat(tr("prot.c_protect"), "count", "protect")
        self.n_mask = Stat(tr("prot.c_mask"), "count")
        self.n_rev = Stat(tr("prot.c_review"), "count", "review")
        for i, w in enumerate((self.n_prot, self.n_mask, self.n_rev)):
            cl.addWidget(w, 0, i)
        s.add(c)
        s = self.sec(tr("prot.this_frame"))
        self.list_w = QWidget()
        self.list_l = QVBoxLayout(self.list_w)
        self.list_l.setContentsMargins(0, 0, 0, 0)
        self.list_l.setSpacing(6)
        s.add(self.list_w)
        s = self.sec(tr("prot.ref_title"))
        s.add(label(tr("prot.ref_v1"), "note", wrap=True))
        s = self.sec(tr("prot.plate_title"))
        self.plate = QLineEdit()
        self.plate.setPlaceholderText(tr("prot.plate_placeholder"))
        s.add(self.plate)
        b1 = button(tr("prot.plate_add"), "", lambda: self.add_plate.emit(self.plate.text()))
        b2 = button(tr("prot.region_v1"))
        b2.setEnabled(False)
        b2.setToolTip(tr("prot.v1_tooltip"))
        s.add(btn_row(b1, b2))
        s.add(label(tr("prot.plate_note"), "note", wrap=True))
        self.finish()

    def set_counts(self, prot: int, mask: int, rev: int) -> None:
        self.n_prot.set(str(prot))
        self.n_mask.set(str(mask))
        self.n_rev.set(str(rev))

    def set_rows(self, rows: list[TrackRowW]) -> None:
        clear_layout(self.list_l)
        if not rows:
            self.list_l.addWidget(label(tr("prot.none_here"), "note"))
        for r in rows:
            r.toggled.connect(self.toggle.emit)
            r.clicked.connect(self.focus.emit)
            self.list_l.addWidget(r)


# ---------------- 4 검수 ----------------
class ReviewPanel(Panel):
    merge = Signal(int, int)
    goto = Signal(int, int)          # frame, track
    mask_exposure = Signal(int)      # 노출 인덱스 (-1 = 전부)
    manual_box = Signal()
    body_mask = Signal()
    preview_mode = Signal(str)
    done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.s_list = self.sec(tr("rev.title", n=0))
        self.list_w = QWidget()
        self.list_l = QVBoxLayout(self.list_w)
        self.list_l.setContentsMargins(0, 0, 0, 0)
        self.list_l.setSpacing(6)
        self.s_list.add(self.list_w)
        s = self.sec(tr("rev.manual"))
        self.manual_b = button(tr("rev.manual_box"), "", self.manual_box.emit)
        self.manual_b.setCheckable(True)
        self.body_b = button(tr("rev.body_mask"), "", self.body_mask.emit)
        s.add(btn_row(self.manual_b, self.body_b))
        self.manual_note = label(tr("rev.manual_note"), "note", wrap=True)
        s.add(self.manual_note)
        s = self.sec(tr("rev.preview"))
        self.seg = Seg([("orig", tr("view.original")), ("mask", tr("view.masked")), ("split", tr("view.split"))])
        self.seg.changed.connect(self.preview_mode.emit)
        s.add(self.seg)
        s = self.sec()
        s.add(btn_row(button(tr("rev.done"), "pri", self.done.emit)))
        self.finish()

    def set_items(self, items: list[tuple[str, str, bytes, QPushButton | None]]) -> None:
        clear_layout(self.list_l)
        self.s_list.title.setText(tr("rev.title", n=len(items)))
        if not items:
            self.list_l.addWidget(label(tr("rev.none"), "note"))
        for title, desc, thumb, btn in items:
            self.list_l.addWidget(TrackRowW(0, title, desc, thumb, trailing=btn))


# ---------------- 5 내보내기 ----------------
class ExportPanel(Panel):
    start = Signal(dict, str)
    style_changed = Signal(str)

    def __init__(self, org_mode: bool, parent=None):
        super().__init__(parent)
        s = self.sec(tr("exp.style"))
        self.style = Seg([("pixelate", tr("style.pixelate")), ("gaussian", tr("style.gaussian")),
                          ("solid", tr("style.solid")), ("segment", tr("style.segment"))])
        self.style.buttons["segment"].setEnabled(False)
        self.style.buttons["segment"].setToolTip(tr("style.segment_v2"))
        self.style.changed.connect(self._style)
        s.add(self.style)
        self.str_l = label("", "k")
        s.add(self.str_l)
        self.strength = QSlider(Qt.Horizontal)
        self.strength.setRange(4, 16)
        self.strength.setValue(8)
        self.strength.setInvertedAppearance(True)
        self.strength.valueChanged.connect(self._labels)
        s.add(self.strength)
        self.pad_l = label("", "k")
        s.add(self.pad_l)
        self.pad = QSlider(Qt.Horizontal)
        self.pad.setRange(100, 160)
        self.pad.setValue(125)
        self.pad.valueChanged.connect(self._labels)
        s.add(self.pad)
        s = self.sec(tr("exp.output"))
        s.add(label(tr("exp.codec"), "k"))
        self.codec = combo()
        for k in ("source", "h264", "hevc"):
            self.codec.addItem(tr(f"codec.{k}"), k)
        s.add(self.codec)
        s.add(label(tr("exp.quality"), "k"))
        self.quality = combo()
        for k in ("source", "high", "normal"):
            self.quality.addItem(tr(f"quality.{k}"), k)
        s.add(self.quality)
        self.audio = QCheckBox(tr("exp.audio"))
        self.audio.setChecked(True)
        self.meta = QCheckBox(tr("exp.strip_meta"))
        self.meta.setChecked(True)
        self.head = QCheckBox(tr("exp.head_fallback"))
        self.head.setChecked(True)
        self.audit_json = QCheckBox(tr("exp.audit_json"))
        self.watermark = QCheckBox(tr("exp.watermark"))
        self.watermark.setVisible(org_mode)
        for c in (self.audio, self.meta, self.head, self.audit_json, self.watermark):
            s.add(c)
        s.add(label(tr("exp.out_path"), "k"))
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        self.out = QLineEdit()
        rl.addWidget(self.out, 1)
        rl.addWidget(button("…", "", self._browse))
        s.add(row)
        self.start_b = button(tr("exp.start"), "pri", self._start)
        s.add(btn_row(self.start_b))
        self.note = label("", "note", wrap=True)
        s.add(self.note)
        self.finish()
        self._labels()

    def _style(self, k: str) -> None:
        self._labels()
        self.style_changed.emit(k)

    def _labels(self) -> None:
        st = self.style.value()
        n = self.strength.value()
        if st == "gaussian":
            self.str_l.setText(tr("exp.strength_blur", n=n))
        elif st == "solid":
            self.str_l.setText(tr("exp.strength_solid"))
        else:
            self.str_l.setText(tr("exp.strength_pixel", n=n))
        self.strength.setEnabled(st != "solid")
        self.pad_l.setText(tr("exp.pad", r=self.pad.value() / 100))

    def _browse(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, tr("exp.out_path"), self.out.text(), "MP4 (*.mp4);;MKV (*.mkv);;MOV (*.mov)")
        if f:
            self.out.setText(f)

    def profile(self) -> dict:
        return {"style": self.style.value(), "strength": float(self.strength.value()),
                "pad_ratio": self.pad.value() / 100, "pad_frames": 5, "codec": self.codec.currentData(),
                "quality": self.quality.currentData(), "strip_meta": self.meta.isChecked(),
                "keep_audio": self.audio.isChecked(), "no_head_fallback": not self.head.isChecked(),
                "watermark_on": self.watermark.isChecked(), "audit_json": self.audit_json.isChecked()}

    def set_summary(self, prot: int, masked: int) -> None:
        self.note.setText(tr("exp.note", prot=prot, masked=masked))

    def _start(self) -> None:
        self.start.emit(self.profile(), self.out.text().strip())


# ---------------- 6 결재·리포트 ----------------
class OrgPanel(Panel):
    create_case = Signal(str, str, str)
    save_line = Signal(int, str)
    approve = Signal(str)
    reject = Signal(str)
    make_pdf = Signal()
    deliver = Signal(int)
    export_log = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.s_case = self.sec(tr("org.case"))
        self.case_form = QWidget()
        fl = QVBoxLayout(self.case_form)
        fl.setContentsMargins(0, 0, 0, 0)
        self.receipt = QLineEdit()
        self.receipt.setPlaceholderText(tr("org.receipt_ph"))
        self.basis = combo()
        self.basis.setEditable(True)
        for k in ("basis.35", "basis.18", "basis.other"):
            self.basis.addItem(tr(k))
        self.requester = QLineEdit()
        self.requester.setPlaceholderText(tr("org.requester_ph"))
        for lbl, w in ((tr("org.receipt"), self.receipt), (tr("org.basis"), self.basis), (tr("org.requester"), self.requester)):
            fl.addWidget(label(lbl, "k"))
            fl.addWidget(w)
        fl.addWidget(btn_row(button(tr("org.create"), "pri", lambda: self.create_case.emit(
            self.receipt.text(), self.basis.currentText(), self.requester.text()))))
        self.s_case.add(self.case_form)
        self.case_info = QWidget()
        il = QVBoxLayout(self.case_info)
        il.setContentsMargins(0, 0, 0, 0)
        self.info = {}
        for k in ("receipt", "basis", "requester", "people", "status"):
            w, v = kv_row(tr(f"org.{k}"), "")
            self.info[k] = v
            il.addWidget(w)
        self.s_case.add(self.case_info)
        s = self.sec(tr("org.line"))
        s.add(label(tr("org.line_steps"), "k"))
        self.preset = combo()
        for n in (2, 3, 4):
            self.preset.addItem(tr(f"org.preset{n}"), n)
        self.preset.setCurrentIndex(1)
        s.add(self.preset)
        s.add(label(tr("org.line_users"), "k"))
        self.names = QLineEdit()
        self.names.setPlaceholderText(tr("org.line_users_ph"))
        s.add(self.names)
        s.add(label(tr("org.line_note"), "note", wrap=True))
        self.save_b = button(tr("org.line_save"), "", lambda: self.save_line.emit(self.preset.currentData(), self.names.text()))
        s.add(btn_row(self.save_b))
        s = self.sec(tr("org.steps"))
        self.steps_w = QWidget()
        self.steps_l = QVBoxLayout(self.steps_w)
        self.steps_l.setContentsMargins(0, 0, 0, 0)
        self.steps_l.setSpacing(4)
        s.add(self.steps_w)
        s.add(label(tr("org.comment"), "k"))
        self.comment = QLineEdit()
        self.comment.setPlaceholderText(tr("org.comment_ph"))
        s.add(self.comment)
        self.approve_b = button(tr("org.approve"), "pri", lambda: self.approve.emit(self.comment.text()))
        self.reject_b = button(tr("org.reject"), "danger", lambda: self.reject.emit(self.comment.text()))
        s.add(btn_row(self.approve_b, self.reject_b))
        s = self.sec(tr("org.deliver"))
        self.c_pdf = QCheckBox(tr("org.c_pdf"))
        self.c_pdf.setChecked(True)
        self.c_hash = QCheckBox(tr("org.c_hash"))
        self.c_hash.setChecked(True)
        self.c_hash.setEnabled(False)
        self.c_wm = QCheckBox(tr("org.c_wm"))
        self.c_wm.setEnabled(False)
        for c in (self.c_pdf, self.c_hash, self.c_wm):
            s.add(c)
        s.add(label(tr("org.retention"), "k"))
        self.retention = combo()
        for days, key in ((90, "org.ret90"), (180, "org.ret180"), (0, "org.ret_forever")):
            self.retention.addItem(tr(key), days)
        s.add(self.retention)
        self.pdf_b = button(tr("org.pdf"), "", self.make_pdf.emit)
        self.out_b = button(tr("org.provide"), "", lambda: self.deliver.emit(self.retention.currentData()))
        s.add(btn_row(self.pdf_b, self.out_b))
        s = self.sec(tr("org.audit"))
        self.audit = QPlainTextEdit()
        self.audit.setObjectName("log")
        self.audit.setReadOnly(True)
        self.audit.setFixedHeight(170)
        s.add(self.audit)
        s.add(btn_row(button(tr("org.audit_export"), "", self.export_log.emit)))
        self.finish()

    def set_case(self, case: dict | None, people: str = "") -> None:
        self.case_form.setVisible(case is None)
        self.case_info.setVisible(case is not None)
        if case:
            self.info["receipt"].setText(case["receipt_no"] or "—")
            self.info["basis"].setText(case["legal_basis"] or "—")
            self.info["requester"].setText(case["requester_name"] or "—")
            self.info["people"].setText(people)
            self.info["status"].setText(tr(f"case.{case['status']}"))

    def set_steps(self, rows: list[tuple[str, str]]) -> None:
        """rows: (상태 done|run|todo, 텍스트)"""
        clear_layout(self.steps_l)
        pal = theme.palette()
        for st, text in rows:
            c = {"done": theme.BRAND, "run": theme.BRAND2}.get(st, pal.line2)
            tc = {"done": pal.ink2, "run": pal.ink}.get(st, pal.ink3)
            lb = QLabel(f"<span style='color:{c}'>{'◉' if st == 'run' else '●'}</span> <span style='color:{tc}'>{text}</span>")
            lb.setTextFormat(Qt.RichText)
            self.steps_l.addWidget(lb)
