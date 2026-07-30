"""End-to-end offline regression for the real 7Rivers ``drums-clean`` path.

This intentionally combines the production order: first translate
DrumScript's own quantized timeline onto the recorded project grid, then run
the complete cleanup recipe against the committed onset-strength envelope.
It complements the stage-specific 7Rivers regressions by protecting the
candidate that is ultimately authored as ``drums-clean`` MIDI.
"""

from __future__ import annotations

import json
from pathlib import Path

from vgt.drum_cleanup import (
    AudioOnsetEvidenceSource,
    BeatGridReference,
    CLEAN_PROFILE_NAME,
    DRUM_CLEANUP_PROFILES,
    apply_drum_cleanup,
)
from vgt.drum_evaluation import instrument_onsets
from vgt.drum_grid import reconcile_event_times
from vgt.drum_midi_score import score_onsets


FIXTURES = Path(__file__).parent / "fixtures" / "drums_7rivers"


def _envelope_evidence() -> AudioOnsetEvidenceSource:
    """Load the committed numeric envelope without attempting to load audio."""
    payload = json.loads((FIXTURES / "onset_strength_envelope.json").read_text())
    # Deliberately bypass ``__init__``: it normally loads a source audio file,
    # which this fixture expressly does not include.  The exercised production
    # logic is the same envelope preparation and bounded evidence lookup.
    source = object.__new__(AudioOnsetEvidenceSource)
    source._hop_length = payload["hop_length"]
    source._envelope = None
    source._baseline = None
    source._prominence_scale = None
    source._sr = None
    source._prepare_envelope(payload["strengths"], payload["sr"])
    return source


def _project_grid() -> tuple[BeatGridReference, float]:
    payload = json.loads((FIXTURES / "project_grid.json").read_text())
    period = 60.0 / float(payload["tempo_bpm"])
    downbeat = float(payload["downbeat_offset_sec"])
    return (
        BeatGridReference(
            beat_times=tuple(downbeat + index * period for index in range(payload["beat_count"])),
            downbeat_offset_s=downbeat,
        ),
        period,
    )


def _onsets(events: list[dict]) -> dict[str, list[float]]:
    return instrument_onsets(events)


def test_combined_7rivers_drums_clean_candidate_keeps_real_hits_on_time() -> None:
    """Score the shipped combined path within the corrected-MIDI coverage.

    Bounds deliberately describe quality, not an exact generated note list:
    no matched hit may be lost after cleanup, the candidate may not grow past
    the reconciled raw material, and matched notes must remain on the observed
    project timeline.  The current numerical baseline is documented in
    ``docs/drums-clean-profile.md``.
    """
    raw_events = json.loads((FIXTURES / "drumscript_raw_events.json").read_text())
    truth_notes = json.loads((FIXTURES / "corrected_ground_truth.json").read_text())
    coverage_end_sec = max(float(note["time_sec"]) for note in truth_notes)
    grid, beat_period_s = _project_grid()

    reconciled, grid_report = reconcile_event_times(
        raw_events, beat_grid=grid, beat_period_s=beat_period_s
    )
    assert grid_report is not None
    cleaned = apply_drum_cleanup(
        reconciled,
        profile=DRUM_CLEANUP_PROFILES[CLEAN_PROFILE_NAME],
        evidence_source=_envelope_evidence(),
        beat_grid=grid,
    )

    truth = _onsets(truth_notes)
    reconciled_in_window = [event for event in reconciled if float(event["time_sec"]) <= coverage_end_sec]
    candidate_events = [
        {"time_sec": event.time_sec, "instruments": [event.instrument]}
        for event in cleaned
        if not event.suppressed and event.time_sec <= coverage_end_sec
    ]
    before_cleanup = score_onsets(truth, _onsets(reconciled_in_window))
    final = score_onsets(truth, _onsets(candidate_events))

    final_global = final["metrics"]["global"]
    before_global = before_cleanup["metrics"]["global"]
    final_timing = final["timing"]["global"]
    candidate_count = len(candidate_events)
    raw_count = sum(len(event["instruments"]) for event in reconciled_in_window)
    suppressed_in_window = sum(event.suppressed and event.time_sec <= coverage_end_sec for event in cleaned)

    # Matched real hits survive the cleanup stage, while deduplication and
    # suppression cannot renew the raw event inflation observed on this stem.
    assert final_global["true_positives"] >= before_global["true_positives"]
    assert candidate_count <= raw_count
    assert 300 <= candidate_count <= 350
    assert 0 < suppressed_in_window <= 20

    # Broad per-instrument floors protect the final candidate without pinning
    # a particular event list or requiring unfixable open-hat classification.
    expected_minimums = {
        "hi_hat_closed": (0.38, 0.55, 0.45),
        "kick": (0.50, 0.47, 0.50),
        "snare": (0.75, 0.55, 0.64),
    }
    for instrument, (precision, recall, f1) in expected_minimums.items():
        metrics = final["metrics"]["per_instrument"][instrument]
        assert metrics["precision"] >= precision
        assert metrics["recall"] >= recall
        assert metrics["f1"] >= f1

    assert final_global["precision"] >= 0.48
    assert final_global["recall"] >= 0.52
    assert final_global["f1"] >= 0.50
    assert final_timing["count"] >= before_global["true_positives"]
    assert abs(final_timing["median_signed_error_ms"]) <= 25.0
