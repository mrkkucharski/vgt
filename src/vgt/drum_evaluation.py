"""Offline, evaluation-only utilities for the deferred DrumScript decision.

Nothing in this module is used by ``vgt analyze`` or by sidecar persistence.
It consumes exported events and annotations to make the backend-selection
decision auditable without treating Basic Pitch as a drum classifier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Iterable


ONSET_TOLERANCE_SECONDS = 0.050
DEFAULT_CLUSTER_WINDOW_SECONDS = 0.020


@dataclass(frozen=True)
class InstrumentMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


def _metrics(expected: Iterable[float], observed: Iterable[float], tolerance: float) -> InstrumentMetrics:
    """One-to-one matching that maximizes matches, then minimizes distance.

    A nearest-first greedy choice can leave a later reference without a valid
    prediction even though a complete matching exists.  The dynamic programme
    below first maximizes the number of matches, then chooses the least total
    onset error; its final action ordering keeps tied reports reproducible.
    """
    refs = sorted(float(value) for value in expected)
    preds = sorted(float(value) for value in observed)
    # Each cell is (matches, accumulated_error).  Sort by negative matches so
    # ``min`` selects the objective above; action order settles exact ties.
    score: list[list[tuple[int, float]]] = [[(0, 0.0) for _ in range(len(preds) + 1)] for _ in range(len(refs) + 1)]
    for ref_index in range(len(refs) - 1, -1, -1):
        for pred_index in range(len(preds) - 1, -1, -1):
            candidates = [score[ref_index + 1][pred_index], score[ref_index][pred_index + 1]]
            distance = abs(refs[ref_index] - preds[pred_index])
            if distance <= tolerance:
                matched, error = score[ref_index + 1][pred_index + 1]
                candidates.append((matched + 1, error + distance))
            score[ref_index][pred_index] = min(candidates, key=lambda item: (-item[0], item[1]))
    matches = score[0][0][0]
    fp = len(preds) - matches
    fn = len(refs) - matches
    precision = matches / (matches + fp) if matches + fp else 0.0
    recall = matches / (matches + fn) if matches + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return InstrumentMetrics(matches, fp, fn, precision, recall, f1)


def evaluate_instruments(
    annotations: dict[str, list[float]], predictions: dict[str, list[float]], *, tolerance: float = ONSET_TOLERANCE_SECONDS
) -> dict[str, object]:
    """Score each declared instrument and provide transparent macro/global views."""
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    instruments = sorted(set(annotations) | set(predictions))
    per_instrument = {
        instrument: _metrics(annotations.get(instrument, []), predictions.get(instrument, []), tolerance)
        for instrument in instruments
    }
    all_metrics = list(per_instrument.values())
    totals = InstrumentMetrics(
        sum(metric.true_positives for metric in all_metrics),
        sum(metric.false_positives for metric in all_metrics),
        sum(metric.false_negatives for metric in all_metrics),
        0.0, 0.0, 0.0,
    )
    precision = totals.true_positives / (totals.true_positives + totals.false_positives) if totals.true_positives + totals.false_positives else 0.0
    recall = totals.true_positives / (totals.true_positives + totals.false_negatives) if totals.true_positives + totals.false_negatives else 0.0
    global_metrics = InstrumentMetrics(
        totals.true_positives, totals.false_positives, totals.false_negatives, precision, recall,
        2 * precision * recall / (precision + recall) if precision + recall else 0.0,
    )
    macro = {
        "precision": sum(metric.precision for metric in all_metrics) / len(all_metrics) if all_metrics else 0.0,
        "recall": sum(metric.recall for metric in all_metrics) / len(all_metrics) if all_metrics else 0.0,
        "f1": sum(metric.f1 for metric in all_metrics) / len(all_metrics) if all_metrics else 0.0,
    }
    return {"tolerance_seconds": tolerance, "per_instrument": {key: asdict(value) for key, value in per_instrument.items()}, "macro": macro, "global": asdict(global_metrics)}


def instrument_onsets(events: list[object]) -> dict[str, list[float]]:
    """Extract labeled onsets from DrumScript's normalized event JSON."""
    result: dict[str, list[float]] = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("time_sec"), (int, float)):
            raise ValueError("event must contain numeric time_sec")
        instruments = event.get("instruments")
        if not isinstance(instruments, list) or not all(isinstance(value, str) for value in instruments):
            raise ValueError("event must contain an instruments string list")
        for instrument in instruments:
            result.setdefault(instrument, []).append(float(event["time_sec"]))
    return result


def collapse_basic_pitch_starts(starts: Iterable[float], *, window: float = DEFAULT_CLUSTER_WINDOW_SECONDS) -> list[float]:
    """Collapse nearby pitched-note starts into unlabeled transient clusters.

    The earliest start is retained as each cluster's timestamp.  Pitches and
    velocities are deliberately absent: this is not a drum-instrument map.
    """
    if window < 0:
        raise ValueError("cluster window cannot be negative")
    clusters: list[float] = []
    for start in sorted(float(value) for value in starts):
        if not clusters or start - clusters[-1] > window:
            clusters.append(start)
    return clusters


def drumscript_onsets(events: list[object]) -> list[float]:
    """Return one onset per DrumScript event, preserving no class judgement."""
    return sorted(float(event["time_sec"]) for event in events if isinstance(event, dict) and "time_sec" in event)


def shadow_comparison(
    drumscript_events: list[object], basic_pitch_starts: Iterable[float], *,
    tolerance: float = ONSET_TOLERANCE_SECONDS, cluster_window: float = DEFAULT_CLUSTER_WINDOW_SECONDS,
) -> dict[str, object]:
    """Temporary evaluation-only agreement counts; never a confidence score."""
    drums = drumscript_onsets(drumscript_events)
    basic = collapse_basic_pitch_starts(basic_pitch_starts, window=cluster_window)
    metrics = _metrics(drums, basic, tolerance)
    return {
        "kind": "temporary-evaluation-only-shadow-comparison",
        "tolerance_seconds": tolerance,
        "cluster_window_seconds": cluster_window,
        "drumscript_onsets": len(drums),
        "basic_pitch_transient_clusters": len(basic),
        "matched": metrics.true_positives,
        "drumscript_only": metrics.false_negatives,
        "basic_pitch_only": metrics.false_positives,
    }


def read_event_json(path: Path) -> list[object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"{path}: event JSON must be an array")
    return value
