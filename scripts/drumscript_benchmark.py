#!/usr/bin/env python3
"""Score precomputed DrumScript event JSON against an explicit JSON manifest.

The manifest intentionally keeps dataset parsing outside production code:
``{"clips": [{"id": "...", "annotations": {"kick": [0.1]}}]}``.
For every clip, ``--events-dir`` must contain ``<id>.json``.  Dataset audio is
never downloaded and DrumScript is never invoked by this command.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from vgt.drum_evaluation import evaluate_instruments, instrument_onsets, read_event_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluation-only annotated drum benchmark (no model invocation).")
    parser.add_argument("manifest", type=Path, help="checked/user-supplied annotated clip manifest JSON")
    parser.add_argument("--events-dir", required=True, type=Path, help="directory of <clip id>.json DrumScript event arrays")
    parser.add_argument("--output", type=Path, help="write reproducible JSON report here (otherwise stdout)")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    clips = manifest.get("clips") if isinstance(manifest, dict) else None
    if not isinstance(clips, list):
        parser.error("manifest must contain a clips array")
    annotations: dict[str, list[float]] = {}
    predictions: dict[str, list[float]] = {}
    for clip in clips:
        if not isinstance(clip, dict) or not isinstance(clip.get("id"), str) or not isinstance(clip.get("annotations"), dict):
            parser.error("each clip needs string id and annotations object")
        for instrument, onsets in clip["annotations"].items():
            if not isinstance(instrument, str) or not isinstance(onsets, list):
                parser.error("annotations must map instrument names to onset arrays")
            annotations.setdefault(instrument, []).extend(float(value) for value in onsets)
        event_path = args.events_dir / f"{clip['id']}.json"
        for instrument, onsets in instrument_onsets(read_event_json(event_path)).items():
            predictions.setdefault(instrument, []).extend(onsets)
    report = {"kind": "annotated-drumscript-benchmark", "clip_count": len(clips), "metrics": evaluate_instruments(annotations, predictions)}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
