"""Before/after regression coverage for issue #183: `drums-clean`'s
onset-evidence normalization changed from `local_peak / global_envelope_max`
to a local, relative prominence measure. This uses the #182 offline scorer
(`vgt.drum_midi_score`) against a synthetic fixture -- built here rather than
committed as a binary, so the fixture and its ground truth stay in one place
-- to turn "the fix helps" into a measured delta instead of a listening
guess, mirroring the real 7Rivers shape: many genuine, moderate-strength
hits and a couple of much louder crash/fill transients."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from vgt.drum_cleanup import (
    CLEAN_PROFILE_NAME,
    DRUM_CLEANUP_PROFILES,
    AudioOnsetEvidenceSource,
    apply_drum_cleanup,
)
from vgt.drum_midi_score import score_onsets

CLEAN = DRUM_CLEANUP_PROFILES[CLEAN_PROFILE_NAME]

# The exact thresholds this issue retunes (see drum_cleanup.py); pinned here,
# independent of the module's current constants, so this regression test
# keeps measuring the real "before" even if the module's values move again.
_PRE_FIX_MIN_EVIDENCE_STRENGTH = 0.35
_PRE_FIX_SUPPRESSION_STRENGTH_THRESHOLD = 0.12


class _PreFixGlobalMaxEvidenceSource(AudioOnsetEvidenceSource):
    """Reproduces the pre-#183 `local_peak / global_envelope_max` strength
    formula, reusing the base class's real librosa envelope loading so the
    only difference from the fixed source is the normalization itself."""

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


def _build_synthetic_drum_stem(path: Path) -> tuple[list[float], float]:
    """A ~20s mix of many quiet-but-genuine hits and two dominant crash
    transients -- the same shape (per-onset strength swamped by a handful of
    much louder transients) measured on the real 7Rivers stem. Returns
    `(all_onset_times, crash_time)`."""
    import numpy as np
    import soundfile as sf

    sr = 22050
    duration_s = 20.0
    samples = np.zeros(int(sr * duration_s), dtype=np.float32)
    rng = np.random.default_rng(0)
    moderate_times = [round(t, 2) for t in list(_frange(1.0, 19.0, 0.7))]
    crash_time = 9.5
    amps = [0.002, 0.003, 0.005, 0.008]

    def burst(time_sec: float, amp: float, length: int = 400) -> None:
        index = int(time_sec * sr)
        samples[index:index + length] += rng.standard_normal(length).astype("float32") * amp

    for i, t in enumerate(moderate_times):
        burst(t, amps[i % len(amps)])
    burst(crash_time, 30.0, length=3000)

    sf.write(str(path), samples, sr, subtype="PCM_16")
    return sorted(moderate_times + [crash_time]), crash_time


def _frange(start: float, stop: float, step: float):
    value = start
    while value < stop:
        yield value
        value += step


def test_local_prominence_normalization_beats_global_max_on_the_182_scorer(tmp_path: Path) -> None:
    path = tmp_path / "synthetic_drum_stem.wav"
    ground_truth_times, _crash_time = _build_synthetic_drum_stem(path)
    ground_truth = {"kick": ground_truth_times}
    raw_events = [{"time_sec": t, "instruments": ["kick"]} for t in ground_truth_times]

    pre_fix_profile = replace(
        CLEAN,
        min_evidence_strength=_PRE_FIX_MIN_EVIDENCE_STRENGTH,
        suppression_strength_threshold=_PRE_FIX_SUPPRESSION_STRENGTH_THRESHOLD,
    )
    pre_fix_cleaned = apply_drum_cleanup(
        raw_events, profile=pre_fix_profile, evidence_source=_PreFixGlobalMaxEvidenceSource(path)
    )
    fixed_cleaned = apply_drum_cleanup(raw_events, profile=CLEAN, evidence_source=AudioOnsetEvidenceSource(path))

    def candidate_times(cleaned) -> dict[str, list[float]]:
        return {"kick": [event.time_sec for event in cleaned if not event.suppressed]}

    def suppressed_count(cleaned) -> int:
        return sum(1 for event in cleaned if event.suppressed)

    pre_fix_report = score_onsets(ground_truth, candidate_times(pre_fix_cleaned))
    fixed_report = score_onsets(ground_truth, candidate_times(fixed_cleaned))

    pre_fix_recall = pre_fix_report["metrics"]["global"]["recall"]
    fixed_recall = fixed_report["metrics"]["global"]["recall"]
    pre_fix_suppressed = suppressed_count(pre_fix_cleaned)
    fixed_suppressed = suppressed_count(fixed_cleaned)

    # The pre-fix global-max normalization reproduces the reported failure
    # mode: it suppresses a large share of genuine hits, and recall suffers.
    assert pre_fix_suppressed >= len(ground_truth_times) // 3
    assert pre_fix_recall < 0.75

    # The local-prominence fix keeps the large majority of genuine hits, and
    # recall improves substantially.
    assert fixed_suppressed <= len(ground_truth_times) // 6
    assert fixed_recall > 0.85
    assert fixed_recall > pre_fix_recall
    assert fixed_suppressed < pre_fix_suppressed
