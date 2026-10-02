"""StageView: 프레임 캔버스(QGraphicsView) + 박스 오버레이 + HUD/범례 + 트랜스포트 (G2-05/06)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app import theme
from app.i18n import tr
from app.widgets.common import button, label

STATUS_COLOR = {"protect": theme.PROTECT, "mask": theme.MASK, "review": theme.REVIEW, "exposure": theme.DANGER}


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
        elif status == "exposure":  # 재검사 노출 위치(검수 '보기')
            pen.setWidthF(3.0 / scale)
        self.setPen(pen)
        self.setBrush(Qt.NoBrush)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, False)
        text = tag + (" ✓" if status == "protect" else " ?" if status == "review" else " !" if status == "exposure" else "")
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
    """프레임 캔버스. 휠=확대/축소(마우스 위치 기준), 확대 상태에서 드래그=이동, Shift+휠=프레임 이동."""

    box_clicked = Signal(int)
    rect_drawn = Signal(QRectF)
    point_clicked = Signal(QPointF)   # 그리기 모드에서 드래그 없이 클릭
    zoom_changed = Signal(float)
    wheel_frames = Signal(int)
    ZOOMS = (0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#0b0d11"))
        self.setAcceptDrops(False)               # 영상 끌어다 놓기는 메인 창이 받는다
        self.viewport().setAcceptDrops(False)
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.pix = QGraphicsPixmapItem()
        self.pix.setTransformationMode(Qt.SmoothTransformation)
        self.scene_.addItem(self.pix)
        self.boxes: list[BoxItem] = []
        self.vw, self.vh = 1280, 720
        self._draw_mode = False
        self._drag_start: QPointF | None = None
        self._rubber: QGraphicsRectItem | None = None
        self.fit_mode = True
        self._last_items: list = []
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setSceneRect(0, 0, self.vw, self.vh)

    # ---- 그리기 모드 ----
    @property
    def draw_mode(self) -> bool:
        return self._draw_mode

    @draw_mode.setter
    def draw_mode(self, on: bool) -> None:
        self._draw_mode = on
        self.viewport().setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self._update_drag()

    def _update_drag(self) -> None:
        self.setDragMode(QGraphicsView.ScrollHandDrag if (not self._draw_mode and not self.fit_mode)
                         else QGraphicsView.NoDrag)

    # ---- 확대/축소 ----
    def zoom(self) -> float:
        return self.scale_factor()

    def set_zoom(self, z: float, anchor_mouse: bool = False) -> None:
        z = max(self.ZOOMS[0], min(self.ZOOMS[-1], z))
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse if anchor_mouse else QGraphicsView.AnchorViewCenter)
        self.fit_mode = False
        cur = self.scale_factor()
        self.scale(z / cur, z / cur)
        self._update_drag()
        self._rebox()
        self.zoom_changed.emit(self.zoom())

    def zoom_in(self, anchor_mouse: bool = False) -> None:
        cur = self.zoom()
        self.set_zoom(next((z for z in self.ZOOMS if z > cur * 1.01), self.ZOOMS[-1]), anchor_mouse)

    def zoom_out(self, anchor_mouse: bool = False) -> None:
        cur = self.zoom()
        self.set_zoom(next((z for z in reversed(self.ZOOMS) if z < cur * 0.99), self.ZOOMS[0]), anchor_mouse)

    def fit(self) -> None:
        self.fit_mode = True
        self._fit()
        self._update_drag()
        self._rebox()
        self.zoom_changed.emit(self.zoom())

    def actual_size(self) -> None:
        """원본 크기 (영상 1픽셀 = 화면 1픽셀)."""
        self.set_zoom(1.0 / max(self.devicePixelRatioF(), 1.0))

    def wheelEvent(self, e) -> None:
        dy = e.angleDelta().y()
        if not dy:
            return
        if e.modifiers() & Qt.ShiftModifier:
            self.wheel_frames.emit(-1 if dy > 0 else 1)
        elif dy > 0:
            self.zoom_in(anchor_mouse=True)
        else:
            self.zoom_out(anchor_mouse=True)
        e.accept()

    def _rebox(self) -> None:
        if self._last_items:
            self.set_boxes(self._last_items)

    def set_video_size(self, w: int, h: int) -> None:
        self.vw, self.vh = max(w, 1), max(h, 1)
        self.setSceneRect(0, 0, self.vw, self.vh)
        self.fit()

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
        self._last_items = items
        s = self.scale_factor()
        for tid, tag, status, rect in items:
            b = BoxItem(tid, tag, status, rect, self.box_clicked.emit, s)
            self.scene_.addItem(b)
            self.boxes.append(b)

    def _fit(self) -> None:
        self.fitInView(QRectF(0, 0, self.vw, self.vh), Qt.KeepAspectRatio)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.fit_mode:
            self._fit()
            self.zoom_changed.emit(self.zoom())

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
            else:
                self.point_clicked.emit(r.center())
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


SPEEDS = (0.25, 0.5, 1.0, 1.5, 2.0, 4.0)


class StageView(QWidget):
    """캔버스 + 재생 컨트롤(처음·-10초·이전 프레임·재생·정지·다음 프레임·+10초·끝, 배속, 프레임 번호)
    + 보기 도구(확대·축소·맞춤·원본 크기, 마스킹 대상 지정)."""

    seek = Signal(int)
    step_frame = Signal(int)
    step_seconds = Signal(float)
    play_toggled = Signal(bool)
    stop_clicked = Signal()
    speed_changed = Signal(float)
    mask_tool = Signal(bool)

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
        self.canvas.wheel_frames.connect(self.step_frame.emit)
        # 트랜스포트 1행: 재생 컨트롤 + 시간 + 슬라이더
        tp = QWidget()
        th = QHBoxLayout(tp)
        th.setContentsMargins(0, 8, 0, 4)
        th.setSpacing(6)
        self.start_b = button("⏮", "tbtn", lambda: self.seek.emit(0))
        self.back10_b = button("−10", "tbtn", lambda: self.step_seconds.emit(-10))
        self.prev_b = button("‹", "tbtn", lambda: self.step_frame.emit(-1))
        self.play_b = button("▶", "tbtn", self._toggle_play)
        self.stop_b = button("■", "tbtn", self._stop)
        self.next_b = button("›", "tbtn", lambda: self.step_frame.emit(1))
        self.fwd10_b = button("+10", "tbtn", lambda: self.step_seconds.emit(10))
        self.end_b = button("⏭", "tbtn", lambda: self.seek.emit(self.slider.maximum()))
        for b, k in ((self.start_b, "start"), (self.back10_b, "back10"), (self.prev_b, "prev"), (self.play_b, "play"),
                     (self.stop_b, "stop"), (self.next_b, "next"), (self.fwd10_b, "fwd10"), (self.end_b, "end")):
            b.setToolTip(tr(f"transport.{k}"))
            th.addWidget(b)
        th.addSpacing(6)
        self.t_cur = label("00:00:00", "transport")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self.seek.emit)
        self.slider.sliderReleased.connect(lambda: self.seek.emit(self.slider.value()))
        self.t_end = label("00:00:00", "transport")
        th.addWidget(self.t_cur)
        th.addWidget(self.slider, 1)
        th.addWidget(self.t_end)
        v.addWidget(tp)
        # 트랜스포트 2행: 배속 · 프레임 이동 · 보기 모드 · 마스킹 대상 · 확대/축소
        tp2 = QWidget()
        t2 = QHBoxLayout(tp2)
        t2.setContentsMargins(0, 0, 0, 10)
        t2.setSpacing(6)
        t2.addWidget(label(tr("transport.speed"), "transport"))
        self.speed = QComboBox()
        for sp in SPEEDS:
            self.speed.addItem(f"{sp:g}×", sp)
        self.speed.setCurrentIndex(SPEEDS.index(1.0))
        self.speed.setToolTip(tr("transport.speed_tip"))
        self.speed.currentIndexChanged.connect(lambda _: self.speed_changed.emit(self.speed.currentData()))
        t2.addWidget(self.speed)
        t2.addSpacing(8)
        t2.addWidget(label(tr("transport.frame"), "transport"))
        self.frame_box = QSpinBox()
        self.frame_box.setRange(0, 0)
        self.frame_box.setKeyboardTracking(False)
        self.frame_box.setToolTip(tr("transport.frame_tip"))
        self.frame_box.setMinimumWidth(90)
        self.frame_box.valueChanged.connect(self.seek.emit)
        t2.addWidget(self.frame_box)
        t2.addSpacing(8)
        self.mode = label(tr("view.original"), "transport")
        t2.addWidget(self.mode)
        t2.addStretch(1)
        self.mask_b = button(tr("tool.mask_target"), "", self._mask_tool)
        self.mask_b.setCheckable(True)
        self.mask_b.setToolTip(tr("tool.mask_target_tip"))
        t2.addWidget(self.mask_b)
        t2.addSpacing(8)
        self.zout_b = button("−", "tbtn", lambda: self.canvas.zoom_out())
        self.fit_b = button(tr("zoom.fit"), "", self.canvas.fit)
        self.one_b = button(tr("zoom.actual"), "", self.canvas.actual_size)
        self.zin_b = button("+", "tbtn", lambda: self.canvas.zoom_in())
        self.zoom_l = label("100%", "transport")
        self.zoom_l.setMinimumWidth(44)
        for b, k in ((self.zout_b, "out"), (self.fit_b, "fit_tip"), (self.one_b, "actual_tip"), (self.zin_b, "in")):
            b.setToolTip(tr(f"zoom.{k}"))
        for w in (self.zout_b, self.zoom_l, self.zin_b, self.fit_b, self.one_b):
            t2.addWidget(w)
        self.canvas.zoom_changed.connect(lambda z: self.zoom_l.setText(f"{z * 100:.0f}%"))
        v.addWidget(tp2)
        self.playing = False
        self.fps = 30.0

    def _stop(self) -> None:
        self.set_playing(False)
        self.stop_clicked.emit()

    def _mask_tool(self) -> None:
        on = self.mask_b.isChecked()
        self.canvas.draw_mode = on
        self.mask_tool.emit(on)

    def set_mask_tool(self, on: bool, enabled: bool = True) -> None:
        self.mask_b.blockSignals(True)
        self.mask_b.setChecked(on)
        self.mask_b.blockSignals(False)
        self.mask_b.setEnabled(enabled)
        if not on:
            self.canvas.draw_mode = False

    def speed_value(self) -> float:
        return float(self.speed.currentData() or 1.0)

    def _toggle_play(self) -> None:
        self.set_playing(not self.playing)
        self.play_toggled.emit(self.playing)

    def set_playing(self, on: bool) -> None:
        self.playing = on
        self.play_b.setText("❚❚" if on else "▶")

    def set_media(self, frames: int, fps: float, w: int, h: int) -> None:
        self.fps = fps or 30.0
        self.slider.setRange(0, max(frames - 1, 0))
        self.frame_box.blockSignals(True)
        self.frame_box.setRange(0, max(frames - 1, 0))
        self.frame_box.blockSignals(False)
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
        if not self.frame_box.hasFocus():
            self.frame_box.blockSignals(True)
            self.frame_box.setValue(frame)
            self.frame_box.blockSignals(False)
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
