"""
Texture-backed zoom waveform renderer.

This is intentionally kept separate from ``zoom_waveform.py`` so the deck can
switch renderers with a flag. The current implementation builds a full-track
texture from ``WaveformData`` at load time; the same class is the handoff point
for loading pre-rendered waveform tiles from ``.wrk`` later.
"""
from __future__ import annotations

import os
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from wrekker.core.deck import WaveformData

from wrekker.ui.widgets.zoom_waveform import ZoomWaveformWidget


class TextureZoomWaveformWidget(ZoomWaveformWidget):
    """Pre-rendered texture renderer with the same public API as ZoomWaveformWidget.

    The classic widget already uses a pixmap cache internally. This renderer
    makes that cache the explicit render contract and uses more conservative
    defaults for texture stability. Keeping it in a separate class lets us later
    replace ``_rebuild_waveform_cache`` with ``.wrk`` tile loading without
    changing ``DeckWidget`` or transport code.
    """

    def __init__(self, deck_id: str, parent=None) -> None:
        super().__init__(deck_id, parent)
        self._cache_scale = self._env_int("WREKKER_TEXTURE_ZOOM_CACHE_SCALE", "4", 1, 8)
        self._peak_smooth = self._env_int("WREKKER_TEXTURE_ZOOM_PEAK_SMOOTH", "5", 0, 15)
        self._texture_source = "runtime"

    @staticmethod
    def _env_int(name: str, default: str, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(os.environ.get(name, default))))
        except ValueError:
            return int(default)

    def set_waveform(self, data: Optional["WaveformData"]) -> None:
        self._texture_source = "runtime"
        super().set_waveform(data)

    @property
    def texture_source(self) -> str:
        return self._texture_source
