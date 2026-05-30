from __future__ import annotations

import json

from wrekker.settings import SETTINGS_SCHEMA_VERSION, SettingsStore


def test_settings_store_creates_default_file(tmp_path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore.load_or_create(path)

    assert path.exists()
    assert store.schema_version == SETTINGS_SCHEMA_VERSION
    assert store.active_profile.name == "Default"
    assert store.get("audio.sample_rate", include_env=False) == 44100
    assert store.get("waveforms.deck_renderer", include_env=False) == "texture"
    assert store.get("waveforms.experimental_qml_enabled", include_env=False) is False


def test_settings_store_round_trip_and_env_precedence(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore.load_or_create(path)
    store.set("preparation.quality", "archive")
    store.save()

    loaded = SettingsStore.load_or_create(path)
    assert loaded.get("preparation.quality", include_env=False) == "archive"

    monkeypatch.setenv("WREKKER_PREPARE_MODE", "balanced")
    assert loaded.get("preparation.quality") == "balanced"
    assert loaded.get("preparation.quality", include_env=False) == "archive"
    assert loaded.runtime_overrides().has("preparation.quality")


def test_settings_store_corrupt_config_falls_back(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not-json", encoding="utf-8")

    store = SettingsStore.load_or_create(path)

    assert store.active_profile_id == "default"
    assert store.load_warnings
    assert store.get("analysis.auto_marker_confidence", include_env=False) == 0.70


def test_settings_store_migrates_legacy_shape(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"settings": {"audio": {"buffer_size": 512}}}), encoding="utf-8")

    store = SettingsStore.load_or_create(path)

    assert store.schema_version == SETTINGS_SCHEMA_VERSION
    assert store.get("audio.buffer_size", include_env=False) == 512
    assert store.get("audio.sample_rate", include_env=False) == 44100


def test_settings_store_reset_section_and_all(tmp_path) -> None:
    store = SettingsStore.load_or_create(tmp_path / "settings.json")
    store.set("audio.buffer_size", 1024)
    store.set("waveforms.deck_renderer", "classic")

    store.reset_section("audio")
    assert store.get("audio.buffer_size", include_env=False) == 256
    assert store.get("waveforms.deck_renderer", include_env=False) == "classic"

    store.reset_all()
    assert store.get("waveforms.deck_renderer", include_env=False) == "texture"


def test_settings_store_profiles_and_profile_export_import(tmp_path) -> None:
    store = SettingsStore.load_or_create(tmp_path / "settings.json")
    live_id = store.create_profile("LIVE - FLX4")
    store.select_profile(live_id)
    store.set("audio.buffer_size", 128)
    duplicate_id = store.duplicate_profile(live_id, "LOW LATENCY TEST")

    assert store.profiles[duplicate_id].settings["audio"]["buffer_size"] == 128
    assert not store.delete_profile("default")
    assert store.delete_profile(duplicate_id)

    profile_path = tmp_path / "profile.json"
    store.export_profile(live_id, profile_path)
    imported_id = store.import_profile(profile_path)
    assert store.profiles[imported_id].settings["audio"]["buffer_size"] == 128


def test_settings_store_full_export_import(tmp_path) -> None:
    store = SettingsStore.load_or_create(tmp_path / "settings.json")
    store.set("storage.fastload_cache_root", str(tmp_path / "cache-a"))
    export_path = tmp_path / "export.json"
    store.export_settings(export_path)

    other = SettingsStore.load_or_create(tmp_path / "other.json")
    result = other.import_settings(export_path)

    assert result.valid
    assert other.get("storage.fastload_cache_root", include_env=False) == str(tmp_path / "cache-a")


def test_settings_validation_rejects_bad_audio_values(tmp_path) -> None:
    store = SettingsStore.load_or_create(tmp_path / "settings.json")
    store.set("audio.sample_rate", 12345)
    store.set("analysis.auto_marker_confidence", 1.5)

    result = store.validate()

    assert not result.valid
    assert any("Sample rate" in err for err in result.errors)
    assert any("confidence" in err for err in result.errors)
