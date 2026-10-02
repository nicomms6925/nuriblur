"""객체 목록 (영상 전체) — 같은 객체로 보이는 트랙을 묶어 썸네일·ID로 보여 주고,
여러 개를 골라 '마스킹 제외(보호)' · '객체 아님(오검출, 가리지 않음)' · '마스킹(기본)'으로 한 번에 지정한다.

묶음은 워커가 준 트랙(병합 포함)과 병합 제안(외형 유사도)만으로 만든다 — UI는 추론하지 않는다.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from app import theme
from app.i18n import tr
from app.views.stage_view import timecode
from app.widgets.common import button, label

MAX_ITEMS = 600
NOISE_FRAMES_S = 0.5     # '짧고 흐린 객체 숨기기': 이보다 짧고
NOISE_CONF = 0.35        # 신뢰도도 이보다 낮은 객체
STATUS_MARK = {"protect": "✓", "ignore": "×", "review": "?", "mask": "", "mixed": "≈"}
STATUS_COLOR = {"protect": theme.PROTECT, "ignore": theme.IGNORE, "review": theme.REVIEW, "mask": theme.MASK,
                "mixed": theme.REVIEW}


class ObjectsDialog(QDialog):
    apply = Signal(list, str)     # 트랙 ID 목록, protect | ignore | mask
    goto = Signal(int, int)       # 프레임, 트랙

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("obj.title"))
        self.resize(980, 680)
        self.job = None
        v = QVBoxLayout(self)
        v.addWidget(label(tr("obj.note"), "note", wrap=True))
        row = QHBoxLayout()
        self.cls = QComboBox()
        for key in ("all", "face", "plate", "person", "vehicle"):
            self.cls.addItem(tr(f"obj.cls_{key}"), key)
        self.state = QComboBox()
        for key in ("all", "mask", "protect", "ignore", "review"):
            self.state.addItem(tr(f"obj.state_{key}"), key)
        self.sort = QComboBox()
        for key in ("long", "start", "conf_low", "conf_high"):
            self.sort.addItem(tr(f"obj.sort_{key}"), key)
        self.noise = QCheckBox(tr("obj.hide_noise"))
        self.noise.setChecked(True)
        for w in (self.cls, self.state, self.sort, self.noise):
            row.addWidget(w)
            if hasattr(w, "currentIndexChanged"):
                w.currentIndexChanged.connect(self.refresh)
        self.noise.toggled.connect(self.refresh)
        row.addStretch(1)
        self.count = label("", "k")
        row.addWidget(self.count)
        v.addLayout(row)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setIconSize(QSize(96, 96))
        self.list.setGridSize(QSize(132, 150))
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setWordWrap(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setStyleSheet("QListWidget::item:selected{background:rgba(14,122,87,0.28);"
                                f"border:2px solid {theme.BRAND};border-radius:6px}}")
        self.list.itemDoubleClicked.connect(self._goto)
        self.list.itemSelectionChanged.connect(self._sel_changed)
        v.addWidget(self.list, 1)
        bar = QHBoxLayout()
        self.sel = label(tr("obj.selected", n=0), "k")
        bar.addWidget(self.sel)
        bar.addStretch(1)
        self.b_all = button(tr("obj.select_all"), "", self.list.selectAll)
        self.b_protect = button(tr("obj.do_protect"), "", lambda: self._apply("protect"))
        self.b_ignore = button(tr("obj.do_ignore"), "", lambda: self._apply("ignore"))
        self.b_mask = button(tr("obj.do_mask"), "pri", lambda: self._apply("mask"))
        for b in (self.b_all, self.b_protect, self.b_ignore, self.b_mask):
            bar.addWidget(b)
        v.addLayout(bar)
        v.addWidget(label(tr("obj.safety"), "note", wrap=True))
        self._sel_changed()

    def set_job(self, job) -> None:
        self.job = job
        self.refresh()

    def visible_objects(self) -> list[dict]:
        j = self.job
        if j is None:
            return []
        fps = j.fps() or 30.0
        cls, st, srt = self.cls.currentData(), self.state.currentData(), self.sort.currentData()
        out = []
        for o in j.objects():
            if cls != "all" and o["cls"] != cls:
                continue
            if st != "all" and o["status"] != st:
                continue
            if self.noise.isChecked() and o["frames"] < NOISE_FRAMES_S * fps and o["conf"] < NOISE_CONF \
                    and o["status"] not in ("protect", "ignore"):
                continue
            out.append(o)
        key = {"long": lambda o: -o["frames"], "start": lambda o: o["start"],
               "conf_low": lambda o: o["conf"], "conf_high": lambda o: -o["conf"]}[srt]
        return sorted(out, key=key)

    def refresh(self) -> None:
        self.list.clear()
        j = self.job
        if j is None:
            return
        objs = self.visible_objects()
        total = len(j.objects())
        fps = j.fps() or 30.0
        for o in objs[:MAX_ITEMS]:
            t = j.tracks[o["id"]]
            more = f" (+{len(o['members']) - 1})" if len(o["members"]) > 1 else ""
            span = f"{timecode(o['start'], fps, False)[3:]}–{timecode(o['end'], fps, False)[3:]}"
            text = f"{STATUS_MARK[o['status']]} {j.tag(t)}{more}\n{span} · {o['conf']:.2f}"
            it = QListWidgetItem(text)
            pm = QPixmap()
            if o["thumb"] and pm.loadFromData(o["thumb"]):
                it.setIcon(QIcon(pm))
            it.setForeground(QColor(STATUS_COLOR[o["status"]]))
            it.setData(Qt.UserRole, o)
            it.setToolTip(tr("obj.tooltip", tags=", ".join(j.tag(j.tracks[m]) for m in o["members"][:20]),
                             n=len(o["members"])))
            self.list.addItem(it)
        self.count.setText(tr("obj.count", shown=min(len(objs), MAX_ITEMS), n=len(objs), total=total))
        self._sel_changed()

    def _selected_members(self) -> list[int]:
        out = []
        for it in self.list.selectedItems():
            out += it.data(Qt.UserRole)["members"]
        return out

    def _sel_changed(self) -> None:
        n = len(self.list.selectedItems())
        self.sel.setText(tr("obj.selected", n=n))
        for b in (self.b_protect, self.b_ignore, self.b_mask):
            b.setEnabled(n > 0)

    def _apply(self, state: str) -> None:
        ids = self._selected_members()
        if ids:
            self.apply.emit(ids, state)

    def _goto(self, it: QListWidgetItem) -> None:
        o = it.data(Qt.UserRole)
        self.goto.emit(int(o["start"]), int(o["id"]))
