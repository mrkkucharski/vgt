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
from typing import Any, Callable, Mapping, Protocol, Sequence
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile

from .drum_cleanup import (
    BeatGridReference,
    DRUM_CLEANUP_PROFILES,
    DRUM_CLEANUP_PROFILE_NAMES,
    AudioOnsetEvidenceSource,
    NullOnsetEvidenceSource,
    apply_drum_cleanup,
    cleaned_events_to_json,
    cleaned_events_to_midi_notes,
)
from .drum_grid import reconcile_event_times
from .pyin_notes import PYIN_ALGORITHM_VERSION

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
ADTOF_PACKAGE_PIN = "adtof-pytorch @ git+https://github.com/xavriley/ADTOF-pytorch.git@85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9"
ADTOF_PACKAGE_VERSION = "0.1.0"
ADTOF_MODEL_VERSION = "Frame_RNN"
ADTOF_WEIGHTS_VERSION = "adtof_frame_rnn_pytorch_weights.pth (bundled converted checkpoint)"
ADTOF_WEIGHTS_SHA256 = "1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320"

# Overrides the whole `uvx ...` invocation with a pre-installed binary, e.g.
# `uv tool install --python 3.11 --with "setuptools<81" "basic-pitch[onnx]==0.4.0"`
# then `VGT_BASIC_PITCH_CMD=basic-pitch`, so an offline machine can prebuild
# the env once instead of paying the ~35s cold `uvx` build on every run.
BASIC_PITCH_CMD_ENV = "VGT_BASIC_PITCH_CMD"
DRUMSCRIPT_CMD_ENV = "VGT_DRUMSCRIPT_CMD"
ADTOF_RUNTIME_VERSION = "python==3.11"
ADTOF_TORCH_VERSION = "torch==2.13.0"
ADTOF_LOCK_FILENAME = "adtof-requirements.lock"
ADTOF_LOCK_SHA256 = "c1c0e70cd0ff9f3045536a49940d9a9e8ada6523bd17424c36fd4f40e5ebb3e2"
ADTOF_TIMEOUT_SECONDS = 120
ADTOF_CLASS_NAMES = ("bass_drum", "snare_drum", "tom_tom", "hi_hat", "cymbal")

# These are deliberately vgt settings, rather than the port's decoder
# settings: Phase 3 consumes its raw sigmoid outputs and owns the resulting
# timing/MIDI contract.  A 10 ms frame rate means the IOIs below suppress only
# duplicate decoder peaks, not musically meaningful repeated drum hits.
ADTOF_PEAK_THRESHOLDS: dict[str, float] = {
    "bass_drum": 0.30,
    "snare_drum": 0.30,
    "tom_tom": 0.25,
    "hi_hat": 0.30,
    # The Phase-0 capture has no cymbal maxima at the port's 0.30 default.
    "cymbal": 0.20,
}
ADTOF_MIN_INTER_ONSET_SECONDS: dict[str, float] = {
    "bass_drum": 0.08,
    "snare_drum": 0.08,
    "tom_tom": 0.08,
    "hi_hat": 0.05,
    "cymbal": 0.10,
}
# Families from the five-class Frame_RNN model.  The model does not expose
# open/closed hats, high/mid toms, or crash/ride separately, so the stable
# primary GM member is used; the companion GM notes remain reserved for a
# future class-set that can distinguish them.
ADTOF_GM_INSTRUMENTS: dict[str, tuple[str, int]] = {
    "bass_drum": ("kick", 36),
    "snare_drum": ("snare", 38),
    "tom_tom": ("mid_tom", 45),
    "hi_hat": ("hi_hat_closed", 42),
    "cymbal": ("crash", 49),
}
ADTOF_NOTE_DURATION_SECONDS = 0.08

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

# Spectral confirmation gate for `_drop_harmonic_ghosts` (see that function's
# docstring and docs/guitar-transcription-findings.md's "Spectral confirmation"
# section). This never widens a drop the interval/onset/overlap/velocity
# heuristic didn't already flag -- it only retains a flagged note when the
# spectrum shows energy at its fundamental beyond what the parent note's own
# harmonic series (fit from its *other* visible harmonics) predicts there.
GUITAR_GHOST_SPECTRAL_N_FFT = 4096
GUITAR_GHOST_SPECTRAL_HOP_LENGTH = 512
GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER = 8  # highest parent harmonic order used to fit the decay curve
GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES = 0.5  # bin-search half-width around each harmonic's exact frequency
GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO = 1.5  # measured/predicted amplitude ratio that counts as "independent"

# Acoustic-guitar `strict-chords` profile: higher Basic Pitch thresholds than
# `guitar-acoustic-*` produce a deliberately sparse, chord-oriented reference
# (see docs/transcription-variants-plan.md). This is a second, independent
# Basic Pitch inference -- unlike detail/clean, it does not share a detection
# hash with the acoustic profiles.
GUITAR_ACOUSTIC_STRICT_ONSET_THRESHOLD = 0.70
GUITAR_ACOUSTIC_STRICT_FRAME_THRESHOLD = 0.70
GUITAR_ACOUSTIC_STRICT_MINIMUM_NOTE_LENGTH_MS = 125.0

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

# pYIN monophonic backend (see `vgt.pyin_notes` for why bass does not use
# Basic Pitch at all, and docs/bass-transcription-findings.md for the measured
# comparison). These are analysis settings, not instrument tuning: the
# per-instrument frequency window and note-length floor come from the profile.
PYIN_SAMPLE_RATE_HZ = 22050
PYIN_FRAME_LENGTH = 2048
# 256 samples at 22050 Hz is an 11.6 ms frame. Onset timing can therefore be
# out by up to one frame, which is well inside the tolerance of a reference
# track meant to be read along with the audio.
PYIN_HOP_LENGTH = 256
# 5 frames (~58 ms) removes single-frame pitch jitter and closes one-frame
# dropouts without merging genuine adjacent semitones -- a real bass note is
# many frames long at any playable tempo.
PYIN_MEDIAN_FILTER_FRAMES = 5
# Re-articulation splitting (see `pyin_notes._rearticulation_frames`). A pitch
# tracker cannot tell one held note from four plucks of the same fret, so a
# same-pitch run is cut wherever the frame envelope restarts. All three values
# are swept in docs/bass-transcription-findings.md against the maintainer's
# full-length hand-corrected bass reference.
PYIN_REARTICULATION_SPAN_FRAMES = 2
# Low on purpose. A re-attack inside a held note is faint -- median rise 0.66 dB
# where an onset the tracker already found reads 2.16 dB -- so a self-sufficient
# threshold misses most of them. The spacing rule below is what makes this
# affordable; raising this without it costs recall for almost no precision.
PYIN_REARTICULATION_RISE_DB = 0.8
# How close together two cuts inside one pitch run may fall, as a fraction of a
# beat. A dotted sixteenth: below it, two detections are far more often one
# attack found twice than two notes played. Expressed musically for the same
# reason `BASS_SUSTAIN_CLAMP_BARS` is -- a fixed millisecond value would block
# genuine repeated notes at a fast tempo and permit double-triggers at a slow
# one. The measured plateau runs 0.31-0.5 beats.
PYIN_REARTICULATION_MINIMUM_SPACING_BEATS = 0.375

# Bass, tracked monophonically. The frequency window is the *fundamental*
# search range handed to pYIN, not a post-filter. Its supported production
# range is 35–330 Hz: 35 Hz is higher than a five-string low B (30.9 Hz), so
# that fundamental and lower/drop-tuned material are outside this profile's
# measured coverage. The upper bound covers a 24-fret 4-string's top. On the
# 7Rivers stem two independent estimators put the actual line at MIDI 29–43,
# comfortably inside the configured range.
BASS_PYIN_FREQUENCY_HZ = (35.0, 330.0)
# 70 ms at 120 BPM is well under a 32nd note, so this only discards tracker
# fragments, never a played note.
BASS_PYIN_MINIMUM_NOTE_LENGTH_MS = 70.0
# A bass note that rings for two bars is a tracker artifact holding through a
# rest, not a played sustain. Expressed in bars for the same reason
# `GUITAR_SUSTAIN_CLAMP_BARS` is.
BASS_SUSTAIN_CLAMP_BARS = 2.0


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
class PyinSettings:
    """The analysis settings a `backend="pyin"` profile adds on top of the
    frequency window and note-length floor it already shares with a Basic
    Pitch profile (see `InstrumentProfile.pyin`)."""

    sample_rate_hz: int = PYIN_SAMPLE_RATE_HZ
    frame_length: int = PYIN_FRAME_LENGTH
    hop_length: int = PYIN_HOP_LENGTH
    median_filter_frames: int = PYIN_MEDIAN_FILTER_FRAMES
    rearticulation_span_frames: int = PYIN_REARTICULATION_SPAN_FRAMES
    rearticulation_rise_db: float = PYIN_REARTICULATION_RISE_DB
    rearticulation_minimum_spacing_beats: float = PYIN_REARTICULATION_MINIMUM_SPACING_BEATS


@dataclass(frozen=True)
class InstrumentProfile:
    """One instrument's complete transcription identity: its detector settings
    plus its ordered post-processing pipeline.

    Adding a second tuned instrument means adding an entry to
    `_INSTRUMENT_PROFILES`, not copying an `if` branch in
    `default_spec_for_target` -- see the module docstring.

    `backend` selects which spec this profile builds. The detector fields below
    are shared where they mean the same thing to both backends
    (`minimum_note_length_ms`, `minimum_frequency_hz`, `maximum_frequency_hz`,
    `cleanup`) and Basic Pitch's own where they don't: `onset_threshold`,
    `frame_threshold`, `melodia_trick`, and `multiple_pitch_bends` describe a
    Basic Pitch inference and are simply not read for a `pyin` profile, which
    carries its analysis settings in `pyin` instead. They are deliberately not
    made `| None` -- `_DEFAULT_PROFILE` supplies them for every profile, and a
    `pyin` profile's `PyinSpec` never serializes them, so they cannot leak into
    its identity (which is what would make them a real footgun rather than an
    unread default).
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
    # Required when `backend == "pyin"`; ignored otherwise.
    pyin: PyinSettings | None = None
    # Bars of sustain `clamp_sustain` allows, when this profile's cleanup
    # includes that stage. Per-profile because a bass ring-out worth keeping is
    # not the same length as an acoustic guitar's.
    sustain_clamp_bars: float = GUITAR_SUSTAIN_CLAMP_BARS


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
# `bass-basic-pitch` is the pre-#/pyin `bass` profile, kept under its explicit
# backend name so an existing sidecar's stored settings still resolve and a user
# can still ask for the old behaviour for comparison. It is no longer bass's
# default: on a real stem Basic Pitch does not produce a usable bass line at
# any setting (see `vgt.pyin_notes` and
# docs/bass-transcription-findings.md).
_BASS_BASIC_PITCH_PROFILE = replace(
    _DEFAULT_PROFILE, name="bass-basic-pitch", minimum_frequency_hz=30.0, maximum_frequency_hz=400.0
)  # Retired comparison profile; its 30 Hz floor includes low B, but establishes no production support.
# A bass is a single-line source.  `force_monophony` was the original attempt
# at exploiting that, and it is retained for comparison, but it resolves an
# overlap by *velocity* -- and a bass ghost harmonic is routinely louder than
# its own fundamental, so on the reference stem it dropped the right note far
# more often than the wrong one (30% frame accuracy). `bass` below solves the
# same problem at the detector instead.
_BASS_MONOPHONIC_PROFILE = replace(
    _BASS_BASIC_PITCH_PROFILE,
    name="bass-monophonic",
    cleanup=(CleanupStage("force_monophony"),),
)

# Bass's default: a monophonic pitch tracker, not a polyphonic model. The
# cleanup pipeline is deliberately short, because a tracker's failure modes are
# not a polyphonic model's -- there are no harmonic ghosts to drop and no
# voices to cap (see `pyin_notes.segment_notes` on why polyphony is 1 by
# construction). What is left is the ordered subset that still applies:
#
# 1. `merge_fragments` rejoins a held note the median filter split across a
#    two-frame pitch wobble. First, for the same reason as the guitar
#    pipeline: every later stage reasons about note lengths. `merge_touching`
#    is off here and only here: the tracker emits touching same-pitch notes
#    solely where it cut a re-articulation, so merging them would undo every
#    split `segment_notes` just made. Measured on the 7Rivers bass stem this
#    stage merges nothing else at all (0 of 167 same-pitch pairs), so the
#    guard costs no repair it was actually performing.
# 2. `drop_isolated_notes` removes a short run at a pitch nothing else in the
#    part touches -- an octave slip the median filter was too narrow to catch.
# 3. `clamp_sustain` caps a note the tracker held through a rest, after
#    merging, since a chain of fragments is how such a note survives step 1.
_BASS_PYIN_CLEANUP: tuple[CleanupStage, ...] = (
    CleanupStage("merge_fragments", {"max_gap_s": GUITAR_FRAGMENT_MERGE_GAP_S, "merge_touching": False}),
    CleanupStage(
        "drop_isolated_notes",
        {
            "max_duration_s": GUITAR_ISOLATED_MAX_DURATION_S,
            "neighbour_window_s": GUITAR_ISOLATED_NEIGHBOUR_WINDOW_S,
        },
    ),
    CleanupStage("clamp_sustain", {}),
)
_BASS_PYIN_PROFILE = InstrumentProfile(
    name="bass-pyin",
    backend="pyin",
    onset_threshold=DEFAULT_ONSET_THRESHOLD,
    frame_threshold=DEFAULT_FRAME_THRESHOLD,
    minimum_note_length_ms=BASS_PYIN_MINIMUM_NOTE_LENGTH_MS,
    minimum_frequency_hz=BASS_PYIN_FREQUENCY_HZ[0],
    maximum_frequency_hz=BASS_PYIN_FREQUENCY_HZ[1],
    multiple_pitch_bends=DEFAULT_MULTIPLE_PITCH_BENDS,
    melodia_trick=DEFAULT_MELODIA_TRICK,
    cleanup=_BASS_PYIN_CLEANUP,
    pyin=PyinSettings(),
    sustain_clamp_bars=BASS_SUSTAIN_CLAMP_BARS,
)
# `bass` is `bass-pyin` under the name `_profile_for_target` resolves for the
# target, so a project that never names a profile gets the tracker.
_BASS_PROFILE = replace(_BASS_PYIN_PROFILE, name="bass")
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
# `bass-monophonic` has a one-stage `force_monophony` pipeline, so it has no
# ordering to get wrong -- but its position is NOT order-independent in general.
# Measured on the 7Rivers bass stem, moving `clamp_sustain` from before it to
# after it swings frame accuracy by ~20 points: a multi-second drone note wins
# every overlap it spans, so it must be shortened before overlaps are resolved.
# Anything that gives `force_monophony` siblings must put it last.
# Do not add it to `vocals`: LALAL's vocals stem routinely contains stacked
# backing vocals and harmonies, which are genuinely polyphonic rather than
# detection artifacts.
#
# `clamp_sustain`'s `params` starts empty: `default_spec_for_target` fills in
# `max_duration_s` from the detected tempo (see `_instantiate_cleanup`), and
# drops the stage entirely when no tempo is known yet, mirroring the old
# `sustain_clamp_s: float | None` field's behaviour.
_GUITAR_ACOUSTIC_FULL_CLEANUP: tuple[CleanupStage, ...] = (
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
            "spectral_n_fft": GUITAR_GHOST_SPECTRAL_N_FFT,
            "spectral_hop_length": GUITAR_GHOST_SPECTRAL_HOP_LENGTH,
            "spectral_max_harmonic_order": GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
            "spectral_freq_tolerance_semitones": GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
            "spectral_independent_energy_ratio": GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
        },
    ),
    CleanupStage(
        "cap_simultaneous_voices",
        {
            "max_voices": GUITAR_MAX_SIMULTANEOUS_VOICES,
            "min_duration_after_cap_s": GUITAR_MIN_NOTE_DURATION_AFTER_CAP_S,
        },
    ),
)

# Detail deliberately keeps only the two cleanup stages that never drop a
# note (`merge_fragments` rejoins split notes; `clamp_sustain` only shortens a
# runaway ring-out) -- see docs/transcription-variants-plan.md's "why detail
# and clean share detection" section. Sharing this ordered prefix with
# `_GUITAR_ACOUSTIC_FULL_CLEANUP` (rather than redeclaring it) keeps a
# retuned merge gap or sustain clamp from silently drifting between the two
# profiles.
_GUITAR_ACOUSTIC_DETAIL_CLEANUP: tuple[CleanupStage, ...] = tuple(
    stage for stage in _GUITAR_ACOUSTIC_FULL_CLEANUP if stage.name in ("merge_fragments", "clamp_sustain")
)

_GUITAR_ACOUSTIC_CLEAN_PROFILE = InstrumentProfile(
    name="guitar-acoustic-clean",
    backend="basic-pitch",
    onset_threshold=GUITAR_ACOUSTIC_ONSET_THRESHOLD,
    frame_threshold=GUITAR_ACOUSTIC_FRAME_THRESHOLD,
    minimum_note_length_ms=GUITAR_ACOUSTIC_MINIMUM_NOTE_LENGTH_MS,
    minimum_frequency_hz=GUITAR_ACOUSTIC_FREQUENCY_HZ[0],
    maximum_frequency_hz=GUITAR_ACOUSTIC_FREQUENCY_HZ[1],
    multiple_pitch_bends=DEFAULT_MULTIPLE_PITCH_BENDS,
    melodia_trick=GUITAR_ACOUSTIC_MELODIA_TRICK,
    cleanup=_GUITAR_ACOUSTIC_FULL_CLEANUP,
    probe_expectations=ProbeExpectations(
        expected_voice_count=GUITAR_MAX_SIMULTANEOUS_VOICES,
        harmonic_ghost_intervals=GUITAR_HARMONIC_GHOST_INTERVALS,
        sustain_cap_s=4.0,
    ),
)

# `guitar-acoustic` is `guitar-acoustic-clean`'s pre-existing name, kept as a
# compatibility alias: every field but `name` (which never enters
# `settings_hash`, see `BasicPitchSpec.to_dict`) is identical, so existing
# sidecars naming `guitar-acoustic` keep exactly the same resolved behavior
# and cache identity.
_GUITAR_ACOUSTIC_PROFILE = replace(_GUITAR_ACOUSTIC_CLEAN_PROFILE, name="guitar-acoustic")

# Detail: same Basic Pitch detection settings as clean (so both can share one
# raw inference), lighter cleanup -- preserves questionable/quiet notes for
# listening and manual review instead of dropping them.
_GUITAR_ACOUSTIC_DETAIL_PROFILE = replace(
    _GUITAR_ACOUSTIC_CLEAN_PROFILE,
    name="guitar-acoustic-detail",
    cleanup=_GUITAR_ACOUSTIC_DETAIL_CLEANUP,
    probe_expectations=None,  # the probe's expected voice/ghost/sustain bounds assume the full cleanup pipeline
)

# Strict-chords: higher onset/frame/minimum-note thresholds than clean, so it
# requires its own Basic Pitch inference (a different detection identity) --
# see docs/transcription-variants-plan.md. Its cleanup is the same full
# pipeline as clean.
_GUITAR_ACOUSTIC_STRICT_CHORDS_PROFILE = replace(
    _GUITAR_ACOUSTIC_CLEAN_PROFILE,
    name="guitar-acoustic-strict-chords",
    onset_threshold=GUITAR_ACOUSTIC_STRICT_ONSET_THRESHOLD,
    frame_threshold=GUITAR_ACOUSTIC_STRICT_FRAME_THRESHOLD,
    minimum_note_length_ms=GUITAR_ACOUSTIC_STRICT_MINIMUM_NOTE_LENGTH_MS,
)

_INSTRUMENT_PROFILES: dict[str, InstrumentProfile] = {
    "default": _DEFAULT_PROFILE,
    "guitar": _GUITAR_PROFILE,
    "bass": _BASS_PROFILE,
    "bass-pyin": _BASS_PYIN_PROFILE,
    "bass-basic-pitch": _BASS_BASIC_PITCH_PROFILE,
    "bass-monophonic": _BASS_MONOPHONIC_PROFILE,
    "vocals": _VOCALS_PROFILE,
    "guitar-acoustic": _GUITAR_ACOUSTIC_PROFILE,
    "guitar-acoustic-detail": _GUITAR_ACOUSTIC_DETAIL_PROFILE,
    "guitar-acoustic-clean": _GUITAR_ACOUSTIC_CLEAN_PROFILE,
    "guitar-acoustic-strict-chords": _GUITAR_ACOUSTIC_STRICT_CHORDS_PROFILE,
}
VALID_PROFILE_NAMES: tuple[str, ...] = tuple(_INSTRUMENT_PROFILES)


@dataclass(frozen=True)
class DrumTranscriptionProfile:
    """A built-in drums profile and the backend it selects.

    DrumScript's cleanup recipes predate the general profile registry.  This
    adapter preserves their serialized spec shape while making backend choice
    a property of the resolved profile rather than of the target name.
    """

    name: str
    backend: str
    cleanup_profile: str | None = None
    # Analysis-only preprocessing for the transcription backend. This is
    # deliberately not a stem transformation; the variant layer renders it
    # into a separate content-addressed WAV.
    audio_frontend: Mapping[str, Any] = field(default_factory=lambda: {"stages": []})


_DRUM_TRANSCRIPTION_PROFILES: dict[str, DrumTranscriptionProfile] = {
    name: DrumTranscriptionProfile(name=name, backend="drumscript", cleanup_profile=name)
    for name in DRUM_CLEANUP_PROFILE_NAMES
}
_DRUM_TRANSCRIPTION_PROFILES["drums-adtof"] = DrumTranscriptionProfile(
    name="drums-adtof", backend="adtof"
)
_DRUM_TRANSCRIPTION_PROFILES["drums-hpss-gentle"] = DrumTranscriptionProfile(
    name="drums-hpss-gentle",
    backend="drumscript",
    cleanup_profile="drums-clean",
    audio_frontend={
        "stages": [
            {"type": "hpss_blend", "component": "percussive", "wet": 0.35,
             "margin": 1.0, "n_fft": 2048, "hop_length": 512}
        ]
    },
)
DRUM_TRANSCRIPTION_PROFILE_NAMES: tuple[str, ...] = tuple(_DRUM_TRANSCRIPTION_PROFILES)
DEFAULT_DRUM_TRANSCRIPTION_PROFILE_NAME = "drums-hpss-gentle"

# The canonical, load-bearing cleanup stage order (see
# `_GUITAR_ACOUSTIC_FULL_CLEANUP`'s docstring above and
# docs/transcription-variants-plan.md's "cleanup order" section). A project
# profile may enable, disable, or reconfigure these stages but may never
# reorder them; `force_monophony` (bass-only) is a single-stage pipeline with
# no ordering constraint and is deliberately not part of this contract.
CANONICAL_CLEANUP_STAGE_ORDER: tuple[str, ...] = (
    "merge_fragments",
    "drop_isolated_notes",
    "clamp_sustain",
    "drop_harmonic_ghosts",
    "cap_simultaneous_voices",
)


def _detection_identity(profile: InstrumentProfile) -> dict[str, Any]:
    """The subset of `profile` that determines one Basic Pitch inference.

    Two profiles with equal `_detection_identity` can share one raw
    detection run (see docs/transcription-variants-plan.md's two-level
    cache); this is exactly why `guitar-acoustic-detail` and
    `guitar-acoustic-clean` are declared with identical detector fields
    above and differ only in `cleanup`.
    """
    return {
        "backend": profile.backend,
        "onset_threshold": profile.onset_threshold,
        "frame_threshold": profile.frame_threshold,
        "minimum_note_length_ms": profile.minimum_note_length_ms,
        "minimum_frequency_hz": profile.minimum_frequency_hz,
        "maximum_frequency_hz": profile.maximum_frequency_hz,
        "multiple_pitch_bends": profile.multiple_pitch_bends,
        "melodia_trick": profile.melodia_trick,
    }


def _cleanup_identity(profile: InstrumentProfile) -> list[dict[str, Any]]:
    return [{"name": stage.name, "params": stage.params} for stage in profile.cleanup]


def profile_detection_hash(profile: InstrumentProfile) -> str:
    """Identity of the Basic Pitch inference `profile` requests.

    Equal for `guitar-acoustic-detail`/`guitar-acoustic-clean`/
    `guitar-acoustic` (identical detector settings); different for
    `guitar-acoustic-strict-chords` (different thresholds, so it needs its
    own inference run).
    """
    return hashlib.sha256(json.dumps(_detection_identity(profile), sort_keys=True).encode("utf-8")).hexdigest()


def profile_cleanup_hash(profile: InstrumentProfile) -> str:
    """Identity of `profile`'s full derived-variant recipe: its detection
    identity plus its ordered, parameterized cleanup pipeline. Differs
    between `guitar-acoustic-detail` and `guitar-acoustic-clean` even though
    they share a detection hash, because their `cleanup` differs."""
    payload = {"detection_hash": profile_detection_hash(profile), "cleanup": _cleanup_identity(profile)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


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
_PROFILE_NAMES_BY_TARGET["guitar"] = (
    "default",
    "guitar",
    "guitar-acoustic",
    "guitar-acoustic-detail",
    "guitar-acoustic-clean",
    "guitar-acoustic-strict-chords",
)
_PROFILE_NAMES_BY_TARGET["bass"] = (
    "default",
    "bass",
    "bass-pyin",
    "bass-basic-pitch",
    "bass-monophonic",
)
_PROFILE_NAMES_BY_TARGET["drums"] = DRUM_TRANSCRIPTION_PROFILE_NAMES


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


def drum_transcription_profile(modes: Mapping[str, str] | None) -> DrumTranscriptionProfile:
    """Resolve drums' selected profile.

    No explicit mode uses the measured gentle-HPSS analysis frontend. Naming
    ``default`` explicitly remains the raw-stem opt-out profile.
    """
    name = modes.get("drums") if isinstance(modes, Mapping) else None
    return _DRUM_TRANSCRIPTION_PROFILES.get(
        name, _DRUM_TRANSCRIPTION_PROFILES[DEFAULT_DRUM_TRANSCRIPTION_PROFILE_NAME]
    )


def backend_for_target_profile(target: str, modes: Mapping[str, str] | None) -> str:
    """Resolve a backend from the selected profile, never from target alone."""
    validate_target(target)
    if target == "drums":
        return drum_transcription_profile(modes).backend
    return _profile_for_target(target, modes).backend


def _profile_for_target(target: str, modes: Mapping[str, str] | None) -> InstrumentProfile:
    """Resolve ``target`` through an optional target-to-profile map.

    A sidecar can outlive the profile registry that wrote it.  Missing or
    unrecognised stored selections therefore safely use the target's default;
    only explicit CLI input is validated by :func:`validate_profile_for_target`.
    """
    profile_name = modes.get(target) if isinstance(modes, Mapping) else None
    # `default` is the target-default selection, not an instruction to bypass
    # that target's profile. This matters for bass, whose default is pYIN;
    # the retained `bass-basic-pitch` profile remains the explicit opt-in.
    if profile_name == "default":
        return _INSTRUMENT_PROFILES.get(target, _DEFAULT_PROFILE)
    if profile_name in valid_profile_names_for_target(target):
        return _INSTRUMENT_PROFILES[profile_name]
    return _INSTRUMENT_PROFILES.get(target, _DEFAULT_PROFILE)


def effective_profile_name_for_target(target: str, modes: Mapping[str, str] | None) -> str:
    """Return the profile execution will use for ``target``.

    This deliberately shares the missing/stale-mode fallback path with
    :func:`default_spec_for_target`, so read-only callers can describe the
    effective configuration without reimplementing profile selection.
    `drums` is not in `_INSTRUMENT_PROFILES`; it resolves through its small
    backend-bearing drum-profile registry instead.
    """
    if target == "drums":
        return drum_transcription_profile(modes).name
    return _profile_for_target(target, modes).name


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
    # The effective project tempo markers, expressed in reference-relative
    # seconds.  MIDI stores quarter-note positions, so this is required to
    # place real-second detector events correctly on a non-constant project
    # tempo map.
    tempo_map: "TempoMapReference | None" = None
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
        if data["tempo_map"] is None:
            del data["tempo_map"]
        if not data["cleanup"]:
            del data["cleanup"]
            data.update(_LEGACY_EMPTY_CLEANUP_FIELDS)
        return data


@dataclass(frozen=True)
class PyinSpec:
    """Everything that changes one monophonic pitch-tracked target's output.

    Unlike `BasicPitchSpec` this carries no `package_pin`/`serialization`: pYIN
    runs in-process through librosa (already a hard vgt dependency), so what
    stands in for a pinned runtime is `algorithm_version` -- see
    `vgt.pyin_notes.PYIN_ALGORITHM_VERSION` for why librosa's own version is
    deliberately not part of the identity.

    Every field here is read by the backend, which is the whole point of not
    reusing `BasicPitchSpec`: a `pyin` variant's `settings_hash` must not
    contain an `onset_threshold` or a `melodia_trick` that nothing consults,
    and `vgt transcription profile show` must not display them.
    """

    backend: str  # "pyin" | "fake"
    algorithm_version: int
    sample_rate_hz: int
    frame_length: int
    hop_length: int
    median_filter_frames: int
    minimum_note_length_ms: float
    minimum_frequency_hz: float
    maximum_frequency_hz: float
    midi_tempo: float | None
    rearticulation_span_frames: int = PYIN_REARTICULATION_SPAN_FRAMES
    rearticulation_rise_db: float = PYIN_REARTICULATION_RISE_DB
    # In beats, not seconds: `PyinTranscriber` resolves it against `midi_tempo`
    # at call time. Keeping the musical value in the identity means a project
    # whose tempo is re-detected invalidates for the right reason -- the
    # spacing genuinely changed -- rather than because a derived number moved.
    rearticulation_minimum_spacing_beats: float = PYIN_REARTICULATION_MINIMUM_SPACING_BEATS
    tempo_map: "TempoMapReference | None" = None
    cleanup: tuple[CleanupStage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["tempo_map"] is None:
            del data["tempo_map"]
        data["cleanup"] = [{"name": stage.name, "params": stage.params} for stage in self.cleanup]
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
    # The project's detected tempo (issue #193): DrumScript's own beat
    # tracker is unreliable (it made a half-tempo octave error on 7Rivers,
    # authoring at 60 BPM against a 120 BPM project), so vgt overrides the
    # authored MIDI's tempo with the project's at its own boundary rather
    # than trusting DrumScript's. `None` only for specs built before this
    # field existed (never true for a freshly constructed spec).
    midi_tempo: float | None
    tempo_map: "TempoMapReference | None" = None
    # `drums-clean` (issue #177): which built-in `vgt.drum_cleanup` recipe to
    # apply to DrumScript's raw events before writing MIDI/JSON. Defaults to
    # "default" -- DrumScript's raw output, untouched -- so every existing
    # spec keeps its exact prior identity (see `to_dict`).
    cleanup_profile: str = "default"
    cleanup: tuple[CleanupStage, ...] = ()
    # The analyzed beat grid. Every profile needs it: `drums-clean` uses it to
    # estimate its systematic latency, and both profiles now author onto it
    # (see `vgt.drum_grid`), so it is a genuine input to the artifact and is
    # hashed as one.
    beat_grid: BeatGridReference | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for `spec_hash`. The `default` profile keeps the pre-#177
        five-field shape (plus `midi_tempo`, #193) apart from `beat_grid`,
        which every profile now carries -- a re-analysis that moves the grid
        must invalidate the MIDI authored against the old one."""
        data: dict[str, Any] = {
            "backend": self.backend,
            "package_pin": self.package_pin,
            "runtime_version": self.runtime_version,
            "classifier_mode": self.classifier_mode,
            "time_signature": list(self.time_signature) if self.time_signature else None,
            "midi_tempo": self.midi_tempo,
            "beat_grid": (
                {"beat_times": list(self.beat_grid.beat_times), "downbeat_offset_s": self.beat_grid.downbeat_offset_s}
                if self.beat_grid is not None else None
            ),
        }
        if self.tempo_map is not None:
            data["tempo_map"] = self.tempo_map.to_dict()
        if self.cleanup_profile != "default":
            data["cleanup_profile"] = self.cleanup_profile
            data["cleanup"] = [{"name": stage.name, "params": stage.params} for stage in self.cleanup]
        return data


@dataclass(frozen=True)
class AdtofSpec:
    """Pinned Phase-0 ADTOF identity. Phase 1 intentionally has no Torch runner."""

    backend: str
    package_pin: str
    package_version: str
    model_version: str
    weights_version: str
    weights_sha256: str
    runtime_version: str
    torch_version: str
    lock_sha256: str
    midi_tempo: float | None
    beat_grid: BeatGridReference | None
    tempo_map: "TempoMapReference | None" = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "backend": self.backend, "package_pin": self.package_pin,
            "package_version": self.package_version, "model_version": self.model_version,
            "weights_version": self.weights_version, "weights_sha256": self.weights_sha256,
            "runtime_version": self.runtime_version, "torch_version": self.torch_version,
            "lock_sha256": self.lock_sha256,
            "midi_tempo": self.midi_tempo,
            "beat_grid": {"beat_times": list(self.beat_grid.beat_times), "downbeat_offset_s": self.beat_grid.downbeat_offset_s}
            if self.beat_grid else None,
            "peak_thresholds": ADTOF_PEAK_THRESHOLDS,
            "min_inter_onset_seconds": ADTOF_MIN_INTER_ONSET_SECONDS,
            "grid_subdivisions": 2,
        }
        if self.tempo_map is not None:
            data["tempo_map"] = self.tempo_map.to_dict()
        return data


@dataclass(frozen=True)
class AdtofActivationResult:
    """Validated raw ADTOF output for Phase 3's vgt-owned post-processing."""

    activations: Any
    metadata: dict[str, Any]
    cache_key: str
    cache_hit: bool


TranscriptionSpec = BasicPitchSpec | PyinSpec | DrumScriptSpec | AdtofSpec

# The two note-producing specs. Both carry a `cleanup` pipeline, a `midi_tempo`
# and a `tempo_map`, which is everything `_apply_cleanup_stages` and
# `derive_variant_artifacts` need -- so a monophonic variant reuses the same
# raw-detection/derived-cleanup machinery as a Basic Pitch one rather than
# getting a parallel code path (see `vgt.transcription_variants`).
NoteSpec = BasicPitchSpec | PyinSpec


@dataclass(frozen=True)
class TempoMapReference:
    """The detected tempo markers vgt applies to REAPER, relative to the
    reference item's start.  ``spans`` mirrors ``tempo.value.spans``: each
    marker changes the BPM from its start onwards.  Keeping this value in the
    spec makes the MIDI coordinate system part of artifact identity."""

    bpm: float
    spans: tuple[tuple[float, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"bpm": self.bpm, "spans": [{"start_seconds": start, "bpm": bpm} for start, bpm in self.spans]}


def tempo_map_reference(tempo_value: Mapping[str, Any] | None) -> TempoMapReference | None:
    """Build the authoring timeline from a persisted tempo-stage value.

    Constant grids intentionally return ``None``: their established
    seconds-to-ticks conversion at ``midi_tempo`` remains byte-for-byte the
    coordinate rule.  A piecewise grid carries every marker that the
    ReaScript applies, including the base BPM at project time zero.
    """
    if not isinstance(tempo_value, Mapping) or tempo_value.get("mode") != "piecewise":
        return None
    try:
        bpm = float(tempo_value["bpm"])
    except (KeyError, TypeError, ValueError):
        return None
    if bpm <= 0:
        return None
    spans: list[tuple[float, float]] = []
    for span in tempo_value.get("spans") or []:
        if not isinstance(span, Mapping):
            continue
        try:
            start, span_bpm = float(span["start_seconds"]), float(span["bpm"])
        except (KeyError, TypeError, ValueError):
            continue
        if start >= 0 and span_bpm > 0:
            spans.append((start, span_bpm))
    return TempoMapReference(bpm=bpm, spans=tuple(sorted(set(spans))))


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
    beat_times: Sequence[float] | None = None,
    downbeat_offset_s: float | None = None,
    tempo_map: TempoMapReference | None = None,
) -> TranscriptionSpec:
    """The per-target default spec, resolved through `_INSTRUMENT_PROFILES`.

    ``modes`` selects a named profile independently for each target. An absent
    or stale selection falls back to that target's default. `time_signature` (a
    tempo-stage string like `"4/4"`) converts that profile's cleanup stage's
    bar-based sustain clamp to seconds at this specific tempo.
    """
    validate_target(target)
    if backend == "drumscript":
        cleanup_profile_name = drum_transcription_profile(modes).cleanup_profile
        if cleanup_profile_name is None:
            raise TranscriptionError("the selected drums profile does not use DrumScript")
        cleanup_profile = DRUM_CLEANUP_PROFILES[cleanup_profile_name]
        cleanup = (CleanupStage("drums-clean", cleanup_profile.as_identity()),) if cleanup_profile.enabled else ()
        return DrumScriptSpec(
            backend=backend,
            package_pin=package_pin if package_pin != BASIC_PITCH_PACKAGE_PIN else DRUMSCRIPT_PACKAGE_PIN,
            runtime_version=drumscript_runtime_version,
            classifier_mode=drumscript_classifier_mode,
            time_signature=drumscript_time_signature,
            midi_tempo=midi_tempo,
            tempo_map=tempo_map,
            cleanup_profile=cleanup_profile_name,
            cleanup=cleanup,
            beat_grid=(
                BeatGridReference(tuple(float(time) for time in beat_times), downbeat_offset_s)
                if beat_times else None
            ),
        )
    if backend == "adtof":
        return AdtofSpec(
            backend="adtof", package_pin=ADTOF_PACKAGE_PIN, package_version=ADTOF_PACKAGE_VERSION,
            model_version=ADTOF_MODEL_VERSION, weights_version=ADTOF_WEIGHTS_VERSION,
            weights_sha256=ADTOF_WEIGHTS_SHA256, runtime_version=ADTOF_RUNTIME_VERSION,
            torch_version=ADTOF_TORCH_VERSION, lock_sha256=ADTOF_LOCK_SHA256, midi_tempo=midi_tempo,
            beat_grid=BeatGridReference(tuple(float(time) for time in beat_times), downbeat_offset_s) if beat_times else None,
            tempo_map=tempo_map,
        )
    profile = _profile_for_target(target, modes)
    bar_seconds = _bar_duration_seconds(midi_tempo, time_signature)
    sustain_clamp_s = bar_seconds * profile.sustain_clamp_bars if bar_seconds else None
    if profile.backend == "pyin":
        return pyin_spec_from_profile(
            profile,
            midi_tempo=midi_tempo,
            sustain_clamp_s=sustain_clamp_s,
            tempo_map=tempo_map,
            # `backend` is the caller's override, which the offline suite uses
            # to force `"fake"`; the profile only decides *which* real backend
            # would run. Defaulting it away here would make every bass spec
            # claim "pyin" even under `FakeTranscriber`.
            backend="pyin" if backend == "basic-pitch" else backend,
        )
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
        tempo_map=tempo_map,
        cleanup=_instantiate_cleanup(profile.cleanup, sustain_clamp_s=sustain_clamp_s),
    )


def pyin_spec_from_profile(
    profile: InstrumentProfile,
    *,
    midi_tempo: float | None,
    sustain_clamp_s: float | None,
    tempo_map: TempoMapReference | None = None,
    backend: str = "pyin",
) -> PyinSpec:
    """Build the `PyinSpec` a `backend="pyin"` profile describes.

    Shared by `default_spec_for_target` and
    `transcription_profiles.spec_from_resolved_profile`, so a profile selected
    through `--mode` and one selected through `variant add --profile` can never
    resolve to different settings.
    """
    if profile.backend != "pyin":
        raise TranscriptionError(f"profile {profile.name!r} does not use the pyin backend")
    if profile.pyin is None:
        raise TranscriptionError(f"pyin profile {profile.name!r} carries no PyinSettings")
    if profile.minimum_frequency_hz is None or profile.maximum_frequency_hz is None:
        raise TranscriptionError(f"pyin profile {profile.name!r} needs an explicit frequency window")
    return PyinSpec(
        backend=backend,
        algorithm_version=PYIN_ALGORITHM_VERSION,
        sample_rate_hz=profile.pyin.sample_rate_hz,
        frame_length=profile.pyin.frame_length,
        hop_length=profile.pyin.hop_length,
        median_filter_frames=profile.pyin.median_filter_frames,
        minimum_note_length_ms=profile.minimum_note_length_ms,
        minimum_frequency_hz=profile.minimum_frequency_hz,
        maximum_frequency_hz=profile.maximum_frequency_hz,
        midi_tempo=midi_tempo,
        rearticulation_span_frames=profile.pyin.rearticulation_span_frames,
        rearticulation_rise_db=profile.pyin.rearticulation_rise_db,
        rearticulation_minimum_spacing_beats=profile.pyin.rearticulation_minimum_spacing_beats,
        tempo_map=tempo_map,
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


def _spec_package_pin(spec: TranscriptionSpec) -> str | None:
    """The pinned package a spec's backend runs, or `None` for one that runs
    in-process. Only pYIN has no pin: librosa is already a vgt dependency, so
    there is no isolated environment to pin, and `PyinSpec.algorithm_version`
    carries the runtime identity a pin would otherwise provide."""
    return None if isinstance(spec, PyinSpec) else spec.package_pin


def _settings_dict(spec: TranscriptionSpec) -> dict[str, Any]:
    if isinstance(spec, PyinSpec):
        return {
            "algorithm_version": spec.algorithm_version,
            "sample_rate_hz": spec.sample_rate_hz,
            "frame_length": spec.frame_length,
            "hop_length": spec.hop_length,
            "median_filter_frames": spec.median_filter_frames,
            "minimum_note_length_ms": spec.minimum_note_length_ms,
            "minimum_frequency_hz": spec.minimum_frequency_hz,
            "maximum_frequency_hz": spec.maximum_frequency_hz,
            "midi_tempo": spec.midi_tempo,
        }
    if isinstance(spec, DrumScriptSpec):
        return {
            "runtime_version": spec.runtime_version,
            "classifier_mode": spec.classifier_mode,
            "time_signature": list(spec.time_signature) if spec.time_signature else None,
            "midi_tempo": spec.midi_tempo,
        }
    if isinstance(spec, AdtofSpec):
        return {
            "model_version": spec.model_version, "weights_sha256": spec.weights_sha256,
            "midi_tempo": spec.midi_tempo,
            "beat_grid": (
                {"beat_times": list(spec.beat_grid.beat_times), "downbeat_offset_s": spec.beat_grid.downbeat_offset_s}
                if spec.beat_grid else None
            ),
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
        "package_pin": _spec_package_pin(spec),
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
        "midi_tempo": spec.midi_tempo,
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
        "package_pin": _spec_package_pin(spec),
        "serialization": spec.serialization if isinstance(spec, BasicPitchSpec) else None,
        "source_role": source_role,
        "input_hash": input_hash,
        "settings_hash": spec_hash(spec),
        "status": "transcribed",
        "midi_file": midi_artifact_name(target),
        "notes_file": notes_artifact_name(target) if isinstance(spec, BasicPitchSpec) else None,
        "events_file": events_artifact_name(target) if isinstance(spec, (DrumScriptSpec, AdtofSpec)) else None,
        "note_count": result.note_count if isinstance(spec, BasicPitchSpec) else None,
        "event_count": result.event_count if isinstance(spec, (DrumScriptSpec, AdtofSpec)) else None,
        "instrument_counts": result.instrument_counts if isinstance(spec, (DrumScriptSpec, AdtofSpec)) else None,
        # GM percussion note numbers select kit instruments; they are not a
        # musical pitch range and must never be presented as one.
        "pitch_range_midi": list(result.pitch_range_midi) if isinstance(spec, BasicPitchSpec) and result.pitch_range_midi else None,
        "first_note_s": result.first_note_s if isinstance(spec, BasicPitchSpec) else None,
        "last_note_s": result.last_note_s if isinstance(spec, BasicPitchSpec) else None,
        "first_event_s": result.first_event_s if isinstance(spec, (DrumScriptSpec, AdtofSpec)) else None,
        "last_event_s": result.last_event_s if isinstance(spec, (DrumScriptSpec, AdtofSpec)) else None,
        "backend_tempo": result.backend_tempo if isinstance(spec, (DrumScriptSpec, AdtofSpec)) else None,
        "midi_tempo": result.midi_tempo if isinstance(spec, (DrumScriptSpec, AdtofSpec)) else spec.midi_tempo,
        # DrumScript does not expose calibrated confidence.  Keeping this
        # explicit prevents downstream consumers from mistaking velocity for
        # a confidence score.
        "confidence": None if isinstance(spec, (DrumScriptSpec, AdtofSpec)) else result.confidence,
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
        "package_pin": _spec_package_pin(spec),
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
        "midi_tempo": spec.midi_tempo,
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
    max_note_duration_s: float | None = None
    max_simultaneous_voices: int | None = None


@dataclass
class RawDetectionResult:
    """One backend's raw, pre-cleanup Basic Pitch output: what
    `BasicPitchTranscriber.detect_raw`/`FakeTranscriber.detect_raw` produce
    before any cleanup stage runs (see docs/transcription-variants-plan.md's
    "Layer 1: raw detection"). Shareable across every variant whose profile
    requests the same `detection_hash` -- exactly what lets detail and clean
    derive from one Basic Pitch inference. DrumScript has no raw/derived
    split (see the plan's "Layer 1" note) and does not produce one of these."""

    notes: list[ParsedNote]
    raw_midi_path: Path
    raw_notes_path: Path
    midi_tempo: float | None


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

    def for_target(self, target: str, modes: Mapping[str, str] | None = None) -> Transcriber: ...

    def spec_for_target(
        self, target: str, *, midi_tempo: float | None, modes: Mapping[str, str] | None = None, time_signature: str | None = None,
        beat_times: Sequence[float] | None = None, downbeat_offset_s: float | None = None,
        tempo_map: TempoMapReference | None = None,
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
    adtof: Transcriber | None = None
    # Defaults to `basic_pitch` so a router built by an existing caller (or a
    # test wiring a single fake) keeps routing every note target to the one
    # transcriber it supplied, rather than failing on bass.
    pyin: Transcriber | None = None

    def for_target(self, target: str, modes: Mapping[str, str] | None = None) -> Transcriber:
        validate_target(target)
        backend = backend_for_target_profile(target, modes)
        if backend == "adtof":
            if self.adtof is None:
                raise TranscriptionError("ADTOF is not available in this router")
            return self.adtof
        if backend == "pyin":
            return self.pyin if self.pyin is not None else self.basic_pitch
        return self.drumscript if backend == "drumscript" else self.basic_pitch

    def spec_for_target(
        self, target: str, *, midi_tempo: float | None, modes: Mapping[str, str] | None = None, time_signature: str | None = None,
        beat_times: Sequence[float] | None = None, downbeat_offset_s: float | None = None,
        tempo_map: TempoMapReference | None = None,
    ) -> TranscriptionSpec:
        backend = backend_for_target_profile(target, modes)
        if backend == "drumscript":
            return default_spec_for_target(
                target,
                backend=backend,
                package_pin=self.drumscript_package_pin,
                midi_tempo=midi_tempo,
                modes=modes,
                drumscript_runtime_version=self.drumscript_runtime_version,
                drumscript_classifier_mode=self.drumscript_classifier_mode,
                drumscript_time_signature=self.drumscript_time_signature,
                beat_times=beat_times,
                downbeat_offset_s=downbeat_offset_s,
                tempo_map=tempo_map,
            )
        return default_spec_for_target(
            target, backend=backend, midi_tempo=midi_tempo, modes=modes, time_signature=time_signature,
            beat_times=beat_times, downbeat_offset_s=downbeat_offset_s, tempo_map=tempo_map,
        )


def production_transcriber_router() -> TranscriberRouter:
    """Current production route: DrumScript handles drums, pYIN handles the
    monophonic targets its profiles claim (bass), Basic Pitch everything else."""
    basic_pitch = BasicPitchTranscriber()
    return TargetTranscriberRouter(
        basic_pitch=basic_pitch,
        drumscript=DrumScriptTranscriber(),
        adtof=AdtofTranscriber(),
        pyin=PyinTranscriber(),
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
    # Inline `isinstance` rather than a hoisted bool: only the inline form lets
    # a type checker narrow the spec union before the attribute access.
    minimum_frequency_hz = spec.minimum_frequency_hz if isinstance(spec, (BasicPitchSpec, PyinSpec)) else None
    maximum_frequency_hz = spec.maximum_frequency_hz if isinstance(spec, (BasicPitchSpec, PyinSpec)) else None
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


def _fitted_beat_period_s(spec: "DrumScriptSpec | AdtofSpec") -> float | None:
    """The one beat period to author drum events against, or None.

    Only a constant grid has one: `tempo_map_reference` returns a map exactly
    when the tempo stage found the song *piecewise*, and there the detected
    per-beat variation is the intended timeline. See `vgt.drum_grid` for why
    the fitted period beats the individually noisy `beat_times` array.
    """
    if spec.tempo_map is not None or not spec.midi_tempo or spec.midi_tempo <= 0:
        return None
    return 60.0 / spec.midi_tempo


def seconds_to_quarter_notes(seconds: float, tempo_bpm: float, tempo_map: TempoMapReference | None = None) -> float:
    """Map a reference-relative second to REAPER's project QN coordinate.

    REAPER's tempo markers are step changes (not MIDI tempo events from the
    imported file), so integrate each effective BPM segment exactly.  With no
    map this deliberately retains #193's constant-tempo conversion.
    """
    if tempo_map is None:
        return seconds * tempo_bpm / 60.0
    qn = 0.0
    cursor = 0.0
    bpm = tempo_map.bpm
    for marker_time, marker_bpm in tempo_map.spans:
        if marker_time <= cursor:
            bpm = marker_bpm
            continue
        if seconds <= marker_time:
            return qn + (seconds - cursor) * bpm / 60.0
        qn += (marker_time - cursor) * bpm / 60.0
        cursor, bpm = marker_time, marker_bpm
    return qn + (seconds - cursor) * bpm / 60.0


def _write_midi(
    path: Path, notes: list[tuple[float, float, int, int]], tempo_bpm: float, *, channel: int = 0,
    tempo_map: TempoMapReference | None = None,
) -> None:
    """Write a minimal, valid single-track Standard MIDI File (format 0)
    containing `notes`, with no external MIDI library (none is a vgt
    dependency, and this issue must add none)."""
    ticks_per_beat = 480
    tempo_uspb = int(round(60_000_000 / tempo_bpm)) if tempo_bpm else 500_000

    raw_events: list[tuple[int, bytes]] = []
    for start_s, end_s, pitch, velocity in notes:
        start_tick = int(round(seconds_to_quarter_notes(start_s, tempo_bpm, tempo_map) * ticks_per_beat))
        end_tick = max(start_tick + 1, int(round(seconds_to_quarter_notes(end_s, tempo_bpm, tempo_map) * ticks_per_beat)))
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

        if isinstance(spec, (DrumScriptSpec, AdtofSpec)):
            instruments = tuple(DRUMSCRIPT_INSTRUMENTS)
            event_count = 4
            events = [
                {"time_sec": round(index * 0.5, 6), "instruments": [instruments[_content_seed(source, spec, f"event-{index}") % len(instruments)]]}
                for index in range(event_count)
            ]
            midi_path = destination_dir / "transcription.mid"
            events_path = destination_dir / "transcription.json"
            tempo_bpm = spec.midi_tempo or 120.0
            # Same seam as the real backend, so the offline suite exercises
            # grid reconciliation rather than a path that skips it.
            events, _reconciliation = reconcile_event_times(
                events, beat_grid=spec.beat_grid, beat_period_s=_fitted_beat_period_s(spec)
            )
            if isinstance(spec, AdtofSpec) or spec.cleanup_profile == "default":
                drum_notes = [
                    (event["time_sec"], event["time_sec"] + 0.1, DRUMSCRIPT_INSTRUMENTS[event["instruments"][0]], 100)
                    for event in events
                ]
                _write_midi(midi_path, drum_notes, tempo_bpm, channel=9, tempo_map=spec.tempo_map)
                events_path.write_text(json.dumps(events), encoding="utf-8")
                final_events = events
            else:
                # No real audio to analyze offline, so the fake backend always
                # exercises `drums-clean`'s "evidence unavailable" fallback
                # path -- deterministic role-default velocities, no timing
                # change, no suppression.
                cleanup_profile = DRUM_CLEANUP_PROFILES[spec.cleanup_profile]
                cleaned = apply_drum_cleanup(
                    events, profile=cleanup_profile, evidence_source=NullOnsetEvidenceSource(), beat_grid=spec.beat_grid
                )
                notes = cleaned_events_to_midi_notes(cleaned, instrument_pitch=DRUMSCRIPT_INSTRUMENTS)
                _write_midi(midi_path, notes, tempo_bpm, channel=9, tempo_map=spec.tempo_map)
                json_events = cleaned_events_to_json(cleaned)
                events_path.write_text(json.dumps(json_events), encoding="utf-8")
                final_events = [event for event in json_events if not event["cleanup"]["suppressed"]]
            counts = {name: sum(name in event["instruments"] for event in final_events) for name in instruments}
            counts = {name: count for name, count in counts.items() if count}
            return TranscriptionResult(
                note_count=len(final_events), pitch_range_midi=None, first_note_s=None, last_note_s=None,
                midi_path=midi_path, events_path=events_path, instrument_counts=counts,
                event_count=len(final_events), first_event_s=final_events[0]["time_sec"] if final_events else None,
                last_event_s=final_events[-1]["time_sec"] if final_events else None, midi_tempo=tempo_bpm,
            )

        notes = _fake_notes(source, spec)
        midi_path = destination_dir / "transcription.mid"
        notes_path = destination_dir / "transcription.csv"
        tempo_bpm = spec.midi_tempo if isinstance(spec, (BasicPitchSpec, PyinSpec)) else None
        tempo_bpm = tempo_bpm or 120.0
        _write_midi(midi_path, notes, tempo_bpm, tempo_map=spec.tempo_map)
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

    def detect_raw(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> RawDetectionResult:
        """Fake counterpart to `BasicPitchTranscriber.detect_raw`: fabricates
        the same deterministic notes `transcribe()` would, but returns them
        pre-cleanup so a caller can derive several cleanup variants from one
        fake "inference" (see transcription_variants.py). DrumScript has no
        raw/derived split (see `RawDetectionResult`'s docstring)."""
        if isinstance(spec, (DrumScriptSpec, AdtofSpec)):
            raise TranscriptionError("FakeTranscriber.detect_raw does not support DrumScriptSpec; drums have no raw/derived split")
        emit = progress or (lambda _message: None)
        emit(f"transcribing (fake): {source.name}")
        destination_dir.mkdir(parents=True, exist_ok=True)

        raw_notes = _fake_notes(source, spec)
        midi_path = destination_dir / "transcription.mid"
        notes_path = destination_dir / "transcription.csv"
        tempo_bpm = spec.midi_tempo or 120.0
        _write_midi(midi_path, raw_notes, tempo_bpm, tempo_map=spec.tempo_map)
        _write_notes_csv(notes_path, raw_notes, source, spec)
        # Re-parse from the file just written, rather than re-deriving
        # `ParsedNote`s from the in-memory tuples, so the returned notes
        # (including each one's fabricated pitch-bend series) exactly match
        # what a later reader of the raw CSV would see -- the same contract
        # `BasicPitchTranscriber.detect_raw` gives a real backend's output.
        return RawDetectionResult(
            notes=parse_notes_csv(notes_path), raw_midi_path=midi_path, raw_notes_path=notes_path, midi_tempo=spec.midi_tempo
        )


class FakeAdtofTranscriber(FakeTranscriber):
    """Offline ADTOF stand-in with the same output contract as the real path.

    It intentionally fabricates final, post-processed events rather than
    activations: the focused post-processing tests cover the DSP itself, while
    this seam keeps lifecycle tests entirely local (and, importantly, free of
    a Torch import).  Do not delegate to :class:`FakeTranscriber` here: that
    fake models DrumScript's larger instrument vocabulary, whereas ADTOF can
    only emit its five documented class-to-GM primary members.
    """

    name = "adtof"

    def transcribe(
        self, source: Path, destination_dir: Path, spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        if not isinstance(spec, AdtofSpec):
            raise TranscriptionError("FakeAdtofTranscriber requires an AdtofSpec")
        emit = progress or (lambda _message: None)
        emit(f"transcribing (fake-adtof): {source.name}")
        destination_dir.mkdir(parents=True, exist_ok=True)

        instruments = tuple(instrument for instrument, _pitch in ADTOF_GM_INSTRUMENTS.values())
        events = [
            {
                "time_sec": round(index * 0.5, 6),
                "instruments": [instruments[_content_seed(source, spec, f"adtof-event-{index}") % len(instruments)]],
            }
            for index in range(4)
        ]
        notes = [
            (
                event["time_sec"], event["time_sec"] + ADTOF_NOTE_DURATION_SECONDS,
                ADTOF_GM_INSTRUMENTS_INV[event["instruments"][0]],
                100,
            )
            for event in events
        ]
        midi_path = destination_dir / "transcription.mid"
        events_path = destination_dir / "transcription.json"
        tempo_bpm = spec.midi_tempo or 120.0
        _write_midi(midi_path, notes, tempo_bpm, channel=9, tempo_map=spec.tempo_map)
        _validate_drumscript_midi(midi_path)
        events_path.write_text(json.dumps(events), encoding="utf-8")
        counts = {instrument: sum(instrument in event["instruments"] for event in events) for instrument in instruments}
        emit(f"transcribed (fake-adtof): {len(events)} events")
        return TranscriptionResult(
            note_count=len(notes), pitch_range_midi=None, first_note_s=None, last_note_s=None,
            midi_path=midi_path, events_path=events_path,
            instrument_counts={instrument: count for instrument, count in counts.items() if count},
            event_count=len(events), first_event_s=events[0]["time_sec"], last_event_s=events[-1]["time_sec"],
            backend_tempo=None, midi_tempo=spec.midi_tempo,
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


def _note_comparison_metrics(notes: list[ParsedNote]) -> tuple[float | None, int | None]:
    """Return the two comparison metrics exposed for retained variants.

    End events sort before starts at the same timestamp: a note ending at an
    onset does not overlap the newly starting note.  This is also the
    convention used by the voice-cap cleanup stage.
    """
    if not notes:
        return None, None
    events = sorted(
        ((note.start_s, 1) for note in notes),
        key=lambda event: (event[0], event[1]),
    )
    events.extend((note.end_s, -1) for note in notes)
    events.sort(key=lambda event: (event[0], event[1]))
    active = maximum = 0
    for _time, delta in events:
        active += delta
        maximum = max(maximum, active)
    return max(note.end_s - note.start_s for note in notes), maximum


def _merge_fragments(notes: list[ParsedNote], max_gap_s: float, merge_touching: bool = True) -> list[ParsedNote]:
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

    `merge_touching=False` excludes the zero-gap case, and exists for the
    `pyin` pipeline: that backend emits two same-pitch notes sharing an exact
    boundary *only* where it deliberately cut a re-articulation (see
    `pyin_notes.segment_notes`), so merging them undoes the split rather than
    repairing anything. Left on for Basic Pitch, whose touching same-pitch
    notes are genuine fragmentation. Measured on the 7Rivers bass stem this is
    not a nicety: unguarded, this stage put every split note back and the
    change scored exactly the same as no change at all.
    """
    by_pitch: dict[int, list[ParsedNote]] = {}
    for note in notes:
        by_pitch.setdefault(note.pitch_midi, []).append(note)

    merged: list[ParsedNote] = []
    for pitch_notes in by_pitch.values():
        pitch_notes.sort(key=lambda note: note.start_s)
        current = pitch_notes[0]
        for candidate in pitch_notes[1:]:
            gap_s = candidate.start_s - current.end_s
            if gap_s <= max_gap_s and (merge_touching or gap_s > 0):
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


class _SpectralAnalysis:
    """One stem's magnitude spectrogram, computed once per `transcribe()` call
    and reused for every candidate ghost/parent pair `_drop_harmonic_ghosts`
    checks (see that function's docstring and
    docs/guitar-transcription-findings.md's spectral-confirmation section).

    Deliberately not a `@dataclass(frozen=True)`: the arrays it wraps are not
    hashable/comparable in the way the rest of this module's frozen value
    types are, and nothing here needs equality or immutability -- only
    `_load_spectral_analysis` constructs one, and it is discarded at the end
    of `_apply_cleanup_stages`.
    """

    def __init__(self, magnitude: Any, freqs: Any, times: Any) -> None:
        self.magnitude = magnitude  # (n_freq_bins, n_frames)
        self.freqs = freqs  # Hz, one per magnitude row
        self.times = times  # seconds, one per magnitude column


def _load_spectral_analysis(
    source: Path, *, n_fft: int = GUITAR_GHOST_SPECTRAL_N_FFT, hop_length: int = GUITAR_GHOST_SPECTRAL_HOP_LENGTH
) -> _SpectralAnalysis:
    """Load `source` and compute its STFT magnitude once, lazily importing
    librosa exactly as `sections.py`'s `_librosa_sections` does -- vgt never
    depends on librosa at import time, only when a stage actually needs it."""
    import librosa
    import numpy as np

    try:
        y, sr = librosa.load(str(source), sr=None, mono=True)
    except Exception as exc:  # librosa raises varied backend-specific errors
        raise TranscriptionError(f"could not load {source} for spectral ghost confirmation: {exc}") from exc
    stft = librosa.stft(y, n_fft=n_fft, hop_length=hop_length)
    magnitude = np.abs(stft)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    times = librosa.frames_to_time(np.arange(magnitude.shape[1]), sr=sr, hop_length=hop_length)
    return _SpectralAnalysis(magnitude=magnitude, freqs=freqs, times=times)


def _midi_to_hz(pitch_midi: int) -> float:
    return 440.0 * 2.0 ** ((pitch_midi - 69) / 12.0)


def _harmonic_order(interval_semitones: int) -> int:
    """The parent-harmonic order a `interval_semitones` gap above the parent's
    fundamental coincides with (12 semitones -> 2nd harmonic/octave, 19 ->
    3rd/12th, 24 -> 4th/2 octaves, ...) -- an equal-tempered interval only
    approximates a harmonic's true just ratio, so this rounds to the nearest
    integer harmonic rather than requiring an exact match."""
    return max(1, round(2.0 ** (interval_semitones / 12.0)))


def _spectral_band_amplitude(
    spectral: _SpectralAnalysis, freq_hz: float, start_s: float, end_s: float, freq_tolerance_semitones: float
) -> float:
    """Peak magnitude within `freq_tolerance_semitones` of `freq_hz`, over the
    `[start_s, end_s)` time window. Peak (not mean) picking tolerates the STFT
    bin grid not landing exactly on `freq_hz`."""
    import numpy as np

    lo = freq_hz * 2.0 ** (-freq_tolerance_semitones / 12.0)
    hi = freq_hz * 2.0 ** (freq_tolerance_semitones / 12.0)
    freq_mask = (spectral.freqs >= lo) & (spectral.freqs <= hi)
    if not freq_mask.any():
        freq_mask = np.zeros_like(spectral.freqs, dtype=bool)
        freq_mask[int(np.argmin(np.abs(spectral.freqs - freq_hz)))] = True

    frame_lo = int(np.searchsorted(spectral.times, start_s, side="left"))
    frame_hi = int(np.searchsorted(spectral.times, end_s, side="right"))
    frame_hi = min(max(frame_hi, frame_lo + 1), spectral.magnitude.shape[1])
    frame_lo = min(frame_lo, frame_hi - 1)
    if frame_lo < 0 or frame_hi <= frame_lo:
        return 0.0

    window = spectral.magnitude[np.ix_(freq_mask, np.arange(frame_lo, frame_hi))]
    return float(window.max()) if window.size else 0.0


def _ghost_has_independent_energy(
    spectral: _SpectralAnalysis,
    ghost: ParsedNote,
    parent: ParsedNote,
    *,
    max_harmonic_order: int,
    freq_tolerance_semitones: float,
    independent_energy_ratio: float,
) -> bool:
    """The "collapsing harmonics" spectral check: does `ghost`'s fundamental
    carry energy beyond what `parent`'s own harmonic series already predicts
    there?

    Fits a log-linear decay curve to `parent`'s *other* visible harmonics
    (excluding the one `ghost`'s pitch coincides with) over their shared
    overlap window, then compares the measured amplitude at `ghost`'s
    fundamental against that curve's prediction at its harmonic order.
    Amplitude well above the prediction means something independent is
    sounding there -- `ghost` is a real note, not a partial. Whenever the
    audio can't settle the question (no overlap window, or too few of
    `parent`'s other harmonics are visible to fit a curve), this returns
    `True` conservatively: absence of evidence here must never *add* a drop,
    only withhold one the heuristic already decided.
    """
    import numpy as np

    start_s = max(ghost.start_s, parent.start_s)
    end_s = min(ghost.end_s, parent.end_s)
    if end_s <= start_s:
        return True

    ghost_order = _harmonic_order(ghost.pitch_midi - parent.pitch_midi)
    parent_fundamental_hz = _midi_to_hz(parent.pitch_midi)

    orders, amplitudes = [], []
    for order in range(1, max_harmonic_order + 1):
        if order == ghost_order:
            continue
        amplitude = _spectral_band_amplitude(
            spectral, parent_fundamental_hz * order, start_s, end_s, freq_tolerance_semitones
        )
        if amplitude > 0.0:
            orders.append(order)
            amplitudes.append(amplitude)
    if len(orders) < 2:
        return True

    slope, intercept = np.polyfit(orders, np.log(amplitudes), 1)
    predicted_amplitude = float(np.exp(slope * ghost_order + intercept))
    measured_amplitude = _spectral_band_amplitude(
        spectral, parent_fundamental_hz * ghost_order, start_s, end_s, freq_tolerance_semitones
    )
    return measured_amplitude > predicted_amplitude * independent_energy_ratio


def _drop_harmonic_ghosts(
    notes: list[ParsedNote],
    intervals: tuple[int, ...],
    onset_tolerance_s: float,
    overlap_fraction: float,
    velocity_slack: float,
    spectral_max_harmonic_order: int = GUITAR_GHOST_SPECTRAL_MAX_HARMONIC_ORDER,
    spectral_freq_tolerance_semitones: float = GUITAR_GHOST_SPECTRAL_FREQ_TOLERANCE_SEMITONES,
    spectral_independent_energy_ratio: float = GUITAR_GHOST_SPECTRAL_INDEPENDENT_ENERGY_RATIO,
    # Accepted (not read here) purely so the STFT size/hop length that
    # `_apply_cleanup_stages` already consumed to build `spectral` remain
    # part of this stage's `params` -- and therefore part of `settings_hash`
    # (see docs/transcription-variants-plan.md's settings-identity section).
    # `spectral`'s own resolution already reflects whichever values were used.
    spectral_n_fft: int = GUITAR_GHOST_SPECTRAL_N_FFT,
    spectral_hop_length: int = GUITAR_GHOST_SPECTRAL_HOP_LENGTH,
    spectral: _SpectralAnalysis | None = None,
) -> list[ParsedNote]:
    """Drop a note that is almost certainly the acoustic partial of a louder,
    lower note already sounding underneath it.

    A note is *flagged* when another note sits a harmonic interval
    (`intervals`) below it, started at essentially the same instant, covers
    most of its duration, and isn't quieter -- a real independent note at a
    harmonic interval (e.g. an intentional octave) will usually fail at least
    one of these and survive.

    When `spectral` is given (the stem's audio was loaded, see
    `_apply_cleanup_stages`), a flagged note is only actually dropped once the
    spectral confirmation gate (`_ghost_has_independent_energy`) also agrees
    there's no independent energy at its fundamental -- this can only *retain*
    a note the heuristic alone would have wrongly dropped, never drop one the
    heuristic would have kept. Without `spectral` (no source stem available,
    e.g. `FakeTranscriber` or a non-guitar target), behaviour is unchanged
    from the heuristic alone.
    """
    kept: list[ParsedNote] = []
    for note in notes:
        is_ghost = False
        ghost_parent: ParsedNote | None = None
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
                ghost_parent = other
                break
        if is_ghost and spectral is not None and ghost_parent is not None:
            if _ghost_has_independent_energy(
                spectral,
                note,
                ghost_parent,
                max_harmonic_order=spectral_max_harmonic_order,
                freq_tolerance_semitones=spectral_freq_tolerance_semitones,
                independent_energy_ratio=spectral_independent_energy_ratio,
            ):
                is_ghost = False
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


def _force_monophony(notes: list[ParsedNote]) -> list[ParsedNote]:
    """Leave at most one note sounding at every instant.

    At an overlap, the winner is chosen deterministically by higher velocity,
    then earlier onset, then lower pitch.  A loser that was already sounding
    is truncated at the winner's onset rather than dropped, preserving its
    rhythmic onset in the reference.  A losing new onset is dropped (including
    an exact-onset tie), because truncating it at its own start would make a
    zero-length event.  Bass is a single-line source, so every such overlap is
    a detection artifact; this must not be used for LALAL vocals, whose stacked
    backing vocals and harmonies are genuinely polyphonic.
    """
    # Process each onset chronologically, with the same priority ordering for
    # exact ties.  `ends` is mutable state so a retired note never becomes
    # active again during a later comparison.
    order = sorted(
        range(len(notes)),
        key=lambda index: (
            notes[index].start_s,
            -notes[index].velocity,
            notes[index].pitch_midi,
            index,
        ),
    )
    ends = [note.end_s for note in notes]
    kept = [True] * len(notes)
    active: int | None = None

    def priority(index: int) -> tuple[int, float, int]:
        note = notes[index]
        return (-note.velocity, note.start_s, note.pitch_midi)

    for index in order:
        start = notes[index].start_s
        if active is not None and ends[active] <= start:
            active = None
        if active is None:
            active = index
        elif priority(index) < priority(active):
            ends[active] = start
            active = index
        else:
            kept[index] = False

    return [
        replace(note, end_s=ends[index])
        for index, note in enumerate(notes)
        if kept[index] and ends[index] > note.start_s
    ]


_CLEANUP_STAGE_FUNCTIONS: dict[str, Callable[..., list[ParsedNote]]] = {
    "merge_fragments": _merge_fragments,
    "drop_isolated_notes": _drop_isolated_notes,
    "clamp_sustain": _clamp_sustain,
    "drop_harmonic_ghosts": _drop_harmonic_ghosts,
    "cap_simultaneous_voices": _cap_simultaneous_voices,
    "force_monophony": _force_monophony,
}


def _apply_cleanup_stages(
    notes: list[ParsedNote],
    spec: NoteSpec,
    source: Path | None = None,
    spectral_cache: dict[tuple[int, int], _SpectralAnalysis] | None = None,
) -> list[ParsedNote]:
    """Run `spec.cleanup`'s ordered stages, dispatching each by its tag name.

    Stage order and each stage's parameters are a property of the profile
    that built `spec.cleanup` (see `_GUITAR_ACOUSTIC_PROFILE`'s docstring for
    why the guitar pipeline's order is load-bearing) -- this executor only
    walks the list `default_spec_for_target` already resolved.

    `source` is the stem audio `transcribe()` already has in hand (the same
    file just fed to Basic Pitch); it is only ever read here, lazily and at
    most once per `(n_fft, hop_length)` key, the first time a
    `drop_harmonic_ghosts` stage needs it, so a target whose cleanup never
    includes that stage never imports librosa or touches the audio a second
    time. Every other stage's signature is untouched by this -- only
    `drop_harmonic_ghosts` accepts a `spectral` keyword.

    `spectral_cache`, when given, is a caller-owned dict this function reads
    and writes in place: a caller deriving several variants from the same
    source in one reconciliation run (see transcription_variants.py) passes
    the same dict across every call so the STFT is computed at most once per
    `(n_fft, hop_length)` configuration for the whole run, not once per
    variant. Defaults to a fresh, call-local dict, preserving the old
    once-per-call behaviour for every other caller.
    """
    cache = spectral_cache if spectral_cache is not None else {}
    for stage in spec.cleanup:
        kwargs = dict(stage.params)
        if stage.name == "drop_harmonic_ghosts" and source is not None:
            n_fft = kwargs.get("spectral_n_fft", GUITAR_GHOST_SPECTRAL_N_FFT)
            hop_length = kwargs.get("spectral_hop_length", GUITAR_GHOST_SPECTRAL_HOP_LENGTH)
            key = (n_fft, hop_length)
            if key not in cache:
                cache[key] = _load_spectral_analysis(source, n_fft=n_fft, hop_length=hop_length)
            kwargs["spectral"] = cache[key]
        notes = _CLEANUP_STAGE_FUNCTIONS[stage.name](notes, **kwargs)
    return notes


def raw_notes_content_hash(path: Path) -> str:
    """Content hash of one raw Basic Pitch note-events CSV -- the "raw
    note-event content hash" half of a derived variant's identity (see
    docs/transcription-variants-plan.md's "Layer 2: derived variant"
    section). Hashing the authoritative CSV bytes, not a re-derived notes
    list, means the hash matches exactly what a later cleanup derivation
    actually reads."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def derive_variant_artifacts(
    raw_notes: list[ParsedNote],
    spec: NoteSpec,
    *,
    midi_path: Path,
    notes_path: Path,
    source: Path | None = None,
    spectral_cache: dict[tuple[int, int], _SpectralAnalysis] | None = None,
) -> TranscriptionResult:
    """Derive one cleanup variant's final MIDI/CSV from `raw_notes` -- already
    parsed once by a raw detection run shared across every variant in its
    detection group -- at a variant-scoped destination, without invoking a
    backend (see docs/transcription-variants-plan.md's two-level cache).
    Always writes both files at `midi_path`/`notes_path`, unlike
    `BasicPitchTranscriber.transcribe`'s in-place conditional rewrite: each
    derived variant owns its own destination, so there is no "unchanged raw
    file" case to skip."""
    notes = _apply_cleanup_stages(raw_notes, spec, source=source, spectral_cache=spectral_cache) if spec.cleanup else list(raw_notes)
    midi_path.parent.mkdir(parents=True, exist_ok=True)
    _write_parsed_notes_csv(notes_path, notes)
    midi_notes = [(note.start_s, note.end_s, note.pitch_midi, note.velocity) for note in notes]
    _write_midi(midi_path, midi_notes, spec.midi_tempo or 120.0, tempo_map=spec.tempo_map)
    note_count, pitch_range_midi, first_note_s, last_note_s = _summarize_notes(notes)
    max_note_duration_s, max_simultaneous_voices = _note_comparison_metrics(notes)
    return TranscriptionResult(
        note_count=note_count,
        pitch_range_midi=pitch_range_midi,
        first_note_s=first_note_s,
        last_note_s=last_note_s,
        midi_path=midi_path,
        notes_path=notes_path,
        max_note_duration_s=max_note_duration_s,
        max_simultaneous_voices=max_simultaneous_voices,
    )


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

    def detect_raw(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> RawDetectionResult:
        """Run Basic Pitch and return its raw, pre-cleanup note events --
        the split half of `transcribe()` a detection-group cache can share
        across every variant that needs the same inference (see
        transcription_variants.py). Never applies `spec.cleanup`; a caller
        wanting a fully derived result calls `derive_variant_artifacts` on
        the returned notes, exactly as `transcribe()` does below."""
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
        return RawDetectionResult(notes=notes, raw_midi_path=midi_path, raw_notes_path=notes_path, midi_tempo=spec.midi_tempo)

    def transcribe(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        if not isinstance(spec, BasicPitchSpec):
            raise TranscriptionError("BasicPitchTranscriber requires a BasicPitchSpec")
        raw = self.detect_raw(source, destination_dir, spec, progress)
        notes = raw.notes
        notes_changed = False

        if spec.cleanup:
            cleaned = _apply_cleanup_stages(notes, spec, source=source)
            if cleaned != notes:
                _write_parsed_notes_csv(raw.raw_notes_path, cleaned)
                notes = cleaned
                notes_changed = True

        # Basic Pitch writes its MIDI using one tempo metadata value.  For a
        # variable project map its CSV remains the authoritative real-second
        # event list, so re-author the MIDI from it.  Constant-map direct
        # calls retain Basic Pitch's original file byte-for-byte.
        if notes_changed or spec.tempo_map is not None:
            midi_notes = [(note.start_s, note.end_s, note.pitch_midi, note.velocity) for note in notes]
            _write_midi(raw.raw_midi_path, midi_notes, spec.midi_tempo or 120.0, tempo_map=spec.tempo_map)

        note_count, pitch_range_midi, first_note_s, last_note_s = _summarize_notes(notes)
        max_note_duration_s, max_simultaneous_voices = _note_comparison_metrics(notes)
        emit = progress or (lambda _message: None)
        emit(f"transcribed (basic-pitch): {note_count} notes")

        return TranscriptionResult(
            note_count=note_count,
            pitch_range_midi=pitch_range_midi,
            first_note_s=first_note_s,
            last_note_s=last_note_s,
            midi_path=raw.raw_midi_path,
            notes_path=raw.raw_notes_path,
            max_note_duration_s=max_note_duration_s,
            max_simultaneous_voices=max_simultaneous_voices,
        )


class PyinTranscriber:
    """Monophonic pitch-tracking backend (see `vgt.pyin_notes`).

    Unlike Basic Pitch and DrumScript this runs in-process: librosa is already
    a hard vgt dependency, so there is no isolated interpreter to pin and no
    subprocess to degrade. Failure is still per target -- unreadable audio or a
    tracker error raises `TranscriptionError`, so the orchestrator marks just
    this target and continues.

    It implements `detect_raw` as well as `transcribe` so a monophonic variant
    joins the same two-level cache as a Basic Pitch one: the F0 track is by far
    the expensive part, and retuning only `cleanup` must not re-run it.
    """

    name = "pyin"

    def detect_raw(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> RawDetectionResult:
        emit = progress or (lambda _message: None)
        if not isinstance(spec, PyinSpec):
            raise TranscriptionError("PyinTranscriber requires a PyinSpec")
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TranscriptionError(f"could not prepare {destination_dir}: {exc}") from exc

        emit(f"transcribing (pyin): {source.name}")
        from .pyin_notes import transcribe_monophonic

        try:
            raw_notes = transcribe_monophonic(
                str(source),
                sample_rate_hz=spec.sample_rate_hz,
                frame_length=spec.frame_length,
                hop_length=spec.hop_length,
                minimum_frequency_hz=spec.minimum_frequency_hz,
                maximum_frequency_hz=spec.maximum_frequency_hz,
                median_filter_frames=spec.median_filter_frames,
                minimum_note_length_ms=spec.minimum_note_length_ms,
                rearticulation_rise_db=spec.rearticulation_rise_db,
                rearticulation_span_frames=spec.rearticulation_span_frames,
                rearticulation_minimum_spacing_s=(
                    spec.rearticulation_minimum_spacing_beats * 60.0 / (spec.midi_tempo or 120.0)
                ),
            )
        except TranscriptionError:
            raise
        except Exception as exc:  # librosa/soundfile raise a wide range of errors
            raise TranscriptionError(f"pyin failed on {source.name}: {exc}") from exc

        notes = [
            ParsedNote(start_s=start, end_s=end, pitch_midi=pitch, velocity=velocity, pitch_bend=())
            for start, end, pitch, velocity in raw_notes
        ]
        midi_path = destination_dir / "transcription.mid"
        notes_path = destination_dir / "transcription.csv"
        _write_parsed_notes_csv(notes_path, notes)
        _write_midi(midi_path, raw_notes, spec.midi_tempo or 120.0, tempo_map=spec.tempo_map)
        emit(f"detected (pyin): {len(notes)} notes")
        return RawDetectionResult(
            notes=notes, raw_midi_path=midi_path, raw_notes_path=notes_path, midi_tempo=spec.midi_tempo
        )

    def transcribe(
        self,
        source: Path,
        destination_dir: Path,
        spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        if not isinstance(spec, PyinSpec):
            raise TranscriptionError("PyinTranscriber requires a PyinSpec")
        raw = self.detect_raw(source, destination_dir, spec, progress)
        return derive_variant_artifacts(
            raw.notes, spec, midi_path=raw.raw_midi_path, notes_path=raw.raw_notes_path
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
                raw_events = parse_drumscript_events(events_source)
                _validate_event_times(raw_events, _source_duration_seconds(source))
                # Before either profile: DrumScript quantizes onto its own
                # beat tracker's grid, anchored at 0.0 and at its own tempo,
                # so its "absolute seconds" start at the item edge instead of
                # the first beat and drift from there (see `vgt.drum_grid`).
                raw_events, reconciliation = reconcile_event_times(
                    raw_events, beat_grid=spec.beat_grid, beat_period_s=_fitted_beat_period_s(spec)
                )
                if reconciliation is not None:
                    emit(f"drum events {reconciliation.describe()}")
                destination_dir.mkdir(parents=True, exist_ok=True)
                midi_path = destination_dir / "transcription.mid"
                events_path = destination_dir / "transcription.json"
                tempo_bpm = spec.midi_tempo or 120.0
                if spec.cleanup_profile == "default":
                    # Re-author at the project tempo rather than byte-copying
                    # DrumScript's MIDI (issue #193): DrumScript's own tempo
                    # detection is unreliable (a half-tempo octave error on
                    # 7Rivers authored 60 BPM against a 120 BPM project), and
                    # REAPER replays every drum item on the project tempo
                    # grid, so a note authored at DrumScript's tempo lands at
                    # the wrong real second. The event JSON's onsets are
                    # already correct real seconds; only velocity has to be
                    # recovered from DrumScript's MIDI, since its event JSON
                    # doesn't carry it (see `parse_drumscript_events`).
                    velocities = _read_percussion_note_velocities(midi_source)
                    notes = [
                        (
                            event["time_sec"],
                            event["time_sec"] + 0.1,
                            DRUMSCRIPT_INSTRUMENTS[instrument],
                            _velocity_near(velocities, DRUMSCRIPT_INSTRUMENTS[instrument], event["time_sec"]),
                        )
                        for event in raw_events
                        for instrument in event["instruments"]
                    ]
                    _write_midi(midi_path, notes, tempo_bpm, channel=9, tempo_map=spec.tempo_map)
                    # Serialized from the events the MIDI was authored from,
                    # not byte-copied from DrumScript's file: after grid
                    # reconciliation the two would otherwise disagree, and
                    # every consumer scores the JSON against the MIDI.
                    events_path.write_text(json.dumps(raw_events), encoding="utf-8")
                    final_events = raw_events
                else:
                    cleanup_profile = DRUM_CLEANUP_PROFILES[spec.cleanup_profile]
                    cleaned = apply_drum_cleanup(
                        raw_events, profile=cleanup_profile, evidence_source=AudioOnsetEvidenceSource(source), beat_grid=spec.beat_grid
                    )
                    notes = cleaned_events_to_midi_notes(cleaned, instrument_pitch=DRUMSCRIPT_INSTRUMENTS)
                    _write_midi(midi_path, notes, tempo_bpm, channel=9, tempo_map=spec.tempo_map)
                    json_events = cleaned_events_to_json(cleaned)
                    events_path.write_text(json.dumps(json_events), encoding="utf-8")
                    final_events = [event for event in json_events if not event["cleanup"]["suppressed"]]
            except (OSError, TranscriptionError) as exc:
                # The work directory is intentionally not useful to callers:
                # it is deleted at scope exit and must never enter the sidecar.
                raise TranscriptionError(_without_temporary_path(str(exc), work_dir)) from exc

        counts: dict[str, int] = {}
        for event in final_events:
            for instrument in event["instruments"]:
                counts[instrument] = counts.get(instrument, 0) + 1
        times = [event["time_sec"] for event in final_events]
        emit(f"transcribed (drumscript): {len(final_events)} events")
        return TranscriptionResult(
            note_count=len(final_events),
            pitch_range_midi=None,
            first_note_s=None,
            last_note_s=None,
            midi_path=midi_path,
            events_path=events_path,
            instrument_counts=counts,
            event_count=len(final_events),
            first_event_s=min(times) if times else None,
            last_event_s=max(times) if times else None,
            backend_tempo=_drumscript_backend_tempo(completed),
            midi_tempo=_midi_tempo_bpm(midi_path),
        )


class AdtofActivationRunner:
    """Run pinned ADTOF inference without importing Torch into vgt.

    The only durable artifact this class can create is an explicitly supplied
    cache entry.  The executable helper, model output, and any accidental
    upstream artifacts live under one ``TemporaryDirectory`` and are removed
    before this method returns.  Cache entries are validated both before being
    written and every time they are read.
    """

    def __init__(self, cache_dir: Path | None = None, *, timeout_seconds: int = ADTOF_TIMEOUT_SECONDS) -> None:
        self.cache_dir = cache_dir
        self.timeout_seconds = timeout_seconds

    def run(
        self, source: Path, spec: AdtofSpec, progress: Callable[[str], None] | None = None,
    ) -> AdtofActivationResult:
        if not isinstance(spec, AdtofSpec):
            raise TranscriptionError("AdtofActivationRunner requires an AdtofSpec")
        source = source.resolve()
        if not source.is_file():
            raise TranscriptionError("drum stem is not a readable file")
        source_lock = Path(__file__).with_name(ADTOF_LOCK_FILENAME)
        if _sha256_file(source_lock) != spec.lock_sha256:
            raise TranscriptionError("ADTOF dependency lock does not match the pinned runtime identity")
        stem_hash = _sha256_file(source)
        cache_key = adtof_activation_cache_key(spec, stem_hash)
        cache_path = self.cache_dir / f"{cache_key}.npz" if self.cache_dir is not None else None
        if cache_path is not None and cache_path.is_file():
            try:
                activations, metadata = load_adtof_activation_dump(cache_path, spec)
            except TranscriptionError:
                # A partial/corrupt cache entry must never be consumed.  It is
                # safe to replace this exact content-addressed file.
                cache_path.unlink(missing_ok=True)
            else:
                return AdtofActivationResult(activations, metadata, cache_key, cache_hit=True)

        argv_prefix = _adtof_base_command(spec)
        if shutil.which(argv_prefix[0]) is None:
            raise TranscriptionError(
                f"{argv_prefix[0]!r} is not on PATH; install uv and pre-fetch the pinned ADTOF environment, "
                "including its committed dependency lock"
            )
        emit = progress or (lambda _message: None)
        emit(f"extracting raw activations (adtof): {source.name}")
        with tempfile.TemporaryDirectory(prefix="vgt-adtof-") as temporary:
            work_dir = Path(temporary)
            helper = work_dir / "adtof_inference.py"
            lock = work_dir / ADTOF_LOCK_FILENAME
            output = work_dir / "activations.npz"
            try:
                shutil.copyfile(Path(__file__).with_name("_adtof_subprocess.py"), helper)
                shutil.copyfile(source_lock, lock)
                completed = subprocess.run(
                    build_adtof_argv(source, output, spec, helper, lock), cwd=work_dir,
                    capture_output=True, text=True, timeout=self.timeout_seconds, errors="replace",
                )
            except subprocess.TimeoutExpired as exc:
                raise TranscriptionError(f"ADTOF inference timed out after {exc.timeout}s") from exc
            except OSError as exc:
                raise TranscriptionError(f"failed to run {argv_prefix[0]!r}: {exc}") from exc
            if completed.returncode != 0:
                context = _adtof_process_context(completed, work_dir)
                raise TranscriptionError(
                    "ADTOF inference failed (the pinned package, model, or bundled weights may be missing) "
                    f"with status {completed.returncode}: {context}"
                )
            try:
                activations, metadata = load_adtof_activation_dump(output, spec)
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    # Copying the already validated NPZ preserves its metadata
                    # exactly and keeps cache materialization out of the child.
                    shutil.copy2(output, cache_path)
            except (OSError, TranscriptionError) as exc:
                raise TranscriptionError(_without_temporary_path(str(exc), work_dir)) from exc

        return AdtofActivationResult(activations, metadata, cache_key, cache_hit=False)


def _adtof_peak_frames(activations: Any, fps: float) -> list[tuple[int, int, float]]:
    """Return `(frame, class_index, height)` maxima after per-class IOI.

    This intentionally has no dependency on Torch or scipy.  Ties choose the
    earlier frame, which keeps plateau handling deterministic and lets the
    subsequent grid association make the musical timing decision.
    """
    import numpy as np

    matrix = np.asarray(activations)
    if matrix.ndim != 2 or matrix.shape[1] != len(ADTOF_CLASS_NAMES):
        raise TranscriptionError("ADTOF activations have an unexpected shape")
    if not math.isfinite(fps) or fps <= 0:
        raise TranscriptionError("ADTOF activation metadata has an invalid fps")
    peaks: list[tuple[int, int, float]] = []
    for class_index, class_name in enumerate(ADTOF_CLASS_NAMES):
        column = matrix[:, class_index]
        threshold = ADTOF_PEAK_THRESHOLDS[class_name]
        candidates: list[tuple[int, float]] = []
        for frame, height in enumerate(column):
            if height < threshold:
                continue
            previous = column[frame - 1] if frame else -np.inf
            following = column[frame + 1] if frame + 1 < len(column) else -np.inf
            if height >= previous and height > following:
                candidates.append((frame, float(height)))

        min_frames = max(1, int(math.ceil(ADTOF_MIN_INTER_ONSET_SECONDS[class_name] * fps)))
        selected: list[tuple[int, float]] = []
        # Non-maximum suppression by height makes a close doublet retain the
        # strongest model evidence, while the final sort restores timeline
        # order.  It is more robust than greedily retaining the first blip.
        for frame, height in sorted(candidates, key=lambda item: (-item[1], item[0])):
            if all(abs(frame - chosen_frame) >= min_frames for chosen_frame, _ in selected):
                selected.append((frame, height))
        peaks.extend((frame, class_index, height) for frame, height in selected)
    return sorted(peaks)


def _adtof_grid_times(beat_grid: BeatGridReference | None, first_s: float, last_s: float) -> list[float]:
    """Build the project eighth-note grid covering the candidate peaks."""
    if beat_grid is None or len(beat_grid.beat_times) < 2:
        return []
    beats = sorted({float(time) for time in beat_grid.beat_times if math.isfinite(float(time))})
    intervals = [right - left for left, right in zip(beats, beats[1:]) if right > left]
    if not intervals:
        return []
    period = sorted(intervals)[len(intervals) // 2]
    if period <= 0:
        return []
    # `downbeat_offset_s` explicitly establishes the meter's phase even if a
    # beat tracker omitted that exact time from its list.  The supplied beats
    # otherwise stay authoritative, including any natural tempo variation.
    if beat_grid.downbeat_offset_s is not None and math.isfinite(beat_grid.downbeat_offset_s):
        downbeat = float(beat_grid.downbeat_offset_s)
        if all(abs(time - downbeat) > period * 0.1 for time in beats):
            beats.append(downbeat)
            beats.sort()
    grid: list[float] = []
    for left, right in zip(beats, beats[1:]):
        grid.extend((left, (left + right) / 2.0))
    grid.append(beats[-1])
    # Peaks can fall before/after a tracker-provided range by a tiny tail.
    while grid and grid[0] > first_s:
        grid.insert(0, grid[0] - period / 2.0)
    while grid and grid[-1] < last_s:
        grid.append(grid[-1] + period / 2.0)
    return grid


def postprocess_adtof_activations(
    activations: Any, metadata: Mapping[str, Any], spec: AdtofSpec,
) -> tuple[list[dict[str, Any]], list[tuple[float, float, int, int]]]:
    """Peak-pick validated ADTOF outputs into vgt's drum event/MIDI shapes.

    Times are raw frame times only until this function associates them with
    vgt's project beat grid.  With no usable grid, real frame times are kept
    rather than inventing one; this makes degraded analysis explicit while
    preserving a valid, project-tempo-authored artifact.
    """
    if not isinstance(spec, AdtofSpec):
        raise TranscriptionError("ADTOF post-processing requires an AdtofSpec")
    try:
        fps = float(metadata["fps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TranscriptionError("ADTOF activation metadata has an invalid fps") from exc
    peaks = _adtof_peak_frames(activations, fps)
    grid = _adtof_grid_times(spec.beat_grid, 0.0, max((frame / fps for frame, _, _ in peaks), default=0.0))

    # A same-class pair can quantize to one grid slot.  Retain the larger
    # activation, then combine different classes into DrumScript-compatible
    # simultaneous `{time_sec, instruments}` events.
    selected: dict[tuple[float, str], tuple[float, str]] = {}
    for frame, class_index, height in peaks:
        raw_time = frame / fps
        time_sec = min(grid, key=lambda point: (abs(point - raw_time), point)) if grid else raw_time
        instrument, _pitch = ADTOF_GM_INSTRUMENTS[ADTOF_CLASS_NAMES[class_index]]
        key = (round(time_sec, 6), instrument)
        previous = selected.get(key)
        if previous is None or height > previous[0]:
            selected[key] = (height, instrument)

    grouped: dict[float, list[tuple[str, float]]] = {}
    for (time_sec, _instrument), (height, instrument) in selected.items():
        grouped.setdefault(time_sec, []).append((instrument, height))
    events: list[dict[str, Any]] = []
    midi_notes: list[tuple[float, float, int, int]] = []
    for time_sec in sorted(grouped):
        instruments = sorted(grouped[time_sec], key=lambda item: ADTOF_GM_INSTRUMENTS_INV[item[0]])
        events.append({"time_sec": time_sec, "instruments": [instrument for instrument, _ in instruments]})
        for instrument, height in instruments:
            pitch = ADTOF_GM_INSTRUMENTS_INV[instrument]
            velocity = max(1, min(127, int(round(1 + 126 * max(0.0, min(1.0, height))))))
            midi_notes.append((time_sec, time_sec + ADTOF_NOTE_DURATION_SECONDS, pitch, velocity))
    return events, midi_notes


# Inverse lookup also fixes deterministic instrument ordering in simultaneous
# events.  It intentionally contains only model-observable primary members.
ADTOF_GM_INSTRUMENTS_INV: dict[str, int] = {instrument: pitch for instrument, pitch in ADTOF_GM_INSTRUMENTS.values()}


class AdtofTranscriber:
    """Real ADTOF backend: isolated inference followed by numpy-only vgt DSP."""

    name = "adtof"

    def __init__(self, activation_runner: AdtofActivationRunner | None = None) -> None:
        self.activation_runner = activation_runner or AdtofActivationRunner()

    def transcribe(
        self, source: Path, destination_dir: Path, spec: TranscriptionSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        if not isinstance(spec, AdtofSpec):
            raise TranscriptionError("AdtofTranscriber requires an AdtofSpec")
        emit = progress or (lambda _message: None)
        raw = self.activation_runner.run(source, spec, progress=emit)
        events, notes = postprocess_adtof_activations(raw.activations, raw.metadata, spec)
        destination_dir.mkdir(parents=True, exist_ok=True)
        midi_path = destination_dir / "transcription.mid"
        events_path = destination_dir / "transcription.json"
        tempo_bpm = spec.midi_tempo or 120.0
        _write_midi(midi_path, notes, tempo_bpm, channel=9, tempo_map=spec.tempo_map)
        _validate_drumscript_midi(midi_path)
        events_path.write_text(json.dumps(events), encoding="utf-8")
        counts: dict[str, int] = {}
        for event in events:
            for instrument in event["instruments"]:
                counts[instrument] = counts.get(instrument, 0) + 1
        emit(f"transcribed (adtof): {len(events)} events")
        return TranscriptionResult(
            note_count=len(notes), pitch_range_midi=None, first_note_s=None, last_note_s=None,
            midi_path=midi_path, events_path=events_path, instrument_counts=counts,
            event_count=len(events), first_event_s=events[0]["time_sec"] if events else None,
            last_event_s=events[-1]["time_sec"] if events else None,
            backend_tempo=None, midi_tempo=spec.midi_tempo,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def adtof_activation_cache_key(spec: AdtofSpec, stem_hash: str) -> str:
    """Stable cache identity for raw inference, independent of Phase 3 tuning."""
    identity = {
        "package_pin": spec.package_pin,
        "package_version": spec.package_version,
        "model_version": spec.model_version,
        "weights_version": spec.weights_version,
        "weights_sha256": spec.weights_sha256,
        "runtime_version": spec.runtime_version,
        "torch_version": spec.torch_version,
        "lock_sha256": spec.lock_sha256,
        "stem_sha256": stem_hash,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _adtof_base_command(spec: AdtofSpec) -> list[str]:
    # ``--offline`` is intentional.  The package and bundled checkpoint must
    # be pre-fetched; the complete committed lock makes inference independent
    # of whichever versions happen to be in a machine's uv cache.
    return [
        "uv", "run", "--offline", "--isolated", "--no-project",
        "--python", spec.runtime_version.removeprefix("python=="),
    ]


def build_adtof_argv(source: Path, output: Path, spec: AdtofSpec, helper: Path, lock: Path) -> list[str]:
    """Build the pinned, network-disabled subprocess command without executing it."""
    if spec.backend != "adtof":
        raise TranscriptionError(f"AdtofActivationRunner cannot build argv for backend {spec.backend!r}")
    return [*_adtof_base_command(spec), "--with-requirements", str(lock), "python", str(helper), str(source.resolve()), str(output), spec.lock_sha256]


def load_adtof_activation_dump(path: Path, spec: AdtofSpec) -> tuple[Any, dict[str, Any]]:
    """Load and strictly validate a raw activation NPZ emitted by the child."""
    try:
        import numpy as np
        with np.load(path, allow_pickle=False) as dump:
            if set(dump.files) != {"activations", "metadata"}:
                raise TranscriptionError("ADTOF activation dump must contain only activations and metadata")
            activations = dump["activations"]
            raw_metadata = dump["metadata"]
    except (OSError, ValueError) as exc:
        raise TranscriptionError("could not read ADTOF activation dump") from exc
    try:
        metadata = json.loads(str(raw_metadata.item()))
    except (AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise TranscriptionError("ADTOF activation dump has invalid metadata") from exc
    if not isinstance(metadata, dict):
        raise TranscriptionError("ADTOF activation metadata must be an object")
    if activations.dtype != np.float32 or activations.ndim != 2 or activations.shape[0] < 1:
        raise TranscriptionError("ADTOF activations must be a non-empty float32 [n_frames, n_classes] matrix")
    if activations.shape[1] != len(ADTOF_CLASS_NAMES):
        raise TranscriptionError(f"ADTOF activations have {activations.shape[1]} classes; expected {len(ADTOF_CLASS_NAMES)}")
    if not np.isfinite(activations).all():
        raise TranscriptionError("ADTOF activations contain non-finite values")
    expected = {
        "package_version": spec.package_version,
        "model_version": spec.model_version,
        "weights_sha256": spec.weights_sha256,
        "runtime_version": spec.runtime_version.removeprefix("python=="),
        "torch_version": spec.torch_version.removeprefix("torch=="),
        "lock_sha256": spec.lock_sha256,
        "device": "cpu",
        "sample_rate": 44100,
        "n_fft": 2048,
        "hop_samples": 441,
        "fps": 100,
        "class_names": list(ADTOF_CLASS_NAMES),
    }
    for name, value in expected.items():
        if metadata.get(name) != value:
            raise TranscriptionError(f"ADTOF activation metadata has unexpected {name}")
    return activations, metadata


def _adtof_process_context(completed: Any, work_dir: Path) -> str:
    context = " | ".join(part for part in (_stderr_tail(completed.stderr), _stderr_tail(completed.stdout)) if part)
    return _without_temporary_path(context or "no output captured", work_dir)


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

    For DrumScript's own raw MIDI this is a detected tempo, not a playback
    instruction vgt trusts: DrumScript's beat tracker can make gross octave
    errors, so vgt never authors the drum reference timeline at this value.
    It re-authors drum MIDI at the project's own tempo instead (issue #193);
    `_read_percussion_note_velocities` below still needs this value to decode
    DrumScript's raw ticks back into real seconds when recovering velocity.
    """
    data = path.read_bytes()
    marker = b"\xff\x51\x03"
    index = data.find(marker)
    if index < 0 or index + 6 > len(data):
        return None
    microseconds_per_beat = int.from_bytes(data[index + 3:index + 6], "big")
    return 60_000_000 / microseconds_per_beat if microseconds_per_beat else None


_DEFAULT_DRUM_VELOCITY = 100


def _read_percussion_note_velocities(path: Path) -> dict[int, list[tuple[float, int]]]:
    """Map GM percussion pitch -> that pitch's `(onset_s, velocity)` note-ons,
    decoded using `path`'s own embedded tempo.

    DrumScript's event JSON carries real-second onsets but no velocity (see
    `parse_drumscript_events`); re-authoring the `default` MIDI at the
    project tempo (issue #193) would otherwise silently drop DrumScript's
    velocities, so this reads them back out of its own raw MIDI to carry
    them forward via `_velocity_near`.
    """
    tempo_bpm = _midi_tempo_bpm(path) or 120.0
    data = path.read_bytes()
    header_length = int.from_bytes(data[4:8], "big")
    ticks_per_beat = int.from_bytes(data[12:14], "big") or 480
    seconds_per_tick = 60.0 / (tempo_bpm * ticks_per_beat)
    by_pitch: dict[int, list[tuple[float, int]]] = {}
    index = 8 + header_length
    while index + 8 <= len(data):
        if data[index:index + 4] != b"MTrk":
            break
        length = int.from_bytes(data[index + 4:index + 8], "big")
        track_end = index + 8 + length
        index += 8
        tick = 0
        running_status: int | None = None
        while index < track_end:
            delta, index = _read_varlen(data, index)
            tick += delta
            if index >= track_end:
                break
            status = data[index]
            if status < 0x80:
                if running_status is None:
                    break
                status = running_status
            else:
                index += 1
            if status == 0xFF:
                index += 1
                payload_length, index = _read_varlen(data, index)
                index += payload_length
                running_status = None
            elif status in (0xF0, 0xF7):
                payload_length, index = _read_varlen(data, index)
                index += payload_length
                running_status = None
            elif 0x80 <= status <= 0xEF:
                running_status = status
                data_length = 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
                if (status & 0xF0) == 0x90 and (status & 0x0F) == 9 and data[index + 1] > 0:
                    onset_s = tick * seconds_per_tick
                    by_pitch.setdefault(data[index], []).append((onset_s, data[index + 1]))
                index += data_length
            else:
                break
            if index > track_end:
                break
        index = track_end
    return by_pitch


def _velocity_near(by_pitch: dict[int, list[tuple[float, int]]], pitch: int, time_sec: float, *, tolerance_s: float = 0.05) -> int:
    """The velocity of the closest same-pitch note-on within `tolerance_s`,
    or `_DEFAULT_DRUM_VELOCITY` when none is close enough to trust."""
    candidates = by_pitch.get(pitch)
    if not candidates:
        return _DEFAULT_DRUM_VELOCITY
    onset_s, velocity = min(candidates, key=lambda item: abs(item[0] - time_sec))
    return velocity if abs(onset_s - time_sec) <= tolerance_s else _DEFAULT_DRUM_VELOCITY


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
        # DrumScript's own classifier emits "unknown" deliberately, for an
        # onset it detected but couldn't assign to a skin/metal instrument
        # (see its classify_event/midi_exporter, which likewise drops
        # "unknown" from its own clean score). That is a known, expected
        # label to discard, not an unrecognized one to fail loudly on.
        recognized = [instrument for instrument in instruments if instrument != "unknown"]
        unsupported = sorted(set(recognized).difference(DRUMSCRIPT_INSTRUMENTS))
        if unsupported:
            raise TranscriptionError(f"drumscript event has unsupported instruments: {', '.join(unsupported)}")
        if not recognized:
            continue
        events.append({"time_sec": float(time_sec), "instruments": recognized})
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
