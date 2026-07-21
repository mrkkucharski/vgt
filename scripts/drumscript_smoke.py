#!/usr/bin/env python3
"""Explicit user-supplied-audio smoke test for the pinned DrumScript adapter."""
from __future__ import annotations

import argparse
from pathlib import Path

from vgt.transcribe import DrumScriptTranscriber, default_spec_for_target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in pinned DrumScript smoke test; it invokes uvx and may download packages.")
    parser.add_argument("audio", type=Path, help="redistributable or user-owned drum-only audio; not committed by vgt")
    parser.add_argument("output", type=Path, help="empty disposable directory for normalized outputs")
    args = parser.parse_args(argv)
    result = DrumScriptTranscriber().transcribe(args.audio, args.output, default_spec_for_target("drums", backend="drumscript"))
    print(f"OK {result.note_count} events: {result.midi_path} {result.events_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
