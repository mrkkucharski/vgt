"""Monophonic pitch-tracking transcription: pYIN F0 -> discrete notes.

This module is the *algorithm* half of the `pyin` transcription backend; the
spec, profile, and `Transcriber` wiring live in `vgt.transcribe` alongside
every other backend's. It deliberately imports nothing from the rest of vgt
and speaks only in plain tuples, so `vgt.transcribe` can import it lazily
(keeping librosa off the import path of a run that never transcribes a
monophonic target) without a circular import.

Why a second note-producing backend exists at all
-------------------------------------------------
Basic Pitch is a polyphonic, piano-trained model. On a separated bass stem it
fails in a specific, measured way: it latches onto sustained low-frequency
energy and emits many simultaneous multi-second "drone" notes. Measured on the
7Rivers bass stem (see docs/bass-transcription-findings.md), its default `bass`
profile produced 966 notes with **22-voice** peak polyphony, two notes held for
~120 s, and ≥17 voices sounding for 98% of the song.

That is not a tuning problem. Eleven Basic Pitch re-runs across onset
threshold, frame threshold, minimum note length, `--no-melodia`, and four
frequency ceilings, each followed by every ordering of the existing cleanup
stages, never scored better than **37%** frame-level pitch accuracy once
reduced to a single line -- because the model's note *boundaries* are wrong, so
no "pick one voice" rule can recover the right note. `force_monophony` resolves
overlaps by highest velocity, and a bass ghost harmonic is routinely louder
than its own fundamental; a lowest-pitch-wins variant scored worse still,
because the 120 s drone then wins every overlap forever.

A bass is a single-line source, so the right tool is a monophonic F0 tracker,
not a polyphonic model with a "keep one note" filter bolted on. pYIN scores
**78.9% frame F-measure** on the shipped `bass` profile, with **10.9% octave
errors** on graded frames. This is the final 7Rivers evaluation against the
estimated CQT reference, not an absolute accuracy claim or an earlier
experiment's result. librosa is already a hard requirement of vgt.

What a tracker still cannot see on its own
------------------------------------------
An F0 tracker reports pitch, and a repeated note on one string does not change
pitch. Four plucks of the same fret are one continuous F0 run, so the
maximal-run rule that makes polyphony 1 by construction also glues them into
one held note. On the 7Rivers bass stem that cost half the part outright: against
the maintainer's full-length hand-corrected reference -- 272 notes, **83% of them
inside a run of repeated notes on one pitch** -- the tracker alone found 46% of
what was played. `_rearticulation_frames` plus `segment_notes`' minimum split
spacing recover them, taking onset F-measure from 57% to 76%. Those numbers come
from a real hand annotation rather than an estimated reference, the only one this
project has for any instrument.

Two independent estimators agree on what that stem actually plays -- pYIN
(time-domain autocorrelation) and a CQT harmonic-sum estimator
(frequency-domain) agree on 85.6% of loud frames, both putting the line at
MIDI ~29-43 with a median of 34. Basic Pitch placed 524 of its 966 notes above
that range entirely.
"""

from __future__ import annotations

from typing import Any, Sequence

# One note as `(start_s, end_s, pitch_midi, velocity)` -- the same 4-tuple
# shape `vgt.transcribe._write_midi` and `_write_notes_csv` already consume,
# so no vgt type has to cross this module's boundary.
NoteTuple = tuple[float, float, int, int]

# Bumped whenever a change here would alter the notes produced from unchanged
# audio and unchanged spec settings. It is part of `PyinSpec`'s identity (see
# that dataclass), so a cached transcription can never survive an algorithm
# change that would have produced different notes. This is the in-process
# equivalent of the `package_pin`/`runtime_version` a subprocess backend pins:
# librosa's own version is deliberately *not* hashed, since a patch-level
# librosa upgrade must not invalidate every user's bass reference.
PYIN_ALGORITHM_VERSION = 2

# Natural-log energy is what `_rearticulation_frames` differences, but the
# threshold is stated in dB because that is the unit the rise is legible in.
_DB_PER_NAT = 20.0 / 2.302585092994046  # 20 / ln(10)


def track_f0(
    source: str,
    *,
    sample_rate_hz: int,
    frame_length: int,
    hop_length: int,
    minimum_frequency_hz: float,
    maximum_frequency_hz: float,
) -> tuple[Any, Any, Any, int]:
    """Run pYIN over `source`, returning `(midi, voiced, rms, sample_rate)`.

    `midi` holds a fractional MIDI pitch per analysis frame with `nan` where
    the frame is unvoiced; `rms` is the per-frame energy `segment_notes` maps
    to velocity. Kept separate from segmentation so a test can exercise the
    (pure, fast) segmentation logic on synthetic tracks without running the
    tracker or reading audio.
    """
    import librosa  # imported lazily: see the module docstring
    import numpy as np

    audio, sample_rate = librosa.load(source, sr=sample_rate_hz, mono=True)
    f0, voiced, _probability = librosa.pyin(
        audio,
        fmin=minimum_frequency_hz,
        fmax=maximum_frequency_hz,
        sr=sample_rate,
        frame_length=frame_length,
        hop_length=hop_length,
    )
    midi = np.full(len(f0), np.nan, dtype=float)
    usable = voiced & ~np.isnan(f0)
    midi[usable] = librosa.hz_to_midi(f0[usable])
    rms = librosa.feature.rms(y=audio, frame_length=frame_length, hop_length=hop_length)[0]
    return midi, voiced, rms, int(sample_rate)


def _median_filter(values: Sequence[float], size: int) -> list[float]:
    """Odd-length running median over `values`, edges clamped.

    Hand-rolled rather than `scipy.ndimage.median_filter`: scipy reaches vgt
    only as a transitive librosa dependency, and this module should not be the
    one place that promotes it to a direct import.
    """
    if size <= 1:
        return list(values)
    half = size // 2
    count = len(values)
    smoothed: list[float] = []
    for index in range(count):
        low = max(0, index - half)
        high = min(count, index + half + 1)
        window = sorted(values[low:high])
        smoothed.append(window[len(window) // 2])
    return smoothed


def _velocity(rms: Any, start_index: int, end_index: int, rms_reference: float) -> int:
    """Map a note's mean frame energy to a MIDI velocity in 1..127.

    Scaled against the whole take's loudest note rather than an absolute dBFS
    figure, so a quietly-mixed stem still yields a readable dynamic range
    instead of collapsing every note onto velocity 1.
    """
    if rms_reference <= 0:
        return 90
    span = rms[start_index:end_index]
    level = float(sum(span) / len(span)) if len(span) else 0.0
    scaled = round(127.0 * (level / rms_reference) ** 0.5)
    return max(1, min(127, scaled))


def _rearticulation_frames(
    rms: Sequence[float], *, span_frames: int, rise_db: float
) -> list[int]:
    """Frames where the string was plucked again without the pitch changing.

    A pitch tracker cannot see a re-articulation: playing the same fret four
    times running is four notes but one continuous F0, so `segment_notes`'
    "maximal run of one pitch" rule emits it as a single held note. The
    audible difference is in the *envelope* -- each pluck restarts the decay --
    so this looks for a sharp rise in the frame energy `track_f0` already
    returns, needing no second pass over the audio.

    A frame qualifies when log energy has risen by `rise_db` over the previous
    `span_frames` frames *and* that rise is a local maximum, so one attack
    yields one frame rather than a smear across its whole ramp.

    `rise_db` is deliberately low (0.8 dB shipped). A re-articulation the
    maintainer's hand reference marks is often *faint* -- median rise 0.66 dB
    against 2.16 dB for an onset the tracker already found -- so a threshold
    high enough to be safe on its own misses two thirds of them. What makes the
    low bar affordable is `segment_notes`' minimum split spacing, not the
    threshold: recall comes from here, precision from there.

    Five alternative detectors were measured against the full hand reference
    and all lost to this one (see docs/bass-transcription-findings.md): mel-band
    spectral flux, a locally adaptive threshold, a decaying peak follower,
    shorter/band-limited RMS envelopes, and beat-grid-guided candidates. The
    93 ms RMS window pYIN needs for its own F0 search turns out to be a *better*
    envelope for this than a sharper one -- the smearing is useful smoothing.

    Returns frame indices; `segment_notes` decides which of them fall inside a
    note and are far enough from its ends and from each other to cut at.
    """
    if rise_db <= 0 or span_frames < 1 or len(rms) <= span_frames:
        return []
    import math

    threshold_nats = rise_db / _DB_PER_NAT
    log_energy = [math.log(max(float(value), 1e-8)) for value in rms]
    rise = [0.0] * len(log_energy)
    for index in range(span_frames, len(log_energy)):
        rise[index] = max(log_energy[index] - log_energy[index - span_frames], 0.0)
    return [
        index
        for index in range(1, len(rise) - 1)
        if rise[index] >= threshold_nats and rise[index] >= rise[index - 1] and rise[index] > rise[index + 1]
    ]


def segment_notes(
    midi: Sequence[float],
    rms: Sequence[float],
    *,
    sample_rate_hz: int,
    hop_length: int,
    median_filter_frames: int,
    minimum_note_length_ms: float,
    rearticulation_rise_db: float = 0.0,
    rearticulation_span_frames: int = 1,
    rearticulation_minimum_spacing_s: float = 0.0,
) -> list[NoteTuple]:
    """Turn a per-frame MIDI pitch track into discrete, non-overlapping notes.

    Quantizes each voiced frame to the nearest semitone, median-filters the
    result to remove single-frame pitch jitter (an unvoiced frame participates
    as a sentinel, so the filter also closes one-frame dropouts rather than
    splitting a held note in two), then emits each maximal run of one pitch as
    a note -- cut at any re-articulation `_rearticulation_frames` found inside
    it, since a repeated note on one string is one pitch run but several notes.

    Two rules govern where a cut may fall, and they do different jobs:

    * **`minimum_note_length_ms`** -- neither piece may fall under the note
      floor. This keeps a note's own attack transient from shaving a sliver off
      its front, and means a split can never produce a piece the length filter
      then drops, leaving a hole where a played note was.
    * **`rearticulation_minimum_spacing_s`** -- consecutive cuts inside one run
      must be at least this far apart. This is what buys the precision that
      lets `rearticulation_rise_db` sit low enough to catch faint re-attacks.
      Measured on the full 7Rivers hand reference, dropping the threshold alone
      (1.0 dB -> 0.8) gains 0.3 F; adding the spacing rule takes it to +2.4, and
      improves *both* cross-validation folds. It is a musical duration, so
      callers derive it from tempo rather than hard-coding milliseconds.

    `rearticulation_rise_db = 0` (the default) disables splitting entirely, so
    a caller that has not opted in gets the plain maximal-run behaviour.

    The emitted notes are non-overlapping *by construction*: every boundary is
    read straight out of the frame-time array rather than accumulated as
    `start + n * hop`, so consecutive notes share an exact float boundary and
    the result's measured polyphony is exactly 1 -- split pieces included. This
    is why the `pyin` profiles need no `cap_simultaneous_voices`/
    `force_monophony` stage to guarantee a single line: the invariant holds
    before cleanup runs, not because a stage enforced it afterwards.
    """
    import math

    frame_count = len(midi)
    if frame_count == 0:
        return []
    frame_seconds = hop_length / float(sample_rate_hz)
    unvoiced = -1.0
    quantized = [unvoiced if math.isnan(value) else float(round(value)) for value in midi]
    smoothed = _median_filter(quantized, median_filter_frames)

    minimum_note_s = minimum_note_length_ms / 1000.0
    minimum_piece_frames = math.ceil(minimum_note_s / frame_seconds)
    minimum_spacing_frames = max(
        minimum_piece_frames, math.ceil(rearticulation_minimum_spacing_s / frame_seconds)
    )
    rearticulations = _rearticulation_frames(
        rms, span_frames=rearticulation_span_frames, rise_db=rearticulation_rise_db
    )
    rms_reference = max(rms) if len(rms) else 0.0
    notes: list[NoteTuple] = []
    index = 0
    while index < frame_count:
        if smoothed[index] < 0:
            index += 1
            continue
        end = index
        while end + 1 < frame_count and smoothed[end + 1] == smoothed[index]:
            end += 1
        bounds = [index]
        for frame in rearticulations:
            if not index < frame <= end:
                continue
            # The spacing rule applies to every piece a cut would create, at
            # both ends -- an attack detected twice is as likely at a run's
            # edge as in its middle.
            if frame - bounds[-1] < minimum_spacing_frames or (end + 1) - frame < minimum_spacing_frames:
                continue
            bounds.append(frame)
        bounds.append(end + 1)
        for low, high in zip(bounds, bounds[1:]):
            start_s = low * frame_seconds
            end_s = high * frame_seconds
            if end_s - start_s >= minimum_note_s:
                notes.append((
                    round(start_s, 6),
                    round(end_s, 6),
                    int(smoothed[index]),
                    _velocity(rms, low, high, rms_reference),
                ))
        index = end + 1
    return notes


def transcribe_monophonic(
    source: str,
    *,
    sample_rate_hz: int,
    frame_length: int,
    hop_length: int,
    minimum_frequency_hz: float,
    maximum_frequency_hz: float,
    median_filter_frames: int,
    minimum_note_length_ms: float,
    rearticulation_rise_db: float = 0.0,
    rearticulation_span_frames: int = 1,
    rearticulation_minimum_spacing_s: float = 0.0,
) -> list[NoteTuple]:
    """Track and segment `source` in one call -- the whole backend algorithm."""
    midi, _voiced, rms, sample_rate = track_f0(
        source,
        sample_rate_hz=sample_rate_hz,
        frame_length=frame_length,
        hop_length=hop_length,
        minimum_frequency_hz=minimum_frequency_hz,
        maximum_frequency_hz=maximum_frequency_hz,
    )
    return segment_notes(
        midi,
        rms,
        sample_rate_hz=sample_rate,
        hop_length=hop_length,
        median_filter_frames=median_filter_frames,
        minimum_note_length_ms=minimum_note_length_ms,
        rearticulation_rise_db=rearticulation_rise_db,
        rearticulation_span_frames=rearticulation_span_frames,
        rearticulation_minimum_spacing_s=rearticulation_minimum_spacing_s,
    )
