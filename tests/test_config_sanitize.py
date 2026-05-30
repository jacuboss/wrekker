from pathlib import Path

import pytest

from wrekker.config.sanitize import (
    assert_no_sensitive_data,
    sanitize_settings,
    sanitize_settings_file,
)


def test_sanitize_flat_sensitive_settings(tmp_path: Path) -> None:
    host_key = "smb_" + "host"
    user_key = "smb_" + "username"
    pass_key = "smb_" + "password"
    raw = {
        host_key: "server-name",
        user_key: "dj",
        pass_key: "secret",
        "music_sources": ["/music"],
        "source_mode": "SMB",
    }
    sanitized = sanitize_settings(raw)
    assert sanitized["smb_host"] == ""
    assert sanitized["smb_username"] == ""
    assert sanitized["smb_password"] == ""
    assert sanitized["music_sources"] == []
    assert sanitized["source_mode"] == "Local Folders"
    assert_no_sensitive_data(sanitized)


def test_sanitize_nested_profile_settings() -> None:
    raw = {
        "profiles": {
            "default": {
                "settings": {
                    "library": {"sources": [{"path": "/private/music"}]},
                    "storage": {
                        "wrekked_root": "/private/prepared",
                        "fastload_cache_root": "/private/cache",
                    },
                }
            }
        }
    }
    sanitized = sanitize_settings(raw)
    settings = sanitized["profiles"]["default"]["settings"]
    assert settings["library"]["sources"] == []
    assert settings["storage"]["wrekked_root"] == ""
    assert settings["storage"]["fastload_cache_root"] == ""
    assert_no_sensitive_data(sanitized)


def test_assert_no_sensitive_data_rejects_local_values() -> None:
    pass_key = "smb_" + "password"
    with pytest.raises(ValueError):
        assert_no_sensitive_data({pass_key: "secret"})


def test_sanitize_settings_file_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "settings.json"
    dst = tmp_path / "safe.json"
    src.write_text('{"smb_' + 'host": "server", "music_sources": ["/x"]}', encoding="utf-8")
    sanitize_settings_file(src, dst)
    assert '"smb_host": ""' in dst.read_text(encoding="utf-8")
