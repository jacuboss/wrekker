"""
.wrk — Wrekker prepared-track container format (v1).

Layout inside the ZIP:
    manifest.json
    audio/full.flac
    audio/vocals.flac  (optional)
    audio/drums.flac   (optional)
    audio/bass.flac    (optional)
    audio/other.flac   (optional)
    analysis/waveform_peaks.f32   raw float32 [n_cols]
    analysis/waveform_colors.bin  raw uint8   [n_cols * 3]
    analysis/stem_energy.f32      raw float32 [n_stems * n_cols]  row = stem
    analysis/beatgrid.json
    analysis/markers.json         auto-detected DJ markers (optional)
    dj/cues.json
    dj/loops.json
    artwork/cover.(jpg|png|webp)

All audio uses FLAC PCM_24.  Stems are stored sample-aligned at the source SR.
Atomic writes: caller writes to a .tmp path, then os.replace() to final path.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from wrekker.core.deck import MARKER_MIN_CONFIDENCE

__all__ = [
    "WRK_VERSION",
    "WREKKER_VERSION",
    "STEM_NAMES",
    "PreparedTrack",
    "WrkMetadata",
    "ValidationReport",
    "create_wrk",
    "load_wrk",
    "load_wrk_metadata",
    "load_wrk_mix",
    "load_wrk_stems",
    "inspect_wrk",
    "validate_wrk",
    "wrk_id_for",
    "wrk_path_for",
    "WrkPrepareMode",
    "encode_flac_bytes",
    "encode_stem_flacs",
]

WRK_VERSION     = 1
WREKKER_VERSION = "0.1.1"
STEM_NAMES      = ("vocals", "drums", "bass", "other")


def _filter_saved_markers(markers: Optional[list]) -> list:
    return [
        m for m in (markers or [])
        if float(m.get("confidence", 0.0) or 0.0) >= MARKER_MIN_CONFIDENCE
    ]


class WrkPrepareMode:
    FAST     = "fast"
    BALANCED = "balanced"
    ARCHIVE  = "archive"


_MODE_FLAC_LEVEL = {
    WrkPrepareMode.FAST: 0,
    WrkPrepareMode.BALANCED: 4,
    WrkPrepareMode.ARCHIVE: 8,
}

# Files that must be present for a valid .wrk
_REQUIRED = {"manifest.json", "audio/full.flac"}


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class PreparedTrack:
    """All data extracted from a .wrk file, ready for engine consumption."""
    manifest:    dict
    audio:       np.ndarray       # (samples, channels) float32 — full mix
    sr:          int
    stems:       Optional[dict]   # name → (channels, samples) float32, or None
    waveform_peaks:  np.ndarray   # float32 [n_cols]
    waveform_colors: np.ndarray   # uint8   [n_cols, 3]
    stem_energy:     np.ndarray   # float32 [n_cols, 4]  (cols: vocal/drum/bass/other)
    beatgrid:    Optional[dict]   # raw beatgrid dict (bpm, first_beat_s, beats, …)
    cues:        list[dict]
    loops:       list[dict]
    artwork:     Optional[bytes]
    markers:     list[dict] = field(default_factory=list)   # auto-detected markers
    stem_horizon: dict | None = None

    # Convenience properties
    @property
    def title(self) -> str:
        return self.manifest.get("metadata", {}).get("title", "")

    @property
    def artist(self) -> str:
        return self.manifest.get("metadata", {}).get("artist", "")

    @property
    def album(self) -> str:
        return self.manifest.get("metadata", {}).get("album", "")

    @property
    def duration_s(self) -> float:
        return self.manifest.get("metadata", {}).get("duration_s", 0.0)

    @property
    def bpm(self) -> Optional[float]:
        return self.manifest.get("metadata", {}).get("bpm")

    @property
    def key(self) -> Optional[str]:
        return self.manifest.get("metadata", {}).get("key")

    @property
    def has_stems(self) -> bool:
        return bool(self.stems)


@dataclass
class ValidationReport:
    valid:    bool
    version:  int
    errors:   list[str]
    warnings: list[str]

    def __str__(self) -> str:
        lines = [f"valid={self.valid}  version={self.version}"]
        lines += [f"  ERROR: {e}" for e in self.errors]
        lines += [f"  WARN:  {w}" for w in self.warnings]
        return "\n".join(lines)


# ── Path helpers ──────────────────────────────────────────────────────────────

def wrk_id_for(source_path: Path) -> str:
    """Deterministic ID for a source file (based on absolute path)."""
    return hashlib.sha256(str(source_path.absolute()).encode()).hexdigest()


def _sanitize_name(name: str, max_len: int = 80) -> str:
    """Make a string safe for use as a file/directory name on Linux."""
    # Characters that would break paths or tools
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    # Collapse multiple whitespace, strip leading/trailing dots and spaces
    name = " ".join(name.split()).strip(". ")
    return name[:max_len] or "_"


def wrk_path_for(source_path: Path, prepared_root: Path) -> Path:
    """
    Human-readable .wrk path mirroring the source folder structure.

        prepared/tracks/{source_folder}/{track_stem}.wrk

    Uses the immediate parent directory name as the set/folder name so a
    DJ can browse the prepared library the same way they browse their music.
    Collisions (two tracks with the same stem in the same folder) are
    resolved by appending a counter: "Track (2).wrk".
    """
    folder = _sanitize_name(source_path.parent.name) or "Unsorted"
    stem   = _sanitize_name(source_path.stem) or wrk_id_for(source_path)[:16]
    base   = prepared_root / "tracks" / folder / f"{stem}.wrk"
    # Resolve collision: if the file exists but belongs to a *different* source,
    # append a counter until we find a free slot.
    if not base.exists():
        return base
    if _index_owner(base) == str(source_path.absolute()):
        return base          # same source → reuse slot
    for n in range(2, 1000):
        candidate = base.with_stem(f"{stem} ({n})")
        if not candidate.exists():
            return candidate
        if _index_owner(candidate) == str(source_path.absolute()):
            return candidate
    return base              # give up, overwrite


def _index_owner(wrk_path: Path) -> str:
    """Return the source_path recorded in the folder index for this .wrk, or ''."""
    index_path = wrk_path.parent / "index.json"
    try:
        idx = json.loads(index_path.read_text(encoding="utf-8"))
        return idx.get(wrk_path.name, {}).get("source_path", "")
    except Exception:
        return ""


def update_folder_index(wrk_path: Path, source_path: Path, source_hash: str) -> None:
    """
    Update (or create) the per-folder index.json that maps each .wrk filename
    back to its source file.  Human-readable; not required for operation.
    """
    index_path = wrk_path.parent / "index.json"
    try:
        idx: dict = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except Exception:
        idx = {}
    idx[wrk_path.name] = {
        "source_path": str(source_path.absolute()),
        "source_hash": source_hash,
        "wrk_id":      wrk_id_for(source_path),
    }
    # Atomic write
    tmp = index_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, index_path)


# ── Source hash ───────────────────────────────────────────────────────────────

def _source_hash(path: Path) -> tuple[str, int]:
    """(sha256(abspath+mtime_ns), mtime_ns)"""
    stat = path.stat()
    payload = f"{path.absolute()}:{stat.st_mtime_ns}"
    return hashlib.sha256(payload.encode()).hexdigest(), stat.st_mtime_ns


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _flac_encode(audio: np.ndarray, sr: int, compression_level: int | None = None) -> bytes:
    """Encode (samples, channels) float32 array to FLAC bytes."""
    import soundfile as sf
    buf = io.BytesIO()
    kwargs = {}
    if compression_level is not None:
        kwargs["compression_level"] = int(max(0, min(8, compression_level)))
    try:
        sf.write(buf, audio, sr, format="FLAC", subtype="PCM_24", **kwargs)
    except TypeError:
        sf.write(buf, audio, sr, format="FLAC", subtype="PCM_24")
    return buf.getvalue()


def _mode_from_env(mode: str | None = None) -> str:
    value = (mode or os.environ.get("WREKKER_PREPARE_MODE") or WrkPrepareMode.FAST).lower()
    if value not in _MODE_FLAC_LEVEL:
        return WrkPrepareMode.FAST
    return value


def flac_compression_level(mode: str | None = None) -> int:
    override = os.environ.get("WREKKER_WRK_FLAC_COMPRESSION_LEVEL")
    if override is not None:
        try:
            return int(max(0, min(8, int(override))))
        except ValueError:
            pass
    return _MODE_FLAC_LEVEL[_mode_from_env(mode)]


def wrk_audio_encode_threads() -> int:
    raw = os.environ.get("WREKKER_WRK_AUDIO_ENCODE_THREADS", "2")
    try:
        return max(1, min(4, int(raw)))
    except ValueError:
        return 2


def encode_flac_bytes(
    audio: np.ndarray,
    sr: int,
    mode: str | None = None,
) -> bytes:
    """Encode one already-decoded audio buffer to FLAC using prepare-mode defaults."""
    return _flac_encode(audio, sr, compression_level=flac_compression_level(mode))


def encode_stem_flacs(
    stems: dict,
    sr: int,
    mode: str | None = None,
    max_workers: int | None = None,
) -> dict[str, bytes]:
    """Encode stem arrays to FLAC bytes in a bounded thread pool."""
    workers = max_workers or wrk_audio_encode_threads()

    def _encode_one(name: str) -> tuple[str, bytes]:
        stem = stems[name]
        return name, encode_flac_bytes(stem.T.astype(np.float32), sr, mode=mode)

    out: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wrk-flac") as pool:
        for name, data in pool.map(_encode_one, STEM_NAMES):
            out[name] = data
    return out


def _flac_decode(data: bytes) -> tuple[np.ndarray, int]:
    """Decode FLAC bytes → (samples, channels) float32, sample_rate."""
    import soundfile as sf
    audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    return audio, sr


# ── Write ─────────────────────────────────────────────────────────────────────

def create_wrk(
    source_path:  Path,
    output_path:  Path,
    audio:        np.ndarray,    # (samples, channels) float32 — full mix
    sr:           int,
    stems:        Optional[dict] = None,   # name → (channels, samples) float32
    waveform_peaks:  Optional[np.ndarray] = None,   # float32 [n_cols]
    waveform_colors: Optional[np.ndarray] = None,   # uint8 [n_cols, 3]
    stem_energy:     Optional[np.ndarray] = None,   # float32 [n_cols, 4]
    beatgrid:     Optional[dict] = None,
    cues:         Optional[list] = None,
    loops:        Optional[list] = None,
    artwork:      Optional[bytes] = None,
    markers:      Optional[list] = None,   # list of AutoMarker.to_dict()
    stem_horizon: Optional[dict] = None,
    title:        str = "",
    artist:       str = "",
    album:        str = "",
    bpm:          Optional[float] = None,
    key:          Optional[str] = None,
    lufs:         Optional[float] = None,
    stem_model:   str = "htdemucs",
    mix_flac:     Optional[bytes] = None,
    stem_flacs:   Optional[dict[str, bytes]] = None,
    prepare_mode: Optional[str] = None,
    timing_ms:    Optional[dict[str, int]] = None,
) -> None:
    """
    Write a .wrk container to output_path atomically.

    Heavy FLAC encoding may be supplied by the preparation scheduler through
    mix_flac/stem_flacs. Large already-compressed or binary assets are stored in
    the ZIP without recompression; only small JSON metadata is deflated.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    timing_ms = timing_ms if timing_ms is not None else {}

    def _ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _add_time(key: str, started: float) -> None:
        timing_ms[key] = timing_ms.get(key, 0) + _ms(started)

    mode = _mode_from_env(prepare_mode)
    src_hash, src_mtime = _source_hash(source_path)
    duration_s = audio.shape[0] / sr
    n_cols = len(waveform_peaks) if waveform_peaks is not None else 0
    has_stems = bool(stem_flacs) or stems is not None

    manifest: dict[str, Any] = {
        "wrk_version":      WRK_VERSION,
        "wrekker_version":  WREKKER_VERSION,
        "stem_model":       stem_model,
        "analysis_version": 1,
        "prepare_mode":     mode,
        "created_at":       time.time(),
        "source": {
            "path":     str(source_path.absolute()),
            "hash":     src_hash,
            "mtime_ns": src_mtime,
        },
        "metadata": {
            "title":      title,
            "artist":     artist,
            "album":      album,
            "duration_s": duration_s,
            "sample_rate": sr,
            "channels":   audio.shape[1] if audio.ndim > 1 else 1,
            "bpm":        bpm,
            "key":        key,
            "lufs":       lufs,
        },
        "contents": {
            "has_full_audio": True,
            "has_stems":      has_stems,
            "has_waveform":   waveform_peaks is not None,
            "has_beatgrid":   beatgrid is not None,
            "has_artwork":    artwork is not None,
            "n_waveform_cols": n_cols,
            "n_stems":        len(STEM_NAMES),
        },
    }

    if mix_flac is None:
        started = time.perf_counter()
        mix_flac = encode_flac_bytes(audio, sr, mode=mode)
        _add_time("packing.mix_flac_encode", started)

    if stem_flacs is None and stems:
        started = time.perf_counter()
        stem_flacs = encode_stem_flacs(stems, sr, mode=mode)
        _add_time("packing.stems_flac_encode", started)

    tmp_path = output_path.with_suffix(".tmp")
    total_started = time.perf_counter()
    try:
        zip_started = time.perf_counter()
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
            def write_json(name: str, value) -> None:
                started = time.perf_counter()
                zf.writestr(
                    name,
                    json.dumps(value, indent=2 if name == "manifest.json" else None),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=1,
                )
                _add_time(f"packing.{name.replace('/', '_')}_write", started)

            def write_stored(name: str, data: bytes) -> None:
                started = time.perf_counter()
                zf.writestr(name, data, compress_type=zipfile.ZIP_STORED)
                bucket = "zip_write_audio" if name.startswith("audio/") else "zip_write_analysis"
                if name.startswith("artwork/"):
                    bucket = "zip_write_artwork"
                _add_time(f"packing.{bucket}", started)

            write_json("manifest.json", manifest)

            write_stored("audio/full.flac", mix_flac)
            if stem_flacs:
                for name in STEM_NAMES:
                    data = stem_flacs.get(name)
                    if data is not None:
                        write_stored(f"audio/{name}.flac", data)

            if waveform_peaks is not None:
                write_stored(
                    "analysis/waveform_peaks.f32",
                    waveform_peaks.astype(np.float32).tobytes(),
                )
            if waveform_colors is not None:
                colors = waveform_colors if waveform_colors.ndim == 2 else waveform_colors.reshape(-1, 3)
                write_stored("analysis/waveform_colors.bin", colors.astype(np.uint8).tobytes())
            if stem_energy is not None:
                write_stored("analysis/stem_energy.f32", stem_energy.astype(np.float32).tobytes())

            if beatgrid:
                bg = dict(beatgrid)
                if "beats" in bg and hasattr(bg["beats"], "tolist"):
                    bg["beats"] = bg["beats"].tolist()
                write_json("analysis/beatgrid.json", bg)

            markers = _filter_saved_markers(markers)
            if markers:
                write_json("analysis/markers.json", markers)
            if stem_horizon:
                write_json("analysis/stem_horizon.json", stem_horizon)

            write_json("dj/cues.json", cues or [])
            write_json("dj/loops.json", loops or [])

            if artwork:
                if artwork[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = "png"
                elif artwork[:3] == b"\xff\xd8\xff":
                    ext = "jpg"
                else:
                    ext = "jpg"
                write_stored(f"artwork/cover.{ext}", artwork)

        _add_time("packing.zip_assembly", zip_started)
        finalize_started = time.perf_counter()
        os.replace(tmp_path, output_path)
        _add_time("packing.zip_finalization", finalize_started)
        timing_ms["packing.total"] = _ms(total_started)

    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── Read ──────────────────────────────────────────────────────────────────────

def load_wrk(path: Path | str) -> PreparedTrack:
    """
    Load all data from a .wrk file.

    Raises zipfile.BadZipFile or KeyError on corrupt/invalid files.
    """
    path = Path(path)
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())

        manifest = json.loads(zf.read("manifest.json"))
        audio, sr = _flac_decode(zf.read("audio/full.flac"))

        # Stems
        stems: Optional[dict] = None
        if manifest.get("contents", {}).get("has_stems"):
            raw_stems = {}
            for name in STEM_NAMES:
                entry = f"audio/{name}.flac"
                if entry in names:
                    s, _ = _flac_decode(zf.read(entry))
                    raw_stems[name] = s.T.astype(np.float32)  # (channels, samples)
            if len(raw_stems) == len(STEM_NAMES):
                stems = raw_stems

        # Waveform
        n_cols = manifest.get("contents", {}).get("n_waveform_cols", 0)
        if "analysis/waveform_peaks.f32" in names and n_cols > 0:
            peaks  = np.frombuffer(zf.read("analysis/waveform_peaks.f32"),
                                   dtype=np.float32).copy()
        else:
            peaks  = np.zeros(2000, dtype=np.float32)

        if "analysis/waveform_colors.bin" in names and n_cols > 0:
            raw = np.frombuffer(zf.read("analysis/waveform_colors.bin"),
                                dtype=np.uint8).copy()
            colors = raw.reshape(-1, 3)
        else:
            colors = np.zeros((2000, 3), dtype=np.uint8)

        n_stems = manifest.get("contents", {}).get("n_stems", 4)
        if "analysis/stem_energy.f32" in names and n_cols > 0:
            se_raw = np.frombuffer(zf.read("analysis/stem_energy.f32"),
                                   dtype=np.float32).copy()
            stem_energy = se_raw.reshape(n_cols, n_stems)
        else:
            stem_energy = np.zeros((2000, 4), dtype=np.float32)

        # Beatgrid
        beatgrid = None
        if "analysis/beatgrid.json" in names:
            beatgrid = json.loads(zf.read("analysis/beatgrid.json"))

        # DJ data
        cues  = json.loads(zf.read("dj/cues.json"))  if "dj/cues.json"  in names else []
        loops = json.loads(zf.read("dj/loops.json")) if "dj/loops.json" in names else []

        # Auto markers
        markers = []
        if "analysis/markers.json" in names:
            try:
                markers = _filter_saved_markers(json.loads(zf.read("analysis/markers.json")))
            except Exception:
                markers = []
        stem_horizon = None
        if "analysis/stem_horizon.json" in names:
            try:
                from wrekker.analysis.stem_horizon import normalize_stem_horizon
                stem_horizon = normalize_stem_horizon(json.loads(zf.read("analysis/stem_horizon.json")))
            except Exception:
                stem_horizon = None

        # Artwork
        artwork = None
        for ext in ("jpg", "png", "webp"):
            entry = f"artwork/cover.{ext}"
            if entry in names:
                artwork = zf.read(entry)
                break

    return PreparedTrack(
        manifest=manifest,
        audio=audio,
        sr=sr,
        stems=stems,
        waveform_peaks=peaks,
        waveform_colors=colors,
        stem_energy=stem_energy,
        beatgrid=beatgrid,
        cues=cues,
        loops=loops,
        artwork=artwork,
        markers=markers,
        stem_horizon=stem_horizon,
    )


# ── Staged-load data structure ────────────────────────────────────────────────

@dataclass
class WrkMetadata:
    """
    Non-audio content from a .wrk file.

    Populated by load_wrk_metadata() — no FLAC decoding required.
    Typically available in < 50 ms even for large .wrk files.
    """
    manifest:        dict
    waveform_peaks:  np.ndarray   # float32 [n_cols]
    waveform_colors: np.ndarray   # uint8   [n_cols, 3]
    stem_energy:     np.ndarray   # float32 [n_cols, 4]
    beatgrid:        Optional[dict]
    cues:            list[dict]
    loops:           list[dict]
    artwork:         Optional[bytes]
    markers:         list[dict] = field(default_factory=list)  # auto-detected markers
    stem_horizon:    dict | None = None

    @property
    def title(self) -> str:
        return self.manifest.get("metadata", {}).get("title", "")

    @property
    def artist(self) -> str:
        return self.manifest.get("metadata", {}).get("artist", "")

    @property
    def album(self) -> str:
        return self.manifest.get("metadata", {}).get("album", "")

    @property
    def duration_s(self) -> float:
        return self.manifest.get("metadata", {}).get("duration_s", 0.0)

    @property
    def sr(self) -> int:
        return self.manifest.get("metadata", {}).get("sample_rate", 44100)

    @property
    def channels(self) -> int:
        return self.manifest.get("metadata", {}).get("channels", 2)

    @property
    def bpm(self) -> Optional[float]:
        return self.manifest.get("metadata", {}).get("bpm")

    @property
    def key(self) -> Optional[str]:
        return self.manifest.get("metadata", {}).get("key")

    @property
    def has_stems(self) -> bool:
        return bool(self.manifest.get("contents", {}).get("has_stems"))

    @property
    def wrk_id(self) -> str:
        """Deterministic ID derived from the recorded source path."""
        src = self.manifest.get("source", {}).get("path", "")
        return hashlib.sha256(src.encode()).hexdigest() if src else ""


# ── Staged-load functions ─────────────────────────────────────────────────────

def load_wrk_metadata(path: Path | str) -> WrkMetadata:
    """
    Read all non-audio content from a .wrk — no FLAC decoding.

    Reads: manifest, waveform peaks/colors, stem_energy, beatgrid, cues,
    loops, artwork.  Typically completes in < 50 ms.
    """
    path = Path(path)
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        n_cols   = manifest.get("contents", {}).get("n_waveform_cols", 0)
        n_stems  = manifest.get("contents", {}).get("n_stems", 4)

        if "analysis/waveform_peaks.f32" in names and n_cols > 0:
            peaks = np.frombuffer(
                zf.read("analysis/waveform_peaks.f32"), dtype=np.float32
            ).copy()
        else:
            peaks = np.zeros(2000, dtype=np.float32)

        if "analysis/waveform_colors.bin" in names and n_cols > 0:
            raw    = np.frombuffer(
                zf.read("analysis/waveform_colors.bin"), dtype=np.uint8
            ).copy()
            colors = raw.reshape(-1, 3)
        else:
            colors = np.zeros((2000, 3), dtype=np.uint8)

        if "analysis/stem_energy.f32" in names and n_cols > 0:
            se_raw      = np.frombuffer(
                zf.read("analysis/stem_energy.f32"), dtype=np.float32
            ).copy()
            stem_energy = se_raw.reshape(n_cols, n_stems)
        else:
            stem_energy = np.zeros((2000, 4), dtype=np.float32)

        beatgrid = None
        if "analysis/beatgrid.json" in names:
            beatgrid = json.loads(zf.read("analysis/beatgrid.json"))

        cues  = json.loads(zf.read("dj/cues.json"))  if "dj/cues.json"  in names else []
        loops = json.loads(zf.read("dj/loops.json")) if "dj/loops.json" in names else []

        markers = []
        if "analysis/markers.json" in names:
            try:
                markers = _filter_saved_markers(json.loads(zf.read("analysis/markers.json")))
            except Exception:
                markers = []
        stem_horizon = None
        if "analysis/stem_horizon.json" in names:
            try:
                from wrekker.analysis.stem_horizon import normalize_stem_horizon
                stem_horizon = normalize_stem_horizon(json.loads(zf.read("analysis/stem_horizon.json")))
            except Exception:
                stem_horizon = None

        artwork = None
        for ext in ("jpg", "png", "webp"):
            entry = f"artwork/cover.{ext}"
            if entry in names:
                artwork = zf.read(entry)
                break

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


def load_wrk_mix(path: Path | str) -> tuple[np.ndarray, int]:
    """
    Decode only the full-mix FLAC from a .wrk.

    Returns (audio, sr) where audio is (samples, channels) float32.
    Does not decode stems.
    """
    with zipfile.ZipFile(Path(path), "r") as zf:
        return _flac_decode(zf.read("audio/full.flac"))


def load_wrk_stems(path: Path | str) -> Optional[dict]:
    """
    Decode only the stem FLACs from a .wrk.

    Returns dict {name: (channels, samples) float32} or None if not available.
    Does not decode the full mix.
    """
    path = Path(path)
    with zipfile.ZipFile(path, "r") as zf:
        names    = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        if not manifest.get("contents", {}).get("has_stems"):
            return None
        raw_stems: dict = {}
        for name in STEM_NAMES:
            entry = f"audio/{name}.flac"
            if entry in names:
                s, _ = _flac_decode(zf.read(entry))
                raw_stems[name] = s.T.astype(np.float32)   # (channels, samples)
        if len(raw_stems) != len(STEM_NAMES):
            return None
        return raw_stems


# ── Inspect (fast, manifest-only) ─────────────────────────────────────────────

def inspect_wrk(path: Path | str) -> dict:
    """Read manifest.json only — no audio decoding."""
    with zipfile.ZipFile(Path(path), "r") as zf:
        return json.loads(zf.read("manifest.json"))


# ── Validate ─────────────────────────────────────────────────────────────────

def validate_wrk(path: Path | str) -> ValidationReport:
    """Check structural integrity without fully decoding audio."""
    path = Path(path)
    errors:   list[str] = []
    warnings: list[str] = []
    version   = 0

    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())

            # Required files
            for req in _REQUIRED:
                if req not in names:
                    errors.append(f"missing required file: {req}")

            if "manifest.json" not in names:
                return ValidationReport(valid=False, version=0,
                                        errors=errors, warnings=warnings)

            manifest = json.loads(zf.read("manifest.json"))
            version  = manifest.get("wrk_version", 0)

            if version > WRK_VERSION:
                warnings.append(
                    f"wrk_version={version} > supported {WRK_VERSION}; "
                    "some features may be unavailable"
                )

            # Check stems consistency
            has_stems = manifest.get("contents", {}).get("has_stems", False)
            if has_stems:
                missing_stems = [
                    n for n in STEM_NAMES
                    if f"audio/{n}.flac" not in names
                ]
                if missing_stems:
                    warnings.append(f"manifest claims stems but missing: {missing_stems}")

            # Waveform consistency
            n_cols = manifest.get("contents", {}).get("n_waveform_cols", 0)
            if n_cols > 0:
                if "analysis/waveform_peaks.f32" not in names:
                    warnings.append("missing waveform_peaks.f32")

            # Try reading manifest metadata
            meta = manifest.get("metadata", {})
            if not meta.get("duration_s"):
                warnings.append("manifest missing duration_s")

    except zipfile.BadZipFile as exc:
        errors.append(f"not a valid ZIP: {exc}")
    except Exception as exc:
        errors.append(f"read error: {exc}")

    return ValidationReport(
        valid   = len(errors) == 0,
        version = version,
        errors  = errors,
        warnings= warnings,
    )
