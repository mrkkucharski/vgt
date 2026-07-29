"""Phase 1 analysis stage: detects tempo/key/sections/chords from the reference
track's source audio and persists the result into the `.vgt` sidecar.

Runs entirely in the Python CLI (never inside REAPER: analysis stays out of
the DAW process). `detect_tempo`,
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
import logging
import math

from . import __version__
from .chords import ChordDetectionError, chord_sheet_path, detect_chords as _detect_chords, render_chord_sheet
from .key import KeyDetectionError, detect_key as _detect_key
from .project import ProjectError, locate_project, track_source_path
from .sections import SectionDetectionError, detect_sections as _detect_sections, render_section_timeline, section_timeline_path
from .sidecar import (
    ANALYSIS_STAGES,
    SidecarError,
    DETECTED_SPLIT_STAGES,
    artifact_namespace_dir,
    ensure_artifact_namespace,
    migrate_transcription_target,
    read_sidecar,
    refresh_stage,
    stage_is_current,
    update_analysis,
)
from .tempo import TempoDetectionError, build_tempo_grid, click_artifact_path, detect_beats, render_click
from .transcribe import (
    Transcriber,
    TranscriberRouter,
    TargetTranscriberRouter,
    events_artifact_name,
    effective_profile_name_for_target,
    midi_artifact_name,
    notes_artifact_name,
    production_transcriber_router,
    resolve_target_source,
    target_input_hash,
    tempo_map_reference,
    validate_profile_for_target,
    validate_target,
)
from .transcription_variants import (
    VariantRequest,
    garbage_collect_raw_cache,
    reconcile_artifact_layout,
    reconcile_variants,
    variant_events_name,
    variant_midi_name,
    variant_notes_name,
)

FUSION_STEM_NAMES = ("instrumental", "guitar", "backing")
_LOG = logging.getLogger(__name__)


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
    render_baseline_artifact: bool = False,
) -> dict[str, Any]:
    """Like `sidecar.refresh_stage`, but tracks `detected` -- the pristine
    machine-detection baseline -- independently of `value`. Used for the
    `key`, `chords`, and `sections` stages (`sidecar.DETECTED_SPLIT_STAGES`).

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
        # Only sections writes a presentation artifact during detection.
        # Key and chords have no `render_artifact` argument.
        "detected": compute(render_artifact=False) if render_baseline_artifact else compute(),
        "detected_input_hash": input_hash,
        "detected_settings_hash": settings_hash,
    }


def apply_artifact_layout(
    project_path: Path, namespace: str, analysis: dict[str, Any], *, emit: Callable[[str], None]
) -> None:
    """Bring this project's transcription artifacts onto the current layout and
    persist the record rewrites atomically (issue #223).

    Runs twice by design: once against the caller's in-memory `analysis`, which
    performs the actual file moves and emits the progress lines, and again
    inside the atomic sidecar update against the authoritative on-disk copy,
    which finds the files already moved and only rewrites the records there.
    `reconcile_artifact_layout` is idempotent and filesystem-driven precisely so
    that second pass is safe (see its docstring).
    """
    namespace_dir = artifact_namespace_dir(project_path, namespace)
    outcome = reconcile_artifact_layout(
        namespace_dir=namespace_dir, targets=analysis["transcription"]["targets"], emit=emit
    )
    if not outcome.records_changed:
        return

    def persist_layout(current: dict[str, Any]) -> None:
        reconcile_artifact_layout(namespace_dir=namespace_dir, targets=current["transcription"]["targets"])

    update_analysis(project_path, persist_layout)


def _refresh_target(
    project_path: Path,
    target: str,
    analysis: dict[str, Any],
    reference_source: Path,
    namespace: str,
    router: TranscriberRouter,
    *,
    force: bool,
    emit: Callable[[str], None],
) -> dict[str, Any]:
    """Reconcile one transcription target's `targets` index entry.

    A small sibling of `_refresh_stage_with_detected`: each entry is
    recomputed only when its own `input_hash`/`settings_hash` pair is stale
    (or `force`), so changing the guitar stem never touches the bass entry.
    Never falls back to the mix and never triggers separation -- a target
    whose stem hasn't arrived yet is simply retained as
    `skipped-missing-source` until a later run finds it (see
    `sidecar.py` schema v10 and `docs/transcription-plan.md` section 2).

    Every target goes through the variants model, so every artifact this
    writes lands under `transcription/<target>/` (#223). The pre-v13
    single-result writer it replaced was the one remaining producer of flat
    `transcription/<target>.mid` artifacts, and the only caller that ever
    reached it was a library caller taking the old default.
    """
    validate_target(target)

    existing_target = analysis["transcription"]["targets"].get(target)

    # Schema v13 retains several generated candidates for one target.  The
    # established analyze flags remain a compatibility surface over that
    # model: they reconcile the target's first retained variant (or create
    # one), never replace the complete target record with the old one-result
    # representation, and never designate any candidate as preferred (#176).
    # In particular, a routine `vgt analyze` after `variant add` must not
    # make the alternatives disappear.
    if isinstance(existing_target, dict):
        record = migrate_transcription_target(
            target, existing_target, analysis["transcription"].get("modes") or {}
        )
    else:
        record = {}
    variants = record.get("variants") if isinstance(record.get("variants"), dict) else {}
    order = record.get("variant_order") if isinstance(record.get("variant_order"), list) else []
    target_variant_id = order[0] if order and order[0] in variants else None

    # Keep the historical name for the automatically managed candidate.  Its
    # immutable id is deterministic for a newly-created compatibility entry,
    # while a migrated or explicitly-created variant retains its existing id.
    if target_variant_id is None:
        target_variant_id = f"default-{target}"
        suffix = 2
        while target_variant_id in variants:
            target_variant_id = f"default-{target}-{suffix}"
            suffix += 1
        label = "default"
    else:
        label = variants[target_variant_id].get("label") or "default"

    tempo_value = analysis["tempo"].get("value")
    midi_tempo = tempo_value.get("bpm") if isinstance(tempo_value, dict) else None
    time_signature = tempo_value.get("time_signature") if isinstance(tempo_value, dict) else None
    beat_times = tempo_value.get("beat_times") if isinstance(tempo_value, dict) else None
    downbeat_offset_s = tempo_value.get("downbeat_offset_seconds") if isinstance(tempo_value, dict) else None
    modes = analysis["transcription"].get("modes") or {}
    transcriber = router.for_target(target, modes)
    spec = router.spec_for_target(
        target, midi_tempo=midi_tempo, modes=modes, time_signature=time_signature,
        beat_times=beat_times, downbeat_offset_s=downbeat_offset_s,
        tempo_map=tempo_map_reference(tempo_value if isinstance(tempo_value, dict) else None),
    )
    profile = modes.get(target) or (variants.get(target_variant_id) or {}).get("requested_profile") or "default"
    effective_profile = (
        modes.get(target)
        or (variants.get(target_variant_id) or {}).get("effective_profile")
        or effective_profile_name_for_target(target, modes)
    )
    request = VariantRequest(
        variant_id=target_variant_id,
        label=label,
        requested_profile=profile,
        effective_profile=effective_profile,
        profile_definition_hash=(variants.get(target_variant_id) or {}).get("profile_definition_hash"),
        spec=spec,
        resolved_settings=(variants.get(target_variant_id) or {}).get("resolved_settings") or {"detection": {}, "cleanup": []},
    )
    resolved = resolve_target_source(project_path, target, analysis, reference_source=reference_source)
    source_path, artifact = resolved if resolved is not None else (None, None)
    input_hash = target_input_hash(source_path, artifact) if source_path is not None else None
    outcome = reconcile_variants(
        target=target,
        requests=[request],
        transcriber=transcriber,
        source=source_path,
        input_hash=input_hash,
        namespace_dir=artifact_namespace_dir(project_path, namespace),
        existing_variants=variants,
        detection_cache=analysis["transcription"].get("detection_cache"),
        force=force,
        emit=emit,
    )
    variants[target_variant_id] = outcome.variants[target_variant_id]
    record["variants"] = variants
    record["variant_order"] = [*order, target_variant_id] if target_variant_id not in order else list(order)
    record.setdefault("discarded_variants", [])
    analysis["transcription"]["detection_cache"] = outcome.detection_cache

    return record


def _tempo_map_beat_times(tempo_value: dict[str, Any], source: Path) -> list[float] | None:
    """Expand a synchronized REAPER step map into reference-relative beats.

    The dedicated ReaScript action intentionally stores map markers rather
    than copying the detector's beat array.  Re-detecting here would make
    later chord analysis disagree with the human-verified map, so derive its
    beat grid directly when the map is valid and the source duration is known.
    """
    if tempo_value.get("source") != "reaper-tempo-map" or tempo_value.get("mode") not in {"constant", "piecewise"}:
        return None
    try:
        bpm = float(tempo_value["bpm"])
        import soundfile
        duration = float(soundfile.info(source).duration)
    except (ImportError, KeyError, TypeError, ValueError, OSError, RuntimeError):
        return None
    if not math.isfinite(bpm) or bpm <= 0 or not math.isfinite(duration) or duration <= 0:
        return None
    markers: list[tuple[float, float]] = []
    for span in tempo_value.get("spans") or []:
        if not isinstance(span, dict):
            return None
        try:
            start, marker_bpm = float(span["start_seconds"]), float(span["bpm"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(start) or not math.isfinite(marker_bpm) or start <= 0 or marker_bpm <= 0:
            return None
        markers.append((start, marker_bpm))
    markers.sort()
    if len({start for start, _ in markers}) != len(markers):
        return None

    beats: list[float] = []
    marker_index = 0
    time = 0.0
    while time <= duration + 1e-9:
        beats.append(round(time, 6))
        next_marker = markers[marker_index][0] if marker_index < len(markers) else math.inf
        next_beat = time + 60.0 / bpm
        if next_marker < next_beat - 1e-9:
            time = next_marker
            bpm = markers[marker_index][1]
            marker_index += 1
        else:
            time = next_beat
            while marker_index < len(markers) and markers[marker_index][0] <= time + 1e-9:
                bpm = markers[marker_index][1]
                marker_index += 1
        if len(beats) > 1_000_000:  # corrupt maps must not exhaust the CLI
            return None
    return beats if len(beats) >= 2 else None


def _tempo_beat_times(tempo_value: dict[str, Any] | None, source: Path) -> list[float]:
    """Beat timestamps backing the chords stage's grid alignment: reused from
    the tempo stage's persisted value when present, otherwise (e.g. after a
    human correction that only set bpm/time-signature, dropping the raw beat
    array) re-detected fresh via the same primary/fallback ladder tempo.py
    uses -- never chords' own ad hoc beat tracker, so both stages always
    agree on one shared grid."""
    if tempo_value and tempo_value.get("beat_times"):
        return tempo_value["beat_times"]
    if tempo_value:
        synchronized_beats = _tempo_map_beat_times(tempo_value, source)
        if synchronized_beats is not None:
            return synchronized_beats
    try:
        beat_times, _beat_positions, _backend = detect_beats(source)
    except TempoDetectionError as exc:
        raise AnalysisError(str(exc)) from exc
    return beat_times


def chord_sources(project_path: Path, source: Path, analysis: dict[str, Any]) -> dict[str, Path]:
    """Return the mix plus usable optional stem artifacts for chord fusion.

    Artifact records are checkpointed independently by separation, so every
    record and file is treated as optional.  This deliberately excludes bass:
    the measured fusion recipe is original + instrumental + guitar + backing.
    """
    from .separation import artifact_path

    sources = {"original": source}
    artifacts = (analysis.get("stems") or {}).get("artifacts") or {}
    for name in FUSION_STEM_NAMES:
        artifact = artifacts.get(name)
        if not isinstance(artifact, dict) or not isinstance(artifact.get("file"), str):
            continue
        try:
            path = artifact_path(project_path, artifact)
        except (KeyError, TypeError, ValueError):
            _LOG.warning("Skipping chord-fusion source %s: malformed artifact record", name)
            continue
        if path.is_file():
            sources[name] = path
        else:
            _LOG.warning("Skipping chord-fusion source %s: artifact is missing (%s)", name, path)
    return sources


def _chord_source_identity(chord_source_paths: dict[str, Path]) -> tuple[str, dict[str, Path]]:
    """Return the chord cache identity and its validated source snapshot.

    Stem artifacts are optional and can disappear between their initial
    ``is_file`` check and this cache calculation (for example, while a user
    removes a failed download). Treat that race exactly like an unavailable
    stem rather than letting it abort the otherwise free chord stage. The
    returned mapping is used for decoding too, so the identity and the source
    set handed to the decoder cannot disagree.
    """
    identities = []
    validated_sources: dict[str, Path] = {}
    for name, path in chord_source_paths.items():
        try:
            identities.append((name, hash_source_file(path)))
            validated_sources[name] = path
        except OSError as exc:
            if name == "original":
                raise
            _LOG.warning("Skipping chord-fusion source %s while hashing (%s): %s", name, path, exc)
    return hashlib.sha256(json.dumps(identities, sort_keys=True).encode("utf-8")).hexdigest(), validated_sources


def detect_chords(
    project_path: Path,
    source: Path,
    settings: dict[str, Any],
    analysis: dict[str, Any],
    namespace: str,
    *,
    render_artifact: bool = True,
    sources: dict[str, Path] | None = None,
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
    tempo_value = analysis["tempo"]["value"]
    beat_times = _tempo_beat_times(tempo_value, source)
    try:
        chords_value = _detect_chords(
            source, beat_times, settings, tempo=tempo_value, sources=sources or chord_sources(project_path, source, analysis)
        )
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
    stages: tuple[str, ...] | None = None,
    transcriber: Transcriber | None = None,
    transcriber_router: TranscriberRouter | None = None,
    transcription_targets: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Run (or refresh) analysis for `project` and persist it into the sidecar.

    Idempotent: unchanged inputs/settings leave cached stage values untouched,
    and stages marked `human_verified` are never recomputed.

    `force` recomputes every stage even when its cache is current, but still
    preserves human-verified stages.

    `progress`, when given, is called with human-readable status lines as each
    stage starts (the detectors are otherwise silent for a minute or more); the
    CLI wires it to stderr so the JSON result on stdout stays pipe-clean.

    `transcriber_router` overrides the target-to-backend route used by the
    `transcription` stage.  The production router intentionally still sends
    every target (including drums) to Basic Pitch in D-A. `transcriber` is a
    backwards-compatible single-backend test hook; it is wrapped in the same
    router. `transcription_targets`, when given, overrides the
    persisted `requested_targets` for this run only -- implements
    `--transcribe-only` without touching the persisted set.
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
    selected_stages = stages or ANALYSIS_STAGES
    unknown_stages = set(selected_stages) - set(ANALYSIS_STAGES)
    if unknown_stages:
        raise AnalysisError(f"Unknown analysis stages: {', '.join(sorted(unknown_stages))}")
    emit(f"running {len(selected_stages)} detectors ({', '.join(selected_stages)}); first run can take a minute or two")

    input_hash = hash_source_file(source)
    analysis = sidecar["analysis"]
    namespace = ensure_artifact_namespace(sidecar, project_path)
    # Namespace allocation is Python-owned stem metadata, but do not replace
    # an existing operation/artifact index while recording it.
    def persist_namespace(current: dict[str, Any]) -> None:
        if current["stems"].get("artifact_namespace") is None:
            current["stems"]["artifact_namespace"] = namespace

    update_analysis(project_path, persist_namespace)
    total = len(selected_stages)
    for position, stage in enumerate(selected_stages, start=1):
        if stage == "transcription":
            # Owns a per-target index (see sidecar.py schema v10) rather than
            # the single input_hash/settings_hash pair this generic loop
            # drives, so each target is reconciled independently below.
            targets_to_run = (
                transcription_targets if transcription_targets is not None else analysis["transcription"]["requested_targets"]
            )
            emit(f"[{position}/{total}] transcription — reconciling {len(targets_to_run)} target(s)…")
            # Before anything writes: relocate any artifact a pre-v13 record
            # still points at, and sweep the flat leftovers a variant's first
            # re-reconcile stranded, so every target's artifacts share one
            # layout regardless of when that target was first transcribed.
            apply_artifact_layout(project_path, namespace, analysis, emit=emit)
            if transcriber is not None and transcriber_router is not None:
                raise AnalysisError("pass either transcriber or transcriber_router, not both")
            active_router = (
                transcriber_router
                or (TargetTranscriberRouter(transcriber, transcriber) if transcriber is not None else production_transcriber_router())
            )
            for target in targets_to_run:
                analysis["transcription"]["targets"][target] = _refresh_target(
                    project_path,
                    target,
                    analysis,
                    source,
                    namespace,
                    active_router,
                    force=force,
                    emit=emit,
                )
                # Each target's success (or failure) becomes durable
                # immediately, same as every other stage below -- a later
                # target failing must not roll back an earlier one.
                update_analysis(
                    project_path,
                    lambda current, target=target: (
                        current["transcription"]["targets"].__setitem__(
                            target, copy.deepcopy(analysis["transcription"]["targets"][target])
                        ),
                        current["transcription"].__setitem__(
                            "detection_cache", copy.deepcopy(analysis["transcription"].get("detection_cache", {}))
                        ),
                    ),
                )
            continue
        stage_settings = settings.get(stage, {})
        settings_hash = _hash_settings(stage_settings)
        chord_source_paths: dict[str, Path] | None = None
        if stage == "chords":
            stage_input_hash, chord_source_paths = _chord_source_identity(chord_sources(project_path, source, analysis))
        else:
            stage_input_hash = input_hash
        if analysis[stage].get("human_verified"):
            if stage in DETECTED_SPLIT_STAGES and (
                force
                or analysis[stage].get("detected_input_hash") != stage_input_hash
                or analysis[stage].get("detected_settings_hash") != settings_hash
            ):
                emit(f"[{position}/{total}] {stage} — human-verified value kept; refreshing detected baseline…")
            else:
                emit(f"[{position}/{total}] {stage} — human-verified, keeping")
        elif not force and stage_is_current(analysis[stage], input_hash=stage_input_hash, settings_hash=settings_hash):
            emit(f"[{position}/{total}] {stage} — unchanged, using cached result")
        elif force:
            emit(f"[{position}/{total}] {stage} — re-analyzing (forced)…")
        else:
            emit(f"[{position}/{total}] {stage} — analyzing…")
        refresh = _refresh_stage_with_detected if stage in DETECTED_SPLIT_STAGES else refresh_stage

        def compute_stage(*, stage: str = stage, stage_settings: dict[str, Any] = stage_settings, **kwargs: Any) -> Any:
            if stage == "chords":
                kwargs["sources"] = chord_source_paths
            return _DETECTORS[stage](project_path, source, stage_settings, analysis, namespace, **kwargs)

        refresh_kwargs = {
            "input_hash": stage_input_hash,
            "settings_hash": settings_hash,
            "compute": compute_stage,
            "force": force,
            "analyzed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        if stage in DETECTED_SPLIT_STAGES:
            refresh_kwargs["render_baseline_artifact"] = stage == "sections"
        analysis[stage] = refresh(analysis[stage], **refresh_kwargs)
        # A later detector or the paid separation stage may fail.  Each local
        # success therefore becomes durable immediately, not only at the end
        # of a full analysis run.
        # Merge only this detector's result.  A separator may be refreshing a
        # paid-operation checkpoint concurrently; never replace its stems
        # block with the snapshot this analysis run started with.
        update_analysis(
            project_path,
            lambda current, stage=stage: current.__setitem__(stage, copy.deepcopy(analysis[stage])),
        )
    emit("writing sidecar")
    analysis["provenance"] = {
        "tool": "vgt",
        "version": __version__,
        "settings": settings,
        "reference_source_path": str(source),
    }

    persisted = update_analysis(
        project_path,
        lambda current: current.__setitem__("provenance", copy.deepcopy(analysis["provenance"])),
    )
    return persisted


def add_transcription_targets(project: str | Path | None, targets: tuple[str, ...]) -> dict[str, Any]:
    """Persist `targets` into `analysis.transcription.requested_targets`,
    deduped and order-preserving. Mirrors `--extra-stem`'s persistence of
    opt-in separation requests: stating a target once is enough for every
    later run to keep refreshing it."""
    for target in targets:
        validate_target(target)
    project_path = locate_project(project)

    def update(current: dict[str, Any]) -> None:
        existing = current["transcription"]["requested_targets"]
        current["transcription"]["requested_targets"] = list(dict.fromkeys([*existing, *targets]))

    try:
        return update_analysis(project_path, update)
    except SidecarError as exc:
        raise AnalysisError(str(exc)) from exc


def set_transcription_modes(project: str | Path | None, modes: dict[str, str]) -> dict[str, Any]:
    """Persist validated target-to-profile selections in the sidecar."""
    for target, profile in modes.items():
        validate_profile_for_target(target, profile)
    project_path = locate_project(project)

    def update(current: dict[str, Any]) -> None:
        current["transcription"]["modes"].update(modes)

    try:
        return update_analysis(project_path, update)
    except SidecarError as exc:
        raise AnalysisError(str(exc)) from exc


def forget_transcription_targets(project: str | Path | None, targets: tuple[str, ...]) -> dict[str, Any]:
    """Remove `targets` from the persisted requested set, drop their
    `targets` index entries, and delete their MIDI/notes artifacts -- the
    only way a kept transcription goes away (see docs/transcription-plan.md
    section 4). A target never requested/computed is silently a no-op.

    A target that has retained multi-variant records (schema v13, see
    `vgt.transcription_lifecycle`) discards every one of its variants' own
    generated artifacts too -- "explicitly discards every generated variant
    for that target", per docs/transcription-variants-plan.md's CLI
    compatibility section -- and any raw detection cache entry left
    unreferenced afterward is garbage-collected, same as a single `variant
    discard` would.
    """
    for target in targets:
        validate_target(target)
    project_path = locate_project(project)
    try:
        sidecar = read_sidecar(project_path)
    except SidecarError as exc:
        raise AnalysisError(str(exc)) from exc

    namespace = sidecar["analysis"]["stems"].get("artifact_namespace")
    if namespace:
        namespace_dir = artifact_namespace_dir(project_path, namespace)
        for target in targets:
            for name in (midi_artifact_name(target), notes_artifact_name(target), events_artifact_name(target)):
                path = namespace_dir / name
                if path.is_file():
                    path.unlink()
            variant_ids = (sidecar["analysis"]["transcription"]["targets"].get(target) or {}).get("variants") or {}
            for variant_id in variant_ids:
                for name in (
                    variant_midi_name(target, variant_id),
                    variant_notes_name(target, variant_id),
                    variant_events_name(target, variant_id),
                ):
                    path = namespace_dir / name
                    if path.is_file():
                        path.unlink()

    def update(current: dict[str, Any]) -> None:
        transcription = current["transcription"]
        transcription["requested_targets"] = [t for t in transcription["requested_targets"] if t not in targets]
        for target in targets:
            transcription["targets"].pop(target, None)
        if namespace:
            kept_cache, _removed = garbage_collect_raw_cache(
                namespace_dir=artifact_namespace_dir(project_path, namespace),
                detection_cache=transcription.get("detection_cache") or {},
                targets=transcription["targets"],
            )
            transcription["detection_cache"] = kept_cache

    return update_analysis(project_path, update)
