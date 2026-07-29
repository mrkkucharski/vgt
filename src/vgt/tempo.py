"""Tempo/beat/downbeat grid detection: madmom's DBN beat+downbeat trackers are
the primary backend; librosa's `beat_track` is the fallback
when madmom isn't installed (it ships behind the `madmom` extra -- see
pyproject.toml) or fails to import (its last release predates modern
Python/NumPy). librosa gives beat times but no downbeats, so its bar phase is
persisted explicitly as unknown rather than treating its first beat as a
downbeat.

This module isolates both backends behind `detect_beats`/`build_tempo_grid`:
`analysis.py` and the CLI never import madmom or librosa directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import statistics

from .sidecar import artifact_namespace_dir

# A global constant-BPM fit's RMS residual, as a fraction of one beat
# interval, below which the grid is reported as a single constant tempo
# rather than a piecewise-linear map. Detector jitter alone (frame
# quantization in the beat tracker) typically contributes a few percent, so
# this is deliberately looser than the per-span split threshold below.
CONSTANT_TEMPO_RESIDUAL_THRESHOLD = 0.05
# CV within a candidate span above which the span is split (tempo drifted).
PIECEWISE_SEGMENT_CV_THRESHOLD = 0.05
_MIN_SPAN_BEATS = 4


class TempoDetectionError(RuntimeError):
    """Neither backend could produce a usable beat grid for the source audio."""


def _madmom_beats(source: Path) -> tuple[list[float], list[int]] | None:
    """Beat times + 1-indexed position-in-bar via madmom's DBN downbeat
    tracker, or None if madmom isn't importable, or fails at runtime, in this
    environment. madmom's last release predates modern Python/NumPy (see
    module docstring), so runtime failures during model processing -- not
    just the import -- are an expected way for it to be unusable here; both
    must fall back to librosa."""
    try:
        from madmom.features.downbeats import DBNDownBeatTrackingProcessor, RNNDownBeatProcessor
    except Exception:
        return None

    try:
        activations = RNNDownBeatProcessor()(str(source))
        beats = DBNDownBeatTrackingProcessor(beats_per_bar=[3, 4], fps=100)(activations)
    except Exception:
        return None
    beat_times = beats[:, 0].tolist()
    beat_positions = beats[:, 1].astype(int).tolist()
    return beat_times, beat_positions


def _librosa_beats(source: Path) -> tuple[list[float], list[int]]:
    """Fallback: beat times only, no downbeats (every position reported as 1,
    meaning "unknown" -- see `_time_signature`)."""
    import librosa

    y, sr = librosa.load(str(source), sr=None, mono=True)
    _tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    return beat_times, [1] * len(beat_times)


def detect_beats(source: Path) -> tuple[list[float], list[int], str]:
    """Return (beat_times_seconds, beat_bar_positions, backend_name)."""
    if not source.is_file():
        raise TempoDetectionError(f"Reference source file not found: {source}")
    madmom_result = _madmom_beats(source)
    if madmom_result is not None:
        beat_times, beat_positions = madmom_result
        backend = "madmom"
    else:
        try:
            beat_times, beat_positions = _librosa_beats(source)
        except Exception as exc:
            raise TempoDetectionError(
                f"Could not detect beats for {source}: madmom was unavailable or failed, "
                f"and the librosa fallback failed ({exc})."
            ) from exc
        backend = "librosa"
    if len(beat_times) < 2:
        raise TempoDetectionError(f"Too few beats detected in {source} to build a tempo grid.")
    return beat_times, beat_positions, backend


def _fit_constant(beat_times: list[float]) -> tuple[float, float]:
    """Least-squares fit of beat-index -> time; returns (bpm, rms_residual_seconds)."""
    n = len(beat_times)
    indices = range(n)
    mean_i = sum(indices) / n
    mean_t = sum(beat_times) / n
    numerator = sum((i - mean_i) * (t - mean_t) for i, t in zip(indices, beat_times))
    denominator = sum((i - mean_i) ** 2 for i in indices)
    interval = numerator / denominator if denominator else (beat_times[-1] - beat_times[0]) / max(n - 1, 1)
    bpm = 60.0 / interval if interval else 0.0
    predicted = [beat_times[0] + i * interval for i in indices]
    residual = (sum((p - t) ** 2 for p, t in zip(predicted, beat_times)) / n) ** 0.5
    return bpm, residual


def _piecewise_spans(beat_times: list[float]) -> tuple[list[dict[str, Any]], float]:
    """Greedily split beat times into runs of roughly-constant tempo. Each
    span is independently least-squares fit; overall residual is the RMS of
    each span's own residual."""
    spans: list[dict[str, Any]] = []
    residuals: list[float] = []
    segment_start = 0
    n = len(beat_times)

    def close_span(end_exclusive: int) -> None:
        segment = beat_times[segment_start:end_exclusive]
        bpm, residual = _fit_constant(segment)
        spans.append({"start_seconds": segment[0], "end_seconds": segment[-1], "bpm": bpm})
        residuals.append(residual)

    # `close_span(i - 1)` drops the beat that triggered the CV overflow, so
    # the window must reach one beat past `_MIN_SPAN_BEATS` before a split is
    # eligible -- otherwise the *closed* span would be one beat short of the
    # minimum.
    i = segment_start + _MIN_SPAN_BEATS + 1
    while i <= n:
        segment = beat_times[segment_start:i]
        intervals = [b - a for a, b in zip(segment, segment[1:])]
        mean_interval = sum(intervals) / len(intervals)
        cv = (statistics.pstdev(intervals) / mean_interval) if mean_interval else 0.0
        if cv > PIECEWISE_SEGMENT_CV_THRESHOLD:
            close_span(i - 1)
            segment_start = i - 2
            i = segment_start + _MIN_SPAN_BEATS + 1
        else:
            i += 1
    if n - segment_start >= 2:
        close_span(n)
    residual_overall = (sum(r**2 for r in residuals) / len(residuals)) ** 0.5 if residuals else 0.0
    return spans, residual_overall


def _downbeat_detected(beat_positions: list[int]) -> bool:
    """Whether positions actually establish a bar phase."""
    return bool(set(beat_positions) - {1})


def _time_signature(beat_positions: list[int], hint: str | None) -> str | None:
    if not _downbeat_detected(beat_positions):
        return hint  # every beat at position 1: downbeats unknown (librosa fallback)
    return f"{max(beat_positions)}/4"


def build_tempo_grid(
    beat_times: list[float],
    beat_positions: list[int],
    backend: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit the beat grid to either a single constant BPM or a piecewise-linear
    tempo map, per the non-invasive tempo-map rule."""
    settings = settings or {}
    mean_interval = (beat_times[-1] - beat_times[0]) / (len(beat_times) - 1)
    constant_bpm, constant_residual = _fit_constant(beat_times)
    relative_residual = (constant_residual / mean_interval) if mean_interval else 0.0

    downbeat_detected = _downbeat_detected(beat_positions)
    downbeat_times = [t for t, pos in zip(beat_times, beat_positions) if pos == 1]
    time_signature = _time_signature(beat_positions, settings.get("time_signature_hint")) or "4/4"

    grid: dict[str, Any] = {
        "downbeat_detected": downbeat_detected,
        "downbeat_offset_seconds": round(downbeat_times[0], 6) if downbeat_detected and downbeat_times else None,
        "time_signature": time_signature,
        "backend": backend,
        "beat_count": len(beat_times),
    }
    if relative_residual <= CONSTANT_TEMPO_RESIDUAL_THRESHOLD:
        grid.update(bpm=round(constant_bpm, 3), mode="constant", residual_seconds=round(constant_residual, 6), spans=None)
    else:
        spans, residual = _piecewise_spans(beat_times)
        grid.update(
            bpm=round(constant_bpm, 3),
            mode="piecewise",
            residual_seconds=round(residual, 6),
            spans=[
                {
                    "start_seconds": round(span["start_seconds"], 6),
                    "end_seconds": round(span["end_seconds"], 6),
                    "bpm": round(span["bpm"], 3),
                }
                for span in spans
            ],
        )
    return grid


def click_artifact_path(project_path: Path, namespace: str) -> Path:
    """Verification artifact lives under the project's `vgt/<namespace>/`
    folder, not inside its own Media folder -- it is vgt-owned, regenerable,
    not part of the song."""
    return artifact_namespace_dir(project_path, namespace) / "tempo-click.wav"


def render_click(source: Path, beat_times: list[float], destination: Path) -> Path:
    """Render a click-only track on the detected beat grid so it can be checked
    by ear (or lined up against the reference in REAPER). The reference audio is
    loaded only to match its sample rate and length; it is not mixed in."""
    import librosa
    import soundfile as sf

    audio, sample_rate = librosa.load(str(source), sr=None, mono=False)
    length = audio.shape[-1] if audio.ndim > 1 else audio.shape[0]
    clicks = librosa.clicks(times=beat_times, sr=sample_rate, length=length)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(destination), clicks, sample_rate)
    return destination
