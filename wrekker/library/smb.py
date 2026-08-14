"""
SMB share helper for Linux.

Strategy
--------
1.  If the share is already mounted somewhere under /run/user or ~/.gvfs,
    return that path directly — no action needed.
2.  Try to mount via `gio mount smb://host/share` (GIO/GVFS, no root needed).
3.  If gio fails, fall back to `mount.cifs` (requires sudo / fstab entry).
4.  Return the local mount path so the rest of the library code just sees a
    normal directory.

The user's SMB credentials are read from ~/.config/wrekker/smb.conf (INI):

    [myserver]
    host     = 192.168.1.10
    share    = music
    username = myuser
    password = secret        ; optional — leave blank for anonymous/guest
    domain   =               ; optional
    mount_point = /mnt/music ; optional — defaults to auto-detect

All credentials are optional; guest / anonymous mounts are supported.
"""

from __future__ import annotations

import configparser
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from wrekker.config.paths import smb_config_file

__all__ = ["SMBShare", "load_smb_config", "save_smb_config"]

_CONFIG_PATH = smb_config_file()

# Paths where GVFS mounts appear
_GVFS_ROOTS = [
    Path(f"/run/user/{os.getuid()}/gvfs"),
    Path.home() / ".gvfs",
]


class SMBMountError(RuntimeError):
    pass


class SMBShare:
    """
    Represents a single SMB share that can be mounted / auto-detected.

    Parameters
    ----------
    host, share:
        SMB server hostname or IP and share name.
    username, password, domain:
        Credentials. All optional.
    mount_point:
        Explicit local mount path. If None, auto-detect from GVFS.
    """

    def __init__(
        self,
        host:        str,
        share:       str,
        username:    str = "",
        password:    str = "",
        domain:      str = "",
        mount_point: Optional[Path] = None,
    ) -> None:
        self.host        = host
        self.share       = share
        self.username    = username
        self.password    = password
        self.domain      = domain
        self.mount_point = mount_point

    @property
    def smb_url(self) -> str:
        if self.username:
            return f"smb://{self.username}@{self.host}/{self.share}"
        return f"smb://{self.host}/{self.share}"

    # ── mount state ───────────────────────────────────────────────────────────

    def local_path(self) -> Optional[Path]:
        """
        Return the local filesystem path where this share is accessible,
        or None if not currently mounted.
        """
        # 1. Explicit mount_point configured
        if self.mount_point and self.mount_point.exists():
            return self.mount_point

        # 2. GVFS auto-detect
        return self._find_gvfs_mount()

    def is_mounted(self) -> bool:
        return self.local_path() is not None

    def _find_gvfs_mount(self) -> Optional[Path]:
        """Scan GVFS roots for a directory that looks like this share."""
        share_lower = self.share.lower()
        host_lower  = self.host.lower()
        for gvfs_root in _GVFS_ROOTS:
            if not gvfs_root.exists():
                continue
            for entry in gvfs_root.iterdir():
                name = entry.name.lower()
                # GVFS names look like: smb-share:server=192.168.1.10,share=music
                if ("smb" in name and host_lower in name and share_lower in name):
                    return entry
        return None

    # ── mount ─────────────────────────────────────────────────────────────────

    def mount(self, timeout: float = 15.0) -> Path:
        """
        Ensure the share is mounted and return its local path.
        Tries gio first, then mount.cifs.
        Raises SMBMountError if neither works.
        """
        existing = self.local_path()
        if existing:
            return existing

        # Try gio mount (no root required, uses GVFS)
        try:
            return self._mount_gio(timeout)
        except SMBMountError:
            pass

        # Fall back to mount.cifs
        if self.mount_point:
            try:
                return self._mount_cifs()
            except SMBMountError:
                pass

        raise SMBMountError(
            f"Could not mount {self.smb_url}. "
            f"Try mounting manually or check {smb_config_file()}."
        )

    def _mount_gio(self, timeout: float) -> Path:
        url = self.smb_url
        env = os.environ.copy()

        # Supply password via stdin if gio expects it
        cmd = ["gio", "mount", url]
        try:
            result = subprocess.run(
                cmd,
                input=(self.password + "\n").encode() if self.password else b"\n",
                capture_output=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise SMBMountError("gio not found")
        except subprocess.TimeoutExpired:
            raise SMBMountError("gio mount timed out")

        if result.returncode != 0:
            err = result.stderr.decode(errors="replace").strip()
            raise SMBMountError(f"gio mount failed: {err}")

        # Give GVFS a moment to create the directory
        for _ in range(20):
            path = self._find_gvfs_mount()
            if path:
                return path
            time.sleep(0.5)

        raise SMBMountError("gio mounted but GVFS path not found")

    def _mount_cifs(self) -> Path:
        if not self.mount_point:
            raise SMBMountError("mount_point required for mount.cifs")
        self.mount_point.mkdir(parents=True, exist_ok=True)
        options = ["vers=3.0"]
        # Credentials go through a 0600 temp file, never the command line —
        # arguments are world-readable via /proc/<pid>/cmdline.
        cred_path: Optional[Path] = None
        if self.username or self.password or self.domain:
            import tempfile
            fd, name = tempfile.mkstemp(prefix="wrekker-smb-", suffix=".cred")
            cred_path = Path(name)
            lines = []
            if self.username:
                lines.append(f"username={self.username}")
            if self.password:
                lines.append(f"password={self.password}")
            if self.domain:
                lines.append(f"domain={self.domain}")
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            options.append(f"credentials={cred_path}")
        cmd = [
            "sudo", "mount", "-t", "cifs",
            f"//{self.host}/{self.share}",
            str(self.mount_point),
            "-o", ",".join(options),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True)
        finally:
            if cred_path is not None:
                try:
                    cred_path.unlink(missing_ok=True)
                except OSError:
                    pass
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace").strip()
            raise SMBMountError(f"mount.cifs failed: {err}")
        return self.mount_point

    def unmount(self) -> None:
        """Unmount via gio (best-effort)."""
        subprocess.run(["gio", "mount", "--unmount", self.smb_url],
                       capture_output=True)


# ── config loader ─────────────────────────────────────────────────────────────

def load_smb_config(config_path: Path = _CONFIG_PATH) -> dict[str, SMBShare]:
    """
    Parse ~/.config/wrekker/smb.conf and return a dict of name→SMBShare.

    Returns an empty dict if the file doesn't exist.
    """
    if not config_path.exists():
        return {}

    try:
        os.chmod(config_path, 0o600)   # tighten legacy files that predate this
    except OSError:
        pass

    cp = configparser.ConfigParser()
    cp.read(config_path)

    shares: dict[str, SMBShare] = {}
    for section in cp.sections():
        host  = cp.get(section, "host",  fallback="")
        share = cp.get(section, "share", fallback="")
        if not host or not share:
            continue
        mp_raw = cp.get(section, "mount_point", fallback="")
        shares[section] = SMBShare(
            host        = host,
            share       = share,
            username    = cp.get(section, "username", fallback=""),
            password    = cp.get(section, "password", fallback=""),
            domain      = cp.get(section, "domain",   fallback=""),
            mount_point = Path(mp_raw).expanduser() if mp_raw else None,
        )
    return shares


def save_smb_config(
    host:        str,
    share:       str,
    username:    str  = "",
    password:    str  = "",
    domain:      str  = "",
    mount_point: str  = "",
    section:     str  = "default",
    config_path: Path = _CONFIG_PATH,
) -> None:
    """Write (or update) a single SMB share entry in smb.conf."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cp = configparser.ConfigParser()
    if config_path.exists():
        cp.read(config_path)
    if section not in cp:
        cp[section] = {}
    cp[section]["host"]  = host
    cp[section]["share"] = share
    for key, val in (("username", username), ("password", password),
                     ("domain", domain), ("mount_point", mount_point)):
        if val:
            cp[section][key] = val
        elif key in cp[section]:
            del cp[section][key]
    with open(config_path, "w") as fh:
        cp.write(fh)
    os.chmod(config_path, 0o600)   # contains credentials


def write_smb_config_example(config_path: Path = _CONFIG_PATH) -> None:
    """Write an example smb.conf if none exists."""
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("""\
# Wrekker SMB library configuration
# Each [section] is one share.
#
# [myserver]
# host        = 192.168.1.10
# share       = music
# username    = myuser
# password    = secret
# domain      =
# mount_point = /mnt/music   ; leave blank to use GVFS auto-mount
""")
    os.chmod(config_path, 0o600)
