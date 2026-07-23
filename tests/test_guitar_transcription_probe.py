"""Regression coverage for the standalone transcription measurement harness."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys

import pytest

import vgt.transcribe as transcribe
from vgt.transcribe import instrument_profile


PROBE_PATH = Path(__file__).parents[1] / "scripts" / "guitar_transcription_probe.py"


def _probe_module():
    spec = importlib.util.spec_from_file_location("guitar_transcription_probe_test", PROBE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_guitar_profiles_keep_the_published_probe_expectations() -> None:
    """The published measurements belong only to the acoustic profile."""
    expectations = instrument_profile("guitar-acoustic").probe_expectations
    assert expectations is not None
    assert expectations.expected_voice_count == 6
    assert expectations.harmonic_ghost_intervals == (12, 19, 24, 28, 31, 36)
    assert expectations.sustain_cap_s == 4.0

    # Generic guitar covers unset/electric guitar types, which have not had
    # the acoustic findings' measurements.  Do not quietly reuse them.
    assert instrument_profile("guitar").probe_expectations is None


def test_selected_profile_with_different_voice_count_changes_only_crowding(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _probe_module()
    notes_csv = tmp_path / "notes.csv"
    notes_csv.write_text("start_time_s,end_time_s,pitch_midi,amplitude\n0,1,60,100\n0,1,64,100\n")
    expectations = instrument_profile("guitar-acoustic").probe_expectations
    assert expectations is not None

    # This is a test-only profile: adding a real instrument and its measured
    # expectations is deliberately outside this issue's scope.  Exercising it
    # through ``main`` proves that named-profile selection, not a direct call
    # to ``report``, controls the expected-voice column.
    monkeypatch.setitem(
        transcribe._INSTRUMENT_PROFILES,
        "test-one-voice",
        replace(
            instrument_profile("guitar-acoustic"),
            name="test-one-voice",
            probe_expectations=replace(expectations, expected_voice_count=1),
        ),
    )

    monkeypatch.setattr(sys, "argv", ["probe", str(notes_csv)])
    probe.main()
    guitar_columns = capsys.readouterr().out.splitlines()[1].split()
    monkeypatch.setattr(sys, "argv", ["probe", str(notes_csv), "--profile", "test-one-voice"])
    probe.main()
    one_voice_columns = capsys.readouterr().out.splitlines()[1].split()

    # The expectation drives only the time-above-voice-limit metric.  The
    # note-event measurements themselves must not change with a profile.
    assert guitar_columns[:7] == one_voice_columns[:7]
    assert guitar_columns[7] != one_voice_columns[7]
    assert guitar_columns[8:] == one_voice_columns[8:]


@pytest.mark.parametrize("profile_name", ["bass", "guitar"])
def test_profile_without_expectations_names_usable_profiles(
    profile_name: str, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _probe_module()
    notes_csv = tmp_path / "notes.csv"
    notes_csv.write_text("start_time_s,end_time_s,pitch_midi,amplitude\n")
    monkeypatch.setattr(sys, "argv", ["probe", str(notes_csv), "--profile", profile_name])

    with pytest.raises(SystemExit):
        probe.main()

    error = capsys.readouterr().err
    assert f"profile {profile_name!r} has no measured probe expectations" in error
    assert "guitar-acoustic" in error


def test_probe_import_does_not_import_or_run_basic_pitch() -> None:
    _probe_module()
    assert "basic_pitch" not in sys.modules
