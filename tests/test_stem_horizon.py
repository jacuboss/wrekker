import numpy as np

from wrekker.analysis.stem_horizon import generate_stem_horizon


def _grid(duration_s: float = 32.0) -> dict:
    beat = 0.5
    beats = [i * beat for i in range(int(duration_s / beat))]
    return {
        "schema_version": 2,
        "bpm": 120.0,
        "beats": beats,
        "downbeats": beats[::4],
        "phrase_markers": [{"position_sec": 0.0, "phrase_length": 8}, {"position_sec": 16.0, "phrase_length": 8}],
    }


def _energy() -> np.ndarray:
    n = 320
    e = np.zeros((n, 4), dtype=np.float32)

    def fill(start_s, end_s, values):
        s = int(start_s / 32.0 * n)
        q = int(end_s / 32.0 * n)
        e[s:q, :] = values

    fill(0, 8, (0.02, 0.44, 0.32, 0.06))
    fill(8, 16, (0.30, 0.44, 0.32, 0.08))
    fill(16, 24, (0.02, 0.06, 0.03, 0.34))
    fill(24, 32, (0.02, 0.45, 0.35, 0.12))
    return e


def test_stem_horizon_generates_bar_aligned_activity_and_transitions() -> None:
    horizon = generate_stem_horizon(_grid(), _energy(), 32.0)

    assert horizon is not None
    assert horizon["schema_version"] == 1
    assert horizon["resolution"] == "bar"
    assert set(horizon["values"]) == {"vocals", "drums", "bass", "other"}
    assert len(horizon["bars"]) == len(horizon["values"]["vocals"])

    transitions = {(t["stem"], t["change"]) for t in horizon["transitions"]}
    assert ("vocals", "in") in transitions
    assert ("vocals", "out") in transitions
    assert ("drums", "out") in transitions
    assert ("bass", "out") in transitions
    assert ("drums", "in") in transitions
    assert ("bass", "in") in transitions


def test_stem_horizon_persists_in_wrk_metadata(tmp_path) -> None:
    import soundfile as sf

    from wrekker.formats.wrk import create_wrk, load_wrk_metadata

    sr = 8000
    audio = np.zeros((sr, 2), dtype=np.float32)
    source = tmp_path / "source.wav"
    sf.write(source, audio, sr)
    wrk = tmp_path / "track.wrk"
    horizon = generate_stem_horizon(_grid(), _energy(), 32.0)

    create_wrk(
        source,
        wrk,
        audio=audio,
        sr=sr,
        waveform_peaks=np.ones(_energy().shape[0], dtype=np.float32),
        waveform_colors=np.zeros((_energy().shape[0], 3), dtype=np.uint8),
        stem_energy=_energy(),
        beatgrid=_grid(),
        stem_horizon=horizon,
    )

    meta = load_wrk_metadata(wrk)
    assert meta.stem_horizon is not None
    assert meta.stem_horizon["values"]["bass"]
