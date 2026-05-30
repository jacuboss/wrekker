"""
3-band parametric EQ per deck.

Bands
-----
LOW  — low-shelf  at  200 Hz  (±12 dB)
MID  — peaking EQ at 1000 Hz  (±12 dB, Q=0.9)
HIGH — high-shelf at 8000 Hz  (±12 dB)

Processing uses scipy.signal.lfilter (C-accelerated, releases the GIL).
When gain == 0 dB the band is bypassed entirely (no-op).

Thread safety
-------------
set_gain() is called from the main thread; process() from the audio callback.
Coefficients and gain_db are written as Python object references (atomic
under the CPython GIL). zi state arrays belong exclusively to the audio thread.
"""
from __future__ import annotations

import math
import numpy as np
from scipy.signal import lfilter

__all__ = ["DeckEQ"]

_DB_MIN = -12.0
_DB_MAX  = 12.0
_BANDS   = ("low", "mid", "high")


# ── biquad coefficient builders (return [b0, b1, b2, a1, a2] normalised) ─────

def _low_shelf(fc: float, gain_db: float, fs: int) -> np.ndarray:
    A      = 10.0 ** (gain_db / 40.0)
    w0     = 2.0 * math.pi * fc / fs
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha  = sin_w0 / 2.0 * math.sqrt(2.0) * math.sqrt(A)

    b0 =      A * ((A+1) - (A-1)*cos_w0 + 2*math.sqrt(A)*alpha)
    b1 =  2 * A * ((A-1) - (A+1)*cos_w0)
    b2 =      A * ((A+1) - (A-1)*cos_w0 - 2*math.sqrt(A)*alpha)
    a0 =           (A+1) + (A-1)*cos_w0 + 2*math.sqrt(A)*alpha
    a1 = -2 *     ((A-1) + (A+1)*cos_w0)
    a2 =           (A+1) + (A-1)*cos_w0 - 2*math.sqrt(A)*alpha
    return np.array([b0/a0, b1/a0, b2/a0, a1/a0, a2/a0])


def _high_shelf(fc: float, gain_db: float, fs: int) -> np.ndarray:
    A      = 10.0 ** (gain_db / 40.0)
    w0     = 2.0 * math.pi * fc / fs
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha  = sin_w0 / 2.0 * math.sqrt(2.0) * math.sqrt(A)

    b0 =      A * ((A+1) + (A-1)*cos_w0 + 2*math.sqrt(A)*alpha)
    b1 = -2 * A * ((A-1) + (A+1)*cos_w0)
    b2 =      A * ((A+1) + (A-1)*cos_w0 - 2*math.sqrt(A)*alpha)
    a0 =           (A+1) - (A-1)*cos_w0 + 2*math.sqrt(A)*alpha
    a1 =  2 *     ((A-1) - (A+1)*cos_w0)
    a2 =           (A+1) - (A-1)*cos_w0 - 2*math.sqrt(A)*alpha
    return np.array([b0/a0, b1/a0, b2/a0, a1/a0, a2/a0])


def _peak_eq(fc: float, gain_db: float, Q: float, fs: int) -> np.ndarray:
    A      = 10.0 ** (gain_db / 40.0)
    w0     = 2.0 * math.pi * fc / fs
    cos_w0 = math.cos(w0)
    alpha  = math.sin(w0) / (2.0 * Q)

    b0 =  1.0 + alpha * A
    b1 = -2.0 * cos_w0
    b2 =  1.0 - alpha * A
    a0 =  1.0 + alpha / A
    a1 = -2.0 * cos_w0
    a2 =  1.0 - alpha / A
    return np.array([b0/a0, b1/a0, b2/a0, a1/a0, a2/a0])


# ── per-band state ────────────────────────────────────────────────────────────

class _Band:
    """One biquad band with scipy.signal.lfilter processing."""

    def __init__(self, coeffs: np.ndarray, gain_db: float = 0.0) -> None:
        self._b       = coeffs[:3].copy()        # [b0, b1, b2]
        self._a       = np.array([1.0, coeffs[3], coeffs[4]])
        self._zi      = np.zeros((2, 2))         # (channels, 2) — audio thread only
        self._gain_db = gain_db                  # main thread writes, audio thread reads

    def set_coeffs(self, coeffs: np.ndarray, gain_db: float) -> None:
        """Main thread — update coefficients atomically."""
        self._b       = coeffs[:3].copy()
        self._a       = np.array([1.0, coeffs[3], coeffs[4]])
        self._gain_db = gain_db

    @property
    def is_unity(self) -> bool:
        return abs(self._gain_db) < 1e-4

    def process(self, frame: np.ndarray) -> None:
        """
        Audio thread — apply biquad in-place via lfilter (C, releases GIL).
        frame: (N, channels) float32 — modified in place.
        """
        b, a = self._b, self._a
        for ch in range(frame.shape[1]):
            y, zf = lfilter(b, a, frame[:, ch], zi=self._zi[ch])
            self._zi[ch]   = zf
            frame[:, ch]   = y.astype(np.float32)


# ── public EQ ─────────────────────────────────────────────────────────────────

class DeckEQ:
    """3-band EQ for one deck. Call process() in audio thread, set_gain() from main."""

    _FC  = {"low": 200.0, "mid": 1000.0, "high": 8000.0}
    _Q   = 0.9

    def __init__(self, fs: int) -> None:
        self._fs    = fs
        self._gains = {"low": 0.0, "mid": 0.0, "high": 0.0}
        self._bands = {
            "low":  _Band(_low_shelf (self._FC["low"],  0.0, fs), 0.0),
            "mid":  _Band(_peak_eq   (self._FC["mid"],  0.0, self._Q, fs), 0.0),
            "high": _Band(_high_shelf(self._FC["high"], 0.0, fs), 0.0),
        }

    def set_gain(self, band: str, gain_db: float) -> None:
        gain_db = max(_DB_MIN, min(_DB_MAX, gain_db))
        self._gains[band] = gain_db
        if band == "low":
            c = _low_shelf(self._FC["low"], gain_db, self._fs)
        elif band == "mid":
            c = _peak_eq(self._FC["mid"], gain_db, self._Q, self._fs)
        else:
            c = _high_shelf(self._FC["high"], gain_db, self._fs)
        self._bands[band].set_coeffs(c, gain_db)

    def get_gain(self, band: str) -> float:
        return self._gains[band]

    def process(self, frame: np.ndarray) -> None:
        """Audio thread. Skips bands at 0 dB."""
        for name in _BANDS:
            b = self._bands[name]
            if not b.is_unity:
                b.process(frame)
