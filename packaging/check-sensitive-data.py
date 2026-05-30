#!/usr/bin/env python3
"""
Pre-release CI gate: scans the repository for sensitive data patterns.

Usage:
    python packaging/check-sensitive-data.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORBIDDEN_PATTERNS = [
    (r'"smb_password"\s*:\s*"[^"]+"', "SMB password in config"),
    (r'"smb_username"\s*:\s*"[^"]+"', "SMB username in config"),
    (r'"smb_host"\s*:\s*"[^"]+"', "SMB host in config"),
    (r'"/home/[a-zA-Z0-9_]+/', "Absolute home path with username"),
    (r'"C:\\\\Users\\\\[a-zA-Z0-9_]+\\\\', "Absolute Windows user path"),
    (r'"smb_host"\s*:\s*"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"', "IP address in SMB host"),
]

SCAN_EXTENSIONS = {".json", ".yaml", ".yml", ".conf", ".ini", ".toml", ".py", ".sh", ".ps1", ".nsi"}
SKIP_PATHS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "Wrekker.AppDir",
    ".claude",
    ".codex",
    ".agents",
}


def scan_repo(root: Path) -> list[tuple[Path, int, str, str]]:
    findings: list[tuple[Path, int, str, str]] = []
    for path in root.rglob("*"):
        if any(skip in path.parts for skip in SKIP_PATHS):
            continue
        if any(part.startswith("target.") for part in path.parts):
            continue
        if path.suffix not in SCAN_EXTENSIONS or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line_num, line in enumerate(text.splitlines(), 1):
            for pattern, description in FORBIDDEN_PATTERNS:
                if re.search(pattern, line):
                    findings.append((path, line_num, description, line.strip()))
    return findings


if __name__ == "__main__":
    root = Path(__file__).parent.parent
    findings = scan_repo(root)
    if findings:
        print("ERROR: Sensitive data detected in repository:")
        for path, line_num, description, line in findings:
            print(f"  {path}:{line_num} - {description}")
            print(f"    {line[:120]}")
        sys.exit(1)
    print("OK: No sensitive data patterns found.")
    sys.exit(0)
