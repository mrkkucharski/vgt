"""Run Essentia's classical multi-pitch estimators directly on a guitar stem
and write a Basic-Pitch-shaped note-events CSV, so the raw output can be
inspected without going through a vgt project or the caching system, or
compared against a real Basic Pitch transcription of the same stem with
`scripts/guitar_transcription_probe.py`.

Thin CLI wrapper only: the actual tracking/segmentation logic lives in
`vgt.essentia_notes`, which is also what the real `guitar-klapuri`/
`guitar-melodia` transcription profiles run in production (see
`vgt.transcribe.EssentiaTranscriber`). This script exists for a fast,
zero-project look at raw output -- exactly one implementation of the
algorithm either way.

    uv run python scripts/essentia_multipitch_probe.py STEM.wav OUT.csv \
        [--algorithm klapuri|melodia] [--min-frequency 80] [--max-frequency 1200] \
        [--min-note-ms 80] [--merge-gap-ms 30]

Then compare against a Basic Pitch CSV for the same stem:

    uv run python scripts/guitar_transcription_probe.py OUT.csv basic_pitch.csv --profile guitar

Requires Essentia (`uv pip install essentia`, or the `vgt[essentia]` extra),
which is deliberately not a hard project dependency -- see
`vgt.essentia_notes`'s module docstring for why.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from vgt.essentia_notes import transcribe_multipitch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stem", type=Path, help="guitar stem WAV to transcribe")
    parser.add_argument("out_csv", type=Path, help="where to write the note-events CSV")
    parser.add_argument("--algorithm", choices=["klapuri", "melodia"], default="klapuri")
    parser.add_argument("--sample-rate", type=float, default=44100.0)
    parser.add_argument("--min-frequency", type=float, default=80.0, help="Hz, salience search floor")
    parser.add_argument("--max-frequency", type=float, default=1200.0, help="Hz, salience search ceiling")
    parser.add_argument("--min-note-ms", type=float, default=80.0, help="drop merged notes shorter than this")
    parser.add_argument("--merge-gap-ms", type=float, default=30.0, help="bridge same-pitch gaps up to this long")
    args = parser.parse_args()

    try:
        notes = transcribe_multipitch(
            str(args.stem),
            algorithm=args.algorithm,
            sample_rate_hz=args.sample_rate,
            minimum_frequency_hz=args.min_frequency,
            maximum_frequency_hz=args.max_frequency,
            minimum_note_length_ms=args.min_note_ms,
            merge_gap_ms=args.merge_gap_ms,
        )
    except ImportError as exc:
        parser.error(f"Essentia is not installed ({exc}); try `uv pip install essentia`")
        return

    lines = ["start_time_s,end_time_s,pitch_midi,velocity,pitch_bend"]
    for start_s, end_s, pitch, velocity in notes:
        lines.append(f"{start_s:.6f},{end_s:.6f},{pitch},{velocity}")
    args.out_csv.write_text("\n".join(lines) + "\n", encoding="utf-8")

    pitches = [pitch for _, _, pitch, _ in notes]
    print(f"algorithm:    {args.algorithm}")
    print(f"notes:        {len(notes)}")
    if pitches:
        print(f"pitch range:  {min(pitches)}-{max(pitches)} (MIDI)")
    print(f"wrote:        {args.out_csv}")


if __name__ == "__main__":
    main()
