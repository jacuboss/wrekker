"""
Fastload cache — local decoded representation of a .wrk for fast reloads.

On first load the .wrk is decoded normally; the result is written to a flat
binary cache in the background.  On subsequent loads the cache is used
directly, skipping ZIP decompression and FLAC decoding entirely.

Cache location: ~/.cache/wrekker/fastload/{cache_key}/

Audio formats
-------------
pcm16  (default)  — int16 interleaved, ~10.6 MB/min at 44.1 kHz stereo
f32               — float32 interleaved, ~21 MB/min at 44.1 kHz stereo

PCM16 halves the cache footprint with negligible quality difference
(96 dB dynamic range) and numpy int16→float32 conversion is ~30-50 ms for
a 5-minute track — much faster than FLAC decoding (seconds).

Directory structure
-------------------
    manifest.json       copy of the .wrk manifest
    mix.pcm16 | mix.f32
    mix.meta.json       {sr, n_frames, n_channels, audio_format}
    stems.meta.json     {sr, n_frames, n_channels, audio_format}  (optional)
    vocals.pcm16 | vocals.f32                                      (optional)
    drums.pcm16  | drums.f32                                       (optional)
    bass.pcm16   | bass.f32                                        (optional)
    other.pcm16  | other.f32                                       (optional)
    waveform_peaks.f32
    waveform_colors.bin
    stem_energy.f32
    beatgrid.json
    cues.json
    loops.json
    artwork.jpg | artwork.png
    ready.flag          written last — JSON validation header

ready.flag is the atomic completion marker.  A cache directory without a
valid ready.flag is treated as incomplete and ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

from wrekker.config.paths import fastload_dir
from wrekker.core.deck import MARKER_MIN_CONFIDENCE

if TYPE_CHECKING:
    from wrekker.formats.wrk import WrkMetadata

__all__ = ["FastloadCache", "FastloadSettings", "FORMAT_PCM16", "FORMAT_F32"]


def _filter_saved_markers(markers: Optional[list]) -> list:
    return [
        m for m in (markers or [])
        if float(m.get("confidence", 0.0) or 0.0) >= MARKER_MIN_CONFIDENCE
    ]

FASTLOAD_VERSION = 2      # bumped: PCM16 default; invalidates v1 f32 caches
STEM_NAMES       = ("vocals", "drums", "bass", "other")

FORMAT_PCM16 = "pcm16"
FORMAT_F32   = "f32"

_DEFAULT_CACHE_ROOT = fastload_dir()

# Scale factor for PCM16 encoding/decoding
_PCM16_SCALE = 32767.0


# ── Settings ───────────────────────────────────────────────────────────────────

@dataclass
class FastloadSettings:
    """
    Controls what gets cached and at what quality.

    Defaults are tuned for performance-first DJing without ballooning disk use.
    """
    enabled:       bool = True
    audio_format:  str  = FORMAT_PCM16   # "pcm16" | "f32"
    cache_stems:   bool = False          # stems cache is optional / large
    cache_root:    Optional[Path] = None # None → _DEFAULT_CACHE_ROOT

    def effective_root(self) -> Path:
        env_root = os.environ.get("WREKKER_FASTLOAD_CACHE")
        if self.cache_root:
            return Path(self.cache_root)
        if env_root:
            return Path(env_root).expanduser()
        return _DEFAULT_CACHE_ROOT


_DEFAULT_SETTINGS = FastloadSettings()


# ── PCM16 helpers ──────────────────────────────────────────────────────────────

def _to_pcm16(audio: np.ndarray) -> np.ndarray:
    """
    Encode float32 audio to int16.
    audio: (n_frames, n_channels) float32, range [-1, 1].
    Returns: (n_frames, n_channels) int16.
    """
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * _PCM16_SCALE).astype(np.int16)


def _from_pcm16(raw: np.ndarray, n_frames: int, n_channels: int) -> np.ndarray:
    """
    Decode int16 samples to float32.
    Returns: (n_frames, n_channels) float32.
    """
    return raw.reshape(n_frames, n_channels).astype(np.float32) / _PCM16_SCALE


# ── Cache class ────────────────────────────────────────────────────────────────

class FastloadCache:
    """
    Manages the fastload cache for .wrk files.

    Thread-safe for read operations; build() must be called from a single
    background thread per .wrk file.
    """

    def __init__(
        self,
        settings:   Optional[FastloadSettings] = None,
        cache_root: Optional[Path] = None,
    ) -> None:
        self._settings = settings or _DEFAULT_SETTINGS
        # cache_root kwarg kept for backwards compat; settings.cache_root wins
        if cache_root is not None:
            self._root = Path(cache_root)
        else:
            self._root = self._settings.effective_root()

    # ── Cache key / directory ──────────────────────────────────────────────────

    def cache_key(self, wrk_path: Path) -> str:
        return hashlib.sha256(str(wrk_path.absolute()).encode()).hexdigest()

    def cache_dir(self, wrk_path: Path) -> Path:
        return self._root / self.cache_key(wrk_path)

    # ── Validation ─────────────────────────────────────────────────────────────

    def is_valid(self, wrk_path: Path) -> bool:
        """
        Return True only if a complete, up-to-date fastload cache exists.

        Validation: ready.flag must exist with matching fastload_version,
        wrk_mtime_ns, and wrk_size.
        """
        try:
            flag_path = self.cache_dir(wrk_path) / "ready.flag"
            if not flag_path.exists():
                return False
            flag = json.loads(flag_path.read_text(encoding="utf-8"))
            if flag.get("fastload_version") != FASTLOAD_VERSION:
                return False
            wrk_stat = wrk_path.stat()
            return (
                flag.get("wrk_mtime_ns") == wrk_stat.st_mtime_ns
                and flag.get("wrk_size")  == wrk_stat.st_size
            )
        except Exception:
            return False

    def has_stems(self, wrk_path: Path) -> bool:
        """Return True if the cache includes stem audio files."""
        d = self.cache_dir(wrk_path)
        return (d / "stems.meta.json").exists()

    # ── Audio format helpers ───────────────────────────────────────────────────

    @staticmethod
    def _audio_ext(fmt: str) -> str:
        return ".pcm16" if fmt == FORMAT_PCM16 else ".f32"

    @staticmethod
    def _write_audio(path: Path, audio: np.ndarray, fmt: str) -> None:
        """Write audio (n_frames, n_channels) float32 to disk."""
        if fmt == FORMAT_PCM16:
            _to_pcm16(np.ascontiguousarray(audio)).tofile(path)
        else:
            np.ascontiguousarray(audio.astype(np.float32)).tofile(path)

    @staticmethod
    def _read_audio(path: Path, n_frames: int, n_channels: int, fmt: str) -> np.ndarray:
        """Read audio from disk → (n_frames, n_channels) float32."""
        raw = np.fromfile(path, dtype=np.int16 if fmt == FORMAT_PCM16 else np.float32)
        if fmt == FORMAT_PCM16:
            return _from_pcm16(raw, n_frames, n_channels)
        return raw.reshape(n_frames, n_channels)

    # ── Read operations ────────────────────────────────────────────────────────

    def load_metadata(self, wrk_path: Path) -> "WrkMetadata":
        """Load all non-audio metadata from the fastload cache."""
        from wrekker.formats.wrk import WrkMetadata

        d        = self.cache_dir(wrk_path)
        manifest = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
        n_cols   = manifest.get("contents", {}).get("n_waveform_cols", 0)
        n_stems  = manifest.get("contents", {}).get("n_stems", 4)

        peaks_path = d / "waveform_peaks.f32"
        peaks      = (
            np.fromfile(peaks_path, dtype=np.float32)
            if peaks_path.exists() and n_cols > 0
            else np.zeros(2000, dtype=np.float32)
        )

        colors_path = d / "waveform_colors.bin"
        if colors_path.exists() and n_cols > 0:
            colors = np.fromfile(colors_path, dtype=np.uint8).reshape(-1, 3)
        else:
            colors = np.zeros((2000, 3), dtype=np.uint8)

        se_path = d / "stem_energy.f32"
        if se_path.exists() and n_cols > 0:
            stem_energy = np.fromfile(se_path, dtype=np.float32).reshape(n_cols, n_stems)
        else:
            stem_energy = np.zeros((2000, 4), dtype=np.float32)

        beatgrid = None
        bg_path  = d / "beatgrid.json"
        if bg_path.exists():
            beatgrid = json.loads(bg_path.read_text(encoding="utf-8"))

        cues_path = d / "cues.json"
        cues      = json.loads(cues_path.read_text()) if cues_path.exists() else []

        loops_path = d / "loops.json"
        loops      = json.loads(loops_path.read_text()) if loops_path.exists() else []

        artwork = None
        for ext in ("jpg", "png"):
            art_path = d / f"artwork.{ext}"
            if art_path.exists():
                artwork = art_path.read_bytes()
                break

        markers = []
        markers_path = d / "markers.json"
        if markers_path.exists():
            try:
                markers = _filter_saved_markers(json.loads(markers_path.read_text(encoding="utf-8")))
            except Exception:
                markers = []
        stem_horizon = None
        horizon_path = d / "stem_horizon.json"
        if horizon_path.exists():
            try:
                from wrekker.analysis.stem_horizon import normalize_stem_horizon
                stem_horizon = normalize_stem_horizon(json.loads(horizon_path.read_text(encoding="utf-8")))
            except Exception:
                stem_horizon = None

        return WrkMetadata(
            manifest        = manifest,
            waveform_peaks  = peaks,
            waveform_colors = colors,
            stem_energy     = stem_energy,
            beatgrid        = beatgrid,
            cues            = cues,
            loops           = loops,
            artwork         = artwork,
            markers         = markers,
            stem_horizon    = stem_horizon,
        )

    def load_mix(self, wrk_path: Path) -> tuple[np.ndarray, int]:
        """
        Load the full-mix audio from cache.
        Returns (audio, sr) where audio is (n_frames, n_channels) float32.
        """
        d    = self.cache_dir(wrk_path)
        meta = json.loads((d / "mix.meta.json").read_text(encoding="utf-8"))
        sr         = meta["sr"]
        n_frames   = meta["n_frames"]
        n_channels = meta["n_channels"]
        fmt        = meta.get("audio_format", FORMAT_F32)

        ext      = self._audio_ext(fmt)
        mix_path = d / f"mix{ext}"
        return self._read_audio(mix_path, n_frames, n_channels, fmt), sr

    def load_all_stems(self, wrk_path: Path) -> Optional[dict]:
        """
        Load all stem arrays from cache.
        Returns {name: (channels, n_frames) float32} or None if not cached.
        """
        d         = self.cache_dir(wrk_path)
        meta_path = d / "stems.meta.json"
        if not meta_path.exists():
            return None
        meta       = json.loads(meta_path.read_text(encoding="utf-8"))
        n_frames   = meta["n_frames"]
        n_channels = meta["n_channels"]
        fmt        = meta.get("audio_format", FORMAT_F32)
        ext        = self._audio_ext(fmt)

        stems: dict = {}
        for name in STEM_NAMES:
            stem_path = d / f"{name}{ext}"
            if not stem_path.exists():
                return None
            # Stored as (n_frames, n_channels); return as (channels, n_frames)
            audio = self._read_audio(stem_path, n_frames, n_channels, fmt)
            stems[name] = audio.T.astype(np.float32)
        return stems

    # ── Write operation (background) ───────────────────────────────────────────

    def build(
        self,
        wrk_path:    Path,
        meta:        "WrkMetadata",
        mix_audio:   np.ndarray,
        mix_sr:      int,
        stems_raw:   Optional[dict] = None,
        audio_format: str = FORMAT_PCM16,
    ) -> None:
        """
        Build a fastload cache from already-decoded data.

        Writes to a temporary directory first, then atomically renames.
        If any error occurs the temporary directory is removed and the
        cache is left incomplete (no ready.flag — will be ignored on load).

        stems_raw: {name: (channels, n_frames) float32} or None.
        If None, only the mix is cached (default recommended mode).

        audio_format: FORMAT_PCM16 (default, ~half size) or FORMAT_F32.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        key       = self.cache_key(wrk_path)
        final_dir = self._root / key
        tmp_dir   = self._root / f"{key}.tmp"

        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            ext = self._audio_ext(audio_format)

            # manifest
            (tmp_dir / "metadata.json").write_text(
                json.dumps(meta.manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # full mix
            mix_c      = np.ascontiguousarray(mix_audio.astype(np.float32))
            n_frames   = mix_c.shape[0]
            n_channels = mix_c.shape[1] if mix_c.ndim > 1 else 1
            if mix_c.ndim == 1:
                mix_c = mix_c[:, np.newaxis]

            self._write_audio(tmp_dir / f"mix{ext}", mix_c, audio_format)
            (tmp_dir / "mix.meta.json").write_text(
                json.dumps({
                    "sr": mix_sr, "n_frames": n_frames,
                    "n_channels": n_channels, "audio_format": audio_format,
                }),
                encoding="utf-8",
            )

            # waveform analysis
            meta.waveform_peaks.astype(np.float32).tofile(
                tmp_dir / "waveform_peaks.f32"
            )
            meta.waveform_colors.astype(np.uint8).tofile(
                tmp_dir / "waveform_colors.bin"
            )
            meta.stem_energy.astype(np.float32).tofile(
                tmp_dir / "stem_energy.f32"
            )

            # beatgrid / DJ data
            if meta.beatgrid:
                bg = dict(meta.beatgrid)
                if "beats" in bg and hasattr(bg["beats"], "tolist"):
                    bg["beats"] = bg["beats"].tolist()
                (tmp_dir / "beatgrid.json").write_text(
                    json.dumps(bg), encoding="utf-8"
                )
            (tmp_dir / "cues.json").write_text(
                json.dumps(meta.cues or []), encoding="utf-8"
            )
            (tmp_dir / "loops.json").write_text(
                json.dumps(meta.loops or []), encoding="utf-8"
            )
            if getattr(meta, "markers", None):
                markers = _filter_saved_markers(meta.markers)
                if markers:
                    (tmp_dir / "markers.json").write_text(
                        json.dumps(markers), encoding="utf-8"
                    )
            if getattr(meta, "stem_horizon", None):
                (tmp_dir / "stem_horizon.json").write_text(
                    json.dumps(meta.stem_horizon), encoding="utf-8"
                )

            # artwork
            if meta.artwork:
                art_ext = (
                    "png" if meta.artwork[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
                )
                (tmp_dir / f"artwork.{art_ext}").write_bytes(meta.artwork)

            # stems (optional)
            if stems_raw:
                first        = next(iter(stems_raw.values()))
                n_frames_s   = first.shape[1] if first.ndim > 1 else len(first)
                n_channels_s = first.shape[0] if first.ndim > 1 else 1
                for name in STEM_NAMES:
                    arr = stems_raw.get(name)
                    if arr is None:
                        continue
                    # arr is (channels, n_frames) → (n_frames, channels) for storage
                    arr_t = np.ascontiguousarray(arr.T.astype(np.float32))
                    self._write_audio(tmp_dir / f"{name}{ext}", arr_t, audio_format)
                (tmp_dir / "stems.meta.json").write_text(
                    json.dumps({
                        "sr": mix_sr, "n_frames": n_frames_s,
                        "n_channels": n_channels_s, "audio_format": audio_format,
                    }),
                    encoding="utf-8",
                )

            # ready.flag — written last; atomic completion marker
            wrk_stat = wrk_path.stat()
            flag = {
                "fastload_version": FASTLOAD_VERSION,
                "wrk_mtime_ns":     wrk_stat.st_mtime_ns,
                "wrk_size":         wrk_stat.st_size,
                "audio_format":     audio_format,
                "has_stems":        bool(stems_raw),
                "created_at":       time.time(),
            }
            (tmp_dir / "ready.flag").write_text(
                json.dumps(flag), encoding="utf-8"
            )

            # atomic rename to final location
            shutil.rmtree(final_dir, ignore_errors=True)
            tmp_dir.rename(final_dir)

        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    # ── Housekeeping ───────────────────────────────────────────────────────────

    def invalidate(self, wrk_path: Path) -> None:
        shutil.rmtree(self.cache_dir(wrk_path), ignore_errors=True)

    def entry_size_bytes(self, wrk_path: Path) -> int:
        """Return total bytes used by this entry's cache directory."""
        total = 0
        d = self.cache_dir(wrk_path)
        if d.is_dir():
            for f in d.rglob("*"):
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    def total_size_bytes(self) -> int:
        total = 0
        if not self._root.exists():
            return 0
        for entry in self._root.iterdir():
            if entry.is_dir():
                for f in entry.rglob("*"):
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return total

    def clean_orphans(self) -> int:
        """Remove incomplete entries (no ready.flag). Returns count removed."""
        removed = 0
        if not self._root.exists():
            return 0
        for entry in self._root.iterdir():
            if entry.is_dir() and not (entry / "ready.flag").exists():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed

    def clean_older_than(self, max_age_days: float) -> int:
        """Remove entries whose ready.flag is older than max_age_days."""
        cutoff  = time.time() - max_age_days * 86400
        removed = 0
        if not self._root.exists():
            return 0
        for entry in self._root.iterdir():
            flag_path = entry / "ready.flag"
            try:
                flag = json.loads(flag_path.read_text())
                if flag.get("created_at", 0) < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except Exception:
                pass
        return removed
