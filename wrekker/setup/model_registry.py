"""
Model registry and first-run dependency detection.

The base application intentionally ships without heavyweight AI assets. This
module knows where Wrekker expects those assets and can download/install missing
components when the setup wizard asks it to.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from wrekker.config.paths import models_dir, python_packages_dir

ProgressCallback = Callable[[str, int, int | None, float], None]
LogCallback = Callable[[str], None]

MODELS: dict[str, dict[str, object]] = {
    "torch_cpu": {
        "label": "PyTorch CPU",
        "pip_packages": ["torch==2.3.0+cpu", "torchaudio==2.3.0+cpu"],
        "index_url": "https://download.pytorch.org/whl/cpu",
        "size_mb": 800,
        "check_import": ["torch", "torchaudio"],
    },
    "htdemucs": {
        "label": "HTDemucs",
        "pip_packages": ["demucs>=4.0.0"],
        "check_import": "demucs",
        "size_mb": 320,
        "note": "Demucs manages HTDemucs weights through its own torch cache.",
    },
    "beat_this": {
        "label": "Beat This!",
        "pip_packages": ["beat-this>=1.1.0"],
        "check_import": "beat_this",
        "size_mb": 85,
        "note": "Beat This! package manages model assets internally where supported.",
    },
}


def ensure_user_python_path() -> Path:
    """Make persistent wizard-installed packages importable in this process."""
    path = python_packages_dir()
    path_s = str(path)
    if path_s not in sys.path:
        sys.path.insert(0, path_s)
    existing = os.environ.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if path_s not in parts:
        os.environ["PYTHONPATH"] = path_s + (os.pathsep + existing if existing else "")
    return path


ensure_user_python_path()


@dataclass(frozen=True)
class ModelInstallResult:
    name: str
    ready: bool
    message: str = ""


def model_path(name: str) -> Path:
    spec = MODELS[name]
    rel = str(spec.get("path") or name)
    return models_dir() / rel


def _check_file_path(name: str) -> Path:
    spec = MODELS[name]
    return model_path(name) / str(spec["check_file"])


def _valid_sha256(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def model_ready(name: str) -> bool:
    spec = MODELS[name]
    check_import = spec.get("check_import")
    if check_import:
        imports = check_import if isinstance(check_import, list) else [check_import]
        if not all(importlib.util.find_spec(mod) is not None for mod in imports):
            return False
        if "check_file" not in spec:
            return True

    path = _check_file_path(name)
    if not path.exists() or path.stat().st_size <= 0:
        return False
    expected = spec.get("sha256")
    if _valid_sha256(expected):
        return _sha256(path).lower() == str(expected).lower()
    return True


def models_ready(required: Iterable[str] | None = None) -> bool:
    names = tuple(required or MODELS.keys())
    return all(model_ready(name) for name in names)


def missing_models(required: Iterable[str] | None = None) -> list[str]:
    names = tuple(required or MODELS.keys())
    return [name for name in names if not model_ready(name)]


def _download_file(
    name: str,
    *,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> ModelInstallResult:
    spec = MODELS[name]
    target = _check_file_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    url = str(spec["url"])
    downloaded = part.stat().st_size if part.exists() else 0
    headers = {}
    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"
        if log:
            log(f"Resuming {name} at {downloaded / 1024 / 1024:.1f} MB")
    request = urllib.request.Request(url, headers=headers)
    start_t = time.monotonic()
    retries = 0
    while True:
        if cancel_event and cancel_event.is_set():
            return ModelInstallResult(name, False, "cancelled")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                total_header = response.headers.get("Content-Length")
                total = int(total_header) + downloaded if total_header else None
                mode = "ab" if downloaded else "wb"
                with part.open(mode) as f:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            return ModelInstallResult(name, False, "cancelled")
                        chunk = response.read(1024 * 512)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(time.monotonic() - start_t, 1e-6)
                        if progress:
                            progress(name, downloaded, total, downloaded / elapsed)
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            retries += 1
            if retries > 5:
                return ModelInstallResult(name, False, str(exc))
            delay = min(30, 2 ** retries)
            if log:
                log(f"{name}: network error, retrying in {delay}s")
            time.sleep(delay)

    part.replace(target)
    expected = spec.get("sha256")
    if _valid_sha256(expected) and _sha256(target).lower() != str(expected).lower():
        target.unlink(missing_ok=True)
        return ModelInstallResult(name, False, "checksum verification failed")
    return ModelInstallResult(name, True, "installed")


def _install_pip_packages(
    name: str,
    *,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> ModelInstallResult:
    spec = MODELS[name]
    packages = [str(p) for p in spec.get("pip_packages", [])]
    if not packages:
        return ModelInstallResult(name, True, "no packages required")
    index_url = str(spec.get("index_url") or "")
    target = ensure_user_python_path()
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--target", str(target), *packages]
    if index_url:
        cmd.extend(["--index-url", index_url])
    if log:
        log(" ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
    )
    start_t = time.monotonic()
    while proc.poll() is None:
        if cancel_event and cancel_event.is_set():
            proc.terminate()
            return ModelInstallResult(name, False, "cancelled")
        line = proc.stdout.readline() if proc.stdout else ""
        if line and log:
            log(line.rstrip())
        if progress:
            # pip does not expose reliable bytes; show an indeterminate-style pulse.
            elapsed = time.monotonic() - start_t
            progress(name, int(elapsed), None, 0.0)
    if proc.stdout:
        for line in proc.stdout:
            if log:
                log(line.rstrip())
    if proc.returncode != 0:
        return ModelInstallResult(name, False, f"pip exited with {proc.returncode}")
    importlib.invalidate_caches()
    return ModelInstallResult(name, model_ready(name), "installed")


def install_model(
    name: str,
    *,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> ModelInstallResult:
    if model_ready(name):
        return ModelInstallResult(name, True, "already installed")
    spec = MODELS[name]
    if spec.get("pip_packages"):
        pip_result = _install_pip_packages(name, progress=progress, log=log, cancel_event=cancel_event)
        if not pip_result.ready and not spec.get("url"):
            return pip_result
        if cancel_event and cancel_event.is_set():
            return pip_result
    if spec.get("url"):
        return _download_file(name, progress=progress, log=log, cancel_event=cancel_event)
    return ModelInstallResult(name, model_ready(name), "installed")


def install_missing(
    *,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> list[ModelInstallResult]:
    results: list[ModelInstallResult] = []
    for name in missing_models():
        result = install_model(name, progress=progress, log=log, cancel_event=cancel_event)
        results.append(result)
        if not result.ready:
            break
    return results
