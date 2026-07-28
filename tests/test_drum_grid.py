"""Offline tests for reconciling a drum backend's grid with vgt's beat grid.

Symbolic only: no audio, no model, no REAPER. The 7Rivers regression at the
bottom uses the committed real-song fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vgt.drum_cleanup import BeatGridReference
from vgt.drum_evaluation import instrument_onsets
from vgt.drum_grid import detect_uniform_step, reconcile_event_times
from vgt.drum_midi_score import _match_pairs, score_onsets


FIXTURES = Path(__file__).parent / "fixtures" / "drums_7rivers"

# The measured 7Rivers project grid (docs/drums-transcription-timing-findings.md).
PROJECT_BPM = 120.004
PROJECT_DOWNBEAT_S = 0.085333


def _grid(bpm: float, downbeat: float, beats: int) -> BeatGridReference:
    period = 60.0 / bpm
    return BeatGridReference(
        beat_times=tuple(downbeat + index * period for index in range(beats)), downbeat_offset_s=downbeat
    )


def _events(times) -> list[dict]:
    return [{"time_sec": time, "instruments": ["kick"]} for time in times]


def _backend_eighths(step: float, indices) -> list[dict]:
    return _events([index * step for index in indices])


def test_detect_uniform_step_recovers_a_grid_no_pair_of_events_spans() -> None:
    """The smallest observed gap is only a first guess: here every adjacent
    pair is two or three steps apart, so the step has to come from the fit."""
    step = detect_uniform_step([index * 0.25 for index in (0, 2, 5, 7, 10, 12, 15, 17, 20)])
    assert step == pytest.approx(0.25)


def test_detect_uniform_step_declines_unquantized_and_tiny_inputs() -> None:
    # A backend emitting real onsets needs no correction; refuse to invent one.
    assert detect_uniform_step([0.0, 0.251, 0.507, 0.740, 1.013, 1.244, 1.502, 1.760, 2.03]) is None
    # Four events fit almost any step, so they are not evidence of a grid.
    assert detect_uniform_step([0.0, 0.5, 1.0, 1.5]) is None


def test_reconciliation_anchors_on_the_downbeat_and_cancels_backend_drift() -> None:
    """The two failures the user sees: a whole-track head start, and a rate
    error that grows until late notes sit a subdivision away from the beat."""
    backend_step = 0.2485  # 0.6% fast, four times DrumScript's real drift
    grid = _grid(bpm=120.0, downbeat=0.4, beats=200)
    events = _backend_eighths(backend_step, range(40))

    reconciled, report = reconcile_event_times(events, beat_grid=grid)

    assert report is not None
    assert report.step_seconds == pytest.approx(backend_step)
    assert report.subdivisions_per_beat == 2
    # Index k of the backend grid becomes index k of the project's, so the
    # first event moves onto the downbeat and the rest onto exact eighths.
    assert [event["time_sec"] for event in reconciled] == pytest.approx(
        [0.4 + index * 0.25 for index in range(40)]
    )
    # The drift was progressive, so the correction has to be too.
    assert report.max_shift_seconds > abs(report.median_shift_seconds)
    assert [event["instruments"] for event in reconciled] == [["kick"]] * len(events)


def test_reconciliation_keeps_the_performance_tempo_rather_than_a_mean_step() -> None:
    """The grid is subdivided beat by beat, so a performance that slows down
    keeps its own timing instead of being flattened onto an average."""
    beats = (0.0, 0.5, 1.0, 1.6, 2.4)  # last two beats are longer
    grid = BeatGridReference(beat_times=beats, downbeat_offset_s=0.0)
    events = _backend_eighths(0.3, range(8))

    reconciled, report = reconcile_event_times(events, beat_grid=grid)

    assert report is not None and report.subdivisions_per_beat == 2
    assert [event["time_sec"] for event in reconciled] == pytest.approx(
        [0.0, 0.25, 0.5, 0.75, 1.0, 1.3, 1.6, 2.0]
    )


@pytest.mark.parametrize(
    "reason, events, grid",
    [
        ("no analyzed grid", _backend_eighths(0.25, range(12)), None),
        ("grid too short to establish a tempo", _backend_eighths(0.25, range(12)), _grid(120.0, 0.0, 1)),
        (
            "backend onsets are not quantized",
            _events([0.0, 0.251, 0.507, 0.740, 1.013, 1.244, 1.502, 1.760, 2.03]),
            _grid(120.0, 0.0, 40),
        ),
        (
            "backend subdivision disagrees with the project tempo",
            _backend_eighths(0.30, range(12)),
            _grid(120.0, 0.0, 40),
        ),
        (
            "analysis only found beats from the middle of the song",
            _backend_eighths(0.25, range(12)),
            _grid(120.0, 30.0, 40),
        ),
        (
            "correction would move a note by more than a whole beat",
            _backend_eighths(0.25, range(0, 400, 4)),
            _grid(114.0, 0.0, 400),
        ),
    ],
)
def test_reconciliation_leaves_events_alone_when_it_cannot_be_trusted(reason, events, grid) -> None:
    reconciled, report = reconcile_event_times(events, beat_grid=grid)

    assert report is None, reason
    assert [event["time_sec"] for event in reconciled] == [event["time_sec"] for event in events], reason
    assert reconciled is not events and reconciled[0] is not events[0]


def test_reconciliation_extrapolates_past_the_last_analyzed_beat() -> None:
    """Drums can outlast the beat tracker's coverage; those events keep the
    correction rather than silently reverting to the backend's timeline."""
    grid = _grid(bpm=120.0, downbeat=0.1, beats=6)  # covers 0.1 .. 2.6 s
    events = _backend_eighths(0.24, range(16))  # runs to 3.6 s

    reconciled, report = reconcile_event_times(events, beat_grid=grid)

    assert report is not None
    assert [event["time_sec"] for event in reconciled] == pytest.approx(
        [0.1 + index * 0.25 for index in range(16)]
    )


def test_7rivers_raw_drumscript_events_land_on_the_beat_after_reconciliation() -> None:
    """Real committed DrumScript output vs the maintainer's corrected MIDI.

    As shipped, DrumScript's grid is anchored at 0.0 instead of the 0.0853 s
    downbeat and runs 120.185 BPM against the project's 120.004, so its events
    are ~85 ms early at the start and drift a whole eighth note by the end.
    Per the fixture README the ground truth only covers the front of the song,
    so both candidates are trimmed to its span before scoring.
    """
    raw = json.loads((FIXTURES / "drumscript_raw_events.json").read_text())
    truth_notes = json.loads((FIXTURES / "corrected_ground_truth.json").read_text())
    coverage_end_sec = max(float(note["time_sec"]) for note in truth_notes)
    truth = instrument_onsets(truth_notes)
    grid = _grid(PROJECT_BPM, PROJECT_DOWNBEAT_S, beats=400)

    reconciled, report = reconcile_event_times(raw, beat_grid=grid)

    assert report is not None
    assert report.step_seconds == pytest.approx(0.249615, abs=1e-5)
    assert report.subdivisions_per_beat == 2

    def _timing_error_ms(events):
        onsets = instrument_onsets([event for event in events if float(event["time_sec"]) <= coverage_end_sec])
        pairs = [
            pair
            for instrument in set(truth) | set(onsets)
            for pair in _match_pairs(truth.get(instrument, []), onsets.get(instrument, []), 0.12)
        ]
        errors = sorted((candidate - reference) * 1000 for reference, candidate in pairs)
        return errors[len(errors) // 2]

    def _f1(events):
        onsets = instrument_onsets([event for event in events if float(event["time_sec"]) <= coverage_end_sec])
        return score_onsets(truth, onsets)["metrics"]["global"]["f1"]

    # Shipped behaviour: notes sit ~89 ms ahead of the drummer.
    assert _timing_error_ms(raw) < -60.0
    # Reconciled: inside the 50 ms scoring tolerance, and far more notes match.
    assert abs(_timing_error_ms(reconciled)) < 25.0
    assert _f1(reconciled) > 4 * _f1(raw)
