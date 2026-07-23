"""Read-only summaries of a project's persisted vgt state."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any
import wave

from .project import ProjectError, track_source_path
from .separation import ARTIFACT_FILENAMES, OPTIONAL_STEMS, OPERATION_ORDER, artifact_path, hash_audio_content
from .sidecar import ANALYSIS_STAGES, DETECTED_SPLIT_STAGES, artifact_namespace_dir, sidecar_path
from .transcribe import effective_profile_name_for_target


class StatusError(ValueError):
    """A vgt status summary cannot be produced."""


def _stage_summary(stage_name: str, stage: dict[str, Any]) -> str | None:
    value = stage.get("value")
    if value is None:
        return None
    if stage_name == "tempo" and isinstance(value, dict):
        bpm, signature = value.get("bpm"), value.get("time_signature")
        if bpm is not None and signature:
            return f"{float(bpm):.1f} BPM, {signature}"
    if stage_name == "key" and isinstance(value, dict):
        root, scale = value.get("root"), value.get("scale")
        if root and scale:
            return f"{root} {scale}"
    if stage_name == "sections" and isinstance(value, list):
        return f"{len(value)} sections"
    if stage_name == "chords" and isinstance(value, dict):
        segments = value.get("segments")
        if isinstance(segments, list):
            return f"{len(segments)} segments"
    return "present"


def _latest_timestamp(timestamps: list[Any]) -> str | None:
    # ISO-8601 UTC values sort chronologically. Ignore malformed legacy values
    # rather than making a read-only diagnostic command fail.
    valid = []
    for value in timestamps:
        if not isinstance(value, str):
            continue
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        valid.append(value)
    return max(valid) if valid else None


def _artifact_paths(project_path: Path, analysis: dict[str, Any]) -> dict[str, Path | None]:
    """Regenerable analysis artifacts live under `vgt/<namespace>/` next to
    the project; until
    an `artifact_namespace` has been recorded (i.e. before `vgt analyze` has
    ever run), none of these paths can be known."""
    stems = analysis.get("stems") if isinstance(analysis.get("stems"), dict) else {}
    namespace = stems.get("artifact_namespace")
    if not namespace:
        return {"click": None, "chord_sheet": None, "section_timeline": None}
    namespace_dir = artifact_namespace_dir(project_path, namespace)
    tempo = analysis.get("tempo") or {}
    chords = analysis.get("chords") or {}
    names = {
        "click": (tempo.get("value") or {}).get("click_artifact_path") if isinstance(tempo, dict) else None,
        "chord_sheet": (chords.get("value") or {}).get("chord_sheet_path") if isinstance(chords, dict) else None,
        "section_timeline": "sections.txt",
    }
    transcription = analysis.get("transcription") if isinstance(analysis.get("transcription"), dict) else {}
    transcription_targets = transcription.get("targets") if isinstance(transcription.get("targets"), dict) else {}
    for target, entry in transcription_targets.items():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("midi_file"), str):
            names[f"transcription_{target}_midi"] = entry["midi_file"]
        if isinstance(entry.get("notes_file"), str):
            names[f"transcription_{target}_notes"] = entry["notes_file"]
        if isinstance(entry.get("events_file"), str):
            names[f"transcription_{target}_events"] = entry["events_file"]
    return {name: namespace_dir / filename if isinstance(filename, str) else None for name, filename in names.items()}


def _transcription_status(analysis: dict[str, Any]) -> dict[str, Any]:
    """Summarize the multi-target `transcription` stage: one entry per
    requested target, reported in `requested_targets` order."""
    transcription = analysis.get("transcription") if isinstance(analysis.get("transcription"), dict) else {}
    requested = transcription.get("requested_targets") if isinstance(transcription.get("requested_targets"), list) else []
    targets = transcription.get("targets") if isinstance(transcription.get("targets"), dict) else {}
    modes = transcription.get("modes") if isinstance(transcription.get("modes"), dict) else {}
    # Match sidecar.upgrade's schema-v9 compatibility bridge without writing
    # anything: an old acoustic stem declaration is the persisted request for
    # the acoustic-guitar transcription profile unless a newer explicit mode
    # supersedes it.
    stems = analysis.get("stems") if isinstance(analysis.get("stems"), dict) else {}
    if stems.get("guitar_type") == "acoustic" and "guitar" not in modes:
        modes = {**modes, "guitar": "guitar-acoustic"}

    backend: str | None = None
    package_pin: str | None = None
    entries: dict[str, Any] = {}
    for target in requested:
        entry = targets.get(target) if isinstance(targets.get(target), dict) else {}
        if backend is None and isinstance(entry.get("backend"), str):
            backend = entry.get("backend")
            package_pin = entry.get("package_pin")
        entries[target] = {
            "requested_mode": modes.get(target),
            "effective_profile": effective_profile_name_for_target(target, modes),
            "backend": entry.get("backend"),
            "package_pin": entry.get("package_pin"),
            "status": entry.get("status"),
            "note_count": entry.get("note_count"),
            "event_count": entry.get("event_count"),
            "instrument_counts": entry.get("instrument_counts"),
            "pitch_range_midi": entry.get("pitch_range_midi"),
            "transcribed_at": entry.get("transcribed_at"),
            "error": entry.get("error"),
            "midi_file": entry.get("midi_file"),
            "notes_file": entry.get("notes_file"),
            "events_file": entry.get("events_file"),
            "first_event_s": entry.get("first_event_s"),
            "last_event_s": entry.get("last_event_s"),
            "backend_tempo": entry.get("backend_tempo"),
            "midi_tempo": entry.get("midi_tempo"),
            "confidence": entry.get("confidence"),
        }
    return {
        "backend": backend,
        "package_pin": package_pin,
        "requested_targets": list(requested),
        "targets": entries,
    }


def build_status(project_path: Path) -> dict[str, Any]:
    """Return a JSON-serializable, non-mutating sidecar summary."""
    path = sidecar_path(project_path)
    if not path.exists():
        raise StatusError(f"No .vgt sidecar found at {path}; run the Phase 0 apply action and vgt analyze first.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatusError(f"Cannot read .vgt sidecar at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise StatusError(f"Cannot read .vgt sidecar at {path}: expected a JSON object.")

    config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    analysis = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}
    source_path: str | None = None
    source_exists: bool | None = None
    source_error: str | None = None
    guid = config.get("reference_track_guid")
    if isinstance(guid, str) and guid:
        try:
            source = track_source_path(project_path, guid)
            source_path, source_exists = str(source), source.is_file()
        except ProjectError as exc:
            source_error = str(exc)

    stages: dict[str, Any] = {}
    for name in ANALYSIS_STAGES:
        if name == "transcription":
            continue  # per-target index, not the value/input_hash/settings_hash shape; see `_transcription_status`
        stage = analysis.get(name)
        stage = stage if isinstance(stage, dict) else {}
        summary = _stage_summary(name, stage)
        human_verified = bool(stage.get("human_verified"))
        detected_present = stage.get("detected") is not None
        state = "missing" if summary is None else ("human-corrected" if human_verified else "detected")
        stages[name] = {
            "present": summary is not None,
            "human_verified": human_verified,
            "detected_present": detected_present,
            "state": state,
            "summary": summary,
            "analyzed_at": stage.get("analyzed_at") if isinstance(stage.get("analyzed_at"), str) else None,
            "verified_at": stage.get("verified_at") if isinstance(stage.get("verified_at"), str) else None,
        }

    artifacts = _artifact_paths(project_path, analysis)
    stems = analysis.get("stems") if isinstance(analysis.get("stems"), dict) else {}
    stem_artifacts = stems.get("artifacts") if isinstance(stems.get("artifacts"), dict) else {}
    stem_operations = stems.get("operations") if isinstance(stems.get("operations"), dict) else {}

    def stem_file_state(record: Any) -> tuple[str, str | None]:
        if not isinstance(record, dict) or not isinstance(record.get("file"), str):
            return "missing", None
        candidate = artifact_path(project_path, record)
        if not candidate.is_file():
            return "missing", str(candidate)
        try:
            with wave.open(str(candidate), "rb") as handle:
                valid_wav = handle.getnframes() > 0 and handle.getframerate() > 0
            valid_hash = candidate.stat().st_size == record.get("size_bytes") and hash_audio_content(candidate) == record.get("sha256")
        except (OSError, EOFError, wave.Error):
            valid_wav = valid_hash = False
        return ("cached" if valid_wav and valid_hash else "corrupt"), str(candidate)

    optional_stems = tuple(name for name in OPTIONAL_STEMS if name in (stems.get("optional_stems") or []) or name in stem_artifacts)
    stem_artifact_status: dict[str, Any] = {}
    for name in (*ARTIFACT_FILENAMES, *optional_stems):
        record = stem_artifacts.get(name)
        file_state, resolved_path = stem_file_state(record)
        stem_artifact_status[name] = {
            "state": file_state,
            "path": resolved_path,
            "operation": record.get("operation") if isinstance(record, dict) else None,
            "side": record.get("side") if isinstance(record, dict) else None,
        }
    stem_operation_status: dict[str, Any] = {}
    operation_ids = (*OPERATION_ORDER, *(f"{name}-original" for name in optional_stems))
    for operation_id in operation_ids:
        record = stem_operations.get(operation_id) if isinstance(stem_operations.get(operation_id), dict) else {}
        backend_state = record.get("backend_state") if isinstance(record.get("backend_state"), dict) else {}
        state = record.get("status", "pending")
        if state == "completed":
            outputs = record.get("outputs") if isinstance(record.get("outputs"), list) else []
            state = "cached" if outputs and all(
                stem_artifact_status[name]["state"] == "cached"
                for name in outputs
                if name in stem_artifact_status
            ) else "corrupt"
        elif state == "in_progress":
            state = "in-progress"
        elif state == "error":
            state = "failed"
        else:
            state = "outstanding"
        stem_operation_status[operation_id] = {
            "state": state,
            "remote_state": backend_state.get("remote_status") or backend_state.get("status"),
            "requested_presets": record.get("requested_presets", {}),
            "effective_presets": record.get("effective_presets", {}),
            "requested_splitter": (record.get("requested_presets") or {}).get("splitter"),
            "effective_splitter": (record.get("effective_presets") or {}).get("splitter"),
            "error": record.get("error"),
            "outputs": record.get("outputs", []),
        }
    return {
        "project": {"path": str(project_path)},
        "sidecar": {"path": str(path), "exists": True, "readable": True, "schema_version": raw.get("schema_version")},
        "reference_track": {
            "name": config.get("reference_track_name"), "guid": guid,
            "source_path": source_path, "source_exists": source_exists, "source_error": source_error,
        },
        "managed_area": {
            "managed_track_guids": raw.get("managed_track_guids", []), "folder_name": config.get("folder_name"),
            "tempo_map_applied": config.get("tempo_map_applied"),
        },
        "stages": stages,
        "transcription": _transcription_status(analysis),
        "timestamps": {
            "last_analysis_at": _latest_timestamp([stage["analyzed_at"] for stage in stages.values()]),
            "last_human_correction_at": _latest_timestamp([stage["verified_at"] for stage in stages.values()]),
        },
        "artifacts": {
            name: {"path": str(artifact) if artifact else None, "exists": artifact.is_file() if artifact else False}
            for name, artifact in artifacts.items()
        },
        "stems": {
            "backend": stems.get("backend"), "api_version": stems.get("api_version"),
            "guitar_type": stems.get("guitar_type") or config.get("guitar_type"), "optional_stems": list(optional_stems), "artifact_namespace": stems.get("artifact_namespace"),
            "human_verified": bool(stems.get("human_verified")), "verified_at": stems.get("verified_at"),
            "operations": stem_operation_status, "artifacts": stem_artifact_status,
        },
    }


def format_status(status: dict[str, Any]) -> str:
    """Render the compact human-facing form of :func:`build_status`."""
    lines = [
        f"Project: {status['project']['path']}",
        f"Sidecar: {status['sidecar']['path']} (schema {status['sidecar']['schema_version']})",
    ]
    reference = status["reference_track"]
    lines += [
        f"Reference track: {reference['name'] or 'unknown'} ({reference['guid'] or 'unknown'})",
        f"Source audio: {reference['source_path'] or reference['source_error'] or 'unknown'} ({'exists' if reference['source_exists'] else 'missing' if reference['source_exists'] is False else 'unknown'})",
    ]
    managed = status["managed_area"]
    lines += [
        f"Managed area: {len(managed['managed_track_guids']) if isinstance(managed['managed_track_guids'], list) else 0} tracks; folder={managed['folder_name'] or 'unknown'}; tempo map applied={managed['tempo_map_applied'] if managed['tempo_map_applied'] is not None else 'unknown'}",
        "Analysis:",
    ]
    for name, stage in status["stages"].items():
        detail = stage["summary"] or "missing"
        if stage["present"]:
            detail += f", {stage['state']}"
            if name in DETECTED_SPLIT_STAGES and stage["detected_present"]:
                detail += ", detected baseline present"
        lines.append(f"  {name}: {detail}")
    transcription = status["transcription"]
    label = transcription["package_pin"] or transcription["backend"] or "not yet run"
    lines.append(f"transcription ({label}): {len(transcription['requested_targets'])} requested")
    for target in transcription["requested_targets"]:
        entry = transcription["targets"].get(target, {})
        status_value = entry.get("status")
        profile = entry.get("effective_profile") or "default"
        profile_text = f"profile {profile}"
        if status_value == "transcribed" and entry.get("event_count") is not None:
            count_text = _format_drum_instruments(entry.get("instrument_counts"))
            package = entry.get("package_pin")
            backend = entry.get("backend")
            backend_text = package.replace("==", " ") if isinstance(package, str) else backend or "drumscript"
            lines.append(
                f"  {target:<8} {entry.get('event_count')} events ({count_text}), {profile_text}, "
                f"{backend_text}, transcribed {entry.get('transcribed_at')}"
            )
        elif status_value == "transcribed":
            pitch = entry.get("pitch_range_midi")
            pitch_text = f"MIDI {pitch[0]}-{pitch[1]}" if pitch else "MIDI ?"
            lines.append(f"  {target:<8} {entry.get('note_count')} notes, {pitch_text}, {profile_text}, transcribed {entry.get('transcribed_at')}")
        elif status_value == "skipped-missing-source":
            lines.append(f"  {target:<8} skipped - no {target} stem available, {profile_text}")
        elif status_value == "error":
            lines.append(f"  {target:<8} error - {entry.get('error')}, {profile_text}")
        else:
            lines.append(f"  {target:<8} not yet run, {profile_text}")
    timestamps = status["timestamps"]
    lines += [
        f"Last analysis: {timestamps['last_analysis_at'] or 'unknown'}",
        f"Last human correction: {timestamps['last_human_correction_at'] or 'unknown'}",
        "Artifacts:",
    ]
    lines.extend(f"  {name}: {'present' if artifact['exists'] else 'missing'} ({artifact['path'] or 'unknown'})" for name, artifact in status["artifacts"].items())
    stems = status["stems"]
    lines += [
        "Stems:",
        f"  guitar type: {stems['guitar_type'] or 'not declared'}; quality: {'human-verified' if stems['human_verified'] else 'not human-verified'}",
        "  Operations:",
    ]
    for name, operation in stems["operations"].items():
        detail = operation["state"]
        if operation["remote_state"]:
            detail += f", remote={operation['remote_state']}"
        if operation["error"]:
            detail += f", error={operation['error']}"
        splitter = operation["requested_splitter"]
        if splitter:
            detail += f", splitter={splitter}"
            if operation["effective_splitter"]:
                detail += f"→{operation['effective_splitter']}"
        lines.append(f"    {name}: {detail}")
    lines.append("  Stem artifacts:")
    lines.extend(f"    {name}: {artifact['state']} ({artifact['path'] or 'unknown'})" for name, artifact in stems["artifacts"].items())
    return "\n".join(lines)


def _format_drum_instruments(value: Any) -> str:
    """Compact the detailed event JSON labels into a stable status summary."""
    counts = value if isinstance(value, dict) else {}
    kick = counts.get("kick", 0)
    snare = counts.get("snare", 0)
    hats = counts.get("hi_hat_closed", 0) + counts.get("hi_hat_open", 0)
    other = sum(count for name, count in counts.items() if name not in {"kick", "snare", "hi_hat_closed", "hi_hat_open"})
    parts = [f"kick {kick}", f"snare {snare}", f"hats {hats}", f"other {other}"]
    return ", ".join(parts)
