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
**84%** on the same stem with 2% octave error, and needs no new dependency:
librosa is already a hard requirement of vgt.

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
PYIN_ALGORITHM_VERSION = 1


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


def segment_notes(
    midi: Sequence[float],
    rms: Sequence[float],
    *,
    sample_rate_hz: int,
    hop_length: int,
    median_filter_frames: int,
    minimum_note_length_ms: float,
) -> list[NoteTuple]:
    """Turn a per-frame MIDI pitch track into discrete, non-overlapping notes.

    Quantizes each voiced frame to the nearest semitone, median-filters the
    result to remove single-frame pitch jitter (an unvoiced frame participates
    as a sentinel, so the filter also closes one-frame dropouts rather than
    splitting a held note in two), then emits each maximal run of one pitch as
    a note.

    The emitted notes are non-overlapping *by construction*: every boundary is
    read straight out of the frame-time array rather than accumulated as
    `start + n * hop`, so consecutive notes share an exact float boundary and
    the result's measured polyphony is exactly 1. This is why the `pyin`
    profiles need no `cap_simultaneous_voices`/`force_monophony` stage to
    guarantee a single line -- the invariant holds before cleanup runs, not
    because a stage enforced it afterwards.
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
        start_s = index * frame_seconds
        end_s = (end + 1) * frame_seconds
        if end_s - start_s >= minimum_note_s:
            notes.append((
                round(start_s, 6),
                round(end_s, 6),
                int(smoothed[index]),
                _velocity(rms, index, end + 1, rms_reference),
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
    )
