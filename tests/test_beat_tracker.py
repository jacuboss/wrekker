from __future__ import annotations

import numpy as np
import soundfile as sf

from wrekker.analysis.beat_tracker import BeatTracker
from wrekker.formats.wrk import create_wrk, load_wrk_metadata


class _FakeModel:
    def __init__(self, beats, downbeats):
        self.beats = np.asarray(beats, dtype=np.float64)
        self.downbeats = np.asarray(downbeats, dtype=np.float64)

    def __call__(self, audio, sr):
        return self.beats, self.downbeats


def _write_audio(path, duration_s=8.0, sr=8000):
    t = np.arange(int(duration_s * sr), dtype=np.float32) / sr
    audio = 0.15 * np.sin(2.0 * np.pi * 440.0 * t)
    sf.write(path, np.column_stack([audio, audio]), sr)
    return np.column_stack([audio, audio]).astype(np.float32), sr


def _tracker_with(beats, downbeats):
    BeatTracker._model = _FakeModel(beats, downbeats)
    BeatTracker._model_error = None
    return BeatTracker()


def test_straight_techno(tmp_path):
    audio_path = tmp_path / "straight.wav"
    _write_audio(audio_path)
    period = 60.0 / 128.0
    beats = [i * period for i in range(128)]
    tracker = _tracker_with(beats, beats[::4])

    grid = tracker.analyze(str(audio_path))

    assert abs(grid.bpm - 128.0) < 0.01
    assert grid.confidence > 0.9
    assert not grid.bpm_variable


def test_swing_groove(tmp_path):
    audio_path = tmp_path / "swing.wav"
    _write_audio(audio_path)
    intervals = [0.42 if i % 2 == 0 else 0.52 for i in range(127)]
    beats = [0.0]
    for interval in intervals:
        beats.append(beats[-1] + interval)
    tracker = _tracker_with(beats, beats[::4])

    grid = tracker.analyze(str(audio_path))

    assert grid.swing_factor > 0.05


def test_variable_tempo(tmp_path):
    audio_path = tmp_path / "variable.wav"
    _write_audio(audio_path)
    intervals = np.linspace(0.44, 0.50, 127)
    beats = [0.0]
    for interval in intervals:
        beats.append(beats[-1] + float(interval))
    tracker = _tracker_with(beats, beats[::4])

    grid = tracker.analyze(str(audio_path))

    assert grid.bpm_variable


def test_wrk_integration(tmp_path):
    audio_path = tmp_path / "track.wav"
    audio, sr = _write_audio(audio_path)
    period = 60.0 / 128.0
    beats = [i * period for i in range(128)]
    grid = _tracker_with(beats, beats[::4]).analyze(str(audio_path))

    wrk_path = tmp_path / "track.wrk"
    create_wrk(
        source_path=audio_path,
        output_path=wrk_path,
        audio=audio,
        sr=sr,
        beatgrid=grid.to_dict(),
        bpm=grid.bpm,
    )
    meta = load_wrk_metadata(wrk_path)

    assert meta.beatgrid["schema_version"] == 2
    assert meta.beatgrid["model"] == "beat_this_v1"
    assert len(meta.beatgrid["downbeats"]) == 32


def test_phrase_detection(tmp_path):
    audio_path = tmp_path / "phrases.wav"
    _write_audio(audio_path)
    period = 60.0 / 128.0
    beats = [i * period for i in range(256)]
    tracker = _tracker_with(beats, beats[::4])

    grid = tracker.analyze(str(audio_path))

    assert grid.phrase_markers
    assert grid.phrase_markers[0].phrase_length in (8, 16)
    assert grid.phrase_markers[0].position_sec == 0.0
