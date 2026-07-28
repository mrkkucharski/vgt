"""Offline tests for the pinned DrumScript subprocess backend.

Every subprocess here is a monkeypatched fixture writer; no test runs uvx or
imports/downloads DrumScript.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from vgt.transcribe import (
    DRUMSCRIPT_CMD_ENV,
    DRUMSCRIPT_PACKAGE_PIN,
    DRUMSCRIPT_RUNTIME_VERSION,
    DrumScriptTranscriber,
    TranscriptionError,
    _varlen,
    build_drumscript_argv,
    default_spec_for_target,
    transcribed_entry,
)
from vgt.status import _transcription_status


def _spec(**changes):
    spec = default_spec_for_target("drums", backend="drumscript")
    return replace(spec, **changes) if changes else spec


def _midi(
    channel: int = 9, notes: tuple[int, ...] = (36, 38), tempo_bpm: float | None = None, onset_s: float = 0.0
) -> bytes:
    """A minimal fixture SMF placing `notes` simultaneously at `onset_s`,
    decoded/encoded using `tempo_bpm` (matching `_read_percussion_note_velocities`'s
    120 BPM fallback when absent) -- so a caller can construct a DrumScript
    MIDI whose note-on tick actually corresponds to a given real second, as
    real DrumScript output does."""
    ticks_per_second = 480 * (tempo_bpm or 120.0) / 60.0
    onset_tick = int(round(onset_s * ticks_per_second))
    track = bytearray()
    if tempo_bpm is not None:
        tempo_uspb = int(round(60_000_000 / tempo_bpm))
        track += b"\x00\xff\x51\x03" + tempo_uspb.to_bytes(3, "big")
    for index, note in enumerate(notes):  # delta zero between them makes the onset polyphonic.
        track += _varlen(onset_tick if index == 0 else 0) + bytes([0x90 | channel, note, 100])
    for index, note in enumerate(notes):
        track += _varlen(20 if index == 0 else 0) + bytes([0x80 | channel, note, 0])
    track += b"\x00\xff\x2f\x00"
    return b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0" + b"MTrk" + len(track).to_bytes(4, "big") + track


def _run_writing(events, midi: bytes = _midi(), stdout: str = "useful stdout"):
    def fake_run(argv, *, cwd, **kwargs):
        out = Path(cwd) / "nested"
        out.mkdir()
        (out / "detected.mid").write_bytes(midi)
        import json
        (out / "events.json").write_text(json.dumps(events), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="useful stderr")
    return fake_run


def _transcribe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events, midi: bytes = _midi()):
    source = tmp_path / "drums.wav"
    source.write_bytes(b"not decoded by this unit test")
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/uvx")
    monkeypatch.setattr(subprocess, "run", _run_writing(events, midi))
    monkeypatch.setattr("vgt.transcribe._source_duration_seconds", lambda _source: 10.0)
    return DrumScriptTranscriber().transcribe(source, tmp_path / "destination", _spec())


def test_argv_is_pinned_isolated_and_never_enables_full_song(tmp_path: Path) -> None:
    source = tmp_path / "drums.wav"
    argv = build_drumscript_argv(source, _spec())

    assert argv == [
        "uvx", "--python", "3.12", "--from", DRUMSCRIPT_PACKAGE_PIN, "python", "-m", "drumscript.main", str(source.resolve()),
    ]
    assert "--full-song" not in argv
    assert DRUMSCRIPT_RUNTIME_VERSION == "python==3.12"


def test_command_override_is_shell_split_not_shell_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DRUMSCRIPT_CMD_ENV, 'offline-drumscript --model "drum kit"')
    argv = build_drumscript_argv(tmp_path / "drums.wav", _spec())
    assert argv[:3] == ["offline-drumscript", "--model", "drum kit"]
    assert "uvx" not in argv


def test_normalizes_valid_outputs_and_preserves_polyphonic_channel_10_midi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _transcribe(tmp_path, monkeypatch, [{"time_sec": 1.25, "instruments": ["kick", "snare"]}])

    assert result.midi_path == tmp_path / "destination" / "transcription.mid"
    assert result.events_path == tmp_path / "destination" / "transcription.json"
    assert result.note_count == 1
    assert result.instrument_counts == {"kick": 1, "snare": 1}
    data = result.midi_path.read_bytes()
    # Both instruments share one event's onset (issue #193 re-authors from
    # `raw_events`, so they land on the same tick), not necessarily tick 0 --
    # the exact byte offset depends on the (project) authoring tempo.
    assert b"\x99\x24\x64\x00\x99\x26\x64" in data
    assert result.events_path.read_text(encoding="utf-8").startswith("[")


def test_drum_midi_tempo_uses_the_project_tempo_not_drumscripts_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #193: DrumScript's own tempo detection can be wrong (a half-
    tempo octave error authoring 60 BPM against a 120 BPM project), so vgt
    overrides at its own boundary with `spec.midi_tempo` -- the recorded
    `midi_tempo` must be the project's tempo, never whatever DrumScript
    authored its own MIDI at."""
    source = tmp_path / "drums.wav"
    source.write_bytes(b"not decoded by this unit test")
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/uvx")
    monkeypatch.setattr(subprocess, "run", _run_writing(
        [{"time_sec": 1.25, "instruments": ["kick"]}], _midi(tempo_bpm=60.09), "Detected tempo: 60.09 BPM"
    ))
    monkeypatch.setattr("vgt.transcribe._source_duration_seconds", lambda _source: 10.0)

    spec = _spec(midi_tempo=120.004)
    result = DrumScriptTranscriber().transcribe(source, tmp_path / "destination", spec)
    entry = transcribed_entry(
        spec, source_role="drums", input_hash="stem-hash", target="drums", result=result, transcribed_at="2026-07-21T00:00:00Z"
    )

    assert entry["backend_tempo"] == pytest.approx(60.09)
    assert entry["midi_tempo"] == pytest.approx(120.004)
    assert entry["first_note_s"] is entry["last_note_s"] is None
    assert entry["first_event_s"] == entry["last_event_s"] == 1.25


def _note_on_tick(data: bytes, pitch: int) -> int | None:
    """The tick offset of `pitch`'s note-on in a written SMF, or `None` if
    absent -- a minimal decoder for asserting on `_write_midi`'s output
    without depending on the module's internal byte layout."""
    header_length = int.from_bytes(data[4:8], "big")
    index = 8 + header_length
    length = int.from_bytes(data[index + 4:index + 8], "big")
    track_end = index + 8 + length
    index += 8
    tick = 0
    while index < track_end:
        delta = 0
        while True:
            byte = data[index]
            index += 1
            delta = (delta << 7) | (byte & 0x7F)
            if not (byte & 0x80):
                break
        tick += delta
        status = data[index]
        index += 1
        if status == 0xFF:
            index += 1
            payload_length = data[index]
            index += 1 + payload_length
        elif 0x90 <= status <= 0x9F:
            note, velocity = data[index], data[index + 1]
            index += 2
            if note == pitch and velocity > 0:
                return tick
        elif 0x80 <= status <= 0x8F:
            index += 2
        else:
            break
    return None


def test_default_profile_reauthors_midi_at_the_project_tempo_not_drumscripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #193's core fix: a hit near the end of a long stem must land at
    its real second under the *project* tempo grid, not be squeezed toward
    the front the way byte-copying DrumScript's 60 BPM MIDI did."""
    source = tmp_path / "drums.wav"
    source.write_bytes(b"not decoded by this unit test")
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/uvx")
    monkeypatch.setattr(subprocess, "run", _run_writing(
        [{"time_sec": 150.0, "instruments": ["kick"]}], _midi(tempo_bpm=60.09, notes=(36,), onset_s=150.0)
    ))
    monkeypatch.setattr("vgt.transcribe._source_duration_seconds", lambda _source: 160.0)

    spec = _spec(midi_tempo=120.0)
    result = DrumScriptTranscriber().transcribe(source, tmp_path / "destination", spec)

    data = result.midi_path.read_bytes()
    tick = _note_on_tick(data, 36)
    assert tick is not None
    # 480 ticks/beat, 120 BPM -> 960 ticks/second; a note at real second 150
    # must be authored at that tick, not DrumScript's 60 BPM tick (75_000,
    # which would play back at half the real elapsed time under a 120 BPM
    # project grid).
    assert tick == pytest.approx(150.0 * 960, abs=1)


def test_default_profile_preserves_drumscripts_velocity_when_reauthoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `default` event JSON has no velocity field (see
    `parse_drumscript_events`), so re-authoring the MIDI at the project
    tempo (issue #193) must recover each note's velocity from DrumScript's
    own MIDI rather than falling back to a flat default."""
    source = tmp_path / "drums.wav"
    source.write_bytes(b"not decoded by this unit test")
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/uvx")
    drumscript_midi = _midi(tempo_bpm=60.09, notes=(36, 38), onset_s=2.5)
    # Distinguish the two notes' velocities from `_midi`'s flat 100 default.
    drumscript_midi = drumscript_midi.replace(bytes([0x99, 36, 100]), bytes([0x99, 36, 57]))
    drumscript_midi = drumscript_midi.replace(bytes([0x99, 38, 100]), bytes([0x99, 38, 111]))
    monkeypatch.setattr(subprocess, "run", _run_writing(
        [{"time_sec": 2.5, "instruments": ["kick", "snare"]}], drumscript_midi
    ))
    monkeypatch.setattr("vgt.transcribe._source_duration_seconds", lambda _source: 10.0)

    spec = _spec(midi_tempo=120.0)
    result = DrumScriptTranscriber().transcribe(source, tmp_path / "destination", spec)

    data = result.midi_path.read_bytes()
    assert bytes([0x99, 36, 57]) in data
    assert bytes([0x99, 38, 111]) in data


@pytest.mark.parametrize(
    "events, message",
    [
        ({"time_sec": 1}, "must be an array"),
        ([{"time_sec": -1, "instruments": ["kick"]}], "invalid time_sec"),
        ([{"time_sec": 1, "instruments": []}], "empty or invalid"),
        ([{"time_sec": 1, "instruments": ["cowbell"]}], "unsupported instruments"),
        ([{"time_sec": 99, "instruments": ["kick"]}], "implausibly beyond"),
    ],
)
def test_rejects_malformed_or_unusable_event_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events, message: str) -> None:
    with pytest.raises(TranscriptionError, match=message):
        _transcribe(tmp_path, monkeypatch, events)


def test_zero_event_array_is_a_valid_visible_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _transcribe(tmp_path, monkeypatch, [], _midi(notes=()))
    assert result.note_count == 0
    assert result.instrument_counts == {}
    assert result.first_note_s is None
    assert result.last_note_s is None


def test_rejects_midi_notes_outside_channel_10(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TranscriptionError, match="percussion channel 10"):
        _transcribe(tmp_path, monkeypatch, [], _midi(channel=0, notes=(36,)))


def test_drum_result_sidecar_uses_stable_target_midi_and_event_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _transcribe(tmp_path, monkeypatch, [{"time_sec": 1, "instruments": ["kick"]}])
    entry = transcribed_entry(
        _spec(), source_role="drums", input_hash="stem-hash", target="drums", result=result, transcribed_at="2026-07-21T00:00:00Z"
    )
    assert entry["midi_file"] == "transcription/drums.mid"
    assert entry["events_file"] == "transcription/drums.json"
    assert entry["notes_file"] is None
    assert entry["event_count"] == 1
    assert entry["first_event_s"] == 1.0
    assert entry["last_event_s"] == 1.0
    assert entry["pitch_range_midi"] is None
    assert entry["confidence"] is None


def test_zero_event_drum_entry_remains_visible_in_status() -> None:
    status = _transcription_status({
        "transcription": {
            "requested_targets": ["drums"],
            "targets": {"drums": {"status": "transcribed", "event_count": 0, "instrument_counts": {}}},
        }
    })
    assert status["targets"]["drums"]["event_count"] == 0


def test_drum_status_groups_hats_and_keeps_zero_events_visible() -> None:
    from vgt.status import format_status

    status = {
        "project": {"path": "song.rpp"}, "sidecar": {"path": "song.vgt", "schema_version": 9},
        "reference_track": {"name": None, "guid": None, "source_path": None, "source_error": None, "source_exists": None},
        "managed_area": {"managed_track_guids": [], "folder_name": None, "tempo_map_applied": None},
        "stages": {},
        "transcription": {"package_pin": None, "backend": None, "requested_targets": ["drums"], "targets": {
            "drums": {"status": "transcribed", "event_count": 0, "instrument_counts": {"hi_hat_closed": 2, "hi_hat_open": 3}, "package_pin": "drumscript==0.1.6", "backend": "drumscript", "transcribed_at": None}
        }},
        "timestamps": {"last_analysis_at": None, "last_human_correction_at": None}, "artifacts": {},
        "stems": {"guitar_type": None, "human_verified": False, "operations": {}, "artifacts": {}},
    }
    assert "0 events (kick 0, snare 0, hats 5, other 0), profile default, drumscript 0.1.6" in format_status(status)


def test_rejects_duplicate_or_missing_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/uvx")
    monkeypatch.setattr("vgt.transcribe._source_duration_seconds", lambda _source: 10.0)

    def fake_run(argv, *, cwd, **kwargs):
        work = Path(cwd)
        (work / "a.mid").write_bytes(_midi())
        (work / "b.mid").write_bytes(_midi())
        (work / "events.json").write_text("[]", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TranscriptionError, match="2 MIDI"):
        DrumScriptTranscriber().transcribe(source, tmp_path / "dest", _spec())


def test_rejects_artifact_symlink_outside_temporary_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"audio")
    outside = tmp_path / "outside.mid"
    outside.write_bytes(_midi())
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/uvx")

    def fake_run(argv, *, cwd, **kwargs):
        work = Path(cwd)
        (work / "detected.mid").symlink_to(outside)
        (work / "events.json").write_text("[]", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TranscriptionError, match="outside its temporary output") as error:
        DrumScriptTranscriber().transcribe(source, tmp_path / "dest", _spec())
    assert "vgt-drumscript-" not in str(error.value)


def test_subprocess_failure_includes_context_but_not_temporary_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/uvx")

    def fake_run(argv, *, cwd, **kwargs):
        return SimpleNamespace(returncode=7, stdout=f"output at {cwd}", stderr=f"bad at {cwd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TranscriptionError, match="status 7") as error:
        DrumScriptTranscriber().transcribe(source, tmp_path / "dest", _spec())
    assert "useful" not in str(error.value)
    assert "bad at <temporary output>" in str(error.value)
    assert "vgt-drumscript-" not in str(error.value)
