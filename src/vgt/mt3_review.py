"""Keep MT3's complete instrumental prediction available for REAPER review.

The normal MT3 profiles deliberately select one target-specific track.  The
analysis review pass is different: it preserves every note-bearing MT3 track
as a separate MIDI file so the user, rather than vgt, can decide what matters.
"""

from __future__ import annotations

from copy import copy
from pathlib import Path
import re

from .transcribe import TranscriptionError


def _safe_name(value: str, index: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return f"{index:02d}-{cleaned or 'unnamed'}"


def split_mt3_midi(source: Path, destination: Path) -> list[dict[str, object]]:
    """Split a multi-track MT3 MIDI into named, independently importable MIDI.

    Conductor-only tracks are ignored.  Original event timing, channel and GM
    program metadata are retained; a tempo map is copied to every output so a
    track remains usable on its own.
    """
    import mido

    try:
        midi = mido.MidiFile(str(source))
    except (OSError, ValueError, EOFError) as exc:
        raise TranscriptionError(f"could not read MT3 MIDI: {exc}") from exc
    tempos = [copy(msg) for track in midi.tracks for msg in track if msg.type == "set_tempo"]
    destination.mkdir(parents=True, exist_ok=True)
    tracks: list[dict[str, object]] = []
    for index, track in enumerate(midi.tracks, start=1):
        if not any(msg.type == "note_on" and msg.velocity > 0 for msg in track):
            continue
        name = next((msg.name for msg in track if msg.is_meta and msg.type == "track_name"), "unnamed")
        filename = _safe_name(name, index) + ".mid"
        out = mido.MidiFile(ticks_per_beat=midi.ticks_per_beat)
        out.tracks.append(mido.MidiTrack([*tempos, mido.MetaMessage("end_of_track", time=0)]))
        out.tracks.append(mido.MidiTrack([copy(msg) for msg in track]))
        out.save(str(destination / filename))
        programs = [msg.program for msg in track if msg.type == "program_change"]
        tracks.append({"file": filename, "name": name, "program": programs[0] if programs else None})
    if not tracks:
        raise TranscriptionError("MT3 output contains no note-bearing tracks")
    return tracks
