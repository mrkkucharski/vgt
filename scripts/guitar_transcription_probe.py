"""Measure the quality of a Basic Pitch guitar transcription.

Evaluation-only. Reads a Basic Pitch note-events CSV and reports the four
metrics that distinguish a usable guitar reference from an unplayable one:
note count, sustain runaway, simultaneous voices, and agreement with vgt's
own detected chords. It neither runs a model nor writes into a vgt project.

    uv run python scripts/guitar_transcription_probe.py NOTES.csv [--chords chords.txt]

`--chords` points at the `chords.txt` vgt already wrote for the same project;
without it the chord-tone column is omitted. Pass several CSVs to compare
parameter variants side by side.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import re
import statistics
from pathlib import Path

# A guitar has six strings; anything above this is a detection artifact, not a
# voicing a learner could play.
GUITAR_VOICES = 6

# Intervals at which a distorted guitar's partials show up as phantom notes:
# octave, twelfth, double octave, and the next two partials.
HARMONIC_INTERVALS = (12, 19, 24, 28, 31, 36)

# Sustains longer than this are frame-threshold runaway rather than a played
# note -- only used to keep chord agreement from being dominated by one drone.
SUSTAIN_CAP_S = 4.0

PITCH_CLASSES = {
    "C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5,
    "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11,
}

Note = tuple[float, float, int, int]  # start_s, end_s, pitch, velocity


def load_notes(path: Path) -> list[Note]:
    """Read a Basic Pitch note-events CSV.

    Rows carry a variable-length trailing pitch-bend sequence, so only the
    first four columns are read and the rest ignored.
    """
    notes: list[Note] = []
    with path.open() as handle:
        reader = csv.reader(handle)
        next(reader, None)  # header
        for row in reader:
            if len(row) < 4:
                continue
            notes.append((float(row[0]), float(row[1]), int(row[2]), int(row[3])))
    return sorted(notes)


def load_chords(path: Path) -> list[tuple[float, frozenset[int]]]:
    """Read vgt's `chords.txt` into (start_seconds, pitch-class set) triads."""
    chords: list[tuple[float, frozenset[int]]] = []
    for line in path.read_text().splitlines():
        match = re.match(r"(\d+):(\d+\.\d+)\s+([A-G]#?):(\w+)", line.strip())
        if not match:
            continue
        start = int(match.group(1)) * 60 + float(match.group(2))
        root = PITCH_CLASSES[match.group(3)]
        third = 4 if match.group(4) == "maj" else 3
        chords.append((start, frozenset({root, (root + third) % 12, (root + 7) % 12})))
    return chords


def polyphony(notes: list[Note]) -> tuple[int, int, float]:
    """Peak voices, time-weighted median voices, and the share of sounding
    time spent above `GUITAR_VOICES`."""
    edges: list[tuple[float, int]] = []
    for start, end, _, _ in notes:
        edges.append((start, 1))
        edges.append((end, -1))
    edges.sort()

    held = collections.Counter()  # voice count -> seconds spent at it
    current = peak = 0
    previous: float | None = None
    for time, delta in edges:
        if previous is not None and time > previous:
            held[current] += time - previous
        current += delta
        peak = max(peak, current)
        previous = time

    total = sum(held.values()) or 1.0
    running = 0.0
    median = 0
    for count in sorted(held):
        running += held[count]
        if running >= total / 2:
            median = count
            break
    crowded = sum(seconds for count, seconds in held.items() if count > GUITAR_VOICES)
    return peak, median, crowded / total


def harmonic_ghost_share(notes: list[Note], step_s: float = 0.02) -> float:
    """Share of sounding-note samples that sit a harmonic interval above
    another note sounding at the same instant."""
    if not notes:
        return 0.0
    end_of_song = max(note[1] for note in notes)

    ghosts = sampled = 0
    active: list[Note] = []
    cursor = 0
    time = 0.0
    while time < end_of_song:
        active = [note for note in active if note[1] > time]
        while cursor < len(notes) and notes[cursor][0] <= time:
            if notes[cursor][1] > time:
                active.append(notes[cursor])
            cursor += 1
        if len(active) > 1:
            pitches = {note[2] for note in active}
            ghosts += sum(
                1 for pitch in pitches
                if any((pitch - other) in HARMONIC_INTERVALS for other in pitches)
            )
            sampled += len(pitches)
        time += step_s
    return ghosts / sampled if sampled else 0.0


def chord_tone_share(notes: list[Note], chords: list[tuple[float, frozenset[int]]]) -> float | None:
    """Share of note-time landing on a tone of the chord detected under it.

    Each note's contribution is capped at `SUSTAIN_CAP_S` so a single runaway
    drone cannot dominate the figure in either direction.
    """
    if not chords:
        return None
    boundaries = [start for start, _ in chords]
    on = off = 0.0
    for start, end, pitch, _ in notes:
        index = bisect.bisect_right(boundaries, start) - 1
        if index < 0:
            continue
        duration = min(end, start + SUSTAIN_CAP_S) - start
        if pitch % 12 in chords[index][1]:
            on += duration
        else:
            off += duration
    total = on + off
    return on / total if total else None


def report(path: Path, label: str, chords: list[tuple[float, frozenset[int]]]) -> None:
    notes = load_notes(path)
    if not notes:
        print(f"{label:<20} (no notes)")
        return
    durations = [end - start for start, end, _, _ in notes]
    peak, median, crowded = polyphony(notes)
    agreement = chord_tone_share(notes, chords)
    print(
        f"{label:<20} {len(notes):>6} {statistics.median(durations) * 1000:>8.0f}"
        f" {max(durations):>8.1f} {sum(1 for d in durations if d > 5):>6}"
        f" {peak:>8} {median:>8} {crowded * 100:>8.0f}"
        f" {harmonic_ghost_share(notes) * 100:>8.1f}"
        f" {'--' if agreement is None else format(agreement * 100, '.1f'):>11}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notes", nargs="+", type=Path, help="Basic Pitch note-events CSV(s)")
    parser.add_argument("--chords", type=Path, help="vgt chords.txt for the same project")
    args = parser.parse_args()

    chords = load_chords(args.chords) if args.chords else []
    # A parameter sweep writes one identically-named CSV per variant directory,
    # so fall back to the containing directory when the stems don't distinguish.
    stems = [path.stem for path in args.notes]
    labels = [path.parent.name for path in args.notes] if len(set(stems)) < len(stems) else stems

    print(
        f"{'variant':<20} {'notes':>6} {'med_ms':>8} {'max_s':>8} {'>5s':>6}"
        f" {'maxpoly':>8} {'medpoly':>8} {'%>6vc':>8} {'%ghost':>8} {'%chordtone':>11}"
    )
    for path, label in zip(args.notes, labels):
        report(path, label, chords)


if __name__ == "__main__":
    main()
