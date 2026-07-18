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

    v2 = upgrade(v1)

    assert v2["schema_version"] == 2
    assert v2["managed_track_guids"] == ["{AAAA}", "{BBBB}"]
    assert v2["config"] == {"reference_track_guid": REFERENCE_GUID}
    for stage in ANALYSIS_STAGES:
        assert v2["analysis"][stage] == {
            "value": None,
            "human_verified": False,
            "input_hash": None,
            "settings_hash": None,
        }
    assert v2["analysis"]["provenance"]["tool"] == "vgt"


def test_analyze_requires_an_existing_sidecar(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)

    with pytest.raises(AnalysisError, match="No .vgt sidecar"):
        analyze(project)


def test_analyze_writes_v2_sidecar_with_skeleton_and_provenance(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    result = analyze(project)

    assert result["schema_version"] == 2
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
    click_artifact = project.with_name(tempo["click_artifact_path"])
    assert click_artifact.is_file()

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
        def detect(_project_path: Path, _source: Path, _settings: dict[str, object]) -> dict[str, str]:
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


def test_cli_analyze_invocation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    assert main(["analyze", str(project)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 2
