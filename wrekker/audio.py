"""
Robust audio loading for all formats DJs actually use.

Backend priority:
  1. soundfile  — fast; handles WAV, native FLAC, AIFF, OGG
  2. ffmpeg     — universal fallback; handles MP3, AAC/M4A, MP4, OPUS, etc.
     Decodes to WAV in memory → soundfile reads the buffer (no temp files).

Returns: (audio: float32 ndarray of shape (samples, channels), sample_rate: int)
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import numpy as np

__all__ = ["load_audio"]


def load_audio(path: Path | str) -> tuple[np.ndarray, int]:
    """
    Load any common audio format to float32 (samples, channels).
    Raises RuntimeError if all backends fail.
    """
    path = Path(path)
    exc_chain: list[str] = []

    # ── soundfile: WAV, native FLAC, AIFF, OGG ───────────────────────────
    try:
        import soundfile as sf
        audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return audio, sr
    except Exception as e:
        exc_chain.append(f"soundfile: {e}")

    # ── ffmpeg → WAV buffer → soundfile: MP3, AAC, M4A, MP4, OPUS, … ────
    try:
        import soundfile as sf
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(path),
                "-f", "wav",          # output format: WAV
                "-acodec", "pcm_f32le",  # float32 PCM
                "pipe:1",             # write to stdout
            ],
            capture_output=True,
            check=True,
        )
        audio, sr = sf.read(io.BytesIO(proc.stdout), dtype="float32", always_2d=True)
        return audio, sr
    except Exception as e:
        exc_chain.append(f"ffmpeg: {e}")

    detail = "  |  ".join(exc_chain)
    raise RuntimeError(f"Cannot load audio file '{path}': {detail}")
