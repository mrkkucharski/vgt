from pathlib import Path
import json
import shutil

import pytest

from vgt import analysis as analysis_module
from vgt.analysis import AnalysisError, analyze
from vgt.cli import main
from vgt.sidecar import ANALYSIS_STAGES, read_sidecar, upgrade, write_sidecar


FIXTURE_DIR = Path(__file__).parents[1] / "test" / "Reaper Project"
FIXTURE = FIXTURE_DIR / "Reaper Project.RPP"
REFERENCE_GUID = "{75418143-1F31-B548-B7D2-96815CB0297D}"  # "The Seven Rivers ..." track


def _project_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "Reaper Project"
    shutil.copytree(FIXTURE_DIR, destination)
    return destination / "Reaper Project.RPP"


def _write_v1_sidecar(project: Path) -> Path:
    sidecar = project.with_suffix(".vgt")
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "managed_track_guids": ["{AAAA}", "{BBBB}"],
                "config": {
                    "reference_track_name": "The Seven Rivers (Full March - 3_00)",
                    "reference_track_guid": REFERENCE_GUID,
                    "folder_name": "[vgt] The Seven Rivers (Full March - 3_00)",
                    "mirror_name": "[vgt] Mirror",
                },
            }
        )
    )
    return sidecar


def test_upgrade_keeps_v1_fields_and_adds_v2_analysis_skeleton() -> None:
    v1 = {
        "schema_version": 1,
        "managed_track_guids": ["{AAAA}", "{BBBB}"],
        "config": {"reference_track_guid": REFERENCE_GUID},
    }

    upgraded = upgrade(v1)

    assert upgraded["schema_version"] == 3
    assert upgraded["managed_track_guids"] == ["{AAAA}", "{BBBB}"]
    assert upgraded["config"] == {"reference_track_guid": REFERENCE_GUID}
    for stage in ANALYSIS_STAGES:
        expected = {
            "value": None,
            "human_verified": False,
            "input_hash": None,
            "settings_hash": None,
        }
        if stage == "chords":
            expected["detected"] = None
        assert upgraded["analysis"][stage] == expected
    assert upgraded["analysis"]["provenance"]["tool"] == "vgt"


def test_upgrade_backfills_detected_from_value_for_v2_chords() -> None:
    """A v2 sidecar has no `detected` field on the chords stage -- the v2 -> v3
    migration seeds it from `value` (best effort; if `value` was already a
    human correction, the true original detection is unrecoverable)."""
    v2 = {
        "schema_version": 2,
        "config": {"reference_track_guid": REFERENCE_GUID},
        "analysis": {
            "chords": {
                "value": {"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C"}]},
                "human_verified": True,
                "input_hash": "abc",
                "settings_hash": "def",
            }
        },
    }

    upgraded = upgrade(v2)

    chords = upgraded["analysis"]["chords"]
    assert chords["detected"] == chords["value"]
    assert chords["detected"] is not chords["value"]  # backfill copies, doesn't alias


def test_upgrade_does_not_clobber_an_existing_detected_field() -> None:
    v3 = {
        "schema_version": 3,
        "analysis": {
            "chords": {
                "value": {"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C"}]},
                "detected": {"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "G"}]},
                "human_verified": True,
                "input_hash": "abc",
                "settings_hash": "def",
            }
        },
    }

    upgraded = upgrade(v3)

    assert upgraded["analysis"]["chords"]["detected"]["segments"][0]["chord"] == "G"


def test_analyze_requires_an_existing_sidecar(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)

    with pytest.raises(AnalysisError, match="No .vgt sidecar"):
        analyze(project)


def test_analyze_writes_v2_sidecar_with_skeleton_and_provenance(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    result = analyze(project)

    assert result["schema_version"] == 3
    assert result["managed_track_guids"] == ["{AAAA}", "{BBBB}"]  # phase 0 fields intact
    for stage in ANALYSIS_STAGES:
        assert result["analysis"][stage]["input_hash"] is not None
        assert result["analysis"][stage]["human_verified"] is False
    provenance = result["analysis"]["provenance"]
    assert provenance["tool"] == "vgt"
    assert provenance["reference_source_path"].endswith("The Seven Rivers (Full March - 3_00).mp3")

    tempo = result["analysis"]["tempo"]["value"]
    assert tempo["bpm"] == pytest.approx(120.0, abs=1.0)
    assert tempo["time_signature"] == "4/4"
    assert tempo["mode"] in {"constant", "piecewise"}
    assert tempo["backend"] in {"madmom", "librosa"}
    assert len(tempo["beat_times"]) > 1
    click_artifact = project.with_name(tempo["click_artifact_path"])
    assert click_artifact.is_file()

    key = result["analysis"]["key"]["value"]
    assert key["root"] in {
        "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
    }
    assert key["scale"] in {"major", "minor"}
    assert 0.0 <= key["confidence"] <= 1.0

    sections = result["analysis"]["sections"]["value"]
    assert len(sections) > 0
    assert sections[0]["start_seconds"] == 0.0
    assert sections[-1]["end_seconds"] > sections[-1]["start_seconds"]
    for earlier, later in zip(sections, sections[1:]):
        assert earlier["end_seconds"] == later["start_seconds"]
        assert earlier["label"]
    timeline = project.with_name(f"{project.stem}.vgt-sections.txt")
    assert timeline.is_file()

    chords = result["analysis"]["chords"]["value"]
    assert chords["vocabulary"] == "maj_min"
    assert len(chords["segments"]) > 0
    tempo_beat_times = set(tempo["beat_times"])
    for segment in chords["segments"]:
        assert segment["end_seconds"] > segment["start_seconds"]
        # Every chord boundary must land on the shared tempo-stage beat grid,
        # not some independently detected grid.
        assert segment["start_seconds"] in tempo_beat_times
        assert segment["end_seconds"] in tempo_beat_times
    chord_sheet = project.with_name(chords["chord_sheet_path"])
    assert chord_sheet.is_file()

    # No correction has been made yet, so `detected` mirrors `value` exactly.
    assert result["analysis"]["chords"]["detected"] == chords

    on_disk = read_sidecar(project)
    assert on_disk == result


def test_analyze_is_idempotent(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    first = analyze(project)
    second = analyze(project)

    assert first == second


def test_stage_cache_only_refreshes_the_stage_with_changed_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    calls = {stage: 0 for stage in ANALYSIS_STAGES}

    def detector(stage: str):
        def detect(
            _project_path: Path, _source: Path, _settings: dict[str, object], _analysis: dict[str, object]
        ) -> dict[str, str]:
            calls[stage] += 1
            return {"stage": stage}

        return detect

    for stage in ANALYSIS_STAGES:
        monkeypatch.setitem(analysis_module._DETECTORS, stage, detector(stage))

    analyze(project)
    analyze(project, settings={"tempo": {"sensitivity": "high"}})

    assert calls == {"tempo": 2, "key": 1, "sections": 1, "chords": 1}


def test_manual_correction_survives_rerun(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    analyze(project)

    sidecar = read_sidecar(project)
    sidecar["analysis"]["tempo"] = {
        "value": {"bpm": 118.0, "downbeat_offset_seconds": 0.25, "time_signature": "4/4"},
        "human_verified": True,
        "input_hash": sidecar["analysis"]["tempo"]["input_hash"],
        "settings_hash": sidecar["analysis"]["tempo"]["settings_hash"],
    }
    write_sidecar(project, sidecar)

    result = analyze(project)

    assert result["analysis"]["tempo"]["value"] == {
        "bpm": 118.0,
        "downbeat_offset_seconds": 0.25,
        "time_signature": "4/4",
    }
    assert result["analysis"]["tempo"]["human_verified"] is True
    # Untouched stages still refresh normally.
    assert result["analysis"]["key"]["human_verified"] is False


def test_chords_fall_back_to_freshly_detected_beats_when_tempo_correction_omits_them(tmp_path: Path) -> None:
    """A human tempo correction that only sets bpm/downbeat/time-signature
    (no beat_times array) must not break the chords stage's grid-snapping --
    it should fall back to detecting beats fresh via the same tempo.py ladder,
    not chords' own ad hoc beat tracker."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    analyze(project)

    sidecar = read_sidecar(project)
    sidecar["analysis"]["tempo"] = {
        "value": {"bpm": 118.0, "downbeat_offset_seconds": 0.25, "time_signature": "4/4"},
        "human_verified": True,
        "input_hash": sidecar["analysis"]["tempo"]["input_hash"],
        "settings_hash": sidecar["analysis"]["tempo"]["settings_hash"],
    }
    write_sidecar(project, sidecar)

    # Force the chords stage to recompute despite the unchanged audio input.
    result = analyze(project, settings={"chords": {"note": "force-recompute"}})

    chords = result["analysis"]["chords"]["value"]
    assert len(chords["beat_times"]) > 1
    beat_times = set(chords["beat_times"])
    for segment in chords["segments"]:
        assert segment["start_seconds"] in beat_times
        assert segment["end_seconds"] in beat_times


def test_key_and_chord_corrections_survive_rerun(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    analyze(project)

    sidecar = read_sidecar(project)
    sidecar["analysis"]["key"] = {
        "value": {"root": "E", "scale": "minor", "confidence": 1.0, "backend": "human"},
        "human_verified": True,
        "input_hash": sidecar["analysis"]["key"]["input_hash"],
        "settings_hash": sidecar["analysis"]["key"]["settings_hash"],
    }
    corrected_chords = {
        "segments": [{"start_seconds": 0.0, "end_seconds": 2.0, "chord": "E:min"}],
        "beat_times": [0.0, 1.0, 2.0],
        "vocabulary": "maj_min",
        "backend": "human",
        "chord_sheet_path": sidecar["analysis"]["chords"]["value"]["chord_sheet_path"],
    }
    sidecar["analysis"]["chords"] = {
        "value": corrected_chords,
        "human_verified": True,
        "input_hash": sidecar["analysis"]["chords"]["input_hash"],
        "settings_hash": sidecar["analysis"]["chords"]["settings_hash"],
    }
    write_sidecar(project, sidecar)

    result = analyze(project)

    assert result["analysis"]["key"]["value"] == {
        "root": "E",
        "scale": "minor",
        "confidence": 1.0,
        "backend": "human",
    }
    assert result["analysis"]["key"]["human_verified"] is True
    assert result["analysis"]["chords"]["value"] == corrected_chords
    assert result["analysis"]["chords"]["human_verified"] is True
    # The original detection (backfilled from `value` since this sidecar was
    # written without a `detected` field) is preserved, not overwritten by
    # the correction.
    assert result["analysis"]["chords"]["detected"] == corrected_chords


def test_read_chords_style_correction_preserves_original_detected(tmp_path: Path) -> None:
    """Mirrors what `vgt_read_chords.lua` does: overwrite only
    `value.segments` and set `human_verified`, leaving `detected` (and every
    other field the detector wrote) untouched -- the core guarantee #19
    adds. A subsequent `analyze()` must not touch `detected` either, since
    the stage is now human-verified."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    first = analyze(project)

    original_detected = first["analysis"]["chords"]["detected"]
    assert original_detected == first["analysis"]["chords"]["value"]

    sidecar = read_sidecar(project)
    corrected_segments = [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C:maj"}]
    sidecar["analysis"]["chords"]["value"]["segments"] = corrected_segments
    sidecar["analysis"]["chords"]["human_verified"] = True
    write_sidecar(project, sidecar)

    result = analyze(project)

    chords = result["analysis"]["chords"]
    assert chords["value"]["segments"] == corrected_segments
    assert chords["human_verified"] is True
    assert chords["detected"] == original_detected
    assert chords["detected"]["segments"] != corrected_segments


def test_section_rename_and_boundary_nudge_survive_rerun(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    first = analyze(project)

    sidecar = read_sidecar(project)
    corrected_sections = [
        {**section, "label": "intro"} if section["index"] == 0 else section
        for section in sidecar["analysis"]["sections"]["value"]
    ]
    corrected_sections[1]["start_seconds"] = corrected_sections[0]["end_seconds"] = round(
        corrected_sections[0]["end_seconds"] + 0.5, 3
    )
    sidecar["analysis"]["sections"] = {
        "value": corrected_sections,
        "human_verified": True,
        "input_hash": sidecar["analysis"]["sections"]["input_hash"],
        "settings_hash": sidecar["analysis"]["sections"]["settings_hash"],
    }
    write_sidecar(project, sidecar)

    result = analyze(project)

    assert result["analysis"]["sections"]["value"] == corrected_sections
    assert result["analysis"]["sections"]["human_verified"] is True
    assert result != first


def test_cli_analyze_invocation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    assert main(["analyze", str(project)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 3
