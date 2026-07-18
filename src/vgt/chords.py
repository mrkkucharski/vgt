"""Beat-aligned maj/min chord detection: madmom's CNN chord recognizer is the
primary backend (optional -- see the `madmom` extra in pyproject.toml, and
tempo.py's docstring for why it's isolated); Chordino via `sonic-annotator`
(also optional -- neither ships a pip package, so it's a system binary +
vamp-plugin check) is the next fallback; a chroma + template-matching
classifier is the always-available last resort. Only major/minor triads are
ever recognized -- 7ths, sus, add9, etc. all collapse to their nearest maj/min
match -- so every result is flagged `"vocabulary": "maj_min"`.

Beat-alignment: chord segment boundaries are always snapped to the shared
beat grid detected by the tempo stage (tempo.py, #8) -- `detect_chords` takes
that grid's beat times as an explicit argument rather than detecting its own,
so key/chord/tempo all agree on one grid (see `analysis.py`'s
`_tempo_beat_times`). This applies uniformly to every backend, including
madmom, whose own chord segmentation isn't beat-quantized.
"""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any

_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_NO_CHORD = "N"


class ChordDetectionError(RuntimeError):
    """No backend could produce a chord sequence for the source audio."""


def _madmom_chords(source: Path) -> list[tuple[float, float, str]] | None:
    """Beat-independent (start, end, label) triples via madmom's CNN chord
    recognizer, or None if madmom isn't importable, or fails at runtime, in
    this environment (mirrors tempo.py's madmom fallback rule: import errors
    and runtime failures both mean "unusable here")."""
    try:
        from madmom.audio.chroma import DeepChromaProcessor  # type: ignore[import-not-found]
        from madmom.features.chords import DeepChromaChordRecognitionProcessor  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        chroma = DeepChromaProcessor()(str(source))
        chords = DeepChromaChordRecognitionProcessor()(chroma)
    except Exception:
        return None
    return [(float(start), float(end), str(label)) for start, end, label in chords]


def _chordino_chords(source: Path) -> list[tuple[float, float, str]] | None:
    """Chordino's maj/min chord sequence via `sonic-annotator` (the vamp
    plugin host) -- the issue's specified fallback when madmom isn't usable.
    Neither `sonic-annotator` nor the nnls-chroma vamp plugin ship as pip
    packages, so most environments will fall through this to the
    chroma-template classifier below; a missing binary, missing plugin, or
    any runtime failure all mean "unusable here", same as the madmom path."""
    import shutil
    import subprocess

    if shutil.which("sonic-annotator") is None:
        return None
    try:
        result = subprocess.run(
            [
                "sonic-annotator",
                "-d",
                "vamp:nnls-chroma:chordino:simplechord",
                str(source),
                "-w",
                "csv",
                "--csv-stdout",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=True,
        )
    except Exception:
        return None
    return _parse_chordino_csv(result.stdout)


def _parse_chordino_csv(csv_text: str) -> list[tuple[float, float, str]] | None:
    """Chordino's `simplechord` transform emits one row per chord *change*
    (a timestamp + label, no duration) -- consecutive rows are paired into
    (start, end, label) segments."""
    import csv
    import io

    rows = [row for row in csv.reader(io.StringIO(csv_text)) if row]
    events: list[tuple[float, str]] = []
    for row in rows:
        try:
            time = float(row[1])
        except (IndexError, ValueError):
            continue
        label = row[-1].strip().strip('"')
        events.append((time, label))
    if len(events) < 2:
        return None

    segments: list[tuple[float, float, str]] = []
    for (start, label), (end, _next_label) in zip(events, events[1:]):
        if label and label != _NO_CHORD:
            segments.append((start, end, label))
    return segments or None


def _chord_templates() -> list[tuple[str, Any]]:
    import numpy as np

    templates = []
    for i, root in enumerate(_PITCH_CLASSES):
        major = np.zeros(12)
        major[[i, (i + 4) % 12, (i + 7) % 12]] = 1.0
        minor = np.zeros(12)
        minor[[i, (i + 3) % 12, (i + 7) % 12]] = 1.0
        templates.append((f"{root}:maj", major))
        templates.append((f"{root}:min", minor))
    return templates


def _best_chord(chroma_vector, templates) -> str:
    import numpy as np

    vector = np.asarray(chroma_vector, dtype=float)
    norm = float(np.linalg.norm(vector))
    if norm < 1e-6:
        return _NO_CHORD
    vector = vector / norm

    best_label, best_score = _NO_CHORD, -1.0
    for label, template in templates:
        template_norm = template / np.linalg.norm(template)
        score = float(np.dot(vector, template_norm))
        if score > best_score:
            best_label, best_score = label, score
    return best_label


_SMOOTHING_WINDOW = 5  # beats; odd, so each label has an equal look-ahead/behind


def _smooth_labels(labels: list[str], window: int = _SMOOTHING_WINDOW) -> list[str]:
    """Per-beat chord classification is noisy (single-beat chroma windows
    flip between e.g. a root's major/minor reading from frame to frame) --
    a majority-vote filter over a small neighborhood turns that chatter into
    stable, musically plausible runs before segments are merged."""
    from collections import Counter

    half = window // 2
    n = len(labels)
    return [Counter(labels[max(0, i - half) : min(n, i + half + 1)]).most_common(1)[0][0] for i in range(n)]


def _template_chords(source: Path, beat_times: list[float]) -> list[tuple[float, float, str]]:
    """Always-available fallback: chroma of the harmonic component
    (percussive transients hurt chord templates) averaged within each
    beat-to-beat window of the shared grid, classified against the 24
    maj/min triad templates, majority-vote smoothed across neighboring
    beats, then run-length-merged into segments."""
    import librosa
    import numpy as np

    y, sr = librosa.load(str(source), sr=None, mono=True)
    if len(y) == 0:
        raise ChordDetectionError(f"{source}: audio contains no samples.")
    duration = librosa.get_duration(y=y, sr=sr)

    y_harmonic = librosa.effects.harmonic(y, margin=8)
    chroma = librosa.feature.chroma_cqt(y=y_harmonic, sr=sr)
    frame_times = librosa.frames_to_time(np.arange(chroma.shape[1]), sr=sr)

    boundaries = sorted({round(t, 6) for t in [0.0, *beat_times, duration]})
    templates = _chord_templates()

    raw_labels: list[str] = []
    raw_bounds: list[tuple[float, float]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        mask = (frame_times >= start) & (frame_times < end)
        if not mask.any():
            continue
        raw_labels.append(_best_chord(chroma[:, mask].mean(axis=1), templates))
        raw_bounds.append((start, end))

    smoothed_labels = _smooth_labels(raw_labels)

    segments: list[tuple[float, float, str]] = []
    for (start, end), label in zip(raw_bounds, smoothed_labels):
        if segments and segments[-1][2] == label:
            segments[-1] = (segments[-1][0], end, label)
        else:
            segments.append((start, end, label))
    return segments


def _snap_to_grid(time: float, grid: list[float]) -> float:
    """Nearest point on the shared beat grid to `time` (grid must be sorted)."""
    idx = bisect.bisect_left(grid, time)
    candidates = [grid[i] for i in (idx - 1, idx) if 0 <= i < len(grid)]
    return min(candidates, key=lambda t: abs(t - time))


def _snap_segments_to_grid(
    segments: list[tuple[float, float, str]], grid: list[float]
) -> list[dict[str, Any]]:
    """Snap every segment boundary to the shared beat grid, then re-merge
    adjacent same-label segments that snapping collapsed together (and drop
    any segment that snapped to zero length)."""
    snapped: list[dict[str, Any]] = []
    for start, end, label in segments:
        grid_start = _snap_to_grid(start, grid)
        grid_end = _snap_to_grid(end, grid)
        if grid_end <= grid_start:
            continue
        if snapped and snapped[-1]["chord"] == label and snapped[-1]["end_seconds"] >= grid_start:
            snapped[-1]["end_seconds"] = max(snapped[-1]["end_seconds"], grid_end)
        else:
            snapped.append({"start_seconds": grid_start, "end_seconds": grid_end, "chord": label})
    for segment in snapped:
        segment["start_seconds"] = round(segment["start_seconds"], 6)
        segment["end_seconds"] = round(segment["end_seconds"], 6)
    return snapped


def detect_chords(source: Path, beat_times: list[float], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return `{"segments", "beat_times", "vocabulary", "backend"}` for
    `source`, with every chord segment boundary snapped to `beat_times` --
    the shared grid from the tempo stage (see module docstring)."""
    del settings  # no tunables yet; accepted for a uniform detector signature
    if not source.is_file():
        raise ChordDetectionError(f"Reference source file not found: {source}")
    grid = sorted(beat_times)
    if len(grid) < 2:
        raise ChordDetectionError(f"Too few beats in the shared grid ({len(grid)}) to snap chords to.")

    madmom_result = _madmom_chords(source)
    if madmom_result is not None:
        raw_segments, backend = madmom_result, "madmom"
    else:
        chordino_result = _chordino_chords(source)
        if chordino_result is not None:
            raw_segments, backend = chordino_result, "chordino"
        else:
            raw_segments, backend = _template_chords(source, grid), "librosa"

    segments = _snap_segments_to_grid(raw_segments, grid)

    return {
        "segments": segments,
        "beat_times": [round(t, 6) for t in grid],
        "vocabulary": "maj_min",
        "backend": backend,
    }


def chord_sheet_path(project_path: Path) -> Path:
    """Verification artifact lives next to the sidecar, not inside the
    project's own Media folder -- it is vgt-owned, not part of the song."""
    return project_path.with_name(f"{project_path.stem}.vgt-chords.txt")


def _format_timestamp(seconds: float) -> str:
    minutes, remainder = divmod(max(seconds, 0.0), 60)
    return f"{int(minutes):02d}:{remainder:05.2f}"


def render_chord_sheet(chords_value: dict[str, Any], destination: Path) -> Path:
    """Write a plain-text chord sheet (timestamp + label per segment) for
    by-eye verification of the detected chord sequence."""
    lines = [
        f"# vocabulary: {chords_value['vocabulary']} (7ths/sus/etc. collapse to nearest maj/min match)",
        f"# backend: {chords_value['backend']}",
        "",
    ]
    for segment in chords_value["segments"]:
        lines.append(f"{_format_timestamp(segment['start_seconds'])}  {segment['chord']}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination
