"""QueuePanel: 드롭존(다중) · 파일 추가 · URL(기관 설치본 숨김) · 큐 목록."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.i18n import tr
from app.session import JobItem
from app.state.machine import S
from app.widgets.common import button, label, repolish

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".ts", ".m4v", ".mts", ".m2ts", ".webm"}


def fmt_dur(ms: int) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def res_label(w: int, h: int) -> str:
    m = min(w, h)
    return "4K" if m >= 2000 else f"{m}p"


class DropZone(QFrame):
    files = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 14, 12, 14)
        t = label(tr("queue.drop_title"))
        t.setStyleSheet("font-weight:600")
        t.setAlignment(Qt.AlignCenter)
        s = label(tr("queue.drop_formats"), "note")
        s.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        v.addWidget(s)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            self.setProperty("hover", True)
            repolish(self)
            e.acceptProposedAction()

    def dragLeaveEvent(self, e) -> None:
        self.setProperty("hover", False)
        repolish(self)

    def dropEvent(self, e) -> None:
        self.setProperty("hover", False)
        repolish(self)
        paths = []
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.is_dir():
                paths += [x for x in sorted(p.iterdir()) if x.suffix.lower() in VIDEO_EXT]
            elif p.suffix.lower() in VIDEO_EXT or p.suffix.lower() == ".nbproj":
                paths.append(p)
        if paths:
            self.files.emit(paths)


class JobRow(QWidget):
    def __init__(self, job: JobItem, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(10)
        self.thumb = QLabel()
        self.thumb.setFixedSize(44, 30)
        self.thumb.setStyleSheet("background:#2b3a44;border-radius:4px;")
        h.addWidget(self.thumb)
        v = QVBoxLayout()
        v.setSpacing(2)
        self.t = label(job.name)
        self.t.setStyleSheet("font-weight:600")
        self.m = label("", "trD")
        v.addWidget(self.t)
        v.addWidget(self.m)
        h.addLayout(v, 1)
        self.update_from(job)

    def update_from(self, job: JobItem) -> None:
        bits = []
        if job.media:
            bits += [res_label(job.media.width, job.media.height), fmt_dur(job.media.duration_ms)]
        s = job.s
        if job.error:
            badge, color = tr("badge.failed"), "#E5534B"
        elif s in (S.ANALYZING,):
            badge, color = tr("badge.analyzing", pct=int(job.progress)), "#1FA97B"
        elif s == S.RENDERING:
            badge, color = tr("badge.rendering", pct=int(job.progress)), "#1FA97B"
        elif s == S.PAUSED:
            badge, color = tr("badge.paused"), "#E8C547"
        elif s in (S.ANALYZED, S.RULES_APPLIED):
            badge, color = tr("badge.select"), "#2BB3A3"
        elif s == S.REVIEWING:
            n = len(job.exposures) or sum(1 for d in job.decisions.values() if d.flag == "REVIEW")
            badge, color = tr("badge.review", n=n), "#E8C547"
        elif s in (S.AUDITED, S.PENDING_APPROVAL, S.APPROVED):
            badge, color = tr(f"badge.{s.value.lower()}"), "#2BB3A3"
        elif s in (S.DELIVERED, S.RETAINED):
            badge, color = tr("badge.done"), "#2BB3A3"
        elif s == S.CANCELLED:
            badge, color = tr("badge.cancelled"), "#6F7785"
        else:
            badge, color = tr("badge.queued"), "#6F7785"
        self.m.setText("  ".join(bits) + f"  <span style='color:{color};font-weight:600'>{badge}</span>")
        self.m.setTextFormat(Qt.RichText)

    def set_thumb(self, jpeg: bytes) -> None:
        pm = QPixmap()
        if pm.loadFromData(jpeg):
            self.thumb.setPixmap(pm.scaled(44, 30, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))


class QueuePanel(QFrame):
    files_added = Signal(list)
    selected = Signal(int)

    def __init__(self, org_mode: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("queue")
        self.setMinimumWidth(220)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        ph = QWidget()
        hh = QHBoxLayout(ph)
        hh.setContentsMargins(14, 12, 14, 8)
        hh.addWidget(label(tr("queue.title"), "ph"))
        hh.addStretch(1)
        hh.addWidget(button(tr("queue.add"), "link", self.pick))
        v.addWidget(ph)
        dz_wrap = QWidget()
        dzl = QVBoxLayout(dz_wrap)
        dzl.setContentsMargins(12, 0, 12, 10)
        self.drop = DropZone()
        self.drop.files.connect(self.files_added.emit)
        dzl.addWidget(self.drop)
        v.addWidget(dz_wrap)
        if not org_mode:  # F-02: 기관 설치본에서는 URL 입력 숨김 (V2)
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 0, 12, 12)
            self.url = QLineEdit()
            self.url.setPlaceholderText(tr("queue.url_placeholder"))
            self.url.setEnabled(False)
            self.url.setToolTip(tr("queue.url_v2"))
            rl.addWidget(self.url, 1)
            b = button(tr("queue.url_fetch"), "pri")
            b.setEnabled(False)
            rl.addWidget(b)
            v.addWidget(row)
        self.list = QListWidget()
        self.list.setObjectName("jobs")
        self.list.currentRowChanged.connect(self.selected.emit)
        v.addWidget(self.list, 1)
        self.rows: list[JobRow] = []

    def pick(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, tr("queue.add_dialog"), "",
                                                tr("queue.filter") + " (*.mp4 *.mov *.mkv *.avi *.ts *.m4v *.nbproj)")
        if files:
            self.files_added.emit([Path(f) for f in files])

    def add(self, job: JobItem) -> None:
        row = JobRow(job)
        it = QListWidgetItem()
        it.setSizeHint(row.sizeHint())
        self.list.addItem(it)
        self.list.setItemWidget(it, row)
        self.rows.append(row)

    def refresh(self, idx: int, job: JobItem) -> None:
        if 0 <= idx < len(self.rows):
            self.rows[idx].update_from(job)

    def select(self, idx: int) -> None:
        self.list.setCurrentRow(idx)
