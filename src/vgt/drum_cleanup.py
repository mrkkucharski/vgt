"""Opt-in `drums-clean` post-processing for DrumScript's raw event output
(issue #177).

`default` (DrumScript's raw output, byte-copied straight through) and
`drums-clean` (this module's conservative cleanup pipeline) are peers, not a
before/after pair -- see docs/drums-clean-profile.md for the user-facing
trade-off and the recommendation to retain both variants for comparison.

Design constraints this module exists to uphold (see AGENTS.md's issue #177
acceptance criteria):

- Never assume a fixed rhythmic pattern, a specific number of hits per bar,
  or a repeated groove; never copy a role from an adjacent measure.
- Every stage falls back to the raw DrumScript event/time/no-change when
  local evidence is absent or ambiguous -- "no signal" is never treated as
  "silence" or "wrong".
- Every bound (simultaneity window, alignment window, static offset,
  velocity range) is an explicit, documented, hashable constant -- never an
  implicit correction derived from one reference track.

This module has no dependency on `vgt.transcribe`: it operates purely on the
DrumScript event shape (`{"time_sec": float, "instruments": [str, ...]}`)
and an injected onset-evidence source, so it is independently testable and
introduces no import cycle with the module that constructs `DrumScriptSpec`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

DEFAULT_PROFILE_NAME = "default"
CLEAN_PROFILE_NAME = "drums-clean"

# Bounded windows and thresholds for `drums-clean`. See the module docstring
# and docs/drums-clean-profile.md: each of these is a fixed, documented,
# hash-identity-bearing constant, not a value tuned to one reference track.
CLEAN_SIMULTANEITY_WINDOW_S = 0.008  # coincident-onset coalescing window; observed evidence: intended-simultaneous notes differed by one MIDI tick (~2ms at 480 PPQN/120 BPM) -- this is a generous multiple of that, still well under a 16th note
# A minimum inter-onset distance expressed in beats, rather than an absolute
# number of milliseconds.  These are deliberately only 1/32 of a beat: this
# stage removes detector duplicates, not musically plausible doubles.  A
# usable beat grid is required before it acts, so an unknown/changing tempo
# never turns into an arbitrary time threshold.
CLEAN_DEDUP_MINIMUM_INTER_ONSET_BEATS: dict[str, float] = {
    "kick": 1 / 32,
    "snare": 1 / 32,
    "low_tom": 1 / 32,
    "mid_tom": 1 / 32,
    "high_tom": 1 / 32,
    "hi_hat_closed": 1 / 32,
    "hi_hat_open": 1 / 32,
    "crash": 1 / 64,
    "ride": 1 / 32,
}
CLEAN_ALIGNMENT_WINDOW_S = 0.03  # bounded local search radius for the final audio-aware timing correction
CLEAN_SYSTEMATIC_ALIGNMENT_WINDOW_S = 0.08  # wider evidence search used only to measure a consistent latency before final local alignment
CLEAN_GRID_REFERENCE_WINDOW_S = 0.08  # only grid-near audio peaks contribute to the latency estimate; fills remain governed by audio alone
CLEAN_STATIC_OFFSET_S = 0.0  # explicit, opt-in-only static latency correction; 0.0 means "none" -- see issue #177's "no universal 7Rivers-derived latency offset" requirement
CLEAN_MIN_EVIDENCE_STRENGTH = 0.30  # local evidence below this normalized strength is treated as absent, not weak -- retuned for issue #183's local-prominence normalization (see AudioOnsetEvidenceSource), under which even quiet-but-real hits typically score >=0.3 while true silence scores ~0.0
CLEAN_SUPPRESSION_STRENGTH_THRESHOLD = 0.12  # only suppress an event when local evidence is this weak (reproducible, not a guess) -- unchanged: under the new normalization this still cleanly separates true silence (~0.0) from genuine hits
CLEAN_VELOCITY_FLOOR = 30
CLEAN_VELOCITY_CEILING = 120

# Role-aware bounded velocity fallbacks, used only when local audio evidence
# is unavailable or ambiguous for an event -- replaces the flat velocity 100
# every event previously received (see issue #177's observed-evidence
# section). Keys mirror `vgt.transcribe.DRUMSCRIPT_INSTRUMENTS`; a dedicated
# test asserts the two stay in sync without this module importing that one.
CLEAN_VELOCITY_DEFAULTS: dict[str, int] = {
    "kick": 100,
    "snare": 95,
    "low_tom": 90,
    "mid_tom": 90,
    "high_tom": 90,
    "hi_hat_closed": 70,
    "hi_hat_open": 75,
    "crash": 105,
    "ride": 80,
}

_FALLBACK_DEFAULT_VELOCITY = 90  # for any future DrumScript instrument label not yet in CLEAN_VELOCITY_DEFAULTS


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class DrumCleanupProfile:
    """One drums cleanup recipe's complete, hashable identity.

    `enabled=False` (the `default` profile) means `apply_drum_cleanup` is
    never invoked at all -- DrumScript's raw MIDI/events pass through
    untouched, exactly as before this issue.
    """

    name: str
    enabled: bool
    dedup_minimum_inter_onset_beats: Mapping[str, float]
    simultaneity_window_s: float
    alignment_window_s: float
    systematic_alignment_window_s: float
    grid_reference_window_s: float
    static_offset_s: float
    min_evidence_strength: float
    suppression_strength_threshold: float
    velocity_floor: int
    velocity_ceiling: int
    velocity_defaults: Mapping[str, int]

    def as_identity(self) -> dict[str, Any]:
        """The settings-identity payload a caller folds into its own spec
        hash (see `vgt.transcribe.DrumScriptSpec`) -- every field capable of
        changing cleaned output must appear here."""
        return {
            "enabled": self.enabled,
            "dedup_minimum_inter_onset_beats": dict(self.dedup_minimum_inter_onset_beats),
            "simultaneity_window_s": self.simultaneity_window_s,
            "alignment_window_s": self.alignment_window_s,
            "systematic_alignment_window_s": self.systematic_alignment_window_s,
            "grid_reference_window_s": self.grid_reference_window_s,
            "static_offset_s": self.static_offset_s,
            "min_evidence_strength": self.min_evidence_strength,
            "suppression_strength_threshold": self.suppression_strength_threshold,
            "velocity_floor": self.velocity_floor,
            "velocity_ceiling": self.velocity_ceiling,
            "velocity_defaults": dict(self.velocity_defaults),
        }


DRUM_CLEANUP_PROFILES: dict[str, DrumCleanupProfile] = {
    DEFAULT_PROFILE_NAME: DrumCleanupProfile(
        name=DEFAULT_PROFILE_NAME,
        enabled=False,
        dedup_minimum_inter_onset_beats={},
        simultaneity_window_s=0.0,
        alignment_window_s=0.0,
        systematic_alignment_window_s=0.0,
        grid_reference_window_s=0.0,
        static_offset_s=0.0,
        min_evidence_strength=1.0,
        suppression_strength_threshold=0.0,
        velocity_floor=100,
        velocity_ceiling=100,
        velocity_defaults={},
    ),
    CLEAN_PROFILE_NAME: DrumCleanupProfile(
        name=CLEAN_PROFILE_NAME,
        enabled=True,
        dedup_minimum_inter_onset_beats=CLEAN_DEDUP_MINIMUM_INTER_ONSET_BEATS,
        simultaneity_window_s=CLEAN_SIMULTANEITY_WINDOW_S,
        alignment_window_s=CLEAN_ALIGNMENT_WINDOW_S,
        systematic_alignment_window_s=CLEAN_SYSTEMATIC_ALIGNMENT_WINDOW_S,
        grid_reference_window_s=CLEAN_GRID_REFERENCE_WINDOW_S,
        static_offset_s=CLEAN_STATIC_OFFSET_S,
        min_evidence_strength=CLEAN_MIN_EVIDENCE_STRENGTH,
        suppression_strength_threshold=CLEAN_SUPPRESSION_STRENGTH_THRESHOLD,
        velocity_floor=CLEAN_VELOCITY_FLOOR,
        velocity_ceiling=CLEAN_VELOCITY_CEILING,
        velocity_defaults=CLEAN_VELOCITY_DEFAULTS,
    ),
}
DRUM_CLEANUP_PROFILE_NAMES: tuple[str, ...] = tuple(DRUM_CLEANUP_PROFILES)


@dataclass(frozen=True)
class OnsetEvidence:
    """One bounded local-audio lookup's result. `time_sec`/`strength` are
    both `None` together -- there is no partial-evidence state -- which is
    exactly the "absent or ambiguous" case every cleanup stage must treat as
    a no-op rather than guess at."""

    time_sec: float | None
    strength: float | None

    @property
    def available(self) -> bool:
        return self.time_sec is not None and self.strength is not None


class OnsetEvidenceSource(Protocol):
    def evidence_near(self, time_sec: float, window_s: float) -> OnsetEvidence: ...


@dataclass(frozen=True)
class BeatGridReference:
    """Analysis-stage beat timestamps plus the detected bar anchor.

    Cleanup uses the grid only to identify reliable, musical samples for a
    *global latency estimate*. It never writes a note directly to a grid line:
    the final target is still a nearby audio onset, preserving performed feel.
    """

    beat_times: tuple[float, ...]
    downbeat_offset_s: float | None

    def eighth_note_times(self) -> tuple[float, ...]:
        beats = [time for time in self.beat_times if time >= 0.0]
        if self.downbeat_offset_s is not None and self.downbeat_offset_s >= 0.0:
            # Keep an explicitly detected bar anchor even if a retained or
            # hand-corrected beat array happens not to include that first beat.
            beats.append(self.downbeat_offset_s)
        beats = tuple(sorted(set(beats)))
        if len(beats) < 2:
            return beats
        eighths: list[float] = []
        for left, right in zip(beats, beats[1:]):
            eighths.extend((left, (left + right) / 2.0))
        eighths.append(beats[-1])
        return tuple(eighths)

    def beat_period_near(self, time_sec: float) -> float | None:
        """Return the enclosing beat period, or ``None`` when tempo is unknown.

        Requiring an event to lie within two known neighbouring beats makes
        de-duplication a safe no-op at an incomplete grid's edges rather than
        extrapolating a possibly stale tempo.
        """
        beats = tuple(sorted(set(time for time in self.beat_times if time >= 0.0)))
        for left, right in zip(beats, beats[1:]):
            if left <= time_sec <= right and right > left:
                return right - left
        return None


class NullOnsetEvidenceSource:
    """No audio evidence, ever. Used whenever no source audio is available
    to analyze (or a caller deliberately wants the safe no-op path) -- every
    stage's absent-evidence fallback then applies uniformly."""

    def evidence_near(self, time_sec: float, window_s: float) -> OnsetEvidence:
        return OnsetEvidence(time_sec=None, strength=None)


@dataclass(frozen=True)
class TableOnsetEvidenceSource:
    """Deterministic, precomputed onset candidates: `(time_sec, strength)`
    pairs with `strength` in `[0, 1]`. Exists for tests and for any caller
    that already ran its own onset detection -- keeps `apply_drum_cleanup`
    itself free of any real audio-analysis dependency."""

    candidates: tuple[tuple[float, float], ...]

    def evidence_near(self, time_sec: float, window_s: float) -> OnsetEvidence:
        nearby = [c for c in self.candidates if abs(c[0] - time_sec) <= window_s]
        if not nearby:
            return OnsetEvidence(time_sec=None, strength=None)
        nearby.sort(key=lambda c: (abs(c[0] - time_sec), -c[1]))
        best = nearby[0]
        if len(nearby) > 1:
            second = nearby[1]
            # Two comparably strong candidates at meaningfully different
            # times: ambiguous, so this is treated as no evidence rather
            # than an arbitrary pick between them.
            if abs(second[1] - best[1]) < 0.05 and abs(second[0] - best[0]) > 1e-6:
                return OnsetEvidence(time_sec=None, strength=None)
        return OnsetEvidence(time_sec=best[0], strength=_clamp(best[1], 0.0, 1.0))


class AudioOnsetEvidenceSource:
    """Bounded local search over one source audio file's onset-strength
    envelope (librosa). Never raises: any failure to load or analyze the
    audio (missing librosa, unreadable file, degenerate/silent signal)
    leaves this source returning "no evidence" for every lookup, which is
    this module's documented safe fallback -- not a special case backends
    must additionally handle.

    Strength is a *local, relative prominence*, not a fraction of the
    song-wide loudest transient (issue #183: a handful of loud crashes/fills
    used to make every genuine but quieter hit score near zero). Each frame
    is compared against its own rolling local baseline and local spread (a
    classic adaptive onset threshold: local median plus a multiple of the
    local median absolute deviation, e.g. Bello et al.'s spectral-flux
    onset detection), so both a quiet verse's real hits (judged against that
    verse's quiet floor) and a busy, densely-hit passage (judged against
    that passage's own typical hit-to-hit variation, not a song-wide
    constant) score on their own local terms -- robust to the few outlier
    transients that dominated a plain global max, and to a fixed baseline
    window being swamped by dense, evenly-loud hits.
    """

    _BASELINE_RADIUS_S = 0.5  # local ambient-loudness/spread window (seconds, each side)
    _PROMINENCE_SCALE_MAD_MULTIPLE = 8.0  # multiple of local MAD treated as "a clear hit's excess"

    def __init__(self, source: Path, *, hop_length: int = 512, center: bool = True) -> None:
        self._source = source
        self._hop_length = hop_length
        # Keep the historical centered envelope for cleanup profiles. The
        # DrumScript no-grid fallback explicitly disables centering because
        # it consumes the returned time as an authored onset, where librosa's
        # half-window delay would otherwise remain audible and visible.
        self._center = center
        self._envelope = None
        self._baseline = None
        self._prominence_scale = None
        self._sr: float | None = None
        self._load()

    def _load(self) -> None:
        try:
            import librosa

            y, sr = librosa.load(str(self._source), sr=None, mono=True)
            envelope = librosa.onset.onset_strength(
                y=y, sr=sr, hop_length=self._hop_length, center=self._center
            )
        except Exception:
            return
        if envelope is None or len(envelope) == 0 or not float(max(envelope)) > 0:
            return
        self._prepare_envelope(envelope, sr)

    def _prepare_envelope(self, envelope: Any, sr: float) -> None:
        """Precompute the rolling local baseline and local prominence scale
        (one value per frame) for `envelope`. Split out from `_load` so
        deterministic tests can inject a synthetic envelope directly,
        without a real audio file."""
        import numpy as np

        frame_time = self._hop_length / float(sr)
        radius = max(1, int(round(self._BASELINE_RADIUS_S / frame_time)))
        envelope_array = np.asarray(envelope, dtype=float)
        padded = np.pad(envelope_array, radius, mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * radius + 1)
        baseline = np.median(windows, axis=1)
        local_mad = np.median(np.abs(windows - baseline[:, None]), axis=1)
        self._envelope = envelope
        self._baseline = baseline
        self._prominence_scale = np.maximum(self._PROMINENCE_SCALE_MAD_MULTIPLE * local_mad, 1e-9)
        self._sr = float(sr)

    def evidence_near(self, time_sec: float, window_s: float) -> OnsetEvidence:
        if self._envelope is None or self._sr is None:
            return OnsetEvidence(time_sec=None, strength=None)
        frame_time = self._hop_length / self._sr
        center = time_sec / frame_time
        span = max(1, int(round(window_s / frame_time)))
        lo = max(0, int(round(center)) - span)
        hi = min(len(self._envelope), int(round(center)) + span + 1)
        if hi <= lo:
            return OnsetEvidence(time_sec=None, strength=None)
        window = self._envelope[lo:hi]
        peak_offset = max(range(len(window)), key=lambda index: window[index])
        peak_value = float(window[peak_offset])
        sorted_values = sorted((float(value) for value in window), reverse=True)
        if len(sorted_values) > 1 and sorted_values[0] > 0 and (sorted_values[1] / sorted_values[0]) > 0.9:
            # Two comparably strong local peaks in the same window: ambiguous.
            return OnsetEvidence(time_sec=None, strength=None)
        peak_index = lo + peak_offset
        prominence = peak_value - float(self._baseline[peak_index])
        strength = _clamp(prominence / float(self._prominence_scale[peak_index]), 0.0, 1.0)
        return OnsetEvidence(time_sec=peak_index * frame_time, strength=strength)


@dataclass(frozen=True)
class CleanedEvent:
    """One derived, inspectable event: a single instrument at a single
    (possibly adjusted) time, carrying enough of its own history to
    reconstruct why it looks the way it does without re-running cleanup."""

    time_sec: float
    instrument: str
    velocity: int
    raw_time_sec: float
    raw_instruments: tuple[str, ...]
    timing_adjustment_s: float
    timing_evidence_strength: float | None
    velocity_source: str  # "audio_evidence" | "role_default"
    suppressed: bool
    suppression_reason: str | None


def _coalesce_groups(
    events: Sequence[Mapping[str, Any]], window_s: float
) -> list[tuple[float, list[Mapping[str, Any]]]]:
    """Group raw events whose time falls within `window_s` of the group's
    anchor (its earliest member) into one coincident-onset group, aligned to
    that anchor time. A zero window (the `default` profile never calls this,
    but a caller passing `enabled=False`'s window would) produces one group
    per input event, unchanged."""
    ordered = sorted(events, key=lambda event: event["time_sec"])
    groups: list[tuple[float, list[Mapping[str, Any]]]] = []
    for event in ordered:
        if groups and event["time_sec"] - groups[-1][0] <= window_s:
            groups[-1][1].append(event)
        else:
            groups.append((event["time_sec"], [event]))
    return groups


def _deduplicate_same_instrument_onsets(
    events: Sequence[Mapping[str, Any]], *, minimum_inter_onset_beats: Mapping[str, float],
    beat_grid: BeatGridReference | None,
) -> list[Mapping[str, Any]]:
    """Keep the first of clearly duplicate same-label onsets.

    The interval is calculated from the local enclosing beat.  If the grid,
    label setting, or local tempo is unavailable, keeping both is the explicit
    conservative fallback.  Events are rebuilt only when a label is removed,
    preserving input ordering and all untouched raw-event fields.
    """
    if beat_grid is None:
        return list(events)
    kept_by_instrument: dict[str, float] = {}
    deduplicated: list[Mapping[str, Any]] = []
    for event in sorted(events, key=lambda item: float(item["time_sec"])):
        time_sec = float(event["time_sec"])
        beat_period = beat_grid.beat_period_near(time_sec)
        instruments: list[str] = []
        for instrument in event["instruments"]:
            minimum_beats = minimum_inter_onset_beats.get(instrument)
            previous = kept_by_instrument.get(instrument)
            if (
                minimum_beats is not None
                and beat_period is not None
                and previous is not None
                and time_sec - previous < minimum_beats * beat_period
            ):
                continue
            instruments.append(instrument)
            kept_by_instrument[instrument] = time_sec
        if instruments:
            if tuple(instruments) == tuple(event["instruments"]):
                deduplicated.append(event)
            else:
                deduplicated.append({**event, "instruments": instruments})
    return deduplicated


def _nearest_distance(time_sec: float, candidates: Sequence[float]) -> float | None:
    return min((abs(time_sec - candidate) for candidate in candidates), default=None)


def _systematic_audio_offset(
    anchors: Sequence[float], *, profile: DrumCleanupProfile, evidence_source: OnsetEvidenceSource,
    beat_grid: BeatGridReference | None,
) -> float:
    """Return the median raw-to-audio latency for unambiguous grid-near hits.

    A median makes a handful of fills, ghost notes, or detector mistakes unable
    to turn a song-wide correction into a large move. Without both a usable
    grid and strong audio evidence this deliberately returns zero.
    """
    if beat_grid is None:
        return 0.0
    eighths = beat_grid.eighth_note_times()
    if not eighths:
        return 0.0
    offsets: list[float] = []
    for anchor in anchors:
        candidate = max(0.0, anchor + profile.static_offset_s)
        evidence = evidence_source.evidence_near(candidate, profile.systematic_alignment_window_s)
        if not evidence.available or evidence.strength < profile.min_evidence_strength:
            continue
        distance = _nearest_distance(evidence.time_sec, eighths)
        if distance is not None and distance <= profile.grid_reference_window_s:
            offsets.append(evidence.time_sec - candidate)
    if not offsets:
        return 0.0
    offsets.sort()
    midpoint = len(offsets) // 2
    return offsets[midpoint] if len(offsets) % 2 else (offsets[midpoint - 1] + offsets[midpoint]) / 2.0


def apply_drum_cleanup(
    events: Sequence[Mapping[str, Any]],
    *,
    profile: DrumCleanupProfile,
    evidence_source: OnsetEvidenceSource,
    beat_grid: BeatGridReference | None = None,
) -> list[CleanedEvent]:
    """Run the five canonical, ordered `drums-clean` stages over raw
    DrumScript `events` and return one `CleanedEvent` per (group, instrument)
    pair. Deterministic for a given `events`/`profile`/`evidence_source`.

    Stage order (each depends on the ones before it):

    1. De-duplicate same-instrument onsets closer than that instrument's
       tempo-scaled minimum interval.  Missing grid/tempo is ambiguous and
       leaves both onsets intact; different instruments are never compared.
    2. Coalesce raw events within `simultaneity_window_s` of each other into
       one aligned onset group (DrumScript sometimes reports a single
       intended onset as two events a tick apart).
    3. Align each group's time to nearby audio evidence, bounded to
       `alignment_window_s` and never earlier than 0.0 (source-start safe).
       No change when evidence is absent or ambiguous.
    4. Shape each instrument's velocity from local evidence strength when
       available and above `min_evidence_strength`; otherwise a bounded,
       role-aware default -- never a flat constant.
    5. Suppress an instrument only when evidence is available and its
       strength is at or below `suppression_strength_threshold` -- a
       reproducible, conservative signal, not an inferred pattern gap. Every
       other event is retained even if it disagrees with a neighbouring
       measure.
    """
    if not profile.enabled:
        raise ValueError(f"profile {profile.name!r} has cleanup disabled; use the raw DrumScript event path instead")

    deduplicated = _deduplicate_same_instrument_onsets(
        events,
        minimum_inter_onset_beats=profile.dedup_minimum_inter_onset_beats,
        beat_grid=beat_grid,
    )
    groups = _coalesce_groups(deduplicated, profile.simultaneity_window_s)
    systematic_offset = _systematic_audio_offset(
        [anchor_time for anchor_time, _group in groups], profile=profile,
        evidence_source=evidence_source, beat_grid=beat_grid,
    )
    cleaned: list[CleanedEvent] = []
    for anchor_time, group in groups:
        candidate_time = max(0.0, anchor_time + profile.static_offset_s + systematic_offset)
        evidence = evidence_source.evidence_near(candidate_time, profile.alignment_window_s)
        if evidence.available and evidence.strength >= profile.min_evidence_strength:
            bounded_time = _clamp(evidence.time_sec, candidate_time - profile.alignment_window_s, candidate_time + profile.alignment_window_s)
            adjusted_time = max(0.0, bounded_time)
            evidence_strength: float | None = evidence.strength
        else:
            adjusted_time = candidate_time
            evidence_strength = evidence.strength if evidence.available else None

        for raw_event in group:
            raw_instruments = tuple(raw_event["instruments"])
            for instrument in raw_instruments:
                if evidence.available and evidence.strength >= profile.min_evidence_strength:
                    velocity_float = profile.velocity_floor + evidence.strength * (profile.velocity_ceiling - profile.velocity_floor)
                    velocity_source = "audio_evidence"
                else:
                    velocity_float = profile.velocity_defaults.get(instrument, _FALLBACK_DEFAULT_VELOCITY)
                    velocity_source = "role_default"
                velocity = int(round(_clamp(velocity_float, 0, 127)))

                suppressed = evidence.available and evidence.strength <= profile.suppression_strength_threshold
                cleaned.append(
                    CleanedEvent(
                        time_sec=adjusted_time,
                        instrument=instrument,
                        velocity=velocity,
                        raw_time_sec=raw_event["time_sec"],
                        raw_instruments=raw_instruments,
                        timing_adjustment_s=adjusted_time - raw_event["time_sec"],
                        timing_evidence_strength=evidence_strength,
                        velocity_source=velocity_source,
                        suppressed=suppressed,
                        suppression_reason="weak-local-audio-evidence" if suppressed else None,
                    )
                )
    return cleaned


def cleaned_events_to_midi_notes(
    cleaned: Sequence[CleanedEvent], *, instrument_pitch: Mapping[str, int], note_duration_s: float = 0.1
) -> list[tuple[float, float, int, int]]:
    """`(start_s, end_s, pitch_midi, velocity)` tuples for every non-
    suppressed cleaned event, ready for a fixed-channel MIDI writer."""
    return [
        (event.time_sec, event.time_sec + note_duration_s, instrument_pitch[event.instrument], event.velocity)
        for event in cleaned
        if not event.suppressed
    ]


def cleaned_events_to_json(cleaned: Sequence[CleanedEvent]) -> list[dict[str, Any]]:
    """The `drums-clean` event JSON: one object per cleaned event, each
    retaining its raw DrumScript source and the cleanup decision applied to
    it -- so a suppression or a timing/velocity change is inspectable and
    reproducible without re-running cleanup (issue #177's event-JSON
    provenance requirement). A suppressed event stays in this array (with
    `cleanup.suppressed: true`) but must never be turned into a MIDI note --
    see `cleaned_events_to_midi_notes`, which filters it out."""
    return [
        {
            "time_sec": event.time_sec,
            "instruments": [event.instrument],
            "raw": {"time_sec": event.raw_time_sec, "instruments": list(event.raw_instruments)},
            "cleanup": {
                "timing_adjustment_s": event.timing_adjustment_s,
                "timing_evidence_strength": event.timing_evidence_strength,
                "velocity": event.velocity,
                "velocity_source": event.velocity_source,
                "suppressed": event.suppressed,
                "suppression_reason": event.suppression_reason,
            },
        }
        for event in cleaned
    ]
