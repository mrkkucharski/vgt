"""Classical multi-pitch transcription: Essentia's Klapuri/Melodia multi-F0
estimators -> discrete notes.

This module is the *algorithm* half of the `essentia` transcription backend;
the spec, profile, and `Transcriber` wiring live in `vgt.transcribe`
alongside every other backend's. It deliberately imports nothing from the
rest of vgt and speaks only in plain tuples, so `vgt.transcribe` can import it
lazily (keeping Essentia off the import path of a run that never selects
`guitar-klapuri`/`guitar-melodia`) without a circular import, and without
requiring Essentia to be installed at all.

Why this backend exists
------------------------
Basic Pitch and MT3 are both neural. This project's own bass findings (see
`vgt.pyin_notes`) show a classical, non-learned tracker can outright beat a
neural model when the tracker's structural assumption (monophony) matches the
source -- pYIN scores 78.9% frame F-measure against Basic Pitch's best-tuned
47.6% on a bass stem. Guitar chords are genuinely polyphonic, so the same
monophonic assumption doesn't transfer; these two profiles exist to make a
*polyphonic* classical alternative -- Klapuri's harmonic-summation salience
function (A. Klapuri, "Multiple Fundamental Frequency Estimation by Summing
Harmonic Amplitudes", ISMIR 2006) and Essentia's multi-source generalization
of the MELODIA salience method (Salamon & Gomez 2012) -- comparable the same
way, rather than staying a one-off script result.

What Essentia's own output does not give you
----------------------------------------------
`MultiPitchKlapuri`/`MultiPitchMelodia` return only per-frame frequency
candidates in Hz -- no note segmentation and no salience/magnitude alongside
each one. Everything past that is this module's own doing: quantizing each
frequency to the nearest semitone, turning a pitch's presence across frames
into a boolean timeline, bridging short gaps, dropping anything left under
the note-length floor, and deriving velocity from the source audio's own
local RMS (scaled the same way `pyin_notes._velocity` scales pYIN's: against
the take's own loudest sampled window, not an absolute dBFS floor -- neither
algorithm reports a magnitude this module could use directly).
"""

from __future__ import annotations

import math
from typing import Sequence

# One note as `(start_s, end_s, pitch_midi, velocity)` -- the same 4-tuple
# shape `vgt.transcribe._write_midi` and `_write_notes_csv` already consume,
# so no vgt type has to cross this module's boundary.
NoteTuple = tuple[float, float, int, int]

# Bumped whenever a change here would alter the notes produced from unchanged
# audio and unchanged spec settings -- the in-process equivalent of the
# `package_pin`/`runtime_version` a subprocess backend pins (see
# `PYIN_ALGORITHM_VERSION`). Essentia's own package version is deliberately
# not hashed, for the same reason librosa's isn't: a patch-level Essentia
# upgrade must not invalidate every user's cached transcription.
ESSENTIA_ALGORITHM_VERSION = 1


def _hz_to_midi(hz: float) -> int:
    return int(round(69 + 12 * math.log2(hz / 440.0)))


def track_multipitch(
    source: str,
    *,
    algorithm: str,
    sample_rate_hz: float,
    minimum_frequency_hz: float,
    maximum_frequency_hz: float,
) -> tuple[list[list[float]], float, list[float]]:
    """Run Essentia over `source`, returning `(pitches_by_frame, hop_seconds,
    rms)`.

    `pitches_by_frame[i]` holds every Hz candidate the algorithm found in
    frame `i` (frames commonly have zero, one, or several); `rms` is the
    per-frame local energy `segment_multipitch` maps to velocity. Kept
    separate from segmentation so a test can exercise the (pure, fast)
    segmentation logic on synthetic frames without Essentia installed.

    Raises `ImportError` if Essentia is not installed -- callers translate
    that into a `vgt`-level error with an install hint (see
    `vgt.transcribe.EssentiaTranscriber`).
    """
    import essentia.standard as es
    import numpy as np

    audio = es.MonoLoader(filename=source, sampleRate=sample_rate_hz)()
    algo_cls = es.MultiPitchKlapuri if algorithm == "klapuri" else es.MultiPitchMelodia
    extractor = algo_cls(
        sampleRate=sample_rate_hz, minFrequency=minimum_frequency_hz, maxFrequency=maximum_frequency_hz
    )
    pitches_by_frame = extractor(audio)
    hop_size = int(extractor.paramValue("hopSize"))
    hop_seconds = hop_size / sample_rate_hz

    frame_count = len(pitches_by_frame)
    window = hop_size * 4
    half = window // 2
    rms = np.zeros(frame_count, dtype=np.float64)
    for index in range(frame_count):
        center = index * hop_size
        segment = audio[max(0, center - half) : min(len(audio), center + half)]
        rms[index] = float(np.sqrt(np.mean(segment.astype(np.float64) ** 2))) if segment.size else 0.0

    return [list(frame) for frame in pitches_by_frame], hop_seconds, rms.tolist()


def _merged_runs(flags: Sequence[bool], merge_gap_frames: int) -> list[tuple[int, int]]:
    """Maximal True-runs in `flags`, bridging gaps of at most
    `merge_gap_frames` False frames. Returns (start, end_exclusive) pairs."""
    runs: list[tuple[int, int]] = []
    frame_count = len(flags)
    index = 0
    while index < frame_count:
        if not flags[index]:
            index += 1
            continue
        start = index
        end = index + 1
        while end < frame_count:
            if flags[end]:
                end += 1
                continue
            gap_end = end
            while gap_end < frame_count and not flags[gap_end] and gap_end - end < merge_gap_frames:
                gap_end += 1
            if gap_end < frame_count and flags[gap_end]:
                end = gap_end + 1
                continue
            break
        runs.append((start, end))
        index = end
    return runs


def _velocity(rms: Sequence[float], start_index: int, end_index: int, rms_reference: float) -> int:
    """Map a note's mean frame energy to a MIDI velocity in 1..127.

    Scaled against the whole take's loudest sampled window rather than an
    absolute dBFS figure -- same reasoning as `pyin_notes._velocity` -- so a
    quietly-mixed stem still yields a readable dynamic range.
    """
    if rms_reference <= 0:
        return 90
    span = rms[start_index:end_index]
    level = float(sum(span) / len(span)) if len(span) else 0.0
    scaled = round(127.0 * (level / rms_reference) ** 0.5)
    return max(1, min(127, scaled))


def segment_multipitch(
    pitches_by_frame: Sequence[Sequence[float]],
    rms: Sequence[float],
    *,
    hop_seconds: float,
    minimum_note_length_ms: float,
    merge_gap_ms: float,
) -> list[NoteTuple]:
    """Turn per-frame multi-pitch candidates into discrete, possibly
    overlapping notes (unlike `pyin_notes.segment_notes`, this is a genuinely
    polyphonic backend, so simultaneous notes are expected, not a bug).

    Each frequency is quantized to the nearest semitone; a pitch's presence
    across frames becomes a boolean timeline, gaps of at most `merge_gap_ms`
    are bridged into one note (Essentia's frame-to-frame candidates are noisy
    enough that a held note routinely drops out for a frame or two), and
    anything still under `minimum_note_length_ms` after bridging is dropped.
    """
    frame_count = len(pitches_by_frame)
    if frame_count == 0:
        return []

    active_by_pitch: dict[int, list[bool]] = {}
    for frame_index, freqs in enumerate(pitches_by_frame):
        for hz in freqs:
            if hz <= 0:
                continue
            midi = _hz_to_midi(float(hz))
            flags = active_by_pitch.setdefault(midi, [False] * frame_count)
            flags[frame_index] = True

    merge_gap_frames = max(0, round((merge_gap_ms / 1000.0) / hop_seconds)) if hop_seconds > 0 else 0
    minimum_note_s = minimum_note_length_ms / 1000.0
    rms_reference = max(rms) if len(rms) else 0.0

    notes: list[NoteTuple] = []
    for midi, flags in active_by_pitch.items():
        for start_frame, end_frame in _merged_runs(flags, merge_gap_frames):
            start_s = start_frame * hop_seconds
            end_s = end_frame * hop_seconds
            if end_s - start_s < minimum_note_s:
                continue
            notes.append((
                round(start_s, 6),
                round(end_s, 6),
                midi,
                _velocity(rms, start_frame, end_frame, rms_reference),
            ))
    notes.sort()
    return notes


def transcribe_multipitch(
    source: str,
    *,
    algorithm: str,
    sample_rate_hz: float,
    minimum_frequency_hz: float,
    maximum_frequency_hz: float,
    minimum_note_length_ms: float,
    merge_gap_ms: float,
) -> list[NoteTuple]:
    """Track and segment `source` in one call -- the whole backend algorithm."""
    pitches_by_frame, hop_seconds, rms = track_multipitch(
        source,
        algorithm=algorithm,
        sample_rate_hz=sample_rate_hz,
        minimum_frequency_hz=minimum_frequency_hz,
        maximum_frequency_hz=maximum_frequency_hz,
    )
    return segment_multipitch(
        pitches_by_frame,
        rms,
        hop_seconds=hop_seconds,
        minimum_note_length_ms=minimum_note_length_ms,
        merge_gap_ms=merge_gap_ms,
    )
