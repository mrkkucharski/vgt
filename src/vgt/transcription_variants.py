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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
import hashlib
import json
import re
import shutil

from .transcribe import (
    BasicPitchSpec,
    AdtofSpec,
    DrumScriptSpec,
    EssentiaSpec,
    Mt3Spec,
    NoteSpec,
    PyinSpec,
    ParsedNote,
    Transcriber,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSpec,
    VALID_TARGETS,
    derive_variant_artifacts,
    events_artifact_name,
    midi_artifact_name,
    notes_artifact_name,
    parse_notes_csv,
    raw_notes_content_hash,
    spec_hash,
    validate_target,
)
from .audio_frontend import canonical_recipe, frontend_hash, frontend_relative_path, render

# Basic Pitch and pYIN share this raw-cache directory name (their disjoint
# `detection_hash` identities already keep the two from ever colliding inside
# it -- see `detection_identity`); MT3 gets its own (issue #287), and Essentia
# gets its own too, so a human browsing the cache can tell a TensorFlow/JAX-
# produced raw MIDI, an in-process classical-DSP one, and every other
# in-process one apart at a glance.
CACHE_BACKEND_DIR = "basic-pitch"
MT3_CACHE_BACKEND_DIR = "mt3"
ESSENTIA_CACHE_BACKEND_DIR = "essentia"


def _cache_backend_dir_for_spec(spec: NoteSpec) -> str:
    if isinstance(spec, Mt3Spec):
        return MT3_CACHE_BACKEND_DIR
    if isinstance(spec, EssentiaSpec):
        return ESSENTIA_CACHE_BACKEND_DIR
    return CACHE_BACKEND_DIR


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
    audio_frontend: dict[str, Any] = field(default_factory=lambda: {"stages": []})


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


def detection_identity(target: str, input_hash: str, spec: NoteSpec) -> dict[str, Any]:
    """Everything that determines one detection run (see the plan's "Layer 1:
    raw detection" section). Two specs with equal identity here can share one
    raw inference regardless of which profile produced them.

    Every note-producing backend resolves through here. Each backend branch's
    identity shape is disjoint from the others' apart from `common`'s keys --
    a pyin, Basic Pitch, Essentia, or MT3 variant can therefore never collide
    with, or be served from, another backend's cache entry. Basic
    Pitch/pYIN/Essentia's detector fields (`minimum_note_length_ms`/frequency
    window) live in their own branches rather than `common`: MT3 is a fixed
    pretrained model with no such settings to hash.
    """
    common = {
        "target": target,
        "input_hash": input_hash,
        "backend": spec.backend,
        # The backend embeds this in the raw MIDI it emits, so it is part of
        # what determines the raw artifact even though it never changes note
        # timings themselves (see the plan's "MIDI tempo metadata" note).
        "midi_tempo": spec.midi_tempo,
        # The raw MIDI is later materialized into a project-relative MIDI
        # artifact.  A changed REAPER marker timeline must therefore get a
        # fresh detection-cache identity as well as a fresh derived variant.
        "tempo_map": spec.tempo_map.to_dict() if spec.tempo_map is not None else None,
    }
    if isinstance(spec, PyinSpec):
        return {
            **common,
            "minimum_note_length_ms": spec.minimum_note_length_ms,
            "minimum_frequency_hz": spec.minimum_frequency_hz,
            "maximum_frequency_hz": spec.maximum_frequency_hz,
            "algorithm_version": spec.algorithm_version,
            "sample_rate_hz": spec.sample_rate_hz,
            "frame_length": spec.frame_length,
            "hop_length": spec.hop_length,
            "median_filter_frames": spec.median_filter_frames,
            # Re-articulation splitting happens inside `segment_notes`, so it
            # shapes the *raw* note list, not a cleanup derivation -- these
            # belong here and not only in `settings_hash`. Omitting them let a
            # profile retune the splitter and be served stale raw notes, which
            # is precisely the "tuning a silent no-op" failure `PyinSpec`'s
            # docstring warns about.
            "rearticulation_span_frames": spec.rearticulation_span_frames,
            "rearticulation_rise_db": spec.rearticulation_rise_db,
            "rearticulation_minimum_spacing_beats": spec.rearticulation_minimum_spacing_beats,
        }
    if isinstance(spec, EssentiaSpec):
        return {
            **common,
            "algorithm": spec.algorithm,
            "algorithm_version": spec.algorithm_version,
            "sample_rate_hz": spec.sample_rate_hz,
            "minimum_note_length_ms": spec.minimum_note_length_ms,
            "minimum_frequency_hz": spec.minimum_frequency_hz,
            "maximum_frequency_hz": spec.maximum_frequency_hz,
            # Bridging/dropping happens inside `segment_multipitch`, so it
            # shapes the *raw* note list, not a cleanup derivation -- same
            # reasoning as pYIN's re-articulation fields just above: omitting
            # it would let a profile retune the merge gap and be served stale
            # raw notes.
            "merge_gap_ms": spec.merge_gap_ms,
        }
    if isinstance(spec, Mt3Spec):
        return {
            **common,
            "repository": spec.repository,
            "tag": spec.tag,
            "commit": spec.commit,
            "runtime_version": spec.runtime_version,
            "lock_sha256": spec.lock_sha256,
            "model_id": spec.model_id,
            "checkpoint_fingerprint": spec.checkpoint_fingerprint,
            # Both directly determine what `mt3-transcribe` decodes (measured
            # for real: 512 vs. 256 frames moved a real song's note count
            # 117 -> 279 on the same checkpoint -- see `vgt.mt3_provision`'s
            # `MT3_INPUT_LENGTH_FRAMES` comment), so they belong in the raw
            # detection identity, not only in `settings_hash`. Omitting them
            # would let a re-pinned window/lookahead be served a stale cached
            # raw MIDI decoded under the old geometry -- exactly the "tuning
            # a silent no-op" failure class `PyinSpec`'s docstring warns about.
            "input_length_frames": spec.input_length_frames,
            "lookahead_frames": spec.lookahead_frames,
            "track_selection_version": spec.track_selection_version,
            "note_normalization_version": spec.note_normalization_version,
        }
    return {
        **common,
        "minimum_note_length_ms": spec.minimum_note_length_ms,
        "minimum_frequency_hz": spec.minimum_frequency_hz,
        "maximum_frequency_hz": spec.maximum_frequency_hz,
        "package_pin": spec.package_pin,
        "serialization": spec.serialization,
        "onset_threshold": spec.onset_threshold,
        "frame_threshold": spec.frame_threshold,
        "multiple_pitch_bends": spec.multiple_pitch_bends,
        "melodia_trick": spec.melodia_trick,
    }


def detection_hash(target: str, input_hash: str, spec: NoteSpec) -> str:
    return _hash(detection_identity(target, input_hash, spec))


def cleanup_identity(raw_notes_hash: str, source_audio_hash: str, spec: NoteSpec) -> dict[str, Any]:
    """Everything that determines one derived cleanup variant beyond its raw
    detection (see the plan's "Layer 2: derived variant" section): the raw
    note-event content, the source audio (ghost confirmation reads the
    waveform), and the ordered, parameterized cleanup pipeline."""
    return {
        "raw_notes_hash": raw_notes_hash,
        "source_audio_hash": source_audio_hash,
        "cleanup": [{"name": stage.name, "params": stage.params} for stage in spec.cleanup],
    }


def cleanup_hash(raw_notes_hash: str, source_audio_hash: str, spec: NoteSpec) -> str:
    return _hash(cleanup_identity(raw_notes_hash, source_audio_hash, spec))


def _cache_root(namespace_dir: Path) -> Path:
    """The parent of every backend's raw-cache subdirectory."""
    return namespace_dir / "transcription" / "cache"


def detection_cache_root(namespace_dir: Path, *, backend_dir: str = CACHE_BACKEND_DIR) -> Path:
    return _cache_root(namespace_dir) / backend_dir


def _raw_midi_name(detection_hash_value: str, *, backend_dir: str = CACHE_BACKEND_DIR) -> str:
    return f"transcription/cache/{backend_dir}/{detection_hash_value}/raw.mid"


def _raw_notes_name(detection_hash_value: str, *, backend_dir: str = CACHE_BACKEND_DIR) -> str:
    return f"transcription/cache/{backend_dir}/{detection_hash_value}/raw.csv"


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


def variant_settings_hash(request: VariantRequest) -> str:
    return _hash({"spec": request.spec.to_dict(), "audio_frontend": canonical_recipe(request.audio_frontend)})


def _base_variant_fields(request: VariantRequest, target: str) -> dict[str, Any]:
    spec = request.spec
    return {
        "label": request.label,
        "requested_profile": request.requested_profile,
        "profile_definition_hash": request.profile_definition_hash,
        "effective_profile": request.effective_profile,
        "backend": spec.backend,
        # pYIN and Essentia run in-process, so neither has a pinned package or
        # a model serialization; their runtime identity is `algorithm_version`,
        # already inside `settings_hash` below (see `vgt.transcribe.PyinSpec`/
        # `EssentiaSpec`). MT3 is a git-cloned project pinned by commit, not a
        # pip package.
        "package_pin": None if isinstance(spec, (PyinSpec, EssentiaSpec, Mt3Spec)) else spec.package_pin,
        "serialization": spec.serialization if isinstance(spec, BasicPitchSpec) else None,
        "source_role": target,
        "settings_hash": variant_settings_hash(request),
        "resolved_settings": request.resolved_settings,
        "midi_tempo": spec.midi_tempo,
        "audio_frontend": canonical_recipe(request.audio_frontend),
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
    settings_hash = variant_settings_hash(request)
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
    settings_hash = variant_settings_hash(request)
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
    representative_spec: NoteSpec,
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
    one detection group, running the backend at most once. On success,
    `detection_cache[detection_hash_value]` is updated in place so the caller
    can persist it immediately (see `reconcile_variants`)."""
    backend_dir = _cache_backend_dir_for_spec(representative_spec)
    raw_midi_path = namespace_dir / _raw_midi_name(detection_hash_value, backend_dir=backend_dir)
    raw_notes_path = namespace_dir / _raw_notes_name(detection_hash_value, backend_dir=backend_dir)
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
        "raw_midi_file": _raw_midi_name(detection_hash_value, backend_dir=backend_dir),
        "raw_notes_file": _raw_notes_name(detection_hash_value, backend_dir=backend_dir),
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

    # A frontend is a cached analysis input, not a replacement stem.  Its
    # bytes, rather than the raw stem's bytes, key inference; records retain
    # both identities so provenance remains auditable.
    contexts: dict[str, tuple[Path, str, str | None]] = {}
    for request in requests:
        recipe = canonical_recipe(request.audio_frontend)
        if recipe["stages"]:
            key = frontend_hash(input_hash, recipe)
            relative = frontend_relative_path(key)
            rendered = namespace_dir / relative
            if not rendered.is_file():
                render(source, rendered, recipe)
            processed_hash = hashlib.sha256(rendered.read_bytes()).hexdigest()
            contexts[request.variant_id] = (rendered, processed_hash, relative)
        else:
            contexts[request.variant_id] = (source, input_hash, None)

    def with_frontend(record: dict[str, Any], request: VariantRequest) -> dict[str, Any]:
        _processed, processed_hash, relative = contexts[request.variant_id]
        record["source_input_hash"] = input_hash
        record["analysis_input_hash"] = processed_hash
        record["analysis_audio_file"] = relative
        record["input_hash"] = processed_hash
        return record

    groups: dict[tuple[str, str, Path], list[VariantRequest]] = {}
    for request in requests:
        processed_source, processed_hash, _relative = contexts[request.variant_id]
        if isinstance(request.spec, (DrumScriptSpec, AdtofSpec)):
            record, count = _reconcile_drumscript_variant(
                request,
                target=target,
                transcriber=transcriber,
                source=processed_source,
                input_hash=processed_hash,
                namespace_dir=namespace_dir,
                existing=existing_variants.get(request.variant_id),
                force=force,
                emit=say,
            )
            invocations += count
            commit_variant(request.variant_id, with_frontend(record, request))
            continue
        if not isinstance(request.spec, (BasicPitchSpec, PyinSpec, EssentiaSpec, Mt3Spec)):
            raise TranscriptionError(f"unsupported spec type for variant {request.variant_id!r}: {type(request.spec)!r}")
        groups.setdefault((detection_hash(target, processed_hash, request.spec), processed_hash, processed_source), []).append(request)

    spectral_cache: dict[tuple[int, int], Any] = {}

    for (detection_hash_value, processed_hash, processed_source), group_requests in groups.items():
        raw_notes_hash, raw_notes_path, raw_error, invoked = _obtain_raw_group(
            detection_hash_value=detection_hash_value,
            representative_spec=group_requests[0].spec,  # type: ignore[arg-type]
            target=target,
            transcriber=transcriber,
            source=processed_source,
            input_hash=processed_hash,
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
                commit_variant(request.variant_id, with_frontend(_error_variant(request, target, processed_hash, detection_hash_value, raw_error), request))
            continue

        assert raw_notes_hash is not None and raw_notes_path is not None
        raw_notes_cache: list[ParsedNote] | None = None

        for request in group_requests:
            spec = request.spec
            assert isinstance(spec, (BasicPitchSpec, PyinSpec, EssentiaSpec, Mt3Spec))
            variant_cleanup_hash = cleanup_hash(raw_notes_hash, processed_hash, spec)
            existing = existing_variants.get(request.variant_id)
            if not force and _existing_basic_pitch_current(
                existing, input_hash=processed_hash, detection_hash_value=detection_hash_value, cleanup_hash_value=variant_cleanup_hash
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
                    source=processed_source,
                    spectral_cache=spectral_cache,
                )
            except TranscriptionError as exc:
                say(f"transcription error for {target}/{request.label}: {exc}")
                commit_variant(
                    request.variant_id,
                    with_frontend(_error_variant(request, target, processed_hash, detection_hash_value, str(exc), cleanup_hash_value=variant_cleanup_hash), request),
                )
                continue
            say(f"transcribed {target}/{request.label}: {result.note_count} notes")
            commit_variant(
                request.variant_id,
                with_frontend(_transcribed_basic_pitch_variant(
                    request, target, processed_hash, detection_hash_value, variant_cleanup_hash, raw_notes_hash, result
                ), request),
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
    this cache's own namespace (any backend's subdirectory under
    `transcription/cache/`, e.g. `basic-pitch/` or `mt3/`) before unlinking,
    and never through a directory glob -- so a concurrently retained
    variant's artifacts can never be swept up by a stale or malformed cache
    entry.
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

    cache_root = _cache_root(namespace_dir).resolve()
    kept: dict[str, dict[str, Any]] = {}
    removed: list[str] = []
    for detection_hash_value, entry in detection_cache.items():
        if detection_hash_value in referenced:
            kept[detection_hash_value] = entry
            continue
        group_dirs: set[Path] = set()
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
            group_dirs.add(candidate.parent)
        for group_dir in group_dirs:
            if group_dir.is_dir():
                try:
                    next(group_dir.iterdir())
                except StopIteration:
                    group_dir.rmdir()
        removed.append(detection_hash_value)
    return kept, removed


# The three artifact kinds a variant can record, each paired with the pre-v13
# flat name it may still carry and the per-target name it belongs at today
# (issue #223). `sidecar.migrate_transcription_target` deliberately keeps a
# migrated variant's flat path verbatim -- it is an in-memory transform and
# must not touch the filesystem -- so relocating those files is this module's
# job, on the same footing as `garbage_collect_raw_cache`.
_ARTIFACT_KINDS: tuple[tuple[str, Callable[[str], str], Callable[[str, str], str]], ...] = (
    ("midi_file", midi_artifact_name, variant_midi_name),
    ("notes_file", notes_artifact_name, variant_notes_name),
    ("events_file", events_artifact_name, variant_events_name),
)

# Mirrors `vgt_initialize.lua`'s `valid_midi_artifact` guard: a variant id
# becomes a path component, so never build one from an id the ReaScript would
# refuse to reconstruct.
_SAFE_VARIANT_ID = re.compile(r"^[A-Za-z0-9_-]+$")

_LEGACY_NAMES: dict[str, Callable[[str], str]] = {key: legacy_name for key, legacy_name, _variant_name in _ARTIFACT_KINDS}

_WORK_DIR_PREFIX = "_work-"


@dataclass
class LayoutReconciliation:
    """What one `reconcile_artifact_layout` pass did.

    `moved` and `missing` are artifact names whose record was rewritten (the
    file relocated, or recorded at its current name while absent), so a caller
    seeing either must persist the records. `removed` names files and scratch
    directories deleted, which needs no sidecar change.
    """

    moved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def records_changed(self) -> bool:
        """Whether any variant record's artifact path was rewritten."""
        return bool(self.moved or self.missing)

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.missing or self.removed)


def _live_artifact_references(targets: dict[str, Any]) -> set[str]:
    """Every artifact path any live record still points at.

    Deliberately includes a legacy record's own top-level flat fields as well
    as its variants': `sidecar.migrate_transcription_target` is additive, so
    an unmigrated record names the same file twice and neither name may be
    treated as unreferenced.
    """
    referenced: set[str] = set()
    for record in targets.values():
        if not isinstance(record, dict):
            continue
        holders: list[dict[str, Any]] = [record]
        variants = record.get("variants")
        if isinstance(variants, dict):
            holders.extend(variant for variant in variants.values() if isinstance(variant, dict))
        for holder in holders:
            for key, _legacy_name, _variant_name in _ARTIFACT_KINDS:
                recorded = holder.get(key)
                if isinstance(recorded, str):
                    referenced.add(recorded)
    return referenced


def _relocate_variant(
    variant: dict[str, Any],
    *,
    target: str,
    variant_id: str,
    namespace_dir: Path,
    outcome: LayoutReconciliation,
    say: Callable[[str], None],
) -> dict[str, str]:
    """Point one variant record at `transcription/<target>/<variant-id>.*`,
    moving the file when it is still sitting at the flat path, and return the
    `{record key: new name}` pairs it rewrote.

    Idempotent by construction, because it decides from the filesystem rather
    than from the record: a record whose file another pass already moved is
    simply rewritten in place. That is what lets a caller run this against its
    in-memory analysis and again against the authoritative on-disk copy inside
    an atomic sidecar update.
    """
    rewritten: dict[str, str] = {}
    for key, legacy_name, variant_name in _ARTIFACT_KINDS:
        recorded = variant.get(key)
        if recorded != legacy_name(target):
            continue
        destination_name = variant_name(target, variant_id)
        source_path = namespace_dir / recorded
        destination_path = namespace_dir / destination_name
        if source_path.is_file():
            if destination_path.is_file():
                # A previous pass already produced the artifact at its current
                # name (this record simply had not been rewritten yet); the
                # flat copy is the stale one.
                source_path.unlink()
            else:
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.replace(destination_path)
            outcome.moved.append(destination_name)
            say(f"transcription layout — moved {recorded} to {destination_name}")
        elif destination_path.is_file():
            outcome.moved.append(destination_name)  # already relocated; record catches up
        else:
            outcome.missing.append(destination_name)
            say(f"transcription layout — {target}/{variant_id}: recorded {recorded} is missing; recorded as {destination_name}")
        variant[key] = destination_name
        rewritten[key] = destination_name
    return rewritten


def _sweep_flat_artifacts(namespace_dir: Path, targets: dict[str, Any], outcome: LayoutReconciliation, say: Callable[[str], None]) -> None:
    """Delete a flat `transcription/<target>.<ext>` no live record references.

    Candidates are derived from `VALID_TARGETS` and the fixed artifact kinds,
    never from a directory glob, and each is checked against the live
    reference set first -- the same "only delete what the sidecar says is
    dead" discipline `garbage_collect_raw_cache` and
    `transcription_lifecycle._delete_variant_artifacts` follow.
    """
    referenced = _live_artifact_references(targets)
    for target in VALID_TARGETS:
        for _key, legacy_name, _variant_name in _ARTIFACT_KINDS:
            name = legacy_name(target)
            if name in referenced:
                continue
            path = namespace_dir / name
            if path.is_file():
                path.unlink()
                outcome.removed.append(name)
                say(f"transcription layout — removed orphaned {name}")


def _sweep_empty_work_dirs(namespace_dir: Path, outcome: LayoutReconciliation) -> None:
    """Remove `transcription/_work-*` scratch directories left empty by an
    earlier run. Each reconcile already deletes its own work directory in a
    `finally`, but the shared `_work-detection` parent it creates outlives
    them; only genuinely empty directories are removed, so an interrupted
    run's in-flight scratch is never swept out from under it."""
    root = namespace_dir / "transcription"
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith(_WORK_DIR_PREFIX):
            continue
        for grandchild in sorted(child.iterdir()):
            if grandchild.is_dir():
                try:
                    grandchild.rmdir()
                except OSError:
                    pass  # still holds an interrupted run's scratch
        try:
            child.rmdir()
        except OSError:
            continue
        outcome.removed.append(f"transcription/{child.name}/")


def reconcile_artifact_layout(
    *, namespace_dir: Path, targets: dict[str, Any], emit: Callable[[str], None] | None = None
) -> LayoutReconciliation:
    """Bring one project's transcription artifacts onto the current layout:
    every artifact under `transcription/<target>/`, with nothing but per-target
    directories and `cache/` directly inside `transcription/` (issue #223).

    Three steps, in order: relocate any artifact still recorded at a pre-v13
    flat path and rewrite the record; delete flat leftovers no live record
    references (the orphans a variant's first re-reconcile leaves behind when
    it writes into the per-target directory); and remove empty `_work-*`
    scratch directories.

    `targets` is mutated in place and the caller persists it -- the same
    contract `analysis._refresh_target` has with its own target record. Safe to
    run on every analyze: a project already on the current layout does no I/O
    beyond a handful of `is_file` checks.
    """
    say = emit or (lambda _message: None)
    outcome = LayoutReconciliation()
    for target, record in targets.items():
        if target not in VALID_TARGETS or not isinstance(record, dict):
            continue  # a malformed or unknown target must not become a path
        variants = record.get("variants")
        if not isinstance(variants, dict):
            continue
        for variant_id, variant in variants.items():
            if not isinstance(variant, dict) or not _SAFE_VARIANT_ID.match(variant_id or ""):
                continue
            rewritten = _relocate_variant(
                variant, target=target, variant_id=variant_id, namespace_dir=namespace_dir, outcome=outcome, say=say
            )
            # A legacy record names the same artifact once more at its own top
            # level (see `_live_artifact_references`), and that copy is the one
            # this variant was derived from. Keep the compatibility view in
            # step, so nothing holds a live reference to a flat path the sweep
            # below is about to consider dead.
            for key, name in rewritten.items():
                if record.get(key) == _LEGACY_NAMES[key](target):
                    record[key] = name
    _sweep_flat_artifacts(namespace_dir, targets, outcome, say)
    _sweep_empty_work_dirs(namespace_dir, outcome)
    return outcome
