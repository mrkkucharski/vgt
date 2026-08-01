"""Variant lifecycle CLI surface: add/rename/discard/purge-discarded for the
multi-variant transcription model (issue #150, section C of
docs/transcription-variants-plan.md; selection removed by #176).

This module owns the sidecar-facing CRUD layer over one target's `variants`/
`variant_order`/`discarded_variants` index. Retained variants are peers,
ordered only for stable presentation -- none is designated preferred,
active, best, or selected. It never duplicates execution logic: `add_variant`
is the only operation that runs a backend, and it does so by handing a
single `VariantRequest` to `vgt.transcription_variants.reconcile_variants` --
the same engine issue #149 built -- so a new variant shares a raw detection
cache entry with any existing variant whose `detection_hash` matches, exactly
as the plan's "Execution behavior" section requires. `rename_variant` never
touches a backend or an artifact; it only ever moves a sidecar pointer (see
the plan's "Rename ... does not rerun transcription" verification
requirement).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import secrets

from .analysis import apply_artifact_layout, reference_source_path
from .drum_cleanup import DRUM_CLEANUP_PROFILES
from .project import locate_project
from .sidecar import (
    artifact_namespace_dir,
    ensure_artifact_namespace,
    migrate_transcription_target,
    read_sidecar,
    update_analysis,
)
from .transcribe import (
    TranscriberRouter,
    TranscriptionError,
    default_spec_for_target,
    backend_for_target_profile,
    effective_profile_name_for_target,
    canonical_drum_profile_name,
    drum_transcription_profile,
    production_transcriber_router,
    resolve_target_source,
    target_input_hash,
    tempo_map_reference,
    valid_profile_names_for_target as builtin_profile_names_for_target,
    validate_target,
)
from .transcription_profiles import (
    ProfileDefinitionError,
    load_project_profiles,
    resolved_settings_snapshot,
    spec_from_resolved_profile,
    validate_profile_for_target,
)
from .transcription_variants import (
    VariantRequest,
    garbage_collect_raw_cache,
    reconcile_variants,
)


class VariantLifecycleError(TranscriptionError):
    """A variant lifecycle operation (add/rename/select/discard/purge) cannot be completed."""


# Fields kept on a discarded variant's compact audit record (see the plan's
# "`discarded_variants` is a compact optional audit list" note): the recipe
# and summary metrics needed to describe what was tried and rejected, never a
# live artifact path -- those are deleted, not archived.
_DISCARDED_RECORD_FIELDS: tuple[str, ...] = (
    "label", "requested_profile", "profile_definition_hash", "effective_profile",
    # Keep every identity component and resolved recipe needed to reconstruct
    # an experiment, but deliberately exclude the three live artifact paths.
    "backend", "package_pin", "serialization", "source_role", "input_hash",
    "settings_hash", "detection_hash", "raw_notes_hash", "cleanup_hash",
    "resolved_settings", "status", "note_count", "event_count", "instrument_counts",
    "pitch_range_midi", "first_note_s", "last_note_s", "max_note_duration_s", "max_simultaneous_voices",
    "first_event_s", "last_event_s", "backend_tempo", "midi_tempo", "confidence",
    "transcribed_at", "error",
)

_LEGACY_FLAT_FIELDS: tuple[str, ...] = (
    "backend", "package_pin", "serialization", "source_role", "input_hash", "settings_hash",
    "status", "midi_file", "notes_file", "events_file", "note_count", "event_count",
    "instrument_counts", "pitch_range_midi", "first_note_s", "last_note_s", "first_event_s",
    "last_event_s", "backend_tempo", "midi_tempo", "confidence", "settings", "transcribed_at", "error",
)


def _empty_target_record() -> dict[str, Any]:
    return {"variants": {}, "variant_order": [], "discarded_variants": []}


def target_record(transcription: dict[str, Any], target: str) -> dict[str, Any]:
    """The schema-v14 `variants`/`variant_order`/`discarded_variants` view
    for `target`, migrating a pre-v13 flat record eagerly (rather than only
    at read time, see `sidecar.upgrade`) so a lifecycle mutation writing this
    record back never drops a legacy `--transcribe`/`--mode` result it hasn't
    been asked to touch."""
    record = transcription.get("targets", {}).get(target)
    if not isinstance(record, dict):
        return _empty_target_record()
    migrated = migrate_transcription_target(target, record, transcription.get("modes") or {})
    migrated.setdefault("variants", {})
    migrated.setdefault("variant_order", [])
    migrated.setdefault("discarded_variants", [])
    # A lifecycle mutation promotes a flat compatibility record to the
    # variants-only shape.  Leaving its old top-level `status` in place would
    # make later analyze runs mistake it for a legacy target and overwrite
    # the retained alternatives through the one-result path.
    for field in _LEGACY_FLAT_FIELDS:
        migrated.pop(field, None)
    # Selection was removed by #176; strip any pointer a not-yet-upgraded
    # schema-13 record still carries rather than reviving it.
    migrated.pop("selected_variant_id", None)
    return migrated


def resolve_variant_ref(record: dict[str, Any], ref: str) -> str:
    """Resolve `ref` -- an immutable variant id or an unambiguous per-target
    label -- to a live variant id. An id always wins outright (ids are
    opaque and unique by construction, see `_new_variant_id`); a label is
    only accepted when exactly one live variant currently carries it (see
    the plan's "Duplicate labels within one target are rejected" and
    "reject ... ambiguous labels" requirements)."""
    variants = record.get("variants") or {}
    if ref in variants:
        return ref
    matches = sorted(
        variant_id for variant_id, variant in variants.items()
        if isinstance(variant, dict) and variant.get("label") == ref
    )
    if not matches:
        raise VariantLifecycleError(f"no retained variant matches {ref!r}")
    if len(matches) > 1:
        raise VariantLifecycleError(f"{ref!r} is ambiguous among variant ids {matches}; use the immutable id instead")
    return matches[0]


def _reject_duplicate_label(record: dict[str, Any], label: str, *, exclude_variant_id: str | None = None) -> None:
    for variant_id, variant in (record.get("variants") or {}).items():
        if variant_id == exclude_variant_id:
            continue
        if isinstance(variant, dict) and variant.get("label") == label:
            raise VariantLifecycleError(f"label {label!r} is already used by variant {variant_id!r} for this target")


def _new_variant_id(record: dict[str, Any]) -> str:
    """An opaque, filesystem-safe id (see the plan's "Variant IDs are
    opaque ... Labels never appear in paths"), distinct from every id this
    target has ever used -- including a discarded one, so a purged variant's
    old artifacts can never collide with a fresh one reusing its id."""
    used = set((record.get("variants") or {}).keys())
    used.update(entry.get("id") for entry in (record.get("discarded_variants") or []) if isinstance(entry, dict))
    while True:
        candidate = secrets.token_hex(4)
        if candidate not in used:
            return candidate


def add_variant(
    project: str | Path | None,
    target: str,
    *,
    label: str,
    profile: str,
    force: bool = False,
    router: TranscriberRouter | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Persist a new named variant for `target` and reconcile it immediately
    (see the plan's "`variant add` persists configuration and then
    runs/reconciles that variant by default").

    Sharing a raw Basic Pitch detection with an existing variant of this
    target is `reconcile_variants`'s job given the current
    `detection_cache` -- this function never re-derives or reruns another
    variant. A new variant is simply appended to `variant_order`; it is
    never designated preferred, active, best, or selected (#176).
    """
    validate_target(target)
    if not label:
        raise VariantLifecycleError("a variant name/label is required")
    emit = progress or (lambda _message: None)
    project_path = locate_project(project)
    sidecar = read_sidecar(project_path)
    analysis = sidecar["analysis"]
    transcription = analysis["transcription"]
    record = target_record(transcription, target)
    _reject_duplicate_label(record, label)

    project_profiles = load_project_profiles(project_path)
    tempo_value = analysis["tempo"].get("value")
    midi_tempo = tempo_value.get("bpm") if isinstance(tempo_value, dict) else None
    time_signature = tempo_value.get("time_signature") if isinstance(tempo_value, dict) else None
    beat_times = tempo_value.get("beat_times") if isinstance(tempo_value, dict) else None
    downbeat_offset_s = tempo_value.get("downbeat_offset_seconds") if isinstance(tempo_value, dict) else None
    tempo_map = tempo_map_reference(tempo_value if isinstance(tempo_value, dict) else None)

    if target == "drums":
        if profile in project_profiles:
            try:
                resolved = validate_profile_for_target(profile, target, project_profiles)
            except ProfileDefinitionError as exc:
                raise VariantLifecycleError(str(exc)) from exc
            drum_base = resolved.detection["drum_profile"]
            drum_profile = drum_transcription_profile({target: drum_base})
            effective_profile = resolved.name
            profile_definition_hash = resolved.profile_definition_hash
            audio_frontend = resolved.audio_frontend
        else:
            if profile not in builtin_profile_names_for_target("drums"):
                raise VariantLifecycleError(
                    f"profile for 'drums' must be one of {builtin_profile_names_for_target('drums')}, got {profile!r}"
                )
            drum_profile = drum_transcription_profile({target: profile})
            effective_profile = canonical_drum_profile_name(profile)
            profile_definition_hash = None
            audio_frontend = dict(drum_profile.audio_frontend)
        resolved_settings = (
            {"backend": drum_profile.backend}
            if drum_profile.cleanup_profile is None
            else {
                "cleanup_profile": drum_profile.cleanup_profile,
                **DRUM_CLEANUP_PROFILES[drum_profile.cleanup_profile].as_identity(),
            }
        )
        resolved_settings["audio_frontend"] = audio_frontend
        routing_modes = {target: drum_profile.name}
        spec = default_spec_for_target(
            target, backend=backend_for_target_profile(target, {target: drum_profile.name}), midi_tempo=midi_tempo,
            time_signature=time_signature, modes={target: drum_profile.name}, beat_times=beat_times,
            downbeat_offset_s=downbeat_offset_s, tempo_map=tempo_map,
        )
    else:
        try:
            # A requested `default` selects this target's default profile
            # (pYIN for bass), rather than the global Basic Pitch preset.
            # Project-local names are not in the builtin registry, so they
            # must reach the resolver directly rather than being folded back
            # to the target default by `effective_profile_name_for_target`.
            selected_profile = profile if profile in project_profiles else effective_profile_name_for_target(target, {target: profile})
            resolved = validate_profile_for_target(
                selected_profile, target, project_profiles
            )
        except ProfileDefinitionError as exc:
            raise VariantLifecycleError(str(exc)) from exc
        effective_profile = resolved.name
        profile_definition_hash = resolved.profile_definition_hash
        resolved_settings = resolved_settings_snapshot(resolved)
        spec = spec_from_resolved_profile(resolved, midi_tempo=midi_tempo, time_signature=time_signature, tempo_map=tempo_map)
        audio_frontend = resolved.audio_frontend
        routing_modes = {target: effective_profile}

    namespace = ensure_artifact_namespace(sidecar, project_path)

    def persist_namespace(current: dict[str, Any]) -> None:
        # Mirrors `analysis.py`'s identical `persist_namespace`: `stems`
        # always carries an explicit `artifact_namespace: None` key (see
        # `sidecar._empty_stems_block`), so `setdefault` would never
        # overwrite it -- an `is None` check is required.
        if current["stems"].get("artifact_namespace") is None:
            current["stems"]["artifact_namespace"] = namespace

    update_analysis(project_path, persist_namespace)
    namespace_dir = artifact_namespace_dir(project_path, namespace)
    # Adding a variant to a target whose first result predates the per-target
    # layout must not leave that older artifact behind at its flat path (#223).
    # Re-derive the target view afterwards: the relocation rewrites the
    # retained variants' recorded paths, and `reconcile_variants` below is
    # handed exactly those records as its `existing_variants`.
    apply_artifact_layout(project_path, namespace, analysis, emit=emit)
    record = target_record(transcription, target)

    resolved_source = resolve_target_source(project_path, target, analysis, reference_source=reference_source_path(project_path, sidecar))
    source, artifact = resolved_source if resolved_source is not None else (None, None)
    input_hash = target_input_hash(source, artifact) if source is not None else None

    variant_id = _new_variant_id(record)
    request = VariantRequest(
        variant_id=variant_id,
        label=label,
        requested_profile=profile,
        effective_profile=effective_profile,
        profile_definition_hash=profile_definition_hash,
        spec=spec,
        resolved_settings=resolved_settings,
        audio_frontend=audio_frontend,
    )

    active_router = router or production_transcriber_router()
    # Route by the resolved profile, not the user's requested alias. In
    # particular, `bass/default` resolves to the bass pYIN profile.
    transcriber = active_router.for_target(target, routing_modes)

    outcome = reconcile_variants(
        target=target,
        requests=[request],
        transcriber=transcriber,
        source=source,
        input_hash=input_hash,
        namespace_dir=namespace_dir,
        existing_variants=record.get("variants"),
        detection_cache=transcription.get("detection_cache"),
        force=force,
        emit=emit,
    )
    new_variant = outcome.variants[variant_id]

    def commit(current: dict[str, Any]) -> None:
        current_transcription = current["transcription"]
        current_transcription.setdefault("targets", {})
        # `variant add` is a persistent request just like the established
        # `--transcribe TARGET` flag.  Without this, adding a variant for a
        # target that was not already requested would leave it invisible to
        # status and untouched by later `vgt analyze` runs.
        requested = current_transcription.setdefault("requested_targets", [])
        if target not in requested:
            requested.append(target)
        current_record = target_record(current_transcription, target)
        current_record["variants"][variant_id] = new_variant
        current_record["variant_order"] = [*current_record["variant_order"], variant_id]
        current_transcription["targets"][target] = current_record
        current_transcription.setdefault("detection_cache", {})
        for detection_hash_value, entry in outcome.detection_cache.items():
            current_transcription["detection_cache"][detection_hash_value] = entry

    persisted = update_analysis(project_path, commit)
    return persisted["analysis"]["transcription"]["targets"][target]["variants"][variant_id]


def rename_variant(project: str | Path | None, target: str, ref: str, *, new_label: str) -> dict[str, Any]:
    """Rename a retained variant's label without touching its artifacts,
    cache references, or any hash -- purely a sidecar pointer edit (see the
    plan's "Renaming ... must not move artifacts, rerun a model, or make
    REAPER lose the variant" and the acceptance criterion that rename never
    reruns transcription)."""
    validate_target(target)
    if not new_label:
        raise VariantLifecycleError("a new variant name/label is required")
    project_path = locate_project(project)

    def update(current: dict[str, Any]) -> None:
        transcription = current["transcription"]
        transcription.setdefault("targets", {})
        record = target_record(transcription, target)
        variant_id = resolve_variant_ref(record, ref)
        _reject_duplicate_label(record, new_label, exclude_variant_id=variant_id)
        record["variants"][variant_id]["label"] = new_label
        transcription["targets"][target] = record

    return update_analysis(project_path, update)["analysis"]["transcription"]["targets"][target]


def _delete_variant_artifacts(namespace_dir: Path, variant: dict[str, Any]) -> None:
    """Delete only artifacts explicitly recorded by ``variant``.

    A variant id determines the normal output names, but is not itself an
    authorization to delete them: legacy variants can retain old paths and a
    malformed sidecar must never turn discard into a broad or out-of-tree
    deletion.  Resolve every recorded path and accept it only when it remains
    inside this project's artifact namespace.
    """
    namespace_root = namespace_dir.resolve()
    for key in ("midi_file", "notes_file", "events_file"):
        relative = variant.get(key)
        if not isinstance(relative, str):
            continue
        path = (namespace_dir / relative).resolve()
        try:
            path.relative_to(namespace_root)
        except ValueError:
            continue
        if path.is_file():
            path.unlink()


def discard_variant(project: str | Path | None, target: str, ref: str) -> dict[str, Any]:
    """Discard one retained variant: delete its own generated artifacts,
    remove it from the live index, archive a compact recipe/metrics record,
    and garbage-collect any raw detection entry no other live variant still
    references (see the plan's "Discarding one variant" steps).

    Any retained variant may be discarded directly -- retained variants are
    peers, so there is no replacement/clear requirement to satisfy first
    (#176 removed selection).
    """
    validate_target(target)
    project_path = locate_project(project)
    sidecar = read_sidecar(project_path)
    analysis = sidecar["analysis"]
    transcription = analysis["transcription"]
    record = target_record(transcription, target)
    variant_id = resolve_variant_ref(record, ref)

    namespace = analysis["stems"].get("artifact_namespace")
    if namespace:
        _delete_variant_artifacts(
            artifact_namespace_dir(project_path, namespace), record["variants"][variant_id]
        )

    def update(current: dict[str, Any]) -> None:
        transcription = current["transcription"]
        transcription.setdefault("targets", {})
        current_record = target_record(transcription, target)
        discarded_variant = current_record["variants"].pop(variant_id, None) or record["variants"][variant_id]
        archived = {
            "id": variant_id,
            "discarded_at": _now(),
            **{key: discarded_variant.get(key) for key in _DISCARDED_RECORD_FIELDS},
        }
        current_record["variant_order"] = [vid for vid in current_record["variant_order"] if vid != variant_id]
        current_record["discarded_variants"] = [*current_record["discarded_variants"], archived]
        transcription["targets"][target] = current_record
        if namespace:
            kept_cache, _removed = garbage_collect_raw_cache(
                namespace_dir=artifact_namespace_dir(project_path, namespace),
                detection_cache=transcription.get("detection_cache") or {},
                targets=transcription["targets"],
            )
            transcription["detection_cache"] = kept_cache

    return update_analysis(project_path, update)["analysis"]["transcription"]["targets"][target]


def purge_discarded(project: str | Path | None, target: str) -> dict[str, Any]:
    """Clear `target`'s compact discarded-variant audit list -- a separate,
    deliberate operation from discard itself (see the plan's "Remove
    archived recipe metadata as a separate deliberate operation")."""
    validate_target(target)
    project_path = locate_project(project)

    def update(current: dict[str, Any]) -> None:
        transcription = current["transcription"]
        transcription.setdefault("targets", {})
        record = target_record(transcription, target)
        record["discarded_variants"] = []
        transcription["targets"][target] = record

    return update_analysis(project_path, update)["analysis"]["transcription"]["targets"][target]


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
