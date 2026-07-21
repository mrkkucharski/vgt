#!/usr/bin/env python3
"""Create a temporary, evaluation-only unlabeled onset comparison report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from vgt.drum_evaluation import read_event_json, shadow_comparison
from vgt.transcribe import parse_notes_csv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Temporary evaluation-only DrumScript/Basic Pitch onset comparison.")
    parser.add_argument("drumscript_events", type=Path)
    parser.add_argument("basic_pitch_notes", type=Path)
    parser.add_argument("--output", type=Path, help="temporary report path; never use a vgt artifact/sidecar path")
    args = parser.parse_args(argv)
    report = shadow_comparison(read_event_json(args.drumscript_events), (note.start_s for note in parse_notes_csv(args.basic_pitch_notes)))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
