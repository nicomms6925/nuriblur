"""TimelinePanel: 트랙 레인 + 룰러 + 플레이헤드, 클릭/드래그 스크러빙 (G2-06).

색 = 판정(보호 청록 / 마스킹 주황 / 검수 노랑), 점선 = 끊긴 구간(병합 제안·병합된 조각 사이).
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

from app import theme
from app.i18n import tr

LANE_H = 26
HEAD_W = 150


@dataclass
class Lane:
    tid: int
    label: str
    status: str                       # protect | mask | review
    segments: list[tuple[int, int]]   # (start, end)
    gaps: list[tuple[int, int]]


class _Body(QWidget):
    seek = Signal(int)
    lane_clicked = Signal(int)

    def __init__(self, owner: TimelinePanel):
        super().__init__()
        self.o = owner
        self.setMouseTracking(True)

    def frame_at(self, x: float) -> int:
        w = max(self.width() - HEAD_W, 1)
        return int(max(0.0, min(1.0, (x - HEAD_W) / w)) * max(self.o.total - 1, 0))

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        pal = theme.palette()
        p.fillRect(self.rect(), QColor(pal.panel))
        w = self.width() - HEAD_W
        total = max(self.o.total, 1)
        f = QFont(theme.sans_family())
        f.setPixelSize(11)
        p.setFont(f)
        lines = QColor(pal.line)
        for i, ln in enumerate(self.o.lanes):
            y = i * LANE_H
            if ln.tid == self.o.selected:
                p.fillRect(0, y, self.width(), LANE_H, QColor(pal.panel2))
            c = QColor({"protect": theme.PROTECT, "mask": theme.MASK, "review": theme.REVIEW,
                        "ignore": theme.IGNORE}.get(ln.status, theme.MASK))
            p.fillRect(QRectF(10, y + 9, 8, 8), c)
            p.setPen(QColor(pal.ink2))
            p.drawText(QRectF(24, y, HEAD_W - 28, LANE_H), Qt.AlignVCenter | Qt.AlignLeft, ln.label)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            for a, b in ln.segments:
                x1 = HEAD_W + a / total * w
                x2 = HEAD_W + (b + 1) / total * w
                p.drawRoundedRect(QRectF(x1, y + 7, max(x2 - x1, 2), 12), 3, 3)
            pen = QPen(QColor(theme.REVIEW), 1.5, Qt.DashLine)
            for a, b in ln.gaps:
                x1 = HEAD_W + a / total * w
                x2 = HEAD_W + b / total * w
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(x1, y + 7, max(x2 - x1, 3), 12), 3, 3)
                p.setPen(Qt.NoPen)
                p.setBrush(c)
            p.setPen(lines)
            p.drawLine(0, y + LANE_H - 1, self.width(), y + LANE_H - 1)
        p.setPen(lines)
        p.drawLine(HEAD_W, 0, HEAD_W, self.height())
        x = HEAD_W + self.o.playhead / total * w
        p.setPen(QPen(QColor(255, 255, 255, 200), 1))
        p.drawLine(int(x), 0, int(x), self.height())

    def mousePressEvent(self, e) -> None:
        i = int(e.position().y() // LANE_H)
        if e.position().x() > HEAD_W:
            self.seek.emit(self.frame_at(e.position().x()))
        if 0 <= i < len(self.o.lanes):
            self.lane_clicked.emit(self.o.lanes[i].tid)

    def mouseMoveEvent(self, e) -> None:
        if e.buttons() & Qt.LeftButton and e.position().x() > HEAD_W:
            self.seek.emit(self.frame_at(e.position().x()))


class _Ruler(QWidget):
    seek = Signal(int)

    def __init__(self, owner: TimelinePanel):
        super().__init__()
        self.o = owner
        self.setFixedHeight(LANE_H)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        pal = theme.palette()
        p.fillRect(self.rect(), QColor(pal.panel))
        p.setPen(QColor(pal.ink3))
        f = QFont(theme.mono_family())
        f.setPixelSize(10)
        p.setFont(f)
        p.drawText(QRectF(10, 0, HEAD_W, LANE_H), Qt.AlignVCenter, tr("timeline.title"))
        w = self.width() - HEAD_W
        total = max(self.o.total, 1)
        dur = total / (self.o.fps or 30)
        step = next((s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800) if dur / s <= 8), 3600)
        t = 0.0
        while t <= dur:
            x = HEAD_W + t / dur * w if dur else HEAD_W
            p.drawText(QRectF(x - 30, 0, 60, LANE_H), Qt.AlignCenter, f"{int(t // 60)}:{int(t % 60):02d}")
            t += step
        p.setPen(QColor(pal.line))
        p.drawLine(0, LANE_H - 1, self.width(), LANE_H - 1)
        x = HEAD_W + self.o.playhead / total * w
        p.setPen(QPen(QColor(255, 255, 255, 220), 1))
        p.drawLine(int(x), 0, int(x), LANE_H)

    def mousePressEvent(self, e) -> None:
        if e.position().x() > HEAD_W:
            w = max(self.width() - HEAD_W, 1)
            self.seek.emit(int(max(0.0, min(1.0, (e.position().x() - HEAD_W) / w)) * max(self.o.total - 1, 0)))

    mouseMoveEvent = mousePressEvent


class TimelinePanel(QFrame):
    seek = Signal(int)
    track_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("timeline")
        self.setMinimumHeight(120)
        self.lanes: list[Lane] = []
        self.total = 0
        self.fps = 30.0
        self.playhead = 0
        self.selected = -1
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.ruler = _Ruler(self)
        self.body = _Body(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.body)
        v.addWidget(self.ruler)
        v.addWidget(self.scroll, 1)
        self.ruler.seek.connect(self.seek.emit)
        self.body.seek.connect(self.seek.emit)
        self.body.lane_clicked.connect(self.track_selected.emit)

    def set_data(self, lanes: list[Lane], total: int, fps: float) -> None:
        self.lanes, self.total, self.fps = lanes, total, fps
        self.body.setMinimumHeight(max(len(lanes), 1) * LANE_H)
        self.body.update()
        self.ruler.update()

    def set_playhead(self, f: int) -> None:
        self.playhead = f
        self.body.update()
        self.ruler.update()

    def select(self, tid: int) -> None:
        self.selected = tid
        for i, ln in enumerate(self.lanes):
            if ln.tid == tid:
                self.scroll.ensureVisible(0, i * LANE_H + LANE_H // 2, 0, LANE_H)
        self.body.update()
