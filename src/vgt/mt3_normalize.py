"""Backend-neutral MT3 multitrack MIDI normalization (issue #286).

MT3 v0.1.0 writes its complete multi-instrument prediction into one Standard
MIDI File; it exposes no stable primary-track field, and an apparent
input-filename track name may have been assigned by whatever imported the
file rather than guaranteed MIDI metadata. This module's selection rule is
therefore structural, not name- or instrument-aware:

1. Select the first MIDI track containing note events (this also skips a
   leading conductor/meta-only track: a track with no notes is simply not a
   candidate).
2. Record that track's name for diagnostics when present, but never require
   or select by it.
3. Discard every other note-bearing track, including detected percussion --
   this is deliberately MT3's first musical track, not target-aware
   program-family filtering (out of scope; see docs/instrument-transcription-
   findings.md before changing that).
4. Fail clearly if no track contains a note event.

This module never invokes or provisions MT3 itself (see `vgt.mt3_provision`
for that), and nothing here is wired to a transcription spec/profile yet --
it only produces vgt's canonical `ParsedNote`/CSV/MIDI contract from an
already-existing MT3 MIDI file, reusing the same tempo-map-aware MIDI writer
every other backend uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .transcribe import (
    ParsedNote,
    TempoMapReference,
    TranscriptionError,
    _note_comparison_metrics,
    _summarize_notes,
    _write_midi,
    _write_parsed_notes_csv,
)

# Bumped whenever a change here would select a different track or produce
# different note boundaries from an unchanged MT3 MIDI file -- the same role
# `PYIN_ALGORITHM_VERSION` plays for that backend's spec identity.
MT3_NORMALIZATION_VERSION = 1

_DEFAULT_TEMPO_USPB = 500_000  # 120 BPM, MIDI's implicit default absent a set_tempo event


@dataclass(frozen=True)
class Mt3SelectedTrack:
    """The first note-bearing track normalized to vgt's canonical note form."""

    track_name: str | None
    notes: tuple[ParsedNote, ...]


def _tempo_events(tracks: list[Any]) -> list[tuple[int, int]]:
    """`(absolute_tick, tempo_uspb)` pairs across every track, sorted and
    starting at tick 0. MT3's tempo events conventionally live in a leading
    conductor track, but nothing here assumes that -- every track is scanned."""
    events: list[tuple[int, int]] = []
    for track in tracks:
        absolute = 0
        for msg in track:
            absolute += msg.time
            if msg.type == "set_tempo":
                events.append((absolute, msg.tempo))
    events.sort(key=lambda item: item[0])
    if not events or events[0][0] != 0:
        events.insert(0, (0, _DEFAULT_TEMPO_USPB))
    merged: list[tuple[int, int]] = []
    for tick, tempo in events:
        if merged and merged[-1][0] == tick:
            merged[-1] = (tick, tempo)  # a later event at the same tick wins
        else:
            merged.append((tick, tempo))
    return merged


def _ticks_to_seconds(tick: int, tempo_events: list[tuple[int, int]], ticks_per_beat: int) -> float:
    """Integrate `tick` through the file's piecewise tempo map -- the MIDI
    analogue of `vgt.transcribe.seconds_to_quarter_notes`'s BPM-span walk."""
    seconds = 0.0
    cursor_tick, tempo = 0, tempo_events[0][1]
    for event_tick, event_tempo in tempo_events:
        if event_tick <= cursor_tick:
            tempo = event_tempo
            continue
        if tick <= event_tick:
            return seconds + (tick - cursor_tick) * tempo / (ticks_per_beat * 1_000_000)
        seconds += (event_tick - cursor_tick) * tempo / (ticks_per_beat * 1_000_000)
        cursor_tick, tempo = event_tick, event_tempo
    return seconds + (tick - cursor_tick) * tempo / (ticks_per_beat * 1_000_000)


def _track_name(track: Any) -> str | None:
    for msg in track:
        if msg.is_meta and msg.type == "track_name":
            return msg.name
    return None


def _extract_track_notes(track: Any, tempo_events: list[tuple[int, int]], ticks_per_beat: int) -> list[ParsedNote]:
    """Pair note-on/off events into `ParsedNote`s, in absolute seconds.

    Keyed by `(channel, pitch)` with a FIFO queue per key, so repeated or
    overlapping notes on the same pitch/channel pair the earliest still-open
    note-on with the next matching note-off rather than crossing streams. A
    note-on with velocity 0 is a note-off (MIDI's running-status convention).
    A note-on left unclosed at the end of the track -- a malformed file, or a
    file truncated mid-note -- is deterministically closed at the track's
    final tick rather than dropped or left to raise.
    """
    active: dict[tuple[int, int], list[tuple[int, int]]] = {}
    notes: list[ParsedNote] = []
    absolute = 0
    for msg in track:
        absolute += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            active.setdefault((msg.channel, msg.note), []).append((absolute, msg.velocity))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            queue = active.get((msg.channel, msg.note))
            if not queue:
                continue  # a note-off with no open note-on for this pitch/channel; ignore it
            start_tick, velocity = queue.pop(0)
            notes.append(ParsedNote(
                start_s=_ticks_to_seconds(start_tick, tempo_events, ticks_per_beat),
                end_s=_ticks_to_seconds(absolute, tempo_events, ticks_per_beat),
                pitch_midi=msg.note, velocity=velocity, pitch_bend=(),
            ))
    for (_channel, pitch), queue in active.items():
        for start_tick, velocity in queue:
            notes.append(ParsedNote(
                start_s=_ticks_to_seconds(start_tick, tempo_events, ticks_per_beat),
                end_s=_ticks_to_seconds(absolute, tempo_events, ticks_per_beat),
                pitch_midi=pitch, velocity=velocity, pitch_bend=(),
            ))
    notes.sort(key=lambda note: (note.start_s, note.pitch_midi))
    return notes


def select_first_musical_track(path: str | Path) -> Mt3SelectedTrack:
    """Select and normalize MT3's first note-bearing track from `path`.

    Raises `TranscriptionError` if the file cannot be parsed or no track
    contains a note event.
    """
    import mido

    try:
        midi = mido.MidiFile(str(path))
    except (OSError, EOFError, ValueError, KeyError, IndexError) as exc:
        raise TranscriptionError(f"{path}: not a valid MIDI file: {exc}") from exc
    if midi.ticks_per_beat <= 0:
        raise TranscriptionError(f"{path}: MIDI file has an invalid ticks-per-beat")

    tempo_events = _tempo_events(midi.tracks)
    for track in midi.tracks:
        notes = _extract_track_notes(track, tempo_events, midi.ticks_per_beat)
        if notes:
            return Mt3SelectedTrack(track_name=_track_name(track), notes=tuple(notes))
    raise TranscriptionError(f"{path}: no MIDI track contains note events")


def summarize_selected_track(selected: Mt3SelectedTrack) -> dict[str, Any]:
    """The retained-variant summary metrics this profile is expected to expose."""
    notes = list(selected.notes)
    note_count, pitch_range, first_note_s, last_note_s = _summarize_notes(notes)
    max_duration_s, max_simultaneous_voices = _note_comparison_metrics(notes)
    return {
        "track_name": selected.track_name,
        "note_count": note_count,
        "pitch_range": pitch_range,
        "first_note_s": first_note_s,
        "last_note_s": last_note_s,
        "max_duration_s": max_duration_s,
        "max_simultaneous_voices": max_simultaneous_voices,
    }


def write_normalized_mt3_artifacts(
    selected: Mt3SelectedTrack, *, csv_path: Path, midi_path: Path, tempo_bpm: float,
    tempo_map: TempoMapReference | None = None,
) -> None:
    """Write vgt's canonical notes CSV and a re-authored single-track MIDI.

    The output MIDI's timing follows the *project's* tempo/tempo-map, exactly
    like every other transcription backend's output -- MT3's own embedded
    tempo only ever decided the absolute-second position of each note during
    selection, above.
    """
    notes = list(selected.notes)
    _write_parsed_notes_csv(csv_path, notes)
    _write_midi(
        midi_path, [(note.start_s, note.end_s, note.pitch_midi, note.velocity) for note in notes],
        tempo_bpm, tempo_map=tempo_map,
    )
