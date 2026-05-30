from __future__ import annotations


def test_deck_qml_waveform_renderer_is_feature_flagged(monkeypatch) -> None:
    from wrekker.ui.widgets.deck_timeline_quick import deck_qml_waveforms_enabled, deck_qml_waveforms_requested

    monkeypatch.delenv("WREKKER_ENABLE_QML_DECK_WAVEFORMS", raising=False)
    monkeypatch.delenv("WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS", raising=False)
    assert deck_qml_waveforms_requested() is False
    assert deck_qml_waveforms_enabled() is False

    monkeypatch.setenv("WREKKER_ENABLE_QML_DECK_WAVEFORMS", "1")
    assert deck_qml_waveforms_requested() is True
    assert deck_qml_waveforms_enabled() is False

    monkeypatch.setenv("WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS", "1")
    assert deck_qml_waveforms_enabled() is True


def test_deck_timeline_model_position_update_does_not_emit_overlay_revision() -> None:
    from wrekker.ui.qml_models import DeckTimelineModel

    model = DeckTimelineModel("A")
    revisions = []
    positions = []
    model.timelineRevisionChanged.connect(lambda: revisions.append(1))
    model.positionSecondsChanged.connect(lambda: positions.append(1))

    kwargs = dict(
        duration_s=120.0,
        beats=(0.0, 0.5, 1.0, 1.5),
        bpm=120.0,
        first_beat_s=0.0,
        cue_positions=[],
        loop=None,
        playing=True,
        sync_enabled=False,
        phase_err=None,
    )
    model.update_position(pos_s=1.0, **kwargs)
    model.update_position(pos_s=1.1, **kwargs)
    model.update_position(pos_s=1.2, **kwargs)

    assert len(positions) == 3
    assert len(revisions) == 1
