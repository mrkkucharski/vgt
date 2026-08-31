"""Tests for the Essentia multi-pitch transcription backend's algorithm half.

`segment_multipitch` is deliberately pure and takes an already-computed
per-frame pitch-candidate list, so every case here runs offline in
microseconds without Essentia, audio, or a model -- the same reason
`test_pyin_notes.py`'s `segment_notes` tests do. The one test that touches
the real tracker is skipped when Essentia isn't installed.
"""

from pathlib import Path
import math

import pytest

from vgt.essentia_notes import (
    ESSENTIA_ALGORITHM_VERSION,
    _hz_to_midi,
    _merged_runs,
    segment_multipitch,
    transcribe_multipitch,
)

# 128-sample hop at 44100 Hz -- Essentia's own default for these algorithms.
SAMPLE_RATE = 44100.0
HOP = 128
FRAME_S = HOP / SAMPLE_RATE

A3_HZ = 220.0  # MIDI 57
A4_HZ = 440.0  # MIDI 69


def _segment(pitches_by_frame, *, rms: list[float] | None = None, min_ms: float = 0.0, gap_ms: float = 0.0):
    n_frames = len(pitches_by_frame)
    return segment_multipitch(
        pitches_by_frame,
        rms if rms is not None else [1.0] * n_frames,
        hop_seconds=FRAME_S,
        minimum_note_length_ms=min_ms,
        merge_gap_ms=gap_ms,
    )


def test_hz_to_midi_quantizes_to_the_nearest_semitone() -> None:
    assert _hz_to_midi(A4_HZ) == 69
    assert _hz_to_midi(A3_HZ) == 57


def test_a_run_of_one_pitch_across_frames_becomes_one_note() -> None:
    notes = _segment([[A4_HZ]] * 10)

    assert notes == [(0.0, round(10 * FRAME_S, 6), 69, 127)]


def test_frames_with_no_candidates_end_a_note() -> None:
    notes = _segment([[A4_HZ], [A4_HZ], [], [], [A4_HZ], [A4_HZ]])

    assert [(pitch, start, end) for start, end, pitch, _v in notes] == [
        (69, 0.0, round(2 * FRAME_S, 6)),
        (69, round(4 * FRAME_S, 6), round(6 * FRAME_S, 6)),
    ]


def test_simultaneous_pitches_produce_overlapping_notes() -> None:
    """Unlike pYIN's monophonic segmenter, this backend is genuinely
    polyphonic: two pitches active in the same frames are two notes that
    overlap, not a conflict to resolve."""
    notes = _segment([[A3_HZ, A4_HZ]] * 5)

    assert sorted(pitch for _s, _e, pitch, _v in notes) == [57, 69]
    assert all(start == 0.0 and end == round(5 * FRAME_S, 6) for start, end, _p, _v in notes)


def test_a_short_gap_is_bridged_into_one_note() -> None:
    notes = _segment([[A4_HZ]] * 3 + [[]] * 2 + [[A4_HZ]] * 3, gap_ms=2 * FRAME_S * 1000)

    assert [pitch for _s, _e, pitch, _v in notes] == [69]
    assert notes[0] == (0.0, round(8 * FRAME_S, 6), 69, 127)


def test_a_gap_longer_than_the_bridge_stays_two_notes() -> None:
    notes = _segment([[A4_HZ]] * 3 + [[]] * 5 + [[A4_HZ]] * 3, gap_ms=FRAME_S * 1000)

    assert [pitch for _s, _e, pitch, _v in notes] == [69, 69]


def test_a_run_shorter_than_the_minimum_note_length_is_dropped() -> None:
    # One frame is ~2.9 ms at Essentia's default hop, so 3 frames (~8.7 ms) is
    # under a 20 ms floor and 10 frames (~29 ms) is over it.
    notes = _segment([[A4_HZ]] * 3 + [[]] + [[A3_HZ]] * 10, min_ms=20.0)

    assert [pitch for _s, _e, pitch, _v in notes] == [57]


def test_velocity_scales_with_frame_energy_and_stays_in_the_midi_range() -> None:
    pitches = [[A4_HZ]] * 4 + [[]] + [[A3_HZ]] * 4
    rms = [1.0] * 4 + [0.0] + [0.04] * 4

    notes = _segment(pitches, rms=rms)
    loud = next(note for note in notes if note[2] == 69)
    quiet = next(note for note in notes if note[2] == 57)

    assert loud[3] == 127
    assert 1 <= quiet[3] < loud[3]


def test_velocity_falls_back_to_a_readable_default_on_a_silent_track() -> None:
    notes = _segment([[A4_HZ]] * 4, rms=[0.0] * 4)

    assert notes[0][3] == 90


def test_an_empty_or_fully_candidate_free_track_produces_no_notes() -> None:
    assert _segment([]) == []
    assert _segment([[]] * 20) == []


def test_merged_runs_bridges_and_splits_correctly() -> None:
    flags = [True, True, False, False, True, False, False, False, True]

    assert _merged_runs(flags, merge_gap_frames=2) == [(0, 5), (8, 9)]
    assert _merged_runs(flags, merge_gap_frames=0) == [(0, 2), (4, 5), (8, 9)]


def test_algorithm_version_is_a_positive_int_so_it_can_gate_the_cache() -> None:
    assert isinstance(ESSENTIA_ALGORITHM_VERSION, int)
    assert ESSENTIA_ALGORITHM_VERSION >= 1


def test_tracking_a_synthesized_chord_recovers_both_pitches(tmp_path: Path) -> None:
    """One end-to-end pass over real audio, so an Essentia API change is
    caught. Skipped where Essentia isn't installed -- it is an optional
    dependency, not a hard one (see the module docstring)."""
    pytest.importorskip("essentia")
    soundfile = pytest.importorskip("soundfile")
    sample_rate = 44100
    duration = 2.0
    samples = [
        0.3 * math.sin(2 * math.pi * A3_HZ * index / sample_rate)
        + 0.3 * math.sin(2 * math.pi * A4_HZ * index / sample_rate)
        for index in range(int(sample_rate * duration))
    ]
    source = tmp_path / "chord.wav"
    soundfile.write(str(source), samples, sample_rate)

    notes = transcribe_multipitch(
        str(source),
        algorithm="klapuri",
        sample_rate_hz=float(sample_rate),
        minimum_frequency_hz=70.0,
        maximum_frequency_hz=1400.0,
        minimum_note_length_ms=80.0,
        merge_gap_ms=30.0,
    )

    assert notes, "a sustained two-note chord must produce at least one note"
    pitches = {pitch for _s, _e, pitch, _v in notes}
    assert 57 in pitches or 69 in pitches
