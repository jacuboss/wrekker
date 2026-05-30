from __future__ import annotations

_APP = None


def _app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_zoom_renderer_defaults_to_texture(monkeypatch) -> None:
    monkeypatch.delenv("WREKKER_ZOOM_RENDERER", raising=False)
    _app()

    from wrekker.ui.widgets.deck import _make_zoom_waveform
    from wrekker.ui.widgets.texture_zoom_waveform import TextureZoomWaveformWidget

    widget = _make_zoom_waveform("A")
    try:
        assert isinstance(widget, TextureZoomWaveformWidget)
    finally:
        widget.deleteLater()


def test_zoom_renderer_can_use_legacy_classic(monkeypatch) -> None:
    monkeypatch.setenv("WREKKER_ZOOM_RENDERER", "classic")
    _app()

    from wrekker.ui.widgets.deck import _make_zoom_waveform
    from wrekker.ui.widgets.zoom_waveform import ZoomWaveformWidget

    widget = _make_zoom_waveform("A")
    try:
        assert type(widget) is ZoomWaveformWidget
    finally:
        widget.deleteLater()


def test_lab_waveform_renderer_defaults_to_texture(monkeypatch) -> None:
    monkeypatch.delenv("WREKKER_LAB_WAVEFORM_RENDERER", raising=False)
    _app()

    from wrekker.ui.widgets.lab import _make_lab_waveform
    from wrekker.ui.widgets.lab_texture_waveform import TextureLabWaveform

    widget = _make_lab_waveform("zoom")
    try:
        assert isinstance(widget, TextureLabWaveform)
    finally:
        widget.deleteLater()


def test_lab_waveform_renderer_can_use_legacy_classic(monkeypatch) -> None:
    monkeypatch.setenv("WREKKER_LAB_WAVEFORM_RENDERER", "classic")
    _app()

    from wrekker.ui.widgets.lab import _LabWaveform, _make_lab_waveform

    widget = _make_lab_waveform("overview")
    try:
        assert type(widget) is _LabWaveform
    finally:
        widget.deleteLater()
