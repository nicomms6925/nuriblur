"""StageView: 프레임 캔버스(QGraphicsView) + 박스 오버레이 + HUD/범례 + 트랜스포트 (G2-05/06)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app import theme
from app.i18n import tr
from app.widgets.common import button, label

STATUS_COLOR = {"protect": theme.PROTECT, "mask": theme.MASK, "review": theme.REVIEW}


def timecode(frame: int, fps: float, with_frames: bool = True) -> str:
    fps = fps or 30.0
    s = frame / fps
    base = f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{int(s % 60):02d}"
    return base + (f".{int(frame % round(fps)):02d}" if with_frames else "")


class BoxItem(QGraphicsRectItem):
    def __init__(self, tid: int, tag: str, status: str, rect: QRectF, on_click, scale: float):
        super().__init__(rect)
        self.tid = tid
        self.on_click = on_click
        c = QColor(STATUS_COLOR[status])
        pen = QPen(c, 2.0 / scale)
        if status == "review":
            pen.setStyle(Qt.DashLine)
        self.setPen(pen)
        self.setBrush(Qt.NoBrush)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, False)
        text = tag + (" ✓" if status == "protect" else " ?" if status == "review" else "")
        f = QFont(theme.mono_family())
        f.setPixelSize(max(8, int(10 / scale)))
        f.setBold(True)
        t = QGraphicsSimpleTextItem(text, self)
        t.setFont(f)
        t.setBrush(QBrush(QColor("#0b0d11")))
        br = t.boundingRect()
        pad = 3 / scale
        bg = QGraphicsRectItem(QRectF(rect.x(), rect.y() - br.height() - pad, br.width() + 2 * pad, br.height() + pad), self)
        bg.setBrush(c)
        bg.setPen(Qt.NoPen)
        t.setParentItem(bg)
        t.setPos(rect.x() + pad, rect.y() - br.height() - pad / 2)
        self.setToolTip(text)

    def hoverEnterEvent(self, e) -> None:
        p = self.pen()
        p.setWidthF(p.widthF() * 1.6)
        self.setPen(p)

    def hoverLeaveEvent(self, e) -> None:
        p = self.pen()
        p.setWidthF(p.widthF() / 1.6)
        self.setPen(p)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.on_click(self.tid)
            e.accept()


class Canvas(QGraphicsView):
    box_clicked = Signal(int)
    rect_drawn = Signal(QRectF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#0b0d11"))
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.pix = QGraphicsPixmapItem()
        self.pix.setTransformationMode(Qt.SmoothTransformation)
        self.scene_.addItem(self.pix)
        self.boxes: list[BoxItem] = []
        self.vw, self.vh = 1280, 720
        self.draw_mode = False
        self._drag_start: QPointF | None = None
        self._rubber: QGraphicsRectItem | None = None
        self.setSceneRect(0, 0, self.vw, self.vh)

    def set_video_size(self, w: int, h: int) -> None:
        self.vw, self.vh = max(w, 1), max(h, 1)
        self.setSceneRect(0, 0, self.vw, self.vh)
        self._fit()

    def set_frame(self, pm: QPixmap) -> None:
        self.pix.setPixmap(pm)
        if pm.width():
            self.pix.setScale(self.vw / pm.width())

    def clear_frame(self) -> None:
        self.pix.setPixmap(QPixmap())

    def scale_factor(self) -> float:
        return self.transform().m11() or 1.0

    def set_boxes(self, items: list[tuple[int, str, str, QRectF]]) -> None:
        for b in self.boxes:
            self.scene_.removeItem(b)
        self.boxes = []
        s = self.scale_factor()
        for tid, tag, status, rect in items:
            b = BoxItem(tid, tag, status, rect, self.box_clicked.emit, s)
            self.scene_.addItem(b)
            self.boxes.append(b)

    def _fit(self) -> None:
        self.fitInView(QRectF(0, 0, self.vw, self.vh), Qt.KeepAspectRatio)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit()

    # 수동 박스 그리기
    def mousePressEvent(self, e) -> None:
        if self.draw_mode and e.button() == Qt.LeftButton:
            self._drag_start = self.mapToScene(e.position().toPoint())
            self._rubber = self.scene_.addRect(QRectF(self._drag_start, self._drag_start),
                                               QPen(QColor(theme.REVIEW), 2 / self.scale_factor(), Qt.DashLine))
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._rubber is not None and self._drag_start is not None:
            self._rubber.setRect(QRectF(self._drag_start, self.mapToScene(e.position().toPoint())).normalized())
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self._rubber is not None:
            r = self._rubber.rect()
            self.scene_.removeItem(self._rubber)
            self._rubber = None
            self._drag_start = None
            if r.width() > 4 and r.height() > 4:
                self.rect_drawn.emit(r.intersected(QRectF(0, 0, self.vw, self.vh)))
            return
        super().mouseReleaseEvent(e)


class Overlay(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet("background:rgba(0,0,0,0.55);color:#dfe3ea;border-radius:5px;padding:2px 7px;"
                           f"font-family:'{theme.mono_family()}';font-size:10.5px;")


class ReportCard(QLabel):
    """6단계: 캔버스 위 보고서 미리보기(서명란 = 결재 단계 수 + 출력 제공)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setStyleSheet("background:#ffffff;color:#1a1f28;border-radius:10px;padding:22px 26px;font-size:12px;")
        self.hide()


class StageView(QWidget):
    seek = Signal(int)
    step_frame = Signal(int)
    play_toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("root")
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 0)
        v.setSpacing(0)
        self.canvas = Canvas()
        v.addWidget(self.canvas, 1)
        self.hud = Overlay(self.canvas)
        self.hud2 = Overlay(self.canvas)
        self.legend = QLabel(self.canvas)
        self.legend.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.legend.setTextFormat(Qt.RichText)
        self.legend.setStyleSheet("background:rgba(0,0,0,0.55);color:#dfe3ea;border-radius:6px;padding:4px 9px;font-size:11px;")
        self.legend.setText(
            f"<span style='color:{theme.MASK}'>■</span> {tr('legend.mask')} &nbsp; "
            f"<span style='color:{theme.PROTECT}'>■</span> {tr('legend.protect')} &nbsp; "
            f"<span style='color:{theme.REVIEW}'>■</span> {tr('legend.review')}")
        self.legend.adjustSize()
        self.report = ReportCard(self.canvas)
        self.empty = QLabel(tr("stage.empty"), self.canvas)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet("color:#6F7785;font-size:14px;background:transparent;")
        # 트랜스포트
        tp = QWidget()
        th = QHBoxLayout(tp)
        th.setContentsMargins(0, 8, 0, 12)
        th.setSpacing(10)
        self.prev_b = button("‹", "tbtn", lambda: self.step_frame.emit(-1))
        self.play_b = button("▶", "tbtn", self._toggle_play)
        self.next_b = button("›", "tbtn", lambda: self.step_frame.emit(1))
        self.prev_b.setToolTip(tr("transport.prev"))
        self.play_b.setToolTip(tr("transport.play"))
        self.next_b.setToolTip(tr("transport.next"))
        self.t_cur = label("00:00:00", "transport")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self.seek.emit)
        self.slider.sliderReleased.connect(lambda: self.seek.emit(self.slider.value()))
        self.t_end = label("00:00:00", "transport")
        self.mode = label(tr("view.original"), "transport")
        for w in (self.prev_b, self.play_b, self.next_b, self.t_cur):
            th.addWidget(w)
        th.addWidget(self.slider, 1)
        th.addWidget(self.t_end)
        th.addWidget(self.mode)
        v.addWidget(tp)
        self.playing = False
        self.fps = 30.0

    def _toggle_play(self) -> None:
        self.set_playing(not self.playing)
        self.play_toggled.emit(self.playing)

    def set_playing(self, on: bool) -> None:
        self.playing = on
        self.play_b.setText("❚❚" if on else "▶")

    def set_media(self, frames: int, fps: float, w: int, h: int) -> None:
        self.fps = fps or 30.0
        self.slider.setRange(0, max(frames - 1, 0))
        self.t_end.setText(timecode(frames, self.fps, False))
        self.canvas.set_video_size(w, h)
        self.hud2.setText(f"{w}×{h} · {self.fps:.2f}")
        self.hud2.adjustSize()
        self.empty.hide()
        self._place()

    def set_position(self, frame: int, total: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(frame)
        self.slider.blockSignals(False)
        self.t_cur.setText(timecode(frame, self.fps, False))
        self.hud.setText(f"{timecode(frame, self.fps)} · f {frame:,} / {total:,}")
        self.hud.adjustSize()
        self._place()

    def show_legend(self, on: bool) -> None:
        self.legend.setVisible(on)

    def show_report(self, html: str | None) -> None:
        if html is None:
            self.report.hide()
            return
        self.report.setText(html)
        self.report.show()
        self._place()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._place()

    def _place(self) -> None:
        cw, ch = self.canvas.width(), self.canvas.height()
        self.hud.move(12, 12)
        self.hud2.move(self.hud.x() + self.hud.width() + 6, 12)
        self.legend.move(cw - self.legend.width() - 12, 12)
        self.empty.setGeometry(0, 0, cw, ch)
        w = min(640, cw - 40)
        self.report.setFixedWidth(max(w, 200))
        self.report.adjustSize()
        self.report.move((cw - self.report.width()) // 2, max(20, (ch - self.report.height()) // 2))
