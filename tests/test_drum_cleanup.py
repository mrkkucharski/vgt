"""Offline coverage for `vgt.drum_cleanup` (issue #177): the `drums-clean`
post-processing pipeline's coalescing, bounded timing alignment, velocity
shaping, and conservative suppression, exercised with strong, weak, and
unavailable/degenerate audio evidence -- entirely independent of a real
DrumScript invocation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from vgt.drum_cleanup import (
    CLEAN_PROFILE_NAME,
    CLEAN_VELOCITY_DEFAULTS,
    DEFAULT_PROFILE_NAME,
    DRUM_CLEANUP_PROFILE_NAMES,
    DRUM_CLEANUP_PROFILES,
    AudioOnsetEvidenceSource,
    NullOnsetEvidenceSource,
    OnsetEvidence,
    TableOnsetEvidenceSource,
    apply_drum_cleanup,
    cleaned_events_to_json,
    cleaned_events_to_midi_notes,
)
from vgt.transcribe import DRUMSCRIPT_INSTRUMENTS

CLEAN = DRUM_CLEANUP_PROFILES[CLEAN_PROFILE_NAME]


def test_velocity_defaults_cover_every_drumscript_instrument() -> None:
    assert set(CLEAN_VELOCITY_DEFAULTS) == set(DRUMSCRIPT_INSTRUMENTS)


def test_profile_registry_has_exactly_default_and_clean() -> None:
    assert DRUM_CLEANUP_PROFILE_NAMES == (DEFAULT_PROFILE_NAME, CLEAN_PROFILE_NAME)
    assert DRUM_CLEANUP_PROFILES[DEFAULT_PROFILE_NAME].enabled is False
    assert DRUM_CLEANUP_PROFILES[CLEAN_PROFILE_NAME].enabled is True


def test_default_profile_cleanup_is_disabled_and_refuses_to_run() -> None:
    with pytest.raises(ValueError):
        apply_drum_cleanup([], profile=DRUM_CLEANUP_PROFILES[DEFAULT_PROFILE_NAME], evidence_source=NullOnsetEvidenceSource())


def test_no_op_fallback_when_evidence_is_unavailable() -> None:
    events = [{"time_sec": 1.0, "instruments": ["kick"]}]
    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=NullOnsetEvidenceSource())

    assert len(cleaned) == 1
    event = cleaned[0]
    assert event.time_sec == pytest.approx(1.0)
    assert event.timing_adjustment_s == pytest.approx(0.0)
    assert event.timing_evidence_strength is None
    assert event.velocity_source == "role_default"
    assert event.velocity == CLEAN_VELOCITY_DEFAULTS["kick"]
    assert event.suppressed is False


def test_ambiguous_evidence_is_treated_as_absent() -> None:
    # Two candidates of comparable strength at meaningfully different times:
    # TableOnsetEvidenceSource refuses to guess between them.
    evidence_source = TableOnsetEvidenceSource(candidates=((1.005, 0.8), (0.995, 0.78)))
    events = [{"time_sec": 1.0, "instruments": ["snare"]}]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source)

    assert cleaned[0].time_sec == pytest.approx(1.0)
    assert cleaned[0].timing_evidence_strength is None
    assert cleaned[0].velocity_source == "role_default"


def test_strong_evidence_aligns_timing_within_the_bounded_window_and_shapes_velocity() -> None:
    evidence_source = TableOnsetEvidenceSource(candidates=((1.012, 0.9),))
    events = [{"time_sec": 1.0, "instruments": ["hi_hat_closed"]}]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source)
    event = cleaned[0]

    assert event.time_sec == pytest.approx(1.012)
    assert event.timing_adjustment_s == pytest.approx(0.012)
    assert event.timing_evidence_strength == pytest.approx(0.9)
    assert event.velocity_source == "audio_evidence"
    expected = CLEAN.velocity_floor + 0.9 * (CLEAN.velocity_ceiling - CLEAN.velocity_floor)
    assert event.velocity == pytest.approx(round(expected), abs=1)
    assert event.suppressed is False


def test_alignment_never_moves_an_event_further_than_the_bounded_window() -> None:
    far_time = 1.0 + CLEAN.alignment_window_s + 0.5
    evidence_source = TableOnsetEvidenceSource(candidates=((far_time, 0.95),))
    events = [{"time_sec": 1.0, "instruments": ["kick"]}]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source)

    # The far candidate is outside the lookup window entirely, so
    # TableOnsetEvidenceSource itself reports no evidence -- the bounded
    # search radius, not a downstream clamp, is what keeps this a no-op.
    assert cleaned[0].time_sec == pytest.approx(1.0)


def test_alignment_is_source_start_safe_even_with_a_negative_static_offset() -> None:
    profile = replace(CLEAN, static_offset_s=-0.02)
    events = [{"time_sec": 0.01, "instruments": ["kick"]}]

    cleaned = apply_drum_cleanup(events, profile=profile, evidence_source=NullOnsetEvidenceSource())

    assert cleaned[0].time_sec >= 0.0


def test_zero_and_near_zero_timestamps_stay_non_negative() -> None:
    events = [{"time_sec": 0.0, "instruments": ["kick"]}, {"time_sec": 0.001, "instruments": ["snare"]}]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=NullOnsetEvidenceSource())

    assert all(event.time_sec >= 0.0 for event in cleaned)


def test_weak_evidence_suppresses_conservatively_and_records_the_reason() -> None:
    evidence_source = TableOnsetEvidenceSource(candidates=((1.0, 0.05),))
    events = [{"time_sec": 1.0, "instruments": ["hi_hat_closed"]}]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source)
    event = cleaned[0]

    assert event.suppressed is True
    assert event.suppression_reason == "weak-local-audio-evidence"


def test_evidence_between_suppression_and_alignment_thresholds_falls_back_without_suppressing() -> None:
    # Strength above the suppression cutoff but below the min-evidence bar
    # used for alignment/velocity confidence: retained, not suppressed, and
    # not timing-adjusted -- exactly the ambiguous-evidence no-op contract.
    strength = (CLEAN.suppression_strength_threshold + CLEAN.min_evidence_strength) / 2
    evidence_source = TableOnsetEvidenceSource(candidates=((1.02, strength),))
    events = [{"time_sec": 1.0, "instruments": ["kick"]}]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source)
    event = cleaned[0]

    assert event.suppressed is False
    assert event.time_sec == pytest.approx(1.0)
    assert event.velocity_source == "role_default"


def test_suppressed_events_are_excluded_from_midi_but_retained_in_json() -> None:
    evidence_source = TableOnsetEvidenceSource(candidates=((1.0, 0.05),))
    events = [{"time_sec": 1.0, "instruments": ["hi_hat_closed"]}, {"time_sec": 2.0, "instruments": ["kick"]}]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source)
    notes = cleaned_events_to_midi_notes(cleaned, instrument_pitch=DRUMSCRIPT_INSTRUMENTS)
    json_events = cleaned_events_to_json(cleaned)

    assert len(notes) == 1  # only the un-suppressed kick becomes a note
    assert notes[0][2] == DRUMSCRIPT_INSTRUMENTS["kick"]
    assert len(json_events) == 2
    suppressed_record = next(record for record in json_events if record["instruments"] == ["hi_hat_closed"])
    assert suppressed_record["cleanup"]["suppressed"] is True
    assert suppressed_record["raw"]["time_sec"] == pytest.approx(1.0)


def test_coalescing_merges_events_within_the_simultaneity_window_to_the_earliest_onset() -> None:
    events = [
        {"time_sec": 1.000, "instruments": ["kick"]},
        {"time_sec": 1.002, "instruments": ["hi_hat_closed"]},  # within the 8ms window
    ]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=NullOnsetEvidenceSource())

    assert all(event.time_sec == pytest.approx(1.000) for event in cleaned)
    assert {event.instrument for event in cleaned} == {"kick", "hi_hat_closed"}


def test_coalescing_keeps_events_outside_the_window_separate() -> None:
    events = [
        {"time_sec": 1.000, "instruments": ["kick"]},
        {"time_sec": 1.100, "instruments": ["snare"]},  # well outside the 8ms window
    ]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=NullOnsetEvidenceSource())

    times = sorted(event.time_sec for event in cleaned)
    assert times[0] == pytest.approx(1.000)
    assert times[1] == pytest.approx(1.100)


def test_apply_drum_cleanup_never_assumes_a_fixed_event_count_or_pattern() -> None:
    """Regression guard for issue #177's core constraint: cleanup must treat
    an arbitrary, irregular event list the same way regardless of how many
    events or which instruments appear -- no groove-shaped branching."""
    sparse = [{"time_sec": 3.0, "instruments": ["crash"]}]
    dense = [{"time_sec": t / 10, "instruments": ["hi_hat_closed"]} for t in range(50)]

    cleaned_sparse = apply_drum_cleanup(sparse, profile=CLEAN, evidence_source=NullOnsetEvidenceSource())
    cleaned_dense = apply_drum_cleanup(dense, profile=CLEAN, evidence_source=NullOnsetEvidenceSource())

    assert len(cleaned_sparse) == 1
    assert len(cleaned_dense) == 50


def test_null_and_table_evidence_sources_never_return_partial_evidence() -> None:
    for evidence in (
        NullOnsetEvidenceSource().evidence_near(1.0, 0.03),
        TableOnsetEvidenceSource(candidates=()).evidence_near(1.0, 0.03),
    ):
        assert evidence.time_sec is None
        assert evidence.strength is None
        assert evidence.available is False


def test_audio_onset_evidence_source_is_a_safe_no_op_for_an_unreadable_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.wav"
    source = AudioOnsetEvidenceSource(missing)

    evidence = source.evidence_near(1.0, 0.03)

    assert evidence.available is False


def test_audio_onset_evidence_source_finds_a_strong_local_transient(tmp_path: Path) -> None:
    import numpy as np
    import soundfile as sf

    sr = 22050
    duration_s = 3.0
    samples = np.zeros(int(sr * duration_s), dtype=np.float32)
    rng = np.random.default_rng(0)
    click_index = int(1.5 * sr)
    burst_length = 400
    # A short broadband noise burst gives onset detection real spectral flux
    # to key on -- a flat plateau (no frequency content) does not.
    samples[click_index:click_index + burst_length] = (
        rng.standard_normal(burst_length).astype(np.float32) * 0.9
    )
    path = tmp_path / "click.wav"
    sf.write(str(path), samples, sr, subtype="PCM_16")

    source = AudioOnsetEvidenceSource(path)
    near_click = source.evidence_near(1.5, 0.05)
    far_from_click = source.evidence_near(0.2, 0.05)

    assert near_click.available is True
    assert near_click.strength > 0.5
    assert near_click.time_sec == pytest.approx(1.5, abs=0.05)
    # Away from the only transient, whatever evidence exists (if any) must
    # be far weaker than at the transient itself -- never mistaken for a
    # confident local onset.
    if far_from_click.available:
        assert far_from_click.strength < near_click.strength
