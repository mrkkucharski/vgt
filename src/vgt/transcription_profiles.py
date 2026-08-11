"""Project-local transcription profile definitions (`<project>.vgt-profiles.toml`).

See docs/transcription-variants-plan.md's "Declarative profile definitions"
section for the product-level contract this module implements: a project
file may declare named profiles that `extends` a built-in
`vgt.transcribe._INSTRUMENT_PROFILES` entry (or another profile in the same
file), overriding only the detection/cleanup fields it wants to change.
Resolution walks that inheritance chain down to a built-in base and produces
an immutable `ResolvedProfile` settings snapshot -- the same shape a later
issue's variant-creation code needs, without this issue wiring it into
`default_spec_for_target` itself (that integration, and any CLI surface, is
explicitly out of this issue's scope; see the plan's implementation
sequence).

Every value capable of changing transcribed output must be part of the
resolved snapshot and therefore visible to `resolved_detection_hash`/
`resolved_cleanup_hash` -- the same "settings identity" contract
`vgt.transcribe.profile_detection_hash`/`profile_cleanup_hash` give built-in
profiles (see that module for why detail/clean share a detection identity).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
import hashlib
import json
import tomllib

from .transcribe import (
    BASIC_PITCH_PACKAGE_PIN,
    BASIC_PITCH_SERIALIZATION,
    CANONICAL_CLEANUP_STAGE_ORDER,
    GUITAR_FRAGMENT_MERGE_GAP_S,
    GUITAR_GHOST_ONSET_TOLERANCE_S,
    GUITAR_GHOST_OVERLAP_FRACTION,
    GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
    GUITAR_GHOST_SPECTRAL_HOP_LENGTH,
    GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
    GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
    GUITAR_GHOST_SPECTRAL_N_FFT,
    GUITAR_GHOST_VELOCITY_SLACK,
    GUITAR_HARMONIC_GHOST_INTERVALS,
    GUITAR_ISOLATED_MAX_DURATION_S,
    GUITAR_ISOLATED_NEIGHBOUR_WINDOW_S,
    GUITAR_MAX_SIMULTANEOUS_VOICES,
    GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
    GUITAR_SUSTAIN_CLAMP_BARS,
    BasicPitchSpec,
    CleanupStage,
    InstrumentProfile,
    Mt3Spec,
    PyinSpec,
    PYIN_ALGORITHM_VERSION,
    TempoMapReference,
    TranscriptionError,
    VALID_TARGETS,
    _bar_duration_seconds,  # noqa: SLF001 -- reuses the same bars->seconds conversion `default_spec_for_target` applies
    _INSTRUMENT_PROFILES,  # noqa: SLF001 -- this module is transcribe.py's one intended reader of the builtin registry
    canonical_drum_profile_name,
    valid_profile_names_for_target as builtin_profile_names_for_target,
    drum_transcription_profile,
)
from .audio_frontend import AudioFrontendError, canonical_recipe

TOML_SCHEMA_VERSION = 2

_TOP_LEVEL_KEYS = {"schema_version", "profiles"}
_PROFILE_KEYS = {"target", "extends", "description", "detection", "cleanup", "audio_frontend"}

# Backend-specific fields are deliberately explicit.  A pYIN profile must not
# accept a Basic Pitch knob which its backend never reads: doing so would make
# an apparent retune a silent no-op and poison its cache identity.
_BASIC_PITCH_DETECTION_FIELDS: tuple[str, ...] = (
    "onset_threshold",
    "frame_threshold",
    "minimum_note_length_ms",
    "minimum_frequency_hz",
    "maximum_frequency_hz",
    "melodia_trick",
    "multiple_pitch_bends",
)

_PYIN_DETECTION_FIELDS: tuple[str, ...] = (
    "minimum_note_length_ms",
    "minimum_frequency_hz",
    "maximum_frequency_hz",
    "sample_rate_hz",
    "frame_length",
    "hop_length",
    "median_filter_frames",
    "rearticulation_span_frames",
    "rearticulation_rise_db",
    "rearticulation_minimum_spacing_beats",
)


class ProfileDefinitionError(TranscriptionError):
    """A project profile definition or its resolution is invalid."""


def profiles_path(project_path: str | Path) -> Path:
    return Path(project_path).with_suffix(".vgt-profiles.toml")


def _fail(message: str) -> None:
    raise ProfileDefinitionError(message)


def _validate_bool(value: Any, ctx: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{ctx} must be a boolean, got {value!r}")
    return value


def _validate_unit_interval(value: Any, ctx: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{ctx} must be a number, got {value!r}")
    if not (0.0 <= value <= 1.0):
        _fail(f"{ctx} must be between 0.0 and 1.0, got {value!r}")
    return float(value)


def _validate_positive_number(value: Any, ctx: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{ctx} must be a number, got {value!r}")
    if not value > 0:
        _fail(f"{ctx} must be positive, got {value!r}")
    return float(value)


def _validate_non_negative_number(value: Any, ctx: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{ctx} must be a number, got {value!r}")
    if value < 0:
        _fail(f"{ctx} must not be negative, got {value!r}")
    return float(value)


def _validate_positive_int(value: Any, ctx: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{ctx} must be an integer, got {value!r}")
    if value < 1:
        _fail(f"{ctx} must be at least 1, got {value!r}")
    return value


def _validate_interval_list(value: Any, ctx: str) -> list[int]:
    if not isinstance(value, list) or not value:
        _fail(f"{ctx} must be a non-empty list of positive integers, got {value!r}")
    return [_validate_positive_int(item, f"{ctx} entry") for item in value]


_DETECTION_VALIDATORS: dict[str, Any] = {
    "onset_threshold": _validate_unit_interval,
    "frame_threshold": _validate_unit_interval,
    "minimum_note_length_ms": _validate_positive_number,
    "minimum_frequency_hz": _validate_positive_number,
    "maximum_frequency_hz": _validate_positive_number,
    "melodia_trick": _validate_bool,
    "multiple_pitch_bends": _validate_bool,
    "sample_rate_hz": _validate_positive_int,
    "frame_length": _validate_positive_int,
    "hop_length": _validate_positive_int,
    "median_filter_frames": _validate_positive_int,
    "rearticulation_span_frames": _validate_positive_int,
    # 0 disables re-articulation splitting, so this is the one detection
    # number a profile may legitimately set to zero.
    "rearticulation_rise_db": _validate_non_negative_number,
    "rearticulation_minimum_spacing_beats": _validate_non_negative_number,
}

# Per-stage accepted parameter keys and their validators. Only the five
# canonical, orderable stages are configurable from a project profile (see
# `CANONICAL_CLEANUP_STAGE_ORDER`'s docstring): `force_monophony` is a
# builtin-only, bass-specific single-stage pipeline with no tuning surface.
_STAGE_VALIDATORS: dict[str, dict[str, Any]] = {
    "merge_fragments": {"max_gap_s": _validate_positive_number, "merge_touching": _validate_bool},
    "drop_isolated_notes": {
        "max_duration_s": _validate_positive_number,
        "neighbour_window_s": _validate_positive_number,
    },
    "clamp_sustain": {"max_bars": _validate_positive_number},
    "drop_harmonic_ghosts": {
        "intervals": _validate_interval_list,
        "onset_tolerance_s": _validate_positive_number,
        "overlap_fraction": _validate_unit_interval,
        "velocity_slack": _validate_non_negative_number,
        "spectral_n_fft": _validate_positive_int,
        "spectral_hop_length": _validate_positive_int,
        "spectral_max_harmonic_order": _validate_positive_int,
        "spectral_freq_tolerance_semitones": _validate_positive_number,
        "spectral_independent_energy_ratio": _validate_positive_number,
    },
    "cap_simultaneous_voices": {
        "max_voices": _validate_positive_int,
        "min_duration_after_cap_s": _validate_positive_number,
    },
}

# Defaults used when a stage is enabled by an override but the profile chain
# never previously configured it (e.g. a project profile turning on
# `drop_isolated_notes` under a base that doesn't use it). Mirrors the
# built-in acoustic profiles' own tuning (see transcribe.py).
_STAGE_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "merge_fragments": {"max_gap_s": GUITAR_FRAGMENT_MERGE_GAP_S},
    "drop_isolated_notes": {
        "max_duration_s": GUITAR_ISOLATED_MAX_DURATION_S,
        "neighbour_window_s": GUITAR_ISOLATED_NEIGHBOUR_WINDOW_S,
    },
    "clamp_sustain": {"max_bars": GUITAR_SUSTAIN_CLAMP_BARS},
    "drop_harmonic_ghosts": {
        "intervals": list(GUITAR_HARMONIC_GHOST_INTERVALS),
        "onset_tolerance_s": GUITAR_GHOST_ONSET_TOLERANCE_S,
        "overlap_fraction": GUITAR_GHOST_OVERLAP_FRACTION,
        "velocity_slack": GUITAR_GHOST_VELOCITY_SLACK,
        "spectral_n_fft": GUITAR_GHOST_SPECTRAL_N_FFT,
        "spectral_hop_length": GUITAR_GHOST_SPECTRAL_HOP_LENGTH,
        "spectral_max_harmonic_order": GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
        "spectral_freq_tolerance_semitones": GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
        "spectral_independent_energy_ratio": GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
    },
    "cap_simultaneous_voices": {
        "max_voices": GUITAR_MAX_SIMULTANEOUS_VOICES,
        "min_duration_after_cap_s": GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
    },
}


@dataclass(frozen=True)
class RawProfileDefinition:
    """One `[profiles.<name>]` table, parsed and key/type/bound-validated but
    not yet resolved against its `extends` chain."""

    name: str
    target: str
    extends: str
    description: str | None
    detection: dict[str, Any]
    cleanup: dict[str, dict[str, Any]]  # stage name -> {"enabled": bool, **params}
    audio_frontend: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ResolvedProfile:
    """One profile's fully resolved, immutable settings snapshot.

    `target` is `None` for a builtin profile resolved without a specific
    target context (e.g. `default`, which is valid for every target);
    project profiles always declare an explicit `target`.
    """

    name: str
    target: str | None
    backend: str
    detection: dict[str, Any]
    cleanup: tuple[CleanupStage, ...]
    audio_frontend: dict[str, Any]
    is_builtin: bool
    profile_definition_hash: str | None  # project profile's own raw-definition hash; None for a pure builtin


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True)


def _hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def parse_profiles_toml(text: str) -> dict[str, RawProfileDefinition]:
    """Parse and validate a `.vgt-profiles.toml` document's text.

    Raises `ProfileDefinitionError` for any unknown key, unknown target,
    missing parent, or out-of-bounds value -- entirely before any profile is
    resolved or a backend is invoked (see the plan's CLI verification
    strategy: "Project profile validation errors occur before backend
    invocation").
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileDefinitionError(f"invalid TOML: {exc}") from exc

    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        _fail(f"unknown top-level key(s) in profiles file: {sorted(unknown)}")

    schema_version = data.get("schema_version")
    if schema_version not in {1, TOML_SCHEMA_VERSION}:
        _fail(f"profiles file schema_version must be 1 or {TOML_SCHEMA_VERSION}, got {schema_version!r}")

    profiles_table = data.get("profiles", {})
    if not isinstance(profiles_table, dict):
        _fail("profiles file's [profiles] must be a table of named profiles")

    definitions: dict[str, RawProfileDefinition] = {}
    for name, table in profiles_table.items():
        if not isinstance(table, dict):
            _fail(f"profile {name!r} must be a table")
        definitions[name] = _parse_one(name, table)
    return definitions


def _parse_one(name: str, table: dict[str, Any]) -> RawProfileDefinition:
    unknown = set(table) - _PROFILE_KEYS
    if unknown:
        _fail(f"profile {name!r} has unknown key(s): {sorted(unknown)}")

    target = table.get("target")
    if not isinstance(target, str) or target not in VALID_TARGETS:
        _fail(f"profile {name!r} has an unknown target {target!r}; must be one of {VALID_TARGETS}")

    extends = table.get("extends")
    if not isinstance(extends, str) or not extends:
        _fail(f"profile {name!r} has no known parent; 'extends' must name a builtin or project profile")

    description = table.get("description")
    if description is not None and not isinstance(description, str):
        _fail(f"profile {name!r}'s description must be a string")

    detection_table = table.get("detection", {})
    if not isinstance(detection_table, dict):
        _fail(f"profile {name!r}'s [detection] must be a table")
    detection: dict[str, Any] = {}
    for key, value in detection_table.items():
        validator = _DETECTION_VALIDATORS.get(key)
        if validator is None:
            _fail(f"profile {name!r} has an unsupported detection parameter {key!r}")
        detection[key] = validator(value, f"profile {name!r}'s detection.{key}")

    cleanup_table = table.get("cleanup", {})
    if not isinstance(cleanup_table, dict):
        _fail(f"profile {name!r}'s [cleanup] must be a table")
    cleanup: dict[str, dict[str, Any]] = {}
    for stage_name, stage_table in cleanup_table.items():
        if stage_name not in CANONICAL_CLEANUP_STAGE_ORDER:
            _fail(f"profile {name!r} names an unsupported cleanup stage {stage_name!r}")
        if not isinstance(stage_table, dict):
            _fail(f"profile {name!r}'s cleanup.{stage_name} must be a table")
        enabled = stage_table.get("enabled", True)
        if not isinstance(enabled, bool):
            _fail(f"profile {name!r}'s cleanup.{stage_name}.enabled must be a boolean")
        validators = _STAGE_VALIDATORS[stage_name]
        params: dict[str, Any] = {}
        for key, value in stage_table.items():
            if key == "enabled":
                continue
            validator = validators.get(key)
            if validator is None:
                _fail(f"profile {name!r}'s cleanup.{stage_name} has an unsupported parameter {key!r}")
            params[key] = validator(value, f"profile {name!r}'s cleanup.{stage_name}.{key}")
        cleanup[stage_name] = {"enabled": enabled, **params}
    frontend_value = table.get("audio_frontend", {"stages": []})
    try:
        audio_frontend = canonical_recipe(frontend_value)
    except AudioFrontendError as exc:
        _fail(f"profile {name!r} has invalid audio_frontend: {exc}")

    return RawProfileDefinition(
        name=name, target=target, extends=extends, description=description,
        detection=detection, cleanup=cleanup, audio_frontend=audio_frontend, raw=table,
    )


def load_project_profiles(project_path: str | Path) -> dict[str, RawProfileDefinition]:
    """Load and validate `<project>.vgt-profiles.toml`, or return `{}` if it
    doesn't exist. Never invokes a backend."""
    path = profiles_path(project_path)
    if not path.is_file():
        return {}
    return parse_profiles_toml(path.read_text(encoding="utf-8"))


def _seed_cleanup_by_name(base: InstrumentProfile) -> dict[str, dict[str, Any]]:
    """The base builtin's cleanup stages, keyed by name, with an empty
    `clamp_sustain` (the one stage the runtime fills in from tempo at
    spec-build time, see `transcribe._instantiate_cleanup`) seeded from its
    documented default so a project override always merges onto real values."""
    seeded: dict[str, dict[str, Any]] = {}
    for stage in base.cleanup:
        seeded[stage.name] = dict(stage.params) if stage.params else dict(_STAGE_DEFAULT_PARAMS.get(stage.name, {}))
    return seeded


def _validate_resolved_detection(detection: Mapping[str, Any], name: str) -> None:
    minimum = detection.get("minimum_frequency_hz")
    maximum = detection.get("maximum_frequency_hz")
    if minimum is not None and maximum is not None and not minimum < maximum:
        _fail(f"profile {name!r} has minimum_frequency_hz >= maximum_frequency_hz ({minimum} >= {maximum})")


def _validate_detection_keys_for_backend(
    chain: list[RawProfileDefinition], *, backend: str, name: str,
) -> None:
    """Reject a detector setting that belongs to a different backend.

    Parsing accepts the union so a project-profile inheritance chain can be
    parsed before its builtin parent is known.  Once resolved, however, every
    key has an unambiguous backend and can receive a useful error.
    """
    allowed = _PYIN_DETECTION_FIELDS if backend == "pyin" else _BASIC_PITCH_DETECTION_FIELDS
    for definition in chain:
        unsupported = sorted(set(definition.detection) - set(allowed))
        if unsupported:
            kind = "pYIN" if backend == "pyin" else "Basic Pitch"
            _fail(
                f"profile {definition.name!r} extends a {kind} profile but has unsupported "
                f"detection parameter(s): {unsupported}"
            )


def _validate_resolved_pyin_detection(detection: Mapping[str, Any], name: str) -> None:
    _validate_resolved_detection(detection, name)
    if detection["hop_length"] > detection["frame_length"]:
        _fail(f"profile {name!r} has hop_length greater than frame_length")
    if detection["median_filter_frames"] % 2 == 0:
        _fail(f"profile {name!r} has an even median_filter_frames; pYIN requires an odd frame count")


def resolve_profile(name: str, project_profiles: Mapping[str, RawProfileDefinition] | None = None) -> ResolvedProfile:
    """Resolve `name` through its `extends` chain to an immutable settings
    snapshot. `name` may be a builtin registered in
    `vgt.transcribe._INSTRUMENT_PROFILES`, or a project profile.

    Raises `ProfileDefinitionError` for an unknown profile/parent, an
    inheritance cycle, or a target mismatch within the chain.
    """
    definitions = project_profiles or {}

    if name not in definitions:
        # ``default`` is a shared cross-target profile name here; the drum
        # compatibility alias is resolved only where the target is known.
        if name in builtin_profile_names_for_target("drums") and name != "default":
            canonical_name = canonical_drum_profile_name(name)
            drum = drum_transcription_profile({"drums": canonical_name})
            return ResolvedProfile(
                name=canonical_name, target="drums", backend=drum.backend,
                detection={"drum_profile": canonical_name}, cleanup=(),
                audio_frontend=dict(drum.audio_frontend), is_builtin=True,
                profile_definition_hash=None,
            )
        if name not in _INSTRUMENT_PROFILES:
            _fail(f"unknown profile {name!r}")
        base = _INSTRUMENT_PROFILES[name]
        if base.backend == "basic-pitch":
            detection = _profile_detection_fields(base)
        elif base.backend == "pyin":
            detection = _pyin_detection_fields(base)
        elif base.backend == "mt3":
            detection = _mt3_detection_fields()
        else:
            detection = {}
        return ResolvedProfile(
            name=name, target=None, backend=base.backend, detection=detection,
            cleanup=tuple(base.cleanup), audio_frontend=dict(base.audio_frontend), is_builtin=True, profile_definition_hash=None,
        )

    chain: list[RawProfileDefinition] = []
    seen: list[str] = []
    current = name
    while current in definitions:
        if current in seen:
            cycle = " -> ".join([*seen, current])
            _fail(f"profile inheritance cycle: {cycle}")
        seen.append(current)
        defn = definitions[current]
        chain.append(defn)
        current = defn.extends

    if current in builtin_profile_names_for_target("drums"):
        current = canonical_drum_profile_name(current)
        target = chain[0].target
        if target != "drums" or any(defn.target != "drums" for defn in chain):
            _fail(f"drum profile {current!r} may only be extended by a drums profile")
        if any(defn.detection or defn.cleanup for defn in chain):
            _fail("project drum profiles may currently override audio_frontend only")
        frontend = dict(drum_transcription_profile({"drums": current}).audio_frontend)
        for defn in reversed(chain):
            if defn.audio_frontend["stages"]:
                frontend = defn.audio_frontend
        return ResolvedProfile(
            name=name, target="drums", backend=drum_transcription_profile({"drums": current}).backend,
            detection={"drum_profile": current}, cleanup=(), audio_frontend=frontend,
            is_builtin=False, profile_definition_hash=_hash(chain[0].raw),
        )
    if current not in _INSTRUMENT_PROFILES:
        _fail(f"profile {chain[-1].name!r} extends unknown parent {current!r}")
    base = _INSTRUMENT_PROFILES[current]
    if base.backend not in {"basic-pitch", "pyin"}:
        _fail(f"profile {name!r} extends {current!r}, whose backend does not support project overrides")

    target = chain[0].target
    for defn in chain:
        if defn.target != target:
            _fail(f"profile {defn.name!r} targets {defn.target!r}, incompatible with {name!r}'s target {target!r}")
    if target == "drums":
        _fail(f"profile {name!r} targets 'drums' but does not extend a built-in drum profile")

    _validate_detection_keys_for_backend(chain, backend=base.backend, name=name)

    detection = _pyin_detection_fields(base) if base.backend == "pyin" else _profile_detection_fields(base)
    cleanup_by_name = _seed_cleanup_by_name(base)
    frontend = dict(base.audio_frontend)
    for defn in reversed(chain):
        for key, value in defn.detection.items():
            detection[key] = value
        for stage_name, override in defn.cleanup.items():
            if not override.get("enabled", True):
                cleanup_by_name.pop(stage_name, None)
                continue
            params = dict(cleanup_by_name.get(stage_name) or _STAGE_DEFAULT_PARAMS.get(stage_name, {}))
            params.update({key: value for key, value in override.items() if key != "enabled"})
            cleanup_by_name[stage_name] = params
        if defn.audio_frontend["stages"]:
            frontend = defn.audio_frontend

    if base.backend == "pyin":
        _validate_resolved_pyin_detection(detection, name)
    else:
        _validate_resolved_detection(detection, name)
    ordered_cleanup = tuple(
        CleanupStage(stage_name, cleanup_by_name[stage_name])
        for stage_name in CANONICAL_CLEANUP_STAGE_ORDER
        if stage_name in cleanup_by_name
    )
    definition_hash = _hash(chain[0].raw)
    return ResolvedProfile(
        name=name, target=target, backend=base.backend, detection=detection,
        cleanup=ordered_cleanup, audio_frontend=frontend, is_builtin=False, profile_definition_hash=definition_hash,
    )


def _pyin_detection_fields(profile: InstrumentProfile) -> dict[str, Any]:
    """A pyin profile's detection settings, in the same `resolved.detection`
    slot a Basic Pitch profile fills -- so `profile show`, `variant list`, and
    the sidecar's `resolved_settings` describe what actually ran instead of an
    empty table or, worse, Basic Pitch fields nothing consulted."""
    settings = profile.pyin
    fields: dict[str, Any] = {
        "minimum_note_length_ms": profile.minimum_note_length_ms,
        "minimum_frequency_hz": profile.minimum_frequency_hz,
        "maximum_frequency_hz": profile.maximum_frequency_hz,
    }
    if settings is not None:
        fields.update(
            sample_rate_hz=settings.sample_rate_hz,
            frame_length=settings.frame_length,
            hop_length=settings.hop_length,
            median_filter_frames=settings.median_filter_frames,
            rearticulation_span_frames=settings.rearticulation_span_frames,
            rearticulation_rise_db=settings.rearticulation_rise_db,
            rearticulation_minimum_spacing_beats=settings.rearticulation_minimum_spacing_beats,
        )
    return fields


def _profile_detection_fields(profile: InstrumentProfile) -> dict[str, Any]:
    return {
        "onset_threshold": profile.onset_threshold,
        "frame_threshold": profile.frame_threshold,
        "minimum_note_length_ms": profile.minimum_note_length_ms,
        "minimum_frequency_hz": profile.minimum_frequency_hz,
        "maximum_frequency_hz": profile.maximum_frequency_hz,
        "melodia_trick": profile.melodia_trick,
        "multiple_pitch_bends": profile.multiple_pitch_bends,
    }


def _mt3_detection_fields() -> dict[str, Any]:
    """MT3's pinned identity, in the same `resolved.detection` slot a Basic
    Pitch/pYIN profile fills -- so `profile show`/`variant add` report the
    fork/commit/model/normalization-version pin instead of an empty table or
    unread Basic Pitch fields. There is no per-`InstrumentProfile` variation
    to read here (issue #288: MT3 has no per-instrument detection settings),
    unlike the two functions above."""
    from .mt3_normalize import MT3_NOTE_NORMALIZATION_VERSION, MT3_TRACK_SELECTION_VERSION
    from .mt3_provision import (
        MT3_INPUT_LENGTH_FRAMES, MT3_LOCK_SHA256, MT3_LOOKAHEAD_FRAMES,
        MT3_MODEL_ID, MT3_PINNED_COMMIT, MT3_PINNED_TAG, MT3_REPO_URL,
        MT3_RUNTIME_VERSION,
    )

    return {
        "repository": MT3_REPO_URL,
        "tag": MT3_PINNED_TAG,
        "commit": MT3_PINNED_COMMIT,
        "runtime_version": MT3_RUNTIME_VERSION,
        "lock_sha256": MT3_LOCK_SHA256,
        "model_id": MT3_MODEL_ID,
        "input_length_frames": MT3_INPUT_LENGTH_FRAMES,
        "lookahead_frames": MT3_LOOKAHEAD_FRAMES,
        "track_selection_version": MT3_TRACK_SELECTION_VERSION,
        "note_normalization_version": MT3_NOTE_NORMALIZATION_VERSION,
    }


def validate_profile_for_target(
    name: str, target: str, project_profiles: Mapping[str, RawProfileDefinition] | None = None
) -> ResolvedProfile:
    """Resolve `name` and reject it if incompatible with `target`.

    A builtin resolved without a target context (`ResolvedProfile.target is
    None`) is compatible with every target -- matching how `default` already
    behaves in `vgt.transcribe`.
    """
    if target not in VALID_TARGETS:
        _fail(f"target must be one of {VALID_TARGETS}, got {target!r}")
    resolved = resolve_profile(name, project_profiles)
    if resolved.target is not None and resolved.target != target:
        _fail(f"profile {name!r} is selected for incompatible target {target!r} (profile targets {resolved.target!r})")
    return resolved


def validate_project_profiles(project_path: str | Path) -> dict[str, ResolvedProfile]:
    """Load and resolve every profile in `<project>.vgt-profiles.toml`.

    Resolving every definition (not just ones a caller happens to reference)
    surfaces an unknown parent, a cycle, or an out-of-bounds value before any
    backend runs -- exactly the validation-before-invocation contract the
    plan requires.
    """
    definitions = load_project_profiles(project_path)
    return {name: resolve_profile(name, definitions) for name in definitions}


def resolved_detection_identity(resolved: ResolvedProfile) -> dict[str, Any]:
    return {"backend": resolved.backend, **resolved.detection}


def resolved_cleanup_identity(resolved: ResolvedProfile) -> list[dict[str, Any]]:
    return [{"name": stage.name, "params": stage.params} for stage in resolved.cleanup]


def resolved_detection_hash(resolved: ResolvedProfile) -> str:
    return _hash(resolved_detection_identity(resolved))


def resolved_cleanup_hash(resolved: ResolvedProfile) -> str:
    payload = {"detection_hash": resolved_detection_hash(resolved), "cleanup": resolved_cleanup_identity(resolved)}
    return _hash(payload)


def resolved_settings_snapshot(resolved: ResolvedProfile) -> dict[str, Any]:
    """The sidecar-ready `resolved_settings` shape (schema v13's
    `targets[target].variants[id].resolved_settings`, see sidecar.py)."""
    return {"detection": dict(resolved.detection), "cleanup": resolved_cleanup_identity(resolved), "audio_frontend": resolved.audio_frontend}


def _instantiate_resolved_cleanup(
    cleanup: tuple[CleanupStage, ...], *, midi_tempo: float | None, time_signature: str | None
) -> tuple[CleanupStage, ...]:
    """Fill in `clamp_sustain`'s tempo-dependent bound, mirroring
    `transcribe._instantiate_cleanup`. Unlike a builtin `InstrumentProfile`
    (whose template always carries an empty `clamp_sustain` params dict,
    filled in only from the fixed `GUITAR_SUSTAIN_CLAMP_BARS` constant), a
    resolved profile's `clamp_sustain` stage already carries its own
    `max_bars` (the project-overridable unit `transcription_profiles`
    validates, see `_STAGE_VALIDATORS`) -- so this reads that value back
    instead of assuming the global default."""
    stages: list[CleanupStage] = []
    for stage in cleanup:
        if stage.name == "clamp_sustain":
            bar_seconds = _bar_duration_seconds(midi_tempo, time_signature)
            if bar_seconds is None:
                continue
            max_bars = stage.params.get("max_bars", GUITAR_SUSTAIN_CLAMP_BARS)
            stage = CleanupStage("clamp_sustain", {"max_duration_s": bar_seconds * max_bars})
        stages.append(stage)
    return tuple(stages)


def spec_from_resolved_profile(
    resolved: ResolvedProfile,
    *,
    package_pin: str = BASIC_PITCH_PACKAGE_PIN,
    serialization: str = BASIC_PITCH_SERIALIZATION,
    midi_tempo: float | None = None,
    time_signature: str | None = None,
    tempo_map: TempoMapReference | None = None,
    mt3_checkpoint_fingerprint: str | None = None,
    target: str | None = None,
) -> BasicPitchSpec | PyinSpec | Mt3Spec:
    """Build the spec a resolved profile (builtin or project-local) describes
    -- the bridge `transcription_profiles.py`'s module docstring calls out as
    later-issue work: turning a `ResolvedProfile` into the same spec shape
    `default_spec_for_target` produces for a builtin-only selection, so
    `vgt.transcription_lifecycle`'s `variant add` can run any resolved profile
    through `vgt.transcription_variants.reconcile_variants` unchanged.

    `mt3_checkpoint_fingerprint` is a caller-supplied local provisioning
    check (see `vgt.mt3_provision.mt3_status`), never derived here -- the
    same reason `vgt.transcribe.default_spec_for_target` never derives it
    either. `target` is required for `backend == "mt3"`: unlike every other
    resolved profile, a *builtin* mt3 profile resolves with `resolved.target
    is None` (the same as `default`, since resolution has no target context
    of its own -- see `resolve_profile`), so the caller's own already-known
    target (`vgt.transcription_lifecycle.add_variant` has one) must be passed
    in explicitly for `vgt.mt3_normalize`'s program-family elimination."""
    if resolved.backend == "mt3":
        if target is None:
            raise ProfileDefinitionError(f"profile {resolved.name!r} requires an explicit target to resolve an Mt3Spec")
        detection = resolved.detection
        return Mt3Spec(
            backend="mt3",
            repository=detection["repository"],
            tag=detection["tag"],
            commit=detection["commit"],
            runtime_version=detection["runtime_version"],
            lock_sha256=detection["lock_sha256"],
            model_id=detection["model_id"],
            input_length_frames=detection["input_length_frames"],
            lookahead_frames=detection["lookahead_frames"],
            checkpoint_fingerprint=mt3_checkpoint_fingerprint,
            track_selection_version=detection["track_selection_version"],
            note_normalization_version=detection["note_normalization_version"],
            target=target,
            midi_tempo=midi_tempo,
            tempo_map=tempo_map,
        )
    if resolved.backend == "pyin":
        detection = resolved.detection
        cleanup = _instantiate_resolved_cleanup(resolved.cleanup, midi_tempo=midi_tempo, time_signature=time_signature)
        return PyinSpec(
            backend="pyin",
            algorithm_version=PYIN_ALGORITHM_VERSION,
            sample_rate_hz=detection["sample_rate_hz"],
            frame_length=detection["frame_length"],
            hop_length=detection["hop_length"],
            median_filter_frames=detection["median_filter_frames"],
            rearticulation_span_frames=detection["rearticulation_span_frames"],
            rearticulation_rise_db=detection["rearticulation_rise_db"],
            rearticulation_minimum_spacing_beats=detection["rearticulation_minimum_spacing_beats"],
            minimum_note_length_ms=detection["minimum_note_length_ms"],
            minimum_frequency_hz=detection["minimum_frequency_hz"],
            maximum_frequency_hz=detection["maximum_frequency_hz"],
            midi_tempo=midi_tempo,
            tempo_map=tempo_map,
            cleanup=cleanup,
        )
    if resolved.backend != "basic-pitch":
        raise ProfileDefinitionError(f"profile {resolved.name!r} is not a Basic Pitch profile and has no spec bridge")
    detection = resolved.detection
    cleanup = _instantiate_resolved_cleanup(resolved.cleanup, midi_tempo=midi_tempo, time_signature=time_signature)
    return BasicPitchSpec(
        backend="basic-pitch",
        package_pin=package_pin,
        serialization=serialization,
        onset_threshold=detection["onset_threshold"],
        frame_threshold=detection["frame_threshold"],
        minimum_note_length_ms=detection["minimum_note_length_ms"],
        minimum_frequency_hz=detection.get("minimum_frequency_hz"),
        maximum_frequency_hz=detection.get("maximum_frequency_hz"),
        multiple_pitch_bends=detection["multiple_pitch_bends"],
        melodia_trick=detection["melodia_trick"],
        midi_tempo=midi_tempo,
        tempo_map=tempo_map,
        cleanup=cleanup,
    )
