"""BasicPitchTranscriber: pinned `uvx` subprocess backend.

No test here invokes `uvx` or a real model -- `subprocess.run` is monkeypatched
throughout, and `VGT_BASIC_PITCH_CMD` tests point at a tiny fixture Python
script that fabricates outputs instead of running Basic Pitch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from vgt.transcribe import (
    BASIC_PITCH_CMD_ENV,
    BASIC_PITCH_PACKAGE_PIN,
    BASIC_PITCH_SERIALIZATION,
    BasicPitchTranscriber,
    ParsedNote,
    TranscriptionError,
    build_basic_pitch_argv,
    default_spec_for_target,
    parse_notes_csv,
)


def _spec(target: str = "guitar", **overrides):
    # This whole module wants a representative `BasicPitchSpec` -- guitar's
    # own implicit default is now the (unrelated) Essentia backend, so an
    # explicit Basic Pitch profile is needed to keep using "guitar" as the
    # convenient stand-in target these tests were written around.
    modes = {"guitar": "guitar-harmonic"} if target == "guitar" else None
    spec = default_spec_for_target(target, midi_tempo=118.02, modes=modes)
    if overrides:
        from dataclasses import replace

        spec = replace(spec, **overrides)
    return spec


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def test_build_argv_uses_uvx_with_the_pinned_package_and_isolation_flags(tmp_path: Path) -> None:
    source = tmp_path / "guitar.wav"
    destination = tmp_path / "out"

    argv = build_basic_pitch_argv(source, destination, _spec())

    assert argv[:8] == [
        "uvx",
        "--python", "3.11",
        "--with", "setuptools<81",
        "--from", BASIC_PITCH_PACKAGE_PIN,
        "basic-pitch",
    ]
    assert argv[8:10] == [str(destination), str(source)]


def test_build_argv_forces_onnx_model_serialization(tmp_path: Path) -> None:
    argv = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec())

    assert "--model-serialization" in argv
    index = argv.index("--model-serialization")
    assert argv[index + 1] == BASIC_PITCH_SERIALIZATION == "onnx"


def test_build_argv_always_requests_midi_and_note_events(tmp_path: Path) -> None:
    argv = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec())

    assert "--save-midi" in argv
    assert "--save-note-events" in argv


def test_build_argv_includes_midi_tempo_from_the_spec(tmp_path: Path) -> None:
    argv = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec(midi_tempo=93.5))

    index = argv.index("--midi-tempo")
    assert argv[index + 1] == "93.5"


def test_build_argv_omits_midi_tempo_when_none(tmp_path: Path) -> None:
    argv = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec(midi_tempo=None))

    assert "--midi-tempo" not in argv


def test_build_argv_includes_frequency_bounds_for_a_narrowed_target(tmp_path: Path) -> None:
    argv = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec("guitar"))

    min_index = argv.index("--minimum-frequency")
    max_index = argv.index("--maximum-frequency")
    assert argv[min_index + 1] == "70.0"
    assert argv[max_index + 1] == "1400.0"


def test_build_argv_omits_frequency_bounds_for_a_full_range_target(tmp_path: Path) -> None:
    argv = build_basic_pitch_argv(tmp_path / "piano.wav", tmp_path / "out", _spec("piano"))

    assert "--minimum-frequency" not in argv
    assert "--maximum-frequency" not in argv


def test_build_argv_includes_minimum_note_length_and_thresholds(tmp_path: Path) -> None:
    argv = build_basic_pitch_argv(
        tmp_path / "guitar.wav", tmp_path / "out", _spec(minimum_note_length_ms=80.0, onset_threshold=0.6, frame_threshold=0.4)
    )

    assert argv[argv.index("--minimum-note-length") + 1] == "80.0"
    assert argv[argv.index("--onset-threshold") + 1] == "0.6"
    assert argv[argv.index("--frame-threshold") + 1] == "0.4"


def test_build_argv_adds_no_melodia_when_melodia_trick_is_disabled(tmp_path: Path) -> None:
    with_melodia = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec(melodia_trick=True))
    without_melodia = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec(melodia_trick=False))

    assert "--no-melodia" not in with_melodia
    assert "--no-melodia" in without_melodia


def test_build_argv_adds_multiple_pitch_bends_only_when_requested(tmp_path: Path) -> None:
    single = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec(multiple_pitch_bends=False))
    multi = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec(multiple_pitch_bends=True))

    assert "--multiple-pitch-bends" not in single
    assert "--multiple-pitch-bends" in multi


def test_build_argv_rejects_a_non_basic_pitch_spec(tmp_path: Path) -> None:
    spec = _spec(backend="fake")
    with pytest.raises(TranscriptionError):
        build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", spec)


# ---------------------------------------------------------------------------
# VGT_BASIC_PITCH_CMD override
# ---------------------------------------------------------------------------


def test_cmd_env_override_replaces_the_whole_uvx_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BASIC_PITCH_CMD_ENV, "basic-pitch --extra-flag")

    argv = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec())

    assert argv[:3] == ["basic-pitch", "--extra-flag", str(tmp_path / "out")]
    assert "uvx" not in argv


def test_cmd_env_override_falls_back_to_uvx_when_blank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only override (e.g. an exported-but-unset shell var) must
    not silently produce an empty command prefix."""
    monkeypatch.setenv(BASIC_PITCH_CMD_ENV, "   ")

    argv = build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec())

    assert argv[0] == "uvx"


def test_cmd_env_override_is_used_for_the_real_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prebuilt `uv tool install`ed binary can stand in for the whole `uvx`
    invocation -- exercised by pointing the override at a fixture script that
    fabricates a valid MIDI/CSV pair, so no real Basic Pitch model runs."""
    script = _write_fake_basic_pitch_script(tmp_path)
    monkeypatch.setenv(BASIC_PITCH_CMD_ENV, f"{sys.executable} {script}")

    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")
    destination = tmp_path / "out"

    result = BasicPitchTranscriber().transcribe(source, destination, _spec())

    assert result.midi_path == destination / "transcription.mid"
    assert result.notes_path == destination / "transcription.csv"
    assert result.note_count == 2


def _write_fake_basic_pitch_script(tmp_path: Path, *, ragged_bend: bool = True) -> Path:
    """A tiny script standing in for the real `basic-pitch` CLI: given
    `<outdir> <source> ...`, writes a MIDI file and a note-events CSV named
    after the source, exactly like Basic Pitch does, so the renaming step is
    exercised against realistic input."""
    script = tmp_path / "fake_basic_pitch.py"
    bend_row = "0.5,0.9,64,80,10,-5" if ragged_bend else "0.5,0.9,64,80"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            from pathlib import Path

            outdir = Path(sys.argv[1])
            source = Path(sys.argv[2])
            outdir.mkdir(parents=True, exist_ok=True)
            stem = source.stem

            midi_bytes = (
                b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big")
                + (1).to_bytes(2, "big") + (480).to_bytes(2, "big")
                + b"MTrk" + (4).to_bytes(4, "big") + bytes([0x00, 0xFF, 0x2F, 0x00])
            )
            (outdir / f"{{stem}}_basic_pitch.mid").write_bytes(midi_bytes)

            csv_lines = [
                "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend",
                "0.0,0.4,40,90",
                "{bend_row}",
            ]
            (outdir / f"{{stem}}_basic_pitch.csv").write_text("\\n".join(csv_lines) + "\\n")
            """
        ),
        encoding="utf-8",
    )
    return script


# ---------------------------------------------------------------------------
# lenient CSV parsing
# ---------------------------------------------------------------------------


def test_parse_notes_csv_reads_a_ragged_bend_row(tmp_path: Path) -> None:
    path = tmp_path / "notes.csv"
    path.write_text(
        "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n"
        "0.0,0.5,40,90\n"
        "0.5,1.2,52,70,10,-5,3\n",
        encoding="utf-8",
    )

    notes = parse_notes_csv(path)

    assert notes == [
        ParsedNote(0.0, 0.5, 40, 90, ()),
        ParsedNote(0.5, 1.2, 52, 70, (10.0, -5.0, 3.0)),
    ]


def test_parse_notes_csv_rejects_a_row_with_too_few_fields(tmp_path: Path) -> None:
    path = tmp_path / "notes.csv"
    path.write_text("start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n0.0,0.5,40\n", encoding="utf-8")

    with pytest.raises(TranscriptionError):
        parse_notes_csv(path)


def test_parse_notes_csv_rejects_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(TranscriptionError):
        parse_notes_csv(path)


def test_parse_notes_csv_tolerates_a_single_trailing_comma(tmp_path: Path) -> None:
    path = tmp_path / "notes.csv"
    path.write_text(
        "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n0.0,0.5,40,90,\n", encoding="utf-8"
    )

    notes = parse_notes_csv(path)

    assert notes == [ParsedNote(0.0, 0.5, 40, 90, ())]


def test_parse_notes_csv_rejects_an_empty_field_inside_the_bend_series(tmp_path: Path) -> None:
    path = tmp_path / "notes.csv"
    path.write_text(
        "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n0.5,1.2,52,70,10,,3\n", encoding="utf-8"
    )

    with pytest.raises(TranscriptionError):
        parse_notes_csv(path)


# ---------------------------------------------------------------------------
# artifact renaming
# ---------------------------------------------------------------------------


def test_transcribe_renames_basic_pitch_named_outputs_to_stable_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")
    destination = tmp_path / "out"

    def fake_run(argv, **kwargs):
        outdir = Path(argv[argv.index(str(destination))])
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "guitar_basic_pitch.mid").write_bytes(_valid_midi_bytes())
        (outdir / "guitar_basic_pitch.csv").write_text(
            "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n0.0,0.5,40,90\n0.5,1.0,52,70,10,-5\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    result = BasicPitchTranscriber().transcribe(source, destination, _spec())

    assert result.midi_path == destination / "transcription.mid"
    assert result.notes_path == destination / "transcription.csv"
    assert not (destination / "guitar_basic_pitch.mid").exists()
    assert not (destination / "guitar_basic_pitch.csv").exists()
    assert result.note_count == 2
    assert result.pitch_range_midi == (40, 52)
    assert result.first_note_s == 0.0
    assert result.last_note_s == 1.0


def test_transcribe_retry_against_a_reused_destination_dir_ignores_stale_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover `transcription.mid`/`.csv` from a prior run in the same
    `destination_dir` (e.g. an orchestrator retry) must not corrupt the
    exactly-one-file check or be mistaken for the retry's own output."""
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "transcription.mid").write_bytes(b"stale-not-even-valid-midi")
    (destination / "transcription.csv").write_text("stale\n", encoding="utf-8")

    def fake_run(argv, **kwargs):
        (destination / "guitar_basic_pitch.mid").write_bytes(_valid_midi_bytes())
        (destination / "guitar_basic_pitch.csv").write_text(
            "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n0.0,0.5,40,90\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    result = BasicPitchTranscriber().transcribe(source, destination, _spec())

    assert result.note_count == 1
    assert result.midi_path.read_bytes() == _valid_midi_bytes()


def _valid_midi_bytes() -> bytes:
    return (
        b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big")
        + (1).to_bytes(2, "big") + (480).to_bytes(2, "big")
        + b"MTrk" + (4).to_bytes(4, "big") + bytes([0x00, 0xFF, 0x2F, 0x00])
    )


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------


def test_transcribe_raises_when_uvx_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")

    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(TranscriptionError, match="uvx"):
        BasicPitchTranscriber().transcribe(source, tmp_path / "out", _spec())


def test_transcribe_raises_with_stderr_tail_on_non_zero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom: onnxruntime failed to load")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    with pytest.raises(TranscriptionError, match="onnxruntime failed to load"):
        BasicPitchTranscriber().transcribe(source, tmp_path / "out", _spec())


def test_transcribe_raises_on_malformed_output_missing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")
    destination = tmp_path / "out"

    def fake_run(argv, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    with pytest.raises(TranscriptionError, match="expected exactly one"):
        BasicPitchTranscriber().transcribe(source, destination, _spec())


def test_transcribe_raises_on_malformed_output_invalid_midi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")
    destination = tmp_path / "out"

    def fake_run(argv, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "guitar_basic_pitch.mid").write_bytes(b"not-a-midi-file")
        (destination / "guitar_basic_pitch.csv").write_text(
            "start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n0.0,0.5,40,90\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    with pytest.raises(TranscriptionError, match="not a valid MIDI file"):
        BasicPitchTranscriber().transcribe(source, destination, _spec())


def test_transcribe_raises_when_command_cannot_be_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    with pytest.raises(TranscriptionError):
        BasicPitchTranscriber().transcribe(source, tmp_path / "out", _spec())


def test_transcribe_raises_when_destination_dir_cannot_be_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing plain file at `destination_dir`'s path (e.g. a stale
    artifact left over from an earlier, differently-shaped run) must not
    escape as a raw FileExistsError -- it's exactly the kind of malformed
    on-disk state this backend has to turn into a TranscriptionError so the
    orchestrator can mark just this target failed and continue."""
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")
    blocker = tmp_path / "out"
    blocker.write_bytes(b"i-am-a-file-not-a-directory")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    with pytest.raises(TranscriptionError, match="could not prepare"):
        BasicPitchTranscriber().transcribe(source, blocker, _spec())


def test_transcribe_tolerates_non_utf8_stderr_on_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashing subprocess dependency (onnxruntime, a locale-encoded error
    message) can put non-UTF-8 bytes on stderr. That must still surface as a
    TranscriptionError with whatever stderr could be captured, not crash the
    whole call with an unhandled UnicodeDecodeError."""
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio")

    def fake_run(argv, **kwargs):
        assert kwargs.get("errors") == "replace"
        return SimpleNamespace(returncode=1, stdout="", stderr="boom � tail")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uvx")

    with pytest.raises(TranscriptionError, match="boom"):
        BasicPitchTranscriber().transcribe(source, tmp_path / "out", _spec())


def test_cmd_env_override_with_unbalanced_quoting_raises_transcription_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-set `VGT_BASIC_PITCH_CMD` with a shell-quoting mistake must
    raise TranscriptionError, not a raw shlex ValueError."""
    monkeypatch.setenv(BASIC_PITCH_CMD_ENV, 'basic-pitch --model-dir "unterminated')

    with pytest.raises(TranscriptionError, match=BASIC_PITCH_CMD_ENV):
        build_basic_pitch_argv(tmp_path / "guitar.wav", tmp_path / "out", _spec())
