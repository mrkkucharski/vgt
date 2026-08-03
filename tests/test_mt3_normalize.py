"""Offline coverage for MT3 multitrack MIDI normalization (issue #286,
revised by issue #290 to select the dominant non-drum track by note count
rather than the first note-bearing track).

Every fixture is a synthetic MIDI file built directly with `mido`; nothing
here invokes or provisions MT3 (see test_mt3_provision.py for that surface).
"""

from __future__ import annotations

from pathlib import Path

import mido
import pytest

from vgt.mt3_normalize import (
    MT3_NOTE_NORMALIZATION_VERSION,
    MT3_TRACK_SELECTION_VERSION,
    Mt3SelectedTrack,
    select_dominant_musical_track,
    summarize_selected_track,
    write_normalized_mt3_artifacts,
)
from vgt.transcribe import ParsedNote, TempoMapReference, TranscriptionError, parse_notes_csv


def _midi(tmp_path: Path, tracks: list[list[mido.Message]], *, ticks_per_beat: int = 480, name: str = "mt3.mid") -> Path:
    midi = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    for messages in tracks:
        track = mido.MidiTrack()
        track.extend(messages)
        midi.tracks.append(track)
    path = tmp_path / name
    midi.save(str(path))
    return path


def _named(name: str) -> mido.MetaMessage:
    return mido.MetaMessage("track_name", name=name, time=0)


def _tempo(uspb: int, time: int = 0) -> mido.MetaMessage:
    return mido.MetaMessage("set_tempo", tempo=uspb, time=time)


def _on(note: int, velocity: int, time: int, channel: int = 0) -> mido.Message:
    return mido.Message("note_on", channel=channel, note=note, velocity=velocity, time=time)


def _off(note: int, time: int, channel: int = 0) -> mido.Message:
    return mido.Message("note_off", channel=channel, note=note, velocity=0, time=time)


CONDUCTOR = [_tempo(500_000)]  # 120 BPM


def _drum_notes(count: int, *, time: int = 0, gap: int = 120) -> list[mido.Message]:
    """`count` short hits on GM's percussion channel (9)."""
    messages = []
    for _ in range(count):
        messages.append(_on(36, 100, time, channel=9))
        messages.append(_off(36, gap, channel=9))
        time = 0
    return messages


def test_selects_the_dominant_track_not_the_first(tmp_path: Path) -> None:
    path = _midi(tmp_path, [
        CONDUCTOR,
        [_named("Piano"), _on(60, 90, 0), _off(60, 480)],  # first in file, only 1 note
        [_named("Guitar"), _on(62, 80, 0), _off(62, 240), _on(64, 80, 0), _off(64, 240)],  # 2 notes
    ])

    selected = select_dominant_musical_track(path)

    assert selected.track_name == "Guitar"
    assert [note.pitch_midi for note in selected.notes] == [62, 64]


def test_drums_are_excluded_even_when_first_and_most_populous(tmp_path: Path) -> None:
    path = _midi(tmp_path, [
        CONDUCTOR,
        [_named("Drums"), *_drum_notes(10)],  # first in file, most notes, but drums
        [_named("Guitar"), _on(60, 90, 0), _off(60, 480)],
    ])

    selected = select_dominant_musical_track(path)

    assert selected.track_name == "Guitar"
    assert [note.pitch_midi for note in selected.notes] == [60]


def test_ties_prefer_the_earlier_track_in_file_order(tmp_path: Path) -> None:
    path = _midi(tmp_path, [
        CONDUCTOR,
        [_named("First"), _on(60, 90, 0), _off(60, 240)],
        [_named("Second"), _on(62, 80, 0), _off(62, 240)],
    ])

    selected = select_dominant_musical_track(path)

    assert selected.track_name == "First"


def test_a_note_bearing_track_with_no_name_is_selected_with_none(tmp_path: Path) -> None:
    path = _midi(tmp_path, [CONDUCTOR, [_on(64, 70, 0), _off(64, 240)]])

    selected = select_dominant_musical_track(path)

    assert selected.track_name is None
    assert len(selected.notes) == 1


def test_empty_and_meta_only_tracks_are_skipped_without_error(tmp_path: Path) -> None:
    path = _midi(tmp_path, [
        CONDUCTOR,
        [_named("Empty")],  # no notes at all
        [_named("Guitar"), _on(62, 90, 0), _off(62, 480)],
    ])

    selected = select_dominant_musical_track(path)

    assert selected.track_name == "Guitar"


def test_no_note_bearing_track_fails_clearly(tmp_path: Path) -> None:
    path = _midi(tmp_path, [CONDUCTOR, [_named("Empty")]])

    with pytest.raises(TranscriptionError, match="no non-drum MIDI track contains note events"):
        select_dominant_musical_track(path)


def test_drums_only_output_fails_clearly(tmp_path: Path) -> None:
    """No pitched candidate at all -- a drums-only stem selection must not
    silently fall back to the drum track."""
    path = _midi(tmp_path, [CONDUCTOR, [_named("Drums"), *_drum_notes(5)]])

    with pytest.raises(TranscriptionError, match="no non-drum MIDI track contains note events"):
        select_dominant_musical_track(path)


def test_malformed_file_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / "bad.mid"
    path.write_bytes(b"not a midi file")

    with pytest.raises(TranscriptionError, match="not a valid MIDI file"):
        select_dominant_musical_track(path)


def test_tempo_change_within_the_track_is_reflected_in_absolute_seconds(tmp_path: Path) -> None:
    # 480 ticks/beat. First 480 ticks at 120 BPM (0.5s/beat) = 0.5s to the
    # tempo change; the note then runs another 480 ticks at 60 BPM (1s/beat).
    path = _midi(tmp_path, [
        [_tempo(500_000), _on(60, 90, 0), _tempo(1_000_000, 480), _off(60, 480)],
    ])

    selected = select_dominant_musical_track(path)

    assert selected.notes[0].start_s == pytest.approx(0.0)
    assert selected.notes[0].end_s == pytest.approx(1.5)


def test_repeated_and_overlapping_notes_on_one_pitch_pair_fifo(tmp_path: Path) -> None:
    # Same pitch struck again before the first is released: two note-ons
    # then two note-offs. FIFO pairing must not cross the two notes.
    path = _midi(tmp_path, [[
        CONDUCTOR[0],
        _on(60, 90, 0),
        _on(60, 50, 240),   # second strike while the first is still ringing
        _off(60, 240),      # closes the first (earliest open) note-on
        _off(60, 240),      # closes the second
    ]])

    selected = select_dominant_musical_track(path)

    assert len(selected.notes) == 2
    first, second = sorted(selected.notes, key=lambda note: note.start_s)
    assert first.velocity == 90 and first.start_s == pytest.approx(0.0) and first.end_s == pytest.approx(0.5)
    assert second.velocity == 50 and second.start_s == pytest.approx(0.25) and second.end_s == pytest.approx(0.75)


def test_note_on_velocity_zero_is_treated_as_note_off(tmp_path: Path) -> None:
    path = _midi(tmp_path, [[CONDUCTOR[0], _on(60, 90, 0), _on(60, 0, 480)]])

    selected = select_dominant_musical_track(path)

    assert len(selected.notes) == 1
    assert selected.notes[0].end_s == pytest.approx(0.5)


def test_unclosed_note_is_closed_deterministically_at_track_end(tmp_path: Path) -> None:
    path = _midi(tmp_path, [[CONDUCTOR[0], _on(60, 90, 0), _on(64, 80, 480)]])  # 64 never gets a note_off

    selected = select_dominant_musical_track(path)

    assert len(selected.notes) == 2
    unclosed = next(note for note in selected.notes if note.pitch_midi == 64)
    assert unclosed.end_s == pytest.approx(unclosed.start_s)


def test_a_note_off_with_no_matching_note_on_is_ignored(tmp_path: Path) -> None:
    path = _midi(tmp_path, [[CONDUCTOR[0], _off(60, 0), _on(64, 90, 0), _off(64, 240)]])

    selected = select_dominant_musical_track(path)

    assert [note.pitch_midi for note in selected.notes] == [64]


def test_no_fabricated_pitch_bends(tmp_path: Path) -> None:
    path = _midi(tmp_path, [[CONDUCTOR[0], _on(60, 90, 0), _off(60, 480)]])

    selected = select_dominant_musical_track(path)

    assert all(note.pitch_bend == () for note in selected.notes)


def test_summarize_selected_track_reports_the_retained_variant_metrics(tmp_path: Path) -> None:
    path = _midi(tmp_path, [[
        CONDUCTOR[0],
        _on(60, 90, 0), _on(64, 70, 0),  # simultaneous notes -> 2 voices
        _off(60, 240), _off(64, 240),
    ]])
    selected = select_dominant_musical_track(path)

    summary = summarize_selected_track(selected)

    assert summary["track_name"] is None
    assert summary["note_count"] == 2
    assert summary["pitch_range"] == (60, 64)
    assert summary["first_note_s"] == pytest.approx(0.0)
    assert summary["last_note_s"] == pytest.approx(0.5)
    assert summary["max_duration_s"] == pytest.approx(0.5)
    assert summary["max_simultaneous_voices"] == 2


def _read_back(path: Path) -> list[tuple[float, int]]:
    """(start_seconds, pitch) pairs from a re-authored MIDI, using mido's own
    whole-file merge/convert so this check is independent of `_write_midi`'s
    internal tick math."""
    midi = mido.MidiFile(str(path))
    absolute = 0.0
    events = []
    for msg in midi:
        absolute += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            events.append((absolute, msg.note))
    return sorted(events)


def test_downstream_midi_stays_aligned_under_a_constant_project_tempo(tmp_path: Path) -> None:
    # Two simultaneous notes (both deltas 0 relative to the prior event), so
    # both must land on the same re-authored tick regardless of project tempo.
    path = _midi(tmp_path, [[CONDUCTOR[0], _on(60, 90, 0), _on(62, 80, 0), _off(60, 480), _off(62, 0)]])
    selected = select_dominant_musical_track(path)  # source MIDI at 120 BPM

    out_csv, out_midi = tmp_path / "out.csv", tmp_path / "out.mid"
    write_normalized_mt3_artifacts(selected, csv_path=out_csv, midi_path=out_midi, tempo_bpm=90.0)

    csv_notes = parse_notes_csv(out_csv)
    assert len(csv_notes) == 2
    assert all(note.pitch_bend == () for note in csv_notes)

    read_back = _read_back(out_midi)
    # Re-authored at the *project's* 90 BPM, independent of MT3's own 120 BPM.
    assert read_back[0][0] == pytest.approx(0.0, abs=1e-3)
    assert read_back[1][0] == pytest.approx(0.0, abs=1e-3)  # both start at t=0


def test_downstream_midi_stays_aligned_under_a_piecewise_project_tempo_map(tmp_path: Path) -> None:
    path = _midi(tmp_path, [[
        CONDUCTOR[0], _on(60, 90, 0), _off(60, 480), _on(62, 80, 480), _off(62, 480),
    ]])
    selected = select_dominant_musical_track(path)
    assert selected.notes[0].start_s == pytest.approx(0.0)
    assert selected.notes[1].start_s == pytest.approx(1.0)

    tempo_map = TempoMapReference(bpm=60.0, spans=((1.0, 120.0),))
    out_csv, out_midi = tmp_path / "out.csv", tmp_path / "out.mid"
    write_normalized_mt3_artifacts(selected, csv_path=out_csv, midi_path=out_midi, tempo_bpm=60.0, tempo_map=tempo_map)

    read_back = _read_back(out_midi)
    # First note at project t=0 (60 BPM span); second note starts at source
    # second 1.0, exactly where the project's tempo map switches to 120 BPM.
    assert read_back[0][0] == pytest.approx(0.0, abs=1e-3)
    assert read_back[1][0] == pytest.approx(1.0, abs=1e-3)


def test_normalization_versions_are_stable_integers() -> None:
    assert isinstance(MT3_TRACK_SELECTION_VERSION, int) and MT3_TRACK_SELECTION_VERSION >= 1
    assert isinstance(MT3_NOTE_NORMALIZATION_VERSION, int) and MT3_NOTE_NORMALIZATION_VERSION >= 1


def test_mt3_selected_track_is_a_frozen_dataclass_of_parsed_notes(tmp_path: Path) -> None:
    path = _midi(tmp_path, [[CONDUCTOR[0], _on(60, 90, 0), _off(60, 480)]])
    selected = select_dominant_musical_track(path)

    assert isinstance(selected, Mt3SelectedTrack)
    assert all(isinstance(note, ParsedNote) for note in selected.notes)
    with pytest.raises(AttributeError):
        selected.track_name = "changed"  # frozen
