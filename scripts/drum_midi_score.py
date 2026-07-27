#!/usr/bin/env python3
"""Offline scorer: compare a candidate drum MIDI/events file against ground truth.

No REAPER, no network, no model invocation -- both inputs are local files
(Standard MIDI or DrumScript event JSON). See docs/drums-transcription-timing-findings.md
for why this exists: manual REAPER listening did not catch that `drums-clean`
made timing worse while deleting ~50% of notes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from vgt.drum_evaluation import ONSET_TOLERANCE_SECONDS
from vgt.drum_midi_score import DEFAULT_WINDOW_SECONDS, read_onsets_from_path, score_onsets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="candidate .mid/.midi/.json to score")
    parser.add_argument("ground_truth", type=Path, help="ground-truth .mid/.midi/.json (human-corrected reference)")
    parser.add_argument("--tolerance", type=float, default=ONSET_TOLERANCE_SECONDS, help="onset match tolerance in seconds (default: 0.05)")
    parser.add_argument(
        "--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS,
        help="fixed window size in seconds for note-count-ratio bins; pass a bar duration (60/bpm*beats_per_bar) to bin per bar",
    )
    parser.add_argument("--output", type=Path, help="write reproducible JSON report here (otherwise stdout)")
    args = parser.parse_args(argv)

    ground_truth = read_onsets_from_path(args.ground_truth)
    candidate = read_onsets_from_path(args.candidate)
    report = score_onsets(ground_truth, candidate, tolerance=args.tolerance, window_seconds=args.window_seconds)
    report = {
        "kind": "offline-drum-midi-score",
        "candidate_path": str(args.candidate),
        "ground_truth_path": str(args.ground_truth),
        **report,
    }

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
