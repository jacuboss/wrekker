"""Global color palette and Qt stylesheet."""
from __future__ import annotations

BG_DEEP    = "#080808"
BG_PANEL   = "#0f0f0f"
BG_CARD    = "#161616"
BG_CTRL    = "#1c1c1c"
BORDER     = "#252525"
BORDER_LIT = "#363636"
TEXT_DIM   = "#363636"
TEXT_MED   = "#707070"
TEXT_BRIGHT= "#c0c0c0"
WHITE      = "#f0f0f0"

DECK_A     = "#00d4ff"   # electric cyan
DECK_B     = "#ff1f5a"   # hot pink

STEM_VOCALS = "#ff6b6b"
STEM_DRUMS  = "#4ecdc4"
STEM_BASS   = "#ffe66d"
STEM_OTHER  = "#a29bfe"

STEM_COLORS: dict[str, str] = {
    "vocals": STEM_VOCALS,
    "drums":  STEM_DRUMS,
    "bass":   STEM_BASS,
    "other":  STEM_OTHER,
}
STEM_LABELS: dict[str, str] = {
    "vocals": "VOC",
    "drums":  "DRM",
    "bass":   "BSS",
    "other":  "OTH",
}

STATUS_OK   = "#00e87a"
STATUS_WARN = "#ffa726"
STATUS_ERR  = "#ff4444"


def deck_color(deck_id: str) -> str:
    return DECK_A if deck_id == "A" else DECK_B


GLOBAL_SS = f"""
* {{
    font-family: "Inter", "Roboto", "Noto Sans", system-ui, sans-serif;
    font-size: 12px;
}}
QWidget {{
    background-color: {BG_DEEP};
    color: {TEXT_BRIGHT};
    border: none;
    outline: none;
}}
QLabel {{ background: transparent; }}

QPushButton {{
    background-color: {BG_CTRL};
    color: {TEXT_MED};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 4px 8px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
}}
QPushButton:hover {{ border-color: {BORDER_LIT}; color: {TEXT_BRIGHT}; }}
QPushButton:checked {{ background-color: {BORDER_LIT}; color: {WHITE}; }}
QPushButton:pressed {{ background-color: {BORDER_LIT}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; }}

QSlider::groove:horizontal {{
    height: 3px;
    background: {BORDER};
    border-radius: 2px;
    margin: 0 4px;
}}
QSlider::handle:horizontal {{
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 6px;
    background: {TEXT_BRIGHT};
}}
QSlider::sub-page:horizontal {{
    border-radius: 2px;
}}

QLineEdit {{
    background: {BG_CTRL};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    color: {TEXT_BRIGHT};
    selection-background-color: #1a3040;
}}
QLineEdit:focus {{ border-color: {DECK_A}; }}

QTableView {{
    background: {BG_PANEL};
    alternate-background-color: {BG_CARD};
    gridline-color: {BORDER};
    border: none;
    selection-background-color: #162a35;
    selection-color: {WHITE};
}}
QTableView::item {{ padding: 3px 8px; border: none; }}
QHeaderView::section {{
    background: {BG_CTRL};
    color: {TEXT_MED};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 5px 8px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.6px;
}}
QScrollBar:vertical {{
    background: {BG_PANEL};
    width: 5px;
    border-radius: 3px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_LIT};
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {BG_PANEL};
    height: 5px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_LIT};
    border-radius: 3px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QSplitter::handle {{ background: {BORDER}; }}
QMenu {{
    background: {BG_CARD};
    border: 1px solid {BORDER_LIT};
    border-radius: 4px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 20px; border-radius: 3px; }}
QMenu::item:selected {{ background: {BORDER_LIT}; color: {WHITE}; }}
"""
