"""
Stem separation model interfaces and implementations.

HTDemucsModel  — high-quality background separation (default)
ONNXModel      — real-time fallback (STUBBED — requires quantized .onnx model)
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Callable, Protocol, runtime_checkable

import numpy as np

__all__ = [
    "Priority",
    "StemStatus",
    "JobID",
    "StemResult",
    "StemModel",
    "HTDemucsModel",
    "UnavailableStemModel",
    "ONNXModel",
    "STEM_NAMES",
]

STEM_NAMES: tuple[str, ...] = ("vocals", "drums", "bass", "other")

JobID = str


class Priority(IntEnum):
    URGENT = 0  # active deck, stems requested with no cache
    HIGH   = 1  # secondary deck
    NORMAL = 2  # next track in queue
    LOW    = 3  # rest of queue, FIFO


class StemStatus(str, Enum):
    NONE          = "none"          # not analyzed, not queued
    QUEUED        = "queued"        # in worker queue
    ANALYZING     = "analyzing"     # currently being processed
    READY         = "ready"         # stems loaded and active in engine
    FAILED        = "failed"        # analysis error
    WRK_LOADING   = "wrk_loading"   # reading .wrk metadata / waveform
    MIX_READY     = "mix_ready"     # full mix loaded, deck playable, stems pending
    STEMS_LOADING = "stems_loading" # decoding stems from .wrk in background


@dataclass(frozen=True)
class StemResult:
    """
    Separated stems for one track.
    Each stem: float32 ndarray of shape (channels, samples).
    """
    vocals: np.ndarray
    drums:  np.ndarray
    bass:   np.ndarray
    other:  np.ndarray
    model:      str    # "htdemucs" | "onnx_fallback"
    duration_s: float

    def __getitem__(self, name: str) -> np.ndarray:
        return getattr(self, name)

    # numpy arrays aren't hashable — disable auto-generated hash from frozen=True
    def __hash__(self) -> int:  # type: ignore[override]
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other


@runtime_checkable
class StemModel(Protocol):
    """Common interface for all stem separation backends."""

    def separate(
        self,
        audio: np.ndarray,
        sr: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> StemResult | None:
        """
        Separate `audio` (channels, samples) at sample rate `sr`.
        Returns None if cancelled via cancel_event.
        Progress fraction [0.0, 1.0] reported via on_progress.
        """
        ...

    def supports_cancellation(self) -> bool:
        """Whether in-flight separation can be interrupted cleanly."""
        ...


# ─── HTDemucs implementation ─────────────────────────────────────────────────

# Realtime factors measured on RTX 2080 and Ryzen 7 4700U.
# Used for progress estimation via elapsed-time curve.
_REALTIME_FACTOR_GPU = 0.05
_REALTIME_FACTOR_CPU = 0.45


class HTDemucsModel:
    """
    High-quality stem separator using Facebook's htdemucs model.

    Auto-detects CUDA and uses GPU when available (~0.05x realtime on RTX 2080).
    Falls back to CPU (~0.45x realtime on Ryzen 7 4700U).
    Lazy-loads model on first call. First run downloads ~80MB to ~/.cache/torch/hub/.

    Progress is reported via an elapsed-time curve since apply_model on BagOfModels
    does not expose per-segment callbacks at the top level.
    Cancellation signals the inference thread to be abandoned; the daemon thread
    finishes its current segment and terminates when the process exits.
    """

    MODEL_NAME = "htdemucs"

    def __init__(self) -> None:
        self._model:  object = None
        self._device: str    = ""
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from demucs.pretrained import get_model
            except ImportError as exc:
                raise RuntimeError(
                    "demucs is not installed. Run: pip install demucs"
                ) from exc
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = get_model(self.MODEL_NAME)
            model.to(device)
            model.eval()
            self._model  = model
            self._device = device

    def _prep_tensor(self, audio: np.ndarray, sr: int) -> "torch.Tensor":
        import torch
        from demucs.audio import convert_audio

        if audio.ndim == 1:
            audio = audio[np.newaxis, :]
        tensor = torch.from_numpy(np.ascontiguousarray(audio)).float()
        tensor = convert_audio(
            tensor, sr,
            self._model.samplerate,
            self._model.audio_channels,
        )
        return tensor.unsqueeze(0).to(self._device)  # (1, ch, samples)

    def _estimate_duration_s(self, audio_dur: float) -> float:
        """Estimate wall-clock seconds for inference on the current device."""
        factor = (
            _REALTIME_FACTOR_GPU if self._device == "cuda"
            else _REALTIME_FACTOR_CPU
        )
        return max(audio_dur * factor, 1.0)

    def separate(
        self,
        audio: np.ndarray,
        sr: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> StemResult | None:
        import torch
        from demucs.apply import apply_model

        # Signal immediately so UI exits "waiting" state.
        if on_progress:
            on_progress(0.0)

        if cancel_event and cancel_event.is_set():
            return None

        # Model load can take 30-120s on first run (download + init).
        # Heartbeat keeps the UI refreshing elapsed time during load.
        if self._model is None:
            _hb_stop = threading.Event()

            def _heartbeat() -> None:
                while not _hb_stop.wait(2.0):
                    if on_progress:
                        on_progress(0.0)

            threading.Thread(target=_heartbeat, daemon=True,
                             name="model-load-hb").start()
            try:
                self._ensure_loaded()
            finally:
                _hb_stop.set()
        else:
            self._ensure_loaded()

        if cancel_event and cancel_event.is_set():
            return None

        tensor    = self._prep_tensor(audio, sr)
        model     = self._model
        audio_dur = audio.shape[-1] / sr
        est_s     = self._estimate_duration_s(audio_dur)

        # Run inference in a daemon thread; poll for cancellation + progress.
        # apply_model on BagOfModels delegates to sub-models internally and does
        # not expose per-segment callbacks, so progress is time-based.
        sources_out: list = [None]
        exc_out:     list = [None]

        def _infer() -> None:
            try:
                with torch.no_grad():
                    sources_out[0] = apply_model(
                        model, tensor,
                        progress=False,
                        device=self._device,
                        num_workers=0,
                    )
            except Exception as exc:
                exc_out[0] = exc

        infer_t = threading.Thread(target=_infer, daemon=True, name="htdemucs-infer")
        t_start = time.monotonic()
        infer_t.start()

        while infer_t.is_alive():
            if cancel_event and cancel_event.is_set():
                return None  # thread continues as daemon until process exit

            elapsed = time.monotonic() - t_start
            if on_progress:
                # Asymptotic curve: reaches 0.95 around est_s, never stalls.
                frac = 1.0 - math.exp(-elapsed / est_s * 1.8)
                on_progress(min(frac, 0.95))

            infer_t.join(timeout=0.5)

        if exc_out[0] is not None:
            raise exc_out[0]

        if cancel_event and cancel_event.is_set():
            return None

        if on_progress:
            on_progress(1.0)

        sources = sources_out[0]
        src_idx = {name: i for i, name in enumerate(model.sources)}

        def _stem(name: str) -> np.ndarray:
            return sources[0, src_idx[name]].cpu().numpy()  # (ch, samples)

        return StemResult(
            vocals=_stem("vocals"),
            drums=_stem("drums"),
            bass=_stem("bass"),
            other=_stem("other"),
            model=f"htdemucs/{self._device}",
            duration_s=audio_dur,
        )

    def supports_cancellation(self) -> bool:
        # Cancellation abandons the daemon thread mid-inference; it terminates
        # at the next segment boundary naturally.
        return True


class UnavailableStemModel:
    """Stem model used when first-time setup was skipped or failed."""

    def __init__(self, reason: str = "AI models are not installed") -> None:
        self.reason = reason

    def separate(
        self,
        audio: np.ndarray,
        sr: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> StemResult | None:
        if on_progress:
            on_progress(0.0)
        raise RuntimeError(
            f"{self.reason}. Open First-Time Setup to install HTDemucs and PyTorch."
        )

    def supports_cancellation(self) -> bool:
        return True


# ─── ONNX real-time fallback (STUB) ──────────────────────────────────────────

class ONNXModel:
    """
    Real-time stem separation fallback using a quantized ONNX model.

    STUB — not implemented yet. Requires a quantized INT8 mdx-net style model
    capable of ~1.0–1.2x realtime on a Ryzen 7 4700U.

    TODO: integrate model from community (e.g. UVR5 MDX-Net INT8 export).
          See: https://github.com/nomadkaraoke/python-audio-separator
    """

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path

    def separate(
        self,
        audio: np.ndarray,
        sr: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> StemResult | None:
        raise NotImplementedError(
            "ONNXModel real-time fallback is not yet implemented. "
            "Only HTDemucsModel (background, high-quality) is available in Phase 1."
        )

    def supports_cancellation(self) -> bool:
        return False
