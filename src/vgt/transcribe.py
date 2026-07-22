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

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
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

# Per-target frequency narrowing. Targets absent here (polyphonic/unpredictable
# sources) get Basic Pitch's own full-range defaults (`None` = unbounded).
_TARGET_FREQUENCY_HZ: dict[str, tuple[float, float]] = {
    "guitar": (70.0, 1400.0),  # below drop/Eb-tuned E2 (82.4 Hz), above 24th-fret E6 (1318.5 Hz)
    "bass": (30.0, 400.0),  # 5-string low B is 30.9 Hz
    "vocals": (70.0, 1200.0),  # bass voice to whistle-adjacent soprano
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def default_spec_for_target(
    target: str,
    *,
    backend: str = "basic-pitch",
    package_pin: str = BASIC_PITCH_PACKAGE_PIN,
    serialization: str = BASIC_PITCH_SERIALIZATION,
    midi_tempo: float | None = None,
    drumscript_runtime_version: str = DRUMSCRIPT_RUNTIME_VERSION,
    drumscript_classifier_mode: str = DRUMSCRIPT_CLASSIFIER_MODE,
    drumscript_time_signature: tuple[int, int] | None = None,
) -> TranscriptionSpec:
    """The per-target default spec (see the frequency table above)."""
    validate_target(target)
    if backend == "drumscript":
        return DrumScriptSpec(
            backend=backend,
            package_pin=package_pin if package_pin != BASIC_PITCH_PACKAGE_PIN else DRUMSCRIPT_PACKAGE_PIN,
            runtime_version=drumscript_runtime_version,
            classifier_mode=drumscript_classifier_mode,
            time_signature=drumscript_time_signature,
        )
    minimum_frequency_hz, maximum_frequency_hz = _TARGET_FREQUENCY_HZ.get(target, (None, None))
    return BasicPitchSpec(
        backend=backend,
        package_pin=package_pin,
        serialization=serialization,
        onset_threshold=DEFAULT_ONSET_THRESHOLD,
        frame_threshold=DEFAULT_FRAME_THRESHOLD,
        minimum_note_length_ms=DEFAULT_MINIMUM_NOTE_LENGTH_MS,
        minimum_frequency_hz=minimum_frequency_hz,
        maximum_frequency_hz=maximum_frequency_hz,
        multiple_pitch_bends=DEFAULT_MULTIPLE_PITCH_BENDS,
        melodia_trick=DEFAULT_MELODIA_TRICK,
        midi_tempo=midi_tempo,
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

    def spec_for_target(self, target: str, *, midi_tempo: float | None) -> TranscriptionSpec: ...


@dataclass(frozen=True)
class TargetTranscriberRouter:
    """Routes a configured set of targets to the drum backend.

    The production router keeps the existing Basic Pitch route until the
    separately evaluated D-F rollout. Tests can opt a fake into the drum
    route through this same seam, so the normal suite never imports either
    real model.
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

    def spec_for_target(self, target: str, *, midi_tempo: float | None) -> TranscriptionSpec:
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
        return default_spec_for_target(target, backend=backend, midi_tempo=midi_tempo)


def production_transcriber_router() -> TranscriberRouter:
    """Current production route; D-F owns the evaluated DrumScript rollout."""
    basic_pitch = BasicPitchTranscriber()
    return TargetTranscriberRouter(basic_pitch=basic_pitch, drumscript=DrumScriptTranscriber())


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
