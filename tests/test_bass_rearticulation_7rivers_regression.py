"""Re-articulation splitting, against the maintainer's hand-corrected bass MIDI.

This is the only test in the suite that can see the bug it guards. Every other
bass metric is frame-level -- "which pitch is sounding now?" -- and four plucks
of one fret sound one pitch throughout, so a transcript emitting one held note
and one emitting four score identically on all of them. Only note *starts*
distinguish them, and only a human annotation supplies those; see
`tests/fixtures/bass_7rivers/README.md`.

Runs entirely offline from a frame table: no librosa, no tracker, no audio.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vgt.transcribe import (
    PYIN_REARTICULATION_MINIMUM_SPACING_BEATS,
    PYIN_REARTICULATION_RISE_DB,
    PYIN_REARTICULATION_SPAN_FRAMES,
    ParsedNote,
    _merge_fragments,
)
from vgt.pyin_notes import segment_notes

FIXTURES = Path(__file__).parent / "fixtures" / "bass_7rivers"
# One pYIN frame is ~11.6 ms and the annotation was placed by ear against a
# waveform, so a tighter tolerance would be measuring the annotator's mouse.
# Matches `scripts/bass_transcription_probe.py`'s `ONSET_TOLERANCE_S`.
ONSET_TOLERANCE_S = 0.05
MINIMUM_NOTE_LENGTH_MS = 70.0
MEDIAN_FILTER_FRAMES = 5
# The reference project's tempo, so the beat-relative spacing resolves to the
# same seconds the shipped run used.
REFERENCE_TEMPO_BPM = 120.004
MINIMUM_SPACING_S = PYIN_REARTICULATION_MINIMUM_SPACING_BEATS * 60.0 / REFERENCE_TEMPO_BPM


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def frames() -> dict:
    return _load("pyin_frames.json")


@pytest.fixture(scope="module")
def annotated_onsets() -> list[float]:
    return sorted(note[0] for note in _load("hand_corrected_notes.json")["notes"])


def _segment(frames: dict, *, rise_db: float, spacing_s: float = MINIMUM_SPACING_S):
    return segment_notes(
        [float("nan") if value is None else value for value in frames["midi"]],
        frames["rms"],
        sample_rate_hz=frames["sample_rate_hz"],
        hop_length=frames["hop_length"],
        median_filter_frames=MEDIAN_FILTER_FRAMES,
        minimum_note_length_ms=MINIMUM_NOTE_LENGTH_MS,
        rearticulation_rise_db=rise_db,
        rearticulation_span_frames=PYIN_REARTICULATION_SPAN_FRAMES,
        rearticulation_minimum_spacing_s=spacing_s,
    )


def _onset_f_measure(notes, reference: list[float]) -> float:
    """One-to-one nearest matching, so shattering one played note into six
    fragments scores one match and five false positives rather than six hits."""
    detected = sorted(start for start, _end, _pitch, _velocity in notes)
    taken = [False] * len(detected)
    matched = 0
    for onset in reference:
        best, best_distance = None, ONSET_TOLERANCE_S
        for index, start in enumerate(detected):
            if not taken[index] and abs(start - onset) <= best_distance:
                best, best_distance = index, abs(start - onset)
        if best is not None:
            taken[best] = True
            matched += 1
    precision = matched / len(detected) if detected else 0.0
    recall = matched / len(reference) if reference else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _max_polyphony(notes) -> int:
    edges = [(start, 1) for start, _e, _p, _v in notes] + [(end, -1) for _s, end, _p, _v in notes]
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    active = peak = 0
    for _time, delta in edges:
        active += delta
        peak = max(peak, active)
    return peak


def test_splitting_recovers_the_repeated_notes_a_pitch_run_hides(frames, annotated_onsets) -> None:
    """The headline: without splitting the tracker finds roughly half the notes
    the maintainer actually played, because the rest share a pitch with their
    neighbour. Bounds are deliberately loose -- this guards the size of the
    effect, not the exact tuning, which docs/bass-transcription-findings.md
    owns and which is expected to move as the annotation grows."""
    without = _onset_f_measure(_segment(frames, rise_db=0.0), annotated_onsets)
    with_split = _onset_f_measure(_segment(frames, rise_db=PYIN_REARTICULATION_RISE_DB), annotated_onsets)

    assert without < 0.62
    assert with_split > 0.72
    assert with_split - without > 0.12


def test_the_spacing_rule_is_what_makes_the_low_threshold_affordable(frames, annotated_onsets) -> None:
    """`rearticulation_rise_db` sits at 0.8 dB, low enough to catch faint
    re-attacks, and that is only safe because cuts must also be spaced apart.
    Without the spacing rule the same threshold splits far more, and scores
    worse -- which is the whole argument for having two knobs instead of one."""
    spaced = _segment(frames, rise_db=PYIN_REARTICULATION_RISE_DB)
    unspaced = _segment(frames, rise_db=PYIN_REARTICULATION_RISE_DB, spacing_s=0.0)

    assert len(unspaced) > len(spaced)
    assert _onset_f_measure(spaced, annotated_onsets) > _onset_f_measure(unspaced, annotated_onsets)


def test_a_split_note_never_overlaps_its_own_other_half(frames) -> None:
    """Polyphony 1 is a construction property of `segment_notes`, and splitting
    a run is exactly where it could quietly stop holding: the pieces are
    adjacent, so an off-by-one in the boundary arithmetic would overlap them.
    The `pyin` profiles ship no `force_monophony` stage to catch that."""
    assert _max_polyphony(_segment(frames, rise_db=PYIN_REARTICULATION_RISE_DB)) == 1


def test_no_split_piece_falls_under_the_note_length_floor(frames) -> None:
    """A cut is refused unless both pieces clear the floor, so splitting can
    never drop a piece and leave a hole where a played note was."""
    notes = _segment(frames, rise_db=PYIN_REARTICULATION_RISE_DB)

    assert notes
    assert min(end - start for start, end, _p, _v in notes) >= MINIMUM_NOTE_LENGTH_MS / 1000.0 - 1e-9


def test_merge_fragments_would_undo_every_split_without_the_touching_guard(frames) -> None:
    """The bass pipeline's `merge_fragments` stage runs after segmentation and
    rejoins same-pitch notes within 30 ms -- and split pieces share an exact
    boundary, so they are the most mergeable pairs there are. Unguarded, the
    whole change silently reduces to no change; this pins that the guard is
    load-bearing rather than decorative."""
    notes = [
        ParsedNote(start_s=start, end_s=end, pitch_midi=pitch, velocity=velocity, pitch_bend=())
        for start, end, pitch, velocity in _segment(frames, rise_db=PYIN_REARTICULATION_RISE_DB)
    ]
    baseline = len(_segment(frames, rise_db=0.0))

    assert len(_merge_fragments(notes, max_gap_s=0.03, merge_touching=True)) == baseline
    assert len(_merge_fragments(notes, max_gap_s=0.03, merge_touching=False)) == len(notes)


def test_the_shipped_pipeline_still_merges_a_genuine_dropout(frames) -> None:
    """The guard must exclude only the zero-gap case: a real fragment pair --
    a note the tracker dropped out of for a frame -- has a positive gap and is
    still rejoined."""
    fragments = [
        ParsedNote(start_s=0.0, end_s=1.0, pitch_midi=34, velocity=80, pitch_bend=()),
        ParsedNote(start_s=1.012, end_s=2.0, pitch_midi=34, velocity=90, pitch_bend=()),
    ]

    assert len(_merge_fragments(fragments, max_gap_s=0.03, merge_touching=False)) == 1


def test_every_rearticulation_setting_is_part_of_the_detection_cache_identity() -> None:
    """These settings shape the raw note list, not a cleanup derivation, so
    changing one must invalidate the cached *detection* -- not merely the
    variant's `settings_hash`. When this was wrong, retuning the splitter and
    re-running produced "unchanged, using cached result" and the old notes;
    only the unrelated `algorithm_version` bump hid it."""
    from dataclasses import replace

    from vgt.transcribe import default_spec_for_target
    from vgt.transcription_variants import detection_hash

    spec = default_spec_for_target("bass", midi_tempo=120.0, time_signature="4/4")
    baseline = detection_hash("bass", "input", spec)

    for field, value in (
        ("rearticulation_span_frames", spec.rearticulation_span_frames + 1),
        ("rearticulation_rise_db", spec.rearticulation_rise_db + 0.5),
        ("rearticulation_minimum_spacing_beats", spec.rearticulation_minimum_spacing_beats + 0.25),
    ):
        assert detection_hash("bass", "input", replace(spec, **{field: value})) != baseline, field
