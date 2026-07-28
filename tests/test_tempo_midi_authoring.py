"""Offline proof that reference MIDI uses project QN positions, not one BPM.

The simulator below is deliberately independent of the writer: it is the
same piecewise-constant marker model REAPER constructs from vgt's tempo
spans, inverted from QNs back to real seconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vgt.transcribe import (
    FakeTranscriber,
    TempoMapReference,
    _write_midi,
    default_spec_for_target,
    parse_notes_csv,
    seconds_to_quarter_notes,
    spec_hash,
)
from vgt.transcription_variants import detection_hash


MAP = TempoMapReference(bpm=120.0, spans=((1.0, 60.0), (2.0, 180.0)))


def _varlen(data: bytes, index: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[index]
        index += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, index


def _note_on_qns(path: Path) -> list[float]:
    data = path.read_bytes()
    ticks_per_beat = int.from_bytes(data[12:14], "big")
    index = 8 + int.from_bytes(data[4:8], "big")
    end = index + 8 + int.from_bytes(data[index + 4:index + 8], "big")
    index += 8
    tick, result = 0, []
    while index < end:
        delta, index = _varlen(data, index)
        tick += delta
        status = data[index]
        index += 1
        if status == 0xFF:
            index += 1
            length, index = _varlen(data, index)
            index += length
        elif 0x80 <= status <= 0x9F:
            _note, velocity = data[index:index + 2]
            index += 2
            if 0x90 <= status <= 0x9F and velocity:
                result.append(tick / ticks_per_beat)
        else:
            raise AssertionError(f"unexpected MIDI status {status:#x}")
    return result


def _reaper_seconds(qn: float, tempo_map: TempoMapReference) -> float:
    """Invert simulated REAPER's marker-based QN timeline."""
    cursor_s, cursor_qn, bpm = 0.0, 0.0, tempo_map.bpm
    for marker_s, marker_bpm in tempo_map.spans:
        marker_qn = cursor_qn + (marker_s - cursor_s) * bpm / 60.0
        if qn <= marker_qn:
            return cursor_s + (qn - cursor_qn) * 60.0 / bpm
        cursor_s, cursor_qn, bpm = marker_s, marker_qn, marker_bpm
    return cursor_s + (qn - cursor_qn) * 60.0 / bpm


def test_constant_tempo_authoring_retains_the_issue_193_coordinate_rule(tmp_path: Path) -> None:
    notes = [(0.0, 0.1, 36, 100), (1.25, 1.4, 38, 100)]
    midi = tmp_path / "constant.mid"
    _write_midi(midi, notes, 120.0, channel=9)
    assert _note_on_qns(midi) == [0.0, 2.5]
    assert seconds_to_quarter_notes(1.25, 120.0) == 2.5


def test_piecewise_map_round_trips_note_starts_and_ends(tmp_path: Path) -> None:
    notes = [(0.75, 1.25, 60, 90), (1.75, 2.25, 64, 90)]
    midi = tmp_path / "ramp.mid"
    _write_midi(midi, notes, 120.0, tempo_map=MAP)
    qns = _note_on_qns(midi)
    assert [_reaper_seconds(qn, MAP) for qn in qns] == pytest.approx([0.75, 1.75], abs=1 / 480)
    assert seconds_to_quarter_notes(1.25, 120.0, MAP) == pytest.approx(2.25)


@pytest.mark.parametrize("profile", ["default", "drums-clean"])
def test_both_drum_profiles_round_trip_through_the_piecewise_project_map(tmp_path: Path, profile: str) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"offline")
    spec = default_spec_for_target("drums", backend="drumscript", midi_tempo=120.0, modes={"drums": profile}, tempo_map=MAP)
    result = FakeTranscriber().transcribe(source, tmp_path / profile, spec)
    assert [_reaper_seconds(qn, MAP) for qn in _note_on_qns(result.midi_path)] == pytest.approx([0.0, 0.5, 1.0, 1.5], abs=1 / 480)


def test_basic_pitch_target_uses_the_piecewise_project_map_and_hashes_it(tmp_path: Path) -> None:
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"offline")
    mapped = default_spec_for_target("guitar", backend="fake", midi_tempo=120.0, tempo_map=MAP)
    changed = default_spec_for_target("guitar", backend="fake", midi_tempo=120.0, tempo_map=TempoMapReference(120.0, ((1.0, 90.0),)))
    result = FakeTranscriber().transcribe(source, tmp_path / "guitar", mapped)
    expected = [note.start_s for note in parse_notes_csv(result.notes_path)]
    assert [_reaper_seconds(qn, MAP) for qn in _note_on_qns(result.midi_path)] == pytest.approx(expected, abs=1 / 480)
    assert spec_hash(mapped) != spec_hash(changed)
    assert detection_hash("guitar", "source-content", mapped) != detection_hash("guitar", "source-content", changed)
