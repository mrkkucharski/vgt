"""Phase 1 analysis stage: detects tempo/key/sections/chords from the reference
track's source audio and persists the result into the `.vgt` sidecar.

Runs entirely in the Python CLI (never inside REAPER, per docs/GOAL.md's
"analysis stays out of the DAW process" requirement). The detectors below are
stubs -- later sub-issues of #7 fill in real DSP/ML -- but the stage-cache and
corrections-survive-rerun framework they run through is the real deliverable
here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import hashlib
import json

from . import __version__
from .project import ProjectError, locate_project, track_source_path
from .sidecar import ANALYSIS_STAGES, SidecarError, read_sidecar, refresh_stage, write_sidecar


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


# Stub detectors: Phase 1 sub-issues replace these with real DSP/ML. Each
# returns the stage's "value" payload, initially empty/unknown.
def detect_tempo(source: Path, settings: dict[str, Any]) -> dict[str, Any]:
    return {"bpm": None, "downbeat_offset_seconds": None, "time_signature": None}


def detect_key(source: Path, settings: dict[str, Any]) -> dict[str, Any]:
    return {"root": None, "scale": None}


def detect_sections(source: Path, settings: dict[str, Any]) -> list[Any]:
    return []


def detect_chords(source: Path, settings: dict[str, Any]) -> list[Any]:
    return []


_DETECTORS: dict[str, Callable[[Path, dict[str, Any]], Any]] = {
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
            compute=lambda stage=stage, stage_settings=stage_settings: _DETECTORS[stage](source, stage_settings),
        )
    analysis["provenance"] = {
        "tool": "vgt",
        "version": __version__,
        "settings": settings,
        "reference_source_path": str(source),
    }

    write_sidecar(project_path, sidecar)
    return sidecar
