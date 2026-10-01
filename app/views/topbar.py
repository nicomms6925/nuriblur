"""TopBar: 로고 · 6단계 스테퍼 · 실행환경 배지."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel

from app.i18n import tr
from app.widgets.common import button, label, repolish

STEPS = ["step.input", "step.analyze", "step.protect", "step.review", "step.export", "step.org"]


class TopBar(QFrame):
    step_clicked = Signal(int)

    def __init__(self, org_mode: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("topbar")
        self.setFixedHeight(48)
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 0, 16, 0)
        h.setSpacing(4)
        mark = label("✦", "logoMark")
        mark.setFixedSize(22, 22)
        mark.setAlignment(Qt.AlignCenter)
        h.addWidget(mark)
        h.addSpacing(6)
        h.addWidget(label(tr("app.name"), "logo"))
        h.addSpacing(4)
        h.addWidget(label(tr("app.version"), "ver"))
        h.addSpacing(20)
        self.buttons = []
        for i, key in enumerate(STEPS, 1):
            if i == 6 and not org_mode:
                break
            b = button(f"{i}  {tr(key)}", "step", lambda _=False, n=i: self.step_clicked.emit(n))
            b.setProperty("cur", False)
            b.setProperty("n", i)
            self.buttons.append(b)
            h.addWidget(b)
        h.addStretch(1)
        self.ep = QLabel(tr("top.ep_starting"))
        self.ep.setObjectName("pill")
        h.addWidget(self.ep, 0, Qt.AlignVCenter)
        h.addSpacing(6)
        h.addWidget(label(tr("top.local_only"), "pill"), 0, Qt.AlignVCenter)

    def set_step(self, cur: int, max_enabled: int) -> None:
        for b in self.buttons:
            n = b.property("n")
            b.setProperty("cur", n == cur)
            done = n < cur
            b.setText(f"{'✓' if done else n}  {tr(STEPS[n - 1])}")
            b.setEnabled(n <= max_enabled)
            repolish(b)

    def set_ep(self, text: str, gpu: bool) -> None:
        self.ep.setText(text)
        self.ep.setObjectName("pillGpu" if gpu else "pill")
        repolish(self.ep)
