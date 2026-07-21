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
import logging
from pathlib import Path
from typing import Any, Mapping

from .sidecar import artifact_namespace_dir

_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_NO_CHORD = "N"
_LOG = logging.getLogger(__name__)


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


def _template_scores(chroma_vector, templates):
    import numpy as np

    vector = np.asarray(chroma_vector, dtype=float)
    norm = float(np.linalg.norm(vector))
    if norm < 1e-6:
        return None
    vector = vector / norm
    return np.array([float(np.dot(vector, template / np.linalg.norm(template))) for _, template in templates])


DEFAULT_DURATION_PRIOR = 0.8
"""Score bonus for retaining the preceding chord in template decoding.

This value came from an exploratory single-song experiment, so it is exposed
as a setting rather than embedded in the decoder.  It is safe without a known
downbeat because it has no bar-phase assumption.
"""

DEFAULT_BAR_AGGREGATION_BEATS = 4
"""Beats to pool for a bar-level chord decision when a 4/4 downbeat is known."""


def _decoder_setting(settings: dict[str, Any], name: str, default: float) -> float:
    value = settings.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ChordDetectionError(f"chords.{name} must be a non-negative number.")
    return float(value)


def _bar_aggregation_beats(settings: dict[str, Any], tempo: dict[str, Any] | None) -> int | None:
    """Return a usable bar size only when the tempo stage knows its downbeat.

    Librosa deliberately reports every beat as position one (see tempo.py),
    which means its offset is not a detected downbeat.  Pooling four beats in
    that case can move every decision one beat out of phase, so the conservative
    default is no aggregation.  Explicit settings still require madmom's
    downbeat-aware tempo result; they only change the group size.
    """
    requested = settings.get("bar_aggregation_beats", DEFAULT_BAR_AGGREGATION_BEATS)
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
        raise ChordDetectionError("chords.bar_aggregation_beats must be a non-negative integer.")
    if requested == 0:
        return None
    if not tempo or tempo.get("backend") != "madmom" or tempo.get("time_signature") != "4/4":
        return None
    if not isinstance(tempo.get("downbeat_offset_seconds"), (int, float)):
        return None
    return requested


def _bar_aggregate_scores(scores, group_indices: list[int | None]):
    """Replace each beat's evidence with the mean evidence of its bar.

    Scores rather than independently chosen labels are pooled, so every label
    in a bar is supported by all of that bar's chroma observations.
    """
    import numpy as np

    pooled = np.asarray(scores, dtype=float).copy()
    groups: dict[int, list[int]] = {}
    for row, group in enumerate(group_indices):
        if group is not None:
            groups.setdefault(group, []).append(row)
    for rows in groups.values():
        pooled[rows] = pooled[rows].mean(axis=0)
    return pooled


def _viterbi_labels(scores, labels: list[str], duration_prior: float) -> list[str]:
    """Decode template scores with a first-order chord-duration prior."""
    import numpy as np

    emissions = np.asarray(scores, dtype=float)
    if emissions.ndim != 2 or emissions.shape[1] != len(labels):
        raise ValueError("scores must have one column per chord label")
    if len(emissions) == 0:
        return []

    path_scores = emissions[0].copy()
    backpointers = np.zeros((len(emissions), len(labels)), dtype=int)
    for beat in range(1, len(emissions)):
        best_previous = int(np.argmax(path_scores))
        transitions = np.full(len(labels), path_scores[best_previous])
        staying = path_scores + duration_prior
        stay_better = staying >= transitions
        transitions[stay_better] = staying[stay_better]
        backpointers[beat, stay_better] = np.flatnonzero(stay_better)
        backpointers[beat, ~stay_better] = best_previous
        path_scores = emissions[beat] + transitions

    indices = [int(np.argmax(path_scores))]
    for beat in range(len(emissions) - 1, 0, -1):
        indices.append(int(backpointers[beat, indices[-1]]))
    indices.reverse()
    return [labels[index] for index in indices]


def _bar_groups(
    bounds: list[tuple[float, float]], beat_times: list[float], tempo: dict[str, Any] | None, bar_beats: int | None
) -> list[int | None]:
    """Map template windows to bars anchored on the detected downbeat."""
    if bar_beats is None:
        return [None] * len(bounds)
    downbeat = float(tempo["downbeat_offset_seconds"])  # guarded by _bar_aggregation_beats
    if len(beat_times) < 2:
        return [None] * len(bounds)
    intervals = [b - a for a, b in zip(beat_times, beat_times[1:]) if b > a]
    if not intervals:
        return [None] * len(bounds)
    interval = min(intervals)
    anchor = min(range(len(beat_times)), key=lambda index: abs(beat_times[index] - downbeat))
    if abs(beat_times[anchor] - downbeat) > interval * 0.1:
        return [None] * len(bounds)

    groups: list[int | None] = []
    for start, _end in bounds:
        index = min(range(len(beat_times)), key=lambda candidate: abs(beat_times[candidate] - start))
        groups.append((index - anchor) // bar_beats if abs(beat_times[index] - start) <= interval * 0.1 else None)
    return groups


def _template_chords(
    source: Path, beat_times: list[float], settings: dict[str, Any], tempo: dict[str, Any] | None
) -> list[tuple[float, float, str]]:
    """Always-available fallback: chroma of the harmonic component
    (percussive transients hurt chord templates) averaged within each
    beat-to-beat window of the shared grid, classified against the 24
    maj/min triad templates, decoded with a chord-duration prior.  When the
    tempo stage has a trustworthy 4/4 downbeat, template scores are also
    pooled within downbeat-aligned bars before decoding."""
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

    raw_bounds: list[tuple[float, float]] = []
    score_rows: list[Any] = []
    for start, end in zip(boundaries, boundaries[1:]):
        mask = (frame_times >= start) & (frame_times < end)
        if not mask.any():
            continue
        scores = _template_scores(chroma[:, mask].mean(axis=1), templates)
        if scores is None:
            score_rows.append(np.full(len(templates), -1.0))
        else:
            score_rows.append(scores)
        raw_bounds.append((start, end))

    if not score_rows:
        return []
    labels = [label for label, _template in templates]
    duration_prior = _decoder_setting(settings, "duration_prior", DEFAULT_DURATION_PRIOR)
    bar_beats = _bar_aggregation_beats(settings, tempo)
    scores = _bar_aggregate_scores(score_rows, _bar_groups(raw_bounds, beat_times, tempo, bar_beats))
    decoded_labels = _viterbi_labels(scores, labels, duration_prior)

    segments: list[tuple[float, float, str]] = []
    for (start, end), label in zip(raw_bounds, decoded_labels):
        if segments and segments[-1][2] == label:
            segments[-1] = (segments[-1][0], end, label)
        else:
            segments.append((start, end, label))
    return segments


def _template_chords_fused(
    sources: Mapping[str, Path], beat_times: list[float], settings: dict[str, Any], tempo: dict[str, Any] | None
) -> tuple[list[tuple[float, float, str]], list[str]]:
    """Pool template evidence from independently decoded audio sources.

    The mix supplies the timeline boundaries.  Each usable source contributes
    one 24-label score vector per shared beat window; those vectors are summed
    before the single duration-prior decode.  A bad stem is deliberately not a
    fatal error: it simply contributes no evidence.
    """
    import librosa
    import numpy as np

    original = sources["original"]
    try:
        mix_y, mix_sr = librosa.load(str(original), sr=None, mono=True)
    except Exception as exc:  # noqa: BLE001 - normalize backend errors
        raise ChordDetectionError(f"{original}: could not load audio: {exc}") from exc
    if len(mix_y) == 0:
        raise ChordDetectionError(f"{original}: audio contains no samples.")
    duration = librosa.get_duration(y=mix_y, sr=mix_sr)
    boundaries = sorted({round(t, 6) for t in [0.0, *beat_times, duration]})
    raw_bounds = list(zip(boundaries, boundaries[1:]))
    templates = _chord_templates()
    summed_scores = np.zeros((len(raw_bounds), len(templates)))
    contributors: list[str] = []

    for name, path in sources.items():
        try:
            y, sr = (mix_y, mix_sr) if name == "original" else librosa.load(str(path), sr=None, mono=True)
            if len(y) == 0:
                raise ValueError("audio contains no samples")
            harmonic = librosa.effects.harmonic(y, margin=8)
            chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr)
            frame_times = librosa.frames_to_time(np.arange(chroma.shape[1]), sr=sr)
            rows = []
            for start, end in raw_bounds:
                mask = (frame_times >= start) & (frame_times < end)
                score = _template_scores(chroma[:, mask].mean(axis=1), templates) if mask.any() else None
                rows.append(np.full(len(templates), -1.0) if score is None else score)
        except Exception as exc:  # noqa: BLE001 - one damaged artifact is optional
            if name == "original":
                raise ChordDetectionError(f"{path}: could not analyze audio: {exc}") from exc
            _LOG.warning("Skipping chord-fusion source %s (%s): %s", name, path, exc)
            continue
        summed_scores += np.asarray(rows)
        contributors.append(name)

    if not contributors or not raw_bounds:
        return [], contributors
    labels = [label for label, _template in templates]
    scores = _bar_aggregate_scores(
        summed_scores, _bar_groups(raw_bounds, beat_times, tempo, _bar_aggregation_beats(settings, tempo))
    )
    decoded_labels = _viterbi_labels(scores, labels, _decoder_setting(settings, "duration_prior", DEFAULT_DURATION_PRIOR))
    segments: list[tuple[float, float, str]] = []
    for (start, end), label in zip(raw_bounds, decoded_labels):
        if segments and segments[-1][2] == label:
            segments[-1] = (segments[-1][0], end, label)
        else:
            segments.append((start, end, label))
    return segments, contributors


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


def detect_chords(
    source: Path,
    beat_times: list[float],
    settings: dict[str, Any] | None = None,
    *,
    tempo: dict[str, Any] | None = None,
    sources: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Return `{"segments", "beat_times", "vocabulary", "backend"}` for
    `source`, with every chord segment boundary snapped to `beat_times` --
    the shared grid from the tempo stage (see module docstring)."""
    settings = settings or {}
    if not source.is_file():
        raise ChordDetectionError(f"Reference source file not found: {source}")
    grid = sorted(beat_times)
    if len(grid) < 2:
        raise ChordDetectionError(f"Too few beats in the shared grid ({len(grid)}) to snap chords to.")

    source_map = {"original": source, **(sources or {})}
    # Preserve the established mix-only backend ladder exactly.  Fusion needs
    # comparable template scores, so it intentionally uses the template path.
    if len(source_map) == 1:
        madmom_result = _madmom_chords(source)
        if madmom_result is not None:
            raw_segments, backend = madmom_result, "madmom"
        else:
            chordino_result = _chordino_chords(source)
            if chordino_result is not None:
                raw_segments, backend = chordino_result, "chordino"
            else:
                raw_segments, backend = _template_chords(source, grid, settings, tempo), "librosa"
        contributors = ["original"]
    else:
        raw_segments, contributors = _template_chords_fused(source_map, grid, settings, tempo)
        backend = "librosa_fusion"

    segments = _snap_segments_to_grid(raw_segments, grid)

    return {
        "segments": segments,
        "beat_times": [round(t, 6) for t in grid],
        "vocabulary": "maj_min",
        "backend": backend,
        "sources": contributors,
    }


def chord_sheet_path(project_path: Path, namespace: str) -> Path:
    """Verification artifact lives under the project's `vgt/<namespace>/`
    folder, not inside its own Media folder -- it is vgt-owned, regenerable,
    not part of the song (see docs/stem-separation-plan.md's Artifact
    layout)."""
    return artifact_namespace_dir(project_path, namespace) / "chords.txt"


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
