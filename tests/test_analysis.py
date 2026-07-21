from pathlib import Path
import json
import shutil
import sys

import pytest

from vgt import analysis as analysis_module
from vgt.analysis import AnalysisError, analyze, chord_sources
from vgt.cli import main
from vgt.sidecar import (
    ANALYSIS_STAGES,
    artifact_namespace_dir,
    ensure_artifact_namespace,
    read_sidecar,
    upgrade,
    write_sidecar,
)


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

    assert upgraded["schema_version"] == 7
    assert upgraded["managed_region_ids"] == []
    assert upgraded["managed_track_guids"] == ["{AAAA}", "{BBBB}"]
    assert upgraded["config"] == {"reference_track_guid": REFERENCE_GUID}
    for stage in ANALYSIS_STAGES:
        expected = {
            "value": None,
            "human_verified": False,
            "input_hash": None,
            "settings_hash": None,
            "analyzed_at": None,
            "verified_at": None,
        }
        if stage in ("chords", "sections"):
            expected["detected"] = None
            expected["detected_input_hash"] = None
            expected["detected_settings_hash"] = None
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


def test_upgrade_backfills_detected_from_value_for_v4_sections() -> None:
    """Same v2 -> v3 chords backfill, applied to sections for the v4 -> v5
    migration (#33): a v4 sidecar has no `detected` field on the sections
    stage, so it is seeded from `value` (best effort)."""
    v4 = {
        "schema_version": 4,
        "config": {"reference_track_guid": REFERENCE_GUID},
        "analysis": {
            "sections": {
                "value": [{"label": "Verse", "start_seconds": 0.0, "end_seconds": 10.0}],
                "human_verified": True,
                "input_hash": "abc",
                "settings_hash": "def",
            }
        },
    }

    upgraded = upgrade(v4)

    sections = upgraded["analysis"]["sections"]
    assert sections["detected"] == sections["value"]
    assert sections["detected"] is not sections["value"]  # backfill copies, doesn't alias


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


def test_first_namespace_migrates_only_exact_known_legacy_analysis_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "song.RPP"
    legacy_click = tmp_path / "song.vgt-tempo-click.wav"
    legacy_chords = tmp_path / "song.vgt-chords.txt"
    legacy_sections = tmp_path / "song.vgt-sections.txt"
    legacy_click.write_bytes(b"click")
    legacy_chords.write_text("chords")
    legacy_sections.write_text("sections")
    unrelated = tmp_path / "song.vgt-not-ours.txt"
    unrelated.write_text("leave me alone")
    sidecar = upgrade(
        {
            "schema_version": 5,
            "analysis": {
                "tempo": {"value": {"click_artifact_path": legacy_click.name}},
                "chords": {"value": {"chord_sheet_path": legacy_chords.name}},
                "sections": {"value": [{"label": "A"}]},
            },
        }
    )

    namespace = ensure_artifact_namespace(sidecar, project)
    destination = artifact_namespace_dir(project, namespace)

    assert (destination / "tempo-click.wav").read_bytes() == b"click"
    assert (destination / "chords.txt").read_text() == "chords"
    assert (destination / "sections.txt").read_text() == "sections"
    assert sidecar["analysis"]["tempo"]["value"]["click_artifact_path"] == "tempo-click.wav"
    assert sidecar["analysis"]["chords"]["value"]["chord_sheet_path"] == "chords.txt"
    assert unrelated.read_text() == "leave me alone"
    # Copying is deliberately non-destructive: legacy files are harmless
    # orphans until an explicit cleanup command owns their removal.
    assert legacy_click.is_file()
    assert legacy_chords.is_file()
    assert legacy_sections.is_file()


def test_generated_namespace_is_opaque_and_survives_a_project_rename(tmp_path: Path) -> None:
    """The namespace must not encode the project name: it is never regenerated,
    so any such claim goes stale the first time the RPP is renamed."""
    project = tmp_path / "Reaper Project.RPP"
    sidecar = upgrade({"analysis": {}})

    namespace = ensure_artifact_namespace(sidecar, project)

    assert "Reaper Project" not in namespace
    assert namespace.isalnum()
    renamed = tmp_path / "7Rivers.RPP"
    assert ensure_artifact_namespace(sidecar, renamed) == namespace


def test_first_namespace_leaves_unrecognized_legacy_paths_unmodified(tmp_path: Path) -> None:
    project = tmp_path / "song.RPP"
    unknown = tmp_path / "custom-click.wav"
    unknown.write_bytes(b"custom")
    sidecar = upgrade({"analysis": {"tempo": {"value": {"click_artifact_path": unknown.name}}}})

    namespace = ensure_artifact_namespace(sidecar, project)

    assert sidecar["analysis"]["tempo"]["value"]["click_artifact_path"] == unknown.name
    assert not artifact_namespace_dir(project, namespace).exists()
    assert unknown.read_bytes() == b"custom"


def test_analyze_requires_an_existing_sidecar(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)

    with pytest.raises(AnalysisError, match="No .vgt sidecar"):
        analyze(project)


def test_analyze_writes_v2_sidecar_with_skeleton_and_provenance(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    result = analyze(project)

    assert result["schema_version"] == 7
    assert result["managed_track_guids"] == ["{AAAA}", "{BBBB}"]  # phase 0 fields intact
    for stage in ANALYSIS_STAGES:
        assert result["analysis"][stage]["input_hash"] is not None
        assert result["analysis"][stage]["human_verified"] is False
        assert result["analysis"][stage]["analyzed_at"] is not None
    provenance = result["analysis"]["provenance"]
    assert provenance["tool"] == "vgt"
    assert provenance["reference_source_path"].endswith("The Seven Rivers (Full March - 3_00).mp3")

    tempo = result["analysis"]["tempo"]["value"]
    assert tempo["bpm"] == pytest.approx(120.0, abs=1.0)
    assert tempo["time_signature"] == "4/4"
    assert tempo["mode"] in {"constant", "piecewise"}
    assert tempo["backend"] in {"madmom", "librosa"}
    assert len(tempo["beat_times"]) > 1
    namespace = result["analysis"]["stems"]["artifact_namespace"]
    assert namespace
    namespace_dir = artifact_namespace_dir(project, namespace)
    click_artifact = namespace_dir / tempo["click_artifact_path"]
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
    timeline = namespace_dir / "sections.txt"
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
    chord_sheet = namespace_dir / chords["chord_sheet_path"]
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
            _project_path: Path,
            _source: Path,
            _settings: dict[str, object],
            _analysis: dict[str, object],
            _namespace: str,
            **_kwargs: object,
        ) -> dict[str, str]:
            calls[stage] += 1
            return {"stage": stage}

        return detect

    for stage in ANALYSIS_STAGES:
        monkeypatch.setitem(analysis_module._DETECTORS, stage, detector(stage))

    analyze(project)
    analyze(project, settings={"tempo": {"sensitivity": "high"}})

    assert calls == {"tempo": 2, "key": 1, "sections": 1, "chords": 1}


def test_chord_source_set_uses_only_the_measured_fusion_artifacts(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    namespace = ensure_artifact_namespace(sidecar, project)
    artifact_dir = artifact_namespace_dir(project, namespace)
    artifact_dir.mkdir(parents=True)
    for name in ("instrumental", "guitar", "backing", "bass"):
        (artifact_dir / f"{name}.wav").write_bytes(b"not decoded here")
        sidecar["analysis"]["stems"]["artifacts"][name] = {
            "file": str((artifact_dir / f"{name}.wav").relative_to(project.parent))
        }
    write_sidecar(project, sidecar)

    sources = chord_sources(project, FIXTURE_DIR / "Media" / "The Seven Rivers (Full March - 3_00).mp3", sidecar["analysis"])

    assert tuple(sources) == ("original", "instrumental", "guitar", "backing")


def test_chord_cache_refreshes_when_a_stem_becomes_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    calls = 0

    def fake_chords(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"segments": [], "beat_times": [0.0, 1.0], "vocabulary": "maj_min", "backend": "test", "sources": ["original"]}

    monkeypatch.setattr(analysis_module, "_detect_chords", fake_chords)
    monkeypatch.setattr(analysis_module, "_tempo_beat_times", lambda *_args: [0.0, 1.0])
    # Seed tempo, avoiding unrelated audio work while keeping the chord stage's
    # normal cache path intact.
    sidecar = read_sidecar(project)
    sidecar["analysis"]["tempo"]["value"] = {"beat_times": [0.0, 1.0]}
    sidecar["analysis"]["tempo"]["input_hash"] = "seed"
    write_sidecar(project, sidecar)
    analyze(project, stages=("chords",))

    sidecar = read_sidecar(project)
    namespace = sidecar["analysis"]["stems"]["artifact_namespace"]
    stem = artifact_namespace_dir(project, namespace) / "instrumental.wav"
    stem.parent.mkdir(parents=True, exist_ok=True)
    stem.write_bytes(b"available")
    sidecar["analysis"]["stems"]["artifacts"]["instrumental"] = {"file": str(stem.relative_to(project.parent))}
    write_sidecar(project, sidecar)
    analyze(project, stages=("chords",))

    assert calls == 2


def test_chords_ignore_a_stem_that_disappears_during_cache_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient/missing optional artifact must not block mix-only chords."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    namespace = ensure_artifact_namespace(sidecar, project)
    stem = artifact_namespace_dir(project, namespace) / "instrumental.wav"
    stem.parent.mkdir(parents=True, exist_ok=True)
    stem.write_bytes(b"available briefly")
    sidecar["analysis"]["stems"]["artifacts"]["instrumental"] = {"file": str(stem.relative_to(project.parent))}
    sidecar["analysis"]["tempo"]["value"] = {"beat_times": [0.0, 1.0]}
    write_sidecar(project, sidecar)

    original_hash = analysis_module.hash_source_file

    def disappear_when_hashed(path: Path) -> str:
        if path == stem:
            stem.unlink()
        return original_hash(path)

    observed_sources: list[tuple[str, ...]] = []

    def fake_chords(*_args: object, sources: dict[str, Path], **_kwargs: object) -> dict[str, object]:
        observed_sources.append(tuple(sources))
        return {"segments": [], "beat_times": [0.0, 1.0], "vocabulary": "maj_min", "backend": "test", "sources": list(sources)}

    monkeypatch.setattr(analysis_module, "hash_source_file", disappear_when_hashed)
    monkeypatch.setattr(analysis_module, "_detect_chords", fake_chords)

    result = analyze(project, stages=("chords",))

    assert observed_sources == [("original",)]
    assert result["analysis"]["chords"]["value"]["sources"] == ["original"]


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


def test_analyze_style_chord_correction_preserves_original_detected(tmp_path: Path) -> None:
    """Mirrors what `vgt_sync.lua` does for chords: overwrite only
    `value.segments` and set `human_verified`, leaving `detected` (and every
    other field the detector wrote) untouched -- the core guarantee #19
    adds. A subsequent `analyze()` with unchanged audio/settings must not
    recompute `detected` either -- it only tracks the current inputs, and
    they haven't moved (see the sibling test below for the case where they
    do)."""
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


def test_detected_keeps_refreshing_against_current_settings_once_value_is_human_verified(tmp_path: Path) -> None:
    """Per #19's design leaning: `detected` is the machine baseline and stays
    live -- even once `value` is human-verified and frozen, a settings change
    still recomputes `detected` (while `value`/`human_verified` don't move).
    The detector is deterministic given the same audio, so the recomputed
    `detected.segments` come out identical -- what's observable is that the
    recompute actually ran, tracked by `detected_settings_hash` moving to the
    new settings hash instead of staying pinned to the stale one."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    analyze(project)

    sidecar = read_sidecar(project)
    stale_detected_settings_hash = sidecar["analysis"]["chords"]["detected_settings_hash"]
    corrected_segments = [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C:maj"}]
    sidecar["analysis"]["chords"]["value"]["segments"] = corrected_segments
    sidecar["analysis"]["chords"]["human_verified"] = True
    write_sidecar(project, sidecar)

    # Same audio, but different chord-detector settings -- `detected` should
    # track this even though `value` is frozen by the human correction.
    result = analyze(project, settings={"chords": {"note": "force-recompute"}})

    chords = result["analysis"]["chords"]
    assert chords["value"]["segments"] == corrected_segments
    assert chords["human_verified"] is True
    assert chords["detected_settings_hash"] != stale_detected_settings_hash


def test_refreshing_detected_after_verification_does_not_overwrite_chord_sheet_artifact(tmp_path: Path) -> None:
    """The `chords.txt` chord-sheet artifact documents the effective,
    human-corrected `value` (see README's Chords section), not the machine
    baseline. Refreshing `detected` after `value` is human-verified must not
    re-render that file with the raw new detection -- otherwise the on-disk
    verification artifact would silently diverge from the corrected `value`
    it's supposed to reflect, even though the JSON `value` itself stays
    correct (#19 fix-cycle-2)."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    first = analyze(project)

    namespace = first["analysis"]["stems"]["artifact_namespace"]
    chord_sheet = artifact_namespace_dir(project, namespace) / first["analysis"]["chords"]["value"]["chord_sheet_path"]
    contents_before_correction = chord_sheet.read_text(encoding="utf-8")

    sidecar = read_sidecar(project)
    corrected_segments = [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C:maj"}]
    sidecar["analysis"]["chords"]["value"]["segments"] = corrected_segments
    sidecar["analysis"]["chords"]["human_verified"] = True
    write_sidecar(project, sidecar)

    # Same audio, but different chord-detector settings -- forces `detected`
    # to recompute even though `value` is frozen by the human correction.
    result = analyze(project, settings={"chords": {"note": "force-recompute"}})

    chords = result["analysis"]["chords"]
    assert chords["detected_input_hash"]  # sanity: the refresh path did run
    assert chord_sheet.read_text(encoding="utf-8") == contents_before_correction


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


def test_sync_style_correction_preserves_original_detected_sections(tmp_path: Path) -> None:
    """Mirrors what `vgt_sync.lua` does for sections: overwrite only `value`
    and set `human_verified`, leaving `detected` untouched -- the same #19
    guarantee `test_analyze_style_chord_correction_preserves_original_detected`
    checks for chords, extended to sections (#33)."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    first = analyze(project)

    original_detected = first["analysis"]["sections"]["detected"]
    assert original_detected == first["analysis"]["sections"]["value"]

    sidecar = read_sidecar(project)
    corrected_sections = [{"label": "Intro", "start_seconds": 0.0, "end_seconds": 5.0}]
    sidecar["analysis"]["sections"]["value"] = corrected_sections
    sidecar["analysis"]["sections"]["human_verified"] = True
    write_sidecar(project, sidecar)

    result = analyze(project)

    sections = result["analysis"]["sections"]
    assert sections["value"] == corrected_sections
    assert sections["human_verified"] is True
    assert sections["detected"] == original_detected
    assert sections["detected"] != corrected_sections


def test_cli_analyze_preserves_local_results_when_lalal_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    monkeypatch.delenv("LALAL_LICENSE_KEY", raising=False)

    assert main(["analyze", "--guitar", "electric", str(project)]) == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["schema_version"] == 7
    assert output["analysis"]["tempo"]["value"] is not None
    assert "stem separation unavailable; continuing with available sources" in captured.err


def test_cli_force_stems_requires_explicit_noninteractive_acknowledgment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    monkeypatch.delenv("LALAL_LICENSE_KEY", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(["analyze", "--guitar", "electric", "--force-stems", str(project)]) == 2

    assert "requires --accept-stem-cost" in capsys.readouterr().err


def test_cli_without_a_guitar_declaration_still_runs_free_chord_analysis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(["analyze", str(project)]) == 0

    captured = capsys.readouterr()
    assert "stem separation unavailable; continuing with available sources" in captured.err
    assert read_sidecar(project)["analysis"]["chords"]["value"] is not None


def test_cli_no_stems_does_not_attempt_separation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    calls: list[tuple[str, ...] | None] = []

    def fake_analyze(*_args: object, stages: tuple[str, ...] | None = None, **_kwargs: object) -> dict[str, object]:
        calls.append(stages)
        return read_sidecar(project)

    monkeypatch.setattr("vgt.cli.analyze", fake_analyze)

    assert main(["analyze", "--no-stems", str(project)]) == 0
    assert calls == [("tempo", "key", "sections"), ("chords",)]


def test_cli_force_does_not_attempt_paid_stems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    calls: list[tuple[str, ...] | None] = []

    def fake_analyze(*_args: object, stages: tuple[str, ...] | None = None, **_kwargs: object) -> dict[str, object]:
        calls.append(stages)
        return read_sidecar(project)

    monkeypatch.setattr("vgt.cli.analyze", fake_analyze)
    monkeypatch.setattr("vgt.cli.separation_preview", lambda *_args, **_kwargs: pytest.fail("must not spend credits"))

    assert main(["analyze", "--force", str(project)]) == 0
    assert calls == [("tempo", "key", "sections"), ("chords",)]


def test_cli_interactive_guitar_declaration_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "acoustic")
    monkeypatch.delenv("LALAL_LICENSE_KEY", raising=False)

    # Separation remains optional even after the interactive declaration:
    # unavailable LALAL credentials must still leave the user with chords.
    assert main(["analyze", str(project)]) == 0

    sidecar = read_sidecar(project)
    assert sidecar["config"]["guitar_type"] == "acoustic"
    assert sidecar["analysis"]["stems"]["guitar_type"] == "acoustic"
    assert sidecar["analysis"]["chords"]["value"] is not None
