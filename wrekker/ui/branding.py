"""Shared Wrekker branding helpers."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QLabel


LOGO_PATH = Path(__file__).resolve().parent / "assets" / "wrekker_logo.svg"


def logo_label(size: int = 24, *, tooltip: str = "Wrekker") -> QLabel:
    label = QLabel()
    label.setFixedSize(size, size)
    label.setToolTip(tooltip)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if LOGO_PATH.exists():
        label.setPixmap(QIcon(str(LOGO_PATH)).pixmap(QSize(size, size)))
    else:
        label.setText("W")
        label.setStyleSheet("color: #ffb000; font-weight: 900;")
    return label
