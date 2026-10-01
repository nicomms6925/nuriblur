"""색 토큰·폰트·QSS (docs/06 색 토큰, 목업 v0.3)."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication

BRAND = "#0E7A57"
BRAND2 = "#1FA97B"
MASK = "#F28C38"
PROTECT = "#2BB3A3"
REVIEW = "#E8C547"
DANGER = "#E5534B"


@dataclass(frozen=True)
class Palette:
    bg: str
    panel: str
    panel2: str
    line: str
    line2: str
    ink: str
    ink2: str
    ink3: str
    brand2: str
    log_bg: str


DARK = Palette("#15181E", "#1D2129", "#242933", "#2F3540", "#3A4150", "#E8EBF0", "#A6ADBA", "#6F7785", BRAND2, "#0F1216")
LIGHT = Palette("#EEF0F3", "#FFFFFF", "#F4F6F8", "#D9DEE5", "#C4CAD3", "#1A1F28", "#4F5767", "#8A92A0", BRAND, "#1A1F28")


def is_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() != Qt.ColorScheme.Light
    except Exception:  # noqa: BLE001
        return True


def palette() -> Palette:
    return DARK if is_dark() else LIGHT


def color(name: str) -> QColor:
    return QColor({"brand": BRAND, "brand2": BRAND2, "mask": MASK, "protect": PROTECT, "review": REVIEW,
                   "danger": DANGER}.get(name, name))


def _family(cands: list[str], fallback: str) -> str:
    fams = set(QFontDatabase.families())
    return next((c for c in cands if c in fams), fallback)


def sans_family() -> str:
    return _family(["Pretendard", "Pretendard Variable", "Malgun Gothic", "Apple SD Gothic Neo"], "Sans Serif")


def mono_family() -> str:
    return _family(["JetBrains Mono", "Cascadia Mono", "Consolas", "D2Coding", "Menlo"], "Monospace")


def mono_font(px: int = 11) -> QFont:
    f = QFont(mono_family())
    f.setPixelSize(px)
    return f


def qss(p: Palette | None = None) -> str:
    p = p or palette()
    sans = sans_family()
    mono = f'{mono_family()}", "{sans}'  # 한글은 모노 글꼴에 없으므로 산세리프로 대체
    return f"""
* {{ font-family: "{sans}"; font-size: 13px; color: {p.ink}; }}
QMainWindow, QWidget#root {{ background: {p.bg}; }}
QWidget#panel, QFrame#panel {{ background: {p.panel}; }}
QFrame#topbar {{ background: {p.panel}; border-bottom: 1px solid {p.line}; }}
QFrame#queue {{ background: {p.panel}; border-right: 1px solid {p.line}; }}
QScrollArea#side, QWidget#sideInner {{ background: {p.panel}; border: none; }}
QFrame#side {{ background: {p.panel}; border-left: 1px solid {p.line}; }}
QFrame#timeline {{ background: {p.panel}; border-top: 1px solid {p.line}; }}
QFrame#sec {{ border: none; border-bottom: 1px solid {p.line}; background: transparent; }}
QLabel#h3 {{ color: {p.ink2}; font-size: 12px; font-weight: 600; }}
QLabel#ph {{ color: {p.ink2}; font-size: 12px; font-weight: 600; }}
QLabel#note {{ color: {p.ink3}; font-size: 11.5px; }}
QLabel#k {{ color: {p.ink2}; font-size: 12.5px; }}
QLabel#v, QLabel#mono {{ font-family: "{mono}"; font-size: 11.5px; }}
QLabel#logo {{ font-weight: 700; font-size: 15px; }}
QLabel#logoMark {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {BRAND}, stop:1 {BRAND2});
  color: white; border-radius: 6px; font-size: 12px; }}
QLabel#ver {{ color: {p.ink3}; font-family: "{mono}"; font-size: 11px; }}
QLabel#pill {{ color: {p.ink3}; font-family: "{mono}"; font-size: 11px; border: 1px solid {p.line2};
  border-radius: 11px; padding: 0 9px; min-height: 22px; max-height: 22px; }}
QLabel#pillGpu {{ color: {p.brand2}; font-family: "{mono}"; font-size: 11px; border: 1px solid {BRAND};
  border-radius: 11px; padding: 0 9px; min-height: 22px; max-height: 22px; }}
QPushButton#step {{ background: transparent; border: none; border-radius: 8px; padding: 6px 10px; color: {p.ink2}; text-align: left; }}
QPushButton#step[cur="true"] {{ background: {p.panel2}; color: {p.ink}; }}
QPushButton#step:disabled {{ color: {p.ink3}; }}
QPushButton {{ background: {p.panel2}; border: 1px solid {p.line2}; border-radius: 8px; padding: 7px 12px; font-weight: 600; font-size: 12.5px; }}
QPushButton:hover {{ border-color: {p.ink3}; }}
QPushButton:focus {{ outline: none; border: 2px solid {BRAND2}; }}
QPushButton:disabled {{ color: {p.ink3}; background: {p.panel}; }}
QPushButton#pri {{ background: {BRAND}; border-color: {BRAND}; color: white; }}
QPushButton#pri:disabled {{ background: {p.line2}; border-color: {p.line2}; color: {p.ink3}; }}
QPushButton#danger {{ color: {DANGER}; }}
QPushButton#link {{ background: transparent; border: none; color: {p.brand2}; padding: 0; font-size: 12px; }}
QPushButton#segBtn {{ background: transparent; border: none; border-radius: 6px; padding: 6px; color: {p.ink2}; font-weight: 500; font-size: 12px; }}
QPushButton#segBtn:checked {{ background: {p.panel}; color: {p.ink}; }}
QFrame#seg {{ background: {p.panel2}; border-radius: 8px; }}
QFrame#drop {{ border: 1.5px dashed {p.line2}; border-radius: 10px; }}
QFrame#drop[hover="true"] {{ border-color: {BRAND2}; }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit#input {{ background: {p.panel2}; border: 1px solid {p.line}; border-radius: 8px;
  padding: 6px 9px; font-size: 12px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {BRAND2}; }}
QComboBox QAbstractItemView {{ background: {p.panel}; selection-background-color: {p.panel2}; }}
QCheckBox {{ font-size: 12.5px; spacing: 8px; }}
QProgressBar {{ background: {p.panel2}; border: none; border-radius: 4px; height: 8px; }}
QProgressBar::chunk {{ border-radius: 4px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {BRAND}, stop:1 {BRAND2}); }}
QPlainTextEdit#log {{ background: {p.log_bg}; color: #b8c0cc; border: none; border-radius: 8px;
  font-family: "{mono}"; font-size: 10.5px; padding: 6px; }}
QFrame#gauge {{ background: {p.panel2}; border-radius: 8px; }}
QFrame#count {{ border: 1px solid {p.line}; border-radius: 8px; }}
QFrame#count[kind="protect"] {{ border-color: {PROTECT}; }}
QFrame#count[kind="review"] {{ border-color: {REVIEW}; }}
QLabel#gN {{ font-family: "{mono}"; font-size: 16px; font-weight: 500; }}
QLabel#cN {{ font-family: "{mono}"; font-size: 18px; }}
QLabel#gL {{ color: {p.ink3}; font-size: 10.5px; }}
QFrame#tr {{ border: 1px solid {p.line}; border-radius: 8px; }}
QFrame#tr:hover {{ background: {p.panel2}; }}
QFrame#tr[sel="true"] {{ border-color: {BRAND2}; }}
QLabel#trN {{ font-weight: 600; font-size: 12px; }}
QLabel#trD {{ font-family: "{mono}"; font-size: 10.5px; color: {p.ink3}; }}
QListWidget#jobs {{ background: transparent; border: none; outline: none; }}
QListWidget#jobs::item {{ border-top: 1px solid {p.line}; padding: 0; }}
QListWidget#jobs::item:selected {{ background: {p.panel2}; border-left: 3px solid {BRAND2}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.line}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {BRAND2}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QLabel#transport {{ color: {p.ink2}; font-family: "{mono}"; font-size: 11px; }}
QPushButton#tbtn {{ min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; padding: 0; border-radius: 6px; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {p.line2}; border-radius: 4px; min-height: 20px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: {p.panel2}; border: 1px solid {p.line2}; color: {p.ink}; }}
QLabel#toast {{ background: {p.panel2}; border: 1px solid {p.line2}; border-radius: 8px; padding: 8px 14px; font-size: 12.5px; }}
QSplitter::handle {{ background: {p.line}; }}
"""
