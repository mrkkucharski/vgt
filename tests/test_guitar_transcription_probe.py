"""Regression coverage for the standalone transcription measurement harness."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys

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
    """The old module constants were six voices, these partials, and four seconds."""
    for name in ("guitar", "guitar-acoustic"):
        expectations = instrument_profile(name).probe_expectations
        assert expectations is not None
        assert expectations.expected_voice_count == 6
        assert expectations.harmonic_ghost_intervals == (12, 19, 24, 28, 31, 36)
        assert expectations.sustain_cap_s == 4.0


def test_different_voice_expectation_changes_only_the_crowding_column(
    tmp_path: Path, capsys
) -> None:
    probe = _probe_module()
    notes_csv = tmp_path / "notes.csv"
    notes_csv.write_text("start_time_s,end_time_s,pitch_midi,amplitude\n0,1,60,100\n0,1,64,100\n")
    expectations = instrument_profile("guitar-acoustic").probe_expectations
    assert expectations is not None

    probe.report(notes_csv, "same", [], expectations)
    guitar_columns = capsys.readouterr().out.split()
    probe.report(notes_csv, "same", [], replace(expectations, expected_voice_count=1))
    one_voice_columns = capsys.readouterr().out.split()

    # The expectation drives only the time-above-voice-limit metric.  The
    # note-event measurements themselves must not change with a profile.
    assert guitar_columns[:7] == one_voice_columns[:7]
    assert guitar_columns[7] != one_voice_columns[7]
    assert guitar_columns[8:] == one_voice_columns[8:]


def test_probe_import_does_not_import_or_run_basic_pitch() -> None:
    _probe_module()
    assert "basic_pitch" not in sys.modules
