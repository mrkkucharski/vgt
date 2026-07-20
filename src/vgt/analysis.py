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
from datetime import UTC, datetime
import copy
import hashlib
import json

from . import __version__
from .chords import ChordDetectionError, chord_sheet_path, detect_chords as _detect_chords, render_chord_sheet
from .key import KeyDetectionError, detect_key as _detect_key
from .project import ProjectError, locate_project, track_source_path
from .sections import SectionDetectionError, detect_sections as _detect_sections, render_section_timeline, section_timeline_path
from .sidecar import (
    ANALYSIS_STAGES,
    SidecarError,
    DETECTED_SPLIT_STAGES,
    ensure_artifact_namespace,
    read_sidecar,
    refresh_stage,
    stage_is_current,
    write_sidecar,
)
from .tempo import TempoDetectionError, build_tempo_grid, click_artifact_path, detect_beats, render_click


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


def detect_tempo(
    project_path: Path, source: Path, settings: dict[str, Any], analysis: dict[str, Any], namespace: str
) -> dict[str, Any]:
    """BPM, downbeat offset, time signature, tempo-map mode/residual, and the
    raw detected beat times (see tempo.py) -- the last of these is the shared
    grid `detect_chords` snaps to -- plus a click-only verification artifact
    rendered under the project's `vgt/<namespace>/` folder."""
    del analysis  # tempo runs first; nothing upstream to read yet
    try:
        beat_times, beat_positions, backend = detect_beats(source)
        grid = build_tempo_grid(beat_times, beat_positions, backend, settings)
        artifact = render_click(source, beat_times, click_artifact_path(project_path, namespace))
    except TempoDetectionError as exc:
        raise AnalysisError(str(exc)) from exc
    grid["click_artifact_path"] = artifact.name
    grid["beat_times"] = [round(t, 6) for t in beat_times]
    return grid


def detect_key(
    project_path: Path, source: Path, settings: dict[str, Any], analysis: dict[str, Any], namespace: str
) -> dict[str, Any]:
    del project_path, analysis, namespace
    try:
        return _detect_key(source, settings)
    except KeyDetectionError as exc:
        raise AnalysisError(str(exc)) from exc


def detect_sections(
    project_path: Path,
    source: Path,
    settings: dict[str, Any],
    analysis: dict[str, Any],
    namespace: str,
    *,
    render_artifact: bool = True,
) -> list[Any]:
    """Section boundaries + generic labels (see sections.py), plus a
    plain-text section-timeline artifact rendered under the project's
    `vgt/<namespace>/` folder for by-eye verification.

    `render_artifact=False` skips writing that artifact: used when this is
    only refreshing the `detected` baseline of an already human-verified
    stage (see `_refresh_stage_with_detected`), so the on-disk
    `sections.txt` -- reflecting the effective, human-corrected `value` --
    isn't silently overwritten with the raw machine detection the human
    corrected away from."""
    del analysis
    try:
        sections_value = _detect_sections(source, settings)
    except SectionDetectionError as exc:
        raise AnalysisError(str(exc)) from exc
    if sections_value and render_artifact:
        render_section_timeline(sections_value, section_timeline_path(project_path, namespace))
    return sections_value


def _refresh_stage_with_detected(
    stage: dict[str, Any],
    *,
    input_hash: str,
    settings_hash: str,
    compute: Callable[..., Any],
    force: bool = False,
    analyzed_at: str | None = None,
) -> dict[str, Any]:
    """Like `sidecar.refresh_stage`, but tracks `detected` -- the pristine
    machine-detection baseline -- independently of `value`. Used for the
    `chords` and `sections` stages (`sidecar.DETECTED_SPLIT_STAGES`).

    Before a human verifies `value`, the two are recomputed together (there
    is only one detector call; `detected` is just the pristine copy of its
    result), same as `refresh_stage`.

    Once a human has verified `value`, `refresh_stage` freezes it for good --
    but `detected` keeps tracking the current audio/settings via its own
    `detected_input_hash`/`detected_settings_hash` pair, recomputing whenever
    those change (or `force`). `value` and `human_verified` are never touched
    by that recompute; only `detected` moves, preserving the human's
    correction while keeping the machine baseline current (#19)."""
    if not stage.get("human_verified"):
        refreshed = refresh_stage(
            stage, input_hash=input_hash, settings_hash=settings_hash, compute=compute, force=force, analyzed_at=analyzed_at
        )
        if refreshed is stage:
            return stage
        return {
            **refreshed,
            "detected": copy.deepcopy(refreshed["value"]),
            "detected_input_hash": input_hash,
            "detected_settings_hash": settings_hash,
        }

    detected_is_current = (
        stage.get("detected_input_hash") == input_hash and stage.get("detected_settings_hash") == settings_hash
    )
    if not force and detected_is_current:
        return stage
    return {
        **stage,
        "detected": compute(render_artifact=False),
        "detected_input_hash": input_hash,
        "detected_settings_hash": settings_hash,
    }


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


def detect_chords(
    project_path: Path,
    source: Path,
    settings: dict[str, Any],
    analysis: dict[str, Any],
    namespace: str,
    *,
    render_artifact: bool = True,
) -> dict[str, Any]:
    """Beat-aligned maj/min chord segments (see chords.py), snapped to the
    tempo stage's shared beat grid, plus a chord-sheet text artifact rendered
    under the project's `vgt/<namespace>/` folder for by-eye verification.

    `render_artifact=False` skips writing that artifact: used when this is
    only refreshing the `detected` baseline of an already human-verified
    stage (see `_refresh_stage_with_detected`), so the on-disk `chords.txt` --
    documented as reflecting the effective, human-corrected `value` -- isn't
    silently overwritten with the raw machine detection the human corrected
    away from."""
    beat_times = _tempo_beat_times(analysis["tempo"]["value"], source)
    try:
        chords_value = _detect_chords(source, beat_times, settings)
    except ChordDetectionError as exc:
        raise AnalysisError(str(exc)) from exc
    artifact_path = chord_sheet_path(project_path, namespace)
    if render_artifact:
        render_chord_sheet(chords_value, artifact_path)
    chords_value["chord_sheet_path"] = artifact_path.name
    return chords_value


_DETECTORS: dict[str, Callable[..., Any]] = {
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


def analyze(
    project: str | Path | None,
    settings: dict[str, dict[str, Any]] | None = None,
    progress: Callable[[str], None] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run (or refresh) analysis for `project` and persist it into the sidecar.

    Idempotent: unchanged inputs/settings leave cached stage values untouched,
    and stages marked `human_verified` are never recomputed.

    `force` recomputes every stage even when its cache is current, but still
    preserves human-verified stages.

    `progress`, when given, is called with human-readable status lines as each
    stage starts (the detectors are otherwise silent for a minute or more); the
    CLI wires it to stderr so the JSON result on stdout stays pipe-clean.
    """
    emit = progress or (lambda _message: None)
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

    emit(f"analyzing reference track '{source.name}'")
    emit(f"running {len(ANALYSIS_STAGES)} detectors ({', '.join(ANALYSIS_STAGES)}); first run can take a minute or two")

    input_hash = hash_source_file(source)
    analysis = sidecar["analysis"]
    namespace = ensure_artifact_namespace(sidecar, project_path)
    total = len(ANALYSIS_STAGES)
    for position, stage in enumerate(ANALYSIS_STAGES, start=1):
        stage_settings = settings.get(stage, {})
        settings_hash = _hash_settings(stage_settings)
        if analysis[stage].get("human_verified"):
            if stage in DETECTED_SPLIT_STAGES and (
                force
                or analysis[stage].get("detected_input_hash") != input_hash
                or analysis[stage].get("detected_settings_hash") != settings_hash
            ):
                emit(f"[{position}/{total}] {stage} — human-verified value kept; refreshing detected baseline…")
            else:
                emit(f"[{position}/{total}] {stage} — human-verified, keeping")
        elif not force and stage_is_current(analysis[stage], input_hash=input_hash, settings_hash=settings_hash):
            emit(f"[{position}/{total}] {stage} — unchanged, using cached result")
        elif force:
            emit(f"[{position}/{total}] {stage} — re-analyzing (forced)…")
        else:
            emit(f"[{position}/{total}] {stage} — analyzing…")
        refresh = _refresh_stage_with_detected if stage in DETECTED_SPLIT_STAGES else refresh_stage
        analysis[stage] = refresh(
            analysis[stage],
            input_hash=input_hash,
            settings_hash=settings_hash,
            compute=lambda stage=stage, stage_settings=stage_settings, **kwargs: _DETECTORS[stage](
                project_path, source, stage_settings, analysis, namespace, **kwargs
            ),
            force=force,
            analyzed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    emit("writing sidecar")
    analysis["provenance"] = {
        "tool": "vgt",
        "version": __version__,
        "settings": settings,
        "reference_source_path": str(source),
    }

    write_sidecar(project_path, sidecar)
    return sidecar
