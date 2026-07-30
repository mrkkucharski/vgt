"""Tests for the monophonic pYIN transcription backend's algorithm half.

`segment_notes` is deliberately pure and takes an already-computed pitch track,
so every case here runs offline in microseconds without librosa, audio, or a
model -- the same reason `FakeTranscriber` exists for the other backends. The
one test that does touch the tracker synthesizes its own tone.
"""

from pathlib import Path
import math

import pytest

from vgt.pyin_notes import (
    PYIN_ALGORITHM_VERSION,
    _median_filter,
    segment_notes,
    transcribe_monophonic,
)

# 256-sample hop at 22050 Hz -- one frame is ~11.61 ms, matching the profile.
SAMPLE_RATE = 22050
HOP = 256
FRAME_S = HOP / SAMPLE_RATE


def _segment(midi: list[float], *, rms: list[float] | None = None, median: int = 1, min_ms: float = 0.0):
    return segment_notes(
        midi,
        rms if rms is not None else [1.0] * len(midi),
        sample_rate_hz=SAMPLE_RATE,
        hop_length=HOP,
        median_filter_frames=median,
        minimum_note_length_ms=min_ms,
    )


def _max_polyphony(notes) -> int:
    edges = [(start, 1) for start, _end, _p, _v in notes] + [(end, -1) for _start, end, _p, _v in notes]
    # Ends sort before starts at the same timestamp: a note ending exactly where
    # the next begins does not overlap it (the convention `transcribe.py` uses).
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    active = peak = 0
    for _time, delta in edges:
        active += delta
        peak = max(peak, active)
    return peak


NAN = float("nan")


def test_a_run_of_one_pitch_becomes_one_note() -> None:
    notes = _segment([40.0] * 10)

    assert notes == [(0.0, round(10 * FRAME_S, 6), 40, 127)]


def test_unvoiced_frames_separate_notes_and_produce_no_note_of_their_own() -> None:
    notes = _segment([40.0, 40.0, NAN, NAN, 45.0, 45.0])

    assert [(pitch, start, end) for start, end, pitch, _v in notes] == [
        (40, 0.0, round(2 * FRAME_S, 6)),
        (45, round(4 * FRAME_S, 6), round(6 * FRAME_S, 6)),
    ]


def test_fractional_pitches_quantize_to_the_nearest_semitone() -> None:
    notes = _segment([39.6, 40.4, 40.49] + [44.6] * 3)

    # 39.6 and 40.4 both round to 40, so they are one note, not two.
    assert [pitch for _s, _e, pitch, _v in notes] == [40, 45]


def test_adjacent_notes_share_an_exact_boundary_so_polyphony_is_one() -> None:
    """The invariant the pyin profiles rely on instead of a voice-cap stage.

    Boundaries must come out of the frame-time grid, not be accumulated as
    `start + n * hop` -- a single float ULP of overlap would report polyphony 2
    and silently make `bass`'s "one line by construction" claim false.
    """
    notes = _segment([40.0, 40.0, 45.0, 45.0, 50.0, 50.0, 45.0])

    assert len(notes) == 4
    assert _max_polyphony(notes) == 1
    for (_start, end, _p, _v), (next_start, *_rest) in zip(notes, notes[1:]):
        assert end == next_start


def test_a_long_alternating_track_never_overlaps() -> None:
    track = [40.0 if index % 2 else 47.0 for index in range(400)]

    notes = _segment(track)

    assert len(notes) == 400
    assert _max_polyphony(notes) == 1


def test_a_run_shorter_than_the_minimum_note_length_is_dropped() -> None:
    # One frame is ~11.61 ms, so three frames (~34.8 ms) is under a 70 ms floor
    # and seven (~81.3 ms) is over it. Six frames would be ~69.7 ms -- just
    # under, which is why the surviving run here is seven and not six.
    notes = _segment([40.0] * 3 + [NAN] + [45.0] * 7, min_ms=70.0)

    assert [pitch for _s, _e, pitch, _v in notes] == [45]


def test_the_minimum_note_length_never_merges_a_dropped_run_into_its_neighbour() -> None:
    notes = _segment([40.0] * 7 + [44.0] * 2 + [47.0] * 7, min_ms=70.0)

    assert [pitch for _s, _e, pitch, _v in notes] == [40, 47]
    # The survivor keeps its own onset; it does not absorb the dropped run.
    # `abs` rather than the default relative tolerance: emitted times are
    # rounded to 6 decimals, which is coarser than approx's 1e-6 *relative*
    # bound at this magnitude.
    assert notes[1][0] == pytest.approx(9 * FRAME_S, abs=1e-6)


def test_the_median_filter_closes_a_one_frame_dropout_instead_of_splitting_a_note() -> None:
    """A single unvoiced or jittered frame inside a held note must not split it
    -- otherwise `merge_fragments` has to clean up after the tracker."""
    with_dropout = _segment([40.0] * 4 + [NAN] + [40.0] * 4, median=5)
    with_jitter = _segment([40.0] * 4 + [52.0] + [40.0] * 4, median=5)

    assert [pitch for _s, _e, pitch, _v in with_dropout] == [40]
    assert [pitch for _s, _e, pitch, _v in with_jitter] == [40]


def test_the_median_filter_keeps_genuinely_adjacent_semitones_apart() -> None:
    notes = _segment([40.0] * 8 + [41.0] * 8, median=5)

    assert [pitch for _s, _e, pitch, _v in notes] == [40, 41]


def test_median_filter_is_an_identity_at_size_one() -> None:
    values = [3.0, 9.0, 1.0, 7.0]

    assert _median_filter(values, 1) == values


def test_velocity_scales_with_frame_energy_and_stays_in_the_midi_range() -> None:
    midi = [40.0] * 4 + [NAN] + [45.0] * 4
    rms = [1.0] * 4 + [0.0] + [0.04] * 4

    loud, quiet = _segment(midi, rms=rms)

    assert loud[3] == 127
    assert 1 <= quiet[3] < loud[3]


def test_velocity_falls_back_to_a_readable_default_on_a_silent_track() -> None:
    """A stem whose RMS is uniformly zero must not collapse to velocity 1."""
    notes = _segment([40.0] * 4, rms=[0.0] * 4)

    assert notes[0][3] == 90


def test_an_empty_or_fully_unvoiced_track_produces_no_notes() -> None:
    assert _segment([]) == []
    assert _segment([NAN] * 20) == []


def test_algorithm_version_is_a_positive_int_so_it_can_gate_the_cache() -> None:
    assert isinstance(PYIN_ALGORITHM_VERSION, int)
    assert PYIN_ALGORITHM_VERSION >= 1


def test_tracking_a_synthesized_tone_recovers_its_pitch(tmp_path: Path) -> None:
    """One end-to-end pass over real audio, so a librosa API change is caught.

    A 98 Hz sine is G2 (MIDI 43) -- an open bass G string -- held long enough to
    survive the note-length floor.
    """
    soundfile = pytest.importorskip("soundfile")
    frequency, duration = 98.0, 1.5
    samples = [
        0.5 * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)
        for index in range(int(SAMPLE_RATE * duration))
    ]
    source = tmp_path / "tone.wav"
    soundfile.write(str(source), samples, SAMPLE_RATE)

    notes = transcribe_monophonic(
        str(source),
        sample_rate_hz=SAMPLE_RATE,
        frame_length=2048,
        hop_length=HOP,
        minimum_frequency_hz=35.0,
        maximum_frequency_hz=330.0,
        median_filter_frames=5,
        minimum_note_length_ms=70.0,
    )

    assert notes, "a sustained tone must produce at least one note"
    longest = max(notes, key=lambda note: note[1] - note[0])
    assert longest[2] == 43
    assert longest[1] - longest[0] > duration / 2
    assert _max_polyphony(notes) == 1
