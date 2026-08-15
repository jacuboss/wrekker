"""
Centralized cross-platform path resolution.

Runtime code should use these helpers instead of hardcoding Linux-only
``~/.config/wrekker`` or ``~/.local/share/wrekker`` paths.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Resolution must stay side-effect safe in read-only sandboxes and
        # partially configured systems. Writers still surface their own errors.
        pass
    return path


def config_dir() -> Path:
    if sys.platform == "win32":
        p = Path.home() / "AppData" / "Roaming" / "Wrekker"
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wrekker"
    return _ensure_dir(p)


def data_dir() -> Path:
    if sys.platform == "win32":
        p = Path.home() / "AppData" / "Local" / "Wrekker"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "wrekker"
    return _ensure_dir(p)


def cache_dir() -> Path:
    if sys.platform == "win32":
        p = Path.home() / "AppData" / "Local" / "Wrekker" / "Cache"
    else:
        p = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "wrekker"
    return _ensure_dir(p)


def models_dir() -> Path:
    override = os.environ.get("WREKKER_MODELS_PATH")
    p = Path(override).expanduser() if override else data_dir() / "models"
    return _ensure_dir(p)


def python_packages_dir() -> Path:
    """Persistent user site used by the AppImage first-time setup wizard.

    Scoped per interpreter version (cp311, cp313, …): compiled wheels installed
    by one Python must never be importable from another — an unversioned dir
    shadowed e.g. a conda numpy with the AppImage's cp311 build and broke it.
    """
    override = os.environ.get("WREKKER_PYTHON_PACKAGES_PATH")
    if override:
        return _ensure_dir(Path(override).expanduser())
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    versioned = data_dir() / "python" / tag / "site-packages"
    if not versioned.exists():
        # The legacy unversioned dir was only ever populated by the py3.11
        # AppImage; keep using it there so AI components aren't re-downloaded.
        legacy = data_dir() / "python" / "site-packages"
        if sys.version_info[:2] == (3, 11) and legacy.is_dir():
            try:
                if any(legacy.iterdir()):
                    return legacy
            except OSError:
                pass
    return _ensure_dir(versioned)


def library_dir() -> Path:
    p = data_dir() / "library"
    return _ensure_dir(p)


def fastload_dir() -> Path:
    # Must match the SettingsStore default (storage.fastload_cache_root) and
    # the documented location; a data-dir default here silently created a
    # second cache tree whenever the env override was not applied.
    p = cache_dir() / "fastload"
    if not p.exists():
        legacy = data_dir() / "fastload"
        if legacy.is_dir() and any(legacy.iterdir()):
            return legacy   # keep using an existing populated legacy cache
    return _ensure_dir(p)


def settings_file() -> Path:
    return config_dir() / "settings.json"


def smb_config_file() -> Path:
    """Contains credentials. Never commit. Excluded via .gitignore."""
    return config_dir() / "smb.conf"


def default_settings_file() -> Path:
    """Bundled sanitized template copied to user config on first launch."""
    return Path(__file__).parent / "default_settings.json"
