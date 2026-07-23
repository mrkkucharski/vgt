"""Measure the quality of a Basic Pitch transcription against a named profile.

Evaluation-only. Reads a Basic Pitch note-events CSV and reports the metrics
that distinguish a usable instrument reference from an unplayable one: note count,
sustain runaway, simultaneous voices, fragmentation, harmonic ghosts, and
agreement with vgt's own detected chords. It neither runs a model nor writes
into a vgt project.

    uv run python scripts/guitar_transcription_probe.py NOTES.csv [--profile guitar-acoustic] [--chords chords.txt]

`--chords` points at the `chords.txt` vgt already wrote for the same project;
without it the chord-tone columns are omitted. Pass several CSVs to compare
parameter variants side by side.

The profile defaults to `guitar-acoustic`: that preserves the exact bounds
used for the published guitar findings, so profile-less historical commands
remain comparable. Profiles without measured probe expectations are rejected
and the error names the profiles this evaluation harness can use.

Two chord-agreement columns are reported, and the difference matters:

* `%ct-on` attributes each note wholly to the chord under its *onset*. This
  is biased against long notes -- a note ringing across a chord change is
  penalised for its whole length -- so it makes a merged transcription look
  worse than the fragmented one it came from, even when nothing changed
  musically.
* `%ct-t` samples every 20 ms against the chord sounding at that instant.
  Use this one whenever the variants being compared differ in note length.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import re
import statistics
from pathlib import Path

from vgt.transcribe import ProbeExpectations, TranscriptionError, VALID_PROFILE_NAMES, instrument_profile

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


def polyphony(notes: list[Note], expected_voice_count: int) -> tuple[int, int, float]:
    """Peak voices, time-weighted median voices, and the share of sounding
    time spent above the selected profile's expected voice count."""
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
    crowded = sum(seconds for count, seconds in held.items() if count > expected_voice_count)
    return peak, median, crowded / total


def harmonic_ghost_share(notes: list[Note], harmonic_intervals: tuple[int, ...], step_s: float = 0.02) -> float:
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
                if any((pitch - other) in harmonic_intervals for other in pitches)
            )
            sampled += len(pitches)
        time += step_s
    return ghosts / sampled if sampled else 0.0


def chord_tone_share_onset(
    notes: list[Note], chords: list[tuple[float, frozenset[int]]], sustain_cap_s: float
) -> float | None:
    """Share of note-time on a chord tone, attributing each note wholly to the
    chord under its onset.

    Biased against long notes -- see the module docstring. Retained because
    the original tuning sweep was scored this way; prefer
    `chord_tone_share_timewise` when note lengths differ between variants.
    """
    if not chords:
        return None
    boundaries = [start for start, _ in chords]
    on = off = 0.0
    for start, end, pitch, _ in notes:
        index = bisect.bisect_right(boundaries, start) - 1
        if index < 0:
            continue
        duration = min(end, start + sustain_cap_s) - start
        if pitch % 12 in chords[index][1]:
            on += duration
        else:
            off += duration
    total = on + off
    return on / total if total else None


def chord_tone_share_timewise(
    notes: list[Note], chords: list[tuple[float, frozenset[int]]], sustain_cap_s: float, step_s: float = 0.02
) -> float | None:
    """Share of sounding time on a chord tone, scored against the chord
    actually sounding at each sampled instant.

    Length-neutral: a note held across a chord change is credited correctly on
    both sides of the boundary instead of being attributed entirely to the
    chord it started under.
    """
    if not chords:
        return None
    boundaries = [start for start, _ in chords]
    on = off = 0
    for start, end, pitch, _ in notes:
        time = start
        limit = min(end, start + sustain_cap_s)
        while time < limit:
            index = bisect.bisect_right(boundaries, time) - 1
            if index >= 0:
                if pitch % 12 in chords[index][1]:
                    on += 1
                else:
                    off += 1
            time += step_s
    total = on + off
    return on / total if total else None


def fragmentation_count(notes: list[Note], max_gap_s: float = 0.03) -> int:
    """Adjacent same-pitch pairs separated by no more than `max_gap_s` -- i.e.
    one held note the model emitted as several. Should be 0 once the merge
    pass has run."""
    by_pitch: dict[int, list[tuple[float, float]]] = collections.defaultdict(list)
    for start, end, pitch, _ in notes:
        by_pitch[pitch].append((start, end))
    fragments = 0
    for spans in by_pitch.values():
        spans.sort()
        fragments += sum(1 for i in range(1, len(spans)) if spans[i][0] - spans[i - 1][1] <= max_gap_s)
    return fragments


def report(
    path: Path, label: str, chords: list[tuple[float, frozenset[int]]], expectations: ProbeExpectations
) -> None:
    notes = load_notes(path)
    if not notes:
        print(f"{label:<20} (no notes)")
        return
    durations = [end - start for start, end, _, _ in notes]
    peak, median, crowded = polyphony(notes, expectations.expected_voice_count)
    onset_share = chord_tone_share_onset(notes, chords, expectations.sustain_cap_s)
    time_share = chord_tone_share_timewise(notes, chords, expectations.sustain_cap_s)
    fmt = lambda value: "--" if value is None else format(value * 100, ".1f")  # noqa: E731
    print(
        f"{label:<20} {len(notes):>6} {statistics.median(durations) * 1000:>8.0f}"
        f" {max(durations):>8.1f} {sum(1 for d in durations if d > 5):>6}"
        f" {peak:>8} {median:>8} {crowded * 100:>8.0f}"
        f" {fragmentation_count(notes):>6}"
        f" {harmonic_ghost_share(notes, expectations.harmonic_ghost_intervals) * 100:>8.1f}"
        f" {fmt(onset_share):>7} {fmt(time_share):>7}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notes", nargs="+", type=Path, help="Basic Pitch note-events CSV(s)")
    parser.add_argument(
        "--profile",
        default="guitar-acoustic",
        help="instrument profile for evaluation (default: guitar-acoustic)",
    )
    parser.add_argument("--chords", type=Path, help="vgt chords.txt for the same project")
    args = parser.parse_args()

    try:
        profile = instrument_profile(args.profile)
    except TranscriptionError as error:
        parser.error(f"{error}; available profiles: {', '.join(VALID_PROFILE_NAMES)}")
    expectations = profile.probe_expectations
    if expectations is None:
        available = ", ".join(
            name for name in VALID_PROFILE_NAMES if instrument_profile(name).probe_expectations is not None
        )
        parser.error(f"profile {args.profile!r} has no measured probe expectations; available: {available}")

    chords = load_chords(args.chords) if args.chords else []
    # A parameter sweep writes one identically-named CSV per variant directory,
    # so fall back to the containing directory when the stems don't distinguish.
    stems = [path.stem for path in args.notes]
    labels = [path.parent.name for path in args.notes] if len(set(stems)) < len(stems) else stems

    print(
        f"{'variant':<20} {'notes':>6} {'med_ms':>8} {'max_s':>8} {'>5s':>6}"
        f" {'maxpoly':>8} {'medpoly':>8} {'%>{expectations.expected_voice_count}vc':>8} {'frag':>6} {'%ghost':>8}"
        f" {'%ct-on':>7} {'%ct-t':>7}"
    )
    for path, label in zip(args.notes, labels):
        report(path, label, chords, expectations)


if __name__ == "__main__":
    main()
