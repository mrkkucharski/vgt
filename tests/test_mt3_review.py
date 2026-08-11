from __future__ import annotations

from pathlib import Path

import mido

from vgt.mt3_review import split_mt3_midi


def test_split_mt3_midi_keeps_every_note_bearing_track_independently(tmp_path: Path) -> None:
    source = tmp_path / "instrumental.mid"
    midi = mido.MidiFile(ticks_per_beat=480)
    midi.tracks.append(mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500_000, time=0)]))
    for name, program, note in (("Acoustic Grand Piano", 0, 60), ("Drums", 0, 36)):
        track = mido.MidiTrack([mido.MetaMessage("track_name", name=name, time=0), mido.Message("program_change", program=program, time=0)])
        channel = 9 if name == "Drums" else 0
        track.extend([mido.Message("note_on", channel=channel, note=note, velocity=90, time=0), mido.Message("note_off", channel=channel, note=note, velocity=0, time=480)])
        midi.tracks.append(track)
    midi.save(source)

    tracks = split_mt3_midi(source, tmp_path / "tracks")

    assert [(track["name"], track["file"]) for track in tracks] == [
        ("Acoustic Grand Piano", "02-acoustic-grand-piano.mid"),
        ("Drums", "03-drums.mid"),
    ]
    for track in tracks:
        output = mido.MidiFile(tmp_path / "tracks" / str(track["file"]))
        assert sum(message.type == "note_on" and message.velocity > 0 for midi_track in output.tracks for message in midi_track) == 1
