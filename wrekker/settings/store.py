"""Central persistent settings store for Wrekker.

Settings are user-facing and profile-based. Environment variables remain
developer/runtime overrides and are never written back to the settings file.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from wrekker.config.paths import cache_dir, data_dir, default_settings_file, settings_file

SETTINGS_SCHEMA_VERSION = 1
SettingsSchemaVersion = int
DEFAULT_SETTINGS_PATH = settings_file()


@dataclass
class SettingsValidationResult:
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.valid = False
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)


@dataclass
class RuntimeOverrides:
    values: dict[str, str] = field(default_factory=dict)

    def has(self, setting_path: str) -> bool:
        return setting_path in self.values

    def label_for(self, setting_path: str) -> str:
        env_name = self.values.get(setting_path)
        if not env_name:
            return ""
        return f"Overridden by {env_name} for this session."


@dataclass
class SettingsProfile:
    id: str
    name: str
    protected: bool
    settings: dict[str, Any]


ENV_SETTING_MAP: dict[str, str] = {
    "preparation.quality": "WREKKER_PREPARE_MODE",
    "preparation.flac_compression_level": "WREKKER_WRK_FLAC_COMPRESSION_LEVEL",
    "preparation.audio_encode_threads": "WREKKER_WRK_AUDIO_ENCODE_THREADS",
    "preparation.cpu_workers": "WREKKER_PREPARE_CPU_WORKERS",
    "analysis.gpu_policy": "WREKKER_PREPARE_GPU_POLICY",
    "analysis.beat_device": "WREKKER_BEAT_DEVICE",
    "analysis.beat_checkpoint": "WREKKER_BEAT_CHECKPOINT",
    "analysis.beat_use_dbn": "WREKKER_BEAT_USE_DBN",
    "analysis.keep_prepare_temp": "WREKKER_KEEP_PREP_TEMP",
    "storage.fastload_cache_root": "WREKKER_FASTLOAD_CACHE",
    "storage.temp_stem_cache_root": "WREKKER_STEM_CACHE_PATH",
    "waveforms.deck_renderer": "WREKKER_ZOOM_RENDERER",
    "waveforms.texture_zoom_cache_scale": "WREKKER_TEXTURE_ZOOM_CACHE_SCALE",
    "waveforms.texture_zoom_peak_smooth": "WREKKER_TEXTURE_ZOOM_PEAK_SMOOTH",
    "waveforms.classic_zoom_cache_scale": "WREKKER_ZOOM_CACHE_SCALE",
    "waveforms.classic_zoom_peak_smooth": "WREKKER_ZOOM_PEAK_SMOOTH",
    "waveforms.experimental_qml_requested": "WREKKER_ENABLE_QML_DECK_WAVEFORMS",
    "waveforms.experimental_qml_enabled": "WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS",
    "lab.force_widget_timeline": "WREKKER_LAB_FORCE_WIDGET_TIMELINE",
    "lab.waveform_renderer": "WREKKER_LAB_WAVEFORM_RENDERER",
    "lab.texture_cache_scale": "WREKKER_LAB_TEXTURE_CACHE_SCALE",
    "lab.texture_px_per_second": "WREKKER_LAB_TEXTURE_PX_PER_SECOND",
    "lab.texture_peak_smooth": "WREKKER_LAB_TEXTURE_PEAK_SMOOTH",
    "diagnostics.ui_tick_log": "WREKKER_UI_TICK_LOG",
    "diagnostics.ui_tick_profile": "WREKKER_UI_TICK_PROFILE",
    "diagnostics.zoom_fps_log": "WREKKER_ZOOM_FPS_LOG",
    "diagnostics.waveform_position_debug": "WREKKER_WAVEFORM_POSITION_DEBUG",
    "diagnostics.waveform_renderer_debug": "WREKKER_WAVEFORM_RENDER_DEBUG",
    "diagnostics.ui_platform_log": "WREKKER_UI_PLATFORM_LOG",
    "diagnostics.disable_cross_overlay": "WREKKER_DISABLE_CROSS_OVERLAY",
    "diagnostics.disable_deck_realtime": "WREKKER_DISABLE_DECK_REALTIME",
}


def _config_home() -> Path:
    return DEFAULT_SETTINGS_PATH.parent


def _data_home() -> Path:
    return data_dir()


def _cache_home() -> Path:
    return cache_dir()


def default_profile_settings() -> dict[str, Any]:
    data = _data_home()
    cache = _cache_home()
    return {
        "audio": {
            "engine_enabled": True,
            "backend": "CPAL default",
            "main_output_device": "System Default",
            "sample_rate": 44100,
            "buffer_size": 256,
            "latency_preset": "Balanced",
            "master_left_channel": 0,
            "master_right_channel": 1,
            "cue_enabled": True,
            "cue_device": "Auto multichannel / FLX4",
            "cue_channel_pair": "2/3",
            "headphone_mix": 0.0,
            "headphone_level": 1.0,
            "mst_cue_on_startup": False,
            "restore_cue_routing": True,
            "auto_reconnect": True,
            "warn_before_output_change": True,
            "fallback_policy": "Ask User",
        },
        "controller": {
            "enabled": True,
            "profile": "DDJ-FLX4",
            "midi_input_port": "Auto",
            "midi_output_port": "Auto",
            "auto_detect_flx4": True,
            "reconnect_automatically": True,
            "led_feedback": True,
            "vu_feedback": True,
            "vinyl_mode": True,
            "scratch_sensitivity": 1.0,
            "nudge_sensitivity": 1.0,
            "jog_response": "Natural",
        },
        "library": {
            "source_mode": "Local Folders",
            "sources": [],
        },
        "storage": {
            "library_db_path": str(data / "library.db"),
            "prepared_db_path": str(data / "prepared.db"),
            "wrekked_root": str(data / "prepared"),
            "wrk_storage_root": str(data / "prepared" / "tracks"),
            "fastload_cache_root": str(cache / "fastload"),
            "temp_stem_cache_root": str(Path("/tmp") / "wrekker" / "stems"),
            "backup_export_root": "",
            "fastload_mode": "Mix Only",
            "fastload_format": "PCM16",
            "cache_stems_in_fastload": False,
            "build_fastload_after_prepare": True,
            "disk_warning_gb": 10,
            "cache_cleanup_policy": "Manual only",
        },
        "preparation": {
            "quality": "fast",
            "flac_compression_level": None,
            "audio_encode_threads": 2,
            "cpu_workers": 4,
        },
        "analysis": {
            "stem_device": "Auto",
            "beat_device": "cpu",
            "gpu_policy": "beat_cpu",
            "beat_checkpoint": "final0",
            "beat_use_dbn": False,
            "keep_prepare_temp": False,
            "beatgrid": True,
            "downbeats": True,
            "phrases": True,
            "key": True,
            "lufs": True,
            "stems": True,
            "stem_energy": True,
            "auto_markers": True,
            "primary_markers_enabled": True,
            "wrekk_markers_enabled": True,
            "auto_marker_confidence": 0.70,
            "marker_display_mode": "PRIMARY + WREKK",
            "wrekk_visibility": "OPPORTUNITIES ONLY",
            "wrekk_structural_lab_threshold": 0.65,
            "wrekk_structural_live_threshold": 0.80,
            "wrekk_opportunity_live_threshold": 0.88,
            "marker_grouping_enabled": True,
            "marker_cooldown_policy": "Balanced",
        },
        "playback": {
            "default_master_gain": 1.0,
            "reset_channel_fader_on_load": False,
            "reset_eq_on_load": False,
            "reset_filter_on_load": False,
            "reset_stems_on_load": True,
            "reset_fx_on_load": True,
            "preserve_manual_stem_balance": False,
            "crossfader_curve": "Equal Power",
            "tempo_range": "±10%",
        },
        "sync": {
            "default_mode": "BPM + Phase + Phrase",
            "phase_align_on_resume": True,
            "master_selection": "Manual Master Selection",
            "pitch_move_on_follower": "Disable Sync",
            "quantize_enabled": False,
            "quantize_resolution": "1 beat",
            "quantize_hot_cues": False,
            "quantize_loops": False,
        },
        "stems": {
            "reset_gains_on_load": True,
            "preserve_balance_between_tracks": False,
            "default_gains": {"vocals": 1.0, "drums": 1.0, "bass": 1.0, "other": 1.0},
            "meters_visible": True,
            "status_visibility": "Detailed",
            "horizon_enabled": True,
            "horizon_display_mode": "LED Blocks",
            "horizon_range_bars": 8,
            "horizon_show_countdown": True,
            "horizon_show_w_flag": True,
            "horizon_dominance_levels": "Three Levels",
            "horizon_display_when": "Always for prepared .wrk tracks",
            "horizon_detail": "Balanced",
            "horizon_use_stem_colors": True,
            "horizon_intensity": 1.0,
            "horizon_dim_inactive": True,
            "horizon_highlight_upcoming_change": True,
        },
        "wrekk": {
            "mode_on_startup": False,
            "macro_curve": "Performance",
            "reset_macro_on_load": True,
            "default_fx_bank": "NORMAL",
            "default_wrekk_fx_target": "Last Used",
            "remember_last_wrekk_fx": True,
            "remember_wrekk_fx_parameters": True,
        },
        "normal_fx": {
            "startup_fx_bank": "NORMAL",
            "default_target": "Master",
            "default_beat_division": "1/2",
            "default_wet": 0.35,
            "default_feedback": 0.30,
            "tail_behavior": "Natural Decay",
            "reset_on_track_load": True,
            "remember_last": True,
            "remember_parameters": True,
        },
        "waveforms": {
            "color_mode": "Spectral",
            "zoom_detail": "Balanced",
            "visual_performance": "Balanced",
            "playhead_color": "White / Magenta",
            "show_beat_grid": True,
            "show_downbeats": True,
            "show_phrase_regions": True,
            "auto_marker_display": "ESSENTIAL",
            "cross_deck_overlay": True,
            "stem_overlay_opacity": 0.65,
            "show_oscilloscopes": True,
            "show_spectrum": True,
            "deck_renderer": "texture",
            "texture_zoom_cache_scale": 4,
            "texture_zoom_peak_smooth": 5,
            "classic_zoom_cache_scale": 2,
            "classic_zoom_peak_smooth": 3,
            "experimental_qml_requested": False,
            "experimental_qml_enabled": False,
        },
        "lab": {
            "default_waveform_source": "DRUMS",
            "default_compare_mode": False,
            "default_phrase_length": 16,
            "snap_to_drum_transient": False,
            "regenerate_unlocked_auto_markers": "Ask",
            "require_save_before_manual_verified": True,
            "preview_starts_in": "Full Mix",
            "default_metronome": False,
            "metronome_level": 0.65,
            "accent_downbeats": True,
            "stem_monitoring": "None",
            "restore_preview_state": False,
            "preview_output": "Follow Main Audio Output",
            "preview_buffer_size": 256,
            "force_widget_timeline": False,
            "waveform_renderer": "texture",
            "texture_cache_scale": 4,
            "texture_px_per_second": 256,
            "texture_peak_smooth": 0,
        },
        "advanced": {
            "show_runtime_overrides": True,
        },
        "diagnostics": {
            "ui_tick_log": False,
            "ui_tick_profile": False,
            "zoom_fps_log": False,
            "waveform_position_debug": False,
            "waveform_renderer_debug": False,
            "ui_platform_log": False,
            "disable_cross_overlay": False,
            "disable_deck_realtime": False,
        },
    }


def _deep_merge(defaults: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(defaults)
    for key, value in (data or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _get_path(data: dict[str, Any], setting_path: str) -> Any:
    cur: Any = data
    for part in setting_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(data: dict[str, Any], setting_path: str, value: Any) -> None:
    cur = data
    parts = setting_path.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _env_value_for(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    return str(value)


class SettingsStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
        self.schema_version: SettingsSchemaVersion = SETTINGS_SCHEMA_VERSION
        self.active_profile_id = "default"
        self.startup_profile_id = "default"
        self.profiles: dict[str, SettingsProfile] = {}
        self.load_warnings: list[str] = []

    @classmethod
    def load_or_create(cls, path: Path | str | None = None) -> "SettingsStore":
        store = cls(path)
        if not store.path.exists():
            if path is None and default_settings_file().exists():
                store.path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(default_settings_file(), store.path)
                store.load()
                return store
            store._set_default_document()
            store.save()
            return store
        store.load()
        return store

    def _set_default_document(self) -> None:
        self.schema_version = SETTINGS_SCHEMA_VERSION
        self.active_profile_id = "default"
        self.startup_profile_id = "default"
        self.profiles = {
            "default": SettingsProfile(
                id="default",
                name="Default",
                protected=True,
                settings=default_profile_settings(),
            )
        }

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings root is not an object")
        except Exception as exc:
            self.load_warnings.append(f"Malformed settings file ignored: {exc}")
            self._set_default_document()
            return
        migrated = self._migrate(raw)
        self.schema_version = int(migrated.get("schema_version", SETTINGS_SCHEMA_VERSION) or SETTINGS_SCHEMA_VERSION)
        self.active_profile_id = str(migrated.get("active_profile_id") or "default")
        self.startup_profile_id = str(migrated.get("startup_profile_id") or self.active_profile_id)
        defaults = default_profile_settings()
        profiles_raw = migrated.get("profiles") or {}
        self.profiles = {}
        if isinstance(profiles_raw, dict):
            for pid, pdata in profiles_raw.items():
                if not isinstance(pdata, dict):
                    continue
                settings = _deep_merge(defaults, pdata.get("settings") or {})
                self.profiles[str(pid)] = SettingsProfile(
                    id=str(pid),
                    name=str(pdata.get("name") or pid),
                    protected=bool(pdata.get("protected", False)),
                    settings=settings,
                )
        if "default" not in self.profiles:
            self.profiles["default"] = SettingsProfile("default", "Default", True, defaults)
        if self.active_profile_id not in self.profiles:
            self.active_profile_id = "default"
        if self.startup_profile_id not in self.profiles:
            self.startup_profile_id = self.active_profile_id

    def _migrate(self, raw: dict[str, Any]) -> dict[str, Any]:
        version = int(raw.get("schema_version", 0) or 0)
        if version > SETTINGS_SCHEMA_VERSION:
            self.load_warnings.append("Settings file was written by a newer Wrekker; unknown fields were preserved where possible.")
        if version <= 0:
            return {
                "schema_version": SETTINGS_SCHEMA_VERSION,
                "active_profile_id": "default",
                "startup_profile_id": "default",
                "profiles": {
                    "default": {
                        "name": "Default",
                        "protected": True,
                        "settings": _deep_merge(default_profile_settings(), raw.get("settings") or {}),
                    }
                },
            }
        raw["schema_version"] = SETTINGS_SCHEMA_VERSION
        return raw

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "active_profile_id": self.active_profile_id,
            "startup_profile_id": self.startup_profile_id,
            "profiles": {
                pid: {
                    "name": p.name,
                    "protected": p.protected,
                    "settings": p.settings,
                }
                for pid, p in self.profiles.items()
            },
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".settings.", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass

    @property
    def active_profile(self) -> SettingsProfile:
        return self.profiles[self.active_profile_id]

    def active_settings(self) -> dict[str, Any]:
        return self.active_profile.settings

    def get(self, setting_path: str, default: Any = None, *, include_env: bool = True) -> Any:
        if include_env:
            env_name = ENV_SETTING_MAP.get(setting_path)
            if env_name and env_name in os.environ:
                return self._coerce_env(setting_path, os.environ[env_name])
        value = _get_path(self.active_settings(), setting_path)
        return default if value is None else value

    def set(self, setting_path: str, value: Any) -> None:
        _set_path(self.active_settings(), setting_path, value)

    def runtime_overrides(self) -> RuntimeOverrides:
        return RuntimeOverrides({
            path: env_name
            for path, env_name in ENV_SETTING_MAP.items()
            if env_name in os.environ
        })

    def apply_environment_defaults(self) -> None:
        env_values = {
            "preparation.quality": "WREKKER_PREPARE_MODE",
            "preparation.flac_compression_level": "WREKKER_WRK_FLAC_COMPRESSION_LEVEL",
            "preparation.audio_encode_threads": "WREKKER_WRK_AUDIO_ENCODE_THREADS",
            "preparation.cpu_workers": "WREKKER_PREPARE_CPU_WORKERS",
            "analysis.gpu_policy": "WREKKER_PREPARE_GPU_POLICY",
            "analysis.beat_device": "WREKKER_BEAT_DEVICE",
            "analysis.beat_checkpoint": "WREKKER_BEAT_CHECKPOINT",
            "analysis.beat_use_dbn": "WREKKER_BEAT_USE_DBN",
            "analysis.keep_prepare_temp": "WREKKER_KEEP_PREP_TEMP",
            "storage.fastload_cache_root": "WREKKER_FASTLOAD_CACHE",
            "storage.temp_stem_cache_root": "WREKKER_STEM_CACHE_PATH",
            "waveforms.deck_renderer": "WREKKER_ZOOM_RENDERER",
            "waveforms.texture_zoom_cache_scale": "WREKKER_TEXTURE_ZOOM_CACHE_SCALE",
            "waveforms.texture_zoom_peak_smooth": "WREKKER_TEXTURE_ZOOM_PEAK_SMOOTH",
            "lab.force_widget_timeline": "WREKKER_LAB_FORCE_WIDGET_TIMELINE",
            "lab.waveform_renderer": "WREKKER_LAB_WAVEFORM_RENDERER",
            "lab.texture_cache_scale": "WREKKER_LAB_TEXTURE_CACHE_SCALE",
            "lab.texture_px_per_second": "WREKKER_LAB_TEXTURE_PX_PER_SECOND",
            "lab.texture_peak_smooth": "WREKKER_LAB_TEXTURE_PEAK_SMOOTH",
            "diagnostics.ui_tick_log": "WREKKER_UI_TICK_LOG",
            "diagnostics.ui_tick_profile": "WREKKER_UI_TICK_PROFILE",
            "diagnostics.zoom_fps_log": "WREKKER_ZOOM_FPS_LOG",
            "diagnostics.waveform_position_debug": "WREKKER_WAVEFORM_POSITION_DEBUG",
            "diagnostics.waveform_renderer_debug": "WREKKER_WAVEFORM_RENDER_DEBUG",
            "diagnostics.ui_platform_log": "WREKKER_UI_PLATFORM_LOG",
            "diagnostics.disable_cross_overlay": "WREKKER_DISABLE_CROSS_OVERLAY",
            "diagnostics.disable_deck_realtime": "WREKKER_DISABLE_DECK_REALTIME",
        }
        for setting_path, env_name in env_values.items():
            if env_name in os.environ:
                continue
            value = _get_path(self.active_settings(), setting_path)
            if value is None:
                continue
            os.environ[env_name] = _env_value_for(value)

        # Keep the explicit two-step guard for QML deck waveforms.
        if "WREKKER_ENABLE_QML_DECK_WAVEFORMS" not in os.environ:
            os.environ["WREKKER_ENABLE_QML_DECK_WAVEFORMS"] = "1" if self.get("waveforms.experimental_qml_requested", False, include_env=False) else "0"
        if "WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS" not in os.environ:
            os.environ["WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS"] = "1" if self.get("waveforms.experimental_qml_enabled", False, include_env=False) else "0"

    def sync_prepared_db_settings(self, prepared_db) -> None:
        if prepared_db is None:
            return
        mappings = {
            "storage.wrekked_root": "wrekked_library_path",
            "storage.wrk_storage_root": "wrk_storage_path",
            "storage.fastload_cache_root": "fastload_cache_path",
            "storage.temp_stem_cache_root": "temporary_stem_cache_path",
            "storage.backup_export_root": "backup_export_path",
            "storage.fastload_mode": "fastload_mode",
            "storage.fastload_format": "fastload_format",
            "library.source_mode": "source_mode",
        }
        for setting_path, db_key in mappings.items():
            value = self.get(setting_path, include_env=False)
            if value not in (None, ""):
                try:
                    prepared_db.set_setting(db_key, str(value))
                except Exception:
                    pass

    def validate(self, profile_id: str | None = None) -> SettingsValidationResult:
        result = SettingsValidationResult()
        profile = self.profiles.get(profile_id or self.active_profile_id)
        if profile is None:
            result.add_error("Active profile does not exist.")
            return result
        settings = profile.settings
        sample_rate = int(_get_path(settings, "audio.sample_rate") or 0)
        if sample_rate not in {44100, 48000, 96000}:
            result.add_error("Sample rate must be 44100, 48000 or 96000 Hz.")
        buffer_size = int(_get_path(settings, "audio.buffer_size") or 0)
        if buffer_size not in {64, 128, 256, 512, 1024}:
            result.add_error("Buffer size must be 64, 128, 256, 512 or 1024 frames.")
        confidence = float(_get_path(settings, "analysis.auto_marker_confidence") or 0.0)
        if not 0.0 <= confidence <= 1.0:
            result.add_error("Auto marker confidence must be between 0% and 100%.")
        for key in (
            "analysis.wrekk_structural_lab_threshold",
            "analysis.wrekk_structural_live_threshold",
            "analysis.wrekk_opportunity_live_threshold",
        ):
            value = float(_get_path(settings, key) or 0.0)
            if not 0.0 <= value <= 1.0:
                result.add_error(f"{key} must be between 0% and 100%.")
        horizon_mode = str(_get_path(settings, "stems.horizon_display_mode") or "")
        if horizon_mode not in {"Off", "LED Blocks", "Future Bars", "Stem Waveforms"}:
            result.add_error("Stem Horizon display mode is invalid.")
        horizon_range = int(_get_path(settings, "stems.horizon_range_bars") or 0)
        if horizon_range not in {4, 8, 16, 32}:
            result.add_error("Stem Horizon range must be 4, 8, 16 or 32 bars.")
        renderer = str(_get_path(settings, "waveforms.deck_renderer") or "")
        if renderer not in {"texture", "classic", "legacy", "qml"}:
            result.add_error("Deck waveform renderer is invalid.")
        if renderer == "qml" and not bool(_get_path(settings, "waveforms.experimental_qml_enabled")):
            result.add_warning("Experimental QML deck waveforms require explicit opt-in.")
        cue_pair = str(_get_path(settings, "audio.cue_channel_pair") or "2/3")
        if cue_pair not in {"2/3"}:
            result.add_warning("Only single-device multichannel CUE on channels 2/3 is implemented.")
        return result

    def reset_section(self, section: str) -> None:
        defaults = default_profile_settings()
        if section in defaults:
            self.active_settings()[section] = deepcopy(defaults[section])

    def reset_all(self) -> None:
        protected = self.active_profile.protected
        name = self.active_profile.name
        self.active_profile.settings = default_profile_settings()
        self.active_profile.protected = protected
        self.active_profile.name = name

    def create_profile(self, name: str) -> str:
        base = self._slug(name) or "profile"
        pid = base
        n = 2
        while pid in self.profiles:
            pid = f"{base}-{n}"
            n += 1
        self.profiles[pid] = SettingsProfile(pid, name.strip() or "Profile", False, default_profile_settings())
        return pid

    def duplicate_profile(self, source_id: str, name: str | None = None) -> str:
        src = self.profiles[source_id]
        pid = self.create_profile(name or f"{src.name} Copy")
        self.profiles[pid].settings = deepcopy(src.settings)
        return pid

    def rename_profile(self, profile_id: str, name: str) -> None:
        if profile_id in self.profiles:
            self.profiles[profile_id].name = name.strip() or self.profiles[profile_id].name

    def delete_profile(self, profile_id: str) -> bool:
        profile = self.profiles.get(profile_id)
        if profile is None or profile.protected:
            return False
        del self.profiles[profile_id]
        if self.active_profile_id == profile_id:
            self.active_profile_id = "default"
        if self.startup_profile_id == profile_id:
            self.startup_profile_id = self.active_profile_id
        return True

    def select_profile(self, profile_id: str) -> None:
        if profile_id not in self.profiles:
            raise KeyError(profile_id)
        self.active_profile_id = profile_id

    def set_startup_profile(self, profile_id: str) -> None:
        if profile_id not in self.profiles:
            raise KeyError(profile_id)
        self.startup_profile_id = profile_id

    def export_settings(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def import_settings(self, path: Path | str) -> SettingsValidationResult:
        other = SettingsStore(path)
        other.load()
        validation = other.validate()
        if validation.valid:
            self.schema_version = other.schema_version
            self.active_profile_id = other.active_profile_id
            self.startup_profile_id = other.startup_profile_id
            self.profiles = other.profiles
        return validation

    def export_profile(self, profile_id: str, path: Path | str) -> None:
        profile = self.profiles[profile_id]
        payload = {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "profile": {
                "name": profile.name,
                "settings": profile.settings,
            },
        }
        Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def import_profile(self, path: Path | str) -> str:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        pdata = raw.get("profile") if isinstance(raw, dict) else None
        if not isinstance(pdata, dict):
            raise ValueError("profile export is invalid")
        pid = self.create_profile(str(pdata.get("name") or "Imported Profile"))
        self.profiles[pid].settings = _deep_merge(default_profile_settings(), pdata.get("settings") or {})
        return pid

    @staticmethod
    def _slug(name: str) -> str:
        out = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
        while "--" in out:
            out = out.replace("--", "-")
        return out.strip("-")

    @staticmethod
    def _coerce_env(setting_path: str, value: str) -> Any:
        default = _get_path(default_profile_settings(), setting_path)
        if isinstance(default, bool):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(value)
            except ValueError:
                return default
        if isinstance(default, float):
            try:
                return float(value)
            except ValueError:
                return default
        return value

    def backup(self) -> Path | None:
        if not self.path.exists():
            return None
        dst = self.path.with_suffix(".json.bak")
        shutil.copy2(self.path, dst)
        return dst
