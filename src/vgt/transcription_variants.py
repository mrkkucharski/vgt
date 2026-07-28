"""Two-level transcription variant reconciliation: raw Basic Pitch detection
grouped and cached by `detection_hash`, with cleanup variants derived
independently from the shared raw notes (issue #149, section B of
docs/transcription-variants-plan.md).

This module owns the *execution* engine only: given a target's requested
variants (each already resolved to a `BasicPitchSpec`/`DrumScriptSpec` by a
caller through `vgt.transcribe`/`vgt.transcription_profiles`), it groups
Basic Pitch variants by detection identity, runs the backend at most once per
uncached group, derives every variant's cleanup independently, and persists
each result durably and immediately so one failure never rolls back another.
It does not decide *what* variants a project wants -- that CLI/status surface
is a later issue (see the plan's implementation sequence, section C) -- and it
does not read or write the sidecar itself; a caller hands in the relevant
slice of `analysis.transcription` and receives back the updated variant
records and detection-cache entries to persist, mirroring how `analysis.py`'s
`_refresh_target` already treats a `targets[target]` entry as data its caller
owns.

Two identities drive this module (see the plan's "Two-level cache" section):

- `detection_hash`: target + source content hash + backend/package/
  serialization identity + every Basic Pitch detection setting + MIDI tempo
  metadata. Two variants with equal `detection_hash` share one Basic Pitch
  inference -- exactly why `guitar-acoustic-detail` and
  `guitar-acoustic-clean` are declared with identical detector settings in
  `vgt.transcribe`.
- `cleanup_hash` (the plan calls this a derived variant's identity): raw
  note-event content hash + source-audio content hash + the variant's ordered
  cleanup settings. The source-audio hash is included even though cleanup
  itself doesn't read the raw CSV again, because acoustic ghost confirmation
  reads the actual waveform -- a source change must invalidate a
  detection-unchanged variant too.

DrumScript targets have no raw/derived split (see
`vgt.transcribe.RawDetectionResult`'s docstring): `reconcile_variants` runs
`transcriber.transcribe()` directly for a `DrumScriptSpec` variant and never
groups or caches it, keeping DrumScript on the same variant record shape
without inventing an unsupported drum cleanup profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
import hashlib
import json
import shutil

from .transcribe import (
    BasicPitchSpec,
    DrumScriptSpec,
    ParsedNote,
    Transcriber,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSpec,
    derive_variant_artifacts,
    parse_notes_csv,
    raw_notes_content_hash,
    spec_hash,
    validate_target,
)

CACHE_BACKEND_DIR = "basic-pitch"


@dataclass(frozen=True)
class VariantRequest:
    """One variant a caller wants reconciled for one target.

    `spec` is the fully resolved backend spec this variant's profile
    produces -- built by the caller via
    `vgt.transcribe.default_spec_for_target`/`vgt.transcription_profiles`, so
    this module never re-derives profile settings itself and stays agnostic
    to where a profile came from (builtin vs. project-local).
    """

    variant_id: str
    label: str
    requested_profile: str
    effective_profile: str
    profile_definition_hash: str | None
    spec: TranscriptionSpec
    resolved_settings: dict[str, Any]


@dataclass
class ReconcileOutcome:
    """What one `reconcile_variants` call produced for one target."""

    variants: dict[str, dict[str, Any]]
    detection_cache: dict[str, dict[str, Any]]
    backend_invocations: int


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True)


def _hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def detection_identity(target: str, input_hash: str, spec: BasicPitchSpec) -> dict[str, Any]:
    """Everything that determines one Basic Pitch inference (see the plan's
    "Layer 1: raw detection" section). Two specs with equal identity here can
    share one raw detection run regardless of which profile produced them."""
    return {
        "target": target,
        "input_hash": input_hash,
        "backend": spec.backend,
        "package_pin": spec.package_pin,
        "serialization": spec.serialization,
        "onset_threshold": spec.onset_threshold,
        "frame_threshold": spec.frame_threshold,
        "minimum_note_length_ms": spec.minimum_note_length_ms,
        "minimum_frequency_hz": spec.minimum_frequency_hz,
        "maximum_frequency_hz": spec.maximum_frequency_hz,
        "multiple_pitch_bends": spec.multiple_pitch_bends,
        "melodia_trick": spec.melodia_trick,
        # Basic Pitch embeds this in the raw MIDI it emits, so it is part of
        # what determines the raw artifact even though it never changes note
        # timings themselves (see the plan's "MIDI tempo metadata" note).
        "midi_tempo": spec.midi_tempo,
        # The raw MIDI is later materialized into a project-relative MIDI
        # artifact.  A changed REAPER marker timeline must therefore get a
        # fresh detection-cache identity as well as a fresh derived variant.
        "tempo_map": spec.tempo_map.to_dict() if spec.tempo_map is not None else None,
    }


def detection_hash(target: str, input_hash: str, spec: BasicPitchSpec) -> str:
    return _hash(detection_identity(target, input_hash, spec))


def cleanup_identity(raw_notes_hash: str, source_audio_hash: str, spec: BasicPitchSpec) -> dict[str, Any]:
    """Everything that determines one derived cleanup variant beyond its raw
    detection (see the plan's "Layer 2: derived variant" section): the raw
    note-event content, the source audio (ghost confirmation reads the
    waveform), and the ordered, parameterized cleanup pipeline."""
    return {
        "raw_notes_hash": raw_notes_hash,
        "source_audio_hash": source_audio_hash,
        "cleanup": [{"name": stage.name, "params": stage.params} for stage in spec.cleanup],
    }


def cleanup_hash(raw_notes_hash: str, source_audio_hash: str, spec: BasicPitchSpec) -> str:
    return _hash(cleanup_identity(raw_notes_hash, source_audio_hash, spec))


def detection_cache_root(namespace_dir: Path) -> Path:
    return namespace_dir / "transcription" / "cache" / CACHE_BACKEND_DIR


def _raw_midi_name(detection_hash_value: str) -> str:
    return f"transcription/cache/{CACHE_BACKEND_DIR}/{detection_hash_value}/raw.mid"


def _raw_notes_name(detection_hash_value: str) -> str:
    return f"transcription/cache/{CACHE_BACKEND_DIR}/{detection_hash_value}/raw.csv"


def variant_midi_name(target: str, variant_id: str) -> str:
    return f"transcription/{target}/{variant_id}.mid"


def variant_notes_name(target: str, variant_id: str) -> str:
    return f"transcription/{target}/{variant_id}.csv"


def variant_events_name(target: str, variant_id: str) -> str:
    return f"transcription/{target}/{variant_id}.json"


def _atomic_replace(local_path: Path, final_path: Path) -> None:
    """Move a backend's local output into its final artifact location via a
    same-directory temp name and rename, mirroring `analysis._replace_artifact`
    so a crash mid-write never leaves a half-written artifact at the final
    path."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_final = final_path.with_suffix(final_path.suffix + ".part")
    shutil.move(str(local_path), str(tmp_final))
    tmp_final.replace(final_path)


def _base_variant_fields(request: VariantRequest, target: str) -> dict[str, Any]:
    spec = request.spec
    return {
        "label": request.label,
        "requested_profile": request.requested_profile,
        "profile_definition_hash": request.profile_definition_hash,
        "effective_profile": request.effective_profile,
        "backend": spec.backend,
        "package_pin": spec.package_pin,
        "serialization": spec.serialization if isinstance(spec, BasicPitchSpec) else None,
        "source_role": target,
        "settings_hash": spec_hash(spec),
        "resolved_settings": request.resolved_settings,
        "midi_tempo": spec.midi_tempo if isinstance(spec, BasicPitchSpec) else None,
    }


def _missing_source_variant(request: VariantRequest, target: str) -> dict[str, Any]:
    return {
        **_base_variant_fields(request, target),
        "input_hash": None,
        "detection_hash": None,
        "raw_notes_hash": None,
        "cleanup_hash": None,
        "status": "skipped-missing-source",
        "midi_file": None,
        "notes_file": None,
        "events_file": None,
        "note_count": None,
        "event_count": None,
        "instrument_counts": None,
        "pitch_range_midi": None,
        "first_note_s": None,
        "last_note_s": None,
        "max_note_duration_s": None,
        "max_simultaneous_voices": None,
        "first_event_s": None,
        "last_event_s": None,
        "transcribed_at": None,
        "error": None,
    }


def _error_variant(
    request: VariantRequest,
    target: str,
    input_hash: str | None,
    detection_hash_value: str | None,
    error: str,
    *,
    cleanup_hash_value: str | None = None,
) -> dict[str, Any]:
    return {
        **_base_variant_fields(request, target),
        "input_hash": input_hash,
        "detection_hash": detection_hash_value,
        "raw_notes_hash": None,
        "cleanup_hash": cleanup_hash_value,
        "status": "error",
        "midi_file": None,
        "notes_file": None,
        "events_file": None,
        "note_count": None,
        "event_count": None,
        "instrument_counts": None,
        "pitch_range_midi": None,
        "first_note_s": None,
        "last_note_s": None,
        "max_note_duration_s": None,
        "max_simultaneous_voices": None,
        "first_event_s": None,
        "last_event_s": None,
        "transcribed_at": None,
        "error": error,
    }


def _transcribed_basic_pitch_variant(
    request: VariantRequest,
    target: str,
    input_hash: str,
    detection_hash_value: str,
    cleanup_hash_value: str,
    raw_notes_hash: str,
    result: TranscriptionResult,
) -> dict[str, Any]:
    return {
        **_base_variant_fields(request, target),
        "input_hash": input_hash,
        "detection_hash": detection_hash_value,
        "raw_notes_hash": raw_notes_hash,
        "cleanup_hash": cleanup_hash_value,
        "status": "transcribed",
        "midi_file": variant_midi_name(target, request.variant_id),
        "notes_file": variant_notes_name(target, request.variant_id),
        "events_file": None,
        "note_count": result.note_count,
        "event_count": None,
        "instrument_counts": None,
        "pitch_range_midi": list(result.pitch_range_midi) if result.pitch_range_midi else None,
        "first_note_s": result.first_note_s,
        "last_note_s": result.last_note_s,
        "max_note_duration_s": result.max_note_duration_s,
        "max_simultaneous_voices": result.max_simultaneous_voices,
        "first_event_s": None,
        "last_event_s": None,
        "transcribed_at": _now(),
        "error": None,
    }


def _transcribed_drumscript_variant(
    request: VariantRequest, target: str, input_hash: str, result: TranscriptionResult
) -> dict[str, Any]:
    settings_hash = spec_hash(request.spec)
    return {
        **_base_variant_fields(request, target),
        "input_hash": input_hash,
        # DrumScript has no raw/derived split (see the module docstring):
        # its one settings hash stands in for both cache layers so the
        # variant record still carries every field the common model expects.
        "detection_hash": settings_hash,
        "raw_notes_hash": None,
        "cleanup_hash": settings_hash,
        # DrumScript's MIDI tempo is detected from its own output, unlike
        # Basic Pitch's, which is an input setting -- see
        # `vgt.transcribe.transcribed_entry`'s identical distinction.
        "backend_tempo": result.backend_tempo,
        "midi_tempo": result.midi_tempo,
        "status": "transcribed",
        "midi_file": variant_midi_name(target, request.variant_id),
        "notes_file": None,
        "events_file": variant_events_name(target, request.variant_id),
        "note_count": None,
        "event_count": result.event_count,
        "instrument_counts": result.instrument_counts,
        "pitch_range_midi": None,
        "first_note_s": None,
        "last_note_s": None,
        "max_note_duration_s": None,
        "max_simultaneous_voices": None,
        "first_event_s": result.first_event_s,
        "last_event_s": result.last_event_s,
        "transcribed_at": _now(),
        "error": None,
    }


def _existing_basic_pitch_current(
    existing: Any, *, input_hash: str, detection_hash_value: str, cleanup_hash_value: str
) -> bool:
    return (
        isinstance(existing, dict)
        and existing.get("status") == "transcribed"
        and existing.get("input_hash") == input_hash
        and existing.get("detection_hash") == detection_hash_value
        and existing.get("cleanup_hash") == cleanup_hash_value
    )


def _existing_drumscript_current(existing: Any, *, input_hash: str, settings_hash: str) -> bool:
    return (
        isinstance(existing, dict)
        and existing.get("status") == "transcribed"
        and existing.get("input_hash") == input_hash
        and existing.get("settings_hash") == settings_hash
    )


def _reconcile_drumscript_variant(
    request: VariantRequest,
    *,
    target: str,
    transcriber: Transcriber,
    source: Path,
    input_hash: str,
    namespace_dir: Path,
    existing: Any,
    force: bool,
    emit: Callable[[str], None],
) -> tuple[dict[str, Any], int]:
    settings_hash = spec_hash(request.spec)
    if not force and _existing_drumscript_current(existing, input_hash=input_hash, settings_hash=settings_hash):
        emit(f"transcription — {target}/{request.label}: unchanged, using cached result")
        return existing, 0

    work_dir = namespace_dir / "transcription" / f"_work-{target}-{request.variant_id}"
    try:
        try:
            result = transcriber.transcribe(source, work_dir, request.spec, progress=emit)
        except TranscriptionError as exc:
            emit(f"transcription error for {target}/{request.label}: {exc}")
            return _error_variant(request, target, input_hash, None, str(exc)), 1

        _atomic_replace(result.midi_path, namespace_dir / variant_midi_name(target, request.variant_id))
        if result.events_path is not None:
            _atomic_replace(result.events_path, namespace_dir / variant_events_name(target, request.variant_id))
    finally:
        if work_dir.is_dir():
            shutil.rmtree(work_dir, ignore_errors=True)

    return _transcribed_drumscript_variant(request, target, input_hash, result), 1


def _obtain_raw_group(
    *,
    detection_hash_value: str,
    representative_spec: BasicPitchSpec,
    target: str,
    transcriber: Transcriber,
    source: Path,
    input_hash: str,
    namespace_dir: Path,
    detection_cache: dict[str, dict[str, Any]],
    force: bool,
    emit: Callable[[str], None],
) -> tuple[str | None, Path | None, str | None, int]:
    """Return `(raw_notes_hash, raw_notes_path, error, invocation_count)` for
    one detection group, running Basic Pitch at most once. On success,
    `detection_cache[detection_hash_value]` is updated in place so the caller
    can persist it immediately (see `reconcile_variants`)."""
    raw_midi_path = namespace_dir / _raw_midi_name(detection_hash_value)
    raw_notes_path = namespace_dir / _raw_notes_name(detection_hash_value)
    entry = detection_cache.get(detection_hash_value)
    cache_valid = (
        not force
        and isinstance(entry, dict)
        and entry.get("input_hash") == input_hash
        and entry.get("target") == target
        and raw_midi_path.is_file()
        and raw_notes_path.is_file()
        and isinstance(entry.get("raw_notes_hash"), str)
    )
    if cache_valid:
        return entry["raw_notes_hash"], raw_notes_path, None, 0

    work_dir = namespace_dir / "transcription" / "_work-detection" / detection_hash_value
    try:
        try:
            emit(f"transcription — {target}: running detection for group {detection_hash_value[:8]}…")
            raw = transcriber.detect_raw(source, work_dir, representative_spec, progress=emit)
        except TranscriptionError as exc:
            emit(f"transcription error for {target} detection group {detection_hash_value[:8]}: {exc}")
            return None, None, str(exc), 1

        raw_midi_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace(raw.raw_midi_path, raw_midi_path)
        _atomic_replace(raw.raw_notes_path, raw_notes_path)
    finally:
        if work_dir.is_dir():
            shutil.rmtree(work_dir, ignore_errors=True)

    raw_hash = raw_notes_content_hash(raw_notes_path)
    detection_cache[detection_hash_value] = {
        "target": target,
        "input_hash": input_hash,
        "raw_midi_file": _raw_midi_name(detection_hash_value),
        "raw_notes_file": _raw_notes_name(detection_hash_value),
        "raw_notes_hash": raw_hash,
        "created_at": _now(),
    }
    return raw_hash, raw_notes_path, None, 1


def reconcile_variants(
    *,
    target: str,
    requests: list[VariantRequest],
    transcriber: Transcriber,
    source: Path | None,
    input_hash: str | None,
    namespace_dir: Path,
    existing_variants: dict[str, dict[str, Any]] | None = None,
    detection_cache: dict[str, dict[str, Any]] | None = None,
    force: bool = False,
    persist_variant: Callable[[str, dict[str, Any]], None] | None = None,
    persist_detection_cache_entry: Callable[[str, dict[str, Any]], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> ReconcileOutcome:
    """Reconcile every requested variant for one target.

    Groups `BasicPitchSpec` requests by `detection_hash` and runs
    `transcriber.detect_raw` at most once per uncached group (see the module
    docstring); every variant in a group then derives its own cleanup result
    independently, reusing one source spectrogram per `(n_fft, hop_length)`
    configuration across every variant in this call that needs ghost
    confirmation. `DrumScriptSpec` requests run `transcriber.transcribe`
    directly, one call each, with no grouping or raw cache.

    Every group/variant result or error is persisted immediately via
    `persist_variant`/`persist_detection_cache_entry` (when given) as soon as
    it's known, so a later group's or variant's failure never rolls back an
    earlier success -- callers wire these to the sidecar's atomic per-target
    commit, mirroring `analysis.py`'s existing per-target durability.
    """
    validate_target(target)
    say = emit or (lambda _message: None)
    existing_variants = existing_variants or {}
    detection_cache_state = dict(detection_cache or {})
    variants: dict[str, dict[str, Any]] = {}
    invocations = 0

    def commit_variant(variant_id: str, record: dict[str, Any]) -> None:
        variants[variant_id] = record
        if persist_variant is not None:
            persist_variant(variant_id, record)

    def commit_detection_cache(detection_hash_value: str) -> None:
        if persist_detection_cache_entry is not None:
            persist_detection_cache_entry(detection_hash_value, detection_cache_state[detection_hash_value])

    if source is None or input_hash is None:
        say(f"transcription skipped for {target}: no {target} stem available")
        for request in requests:
            commit_variant(request.variant_id, _missing_source_variant(request, target))
        return ReconcileOutcome(variants=variants, detection_cache=detection_cache_state, backend_invocations=0)

    groups: dict[str, list[VariantRequest]] = {}
    for request in requests:
        if isinstance(request.spec, DrumScriptSpec):
            record, count = _reconcile_drumscript_variant(
                request,
                target=target,
                transcriber=transcriber,
                source=source,
                input_hash=input_hash,
                namespace_dir=namespace_dir,
                existing=existing_variants.get(request.variant_id),
                force=force,
                emit=say,
            )
            invocations += count
            commit_variant(request.variant_id, record)
            continue
        if not isinstance(request.spec, BasicPitchSpec):
            raise TranscriptionError(f"unsupported spec type for variant {request.variant_id!r}: {type(request.spec)!r}")
        groups.setdefault(detection_hash(target, input_hash, request.spec), []).append(request)

    spectral_cache: dict[tuple[int, int], Any] = {}

    for detection_hash_value, group_requests in groups.items():
        raw_notes_hash, raw_notes_path, raw_error, invoked = _obtain_raw_group(
            detection_hash_value=detection_hash_value,
            representative_spec=group_requests[0].spec,  # type: ignore[arg-type]
            target=target,
            transcriber=transcriber,
            source=source,
            input_hash=input_hash,
            namespace_dir=namespace_dir,
            detection_cache=detection_cache_state,
            force=force,
            emit=say,
        )
        invocations += invoked
        if invoked:
            commit_detection_cache(detection_hash_value)

        if raw_error is not None:
            for request in group_requests:
                commit_variant(request.variant_id, _error_variant(request, target, input_hash, detection_hash_value, raw_error))
            continue

        assert raw_notes_hash is not None and raw_notes_path is not None
        raw_notes_cache: list[ParsedNote] | None = None

        for request in group_requests:
            spec = request.spec
            assert isinstance(spec, BasicPitchSpec)
            variant_cleanup_hash = cleanup_hash(raw_notes_hash, input_hash, spec)
            existing = existing_variants.get(request.variant_id)
            if not force and _existing_basic_pitch_current(
                existing, input_hash=input_hash, detection_hash_value=detection_hash_value, cleanup_hash_value=variant_cleanup_hash
            ):
                say(f"transcription — {target}/{request.label}: unchanged, using cached result")
                commit_variant(request.variant_id, existing)
                continue
            try:
                if raw_notes_cache is None:
                    raw_notes_cache = parse_notes_csv(raw_notes_path)
                result = derive_variant_artifacts(
                    raw_notes_cache,
                    spec,
                    midi_path=namespace_dir / variant_midi_name(target, request.variant_id),
                    notes_path=namespace_dir / variant_notes_name(target, request.variant_id),
                    source=source,
                    spectral_cache=spectral_cache,
                )
            except TranscriptionError as exc:
                say(f"transcription error for {target}/{request.label}: {exc}")
                commit_variant(
                    request.variant_id,
                    _error_variant(request, target, input_hash, detection_hash_value, str(exc), cleanup_hash_value=variant_cleanup_hash),
                )
                continue
            say(f"transcribed {target}/{request.label}: {result.note_count} notes")
            commit_variant(
                request.variant_id,
                _transcribed_basic_pitch_variant(
                    request, target, input_hash, detection_hash_value, variant_cleanup_hash, raw_notes_hash, result
                ),
            )

    return ReconcileOutcome(variants=variants, detection_cache=detection_cache_state, backend_invocations=invocations)


def garbage_collect_raw_cache(
    *, namespace_dir: Path, detection_cache: dict[str, dict[str, Any]], targets: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Delete raw-detection cache entries no live variant across any target
    still references, returning `(kept_cache, removed_hashes)`.

    Reference-counted and exact: the referenced set is every live
    `variants[...].detection_hash` across every target's `variants` index
    (never `discarded_variants`, which by design keeps no cache reference,
    see docs/transcription-variants-plan.md's discard semantics). An
    unreferenced entry's own recorded `raw_midi_file`/`raw_notes_file` paths
    are the only files ever deleted -- resolved and checked to stay inside
    this cache's own namespace before unlinking, and never through a
    directory glob -- so a concurrently retained variant's artifacts can
    never be swept up by a stale or malformed cache entry.
    """
    referenced: set[str] = set()
    for target_record in targets.values():
        if not isinstance(target_record, dict):
            continue
        variant_map = target_record.get("variants")
        if not isinstance(variant_map, dict):
            continue
        for variant in variant_map.values():
            if isinstance(variant, dict) and isinstance(variant.get("detection_hash"), str):
                referenced.add(variant["detection_hash"])

    cache_root = detection_cache_root(namespace_dir).resolve()
    kept: dict[str, dict[str, Any]] = {}
    removed: list[str] = []
    for detection_hash_value, entry in detection_cache.items():
        if detection_hash_value in referenced:
            kept[detection_hash_value] = entry
            continue
        for key in ("raw_midi_file", "raw_notes_file"):
            relative = entry.get(key) if isinstance(entry, dict) else None
            if not isinstance(relative, str):
                continue
            candidate = (namespace_dir / relative).resolve()
            try:
                candidate.relative_to(cache_root)
            except ValueError:
                continue  # never delete anything outside this cache's own namespace
            if candidate.is_file():
                candidate.unlink()
        group_dir = cache_root / detection_hash_value
        if group_dir.is_dir():
            try:
                next(group_dir.iterdir())
            except StopIteration:
                group_dir.rmdir()
        removed.append(detection_hash_value)
    return kept, removed
