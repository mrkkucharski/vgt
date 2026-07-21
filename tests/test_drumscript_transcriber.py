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
    build_drumscript_argv,
    default_spec_for_target,
    transcribed_entry,
)
from vgt.status import _transcription_status


def _spec(**changes):
    spec = default_spec_for_target("drums", backend="drumscript")
    return replace(spec, **changes) if changes else spec


def _midi(channel: int = 9, notes: tuple[int, ...] = (36, 38)) -> bytes:
    track = bytearray()
    for note in notes:  # delta zero makes the onset polyphonic.
        track += bytes([0, 0x90 | channel, note, 100])
    for note in notes:
        track += bytes([20, 0x80 | channel, note, 0])
    track += b"\x00\xff\x2f\x00"
    return b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0" + b"MTrk" + len(track).to_bytes(4, "big") + track


def _run_writing(events, midi: bytes = _midi()):
    def fake_run(argv, *, cwd, **kwargs):
        out = Path(cwd) / "nested"
        out.mkdir()
        (out / "detected.mid").write_bytes(midi)
        import json
        (out / "events.json").write_text(json.dumps(events), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="useful stdout", stderr="useful stderr")
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
        "uvx", "--python", "3.12", "--from", DRUMSCRIPT_PACKAGE_PIN, "drumscript", str(source.resolve()),
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
    assert b"\x00\x99\x24\x64\x00\x99\x26\x64" in data
    assert result.events_path.read_text(encoding="utf-8").startswith("[")


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
    assert "0 events (kick 0, snare 0, hats 5, other 0), drumscript 0.1.6" in format_status(status)


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
