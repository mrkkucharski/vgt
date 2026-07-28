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
    backend_step = 0.249615  # DrumScript's real 7Rivers grid
    grid = _grid(PROJECT_BPM, PROJECT_DOWNBEAT_S, beats=200)
    events = _backend_eighths(backend_step, range(40))

    reconciled, report = reconcile_event_times(events, beat_grid=grid)

    assert report is not None
    assert report.step_seconds == pytest.approx(backend_step)
    assert report.subdivisions_per_beat == 2
    # Every event lands on an exact eighth of the project's grid: the first on
    # the downbeat, and the last with the accumulated drift taken back out.
    assert [event["time_sec"] for event in reconciled] == pytest.approx(
        [PROJECT_DOWNBEAT_S + index * 30.0 / PROJECT_BPM for index in range(40)]
    )
    # The drift was progressive, so the correction has to be too.
    assert report.max_shift_seconds > abs(report.median_shift_seconds)
    assert [event["instruments"] for event in reconciled] == [["kick"]] * len(events)


def test_a_backend_grid_that_slips_a_whole_slot_still_lands_on_the_played_beat() -> None:
    """The failure this replaced index-counting to fix.

    A backend quantizing to its own slightly-fast grid stays within half a
    slot of each real hit, but its *count* of slots runs ahead -- here a whole
    slot by two thirds of the way through. Re-emitting its index on the
    project grid puts every later note one eighth late; each event has to be
    matched to the project line it is actually nearest to.
    """
    downbeat, project_step, backend_step = 0.085333, 0.249992, 0.249615
    played = [downbeat + index * project_step for index in range(640)]
    # What the backend reports: each real hit rounded onto its own grid.
    events = _events([round(time / backend_step) * backend_step for time in played])

    reconciled, report = reconcile_event_times(
        events, beat_grid=_grid(120.004, downbeat, beats=400), beat_period_s=2 * project_step
    )

    assert report is not None
    assert [event["time_sec"] for event in reconciled] == pytest.approx(played, abs=1e-6)


def test_a_fitted_period_ignores_a_single_mistracked_beat() -> None:
    """The detected `beat_times` are individually noisy -- on 7Rivers eight
    beats sit >50 ms off the fitted line. Authoring onto the raw array copies
    each of those local errors into the MIDI (the notes then follow the click's
    own hiccup instead of the drummer), so a constant grid authors against the
    fitted period instead."""
    beats = [0.1 + index * 0.5 for index in range(40)]
    beats[8] -= 0.07  # the beat tracker fired early on one beat
    grid = BeatGridReference(beat_times=tuple(beats), downbeat_offset_s=0.1)
    events = _backend_eighths(0.2496, range(32))

    on_measured, _ = reconcile_event_times(events, beat_grid=grid)
    on_fitted, report = reconcile_event_times(events, beat_grid=grid, beat_period_s=0.5)

    assert report is not None
    # Index 16 is that beat. The measured array drags the note off with it.
    assert on_measured[16]["time_sec"] == pytest.approx(4.03)
    assert on_fitted[16]["time_sec"] == pytest.approx(4.1)
    assert [event["time_sec"] for event in on_fitted] == pytest.approx(
        [0.1 + index * 0.25 for index in range(32)]
    )


def test_reconciliation_keeps_the_performance_tempo_rather_than_a_mean_step() -> None:
    """The grid is subdivided beat by beat, so a performance that slows down
    keeps its own timing instead of being flattened onto an average."""
    beats = (0.0, 0.5, 1.0, 1.5, 2.0, 2.55, 3.1)  # the last two beats are longer
    grid = BeatGridReference(beat_times=beats, downbeat_offset_s=0.0)
    events = _backend_eighths(0.26, range(12))

    reconciled, report = reconcile_event_times(events, beat_grid=grid)

    assert report is not None and report.subdivisions_per_beat == 2
    # The tail follows the slowdown instead of a mean step laid over the song.
    assert [event["time_sec"] for event in reconciled] == pytest.approx(
        [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.275, 2.55, 2.825]
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
    events = _backend_eighths(0.2485, range(16))  # runs to 3.7 s

    for period in (None, 0.5):
        reconciled, report = reconcile_event_times(events, beat_grid=grid, beat_period_s=period)

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

    reconciled, report = reconcile_event_times(raw, beat_grid=grid, beat_period_s=60.0 / PROJECT_BPM)

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
