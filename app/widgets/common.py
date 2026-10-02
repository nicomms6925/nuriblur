"""공용 위젯: 섹션, 세그먼트 버튼, 게이지·카운터, 토글, 트랙 행, 토스트."""
from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app import theme


def label(text: str = "", name: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    return lb


def button(text: str, name: str = "", slot=None) -> QPushButton:
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.setCursor(Qt.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b


def clear_layout(lay) -> None:
    """레이아웃의 위젯을 즉시 숨기고 떼어낸 뒤 삭제 예약."""
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


class Section(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("sec")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(16, 12, 16, 12)
        self.v.setSpacing(6)
        self.title = label(title, "h3")
        if title:
            self.v.addWidget(self.title)

    def add(self, w) -> QWidget:
        if isinstance(w, QWidget):
            self.v.addWidget(w)
        else:
            self.v.addLayout(w)
        return w


def kv_row(k: str, v: str = "") -> tuple[QWidget, QLabel]:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 2, 0, 2)
    kl = label(k, "k")
    kl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
    vl = label(v, "v")
    vl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    vl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    h.addWidget(kl)
    h.addStretch(1)
    h.addWidget(vl)
    return w, vl


class Seg(QFrame):
    changed = Signal(str)

    def __init__(self, items: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setObjectName("seg")
        h = QHBoxLayout(self)
        h.setContentsMargins(3, 3, 3, 3)
        h.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in items:
            b = button(text, "segBtn")
            b.setCheckable(True)
            b.setProperty("key", key)
            self.group.addButton(b)
            self.buttons[key] = b
            h.addWidget(b)
        if items:
            self.buttons[items[0][0]].setChecked(True)
        self.group.buttonClicked.connect(lambda b: self.changed.emit(b.property("key")))

    def value(self) -> str:
        b = self.group.checkedButton()
        return b.property("key") if b else ""

    def set_value(self, key: str) -> None:
        if key in self.buttons:
            self.buttons[key].setChecked(True)


class Stat(QFrame):
    """게이지(gauge) 또는 카운터(count) 칸."""

    def __init__(self, caption: str, kind: str = "gauge", accent: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName(kind)
        if accent:
            self.setProperty("kind", accent)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(0)
        self.n = label("—", "gN" if kind == "gauge" else "cN")
        v.addWidget(self.n)
        v.addWidget(label(caption, "gL"))

    def set(self, text: str) -> None:
        self.n.setText(text)


class Toggle(QAbstractButton):
    """보호(청록)/마스킹(주황)/검수(노랑) 토글 스위치."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.review = False
        self.setFixedSize(34, 20)
        self.setFocusPolicy(Qt.StrongFocus)

    def sizeHint(self) -> QSize:
        return QSize(34, 20)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = theme.PROTECT if self.isChecked() else (theme.REVIEW if self.review else theme.MASK)
        p.setBrush(QColor(c))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, 34, 20), 10, 10)
        p.setBrush(QColor("white"))
        x = 16 if self.isChecked() else 2
        p.drawEllipse(QRectF(x, 2, 16, 16))
        if self.hasFocus():
            p.setPen(QColor(theme.BRAND2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(0.5, 0.5, 33, 19), 10, 10)


class TrackRowW(QFrame):
    clicked = Signal(int)
    toggled = Signal(int, bool)

    def __init__(self, tid: int, title: str, desc: str, thumb: bytes = b"", trailing: QWidget | None = None,
                 toggle_state: str | None = None, parent=None):
        super().__init__(parent)
        self.tid = tid
        self.setObjectName("tr")
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(8)
        th = QLabel()
        th.setFixedSize(36, 36)
        if thumb:
            pm = QPixmap()
            pm.loadFromData(thumb)
            th.setPixmap(pm.scaled(36, 36, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        th.setStyleSheet("background:#2b3a44;border-radius:6px;")
        h.addWidget(th)
        v = QVBoxLayout()
        v.setSpacing(0)
        self.t = label(title, "trN")
        self.t.setTextFormat(Qt.RichText)
        self.d = label(desc, "trD")
        for lb in (self.t, self.d):
            lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(desc)
        v.addWidget(self.t)
        v.addWidget(self.d)
        h.addLayout(v, 1)
        self.toggle: Toggle | None = None
        if toggle_state is not None:
            self.toggle = Toggle()
            self.toggle.setChecked(toggle_state == "protect")
            self.toggle.review = toggle_state == "review"
            self.toggle.clicked.connect(lambda c: self.toggled.emit(self.tid, c))
            h.addWidget(self.toggle)
        if trailing is not None:
            h.addWidget(trailing)

    def mousePressEvent(self, e) -> None:
        self.clicked.emit(self.tid)
        super().mousePressEvent(e)


class Toast(QLabel):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.eff)
        self.anim = QPropertyAnimation(self.eff, b"opacity", self)
        self.anim.setDuration(200)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._hide)
        self.eff.setOpacity(0)
        self.hide()

    def say(self, msg: str, ms: int = 2200) -> None:
        self.setText(msg)
        self.adjustSize()
        par = self.parentWidget()
        self.move((par.width() - self.width()) // 2, par.height() - 200)
        self.show()
        self.raise_()
        self.anim.stop()
        self.anim.setStartValue(self.eff.opacity())
        self.anim.setEndValue(1.0)
        self.anim.start()
        self.timer.start(ms)

    def _hide(self) -> None:
        self.anim.stop()
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)
        self.anim.start()


def hspacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w
