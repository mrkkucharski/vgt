"""Phase 1 analysis stage: detects tempo/key/sections/chords from the reference
track's source audio and persists the result into the `.vgt` sidecar.

Runs entirely in the Python CLI (never inside REAPER, per docs/GOAL.md's
"analysis stays out of the DAW process" requirement). `detect_tempo`,
`detect_key`, `detect_sections`, and `detect_chords` are all real detectors
(see tempo.py/key.py/sections.py/chords.py), running through the same
stage-cache and corrections-survive-rerun framework.

Stages run in `sidecar.ANALYSIS_STAGES` order (tempo before chords), so
`detect_chords` can read the tempo stage's just-refreshed beat grid out of
`analysis` and snap chord boundaries to it -- the shared beat-synchronous
grid the chords stage is required to align to, rather than detecting its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import hashlib
import json

from . import __version__
from .chords import ChordDetectionError, chord_sheet_path, detect_chords as _detect_chords, render_chord_sheet
from .key import KeyDetectionError, detect_key as _detect_key
from .project import ProjectError, locate_project, track_source_path
from .sections import SectionDetectionError, detect_sections as _detect_sections, render_section_timeline, section_timeline_path
from .sidecar import ANALYSIS_STAGES, SidecarError, read_sidecar, refresh_stage, write_sidecar
from .tempo import TempoDetectionError, build_tempo_grid, click_artifact_path, detect_beats, render_click_over_mix


class AnalysisError(ValueError):
    """The project or its sidecar can't be analyzed."""


def _hash_settings(settings: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()


def hash_source_file(path: Path) -> str:
    """Cheap identity hash for a (possibly large) audio file: path, size, and
    mtime rather than file contents, so re-running doesn't re-read the audio."""
    stat = path.stat()
    digest = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(digest.encode("utf-8")).hexdigest()


def detect_tempo(project_path: Path, source: Path, settings: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """BPM, downbeat offset, time signature, tempo-map mode/residual, and the
    raw detected beat times (see tempo.py) -- the last of these is the shared
    grid `detect_chords` snaps to -- plus a click-over-mix verification
    artifact rendered next to the sidecar."""
    del analysis  # tempo runs first; nothing upstream to read yet
    try:
        beat_times, beat_positions, backend = detect_beats(source)
        grid = build_tempo_grid(beat_times, beat_positions, backend, settings)
        artifact = render_click_over_mix(source, beat_times, click_artifact_path(project_path))
    except TempoDetectionError as exc:
        raise AnalysisError(str(exc)) from exc
    grid["click_artifact_path"] = artifact.name
    grid["beat_times"] = [round(t, 6) for t in beat_times]
    return grid


def detect_key(project_path: Path, source: Path, settings: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    del project_path, analysis
    try:
        return _detect_key(source, settings)
    except KeyDetectionError as exc:
        raise AnalysisError(str(exc)) from exc


def detect_sections(project_path: Path, source: Path, settings: dict[str, Any], analysis: dict[str, Any]) -> list[Any]:
    """Section boundaries + generic labels (see sections.py), plus a
    plain-text section-timeline artifact rendered next to the sidecar for
    by-eye verification."""
    del analysis
    try:
        sections_value = _detect_sections(source, settings)
    except SectionDetectionError as exc:
        raise AnalysisError(str(exc)) from exc
    if sections_value:
        render_section_timeline(sections_value, section_timeline_path(project_path))
    return sections_value


def _tempo_beat_times(tempo_value: dict[str, Any] | None, source: Path) -> list[float]:
    """Beat timestamps backing the chords stage's grid alignment: reused from
    the tempo stage's persisted value when present, otherwise (e.g. after a
    human correction that only set bpm/time-signature, dropping the raw beat
    array) re-detected fresh via the same primary/fallback ladder tempo.py
    uses -- never chords' own ad hoc beat tracker, so both stages always
    agree on one shared grid."""
    if tempo_value and tempo_value.get("beat_times"):
        return tempo_value["beat_times"]
    try:
        beat_times, _beat_positions, _backend = detect_beats(source)
    except TempoDetectionError as exc:
        raise AnalysisError(str(exc)) from exc
    return beat_times


def detect_chords(project_path: Path, source: Path, settings: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Beat-aligned maj/min chord segments (see chords.py), snapped to the
    tempo stage's shared beat grid, plus a chord-sheet text artifact rendered
    next to the sidecar for by-eye verification."""
    beat_times = _tempo_beat_times(analysis["tempo"]["value"], source)
    try:
        chords_value = _detect_chords(source, beat_times, settings)
    except ChordDetectionError as exc:
        raise AnalysisError(str(exc)) from exc
    artifact = render_chord_sheet(chords_value, chord_sheet_path(project_path))
    chords_value["chord_sheet_path"] = artifact.name
    return chords_value


_DETECTORS: dict[str, Callable[[Path, Path, dict[str, Any], dict[str, Any]], Any]] = {
    "tempo": detect_tempo,
    "key": detect_key,
    "sections": detect_sections,
    "chords": detect_chords,
}


def reference_source_path(project_path: Path, sidecar: dict[str, Any]) -> Path:
    config = sidecar.get("config") or {}
    guid = config.get("reference_track_guid")
    if not guid:
        raise AnalysisError(f"{project_path}: sidecar has no config.reference_track_guid; run Phase 0 apply first.")
    return track_source_path(project_path, guid)


def analyze(project: str | Path | None, settings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run (or refresh) analysis for `project` and persist it into the sidecar.

    Idempotent: unchanged inputs/settings leave cached stage values untouched,
    and stages marked `human_verified` are never recomputed.
    """
    settings = settings or {}
    project_path = locate_project(project)
    try:
        sidecar = read_sidecar(project_path)
    except SidecarError as exc:
        raise AnalysisError(str(exc)) from exc

    try:
        source = reference_source_path(project_path, sidecar)
    except ProjectError as exc:
        raise AnalysisError(str(exc)) from exc
    if not source.is_file():
        raise AnalysisError(f"Reference source file not found: {source}")

    input_hash = hash_source_file(source)
    analysis = sidecar["analysis"]
    for stage in ANALYSIS_STAGES:
        stage_settings = settings.get(stage, {})
        analysis[stage] = refresh_stage(
            analysis[stage],
            input_hash=input_hash,
            settings_hash=_hash_settings(stage_settings),
            compute=lambda stage=stage, stage_settings=stage_settings: _DETECTORS[stage](
                project_path, source, stage_settings, analysis
            ),
        )
    analysis["provenance"] = {
        "tool": "vgt",
        "version": __version__,
        "settings": settings,
        "reference_source_path": str(source),
    }

    write_sidecar(project_path, sidecar)
    return sidecar
