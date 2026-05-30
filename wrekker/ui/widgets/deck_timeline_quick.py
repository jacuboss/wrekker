"""Qt Quick deck timeline bridge.

This keeps deck waveform rendering in QML while DeckWidget remains the Python
owner of state and user-action routing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from wrekker.ui.qml_models import DeckTimelineModel

log = logging.getLogger(__name__)

_DECK_QML_FLAG = "WREKKER_ENABLE_QML_DECK_WAVEFORMS"
_DECK_QML_UNSTABLE_FLAG = "WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS"


def deck_qml_waveforms_enabled() -> bool:
    return os.environ.get(_DECK_QML_FLAG) == "1" and os.environ.get(_DECK_QML_UNSTABLE_FLAG) == "1"


def deck_qml_waveforms_requested() -> bool:
    return os.environ.get(_DECK_QML_FLAG) == "1"


class DeckTimelineQuick(QWidget):
    seek_requested = pyqtSignal(float)
    marker_right_clicked = pyqtSignal(object)

    def __init__(self, deck_id: str, parent=None) -> None:
        super().__init__(parent)
        self._model = DeckTimelineModel(deck_id, self)
        self._available = False
        self._quick_view = None
        self._container = None
        self._fallback_reason = ""
        self.setMinimumHeight(164)
        self.setMaximumHeight(176)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        if not deck_qml_waveforms_enabled():
            if deck_qml_waveforms_requested():
                self._fallback_reason = (
                    f"{_DECK_QML_FLAG} was requested, but the current Qt Quick deck "
                    f"window-container path is disabled after performance regression. "
                    f"Set {_DECK_QML_UNSTABLE_FLAG}=1 only for profiling."
                )
            else:
                self._fallback_reason = f"{_DECK_QML_FLAG} is not enabled"
            label = QLabel("Qt Quick deck waveforms disabled; using QWidget timeline.")
            label.setStyleSheet("color: #8a959d; background: #090d10; border: 1px solid #26323a; padding: 8px;")
            lay.addWidget(label)
            return
        try:
            from PyQt6.QtQml import QQmlError
            from PyQt6.QtQuick import QQuickView

            self._quick_view = QQuickView()
            self._quick_view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
            self._quick_view.rootContext().setContextProperty("deckTimelineModel", self._model)
            qml_path = Path(__file__).resolve().parents[1] / "qml" / "DeckTimeline.qml"
            self._quick_view.setSource(QUrl.fromLocalFile(str(qml_path)))
            if self._quick_view.status() == QQuickView.Status.Error:
                errors: list[QQmlError] = self._quick_view.errors()
                raise RuntimeError("; ".join(e.toString() for e in errors) or "QQuickView failed")
            root = self._quick_view.rootObject()
            if root is None:
                raise RuntimeError("DeckTimeline.qml did not create a root object")
            root.seekRequested.connect(self.seek_requested.emit)
            root.markerContextRequested.connect(self._on_marker_context_requested)
            self._container = QWidget.createWindowContainer(self._quick_view, self)
            self._container.setMinimumHeight(164)
            self._container.setMaximumHeight(176)
            lay.addWidget(self._container)
            self._available = True
            if os.environ.get("WREKKER_WAVEFORM_RENDER_DEBUG") == "1":
                log.info("Deck %s waveform renderer: QQuickView/createWindowContainer", deck_id)
        except Exception as exc:
            self._fallback_reason = str(exc)
            log.warning("Qt Quick deck timeline unavailable; using QWidget fallback: %s", exc)
            label = QLabel("Qt Quick deck timeline unavailable.")
            label.setStyleSheet("color: #ffb000; background: #090d10; border: 1px solid #26323a; padding: 8px;")
            lay.addWidget(label)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def fallback_reason(self) -> str:
        return self._fallback_reason

    def set_waveform(self, data) -> None:
        self._model.set_waveform(data)

    def set_markers(self, markers) -> None:
        self._model.set_markers(markers)

    def set_marker_display_mode(self, mode) -> None:
        self._model.set_marker_display_mode(mode)

    def update_position(
        self,
        pos_s: float,
        duration_s: float,
        beats,
        bpm: float,
        first_beat_s: float,
        cue_positions,
        loop,
        playing: bool,
        sync_enabled: bool = False,
        phase_err=None,
    ) -> None:
        self._model.update_position(
            pos_s,
            duration_s,
            beats,
            bpm,
            first_beat_s,
            cue_positions,
            loop,
            playing,
            sync_enabled,
            phase_err,
        )

    def set_other_deck(self, pos_s: float, beats, bpm: float, first_beat_s: float, source_playing: bool = True) -> None:
        self._model.set_other_deck(pos_s, beats, bpm, first_beat_s)

    def set_stem_gains(self, gains) -> None:
        # Stem-gain overview strips remain Python-side data; QML timeline does
        # not alter audio or stem state.
        return

    def _on_marker_context_requested(self, marker_id: str) -> None:
        self.marker_right_clicked.emit(self._model.marker_object(marker_id))
