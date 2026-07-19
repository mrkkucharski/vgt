from __future__ import annotations

import json
from pathlib import Path
import shutil

from vgt.cli import main


FIXTURE_DIR = Path(__file__).parents[1] / "test" / "Reaper Project"
REFERENCE_GUID = "{75418143-1F31-B548-B7D2-96815CB0297D}"


def _project_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "Reaper Project"
    shutil.copytree(FIXTURE_DIR, destination)
    return destination / "Reaper Project.RPP"


def test_status_reports_analysis_corrections_artifacts_and_json(tmp_path: Path, capsys) -> None:
    project = _project_copy(tmp_path)
    project.with_name(f"{project.stem}.vgt-tempo-click.wav").write_bytes(b"click")
    project.with_name(f"{project.stem}.vgt-chords.txt").write_text("chords")
    project.with_name(f"{project.stem}.vgt-sections.txt").write_text("sections")
    project.with_suffix(".vgt").write_text(json.dumps({
        "schema_version": 3,
        "managed_track_guids": ["{A}", "{B}"],
        "config": {
            "reference_track_name": "The Seven Rivers (Full March - 3_00)",
            "reference_track_guid": REFERENCE_GUID,
            "folder_name": "[vgt] The Seven Rivers (Full March - 3_00)",
            "mirror_name": "[vgt] Mirror",
            "tempo_map_applied": True,
        },
        "analysis": {
            "tempo": {"value": {"bpm": 120, "time_signature": "4/4", "click_artifact_path": f"{project.stem}.vgt-tempo-click.wav"}, "analyzed_at": "2026-07-19T10:00:00Z"},
            "key": {"value": {"root": "A#", "scale": "minor"}, "analyzed_at": "2026-07-19T10:00:00Z"},
            "sections": {"value": [{}, {}], "analyzed_at": "2026-07-19T10:00:00Z"},
            "chords": {"value": {"segments": [{}, {}], "chord_sheet_path": f"{project.stem}.vgt-chords.txt"}, "detected": {"segments": [{}, {}, {}]}, "human_verified": True, "analyzed_at": "2026-07-19T10:00:00Z", "verified_at": "2026-07-19T11:00:00Z"},
        },
    }))

    assert main(["status", str(project)]) == 0
    text = capsys.readouterr().out
    assert "120.0 BPM, 4/4, detected" in text
    assert "A# minor, detected" in text
    assert "2 sections, detected" in text
    assert "2 segments, human-corrected, detected baseline present" in text
    assert "Last human correction: 2026-07-19T11:00:00Z" in text
    assert "click: present" in text

    assert main(["status", "--json", str(project)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["reference_track"]["source_exists"] is True
    assert status["stages"]["chords"]["detected_present"] is True
    assert status["artifacts"]["section_timeline"]["exists"] is True


def test_status_handles_older_sidecars_and_missing_sidecars(tmp_path: Path, capsys) -> None:
    project = _project_copy(tmp_path)
    assert main(["status", str(project)]) == 2
    assert "No .vgt sidecar" in capsys.readouterr().err

    project.with_suffix(".vgt").write_text(json.dumps({"schema_version": 2, "config": {}, "analysis": {}}))
    assert main(["status", str(project)]) == 0
    text = capsys.readouterr().out
    assert "tempo: missing" in text
    assert "Last analysis: unknown" in text
    assert "Last human correction: unknown" in text
