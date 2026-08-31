"""Backend-neutral MT3 multitrack MIDI normalization (issue #286, revised by
issue #290 to select the dominant track by duration rather than the first,
with target-aware program-family elimination for explicitly named tracks;
revised again 2026-08-31 to merge same-family named survivors instead of
picking only the single largest one -- see step 3 below).

MT3 writes its complete multi-instrument prediction into one Standard
MIDI File; it exposes no stable primary-track field, and an apparent
input-filename track name may have been assigned by whatever imported the
file rather than guaranteed MIDI metadata. This module's selection rule is
therefore structural wherever possible, not a guess about audio content:

1. Exclude every track that is drums: General MIDI's percussion channel (MIDI
   channel 9, 0-indexed) is a universal, structural convention, not an
   instrument-family guess, and MT3's own writer places a drum note's channel
   this way unconditionally (see `mt3/note_sequences.py`'s
   `assign_instruments`, which pins a drum note to a fixed instrument index
   regardless of when it occurs -- verified against MT3's real output on two
   songs; see docs/instrument-transcription-findings.md finding 7).
2. Among the remaining tracks, eliminate any track MT3 explicitly *named*
   (every non-first instrument gets a MIDI `track_name` meta event derived
   from its own declared GM program -- see `note_seq`'s MIDI writer) whose
   declared program falls outside the requested target's General MIDI
   instrument family (guitar: 24-31; bass: 32-39). This is reading MT3's own
   declared classification for a track it was confident enough to label, not
   vgt inventing an instrument-family guess. Any *unnamed* track (MT3's
   structurally first-decoded instrument -- see finding 7) is never
   eliminated by this rule: there is nothing to check its label against, so
   it stays a candidate regardless of its numeric program.
3. Among the survivors, merge every *named*, in-family track's notes into one
   combined candidate first: MT3 does not reliably keep one real performance
   in one program (measured for real -- a single electric guitar part split
   across clean/overdriven/distortion-guitar programs purely by tone; see
   docs/instrument-transcription-findings.md finding 7's 7Rivers guitar
   example), and once a track is both named and in-family, that split is
   MT3's own declared classification agreeing it belongs to the target, not
   an ambiguous case worth discarding to a duration tiebreak. Each unnamed
   track, having no label to confirm that agreement, still competes as its
   own separate candidate -- this preserves the original protection a real
   counter-example required (docs' "Perfect_Chcemy-byc-soba" bass case,
   where an always-unnamed track was *not* actually bass and had to lose the
   duration comparison rather than being merged in blind). Select whichever
   candidate -- the merged named-family group, or one of the unnamed tracks
   -- has the most total note *duration* (not note count): a source-
   separated single-instrument stem is expected to be dominated, in how much
   of the track it actually occupies, by its intended instrument. Raw note
   count is gameable by fragmentation -- a spurious track with many short
   notes can outcount a correct track with fewer, longer ones (measured for
   real: see docs/instrument-transcription-findings.md finding 7). Ties
   resolve to the earlier-scanned candidate, deterministically.
4. Record the selected candidate's name(s) for diagnostics when present
   (joined with "+" for a merged group), but never require or select by it.
5. Fail clearly if no surviving track contains a note event.

This module never invokes or provisions MT3 itself (see `vgt.mt3_provision`
for that); it only produces vgt's canonical `ParsedNote`/CSV/MIDI contract
from an already-existing MT3 MIDI file, reusing the same tempo-map-aware
MIDI writer every other backend uses (see `vgt.transcribe.Mt3Transcriber`,
`Mt3Spec`, and the `guitar-mt3`/`bass-mt3` profiles for how it is wired up).
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

# Two independent identity fields (both part of `Mt3Spec`'s serialized
# identity -- see `vgt.transcribe`), bumped separately so retuning one never
# masquerades as a change to the other: `TRACK_SELECTION` covers which track
# `select_dominant_musical_track` picks; `NOTE_NORMALIZATION` covers how its
# note-on/off pairs are converted into `ParsedNote`s once a track is chosen.
# The same role `PYIN_ALGORITHM_VERSION` plays for that backend's identity.
# TRACK_SELECTION bumped 1 -> 2 -> 3 by issue #290 (first: dominant-by-note-
# count with drum-channel exclusion, replacing first-by-decode-order; then:
# dominant-by-duration plus target-aware family elimination of named
# tracks); 3 -> 4 (2026-08-31) merges same-family named survivors into one
# candidate before the duration pick, instead of discarding all but the
# single largest (see the module docstring's step 3 -- found for real on a
# guitar-mt3 run where MT3 split one electric guitar part into
# clean/overdriven/distortion-guitar programs and vgt kept only one of the
# three). Any raw MT3 detection cached under an older version must not be
# reused as if it were selected under the current rule.
MT3_TRACK_SELECTION_VERSION = 4
MT3_NOTE_NORMALIZATION_VERSION = 1

# General MIDI Level 1 program-family ranges (0-indexed), keyed by the vgt
# target they may eliminate a *named* track for. Guitar: patches 25-32 in the
# 1-indexed GM spec (Nylon/Steel/Jazz/Clean/Muted/Overdriven/Distortion
# Guitar, Guitar Harmonics); Bass: patches 33-40 (Acoustic/Electric-finger/
# Electric-pick/Fretless/Slap 1-2/Synth 1-2 Bass).
GM_PROGRAM_FAMILIES: dict[str, range] = {
    "guitar": range(24, 32),
    "bass": range(32, 40),
}

_DEFAULT_TEMPO_USPB = 500_000  # 120 BPM, MIDI's implicit default absent a set_tempo event


@dataclass(frozen=True)
class Mt3SelectedTrack:
    """The dominant (most note-populous, non-drum) track normalized to vgt's
    canonical note form."""

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


_DRUM_CHANNEL = 9  # General MIDI percussion convention, 0-indexed ("channel 10")


def _is_drum_track(track: Any) -> bool:
    """Whether `track`'s notes are on GM's percussion channel.

    MT3's own writer assigns every drum note this channel unconditionally
    (see the module docstring), so this is a structural check, not a guess.
    """
    return any(msg.type in ("note_on", "note_off") and msg.channel == _DRUM_CHANNEL for msg in track)


def _track_program(track: Any) -> int | None:
    """The GM program MT3 declared for `track` via `program_change`, or
    `None` if it never sent one (MIDI's own default would be program 0, but
    the absence itself is what `_is_named_track` uses to grant the pass)."""
    for msg in track:
        if msg.type == "program_change":
            return msg.program
    return None


def _is_named_track(track: Any) -> bool:
    """Whether MT3 gave `track` an explicit `track_name` meta event.

    Every instrument `note_seq` assigns after the structurally-first one gets
    a name derived from its own declared GM program; the first-decoded
    instrument does not (see the module docstring) -- so "named" doubles as
    "MT3 was confident enough to label this instrument," which is exactly the
    condition family elimination should apply to.
    """
    return any(msg.is_meta and msg.type == "track_name" for msg in track)


def _total_duration_s(notes: tuple[ParsedNote, ...]) -> float:
    return sum(note.end_s - note.start_s for note in notes)


def select_dominant_musical_track(path: str | Path, *, target: str | None = None) -> Mt3SelectedTrack:
    """Select and normalize MT3's dominant candidate from `path`: the most
    total note duration, among tracks that are not drums and were not
    explicitly named as a different instrument family -- with every named,
    in-family track merged into one candidate first (see the module
    docstring's step 3).

    `target`, when it names a target in `GM_PROGRAM_FAMILIES` (currently
    "guitar" or "bass"), eliminates every explicitly *named* track whose
    declared GM program falls outside that family, then merges the remaining
    named tracks' notes into one candidate -- MT3 itself confirmed they
    belong to the target's family, and does not reliably keep one real
    performance in one program. Any unnamed track is never eliminated by
    this rule and never merged into the named group either: there is no
    label to confirm it agrees, so each one still competes as its own
    separate candidate (see the module docstring). `None`, or a target with
    no known family, applies no family elimination or merging -- only the
    drum-channel exclusion, with every surviving track its own candidate.

    Raises `TranscriptionError` if the file cannot be parsed or no surviving
    track contains a note event.
    """
    import mido

    try:
        midi = mido.MidiFile(str(path))
    except (OSError, EOFError, ValueError, KeyError, IndexError) as exc:
        raise TranscriptionError(f"{path}: not a valid MIDI file: {exc}") from exc
    if midi.ticks_per_beat <= 0:
        raise TranscriptionError(f"{path}: MIDI file has an invalid ticks-per-beat")

    family = GM_PROGRAM_FAMILIES.get(target) if target else None

    tempo_events = _tempo_events(midi.tracks)
    candidates: list[Mt3SelectedTrack] = []
    named_family_notes: list[ParsedNote] = []
    named_family_names: list[str] = []
    for track in midi.tracks:
        if _is_drum_track(track):
            continue
        named = _is_named_track(track)
        if family is not None and named and _track_program(track) not in family:
            continue
        notes = _extract_track_notes(track, tempo_events, midi.ticks_per_beat)
        if not notes:
            continue
        if family is not None and named:
            named_family_notes.extend(notes)
            name = _track_name(track)
            if name:
                named_family_names.append(name)
        else:
            candidates.append(Mt3SelectedTrack(track_name=_track_name(track), notes=tuple(notes)))
    if named_family_notes:
        named_family_notes.sort(key=lambda note: (note.start_s, note.pitch_midi))
        candidates.append(Mt3SelectedTrack(track_name="+".join(named_family_names) or None, notes=tuple(named_family_notes)))

    best: Mt3SelectedTrack | None = None
    best_duration = 0.0
    for candidate in candidates:  # strictly greater: a tie keeps the earlier-scanned candidate
        duration = _total_duration_s(candidate.notes)
        if duration > best_duration:
            best = candidate
            best_duration = duration
    if best is None:
        family_detail = f" of the {target!r} program family (or unnamed)" if family is not None else ""
        raise TranscriptionError(f"{path}: no surviving MIDI track{family_detail} contains note events")
    return best


def merge_all_musical_tracks(path: str | Path) -> Mt3SelectedTrack:
    """Merge every non-drum track from a `--force-program` MT3 file into one
    `Mt3SelectedTrack` (docs/on-demand-track-transcription-plan.md).

    `--force-program` already pins every non-drum note onto a single GM
    program, so there is nothing left to disambiguate the way
    `select_dominant_musical_track` does: no duration comparison, no
    target-family elimination (there is no target here at all). The one
    remaining split is MT3's own MIDI writer still separating notes across
    *rhythm* (e.g. guitar-lead vs. guitar-rhythm; the pinned checkpoint
    predicts this independently of program -- see `vgt.mt3_provision`'s
    `MT3_MODEL_ID` comment), so a forced-program file can still be two MIDI
    tracks. Concatenating them here is exactly what turns that into one
    usable track.

    Raises `TranscriptionError` if the file cannot be parsed or no surviving
    track contains a note event.
    """
    import mido

    try:
        midi = mido.MidiFile(str(path))
    except (OSError, EOFError, ValueError, KeyError, IndexError) as exc:
        raise TranscriptionError(f"{path}: not a valid MIDI file: {exc}") from exc
    if midi.ticks_per_beat <= 0:
        raise TranscriptionError(f"{path}: MIDI file has an invalid ticks-per-beat")

    tempo_events = _tempo_events(midi.tracks)
    notes: list[ParsedNote] = []
    for track in midi.tracks:
        if _is_drum_track(track):
            continue
        notes.extend(_extract_track_notes(track, tempo_events, midi.ticks_per_beat))
    if not notes:
        raise TranscriptionError(f"{path}: no surviving MIDI track contains note events")
    notes.sort(key=lambda note: (note.start_s, note.pitch_midi))
    return Mt3SelectedTrack(track_name=None, notes=tuple(notes))


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
