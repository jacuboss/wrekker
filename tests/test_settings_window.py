from __future__ import annotations

_APP = None


def _app():
    global _APP
    from PyQt6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_settings_window_builds_core_sections(tmp_path) -> None:
    _app()
    from wrekker.settings import SettingsStore
    from wrekker.ui.widgets.settings_window import SettingsWindow

    store = SettingsStore.load_or_create(tmp_path / "settings.json")
    win = SettingsWindow(store)
    try:
        labels = [win._nav.item(i).text() for i in range(win._nav.count())]
        assert "Audio & Routing" in labels
        assert "WREKKED & Fastload" in labels
        assert "Waveforms & Display" in labels
        assert "WREKKER LAB" in labels
        assert store.get("waveforms.deck_renderer", include_env=False) == "texture"
    finally:
        win.close()
        win.deleteLater()


def test_settings_window_search_filters_sections(tmp_path) -> None:
    _app()
    from wrekker.settings import SettingsStore
    from wrekker.ui.widgets.settings_window import SettingsWindow

    store = SettingsStore.load_or_create(tmp_path / "settings.json")
    win = SettingsWindow(store)
    try:
        win._search.setText("fastload")
        visible = [win._nav.item(i).text() for i in range(win._nav.count()) if not win._nav.item(i).isHidden()]
        assert "WREKKED & Fastload" in visible
    finally:
        win.close()
        win.deleteLater()


def test_settings_window_device_selectors_are_enabled_and_populated(tmp_path, monkeypatch) -> None:
    _app()
    from wrekker.settings import SettingsStore
    from wrekker.ui.widgets.settings_window import SettingsWindow

    monkeypatch.setattr(SettingsWindow, "_audio_devices", lambda self: ["System Default", "USB Audio (4 ch)"])
    monkeypatch.setattr(SettingsWindow, "_cue_devices", lambda self: ["Auto multichannel / FLX4", "USB Audio (4 ch)"])
    monkeypatch.setattr(SettingsWindow, "_midi_ports", lambda self, direction: ["Auto", f"DDJ-FLX4 {direction}"])

    store = SettingsStore.load_or_create(tmp_path / "settings.json")
    win = SettingsWindow(store)
    try:
        main = win._controls["audio.main_output_device"]
        cue = win._controls["audio.cue_device"]
        midi_in = win._controls["controller.midi_input_port"]
        midi_out = win._controls["controller.midi_output_port"]

        assert main.isEnabled()
        assert main.count() == 2
        assert cue.isEnabled()
        assert cue.count() == 2
        assert midi_in.count() == 2
        assert midi_out.count() == 2
    finally:
        win.close()
        win.deleteLater()
