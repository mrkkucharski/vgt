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
    BeatGridReference,
    CLEAN_DEDUP_MINIMUM_INTER_ONSET_BEATS,
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
from vgt.drum_midi_score import score_onsets

CLEAN = DRUM_CLEANUP_PROFILES[CLEAN_PROFILE_NAME]


def _half_second_grid() -> BeatGridReference:
    return BeatGridReference(beat_times=tuple(index * 0.5 for index in range(12)), downbeat_offset_s=0.0)


def test_velocity_defaults_cover_every_drumscript_instrument() -> None:
    assert set(CLEAN_VELOCITY_DEFAULTS) == set(DRUMSCRIPT_INSTRUMENTS)


def test_dedup_intervals_cover_every_drumscript_instrument() -> None:
    assert set(CLEAN_DEDUP_MINIMUM_INTER_ONSET_BEATS) == set(DRUMSCRIPT_INSTRUMENTS)


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


def test_grid_guided_systematic_offset_corrects_a_constant_late_detector() -> None:
    """The grid selects reliable beat-near audio peaks; audio remains the target."""
    raw_times = (0.045, 0.545, 1.045, 1.545)
    events = [{"time_sec": time, "instruments": ["kick"]} for time in raw_times]
    evidence_source = TableOnsetEvidenceSource(candidates=tuple((time - 0.045, 0.9) for time in raw_times))

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source, beat_grid=_half_second_grid())

    assert [event.time_sec for event in cleaned] == pytest.approx([0.0, 0.5, 1.0, 1.5])
    assert [event.timing_adjustment_s for event in cleaned] == pytest.approx([-0.045] * 4)

    # The shipped #182 scorer sees the systematic +45ms matched-note error
    # disappear on this deterministic equivalent of the corrected fixture.
    truth = {"kick": [0.0, 0.5, 1.0, 1.5]}
    before = score_onsets(truth, {"kick": list(raw_times)})
    after = score_onsets(truth, {"kick": [event.time_sec for event in cleaned]})
    assert before["timing"]["global"]["median_signed_error_ms"] == pytest.approx(45.0)
    assert after["timing"]["global"]["median_signed_error_ms"] == pytest.approx(0.0)


def test_grid_guided_systematic_offset_does_not_degrade_aligned_onsets() -> None:
    events = [{"time_sec": time, "instruments": ["snare"]} for time in (0.0, 0.5, 1.0, 1.5)]
    evidence_source = TableOnsetEvidenceSource(candidates=tuple((event["time_sec"], 0.9) for event in events))

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source, beat_grid=_half_second_grid())

    assert [event.time_sec for event in cleaned] == pytest.approx([event["time_sec"] for event in events])
    assert [event.timing_adjustment_s for event in cleaned] == pytest.approx([0.0] * len(events))


def test_grid_guided_systematic_offset_remains_source_start_safe() -> None:
    events = [{"time_sec": 0.02, "instruments": ["kick"]}, {"time_sec": 0.52, "instruments": ["kick"]}]
    evidence_source = TableOnsetEvidenceSource(candidates=((0.0, 0.9), (0.5, 0.9)))

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=evidence_source, beat_grid=_half_second_grid())

    assert all(event.time_sec >= 0.0 for event in cleaned)


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


def test_dedup_collapses_a_same_instrument_burst_using_the_tempo_scaled_interval() -> None:
    # At 120 BPM kick's 1/32-beat threshold is 15.625 ms.  The first two
    # events are a detector burst; the third is far enough away to retain.
    events = [
        {"time_sec": 1.000, "instruments": ["kick"]},
        {"time_sec": 1.010, "instruments": ["kick"]},
        {"time_sec": 1.030, "instruments": ["kick"]},
    ]

    cleaned = apply_drum_cleanup(
        events, profile=CLEAN, evidence_source=NullOnsetEvidenceSource(), beat_grid=_half_second_grid()
    )

    assert [event.raw_time_sec for event in cleaned] == pytest.approx([1.000, 1.030])


def test_dedup_preserves_same_instrument_onsets_spaced_above_the_interval() -> None:
    events = [
        {"time_sec": 1.000, "instruments": ["hi_hat_closed"]},
        {"time_sec": 1.020, "instruments": ["hi_hat_closed"]},
    ]

    cleaned = apply_drum_cleanup(
        events, profile=CLEAN, evidence_source=NullOnsetEvidenceSource(), beat_grid=_half_second_grid()
    )

    assert [event.raw_time_sec for event in cleaned] == pytest.approx([1.000, 1.020])


def test_dedup_never_merges_cross_instrument_coincidences() -> None:
    events = [
        {"time_sec": 1.000, "instruments": ["kick"]},
        {"time_sec": 1.001, "instruments": ["hi_hat_closed"]},
    ]

    cleaned = apply_drum_cleanup(
        events, profile=CLEAN, evidence_source=NullOnsetEvidenceSource(), beat_grid=_half_second_grid()
    )

    assert {event.instrument for event in cleaned} == {"kick", "hi_hat_closed"}


def test_dedup_keeps_both_when_no_tempo_reference_is_available() -> None:
    events = [{"time_sec": 1.000, "instruments": ["kick"]}, {"time_sec": 1.001, "instruments": ["kick"]}]

    cleaned = apply_drum_cleanup(events, profile=CLEAN, evidence_source=NullOnsetEvidenceSource())

    assert len(cleaned) == 2


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


def _synthetic_envelope_with_one_dominant_transient() -> tuple[list[float], float, int, list[int], list[int]]:
    """A deterministic onset-strength envelope shaped like the reported
    7Rivers failure mode: mostly quiet background, many moderate (but
    genuine) onsets spread across the whole file, and a couple of loud
    transients (crashes/fills) tens of times louder than everything else --
    the same "a few loud transients dominate the maximum" shape measured on
    the real stem (~659 events, a handful of standout crashes). Returns
    `(envelope, sr, hop_length, moderate_frames, dominant_frames)`."""
    hop_length = 512
    sr = 22050.0
    length = 6000
    # Small deterministic jitter, not a flat plateau, so a query centered on
    # background alone never lands on an exact tie (which the ambiguous-peak
    # check would -- correctly -- treat as "no evidence" regardless of
    # normalization, rather than exercising the baseline/scale math at all).
    envelope = [0.05 + (index % 3) * 0.001 for index in range(length)]
    moderate_frames = list(range(100, 5900, 40))  # ~145 genuine, moderate onsets
    for frame in moderate_frames:
        envelope[frame] = 1.0
    dominant_frames = [3005, 4505]  # off the moderate-onset stride so they never collide
    for frame in dominant_frames:
        envelope[frame] = 20.0
    return envelope, sr, hop_length, moderate_frames, dominant_frames


def test_local_prominence_normalization_does_not_collapse_with_one_dominant_transient() -> None:
    """Issue #183 regression: a couple of loud transients must not drag
    every other genuine, quieter onset's strength down toward zero the way
    plain global-max normalization did."""
    envelope, sr, hop_length, moderate_frames, dominant_frames = _synthetic_envelope_with_one_dominant_transient()
    frame_time = hop_length / sr

    source = AudioOnsetEvidenceSource(Path("unused.wav"), hop_length=hop_length)
    source._prepare_envelope(envelope, sr)

    for frame in moderate_frames:
        evidence = source.evidence_near(frame * frame_time, 0.05)
        assert evidence.available is True
        # What the old `peak / global_envelope_max` formula would have given
        # for this same moderate onset -- the exact collapse this issue fixes.
        old_global_max_strength = envelope[frame] / max(envelope)
        assert old_global_max_strength < CLEAN.suppression_strength_threshold
        # The new local-prominence strength must clear both thresholds, i.e.
        # neither suppressed nor treated as absent for alignment/velocity.
        assert evidence.strength > CLEAN.suppression_strength_threshold
        assert evidence.strength >= CLEAN.min_evidence_strength

    for frame in dominant_frames:
        dominant_evidence = source.evidence_near(frame * frame_time, 0.05)
        assert dominant_evidence.strength == pytest.approx(1.0)


def test_local_prominence_normalization_still_treats_true_silence_as_weak() -> None:
    """The fix must not simply inflate every strength to 1.0 -- a frame with
    no local excess over its own background must still score near zero."""
    envelope, sr, hop_length, _moderate_frames, _dominant_frames = _synthetic_envelope_with_one_dominant_transient()
    frame_time = hop_length / sr
    quiet_frame = 20  # far from every spike, pure background level

    source = AudioOnsetEvidenceSource(Path("unused.wav"), hop_length=hop_length)
    source._prepare_envelope(envelope, sr)

    evidence = source.evidence_near(quiet_frame * frame_time, 0.05)
    # Background jitter this close together is itself ambiguous (no single
    # confident peak) -- either "no evidence" or a strength at/below the
    # suppression bar is the safe, conservative outcome; never a confident
    # high strength.
    assert evidence.available is False or evidence.strength <= CLEAN.suppression_strength_threshold


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
