"""Offline #185 score regression for conservative DrumScript de-duplication."""

from __future__ import annotations

import json
from pathlib import Path

from vgt.drum_cleanup import (
    BeatGridReference,
    CLEAN_PROFILE_NAME,
    DRUM_CLEANUP_PROFILES,
    NullOnsetEvidenceSource,
    apply_drum_cleanup,
)
from vgt.drum_evaluation import instrument_onsets
from vgt.drum_midi_score import score_onsets


FIXTURES = Path(__file__).parent / "fixtures" / "drums_7rivers"


def _ground_truth() -> tuple[dict[str, list[float]], float]:
    notes = json.loads((FIXTURES / "corrected_ground_truth.json").read_text())
    coverage_end_sec = max(float(note["time_sec"]) for note in notes)
    return instrument_onsets(notes), coverage_end_sec


def test_dedup_moves_7rivers_note_count_toward_truth_without_losing_raw_recall() -> None:
    """Use #182's scorer on the committed real-song symbolic fixture.

    The grid is deliberately only a tempo reference for this test; no audio
    evidence or timing adjustment is involved. This isolates exactly the
    same-label duplicate removal promised by the profile.

    Per ``tests/fixtures/drums_7rivers/README.md``, the ground truth only
    covers measures 3-30 (~0-57s) of the full 0-160s song; every candidate is
    trimmed to that same coverage window before scoring, otherwise the
    candidate's out-of-range notes all score as false positives and the
    precision/recall numbers are meaningless.
    """
    raw = json.loads((FIXTURES / "drumscript_raw_events.json").read_text())
    truth, coverage_end_sec = _ground_truth()
    # The measured project tempo is 120.004 BPM. Cover the entire fixture so
    # no event falls outside the known-tempo range and takes the safe no-op.
    beat_period = 60.0 / 120.004
    beat_grid = BeatGridReference(
        beat_times=tuple(index * beat_period for index in range(400)), downbeat_offset_s=0.0
    )
    deduplicated = apply_drum_cleanup(
        raw,
        profile=DRUM_CLEANUP_PROFILES[CLEAN_PROFILE_NAME],
        evidence_source=NullOnsetEvidenceSource(),
        beat_grid=beat_grid,
    )
    raw_in_window = [event for event in raw if float(event["time_sec"]) <= coverage_end_sec]
    candidate = instrument_onsets(
        [
            {"time_sec": event.time_sec, "instruments": [event.instrument]}
            for event in deduplicated
            if not event.suppressed and event.time_sec <= coverage_end_sec
        ]
    )

    before = score_onsets(truth, instrument_onsets(raw_in_window))
    after = score_onsets(truth, candidate)
    before_count = sum(len(times) for times in instrument_onsets(raw_in_window).values())
    after_count = sum(len(times) for times in candidate.values())
    truth_count = sum(len(times) for times in truth.values())

    assert abs(after_count / truth_count - 1.0) < abs(before_count / truth_count - 1.0)
    assert after["metrics"]["global"]["recall"] >= before["metrics"]["global"]["recall"]
