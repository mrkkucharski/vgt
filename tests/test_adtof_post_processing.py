"""Unit coverage for Phase 3's numpy-only ADTOF post-processing."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from vgt.drum_cleanup import BeatGridReference
from vgt.drum_evaluation import instrument_onsets
from vgt.drum_midi_score import score_onsets
from vgt.transcribe import (
    AdtofActivationResult,
    AdtofSpec,
    AdtofTranscriber,
    ADTOF_GM_INSTRUMENTS,
    _midi_has_non_percussion_notes,
    default_spec_for_target,
    postprocess_adtof_activations,
)


def _spec() -> AdtofSpec:
    spec = default_spec_for_target(
        "drums", backend="adtof", midi_tempo=120.0,
        beat_times=[0.0, 0.5, 1.0], downbeat_offset_s=0.0,
    )
    assert isinstance(spec, AdtofSpec)
    return spec


def _metadata() -> dict[str, object]:
    return {"fps": 100}


def test_postprocessor_peak_picks_and_quantizes_to_project_eighth_grid() -> None:
    activations = np.zeros((101, 5), dtype=np.float32)
    # A nearby lower kick is suppressed by the 80 ms class IOI.  The other
    # peaks intentionally land close to the 0.5/0.75 grid slots.
    activations[2, 0] = 0.9
    activations[6, 0] = 0.7
    activations[48, 1] = 0.6
    activations[73, 3] = 0.8
    activations[97, 4] = 0.4

    events, notes = postprocess_adtof_activations(activations, _metadata(), _spec())

    assert events == [
        {"time_sec": 0.0, "instruments": ["kick"]},
        {"time_sec": 0.5, "instruments": ["snare"]},
        {"time_sec": 0.75, "instruments": ["hi_hat_closed"]},
        {"time_sec": 1.0, "instruments": ["crash"]},
    ]
    assert [note[2] for note in notes] == [36, 38, 42, 49]
    assert all(1 <= note[3] <= 127 for note in notes)


def test_grid_association_uses_the_downbeat_when_beat_times_omit_it() -> None:
    spec = _spec()
    spec = AdtofSpec(**{**spec.__dict__, "beat_grid": BeatGridReference((0.5, 1.0), 0.0)})
    activations = np.zeros((30, 5), dtype=np.float32)
    activations[3, 0] = 0.8

    events, _notes = postprocess_adtof_activations(activations, _metadata(), spec)

    assert events == [{"time_sec": 0.0, "instruments": ["kick"]}]


class _Runner:
    def __init__(self, activations: np.ndarray) -> None:
        self.activations = activations

    def run(self, _source: Path, _spec: AdtofSpec, progress=None) -> AdtofActivationResult:
        return AdtofActivationResult(self.activations, _metadata(), "test", cache_hit=False)


def test_transcriber_authors_drumscript_contract_at_project_tempo_and_allows_empty(tmp_path: Path) -> None:
    result = AdtofTranscriber(_Runner(np.zeros((4, 5), dtype=np.float32))).transcribe(
        tmp_path / "drums.wav", tmp_path / "output", _spec()
    )

    assert result.backend_tempo is None
    assert result.midi_tempo == 120.0
    assert result.event_count == 0
    assert result.instrument_counts == {}
    assert result.first_event_s is None
    assert result.last_event_s is None
    assert json.loads(result.events_path.read_text(encoding="utf-8")) == []  # type: ignore[union-attr]
    assert _midi_has_non_percussion_notes(result.midi_path.read_bytes()) is False


def test_class_mapping_has_only_gm_percussion_primary_members() -> None:
    assert ADTOF_GM_INSTRUMENTS == {
        "bass_drum": ("kick", 36), "snare_drum": ("snare", 38),
        "tom_tom": ("mid_tom", 45), "hi_hat": ("hi_hat_closed", 42),
        "cymbal": ("crash", 49),
    }


def test_phase0_capture_beats_the_committed_drumscript_baseline_in_the_scored_window() -> None:
    root = Path(__file__).parents[1]
    with np.load(root / "docs/fixtures/adtof-phase-0/7rivers-drums-activations.npz") as capture:
        events, _notes = postprocess_adtof_activations(
            capture["activations"], _metadata(),
            default_spec_for_target(
                "drums", backend="adtof", midi_tempo=120.004,
                beat_times=[0.0853 + index * (60.0 / 120.004) for index in range(400)], downbeat_offset_s=0.0853,
            ),
        )
    ground_truth = json.loads((root / "tests/fixtures/drums_7rivers/corrected_ground_truth.json").read_text())
    baseline = json.loads((root / "tests/fixtures/drums_7rivers/drumscript_raw_events.json").read_text())
    window_end_s = 57.0
    score = score_onsets(
        instrument_onsets(ground_truth), instrument_onsets([event for event in events if event["time_sec"] <= window_end_s]),
    )
    baseline_score = score_onsets(
        instrument_onsets(ground_truth),
        instrument_onsets([event for event in baseline if event["time_sec"] <= window_end_s]),
    )

    assert score["metrics"]["global"]["f1"] > baseline_score["metrics"]["global"]["f1"]
    assert abs(score["timing"]["global"]["median_signed_error_ms"]) < abs(
        baseline_score["timing"]["global"]["median_signed_error_ms"]
    )
