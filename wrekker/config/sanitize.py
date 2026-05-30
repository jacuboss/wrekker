"""
Config sanitization for public distribution.

This module strips local paths and network credentials from settings before a
config file is committed or bundled into release artifacts.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


SENSITIVE_SETTINGS_KEYS = [
    "smb_host",
    "smb_share",
    "smb_username",
    "smb_password",
    "smb_domain",
    "smb_mount_point",
    "source_mode",
    "wrekked_library_path",
    "wrk_storage_path",
    "fastload_cache_path",
    "temp_stem_cache_path",
    "music_sources",
    "backup_export_location",
]

SETTINGS_SAFE_DEFAULTS = {
    "source_mode": "Local Folders",
    "smb_host": "",
    "smb_share": "",
    "smb_username": "",
    "smb_password": "",
    "smb_domain": "",
    "smb_mount_point": "",
    "wrekked_library_path": "",
    "wrk_storage_path": "",
    "fastload_cache_path": "",
    "temp_stem_cache_path": "",
    "music_sources": [],
    "backup_export_location": "",
}


def _walk_dicts(data: dict[str, Any]):
    yield data
    for value in data.values():
        if isinstance(value, dict):
            yield from _walk_dicts(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from _walk_dicts(item)


def sanitize_settings(settings_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Return a copy of ``settings_dict`` with sensitive fields replaced by safe
    defaults. The function supports both legacy flat settings and Wrekker's
    current nested/profile-based settings document.
    """
    sanitized = deepcopy(settings_dict)
    for obj in _walk_dicts(sanitized):
        for key in SENSITIVE_SETTINGS_KEYS:
            if key in obj:
                obj[key] = deepcopy(SETTINGS_SAFE_DEFAULTS[key])

        # Current SettingsStore names.
        if "source_mode" in obj:
            obj["source_mode"] = "Local Folders"
        if "sources" in obj and isinstance(obj["sources"], list):
            obj["sources"] = []
        for key in (
            "wrekked_root",
            "wrk_storage_root",
            "fastload_cache_root",
            "temp_stem_cache_root",
            "backup_export_root",
            "library_db_path",
            "prepared_db_path",
        ):
            if key in obj:
                obj[key] = ""
    return sanitized


def sanitize_settings_file(input_path: Path, output_path: Path) -> None:
    """Read, sanitize and write a settings JSON file."""
    settings = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("settings JSON root must be an object")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(sanitize_settings(settings), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def assert_no_sensitive_data(settings_dict: dict[str, Any]) -> None:
    """Raise ValueError if sensitive fields contain non-default values."""
    findings: list[str] = []
    for obj in _walk_dicts(settings_dict):
        for key, safe in SETTINGS_SAFE_DEFAULTS.items():
            if key not in obj:
                continue
            value = obj[key]
            if value not in ("", [], None, safe):
                findings.append(key)
        for key in (
            "wrekked_root",
            "wrk_storage_root",
            "fastload_cache_root",
            "temp_stem_cache_root",
            "backup_export_root",
            "library_db_path",
            "prepared_db_path",
        ):
            if key in obj and obj[key] not in ("", [], None):
                findings.append(key)
    if findings:
        unique = ", ".join(sorted(set(findings)))
        raise ValueError(f"Sensitive settings are not sanitized: {unique}")
