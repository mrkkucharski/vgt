#!/usr/bin/env python3
"""Prepare a disposable VGT/REAPER comparison project; never opens REAPER."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


PROFILES = '''schema_version = 2

[profiles.drums-hpss]
target = "drums"
extends = "drums-clean"
[profiles.drums-hpss.audio_frontend]
stages = [{ type = "hpss_blend", component = "percussive", wet = 0.6, margin = 1.0, n_fft = 2048, hop_length = 512 }]

[profiles.drums-gate]
target = "drums"
extends = "drums-clean"
[profiles.drums-gate.audio_frontend]
stages = [{ type = "soft_gate", threshold_dbfs = -45, reduction_db = 12, attack_ms = 5, release_ms = 80 }]

[profiles.bass-bandpass]
target = "bass"
extends = "bass"
[profiles.bass-bandpass.audio_frontend]
stages = [{ type = "bandpass", low_hz = 30, high_hz = 600, order = 4 }]

[profiles.bass-harmonic]
target = "bass"
extends = "bass"
[profiles.bass-harmonic.audio_frontend]
stages = [{ type = "hpss_blend", component = "harmonic", wet = 0.5 }]

[profiles.guitar-bandpass]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.guitar-bandpass.audio_frontend]
stages = [{ type = "bandpass", low_hz = 70, high_hz = 5000, order = 4 }]

[profiles.guitar-harmonic]
target = "guitar"
extends = "guitar-acoustic-clean"
[profiles.guitar-harmonic.audio_frontend]
stages = [{ type = "hpss_blend", component = "harmonic", wet = 0.5 }]
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing project directory, e.g. test/7Rivers")
    parser.add_argument("output", type=Path, help="New disposable directory; it must not already exist")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if not source.is_dir() or output.exists() or source == output:
        parser.error("source must exist and output must be a distinct, nonexistent directory")
    shutil.copytree(source, output)
    project = next(output.glob("*.RPP"), None)
    if project is None:
        parser.error("copied directory contains no .RPP project")
    profiles = project.with_suffix(".vgt-profiles.toml")
    profiles.write_text(PROFILES, encoding="utf-8")
    review = output / "stem-cleanup-review.md"
    review.write_text("# Stem cleanup REAPER review\n\nFor each candidate: solo raw/processed audio, compare MIDI, and record attacks, quiet notes, tails, bleed, timing, and artifacts.\n", encoding="utf-8")
    print(project)
    print("Next: validate profiles, add baseline/candidate variants, then open this copy in REAPER and run initialize.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
