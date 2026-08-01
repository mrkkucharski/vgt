"""Score the librosa section fallback against human-corrected vgt regions.

Evaluation-only: this script reads local audio and the human-verified
``analysis.sections.value`` timeline in a sidecar. It writes no project state.
Pass one or more AUDIO SIDECAR pairs; the sidecar's preserved ``detected``
timeline is the before result and the current fallback is the after result.

    uv run python scripts/section_detection_probe.py \
      "test/7Rivers/Media/The Seven Rivers (Full March - 3_00).mp3" \
      test/7Rivers/7Rivers.vgt
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from vgt.sections import _librosa_sections


BOUNDARY_TOLERANCE_SECONDS = 4.0


def _interior_boundaries(sections: list[dict[str, Any]]) -> list[float]:
    return [float(section["start_seconds"]) for section in sections[1:]]


def _score(reference: list[float], predicted: list[float]) -> dict[str, Any]:
    unmatched = set(range(len(predicted)))
    matches: list[dict[str, float]] = []
    for expected in reference:
        candidates = [
            index
            for index in unmatched
            if abs(predicted[index] - expected) <= BOUNDARY_TOLERANCE_SECONDS
        ]
        if candidates:
            match = min(candidates, key=lambda index: abs(predicted[index] - expected))
            unmatched.remove(match)
            matches.append(
                {
                    "reference_seconds": expected,
                    "predicted_seconds": predicted[match],
                    "absolute_error_seconds": abs(predicted[match] - expected),
                }
            )
    true_positives = len(matches)
    false_positives = len(predicted) - true_positives
    false_negatives = len(reference) - true_positives
    precision = true_positives / len(predicted) if predicted else 0.0
    recall = true_positives / len(reference) if reference else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "boundary_count": len(predicted),
        "region_count": len(predicted) + 1,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "boundaries_seconds": predicted,
        "matches": matches,
    }


def measure(audio: Path, sidecar: Path) -> dict[str, Any]:
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    stage = data["analysis"]["sections"]
    if not stage.get("human_verified"):
        raise ValueError(f"{sidecar}: sections are not human-verified")
    reference = _interior_boundaries(stage["value"])
    baseline = _interior_boundaries(stage["detected"])
    boundaries, _labels = _librosa_sections(audio, {})
    current = [float(boundary) for boundary in boundaries[1:-1]]
    return {
        "audio": str(audio),
        "sidecar": str(sidecar),
        "tolerance_seconds": BOUNDARY_TOLERANCE_SECONDS,
        "reference": {
            "boundary_count": len(reference),
            "region_count": len(reference) + 1,
            "boundaries_seconds": reference,
        },
        "baseline": _score(reference, baseline),
        "current": _score(reference, current),
    }


def _summary(reports: list[dict[str, Any]]) -> str:
    lines = [
        "song                                      result    regions  tp fp fn  precision recall    f1",
    ]
    for report in reports:
        name = Path(report["audio"]).stem
        for result_name in ("baseline", "current"):
            result = report[result_name]
            lines.append(
                f"{name[:40]:<40}  {result_name:<8} {result['region_count']:>7} "
                f"{result['true_positives']:>3} {result['false_positives']:>2} "
                f"{result['false_negatives']:>2} {result['precision']:>10.3f} "
                f"{result['recall']:>6.3f} {result['f1']:>6.3f}"
            )
    for result_name in ("baseline", "current"):
        macro_f1 = sum(report[result_name]["f1"] for report in reports) / len(reports)
        lines.append(f"macro {result_name:<8} f1={macro_f1:.3f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+", metavar="PATH", help="AUDIO SIDECAR pair(s)")
    parser.add_argument("--output", type=Path, help="write the complete machine-readable JSON report")
    args = parser.parse_args(argv)
    if len(args.paths) % 2:
        parser.error("paths must be AUDIO SIDECAR pairs")
    reports = [measure(*args.paths[index : index + 2]) for index in range(0, len(args.paths), 2)]
    print(_summary(reports))
    if args.output:
        args.output.write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
