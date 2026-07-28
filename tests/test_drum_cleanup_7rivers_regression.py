"""Before/after regression for issue #183 against the real 7Rivers project
data committed in `tests/fixtures/drums_7rivers/` (added for #182-#185, see
that directory's README for provenance). Unlike
`test_drum_cleanup_normalization_regression.py`'s synthetic fixture, this
replays the *actual* onset-strength envelope that reproduces the reported
global-max-dominance pathology (global_max ~24.6, median ~0.09) against
DrumScript's real raw events and the maintainer's real hand-corrected ground
truth -- entirely offline, no audio, no REAPER, no model."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from vgt.drum_cleanup import (
    CLEAN_PROFILE_NAME,
    DRUM_CLEANUP_PROFILES,
    AudioOnsetEvidenceSource,
    apply_drum_cleanup,
)
from vgt.drum_evaluation import evaluate_instruments, instrument_onsets

FIXTURES = Path(__file__).parent / "fixtures" / "drums_7rivers"
CLEAN = DRUM_CLEANUP_PROFILES[CLEAN_PROFILE_NAME]

# The exact thresholds this issue retunes (see drum_cleanup.py); pinned here
# so this regression keeps measuring the real "before" even if the module's
# current constants move again.
_PRE_FIX_MIN_EVIDENCE_STRENGTH = 0.35
_PRE_FIX_SUPPRESSION_STRENGTH_THRESHOLD = 0.12


class _PreFixGlobalMaxEvidenceSource(AudioOnsetEvidenceSource):
    """Reproduces the pre-#183 `local_peak / global_envelope_max` strength
    formula over an injected envelope (no real audio decoding involved)."""

    def evidence_near(self, time_sec: float, window_s: float):
        from vgt.drum_cleanup import OnsetEvidence, _clamp

        if self._envelope is None or self._sr is None:
            return OnsetEvidence(time_sec=None, strength=None)
        frame_time = self._hop_length / self._sr
        center = time_sec / frame_time
        span = max(1, int(round(window_s / frame_time)))
        lo = max(0, int(round(center)) - span)
        hi = min(len(self._envelope), int(round(center)) + span + 1)
        if hi <= lo:
            return OnsetEvidence(time_sec=None, strength=None)
        window = self._envelope[lo:hi]
        envelope_max = float(max(self._envelope))
        peak_offset = max(range(len(window)), key=lambda index: window[index])
        peak_value = float(window[peak_offset])
        sorted_values = sorted((float(value) for value in window), reverse=True)
        if len(sorted_values) > 1 and sorted_values[0] > 0 and (sorted_values[1] / sorted_values[0]) > 0.9:
            return OnsetEvidence(time_sec=None, strength=None)
        strength = _clamp(peak_value / envelope_max, 0.0, 1.0)
        return OnsetEvidence(time_sec=(lo + peak_offset) * frame_time, strength=strength)


def _load_envelope_source(cls: type[AudioOnsetEvidenceSource]) -> AudioOnsetEvidenceSource:
    payload = json.loads((FIXTURES / "onset_strength_envelope.json").read_text())
    source = cls(Path("7rivers-drum-stem-not-committed.wav"), hop_length=payload["hop_length"])
    source._prepare_envelope(payload["strengths"], payload["sr"])
    return source


def _load_raw_events() -> list[dict]:
    return json.loads((FIXTURES / "drumscript_raw_events.json").read_text())


def _load_ground_truth() -> dict[str, list[float]]:
    notes = json.loads((FIXTURES / "corrected_ground_truth.json").read_text())
    truth: dict[str, list[float]] = {}
    for note in notes:
        for instrument in note["instruments"]:
            truth.setdefault(instrument, []).append(note["time_sec"])
    return truth


def test_local_prominence_normalization_beats_global_max_on_real_7rivers_data() -> None:
    """DrumScript's raw instrument labels don't always match the
    human-corrected ground truth's (issue #183's root cause #4 is a
    separate, backend-classification problem), so absolute precision/recall
    against `corrected_ground_truth.json` is low for every variant compared
    here. What this issue's fix controls is suppression: how much of
    whatever the *raw*, uncleaned output could have matched survives
    cleanup. That's measured as true-positive count relative to the raw
    (uncleaned) baseline -- suppression should only remove events that
    don't correspond to a real ground-truth match, never a true positive."""
    raw_events = _load_raw_events()
    ground_truth = _load_ground_truth()
    raw_report = evaluate_instruments(ground_truth, instrument_onsets(raw_events))

    pre_fix_profile = replace(
        CLEAN,
        min_evidence_strength=_PRE_FIX_MIN_EVIDENCE_STRENGTH,
        suppression_strength_threshold=_PRE_FIX_SUPPRESSION_STRENGTH_THRESHOLD,
    )
    pre_fix_cleaned = apply_drum_cleanup(
        raw_events, profile=pre_fix_profile, evidence_source=_load_envelope_source(_PreFixGlobalMaxEvidenceSource)
    )
    fixed_cleaned = apply_drum_cleanup(
        raw_events, profile=CLEAN, evidence_source=_load_envelope_source(AudioOnsetEvidenceSource)
    )

    def candidate(cleaned) -> dict[str, list[float]]:
        return instrument_onsets(
            [{"time_sec": event.time_sec, "instruments": [event.instrument]} for event in cleaned if not event.suppressed]
        )

    pre_fix_report = evaluate_instruments(ground_truth, candidate(pre_fix_cleaned))
    fixed_report = evaluate_instruments(ground_truth, candidate(fixed_cleaned))

    raw_true_positives = raw_report["global"]["true_positives"]
    pre_fix_true_positives = pre_fix_report["global"]["true_positives"]
    fixed_true_positives = fixed_report["global"]["true_positives"]

    # Reproduces the reported pathology: global-max normalization's
    # suppression destroys the large majority of the raw output's real
    # (ground-truth-matching) hits, not just its false positives.
    assert pre_fix_true_positives < raw_true_positives * 0.5

    # The local-prominence fix's suppression costs (essentially) none of the
    # raw output's real hits, while still trimming false positives, so
    # precision improves over the raw baseline without sacrificing recall.
    assert fixed_true_positives >= raw_true_positives * 0.9
    assert fixed_report["global"]["false_positives"] < raw_report["global"]["false_positives"]
    assert fixed_report["global"]["precision"] > raw_report["global"]["precision"]
    assert fixed_report["global"]["recall"] > pre_fix_report["global"]["recall"]
