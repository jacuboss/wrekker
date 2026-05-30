"""
Real-time audio analysis for Wrekker decks.

LUFSMeter      — ITU-R BS.1770-4 integrated loudness (K-weighting filter)
SpectralMeter  — RMS per frequency band via FFT (per-stem capable)
PhaseCorrelator — stereo phase/mono-compatibility between two decks
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
from scipy.signal import sosfilt

from wrekker.core.deck import LoudnessMeasure, SpectralBands

__all__ = ["LUFSMeter", "SpectralMeter", "PhaseCorrelator"]

_NEG_INF = float("-inf")


# ─── K-weighting filter ───────────────────────────────────────────────────────

def _k_weighting_sos(fs: int) -> np.ndarray:
    """
    Two-stage K-weighting SOS coefficients for ITU-R BS.1770-4.

    Stage 1: high-shelf pre-filter  (+4 dB shelf above ~1.7 kHz)
    Stage 2: high-pass RLB weighting (−3 dB/oct below ~100 Hz)
    """
    # ── Stage 1: high-shelf ──────────────────────────────────────────────
    f0 = 1681.974450955533
    G  = 3.999843853973347
    Q  = 0.7071752369554196
    K  = math.tan(math.pi * f0 / fs)
    Vh = 10.0 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416

    a0 = 1.0 + K / Q + K * K
    s1 = np.array([[
        (Vh + Vb * K / Q + K * K) / a0,   # b0
        2.0 * (K * K - Vh) / a0,           # b1
        (Vh - Vb * K / Q + K * K) / a0,   # b2
        1.0,
        2.0 * (K * K - 1.0) / a0,          # a1
        (1.0 - K / Q + K * K) / a0,        # a2
    ]])

    # ── Stage 2: high-pass RLB ───────────────────────────────────────────
    f1 = 38.13547087602444
    Q1 = 0.5003270373238773
    K1 = math.tan(math.pi * f1 / fs)

    a0 = 1.0 + K1 / Q1 + K1 * K1
    s2 = np.array([[
        1.0 / a0,
       -2.0 / a0,
        1.0 / a0,
        1.0,
        2.0 * (K1 * K1 - 1.0) / a0,
        (1.0 - K1 / Q1 + K1 * K1) / a0,
    ]])

    return np.vstack([s1, s2])   # (2, 6)


def _sosfilt_step(
    sos: np.ndarray,    # (n_sections, 6)
    x:   np.ndarray,   # (frames, channels) float64
    zi:  np.ndarray,   # (n_sections, channels, 2)
) -> tuple[np.ndarray, np.ndarray]:
    """
    Apply SOS filter per channel using scipy.sosfilt (C-accelerated, GIL released).
    zi is updated in-place and returned.
    """
    channels = x.shape[1]
    out = np.empty_like(x)
    for ch in range(channels):
        out[:, ch], zi[:, ch, :] = sosfilt(sos, x[:, ch], zi=zi[:, ch, :])
    return out, zi


# ─── LUFS meter ───────────────────────────────────────────────────────────────

class LUFSMeter:
    """
    ITU-R BS.1770-4 loudness meter.

    Call process(audio) from the audio callback each frame.
    Read snapshot() at ~10 Hz for UI updates.

    audio shape: (frames, channels) float32 or float64
    The meter automatically downmixes to its configured channel count.
    """

    _ABS_GATE = -70.0

    def __init__(self, sr: int, channels: int = 2) -> None:
        self._sr       = sr
        self._channels = channels
        self._sos      = _k_weighting_sos(sr)
        self._zi       = np.zeros((self._sos.shape[0], channels, 2), dtype=np.float64)

        mom_n = int(0.400 * sr)   # 400 ms
        st_n  = int(3.0   * sr)   # 3 s

        self._mom_buf = np.zeros(mom_n, dtype=np.float64)
        self._st_buf  = np.zeros(st_n,  dtype=np.float64)
        self._mom_pos = 0
        self._st_pos  = 0
        self._mom_n   = mom_n
        self._st_n    = st_n
        self._filled  = 0

        self._true_peak = _NEG_INF

    def process(self, audio: np.ndarray) -> None:
        """Feed (frames, channels) float32/64. Called in audio callback."""
        if audio.shape[0] == 0:
            return

        # Ensure 2D
        x = audio if audio.ndim == 2 else audio[:, np.newaxis]

        # Downmix/upmix to configured channel count
        if x.shape[1] > self._channels:
            x = np.mean(x, axis=1, keepdims=True)
            if self._channels > 1:
                x = np.repeat(x, self._channels, axis=1)
        elif x.shape[1] < self._channels:
            x = np.repeat(x, self._channels // x.shape[1], axis=1)

        x = x.astype(np.float64)

        # True peak
        pk = float(np.max(np.abs(x)))
        if pk > 0.0:
            self._true_peak = max(self._true_peak, 20.0 * math.log10(pk))

        # K-weighting filter (C-accelerated via sosfilt)
        filtered, self._zi = _sosfilt_step(self._sos, x, self._zi)

        # Mean-square per frame (over channels)
        ms_frame = np.mean(filtered ** 2, axis=1)

        frames = len(ms_frame)
        # Vectorised circular buffer write
        for i in range(frames):
            v = ms_frame[i]
            self._mom_buf[self._mom_pos] = v
            self._st_buf [self._st_pos ] = v
            self._mom_pos = (self._mom_pos + 1) % self._mom_n
            self._st_pos  = (self._st_pos  + 1) % self._st_n

        self._filled += frames

    def snapshot(self) -> LoudnessMeasure:
        mom_lufs = self._window_lufs(self._mom_buf, self._mom_n)
        st_lufs  = self._window_lufs(self._st_buf,  self._st_n)
        return LoudnessMeasure(
            momentary_lufs  = mom_lufs,
            short_term_lufs = st_lufs,
            true_peak_dbfs  = self._true_peak,
        )

    def reset(self) -> None:
        self._mom_buf[:] = 0.0
        self._st_buf [:] = 0.0
        self._mom_pos = 0
        self._st_pos  = 0
        self._filled  = 0
        self._true_peak = _NEG_INF
        self._zi[:] = 0.0

    @staticmethod
    def _window_lufs(buf: np.ndarray, n: int) -> float:
        ms = np.mean(buf)
        if ms <= 0.0:
            return _NEG_INF
        lufs = -0.691 + 10.0 * math.log10(ms)
        return lufs if lufs > -140.0 else _NEG_INF


# ─── spectral meter ───────────────────────────────────────────────────────────

_N_SPECTRUM = 16   # log-spaced bands exposed to the UI


class SpectralMeter:
    """
    Per-band RMS energy via short-time FFT.

    Computes two sets of bands from each FFT frame:
      • 4 named bands (sub/bass/mids/highs) for stem display via snapshot()
      • 16 log-spaced bands (30–18 kHz) for the spectrum widget via snapshot_raw()

    FFT fires every fft_size samples (~23 ms @ 44100 Hz with fft_size=1024).
    """

    _BANDS: tuple[tuple[str, float, float], ...] = (
        ("sub",   20.0,    80.0),
        ("bass",  80.0,   300.0),
        ("mids",  300.0, 3000.0),
        ("highs", 3000.0, 20000.0),
    )

    def __init__(self, sr: int, fft_size: int = 1024) -> None:
        self._sr       = sr
        self._fft_size = fft_size
        self._window   = np.hanning(fft_size).astype(np.float32)
        self._buf      = np.zeros(fft_size, dtype=np.float32)
        self._buf_pos  = 0

        freqs = np.fft.rfftfreq(fft_size, 1.0 / sr)

        # 4-band masks (for SpectralBands / stem display)
        self._masks: list[np.ndarray] = [
            (freqs >= flo) & (freqs < fhi) for _, flo, fhi in self._BANDS
        ]
        self._result = np.full(4, -96.0, dtype=np.float64)

        # 16-band log-spaced masks (30 Hz – 18 kHz)
        edges = np.logspace(np.log10(30.0), np.log10(18000.0), _N_SPECTRUM + 1)
        self._log_masks: list[np.ndarray] = [
            (freqs >= edges[i]) & (freqs < edges[i + 1])
            for i in range(_N_SPECTRUM)
        ]
        self._result_raw = np.full(_N_SPECTRUM, -96.0, dtype=np.float64)

    def process(self, audio: np.ndarray) -> None:
        """Feed (frames, channels). Uses mono mix. Called in audio callback."""
        mono = np.mean(audio, axis=1).astype(np.float32) if audio.ndim > 1 else audio.astype(np.float32)

        frames = len(mono)
        space  = self._fft_size - self._buf_pos

        if frames >= space:
            self._buf[self._buf_pos:] = mono[:space]
            self._compute_fft()
            self._buf_pos = 0
        else:
            self._buf[self._buf_pos:self._buf_pos + frames] = mono
            self._buf_pos += frames

    def _compute_fft(self) -> None:
        spec  = np.abs(np.fft.rfft(self._buf * self._window))
        spec /= (self._fft_size * 0.5)
        for i, mask in enumerate(self._masks):
            if mask.any():
                rms = math.sqrt(float(np.mean(spec[mask] ** 2)) + 1e-30)
                self._result[i] = 20.0 * math.log10(rms)
        for i, mask in enumerate(self._log_masks):
            if mask.any():
                rms = math.sqrt(float(np.mean(spec[mask] ** 2)) + 1e-30)
                self._result_raw[i] = 20.0 * math.log10(rms)

    def snapshot(self) -> SpectralBands:
        r = self._result
        return SpectralBands(
            sub=float(r[0]), bass=float(r[1]),
            mids=float(r[2]), highs=float(r[3]),
        )

    def snapshot_raw(self) -> tuple[float, ...]:
        """Return all 16 log-spaced dBFS values for the UI spectrum widget."""
        return tuple(float(v) for v in self._result_raw)

    def reset(self) -> None:
        self._buf[:] = 0.0
        self._buf_pos = 0
        self._result[:] = -96.0
        self._result_raw[:] = -96.0


# ─── phase correlator ─────────────────────────────────────────────────────────

class PhaseCorrelator:
    """
    Goniometer-style phase correlation between two stereo decks.
    Returns values in [-1.0, +1.0].
    """

    def __init__(self, sr: int, window_ms: float = 300.0) -> None:
        n = max(1, int(window_ms / 1000.0 * sr))
        self._n   = n
        self._a   = np.zeros(n, dtype=np.float64)
        self._b   = np.zeros(n, dtype=np.float64)
        self._pos = 0

    def update(self, deck_a: np.ndarray, deck_b: np.ndarray) -> None:
        mono_a = np.mean(deck_a, axis=1) if deck_a.ndim > 1 else deck_a
        mono_b = np.mean(deck_b, axis=1) if deck_b.ndim > 1 else deck_b

        frames = len(mono_a)
        idx    = np.arange(self._pos, self._pos + frames) % self._n
        self._a[idx] = mono_a.astype(np.float64)
        self._b[idx] = mono_b.astype(np.float64)
        self._pos    = (self._pos + frames) % self._n

    def correlation(self) -> float:
        a = self._a - self._a.mean()
        b = self._b - self._b.mean()
        denom = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b)))
        if denom < 1e-10:
            return 0.0
        return float(np.dot(a, b) / denom)

    def reset(self) -> None:
        self._a[:] = 0.0
        self._b[:] = 0.0
        self._pos  = 0
