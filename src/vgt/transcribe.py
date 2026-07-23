"""Per-target MIDI transcription seam.

Mirrors `separation.py`'s split between an orchestrator (owned by a later
issue) and a thin backend (`Transcriber`): this module owns the per-target
`TranscriptionSpec` defaults, spec hashing, target-name validation, and
artifact naming, while a backend only turns one stem into a MIDI/CSV pair.
`FakeTranscriber` writes a small deterministic, valid MIDI file (plus its
matching lenient-CSV note list) instead of invoking Basic Pitch, so the
offline test suite never runs a model -- exactly the role `FakeSeparator`
plays for stem separation. `BasicPitchTranscriber` is the real backend: a
pinned `uvx` subprocess invocation, isolated from vgt's own interpreter
because Basic Pitch cannot resolve on vgt's Python (see the module's
`docs/transcription-plan.md` "one hard constraint" section) -- it must never
become a vgt dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile

# Valid target names: the separation artifact names, plus the untouched mix
# ("original"). A target is always a single named source, never a merged set.
VALID_TARGETS: tuple[str, ...] = (
    "guitar",
    "bass",
    "vocals",
    "drums",
    "instrumental",
    "backing",
    "strings",
    "piano",
    "original",
)

# Display labels are shared with the ReaScript's `[vgt] … Ref (MIDI)` track
# names. Keep this mapping alongside the target contract rather than deriving
# labels from identifiers (notably `backing` and `piano`).
TARGET_LABELS: dict[str, str] = {
    "guitar": "Guitar",
    "bass": "Bass",
    "vocals": "Vocals",
    "drums": "Drums",
    "instrumental": "Instrumental",
    "backing": "Backing (no guitar)",
    "strings": "Strings",
    "piano": "Keys / Piano",
    "original": "Original",
}

BASIC_PITCH_PACKAGE_PIN = "basic-pitch[onnx]==0.4.0"
BASIC_PITCH_SERIALIZATION = "onnx"
DRUMSCRIPT_PACKAGE_PIN = "drumscript==0.1.6"
# This is the isolated interpreter contract D-B will implement.  It is part of
# the identity now so changing it later cannot reuse a MIDI made by another
# runtime.
DRUMSCRIPT_RUNTIME_VERSION = "python==3.12"
DRUMSCRIPT_CLASSIFIER_MODE = "standard-polyphonic"

# Overrides the whole `uvx ...` invocation with a pre-installed binary, e.g.
# `uv tool install --python 3.11 --with "setuptools<81" "basic-pitch[onnx]==0.4.0"`
# then `VGT_BASIC_PITCH_CMD=basic-pitch`, so an offline machine can prebuild
# the env once instead of paying the ~35s cold `uvx` build on every run.
BASIC_PITCH_CMD_ENV = "VGT_BASIC_PITCH_CMD"
DRUMSCRIPT_CMD_ENV = "VGT_DRUMSCRIPT_CMD"

# DrumScript's public event labels and their General MIDI percussion notes.
# Keep this deliberately small and explicit: accepting a new upstream label is
# an output-contract change, not something to silently pass through.
DRUMSCRIPT_INSTRUMENTS: dict[str, int] = {
    "kick": 36,
    "snare": 38,
    "low_tom": 41,
    "mid_tom": 45,
    "high_tom": 48,
    "hi_hat_closed": 42,
    "hi_hat_open": 46,
    "crash": 49,
    "ride": 51,
}

# Shared defaults across every target (see docs/transcription-plan.md section 1).
DEFAULT_ONSET_THRESHOLD = 0.5
DEFAULT_FRAME_THRESHOLD = 0.3
DEFAULT_MINIMUM_NOTE_LENGTH_MS = 60.0
DEFAULT_MULTIPLE_PITCH_BENDS = False  # single pitch-bend mode
DEFAULT_MELODIA_TRICK = True

# Acoustic-guitar-only overrides (see docs/guitar-transcription-findings.md).
# A continuously strummed steel-string rings for seconds, so successive
# chords overlap their own ring-out and the shared 0.3 frame threshold never
# sees an activation drop low enough to release a note; melodia then bridges
# the surviving gaps into multi-minute drones. These were measured against
# one real acoustic stem only -- `guitar_type: electric` (and unset) keep the
# generic guitar defaults above, since a palm-muted electric's sustain
# behaviour is different and untested.
GUITAR_ACOUSTIC_FREQUENCY_HZ = (80.0, 1200.0)  # standard-tuned low E2 (82.4 Hz) to a steel-string's practical top
GUITAR_ACOUSTIC_ONSET_THRESHOLD = 0.6
GUITAR_ACOUSTIC_FRAME_THRESHOLD = 0.65
GUITAR_ACOUSTIC_MINIMUM_NOTE_LENGTH_MS = 100.0
GUITAR_ACOUSTIC_MELODIA_TRICK = False

# Post-transcription acoustic-guitar cleanup: no Basic Pitch setting alone
# gets polyphony down to something playable, so this always runs alongside
# the acoustic overrides above (see `_apply_cleanup_stages`).
GUITAR_MAX_SIMULTANEOUS_VOICES = 6  # a guitar has six strings
GUITAR_SUSTAIN_CLAMP_BARS = 2.0  # in bars, not seconds, so slower material isn't clamped tighter than faster material
GUITAR_HARMONIC_GHOST_INTERVALS: tuple[int, ...] = (12, 19, 24, 28, 31, 36)  # octave, 12th, and further partials
GUITAR_GHOST_ONSET_TOLERANCE_S = 0.05
GUITAR_GHOST_OVERLAP_FRACTION = 0.6
GUITAR_GHOST_VELOCITY_SLACK = 4
GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S = 0.04

# Raising `frame_threshold` to stop the drones has a side effect: a held note
# whose activation dips below the threshold mid-way is emitted as two notes
# split in place.  Measured on the 7Rivers output, 390 of 435 same-pitch gaps
# under 300 ms were *exactly* zero-width, with nothing at all between 0 and
# 10 ms and a clean cliff before genuine re-articulations above 300 ms -- so
# this threshold sits in the model's frame domain (a few analysis hops), not
# the musical domain, and is deliberately NOT tempo-scaled: at a slow tempo a
# bar-relative gap would start swallowing real repeated notes.
GUITAR_FRAGMENT_MERGE_GAP_S = 0.03

# A short note with no same-pitch neighbour anywhere near it is almost always
# a transient artifact rather than a played note.  Both bounds are deliberately
# conservative -- this removes ~17 notes on the reference track, and the point
# is to catch obvious blips, not to thin the part.
GUITAR_ISOLATED_MAX_DURATION_S = 0.15
GUITAR_ISOLATED_NEIGHBOUR_WINDOW_S = 1.0


@dataclass(frozen=True)
class CleanupStage:
    """One named, parameterized step in a `BasicPitchSpec.cleanup` pipeline.

    A tuning constant a stage needs belongs in `params`, not in a module
    global read directly by the stage function: `params` flows through
    `BasicPitchSpec.to_dict()` into `settings_hash` by construction, so
    retuning a stage can never leave a cached transcription stale without the
    hash noticing (the bug this record exists to close).
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeExpectations:
    """Measured bounds used to evaluate one instrument's note-event CSV.

    These do not affect transcription.  They let the standalone probe score
    an output against the same instrument identity that selected its
    transcription settings, without silently carrying guitar assumptions to a
    new instrument.
    """

    expected_voice_count: int
    harmonic_ghost_intervals: tuple[int, ...]
    sustain_cap_s: float


@dataclass(frozen=True)
class InstrumentProfile:
    """One instrument's complete transcription identity: Basic Pitch model
    parameters plus its ordered post-processing pipeline.

    Adding a second tuned instrument means adding an entry to
    `_INSTRUMENT_PROFILES`, not copying an `if` branch in
    `default_spec_for_target` -- see the module docstring.
    """

    name: str
    backend: str
    onset_threshold: float
    frame_threshold: float
    minimum_note_length_ms: float
    minimum_frequency_hz: float | None
    maximum_frequency_hz: float | None
    multiple_pitch_bends: bool
    melodia_trick: bool
    cleanup: tuple[CleanupStage, ...] = ()
    probe_expectations: ProbeExpectations | None = None


_DEFAULT_PROFILE = InstrumentProfile(
    name="default",
    backend="basic-pitch",
    onset_threshold=DEFAULT_ONSET_THRESHOLD,
    frame_threshold=DEFAULT_FRAME_THRESHOLD,
    minimum_note_length_ms=DEFAULT_MINIMUM_NOTE_LENGTH_MS,
    minimum_frequency_hz=None,
    maximum_frequency_hz=None,
    multiple_pitch_bends=DEFAULT_MULTIPLE_PITCH_BENDS,
    melodia_trick=DEFAULT_MELODIA_TRICK,
)

# Per-target frequency narrowing. Targets absent from `_INSTRUMENT_PROFILES`
# (polyphonic/unpredictable sources) fall back to `_DEFAULT_PROFILE`'s
# unbounded range.
_GUITAR_PROFILE = replace(
    _DEFAULT_PROFILE,
    name="guitar",
    minimum_frequency_hz=70.0,
    maximum_frequency_hz=1400.0,
)  # below drop/Eb-tuned E2 (82.4 Hz), above 24th-fret E6 (1318.5 Hz)
_BASS_PROFILE = replace(
    _DEFAULT_PROFILE, name="bass", minimum_frequency_hz=30.0, maximum_frequency_hz=400.0
)  # 5-string low B is 30.9 Hz
_VOCALS_PROFILE = replace(
    _DEFAULT_PROFILE, name="vocals", minimum_frequency_hz=70.0, maximum_frequency_hz=1200.0
)  # bass voice to whistle-adjacent soprano

# `_GUITAR_ACOUSTIC_PROFILE`'s cleanup order is load-bearing, and every stage
# depends on the ones before it:
#
# 1. `merge_fragments` reassembles a note the model split in place. It must be
#    first: every later stage reasons about note *lengths*, and a fragmented
#    note has the wrong length. Running it last instead re-extends notes past
#    decisions the clamp and voice cap already made -- merging the reference
#    track's *finished* output re-created a 7.1 s note under a 4 s clamp, and
#    at a 30 ms gap pushed polyphony from 6 to 7, breaking both invariants
#    this pipeline exists to guarantee.
# 2. `drop_isolated_notes` removes blips, now that a lone fragment of a real
#    note is no longer mistaken for one.
# 3. `clamp_sustain` caps runaway sustains -- after merging, since a chain of
#    fragments is exactly how a drone survives step 1.
# 4. `drop_harmonic_ghosts` compares overlaps against clamped (no longer
#    absurdly long) durations.
# 5. `cap_simultaneous_voices` runs last so it enforces six voices on the
#    final note lengths, which is the only place the invariant can hold.
#
# `clamp_sustain`'s `params` starts empty: `default_spec_for_target` fills in
# `max_duration_s` from the detected tempo (see `_instantiate_cleanup`), and
# drops the stage entirely when no tempo is known yet, mirroring the old
# `sustain_clamp_s: float | None` field's behaviour.
_GUITAR_ACOUSTIC_PROFILE = InstrumentProfile(
    name="guitar-acoustic",
    backend="basic-pitch",
    onset_threshold=GUITAR_ACOUSTIC_ONSET_THRESHOLD,
    frame_threshold=GUITAR_ACOUSTIC_FRAME_THRESHOLD,
    minimum_note_length_ms=GUITAR_ACOUSTIC_MINIMUM_NOTE_LENGTH_MS,
    minimum_frequency_hz=GUITAR_ACOUSTIC_FREQUENCY_HZ[0],
    maximum_frequency_hz=GUITAR_ACOUSTIC_FREQUENCY_HZ[1],
    multiple_pitch_bends=DEFAULT_MULTIPLE_PITCH_BENDS,
    melodia_trick=GUITAR_ACOUSTIC_MELODIA_TRICK,
    cleanup=(
        CleanupStage("merge_fragments", {"max_gap_s": GUITAR_FRAGMENT_MERGE_GAP_S}),
        CleanupStage(
            "drop_isolated_notes",
            {
                "max_duration_s": GUITAR_ISOLATED_MAX_DURATION_S,
                "neighbour_window_s": GUITAR_ISOLATED_NEIGHBOUR_WINDOW_S,
            },
        ),
        CleanupStage("clamp_sustain", {}),
        CleanupStage(
            "drop_harmonic_ghosts",
            {
                "intervals": GUITAR_HARMONIC_GHOST_INTERVALS,
                "onset_tolerance_s": GUITAR_GHOST_ONSET_TOLERANCE_S,
                "overlap_fraction": GUITAR_GHOST_OVERLAP_FRACTION,
                "velocity_slack": GUITAR_GHOST_VELOCITY_SLACK,
            },
        ),
        CleanupStage(
            "cap_simultaneous_voices",
            {
                "max_voices": GUITAR_MAX_SIMULTANEOUS_VOICES,
                "min_duration_after_cap_s": GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
            },
        ),
    ),
    probe_expectations=ProbeExpectations(
        expected_voice_count=GUITAR_MAX_SIMULTANEOUS_VOICES,
        harmonic_ghost_intervals=GUITAR_HARMONIC_GHOST_INTERVALS,
        sustain_cap_s=4.0,
    ),
)

_INSTRUMENT_PROFILES: dict[str, InstrumentProfile] = {
    "default": _DEFAULT_PROFILE,
    "guitar": _GUITAR_PROFILE,
    "bass": _BASS_PROFILE,
    "vocals": _VOCALS_PROFILE,
    "guitar-acoustic": _GUITAR_ACOUSTIC_PROFILE,
}
VALID_PROFILE_NAMES: tuple[str, ...] = tuple(_INSTRUMENT_PROFILES)


def instrument_profile(name: str) -> InstrumentProfile:
    """Return a named registry profile, rejecting unknown names clearly.

    This read-only accessor is intentionally usable by evaluation tools as
    well as transcription orchestration; resolving it never invokes a model
    or a backend.
    """
    validate_profile_name(name)
    return _INSTRUMENT_PROFILES[name]

# A profile is an instrument-specific transcription identity.  ``default``
# deliberately remains available for every target: selecting it explicitly is
# useful when a user wants to opt out of that target's tuned default.  Stored
# sidecars may name profiles that were later removed or moved to another
# target, so lookup still falls back safely below.
_PROFILE_NAMES_BY_TARGET: dict[str, tuple[str, ...]] = {
    target: ("default", target) if target in _INSTRUMENT_PROFILES else ("default",)
    for target in VALID_TARGETS
}
_PROFILE_NAMES_BY_TARGET["guitar"] = ("default", "guitar", "guitar-acoustic")


def validate_profile_name(profile: str) -> str:
    if profile not in _INSTRUMENT_PROFILES:
        raise TranscriptionError(f"profile must be one of {VALID_PROFILE_NAMES}, got {profile!r}")
    return profile


def valid_profile_names_for_target(target: str) -> tuple[str, ...]:
    """Return the registry profiles an explicit mode may select for target."""
    validate_target(target)
    return _PROFILE_NAMES_BY_TARGET[target]


def validate_profile_for_target(target: str, profile: str) -> str:
    """Validate an explicit ``TARGET=PROFILE`` selection from the CLI/API."""
    valid_profiles = valid_profile_names_for_target(target)
    if profile not in valid_profiles:
        raise TranscriptionError(
            f"profile for {target!r} must be one of {valid_profiles}, got {profile!r}"
        )
    return profile


def _profile_for_target(target: str, modes: Mapping[str, str] | None) -> InstrumentProfile:
    """Resolve ``target`` through an optional target-to-profile map.

    A sidecar can outlive the profile registry that wrote it.  Missing or
    unrecognised stored selections therefore safely use the target's default;
    only explicit CLI input is validated by :func:`validate_profile_for_target`.
    """
    profile_name = modes.get(target) if isinstance(modes, Mapping) else None
    if profile_name in valid_profile_names_for_target(target):
        return _INSTRUMENT_PROFILES[profile_name]
    return _INSTRUMENT_PROFILES.get(target, _DEFAULT_PROFILE)


def _instantiate_cleanup(
    template: tuple[CleanupStage, ...], *, sustain_clamp_s: float | None
) -> tuple[CleanupStage, ...]:
    """Fill in the one cleanup parameter a profile can't declare statically:
    `clamp_sustain`'s bound, which depends on the tempo detected for this
    specific transcription run, not on instrument tuning. Drops the stage
    entirely when no tempo is known yet."""
    stages: list[CleanupStage] = []
    for stage in template:
        if stage.name == "clamp_sustain":
            if sustain_clamp_s is None:
                continue
            stage = CleanupStage("clamp_sustain", {"max_duration_s": sustain_clamp_s})
        stages.append(stage)
    return tuple(stages)


class TranscriptionError(ValueError):
    """A target, spec, or transcription request cannot be processed."""


def validate_target(target: str) -> str:
    if target not in VALID_TARGETS:
        raise TranscriptionError(f"target must be one of {VALID_TARGETS}, got {target!r}")
    return target


def midi_artifact_name(target: str) -> str:
    return f"transcription/{validate_target(target)}.mid"


def notes_artifact_name(target: str) -> str:
    return f"transcription/{validate_target(target)}.csv"


def events_artifact_name(target: str) -> str:
    return f"transcription/{validate_target(target)}.json"


# The five guitar-only fields `BasicPitchSpec` carried before the `cleanup`
# tuple replaced them, always at these defaults for every target without a
# cleanup pipeline. `BasicPitchSpec.to_dict` re-inserts this exact shape
# whenever `cleanup` is empty, so removing these fields from the dataclass
# does not move `settings_hash` for bass/vocals/generic-guitar/etc. -- only
# guitar-acoustic's hash is meant to move (see that field's docstring).
_LEGACY_EMPTY_CLEANUP_FIELDS: dict[str, Any] = {
    "max_simultaneous_voices": None,
    "sustain_clamp_s": None,
    "drop_harmonic_ghosts": False,
    "merge_gap_s": None,
    "drop_isolated_notes": False,
}


@dataclass(frozen=True)
class BasicPitchSpec:
    """Everything that changes one target's transcribed output. One spec per
    target, derived from `default_spec_for_target` -- so retuning one
    target's thresholds never changes another target's `settings_hash`."""

    backend: str  # "basic-pitch" | "fake"
    package_pin: str
    serialization: str
    onset_threshold: float
    frame_threshold: float
    minimum_note_length_ms: float
    minimum_frequency_hz: float | None
    maximum_frequency_hz: float | None
    multiple_pitch_bends: bool
    melodia_trick: bool
    midi_tempo: float | None  # from the tempo stage's detected BPM
    # Post-transcription cleanup this target's profile requests (see
    # `_apply_cleanup_stages`); empty for every target without a pipeline, so
    # their `settings_hash` reflects "disabled". Every stage's tuning
    # parameters live here too -- see `CleanupStage`.
    cleanup: tuple[CleanupStage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize for `spec_hash`.

        When `cleanup` is empty, this reproduces the pre-refactor spec's exact
        five-field shape (`_LEGACY_EMPTY_CLEANUP_FIELDS`) instead of the new
        `cleanup` key, so `settings_hash` is byte-identical to before for
        every target except guitar-acoustic -- the one target whose non-empty
        `cleanup` is this refactor's single deliberate hash change (see the
        module's guitar-acoustic profile docstring).
        """
        data = asdict(self)
        if not data["cleanup"]:
            del data["cleanup"]
            data.update(_LEGACY_EMPTY_CLEANUP_FIELDS)
        return data


@dataclass(frozen=True)
class DrumScriptSpec:
    """Settings that can affect DrumScript events or the MIDI derived from them.

    D-A deliberately defines identity only; it does not install or execute
    DrumScript.  `time_signature` is optional because the planned backend may
    use the detected signature as an input in a later issue.
    """

    backend: str
    package_pin: str
    runtime_version: str
    classifier_mode: str
    time_signature: tuple[int, int] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


TranscriptionSpec = BasicPitchSpec | DrumScriptSpec


def _bar_duration_seconds(bpm: float | None, time_signature: str | None) -> float | None:
    """Seconds per bar for a `bpm`/`"N/D"` pair, or `None` when `bpm` isn't
    known yet. Assumes 4/4 when `time_signature` is missing or malformed --
    the same fallback `tempo.py` already applies when detection can't
    establish a signature."""
    if not bpm:
        return None
    numerator, denominator = 4, 4
    match = re.match(r"(\d+)/(\d+)", time_signature) if time_signature else None
    if match:
        numerator, denominator = int(match.group(1)), int(match.group(2))
    beats_per_bar = numerator * (4.0 / denominator)
    return beats_per_bar * 60.0 / bpm


def default_spec_for_target(
    target: str,
    *,
    backend: str = "basic-pitch",
    package_pin: str = BASIC_PITCH_PACKAGE_PIN,
    serialization: str = BASIC_PITCH_SERIALIZATION,
    midi_tempo: float | None = None,
    modes: Mapping[str, str] | None = None,
    time_signature: str | None = None,
    drumscript_runtime_version: str = DRUMSCRIPT_RUNTIME_VERSION,
    drumscript_classifier_mode: str = DRUMSCRIPT_CLASSIFIER_MODE,
    drumscript_time_signature: tuple[int, int] | None = None,
) -> TranscriptionSpec:
    """The per-target default spec, resolved through `_INSTRUMENT_PROFILES`.

    ``modes`` selects a named profile independently for each target. An absent
    or stale selection falls back to that target's default. `time_signature` (a
    tempo-stage string like `"4/4"`) converts that profile's cleanup stage's
    bar-based sustain clamp to seconds at this specific tempo.
    """
    validate_target(target)
    if backend == "drumscript":
        return DrumScriptSpec(
            backend=backend,
            package_pin=package_pin if package_pin != BASIC_PITCH_PACKAGE_PIN else DRUMSCRIPT_PACKAGE_PIN,
            runtime_version=drumscript_runtime_version,
            classifier_mode=drumscript_classifier_mode,
            time_signature=drumscript_time_signature,
        )
    profile = _profile_for_target(target, modes)
    bar_seconds = _bar_duration_seconds(midi_tempo, time_signature)
    sustain_clamp_s = bar_seconds * GUITAR_SUSTAIN_CLAMP_BARS if bar_seconds else None
    return BasicPitchSpec(
        backend=backend,
        package_pin=package_pin,
        serialization=serialization,
        onset_threshold=profile.onset_threshold,
        frame_threshold=profile.frame_threshold,
        minimum_note_length_ms=profile.minimum_note_length_ms,
        minimum_frequency_hz=profile.minimum_frequency_hz,
        maximum_frequency_hz=profile.maximum_frequency_hz,
        multiple_pitch_bends=profile.multiple_pitch_bends,
        melodia_trick=profile.melodia_trick,
        midi_tempo=midi_tempo,
        cleanup=_instantiate_cleanup(profile.cleanup, sustain_clamp_s=sustain_clamp_s),
    )


def spec_hash(spec: TranscriptionSpec) -> str:
    return hashlib.sha256(json.dumps(spec.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()


def resolve_target_source(
    project_path: Path, target: str, analysis: dict[str, Any], *, reference_source: Path
) -> tuple[Path, dict[str, Any] | None] | None:
    """Resolve `target`'s source audio, or `None` if it isn't available yet.

    Mirrors the defensive treatment `analysis.chord_sources` gives optional
    stem artifacts: a target whose stem is missing (not yet separated, or its
    file has disappeared) resolves to `None` -- never the mix -- so a caller
    can record a retained `skipped-missing-source` entry instead of silently
    substituting the wrong source. `original` is the one target that legitimately
    resolves to the reference mix rather than a separated stem. The second
    tuple element is the stem's artifact record when one exists, so a caller
    can prefer its already-computed `sha256` over rehashing the file.
    """
    validate_target(target)
    if target == "original":
        return (reference_source, None) if reference_source.is_file() else None

    from .separation import artifact_path

    artifacts = (analysis.get("stems") or {}).get("artifacts") or {}
    artifact = artifacts.get(target)
    if not isinstance(artifact, dict) or not isinstance(artifact.get("file"), str):
        return None
    try:
        path = artifact_path(project_path, artifact)
    except (KeyError, TypeError, ValueError):
        return None
    return (path, artifact) if path.is_file() else None


def target_input_hash(path: Path, artifact: dict[str, Any] | None) -> str:
    """The stem artifact's recorded `sha256` when present (separation already
    content-addresses it), falling back to `analysis.hash_source_file` --
    e.g. for `original`, which has no separation artifact record."""
    if isinstance(artifact, dict) and isinstance(artifact.get("sha256"), str):
        return artifact["sha256"]
    from .analysis import hash_source_file

    return hash_source_file(path)


def _settings_dict(spec: TranscriptionSpec) -> dict[str, Any]:
    if isinstance(spec, DrumScriptSpec):
        return {
            "runtime_version": spec.runtime_version,
            "classifier_mode": spec.classifier_mode,
            "time_signature": list(spec.time_signature) if spec.time_signature else None,
        }
    return {
        "onset_threshold": spec.onset_threshold,
        "frame_threshold": spec.frame_threshold,
        "minimum_note_length_ms": spec.minimum_note_length_ms,
        "minimum_frequency_hz": spec.minimum_frequency_hz,
        "maximum_frequency_hz": spec.maximum_frequency_hz,
        "multiple_pitch_bends": spec.multiple_pitch_bends,
        "melodia_trick": spec.melodia_trick,
    }


def missing_source_entry(spec: TranscriptionSpec, source_role: str) -> dict[str, Any]:
    """A retained `targets` index entry for a target whose source isn't
    available yet. Never deleted by a caller -- it is the record of a still-
    requested target waiting for its stem to arrive (see module docstring)."""
    return {
        "backend": spec.backend,
        "package_pin": spec.package_pin,
        "serialization": spec.serialization if isinstance(spec, BasicPitchSpec) else None,
        "source_role": source_role,
        "input_hash": None,
        "settings_hash": spec_hash(spec),
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
        "first_event_s": None,
        "last_event_s": None,
        "backend_tempo": None,
        "midi_tempo": spec.midi_tempo if isinstance(spec, BasicPitchSpec) else None,
        "confidence": None,
        "settings": _settings_dict(spec),
        "transcribed_at": None,
        "error": None,
    }


def transcribed_entry(
    spec: TranscriptionSpec,
    *,
    source_role: str,
    input_hash: str,
    target: str,
    result: TranscriptionResult,
    transcribed_at: str,
) -> dict[str, Any]:
    """A `targets` index entry recording a completed transcription."""
    return {
        "backend": spec.backend,
        "package_pin": spec.package_pin,
        "serialization": spec.serialization if isinstance(spec, BasicPitchSpec) else None,
        "source_role": source_role,
        "input_hash": input_hash,
        "settings_hash": spec_hash(spec),
        "status": "transcribed",
        "midi_file": midi_artifact_name(target),
        "notes_file": notes_artifact_name(target) if isinstance(spec, BasicPitchSpec) else None,
        "events_file": events_artifact_name(target) if isinstance(spec, DrumScriptSpec) else None,
        "note_count": result.note_count if isinstance(spec, BasicPitchSpec) else None,
        "event_count": result.event_count if isinstance(spec, DrumScriptSpec) else None,
        "instrument_counts": result.instrument_counts if isinstance(spec, DrumScriptSpec) else None,
        # GM percussion note numbers select kit instruments; they are not a
        # musical pitch range and must never be presented as one.
        "pitch_range_midi": list(result.pitch_range_midi) if isinstance(spec, BasicPitchSpec) and result.pitch_range_midi else None,
        "first_note_s": result.first_note_s if isinstance(spec, BasicPitchSpec) else None,
        "last_note_s": result.last_note_s if isinstance(spec, BasicPitchSpec) else None,
        "first_event_s": result.first_event_s if isinstance(spec, DrumScriptSpec) else None,
        "last_event_s": result.last_event_s if isinstance(spec, DrumScriptSpec) else None,
        "backend_tempo": result.backend_tempo if isinstance(spec, DrumScriptSpec) else None,
        "midi_tempo": result.midi_tempo if isinstance(spec, DrumScriptSpec) else spec.midi_tempo,
        # DrumScript does not expose calibrated confidence.  Keeping this
        # explicit prevents downstream consumers from mistaking velocity for
        # a confidence score.
        "confidence": None if isinstance(spec, DrumScriptSpec) else result.confidence,
        "settings": _settings_dict(spec),
        "transcribed_at": transcribed_at,
        "error": None,
    }


def error_entry(spec: TranscriptionSpec, *, source_role: str, input_hash: str | None, error: str) -> dict[str, Any]:
    """A retained `targets` index entry recording a backend failure for one
    target. Mirrors `missing_source_entry`'s retention: the remaining targets
    still run (see `analysis._refresh_target`), and a later run retries this
    target from scratch rather than leaving a dangling "in progress" state."""
    return {
        "backend": spec.backend,
        "package_pin": spec.package_pin,
        "serialization": spec.serialization if isinstance(spec, BasicPitchSpec) else None,
        "source_role": source_role,
        "input_hash": input_hash,
        "settings_hash": spec_hash(spec),
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
        "first_event_s": None,
        "last_event_s": None,
        "backend_tempo": None,
        "midi_tempo": spec.midi_tempo if isinstance(spec, BasicPitchSpec) else None,
        "confidence": None,
        "settings": _settings_dict(spec),
        "transcribed_at": None,
        "error": error,
    }


@dataclass
class TranscriptionResult:
    """What a `Transcriber` hands back after a successful transcription."""

    note_count: int
    pitch_range_midi: tuple[int, int] | None
    first_note_s: float | None
    last_note_s: float | None
    midi_path: Path
    notes_path: Path | None = None
    events_path: Path | None = None
    instrument_counts: dict[str, int] | None = None
    event_count: int | None = None
    first_event_s: float | None = None
    last_event_s: float | None = None
    backend_tempo: float | None = None
    midi_tempo: float | None = None
    confidence: float | None = None


class Transcriber(Protocol):
    """Thin backend seam. The orchestrator (a later issue) owns target
    resolution, caching, and artifact naming; a backend only transcribes one
    source and reports back what it produced."""

    name: str

    def transcribe(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult: ...


class TranscriberRouter(Protocol):
    """The sole target-to-backend selection seam.

    Production and tests both call this interface; analysis never decides a
    backend from a target name itself.
    """

    def for_target(self, target: str) -> Transcriber: ...

    def spec_for_target(
        self, target: str, *, midi_tempo: float | None, modes: Mapping[str, str] | None = None, time_signature: str | None = None
    ) -> TranscriptionSpec: ...


@dataclass(frozen=True)
class TargetTranscriberRouter:
    """Routes a configured set of targets to the drum backend.

    The production router sends `drums` to DrumScript and every other
    target to Basic Pitch. Tests can opt a fake into the drum route through
    this same seam, so the normal suite never imports either real model.
    """

    basic_pitch: Transcriber
    drumscript: Transcriber
    drumscript_targets: tuple[str, ...] = ()
    drumscript_package_pin: str = DRUMSCRIPT_PACKAGE_PIN
    drumscript_runtime_version: str = DRUMSCRIPT_RUNTIME_VERSION
    drumscript_classifier_mode: str = DRUMSCRIPT_CLASSIFIER_MODE
    drumscript_time_signature: tuple[int, int] | None = None

    def for_target(self, target: str) -> Transcriber:
        validate_target(target)
        return self.drumscript if target in self.drumscript_targets else self.basic_pitch

    def spec_for_target(
        self, target: str, *, midi_tempo: float | None, modes: Mapping[str, str] | None = None, time_signature: str | None = None
    ) -> TranscriptionSpec:
        backend = self.for_target(target).name
        if backend == "drumscript":
            return default_spec_for_target(
                target,
                backend=backend,
                package_pin=self.drumscript_package_pin,
                midi_tempo=midi_tempo,
                drumscript_runtime_version=self.drumscript_runtime_version,
                drumscript_classifier_mode=self.drumscript_classifier_mode,
                drumscript_time_signature=self.drumscript_time_signature,
            )
        return default_spec_for_target(
            target, backend=backend, midi_tempo=midi_tempo, modes=modes, time_signature=time_signature
        )


def production_transcriber_router() -> TranscriberRouter:
    """Current production route: DrumScript handles drums, Basic Pitch everything else."""
    basic_pitch = BasicPitchTranscriber()
    return TargetTranscriberRouter(
        basic_pitch=basic_pitch,
        drumscript=DrumScriptTranscriber(),
        drumscript_targets=("drums",),
    )


def _hz_to_midi(hz: float) -> int:
    return round(69 + 12 * math.log2(hz / 440.0))


def _content_seed(source: Path, spec: TranscriptionSpec, salt: str) -> int:
    """Derive a small deterministic value from `source`'s bytes, the spec,
    and `salt`, so `FakeTranscriber`'s fabricated notes are still
    content-addressed: the same input reliably reproduces the same notes
    (cache-hit tests), and different input or spec reliably changes them
    (cache-invalidation tests) -- mirroring `separation._content_seed`."""
    digest = hashlib.sha256()
    digest.update(source.read_bytes())
    digest.update(json.dumps(spec.to_dict(), sort_keys=True).encode("utf-8"))
    digest.update(salt.encode("utf-8"))
    return int.from_bytes(digest.digest()[:2], "big")


def _fake_notes(source: Path, spec: TranscriptionSpec, note_count: int = 4) -> list[tuple[float, float, int, int]]:
    """A short, deterministic note list: (start_s, end_s, pitch_midi, velocity)."""
    minimum_frequency_hz = spec.minimum_frequency_hz if isinstance(spec, BasicPitchSpec) else None
    maximum_frequency_hz = spec.maximum_frequency_hz if isinstance(spec, BasicPitchSpec) else None
    min_pitch = _hz_to_midi(minimum_frequency_hz or 82.4)  # standard-tuning low E as a fallback center
    max_pitch = _hz_to_midi(maximum_frequency_hz or 880.0)
    if max_pitch <= min_pitch:
        max_pitch = min_pitch + 24
    span = max_pitch - min_pitch

    notes: list[tuple[float, float, int, int]] = []
    t = 0.0
    for index in range(note_count):
        seed = _content_seed(source, spec, f"note-{index}")
        pitch = min_pitch + (seed % (span + 1))
        duration = 0.25 + (seed % 500) / 1000.0
        velocity = 40 + (seed % 60)
        notes.append((round(t, 6), round(t + duration, 6), pitch, velocity))
        t += duration + 0.1
    return notes


def _varlen(value: int) -> bytes:
    """MIDI variable-length quantity encoding."""
    chunks = [value & 0x7F]
    value >>= 7
    while value:
        chunks.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(chunks))


def _write_midi(path: Path, notes: list[tuple[float, float, int, int]], tempo_bpm: float, *, channel: int = 0) -> None:
    """Write a minimal, valid single-track Standard MIDI File (format 0)
    containing `notes`, with no external MIDI library (none is a vgt
    dependency, and this issue must add none)."""
    ticks_per_beat = 480
    ticks_per_second = ticks_per_beat * tempo_bpm / 60.0
    tempo_uspb = int(round(60_000_000 / tempo_bpm)) if tempo_bpm else 500_000

    raw_events: list[tuple[int, bytes]] = []
    for start_s, end_s, pitch, velocity in notes:
        start_tick = int(round(start_s * ticks_per_second))
        end_tick = max(start_tick + 1, int(round(end_s * ticks_per_second)))
        raw_events.append((start_tick, bytes([0x90 | channel, pitch & 0x7F, velocity & 0x7F])))
        raw_events.append((end_tick, bytes([0x80 | channel, pitch & 0x7F, 0])))
    raw_events.sort(key=lambda item: item[0])

    track = bytearray()
    track += _varlen(0) + bytes([0xFF, 0x51, 0x03]) + tempo_uspb.to_bytes(3, "big")
    previous_tick = 0
    for tick, data in raw_events:
        track += _varlen(max(0, tick - previous_tick)) + data
        previous_tick = tick
    track += _varlen(0) + bytes([0xFF, 0x2F, 0x00])  # end of track

    header = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") + ticks_per_beat.to_bytes(2, "big")
    track_chunk = b"MTrk" + len(track).to_bytes(4, "big") + bytes(track)
    path.write_bytes(header + track_chunk)


def _write_notes_csv(path: Path, notes: list[tuple[float, float, int, int]], source: Path, spec: TranscriptionSpec) -> None:
    """Write the note-events CSV. The real Basic Pitch header is
    `start_time_s,end_time_s,pitch_midi,velocity,pitch_bend`, with
    `pitch_bend` a variable-length trailing sequence -- rows can have
    differing column counts, and this fake deliberately varies row length
    too, so a strict-CSV-reader regression would be caught here rather than
    only against real Basic Pitch output."""
    lines = ["start_time_s,end_time_s,pitch_midi,velocity,pitch_bend"]
    for index, (start_s, end_s, pitch, velocity) in enumerate(notes):
        bend_count = _content_seed(source, spec, f"bend-count-{index}") % 3
        bend_values = [str(_content_seed(source, spec, f"bend-{index}-{bend}") % 200 - 100) for bend in range(bend_count)]
        lines.append(",".join([f"{start_s:.6f}", f"{end_s:.6f}", str(pitch), str(velocity), *bend_values]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class FakeTranscriber:
    """Writes a small deterministic, valid MIDI file and matching CSV instead
    of invoking Basic Pitch, so the offline suite (and anything exercising
    the transcription seam end to end) never runs a model."""

    name = "fake"

    def transcribe(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        emit = progress or (lambda _message: None)
        emit(f"transcribing (fake): {source.name}")
        destination_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(spec, DrumScriptSpec):
            instruments = tuple(DRUMSCRIPT_INSTRUMENTS)
            event_count = 4
            events = [
                {"time_sec": round(index * 0.5, 6), "instruments": [instruments[_content_seed(source, spec, f"event-{index}") % len(instruments)]]}
                for index in range(event_count)
            ]
            drum_notes = [
                (event["time_sec"], event["time_sec"] + 0.1, DRUMSCRIPT_INSTRUMENTS[event["instruments"][0]], 100)
                for event in events
            ]
            midi_path = destination_dir / "transcription.mid"
            events_path = destination_dir / "transcription.json"
            _write_midi(midi_path, drum_notes, 120.0, channel=9)
            events_path.write_text(json.dumps(events), encoding="utf-8")
            counts = {name: sum(name in event["instruments"] for event in events) for name in instruments}
            counts = {name: count for name, count in counts.items() if count}
            return TranscriptionResult(
                note_count=len(events), pitch_range_midi=None, first_note_s=None, last_note_s=None,
                midi_path=midi_path, events_path=events_path, instrument_counts=counts,
                event_count=len(events), first_event_s=events[0]["time_sec"] if events else None,
                last_event_s=events[-1]["time_sec"] if events else None, midi_tempo=120.0,
            )

        notes = _fake_notes(source, spec)
        midi_path = destination_dir / "transcription.mid"
        notes_path = destination_dir / "transcription.csv"
        tempo_bpm = spec.midi_tempo if isinstance(spec, BasicPitchSpec) else None
        tempo_bpm = tempo_bpm or 120.0
        _write_midi(midi_path, notes, tempo_bpm)
        _write_notes_csv(notes_path, notes, source, spec)

        pitches = [pitch for _start, _end, pitch, _velocity in notes]
        return TranscriptionResult(
            note_count=len(notes),
            pitch_range_midi=(min(pitches), max(pitches)) if pitches else None,
            first_note_s=notes[0][0] if notes else None,
            last_note_s=max(end for _start, end, _pitch, _velocity in notes) if notes else None,
            midi_path=midi_path,
            notes_path=notes_path,
        )


def _basic_pitch_base_command(package_pin: str) -> list[str]:
    """The invocation prefix, before `<destination_dir> <source>` and the
    spec-derived flags. Honours `VGT_BASIC_PITCH_CMD` as a full override for
    a pre-installed binary (see the env var's docstring above); otherwise a
    pinned, isolated `uvx` invocation -- Basic Pitch never becomes a vgt
    dependency."""
    override = os.environ.get(BASIC_PITCH_CMD_ENV)
    if override:
        try:
            parts = shlex.split(override)
        except ValueError as exc:
            raise TranscriptionError(f"{BASIC_PITCH_CMD_ENV} is not a valid shell command: {override!r} ({exc})") from exc
        if parts:
            return parts
    return [
        "uvx",
        "--python", "3.11",
        "--with", "setuptools<81",
        "--from", package_pin,
        "basic-pitch",
    ]


def build_basic_pitch_argv(source: Path, destination_dir: Path, spec: BasicPitchSpec) -> list[str]:
    """The full `basic-pitch` command line for one target. Pure and
    side-effect-free so tests can assert on it without running `uvx` or a
    model (per the issue's acceptance criteria)."""
    if spec.backend != "basic-pitch":
        raise TranscriptionError(f"BasicPitchTranscriber cannot build argv for backend {spec.backend!r}")
    argv = [
        *_basic_pitch_base_command(spec.package_pin),
        str(destination_dir),
        str(source),
        # Forced explicitly: the onnx extra still installs coremltools on
        # macOS, and Basic Pitch's default tf -> coreml -> tflite -> onnx
        # preference order would otherwise silently pick CoreML -- a
        # different runtime behind the same command line.
        "--model-serialization", spec.serialization,
        "--save-midi",
        "--save-note-events",
    ]
    if spec.midi_tempo is not None:
        argv += ["--midi-tempo", str(spec.midi_tempo)]
    argv += ["--minimum-note-length", str(spec.minimum_note_length_ms)]
    if spec.minimum_frequency_hz is not None:
        argv += ["--minimum-frequency", str(spec.minimum_frequency_hz)]
    if spec.maximum_frequency_hz is not None:
        argv += ["--maximum-frequency", str(spec.maximum_frequency_hz)]
    argv += ["--onset-threshold", str(spec.onset_threshold)]
    argv += ["--frame-threshold", str(spec.frame_threshold)]
    if not spec.melodia_trick:
        argv.append("--no-melodia")
    if spec.multiple_pitch_bends:
        argv.append("--multiple-pitch-bends")
    return argv


def _stderr_tail(stderr: str | None, limit: int = 4000) -> str:
    return (stderr or "")[-limit:]


def _clear_stale_outputs(destination_dir: Path) -> None:
    """Remove any `.mid`/`.csv` already in `destination_dir` before invoking
    `uvx`, so a retry against a reused directory (e.g. the orchestrator
    re-running a target after a prior failure) starts from a clean slate --
    otherwise a leftover renamed `transcription.mid`/`.csv` from an earlier
    call would corrupt `_collect_and_rename_outputs`'s exactly-one-file
    check, or worse, get silently mistaken for the current run's output."""
    for pattern in ("*.mid", "*.csv"):
        for stale in destination_dir.glob(pattern):
            stale.unlink()


def _collect_and_rename_outputs(destination_dir: Path) -> tuple[Path, Path]:
    """Basic Pitch names its outputs after the input file (e.g.
    `guitar_basic_pitch.mid`), which would collide across targets sharing a
    destination dir. Rename whatever it produced to the same stable
    `transcription.mid` / `transcription.csv` names `FakeTranscriber` uses,
    so the two backends are interchangeable from the orchestrator's side;
    final per-target artifact naming (`transcription/<target>.mid`) is the
    orchestrator's job, not this backend's."""
    midi_candidates = sorted(destination_dir.glob("*.mid"))
    notes_candidates = sorted(destination_dir.glob("*.csv"))
    if len(midi_candidates) != 1 or len(notes_candidates) != 1:
        raise TranscriptionError(
            f"basic-pitch produced {len(midi_candidates)} MIDI file(s) and "
            f"{len(notes_candidates)} CSV file(s) in {destination_dir}; expected exactly one of each"
        )
    midi_path = destination_dir / "transcription.mid"
    notes_path = destination_dir / "transcription.csv"
    if midi_candidates[0] != midi_path:
        midi_candidates[0].replace(midi_path)
    if notes_candidates[0] != notes_path:
        notes_candidates[0].replace(notes_path)
    return midi_path, notes_path


def _validate_basic_pitch_midi(path: Path) -> None:
    """Raise `TranscriptionError` unless `path` is a non-empty, readable
    Standard MIDI File -- never trust a backend's raw output as a valid
    artifact (mirrors `separation._validate_wav`)."""
    try:
        header = path.read_bytes()[:8]
    except OSError as exc:
        raise TranscriptionError(f"{path}: MIDI file is not readable: {exc}") from exc
    if len(header) < 8 or header[:4] != b"MThd":
        raise TranscriptionError(f"{path}: not a valid MIDI file")


@dataclass(frozen=True)
class ParsedNote:
    """One row of Basic Pitch's note-events CSV, leniently parsed (see
    `parse_notes_csv`)."""

    start_s: float
    end_s: float
    pitch_midi: int
    velocity: int
    pitch_bend: tuple[float, ...]


def parse_notes_csv(path: Path) -> list[ParsedNote]:
    """Parse Basic Pitch's note-events CSV.

    The header is `start_time_s,end_time_s,pitch_midi,velocity,pitch_bend`,
    but `pitch_bend` is a variable-length trailing sequence of values, so
    rows have differing column counts. This must never be handed to a strict
    CSV reader (e.g. `csv.DictReader`) -- only the first four fields are
    fixed-position; everything after them is that row's bend series.
    """
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise TranscriptionError(f"{path}: empty notes CSV")
    notes: list[ParsedNote] = []
    for line in lines[1:]:
        fields = line.split(",")
        if len(fields) < 4:
            raise TranscriptionError(f"{path}: malformed note row {line!r}")
        bend_fields = fields[4:]
        if bend_fields and bend_fields[-1] == "":
            bend_fields = bend_fields[:-1]  # a trailing comma, not a value
        try:
            start_s = float(fields[0])
            end_s = float(fields[1])
            pitch_midi = int(float(fields[2]))
            velocity = int(float(fields[3]))
            pitch_bend = tuple(float(value) for value in bend_fields)
        except ValueError as exc:
            raise TranscriptionError(f"{path}: malformed note row {line!r}: {exc}") from exc
        notes.append(ParsedNote(start_s, end_s, pitch_midi, velocity, pitch_bend))
    return notes


def _summarize_notes(notes: list[ParsedNote]) -> tuple[int, tuple[int, int] | None, float | None, float | None]:
    if not notes:
        return 0, None, None, None
    pitches = [note.pitch_midi for note in notes]
    return (
        len(notes),
        (min(pitches), max(pitches)),
        min(note.start_s for note in notes),
        max(note.end_s for note in notes),
    )


def _merge_fragments(notes: list[ParsedNote], max_gap_s: float) -> list[ParsedNote]:
    """Rejoin a held note the model split in place.

    Two notes of the same pitch separated by no more than `max_gap_s` become
    one note spanning both (see `GUITAR_FRAGMENT_MERGE_GAP_S` for why the
    threshold is this small and why it isn't tempo-scaled). Chains of three or
    more fragments collapse in a single pass, since each merge extends the
    span the next candidate is compared against.

    The merged note keeps the *loudest* fragment's velocity, not the first's:
    a split note's later fragment can carry the true peak when the model's
    confidence rose mid-note, and it keeps a reassembled note from being
    spuriously retired by the voice cap for looking quiet.
    """
    by_pitch: dict[int, list[ParsedNote]] = {}
    for note in notes:
        by_pitch.setdefault(note.pitch_midi, []).append(note)

    merged: list[ParsedNote] = []
    for pitch_notes in by_pitch.values():
        pitch_notes.sort(key=lambda note: note.start_s)
        current = pitch_notes[0]
        for candidate in pitch_notes[1:]:
            if candidate.start_s - current.end_s <= max_gap_s:
                current = replace(
                    current,
                    end_s=max(current.end_s, candidate.end_s),
                    velocity=max(current.velocity, candidate.velocity),
                    pitch_bend=current.pitch_bend + candidate.pitch_bend,
                )
            else:
                merged.append(current)
                current = candidate
        merged.append(current)
    return sorted(merged, key=lambda note: (note.start_s, note.pitch_midi))


def _drop_isolated_notes(notes: list[ParsedNote], max_duration_s: float, neighbour_window_s: float) -> list[ParsedNote]:
    """Drop a short note that has no same-pitch neighbour anywhere near it.

    A real short note on a guitar almost always sits in a run, a repeated
    figure, or beside its own re-articulation; one with nothing at that pitch
    within `neighbour_window_s` on either side is far more likely a transient
    the model latched onto. Deliberately conservative on both bounds -- this
    is for obvious blips, not for thinning a busy part.

    Must run *after* `_merge_fragments`: a fragment of a real held note looks
    exactly like an isolated blip until its siblings have been rejoined to it.
    """
    by_pitch: dict[int, list[ParsedNote]] = {}
    for note in notes:
        by_pitch.setdefault(note.pitch_midi, []).append(note)

    kept: list[ParsedNote] = []
    for note in notes:
        if note.end_s - note.start_s > max_duration_s:
            kept.append(note)
            continue
        has_neighbour = any(
            other is not note
            and other.start_s < note.end_s + neighbour_window_s
            and other.end_s > note.start_s - neighbour_window_s
            for other in by_pitch[note.pitch_midi]
        )
        if has_neighbour:
            kept.append(note)
    return kept


def _clamp_sustain(notes: list[ParsedNote], max_duration_s: float) -> list[ParsedNote]:
    """Cap every note's duration at `max_duration_s`, leaving its onset
    untouched. Fixes the acoustic ring-out runaway (see
    `GUITAR_ACOUSTIC_FRAME_THRESHOLD`'s docstring): a note that never released
    reports as a normal-length note instead of a multi-minute drone."""
    return [
        note if note.end_s - note.start_s <= max_duration_s else replace(note, end_s=note.start_s + max_duration_s)
        for note in notes
    ]


def _drop_harmonic_ghosts(
    notes: list[ParsedNote],
    intervals: tuple[int, ...],
    onset_tolerance_s: float,
    overlap_fraction: float,
    velocity_slack: float,
) -> list[ParsedNote]:
    """Drop a note that is almost certainly the acoustic partial of a louder,
    lower note already sounding underneath it.

    A note is dropped only when another note sits a harmonic interval
    (`intervals`) below it, started at essentially the same instant, covers
    most of its duration, and isn't quieter -- a real independent note at a
    harmonic interval (e.g. an intentional octave) will usually fail at least
    one of these and survive.
    """
    kept: list[ParsedNote] = []
    for note in notes:
        is_ghost = False
        for other in notes:
            if other is note or other.pitch_midi >= note.pitch_midi:
                continue
            if (note.pitch_midi - other.pitch_midi) not in intervals:
                continue
            if not (other.start_s - onset_tolerance_s <= note.start_s <= other.end_s):
                continue
            overlap = min(note.end_s, other.end_s) - max(note.start_s, other.start_s)
            if overlap > overlap_fraction * (note.end_s - note.start_s) and other.velocity >= note.velocity - velocity_slack:
                is_ghost = True
                break
        if not is_ghost:
            kept.append(note)
    return kept


def _cap_simultaneous_voices(
    notes: list[ParsedNote], max_voices: int, min_duration_after_cap_s: float
) -> list[ParsedNote]:
    """Never let more than `max_voices` notes sound at once -- a guitar has
    only that many strings, so anything above it is a detection artifact, not
    a voicing a learner could play. Retires the quietest currently-sounding
    voice early (truncating it, not deleting it) rather than dropping a note
    outright, so its onset still appears on the reference track."""
    order = sorted(range(len(notes)), key=lambda index: notes[index].start_s)
    ends = [note.end_s for note in notes]
    active: list[int] = []
    for index in order:
        start = notes[index].start_s
        active = [held for held in active if ends[held] > start]
        if len(active) >= max_voices:
            victim = min(active, key=lambda held: (notes[held].velocity, -notes[held].start_s))
            ends[victim] = start
            active.remove(victim)
        active.append(index)
    capped = [replace(note, end_s=ends[index]) for index, note in enumerate(notes)]
    return [note for note in capped if note.end_s - note.start_s > min_duration_after_cap_s]


_CLEANUP_STAGE_FUNCTIONS: dict[str, Callable[..., list[ParsedNote]]] = {
    "merge_fragments": _merge_fragments,
    "drop_isolated_notes": _drop_isolated_notes,
    "clamp_sustain": _clamp_sustain,
    "drop_harmonic_ghosts": _drop_harmonic_ghosts,
    "cap_simultaneous_voices": _cap_simultaneous_voices,
}


def _apply_cleanup_stages(notes: list[ParsedNote], spec: BasicPitchSpec) -> list[ParsedNote]:
    """Run `spec.cleanup`'s ordered stages, dispatching each by its tag name.

    Stage order and each stage's parameters are a property of the profile
    that built `spec.cleanup` (see `_GUITAR_ACOUSTIC_PROFILE`'s docstring for
    why the guitar pipeline's order is load-bearing) -- this executor only
    walks the list `default_spec_for_target` already resolved.
    """
    for stage in spec.cleanup:
        notes = _CLEANUP_STAGE_FUNCTIONS[stage.name](notes, **stage.params)
    return notes


def _write_parsed_notes_csv(path: Path, notes: list[ParsedNote]) -> None:
    """Rewrite the note-events CSV after guitar cleanup, preserving each
    surviving note's real pitch-bend series (unlike `_write_notes_csv`, used
    only by `FakeTranscriber`, which fabricates one)."""
    lines = ["start_time_s,end_time_s,pitch_midi,velocity,pitch_bend"]
    for note in notes:
        bend = [str(value) for value in note.pitch_bend]
        lines.append(",".join([f"{note.start_s:.6f}", f"{note.end_s:.6f}", str(note.pitch_midi), str(note.velocity), *bend]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class BasicPitchTranscriber:
    """Runs Basic Pitch as a pinned, isolated `uvx` subprocess -- see the
    module docstring for why it cannot be a vgt dependency. Degradation is
    per target: a missing `uvx`, a non-zero exit, or malformed output all
    raise `TranscriptionError` carrying the captured stderr tail, so the
    orchestrator (T-C) can mark just that target `error` and continue with
    the others."""

    name = "basic-pitch"

    def transcribe(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        emit = progress or (lambda _message: None)
        if not isinstance(spec, BasicPitchSpec):
            raise TranscriptionError("BasicPitchTranscriber requires a BasicPitchSpec")
        argv = build_basic_pitch_argv(source, destination_dir, spec)

        if shutil.which(argv[0]) is None:
            raise TranscriptionError(
                f"{argv[0]!r} is not on PATH; install uv (for uvx) or set {BASIC_PITCH_CMD_ENV} to a "
                "prebuilt basic-pitch binary (uv tool install --python 3.11 --with \"setuptools<81\" "
                f'"{spec.package_pin}")'
            )

        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            _clear_stale_outputs(destination_dir)
        except OSError as exc:
            raise TranscriptionError(f"could not prepare {destination_dir}: {exc}") from exc

        emit(f"transcribing (basic-pitch): {source.name}")
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=600, errors="replace"
            )
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionError(f"basic-pitch timed out after {exc.timeout}s") from exc
        except OSError as exc:
            raise TranscriptionError(f"failed to run {argv[0]!r}: {exc}") from exc

        if completed.returncode != 0:
            raise TranscriptionError(
                f"basic-pitch exited with status {completed.returncode}: {_stderr_tail(completed.stderr)}"
            )

        midi_path, notes_path = _collect_and_rename_outputs(destination_dir)
        _validate_basic_pitch_midi(midi_path)
        notes = parse_notes_csv(notes_path)

        if spec.cleanup:
            cleaned = _apply_cleanup_stages(notes, spec)
            if cleaned != notes:
                _write_parsed_notes_csv(notes_path, cleaned)
                midi_notes = [(note.start_s, note.end_s, note.pitch_midi, note.velocity) for note in cleaned]
                _write_midi(midi_path, midi_notes, spec.midi_tempo or 120.0)
                notes = cleaned

        note_count, pitch_range_midi, first_note_s, last_note_s = _summarize_notes(notes)
        emit(f"transcribed (basic-pitch): {note_count} notes")

        return TranscriptionResult(
            note_count=note_count,
            pitch_range_midi=pitch_range_midi,
            first_note_s=first_note_s,
            last_note_s=last_note_s,
            midi_path=midi_path,
            notes_path=notes_path,
        )


class DrumScriptTranscriber:
    """Pinned, isolated DrumScript backend.

    DrumScript is intentionally only ever a subprocess.  Its output is made
    in a fresh directory outside vgt's namespace, validated there, then copied
    to stable backend-local names.  This keeps failed or extra files (notably
    its PDF report) out of the sidecar artifact namespace.
    """

    name = "drumscript"

    def transcribe(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        emit = progress or (lambda _message: None)
        if not isinstance(spec, DrumScriptSpec):
            raise TranscriptionError("DrumScriptTranscriber requires a DrumScriptSpec")
        source = source.resolve()
        if not source.is_file():
            raise TranscriptionError("drum stem is not a readable file")
        argv_prefix = _drumscript_base_command(spec)
        if shutil.which(argv_prefix[0]) is None:
            raise TranscriptionError(
                f"{argv_prefix[0]!r} is not on PATH; install uv (for uvx) or set "
                f"{DRUMSCRIPT_CMD_ENV} to a prebuilt drumscript command"
            )

        # DrumScript accepts the separated stem as its positional input.  Do
        # not add --full-song: that would re-run Demucs on a source vgt already
        # obtained from LALAL.
        argv = build_drumscript_argv(source, spec)
        emit(f"transcribing (drumscript): {source.name}")
        with tempfile.TemporaryDirectory(prefix="vgt-drumscript-") as temporary:
            work_dir = Path(temporary)
            # DrumScript 0.1.6's score builder writes relative ``outputs/``
            # artifacts but does not create that directory itself.
            (work_dir / "outputs").mkdir()
            try:
                completed = subprocess.run(
                    argv, cwd=work_dir, capture_output=True, text=True, timeout=600, errors="replace"
                )
            except subprocess.TimeoutExpired as exc:
                raise TranscriptionError(f"drumscript timed out after {exc.timeout}s") from exc
            except OSError as exc:
                raise TranscriptionError(f"failed to run {argv_prefix[0]!r}: {exc}") from exc
            if completed.returncode != 0:
                raise TranscriptionError(
                    f"drumscript exited with status {completed.returncode}: "
                    f"{_drumscript_process_context(completed, work_dir)}"
                )

            try:
                midi_source, events_source = _collect_drumscript_outputs(work_dir)
                _validate_drumscript_midi(midi_source)
                events = parse_drumscript_events(events_source)
                _validate_event_times(events, _source_duration_seconds(source))
                destination_dir.mkdir(parents=True, exist_ok=True)
                midi_path = destination_dir / "transcription.mid"
                events_path = destination_dir / "transcription.json"
                shutil.copy2(midi_source, midi_path)
                shutil.copy2(events_source, events_path)
            except (OSError, TranscriptionError) as exc:
                # The work directory is intentionally not useful to callers:
                # it is deleted at scope exit and must never enter the sidecar.
                raise TranscriptionError(_without_temporary_path(str(exc), work_dir)) from exc

        counts: dict[str, int] = {}
        for event in events:
            for instrument in event["instruments"]:
                counts[instrument] = counts.get(instrument, 0) + 1
        times = [event["time_sec"] for event in events]
        emit(f"transcribed (drumscript): {len(events)} events")
        return TranscriptionResult(
            note_count=len(events),
            pitch_range_midi=None,
            first_note_s=None,
            last_note_s=None,
            midi_path=midi_path,
            events_path=events_path,
            instrument_counts=counts,
            event_count=len(events),
            first_event_s=min(times) if times else None,
            last_event_s=max(times) if times else None,
            backend_tempo=_drumscript_backend_tempo(completed),
            midi_tempo=_midi_tempo_bpm(midi_path),
        )


def _drumscript_base_command(spec: DrumScriptSpec) -> list[str]:
    override = os.environ.get(DRUMSCRIPT_CMD_ENV)
    if override:
        try:
            parts = shlex.split(override)
        except ValueError as exc:
            raise TranscriptionError(f"{DRUMSCRIPT_CMD_ENV} is not a valid shell command: {exc}") from exc
        if parts:
            return parts
    # v0.1.6's console-script entry point calls ``main`` directly and drops
    # its required input argument.  Module execution retains its argparse
    # wrapper, still inside the pinned uvx environment.
    return [
        "uvx", "--python", spec.runtime_version.removeprefix("python=="), "--from", spec.package_pin,
        "python", "-m", "drumscript.main",
    ]


def build_drumscript_argv(source: Path, spec: DrumScriptSpec) -> list[str]:
    """Pure command construction for the isolated, already-separated stem."""
    if spec.backend != "drumscript":
        raise TranscriptionError(f"DrumScriptTranscriber cannot build argv for backend {spec.backend!r}")
    return [*_drumscript_base_command(spec), str(source.resolve())]


def _without_temporary_path(message: str, work_dir: Path) -> str:
    return message.replace(str(work_dir), "<temporary output>")


def _drumscript_process_context(completed: Any, work_dir: Path) -> str:
    context = " | ".join(part for part in (_stderr_tail(completed.stderr), _stderr_tail(completed.stdout)) if part)
    return _without_temporary_path(context or "no output captured", work_dir)


def _contained_files(work_dir: Path, suffixes: tuple[str, ...]) -> list[Path]:
    root = work_dir.resolve()
    candidates: list[Path] = []
    for path in work_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes:
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise TranscriptionError("drumscript produced an artifact outside its temporary output") from exc
            candidates.append(path)
    return sorted(candidates)


def _collect_drumscript_outputs(work_dir: Path) -> tuple[Path, Path]:
    midi = _contained_files(work_dir, (".mid", ".midi"))
    events = _contained_files(work_dir, (".json",))
    if len(midi) != 1 or len(events) != 1:
        raise TranscriptionError(
            f"drumscript produced {len(midi)} MIDI file(s) and {len(events)} event JSON file(s); expected exactly one of each"
        )
    return midi[0], events[0]


def _validate_drumscript_midi(path: Path) -> None:
    _validate_basic_pitch_midi(path)
    # Empty drum transcriptions have no channel events; otherwise require the
    # GM percussion channel (MIDI channel 10, encoded as low nibble 9).
    if _midi_has_non_percussion_notes(path.read_bytes()):
        raise TranscriptionError("drumscript MIDI contains notes outside GM percussion channel 10")


def _midi_tempo_bpm(path: Path) -> float | None:
    """Return the first SMF tempo meta event, when the exporter wrote one.

    DrumScript MIDI is authoritative for its playback tempo, while no
    confidence can be inferred from the fixed note velocity it writes.
    """
    data = path.read_bytes()
    marker = b"\xff\x51\x03"
    index = data.find(marker)
    if index < 0 or index + 6 > len(data):
        return None
    microseconds_per_beat = int.from_bytes(data[index + 3:index + 6], "big")
    return 60_000_000 / microseconds_per_beat if microseconds_per_beat else None


_DRUMSCRIPT_TEMPO = re.compile(
    r"\b(?:detected\s+)?tempo\s*[:=]\s*(\d+(?:\.\d+)?)\s*(?:bpm)?\b", re.IGNORECASE
)


def _drumscript_backend_tempo(completed: Any) -> float | None:
    """Read a tempo explicitly reported by DrumScript, without estimating one.

    The v0.1.6 command's machine-readable event artifact is an array, so its
    optional tempo diagnostic is emitted in process output rather than added
    to that artifact.  Keep this deliberately conservative: an unrelated
    number in progress output is never treated as a BPM value.
    """
    output = "\n".join(
        value for value in (getattr(completed, "stdout", None), getattr(completed, "stderr", None)) if isinstance(value, str)
    )
    match = _DRUMSCRIPT_TEMPO.search(output)
    if not match:
        return None
    tempo = float(match.group(1))
    return tempo if math.isfinite(tempo) and tempo > 0 else None


def _read_varlen(data: bytes, index: int) -> tuple[int, int]:
    value = 0
    while True:
        if index >= len(data):
            raise TranscriptionError("drumscript MIDI is truncated")
        byte = data[index]
        index += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, index


def _midi_has_non_percussion_notes(data: bytes) -> bool:
    """Read enough SMF structure to distinguish status bytes from note data.

    Searching raw bytes is incorrect because a velocity or a meta payload can
    happen to equal a MIDI status value.
    """
    header_length = int.from_bytes(data[4:8], "big")
    index = 8 + header_length
    while index + 8 <= len(data):
        if data[index:index + 4] != b"MTrk":
            raise TranscriptionError("drumscript MIDI has an invalid track chunk")
        length = int.from_bytes(data[index + 4:index + 8], "big")
        track_end = index + 8 + length
        if track_end > len(data):
            raise TranscriptionError("drumscript MIDI is truncated")
        index += 8
        running_status: int | None = None
        while index < track_end:
            _delta, index = _read_varlen(data, index)
            if index >= track_end:
                raise TranscriptionError("drumscript MIDI is truncated")
            status = data[index]
            if status < 0x80:
                if running_status is None:
                    raise TranscriptionError("drumscript MIDI has invalid running status")
                status = running_status
            else:
                index += 1
            if status == 0xFF:
                if index >= track_end:
                    raise TranscriptionError("drumscript MIDI is truncated")
                index += 1  # meta type
                payload_length, index = _read_varlen(data, index)
                index += payload_length
                running_status = None
            elif status in (0xF0, 0xF7):
                payload_length, index = _read_varlen(data, index)
                index += payload_length
                running_status = None
            elif 0x80 <= status <= 0xEF:
                running_status = status
                data_length = 1 if (status & 0xE0) in (0xC0, 0xD0) else 2
                if status & 0xF0 in (0x80, 0x90) and (status & 0x0F) != 9:
                    return True
                index += data_length
            else:
                raise TranscriptionError("drumscript MIDI has an unsupported system event")
            if index > track_end:
                raise TranscriptionError("drumscript MIDI is truncated")
        index = track_end
    return False


def parse_drumscript_events(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranscriptionError("drumscript event JSON is unreadable or malformed") from exc
    if not isinstance(raw, list):
        raise TranscriptionError("drumscript event JSON must be an array")
    events: list[dict[str, Any]] = []
    for event in raw:
        if not isinstance(event, dict):
            raise TranscriptionError("drumscript event JSON contains a non-object event")
        time_sec = event.get("time_sec")
        instruments = event.get("instruments")
        if not isinstance(time_sec, (int, float)) or isinstance(time_sec, bool) or not math.isfinite(time_sec) or time_sec < 0:
            raise TranscriptionError("drumscript event has an invalid time_sec")
        if not isinstance(instruments, list) or not instruments or not all(isinstance(item, str) for item in instruments):
            raise TranscriptionError("drumscript event has an empty or invalid instruments list")
        unsupported = sorted(set(instruments).difference(DRUMSCRIPT_INSTRUMENTS))
        if unsupported:
            raise TranscriptionError(f"drumscript event has unsupported instruments: {', '.join(unsupported)}")
        events.append({"time_sec": float(time_sec), "instruments": instruments})
    return events


def _source_duration_seconds(source: Path) -> float:
    try:
        import soundfile
        duration = float(soundfile.info(source).duration)
    except Exception as exc:
        raise TranscriptionError("could not determine drum stem duration") from exc
    if not math.isfinite(duration) or duration < 0:
        raise TranscriptionError("drum stem has an invalid duration")
    return duration


def _validate_event_times(events: list[dict[str, Any]], duration: float) -> None:
    # A small export tail is harmless; a materially longer sequence is almost
    # certainly a unit/path mistake.  This remains deliberately independent
    # of the subprocess exit status.
    limit = duration + max(5.0, duration * 0.1)
    if any(event["time_sec"] > limit for event in events):
        raise TranscriptionError("drumscript event time is implausibly beyond the source duration")
