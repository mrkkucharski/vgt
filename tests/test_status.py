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
    namespace = f"{project.stem}-abc123"
    namespace_dir = project.parent / "vgt" / namespace
    namespace_dir.mkdir(parents=True)
    (namespace_dir / "tempo-click.wav").write_bytes(b"click")
    (namespace_dir / "chords.txt").write_text("chords")
    (namespace_dir / "sections.txt").write_text("sections")
    project.with_suffix(".vgt").write_text(json.dumps({
        "schema_version": 6,
        "managed_track_guids": ["{A}", "{B}"],
        "config": {
            "reference_track_name": "The Seven Rivers (Full March - 3_00)",
            "reference_track_guid": REFERENCE_GUID,
            "folder_name": "[vgt] The Seven Rivers (Full March - 3_00)",
            "tempo_map_applied": True,
        },
        "analysis": {
            "tempo": {"value": {"bpm": 120, "time_signature": "4/4", "downbeat_detected": False, "click_artifact_path": "tempo-click.wav"}, "analyzed_at": "2026-07-19T10:00:00Z"},
            "key": {"value": {"root": "A#", "scale": "minor"}, "analyzed_at": "2026-07-19T10:00:00Z"},
            "sections": {"value": [{}, {}], "analyzed_at": "2026-07-19T10:00:00Z"},
            "chords": {"value": {"segments": [{}, {}], "chord_sheet_path": "chords.txt"}, "detected": {"segments": [{}, {}, {}]}, "human_verified": True, "analyzed_at": "2026-07-19T10:00:00Z", "verified_at": "2026-07-19T11:00:00Z"},
            "stems": {"artifact_namespace": namespace},
            "transcription": {
                "requested_targets": ["guitar", "bass", "vocals"],
                "targets": {
                    "guitar": {
                        "backend": "basic-pitch", "package_pin": "basic-pitch[onnx]==0.4.0",
                        "status": "transcribed", "note_count": 872, "pitch_range_midi": [40, 76],
                        "transcribed_at": "2026-07-19T10:05:00Z",
                        "midi_file": "transcription/guitar.mid", "notes_file": "transcription/guitar.csv",
                    },
                    "bass": {
                        "backend": "basic-pitch", "package_pin": "basic-pitch[onnx]==0.4.0",
                        "status": "error", "error": "basic-pitch exited with status 1",
                    },
                    "vocals": {"status": "skipped-missing-source"},
                },
            },
        },
    }))
    (namespace_dir / "transcription").mkdir(parents=True)
    (namespace_dir / "transcription" / "guitar.mid").write_bytes(b"midi")

    assert main(["status", str(project)]) == 0
    text = capsys.readouterr().out
    assert "120.0 BPM, 4/4, bar phase unknown, detected" in text
    assert "A# minor" in text
    assert "2 sections, detected" in text
    assert "2 segments" in text
    assert "human-corrected" not in text
    assert "Last human correction: unknown" in text
    assert "click: present" in text
    assert "transcription (basic-pitch[onnx]==0.4.0): 3 requested" in text
    assert "guitar   872 notes, MIDI 40-76, profile guitar-harmonic, transcribed 2026-07-19T10:05:00Z" in text
    assert "bass     error - basic-pitch exited with status 1, profile bass" in text
    assert "vocals   skipped - no vocals stem available, profile vocals" in text

    assert main(["status", "--json", str(project)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["reference_track"]["source_exists"] is True
    assert "human_verified" not in status["stages"]["chords"]
    assert "detected_present" not in status["stages"]["chords"]
    assert "state" not in status["stages"]["chords"]
    assert "verified_at" not in status["stages"]["chords"]
    assert status["stages"]["tempo"]["downbeat_detected"] is False
    assert status["artifacts"]["section_timeline"]["exists"] is True
    assert set(status["stems"]["operations"]) == {
        "vocals-original", "bass-original", "drums-original", "guitar-original", "guitar-instrumental"
    }
    assert set(status["stems"]["artifacts"]) == {"vocals", "instrumental", "bass", "drums", "guitar", "backing"}
    assert status["stems"]["artifacts"]["vocals"]["state"] == "missing"

    transcription = status["transcription"]
    assert transcription["requested_targets"] == ["guitar", "bass", "vocals"]
    assert transcription["backend"] == "basic-pitch"
    assert transcription["targets"]["guitar"]["status"] == "transcribed"
    assert transcription["targets"]["guitar"]["requested_mode"] is None
    assert transcription["targets"]["guitar"]["effective_profile"] == "guitar-harmonic"
    assert transcription["targets"]["guitar"]["note_count"] == 872
    assert transcription["targets"]["bass"]["status"] == "error"
    assert transcription["targets"]["vocals"]["status"] == "skipped-missing-source"
    assert status["artifacts"]["transcription_guitar_midi"]["exists"] is True
    assert status["artifacts"]["transcription_guitar_midi"]["path"].endswith("transcription/guitar.mid")


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
    # `build_status` now runs the sidecar through `sidecar.upgrade` (see
    # status.py), so a transcription-block-less legacy sidecar reports the
    # same schema-v9 default request `analyze()` itself would actually use
    # (`DEFAULT_TRANSCRIPTION_TARGETS`), rather than status's previous,
    # inconsistent "0 requested" read of the untouched raw block.
    assert "transcription (not yet run): 1 requested, 0 retained variant(s)" in text


def test_status_json_loads_legacy_transcription_record_without_events_file(tmp_path: Path, capsys) -> None:
    project = _project_copy(tmp_path)
    project.with_suffix(".vgt").write_text(json.dumps({
        "schema_version": 9, "config": {}, "analysis": {"transcription": {
            "requested_targets": ["guitar"],
            "targets": {"guitar": {"status": "transcribed", "note_count": 2, "midi_file": "transcription/guitar.mid"}},
        }},
    }))
    assert main(["status", "--json", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["transcription"]["targets"]["guitar"]["events_file"] is None


def test_status_resolves_profiles_for_legacy_and_mixed_targets(tmp_path: Path, capsys) -> None:
    project = _project_copy(tmp_path)
    project.with_suffix(".vgt").write_text(json.dumps({
        "schema_version": 9,
        "config": {},
        "analysis": {
            "stems": {"guitar_type": "acoustic"},
            "transcription": {
                "requested_targets": ["guitar", "bass", "drums", "vocals"],
                "modes": {"bass": "bass-monophonic", "vocals": "retired-profile"},
                "targets": {
                    "guitar": {"status": "transcribed", "note_count": 1, "pitch_range_midi": [40, 40]},
                    "bass": {"status": "skipped-missing-source"},
                    "drums": {"status": "error", "error": "backend failed"},
                },
            },
        },
    }))

    assert main(["status", str(project)]) == 0
    text = capsys.readouterr().out
    assert "guitar   1 notes, MIDI 40-40, profile guitar-acoustic" in text
    assert "bass     skipped - no bass stem available, profile bass-monophonic" in text
    assert "drums    error - backend failed, profile raw" in text
    assert "vocals   not yet run, profile vocals" in text

    assert main(["status", "--json", str(project)]) == 0
    targets = json.loads(capsys.readouterr().out)["transcription"]["targets"]
    assert targets["guitar"]["requested_mode"] == "guitar-acoustic"
    assert targets["guitar"]["effective_profile"] == "guitar-acoustic"
    assert targets["bass"]["requested_mode"] == "bass-monophonic"
    assert targets["bass"]["effective_profile"] == "bass-monophonic"
    assert targets["drums"]["requested_mode"] is None
    assert targets["drums"]["effective_profile"] == "raw"
    assert targets["vocals"]["requested_mode"] == "retired-profile"
    assert targets["vocals"]["effective_profile"] == "vocals"
