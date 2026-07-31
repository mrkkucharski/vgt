from pathlib import Path
from typing import Any
import hashlib
import json
import shutil
import sys

import pytest

from vgt import analysis as analysis_module
from vgt.analysis import AnalysisError, add_transcription_targets, analyze, chord_sources, forget_transcription_targets, set_transcription_modes
from vgt.cli import main
from vgt.sidecar import (
    ANALYSIS_STAGES,
    artifact_namespace_dir,
    ensure_artifact_namespace,
    read_sidecar,
    upgrade,
    write_sidecar,
)
from vgt.transcription_variants import detection_cache_root
from vgt.transcribe import (
    FakeTranscriber,
    TargetTranscriberRouter,
    TranscriptionError,
    default_spec_for_target,
    events_artifact_name,
    midi_artifact_name,
    notes_artifact_name,
    spec_hash,
)


FIXTURE_DIR = Path(__file__).parents[1] / "test" / "Reaper Project"
FIXTURE = FIXTURE_DIR / "Reaper Project.RPP"
REFERENCE_GUID = "{75418143-1F31-B548-B7D2-96815CB0297D}"  # "The Seven Rivers ..." track


def _project_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "Reaper Project"
    shutil.copytree(FIXTURE_DIR, destination)
    return destination / "Reaper Project.RPP"


def _default_variant(record: dict[str, Any]) -> dict[str, Any]:
    """The automatically managed candidate an analyze run reconciles: a
    target's first retained variant. Since #223 removed the pre-v13 flat
    writer, this is the only per-target record shape `analyze` produces, so
    a target's transcription result is always read through here."""
    return record["variants"][record["variant_order"][0]]


def _add_fake_stem(project: Path, sidecar: dict, target: str, content: bytes) -> None:
    """Fabricate a `stems.artifacts[target]` record pointing at a real file
    holding `content`, without running real separation -- `resolve_target_source`
    only reads `file` (resolved relative to the song folder) and `sha256`."""
    filename = f"{target}.bin"
    (project.parent / filename).write_bytes(content)
    sidecar["analysis"].setdefault("stems", {}).setdefault("artifacts", {})[target] = {
        "file": filename,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


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


@pytest.fixture(autouse=True)
def _offline_analysis_detectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep orchestration tests independent of DSP library/runtime variance.

    Detector algorithms have focused tests of their own.  These tests exercise
    sidecar caching, corrections, and CLI routing, so use small deterministic
    artifacts instead of decoding the full MP3 fixture through platform-native
    numeric backends on every call.
    """
    def tempo(project: Path, _source: Path, _settings: dict[str, object], _analysis: dict[str, object], namespace: str, **_kwargs: object) -> dict[str, object]:
        artifact = artifact_namespace_dir(project, namespace) / "tempo-click.wav"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"RIFFfixture")
        return {"bpm": 120.0, "time_signature": "4/4", "mode": "constant", "backend": "fixture", "beat_times": [0.0, 1.0, 2.0], "click_artifact_path": artifact.name}

    def key(_project: Path, _source: Path, _settings: dict[str, object], _analysis: dict[str, object], _namespace: str, **_kwargs: object) -> dict[str, object]:
        return {"root": "C", "scale": "major", "confidence": 1.0, "backend": "fixture"}

    def sections(project: Path, _source: Path, _settings: dict[str, object], _analysis: dict[str, object], namespace: str, **_kwargs: object) -> list[dict[str, object]]:
        value = [
            {"index": 0, "label": "A", "start_seconds": 0.0, "end_seconds": 1.0},
            {"index": 1, "label": "B", "start_seconds": 1.0, "end_seconds": 2.0},
        ]
        timeline = artifact_namespace_dir(project, namespace) / "sections.txt"
        timeline.parent.mkdir(parents=True, exist_ok=True)
        timeline.write_text("0.000\t1.000\tA\n1.000\t2.000\tB\n", encoding="utf-8")
        return value

    def chords(_source: Path, beat_times: list[float], _settings: dict[str, object], **_kwargs: object) -> dict[str, object]:
        return {
            "segments": [
                {"start_seconds": beat_times[0], "end_seconds": beat_times[1], "chord": "C:maj"},
                {"start_seconds": beat_times[1], "end_seconds": beat_times[2], "chord": "G:maj"},
            ],
            "vocabulary": "maj_min",
            "backend": "fixture",
            "beat_times": beat_times,
        }

    monkeypatch.setitem(analysis_module._DETECTORS, "tempo", tempo)
    monkeypatch.setitem(analysis_module._DETECTORS, "key", key)
    monkeypatch.setitem(analysis_module._DETECTORS, "sections", sections)
    monkeypatch.setattr(analysis_module, "_detect_chords", chords)
    monkeypatch.setattr(analysis_module, "_tempo_beat_times", lambda *_args: [0.0, 1.0, 2.0])


def test_upgrade_keeps_v1_fields_and_adds_v2_analysis_skeleton() -> None:
    v1 = {
        "schema_version": 1,
        "managed_track_guids": ["{AAAA}", "{BBBB}"],
        "config": {"reference_track_guid": REFERENCE_GUID},
    }

    upgraded = upgrade(v1)

    assert upgraded["schema_version"] == 18
    assert upgraded["managed_region_ids"] == []
    assert upgraded["managed_track_guids"] == ["{AAAA}", "{BBBB}"]
    assert upgraded["config"] == {"reference_track_guid": REFERENCE_GUID}
    for stage in ANALYSIS_STAGES:
        if stage == "transcription":
            continue
        expected = {
            "value": None,
            "input_hash": None,
            "settings_hash": None,
            "analyzed_at": None,
        }
        if stage in ("tempo", "sections"):
            expected["human_verified"] = False
            expected["verified_at"] = None
            expected["detected"] = None
            expected["detected_input_hash"] = None
            expected["detected_settings_hash"] = None
        assert upgraded["analysis"][stage] == expected
    assert upgraded["analysis"]["transcription"] == {"requested_targets": ["guitar"], "modes": {}, "targets": {}, "detection_cache": {}}
    assert upgraded["analysis"]["provenance"]["tool"] == "vgt"


def test_upgrade_v16_chords_and_key_corrections_collapse_and_cli_analyzes(tmp_path: Path) -> None:
    """A 7Rivers-shaped v16 sidecar keeps its effective values but sheds the
    redundant chords/key sync metadata before the CLI refreshes both stages."""
    project = _project_copy(tmp_path)
    legacy = {
        "schema_version": 16,
        "config": {"reference_track_guid": REFERENCE_GUID},
        "analysis": {
            "chords": {
                "value": {"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "Dm"}]},
                "detected": {"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "C"}]},
                "input_hash": "chords-input", "settings_hash": "chords-settings",
                "detected_input_hash": "detected-input", "detected_settings_hash": "detected-settings",
                "human_verified": True, "verified_at": "2026-07-01T10:00:00Z",
            },
            "key": {
                "value": {"root": "E", "scale": "minor"},
                "detected": {"root": "D", "scale": "major"},
                "input_hash": "key-input", "settings_hash": "key-settings",
                "detected_input_hash": "detected-input", "detected_settings_hash": "detected-settings",
                "human_verified": False, "verified_at": None,
            },
        },
    }
    upgraded = upgrade(legacy)
    assert upgraded["schema_version"] == 18
    assert upgraded["analysis"]["chords"] == {
        "value": legacy["analysis"]["chords"]["value"], "input_hash": "chords-input",
        "settings_hash": "chords-settings", "analyzed_at": None,
    }
    assert upgraded["analysis"]["key"] == {
        "value": legacy["analysis"]["key"]["value"], "input_hash": "key-input",
        "settings_hash": "key-settings", "analyzed_at": None,
    }

    project.with_suffix(".vgt").write_text(json.dumps(legacy))
    assert main(["analyze", "--no-transcribe", str(project)]) == 0
    persisted = read_sidecar(project)
    assert persisted["schema_version"] == 18
    assert persisted["analysis"]["chords"]["value"]["segments"][0]["chord"] == "C:maj"
    assert persisted["analysis"]["key"]["value"] == {"root": "C", "scale": "major", "confidence": 1.0, "backend": "fixture"}


def test_upgrade_marks_legacy_librosa_tempo_as_unknown_bar_phase() -> None:
    upgraded = upgrade({"schema_version": 10, "analysis": {"tempo": {"value": {
        "backend": "librosa", "bpm": 120.0, "downbeat_offset_seconds": 0.25,
    }}}})

    assert upgraded["schema_version"] == 18
    assert upgraded["analysis"]["tempo"]["value"]["downbeat_detected"] is False


def test_upgrade_attributes_a_legacy_beat_tracker_downbeat_to_the_beat_tracker() -> None:
    """A legacy value with a real `backend` on record did go through
    `detect_beats`, so it is the normal, expected "beat_tracker" case."""
    upgraded = upgrade({"schema_version": 15, "analysis": {"tempo": {"value": {
        "backend": "madmom", "bpm": 120.0, "downbeat_detected": True, "downbeat_offset_seconds": 0.25,
    }}}})

    assert upgraded["analysis"]["tempo"]["value"]["downbeat_source"] == "beat_tracker"


def test_upgrade_does_not_attribute_a_reaper_tempo_map_downbeat_to_the_beat_tracker() -> None:
    """A tempo map adopted from REAPER (issue #276 review) never went through
    `detect_beats` -- schema 18 must not claim otherwise."""
    upgraded = upgrade({"schema_version": 16, "analysis": {"tempo": {
        "human_verified": True,
        "value": {"source": "reaper-tempo-map", "mode": "piecewise", "bpm": 120.0, "spans": []},
    }}})

    tempo_value = upgraded["analysis"]["tempo"]["value"]
    assert tempo_value["downbeat_detected"] is True  # pre-existing schema-11 heuristic, unchanged by this fix
    assert tempo_value["downbeat_source"] is None


def test_upgrade_backfills_downbeat_source_onto_the_detected_baseline_too() -> None:
    """The `detected` baseline is documented to mirror `value`'s tempo-grid
    shape; a legacy sidecar's `detected` must gain `downbeat_source` on
    upgrade the same way `value` does, not be left out."""
    upgraded = upgrade({"schema_version": 16, "analysis": {"tempo": {
        "human_verified": True,
        "value": {"bpm": 118.0, "downbeat_offset_seconds": 0.25, "time_signature": "4/4"},
        "detected": {"backend": "madmom", "bpm": 120.0, "downbeat_detected": True, "downbeat_offset_seconds": 0.1},
    }}})

    assert upgraded["analysis"]["tempo"]["detected"]["downbeat_source"] == "beat_tracker"


def test_upgrade_does_not_attribute_a_backfilled_reaper_tempo_map_detected_to_the_beat_tracker() -> None:
    """Reviewer finding on #276: a legacy sidecar with no `detected` at all
    gets one backfilled by copying `value` verbatim (the schema-2/4
    best-effort backfill), so a human/REAPER-authored `value` with no
    `backend` produces a `detected` that is just as un-machine-computed as
    `value` -- it must get the same `null` provenance, not "beat_tracker"."""
    upgraded = upgrade({"schema_version": 10, "analysis": {"tempo": {
        "human_verified": True,
        "value": {"source": "reaper-tempo-map", "mode": "piecewise", "bpm": 120.0, "spans": []},
    }}})

    tempo = upgraded["analysis"]["tempo"]
    assert tempo["value"]["downbeat_source"] is None
    assert tempo["detected"]["downbeat_source"] is None


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


def test_upgrade_adds_v10_transcription_block_to_a_v8_sidecar() -> None:
    v8 = {
        "schema_version": 8,
        "config": {"reference_track_guid": REFERENCE_GUID},
        "analysis": {
            "stems": {"artifact_namespace": "abc12345", "optional_stems": ["piano"]},
        },
    }

    upgraded = upgrade(v8)

    assert upgraded["schema_version"] == 18
    assert upgraded["analysis"]["transcription"] == {"requested_targets": ["guitar"], "modes": {}, "targets": {}, "detection_cache": {}}
    # Unrelated v8 fields survive the upgrade untouched.
    assert upgraded["analysis"]["stems"]["artifact_namespace"] == "abc12345"
    assert upgraded["analysis"]["stems"]["optional_stems"] == ["piano"]


def test_upgrade_preserves_an_existing_transcription_block() -> None:
    v9 = {
        "schema_version": 9,
        "analysis": {
            "transcription": {
                "requested_targets": ["guitar", "bass"],
                "targets": {"guitar": {"status": "transcribed", "note_count": 872}},
            }
        },
    }

    upgraded = upgrade(v9)

    transcription = upgraded["analysis"]["transcription"]
    assert transcription["requested_targets"] == ["guitar", "bass"]
    assert transcription["modes"] == {}
    assert transcription["detection_cache"] == {}
    guitar = transcription["targets"]["guitar"]
    # The pre-v13 flat fields survive verbatim -- this migration is additive.
    assert guitar["status"] == "transcribed"
    assert guitar["note_count"] == 872
    # A schema v13 `variants` view is derived from those flat fields too.
    assert "selected_variant_id" not in guitar
    assert len(guitar["variant_order"]) == 1
    variant_id = guitar["variant_order"][0]
    assert guitar["variants"][variant_id]["label"] == "default"
    assert guitar["variants"][variant_id]["status"] == "transcribed"
    assert guitar["variants"][variant_id]["note_count"] == 872
    assert guitar["discarded_variants"] == []
    # Deterministic and idempotent: re-upgrading an already-migrated record
    # (e.g. a second `read_sidecar` call) derives the same variant id.
    assert upgrade(upgraded)["analysis"]["transcription"]["targets"]["guitar"]["variant_order"][0] == variant_id


def test_upgrade_preserves_an_intentionally_empty_transcription_target_set() -> None:
    """Forgetting the final target must not resurrect the default guitar
    request on every sidecar read."""
    v9 = {"schema_version": 9, "analysis": {"transcription": {"requested_targets": [], "targets": {}}}}

    upgraded = upgrade(v9)

    assert upgraded["analysis"]["transcription"] == {"requested_targets": [], "modes": {}, "targets": {}, "detection_cache": {}}


def test_upgrade_migrates_v9_acoustic_guitar_to_its_equivalent_profile_hash() -> None:
    v9 = {
        "schema_version": 9,
        "analysis": {"stems": {"guitar_type": "acoustic"}, "transcription": {"requested_targets": ["guitar"], "targets": {}}},
    }

    upgraded = upgrade(v9)

    modes = upgraded["analysis"]["transcription"]["modes"]
    assert modes == {"guitar": "guitar-acoustic"}
    # #110 selected this exact acoustic profile from stems.guitar_type. The
    # migration must preserve its settings identity, not invalidate its cache.
    # This hash moved once more in #144 (the spectral ghost-confirmation gate
    # added new `drop_harmonic_ghosts` params), and again in #148 (the
    # spectral STFT size/hop length became hash-visible params instead of
    # silent module constants) -- both expected one-time invalidations of
    # only the guitar-acoustic target's cache.
    assert spec_hash(default_spec_for_target("guitar", modes=modes, midi_tempo=120.0)) == (
        "da13a57eec4940239d2bad7f30b19ff74f326ee666dadc749ca21621ceb2c769"
    )


def test_upgrade_migrates_an_existing_acoustic_guitar_target_to_a_guitar_acoustic_variant() -> None:
    """An existing v9-v12 acoustic-guitar sidecar has both a legacy
    `stems.guitar_type: acoustic` declaration and an already-transcribed
    `targets["guitar"]` record. #148's migration must derive the variant's
    `requested_profile` from that declaration (migration step 6), not from
    the target's own settings, since the flat record predates `modes`."""
    v9 = {
        "schema_version": 9,
        "analysis": {
            "stems": {"guitar_type": "acoustic"},
            "transcription": {
                "requested_targets": ["guitar"],
                "targets": {
                    "guitar": {
                        "status": "transcribed",
                        "settings_hash": "abc123",
                        "midi_file": "transcription/guitar.mid",
                        "notes_file": "transcription/guitar.csv",
                    }
                },
            },
        },
    }

    upgraded = upgrade(v9)

    guitar = upgraded["analysis"]["transcription"]["targets"]["guitar"]
    # Legacy artifact paths are untouched -- no file is moved during migration.
    assert guitar["midi_file"] == "transcription/guitar.mid"
    assert "selected_variant_id" not in guitar
    variant_id = guitar["variant_order"][0]
    variant = guitar["variants"][variant_id]
    assert variant["requested_profile"] == "guitar-acoustic"
    assert variant["effective_profile"] == "guitar-acoustic"
    assert variant["midi_file"] == "transcription/guitar.mid"
    assert variant["resolved_settings"]["detection"]
    assert variant["detection_hash"] is not None
    assert variant["cleanup_hash"] is not None


def test_upgrade_keeps_an_explicit_transcription_mode_over_the_legacy_declaration() -> None:
    upgraded = upgrade(
        {"schema_version": 9, "analysis": {"stems": {"guitar_type": "acoustic"}, "transcription": {"modes": {"guitar": "guitar"}}}}
    )

    assert upgraded["analysis"]["transcription"]["modes"] == {"guitar": "guitar"}


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

    assert result["schema_version"] == 18
    assert result["managed_track_guids"] == ["{AAAA}", "{BBBB}"]  # phase 0 fields intact
    for stage in ANALYSIS_STAGES:
        if stage == "transcription":
            continue
        assert result["analysis"][stage]["input_hash"] is not None
        if stage in ("tempo", "sections"):
            assert result["analysis"][stage]["human_verified"] is False
        assert result["analysis"][stage]["analyzed_at"] is not None
    transcription = result["analysis"]["transcription"]
    assert transcription["requested_targets"] == ["guitar"]
    # No separation ran, so the default `guitar` target has no stem to
    # transcribe yet -- retained as `skipped-missing-source`, never falling
    # back to the mix.
    assert _default_variant(transcription["targets"]["guitar"])["status"] == "skipped-missing-source"
    provenance = result["analysis"]["provenance"]
    assert provenance["tool"] == "vgt"
    assert provenance["reference_source_path"].endswith("The Seven Rivers (Full March - 3_00).mp3")

    tempo = result["analysis"]["tempo"]["value"]
    assert tempo["bpm"] == pytest.approx(120.0, abs=1.0)
    assert tempo["time_signature"] == "4/4"
    assert tempo["mode"] in {"constant", "piecewise"}
    assert tempo["backend"] == "fixture"
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

    assert "detected" not in result["analysis"]["chords"]

    on_disk = read_sidecar(project)
    assert on_disk == result


def test_analyze_is_idempotent(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    first = analyze(project)
    second = analyze(project)

    # `generation` (#138) is the sidecar commit protocol's monotonic conflict
    # counter. Chords and key also deliberately refresh on every analysis
    # run, so their new analysis timestamps are excluded here.
    assert second["generation"] > first["generation"]
    first.pop("generation")
    second.pop("generation")
    for stage in ("key", "chords"):
        first["analysis"][stage].pop("analyzed_at")
        second["analysis"][stage].pop("analyzed_at")
    assert first == second


def test_analyze_infers_downbeat_from_chord_boundaries_when_beat_tracker_finds_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #276: the beat tracker reporting no downbeat must not be the end
    of the road -- chord segment boundaries landing cleanly on 4-beat bar
    lines should let `vgt analyze` recover the bar phase on its own."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    beat_times = [float(i) for i in range(40)]

    def tempo(project: Path, _source: Path, _settings: dict, _analysis: dict, namespace: str, **_kwargs) -> dict:
        artifact = artifact_namespace_dir(project, namespace) / "tempo-click.wav"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"RIFFfixture")
        return {
            "bpm": 120.0,
            "time_signature": "4/4",
            "mode": "constant",
            "backend": "fixture",
            "downbeat_detected": False,
            "downbeat_offset_seconds": None,
            "downbeat_source": None,
            "beat_times": beat_times,
            "click_artifact_path": artifact.name,
        }

    def chords(_source: Path, beat_times: list[float], _settings: dict, **_kwargs) -> dict:
        bar_starts = [0, 8, 16, 24, 32]  # every change lands on a 4-beat bar line
        segments = [
            {"start_seconds": beat_times[start], "end_seconds": beat_times[start + 4], "chord": "C:maj"}
            for start in bar_starts
        ]
        return {"segments": segments, "vocabulary": "maj_min", "backend": "fixture", "beat_times": beat_times}

    monkeypatch.setitem(analysis_module._DETECTORS, "tempo", tempo)
    monkeypatch.setattr(analysis_module, "_detect_chords", chords)
    monkeypatch.setattr(analysis_module, "_tempo_beat_times", lambda *_args: beat_times)

    result = analyze(project)

    tempo_value = result["analysis"]["tempo"]["value"]
    assert tempo_value["downbeat_detected"] is True
    assert tempo_value["downbeat_offset_seconds"] == 0.0
    assert tempo_value["downbeat_source"] == "chords"
    assert result["analysis"]["tempo"]["detected"]["downbeat_source"] == "chords"


def test_chord_inferred_downbeat_does_not_clobber_a_correction_that_lands_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (issue #276 review): if a human tempo correction (e.g. via
    the REAPER tempo-map sync action) lands on disk while the chords stage is
    still computing -- after this run's own tempo stage turn already
    persisted no-downbeat, before the chord-inference patch fires -- that
    correction must survive, not be silently overwritten by a stale
    in-memory chord-inferred downbeat that never saw it."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    beat_times = [float(i) for i in range(40)]

    def tempo(project: Path, _source: Path, _settings: dict, _analysis: dict, namespace: str, **_kwargs) -> dict:
        artifact = artifact_namespace_dir(project, namespace) / "tempo-click.wav"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"RIFFfixture")
        return {
            "bpm": 120.0,
            "time_signature": "4/4",
            "mode": "constant",
            "backend": "fixture",
            "downbeat_detected": False,
            "downbeat_offset_seconds": None,
            "downbeat_source": None,
            "beat_times": beat_times,
            "click_artifact_path": artifact.name,
        }

    def chords(_source: Path, beat_times: list[float], _settings: dict, **_kwargs) -> dict:
        # Simulate a concurrent writer landing a human correction on disk
        # while this stage is "still running" -- before its own result (and
        # the chord-inference patch riding along with it) is persisted.
        sidecar = read_sidecar(project)
        sidecar["analysis"]["tempo"]["human_verified"] = True
        sidecar["analysis"]["tempo"]["value"] = {
            "bpm": 90.0, "downbeat_offset_seconds": 3.0, "time_signature": "3/4",
        }
        write_sidecar(project, sidecar)

        bar_starts = [0, 8, 16, 24, 32]  # would otherwise infer phase 0
        segments = [
            {"start_seconds": beat_times[start], "end_seconds": beat_times[start + 4], "chord": "C:maj"}
            for start in bar_starts
        ]
        return {"segments": segments, "vocabulary": "maj_min", "backend": "fixture", "beat_times": beat_times}

    monkeypatch.setitem(analysis_module._DETECTORS, "tempo", tempo)
    monkeypatch.setattr(analysis_module, "_detect_chords", chords)
    monkeypatch.setattr(analysis_module, "_tempo_beat_times", lambda *_args: beat_times)

    result = analyze(project)

    tempo_value = result["analysis"]["tempo"]["value"]
    assert tempo_value["bpm"] == 90.0
    assert tempo_value["downbeat_offset_seconds"] == 3.0
    assert tempo_value["time_signature"] == "3/4"
    assert result["analysis"]["tempo"]["human_verified"] is True


def test_analyze_does_not_overwrite_a_beat_tracker_detected_downbeat_with_chords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A downbeat the tempo backend already detected must never be
    second-guessed by chord-derived inference, even if chords would suggest a
    different phase."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    beat_times = [float(i) for i in range(40)]

    def tempo(project: Path, _source: Path, _settings: dict, _analysis: dict, namespace: str, **_kwargs) -> dict:
        artifact = artifact_namespace_dir(project, namespace) / "tempo-click.wav"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"RIFFfixture")
        return {
            "bpm": 120.0,
            "time_signature": "4/4",
            "mode": "constant",
            "backend": "madmom",
            "downbeat_detected": True,
            "downbeat_offset_seconds": beat_times[2],
            "downbeat_source": "beat_tracker",
            "beat_times": beat_times,
            "click_artifact_path": artifact.name,
        }

    def chords(_source: Path, beat_times: list[float], _settings: dict, **_kwargs) -> dict:
        bar_starts = [0, 8, 16, 24, 32]  # would suggest phase 0, not phase 2
        segments = [
            {"start_seconds": beat_times[start], "end_seconds": beat_times[start + 4], "chord": "C:maj"}
            for start in bar_starts
        ]
        return {"segments": segments, "vocabulary": "maj_min", "backend": "fixture", "beat_times": beat_times}

    monkeypatch.setitem(analysis_module._DETECTORS, "tempo", tempo)
    monkeypatch.setattr(analysis_module, "_detect_chords", chords)
    monkeypatch.setattr(analysis_module, "_tempo_beat_times", lambda *_args: beat_times)

    result = analyze(project)

    tempo_value = result["analysis"]["tempo"]["value"]
    assert tempo_value["downbeat_offset_seconds"] == beat_times[2]
    assert tempo_value["downbeat_source"] == "beat_tracker"


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

    # "transcription" is deliberately skipped by the generic loop (it owns its
    # own per-target index, see analysis.py); it never calls its "detector".
    assert calls == {"tempo": 2, "key": 2, "sections": 1, "chords": 2, "transcription": 0}


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
        "downbeat_detected": True,
        # Hand-authored correction with no `backend` on record: never went
        # through `detect_beats`, so its provenance isn't "beat_tracker".
        "downbeat_source": None,
    }
    assert result["analysis"]["tempo"]["human_verified"] is True
    # Untouched stages still refresh normally.
    assert result["analysis"]["key"]["value"]["backend"] == "fixture"


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


def test_synchronized_reaper_tempo_map_derives_the_chord_grid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A verified REAPER map, unlike an old hand-written BPM correction,
    remains the source of truth for later chord alignment."""
    source = tmp_path / "reference.wav"
    source.write_bytes(b"placeholder")

    class Info:
        duration = 4.0

    import soundfile
    monkeypatch.setattr(soundfile, "info", lambda _: Info())
    beats = analysis_module._tempo_map_beat_times(
        {"source": "reaper-tempo-map", "mode": "piecewise", "bpm": 120, "spans": [{"start_seconds": 1.5, "bpm": 60}]},
        source,
    )

    assert beats == [0.0, 0.5, 1.0, 1.5, 2.5, 3.5]


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
    assert captured.out == ""
    sidecar = read_sidecar(project)
    assert sidecar["schema_version"] == 18
    assert sidecar["analysis"]["tempo"]["value"] is not None
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


def test_cli_extra_stem_requires_explicit_noninteractive_acknowledgment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("vgt.cli.LalalSeparator", lambda: pytest.fail("must not submit paid work"))

    assert main(["analyze", "--guitar", "electric", "--extra-stem", "strings", str(project)]) == 2

    assert "requires --accept-stem-cost" in capsys.readouterr().err


def test_cli_pending_persisted_extra_stem_still_requires_noninteractive_acknowledgment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    sidecar["analysis"]["stems"]["optional_stems"] = ["strings"]
    write_sidecar(project, sidecar)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("vgt.cli.LalalSeparator", lambda: pytest.fail("must not submit paid work"))

    # A prior opt-in request can survive an interrupted/declined run.  A
    # retry without repeating the flag must not silently charge for it.
    assert main(["analyze", "--guitar", "electric", str(project)]) == 2

    assert "requires --accept-stem-cost" in capsys.readouterr().err


def test_cli_accepts_keys_piano_extra_stem_alias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("vgt.cli.LalalSeparator", lambda: pytest.fail("must not submit paid work"))

    assert main(["analyze", "--guitar", "electric", "--extra-stem", "keys/piano", str(project)]) == 2
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
    assert calls == [("tempo", "key", "sections"), ("chords", "transcription")]


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
    assert calls == [("tempo", "key", "sections"), ("chords", "transcription")]


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


# --- T-C: per-target transcription reconciliation -------------------------


def test_refresh_target_per_target_cache_independence(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio-v1")
    _add_fake_stem(project, sidecar, "bass", b"bass-audio-v1")
    write_sidecar(project, sidecar)

    first = analyze(project, stages=("transcription",), transcription_targets=("guitar", "bass"), transcriber=FakeTranscriber())
    guitar_first = first["analysis"]["transcription"]["targets"]["guitar"]
    bass_first = first["analysis"]["transcription"]["targets"]["bass"]
    assert _default_variant(guitar_first)["status"] == "transcribed"
    assert _default_variant(bass_first)["status"] == "transcribed"

    # Changing only the guitar stem's content must never touch bass's entry.
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio-v2")
    write_sidecar(project, sidecar)

    second = analyze(project, stages=("transcription",), transcription_targets=("guitar", "bass"), transcriber=FakeTranscriber())
    assert (
        _default_variant(second["analysis"]["transcription"]["targets"]["guitar"])["input_hash"]
        != _default_variant(guitar_first)["input_hash"]
    )
    assert second["analysis"]["transcription"]["targets"]["bass"] == bass_first


def test_variant_compatibility_refresh_preserves_other_candidates(tmp_path: Path) -> None:
    """The CLI's historical analyze flags refresh one target's first retained
    (default) variant, never collapse a target back to the old flat
    one-result record, and never designate any candidate as preferred (#176)."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio")
    write_sidecar(project, sidecar)

    first = analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber=FakeTranscriber())
    target = first["analysis"]["transcription"]["targets"]["guitar"]
    assert "selected_variant_id" not in target
    default_id = target["variant_order"][0]
    assert default_id is not None

    sidecar = read_sidecar(project)
    target = sidecar["analysis"]["transcription"]["targets"]["guitar"]
    alternative_id = "alternative"
    target["variants"][alternative_id] = {**target["variants"][default_id], "label": "alternative"}
    target["variant_order"].append(alternative_id)
    write_sidecar(project, sidecar)

    refreshed = analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber=FakeTranscriber())
    target = refreshed["analysis"]["transcription"]["targets"]["guitar"]
    assert target["variant_order"] == [default_id, alternative_id]
    assert set(target["variants"]) == {default_id, alternative_id}
    assert "selected_variant_id" not in target


def test_selecting_one_mode_changes_only_its_target_settings_hash(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio")
    _add_fake_stem(project, sidecar, "bass", b"bass-audio")
    write_sidecar(project, sidecar)

    first = analyze(project, stages=("transcription",), transcription_targets=("guitar", "bass"), transcriber=FakeTranscriber())
    bass_first = first["analysis"]["transcription"]["targets"]["bass"]
    guitar_first = first["analysis"]["transcription"]["targets"]["guitar"]

    set_transcription_modes(project, {"guitar": "guitar-acoustic"})
    second = analyze(project, stages=("transcription",), transcription_targets=("guitar", "bass"), transcriber=FakeTranscriber())

    assert (
        _default_variant(second["analysis"]["transcription"]["targets"]["guitar"])["settings_hash"]
        != _default_variant(guitar_first)["settings_hash"]
    )
    assert second["analysis"]["transcription"]["targets"]["bass"] == bass_first


def test_refresh_target_fills_in_once_a_missing_stem_arrives(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    first = analyze(project, stages=("transcription",), transcription_targets=("vocals",), transcriber=FakeTranscriber())
    assert _default_variant(first["analysis"]["transcription"]["targets"]["vocals"])["status"] == "skipped-missing-source"

    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "vocals", b"vocals-audio")
    write_sidecar(project, sidecar)

    second = analyze(project, stages=("transcription",), transcription_targets=("vocals",), transcriber=FakeTranscriber())
    assert _default_variant(second["analysis"]["transcription"]["targets"]["vocals"])["status"] == "transcribed"


def test_refresh_target_force_recomputes_every_unchanged_target(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio")
    _add_fake_stem(project, sidecar, "bass", b"bass-audio")
    write_sidecar(project, sidecar)

    class _CountingTranscriber(FakeTranscriber):
        """Counts backend invocations. Basic Pitch targets reach the backend
        through `detect_raw` (one call per uncached detection group), drums
        through `transcribe`; counting both keeps this a count of real backend
        work regardless of which half a target uses."""

        def __init__(self) -> None:
            self.calls = 0

        def transcribe(self, source, destination_dir, spec, progress=None):
            self.calls += 1
            return super().transcribe(source, destination_dir, spec, progress)

        def detect_raw(self, source, destination_dir, spec, progress=None):
            self.calls += 1
            return super().detect_raw(source, destination_dir, spec, progress)

    transcriber = _CountingTranscriber()
    targets = ("guitar", "bass")
    analyze(project, stages=("transcription",), transcription_targets=targets, transcriber=transcriber)
    assert transcriber.calls == 2

    # Unchanged inputs/settings: cached, no recompute.
    analyze(project, stages=("transcription",), transcription_targets=targets, transcriber=transcriber)
    assert transcriber.calls == 2

    # --force recomputes every resolvable target, even unchanged ones.
    analyze(project, stages=("transcription",), transcription_targets=targets, transcriber=transcriber, force=True)
    assert transcriber.calls == 4


def test_refresh_target_isolates_one_targets_failure_from_the_others(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio")
    _add_fake_stem(project, sidecar, "bass", b"bass-audio")
    write_sidecar(project, sidecar)

    class _PartiallyFailingTranscriber(FakeTranscriber):
        def transcribe(self, source, destination_dir, spec, progress=None):
            if source.name == "guitar.bin":
                raise TranscriptionError("boom: guitar backend exploded")
            return super().transcribe(source, destination_dir, spec, progress)

        def detect_raw(self, source, destination_dir, spec, progress=None):
            if source.name == "guitar.bin":
                raise TranscriptionError("boom: guitar backend exploded")
            return super().detect_raw(source, destination_dir, spec, progress)

    result = analyze(
        project,
        stages=("transcription",),
        transcription_targets=("guitar", "bass"),
        transcriber=_PartiallyFailingTranscriber(),
    )

    guitar = _default_variant(result["analysis"]["transcription"]["targets"]["guitar"])
    bass = _default_variant(result["analysis"]["transcription"]["targets"]["bass"])
    assert guitar["status"] == "error"
    assert "boom: guitar backend exploded" in guitar["error"]
    assert bass["status"] == "transcribed"


def test_refresh_target_uses_the_injected_router_and_keeps_drum_cache_independent(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    for target in ("guitar", "bass", "drums"):
        _add_fake_stem(project, sidecar, target, f"{target}-audio".encode())
    write_sidecar(project, sidecar)

    class FakeBasicPitch(FakeTranscriber):
        name = "basic-pitch"

    class FakeDrumScript(FakeTranscriber):
        name = "drumscript"

    basic_pitch = FakeBasicPitch()
    drumscript = FakeDrumScript()
    router = TargetTranscriberRouter(basic_pitch, drumscript, drumscript_targets=("drums",))
    targets = ("guitar", "bass", "drums")
    first = analyze(project, stages=("transcription",), transcription_targets=targets, transcriber_router=router)
    first_targets = first["analysis"]["transcription"]["targets"]
    assert _default_variant(first_targets["drums"])["backend"] == "drumscript"
    assert _default_variant(first_targets["guitar"])["backend"] == "basic-pitch"
    # Bass resolves to the monophonic tracker. A router built without an
    # explicit `pyin` transcriber still routes it to the one note backend it was
    # given, so injecting a single fake keeps covering every note target.
    assert _default_variant(first_targets["bass"])["backend"] == "pyin"

    # Changing a DrumScript-only option changes only the drums settings hash;
    # the normal cache path therefore leaves every Basic Pitch target intact.
    changed_router = TargetTranscriberRouter(
        basic_pitch,
        drumscript,
        drumscript_targets=("drums",),
        drumscript_classifier_mode="rudiment",
    )
    second = analyze(project, stages=("transcription",), transcription_targets=targets, transcriber_router=changed_router)
    second_targets = second["analysis"]["transcription"]["targets"]
    assert (
        _default_variant(second_targets["drums"])["settings_hash"]
        != _default_variant(first_targets["drums"])["settings_hash"]
    )
    assert second_targets["guitar"] == first_targets["guitar"]
    assert second_targets["bass"] == first_targets["bass"]
    drum = _default_variant(second_targets["drums"])
    # Every artifact lives under its target's own directory (#223).
    assert drum["events_file"] == f"transcription/drums/{second_targets['drums']['variant_order'][0]}.json"
    assert drum["event_count"] == 4
    assert drum["pitch_range_midi"] is None
    # A variant record carries no `confidence` at all -- it was a pre-v13 flat
    # field, and DrumScript never reported one either way.
    assert drum.get("confidence") is None


def test_drum_backend_error_is_isolated_from_other_transcription_targets(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    for target in ("guitar", "drums"):
        _add_fake_stem(project, sidecar, target, target.encode())
    write_sidecar(project, sidecar)

    class FailingDrumBackend(FakeTranscriber):
        name = "drumscript"

        def transcribe(self, *_args, **_kwargs):
            raise TranscriptionError("drum backend failed")

    router = TargetTranscriberRouter(FakeTranscriber(), FailingDrumBackend(), drumscript_targets=("drums",))
    result = analyze(project, stages=("transcription",), transcription_targets=("guitar", "drums"), transcriber_router=router)

    assert _default_variant(result["analysis"]["transcription"]["targets"]["guitar"])["status"] == "transcribed"
    drums = _default_variant(result["analysis"]["transcription"]["targets"]["drums"])
    assert drums["status"] == "error"
    assert drums["backend"] == "drumscript"
    assert "drum backend failed" in drums["error"]


def test_analyze_relocates_a_legacy_flat_layout_and_sweeps_its_leftovers(tmp_path: Path) -> None:
    """The state a real pre-v13 project is in (#223): one target's artifacts
    still at their flat paths with the record pointing there, another target's
    flat files stranded by an earlier re-reconcile, and an empty detection
    work directory. One analyze must leave every artifact under
    `transcription/<target>/` -- without re-running the backend, since
    relocating a file changes no identity."""
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio")
    write_sidecar(project, sidecar)
    transcriber = FakeTranscriber()
    analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber=transcriber)

    # Rewind that result into the pre-v13 shape: files at the flat paths, the
    # record naming them, plus leftovers from a target already re-reconciled.
    sidecar = read_sidecar(project)
    namespace_dir = artifact_namespace_dir(project, sidecar["analysis"]["stems"]["artifact_namespace"])
    record = sidecar["analysis"]["transcription"]["targets"]["guitar"]
    variant_id = record["variant_order"][0]
    variant = record["variants"][variant_id]
    for key, flat_name in (("midi_file", midi_artifact_name("guitar")), ("notes_file", notes_artifact_name("guitar"))):
        (namespace_dir / variant[key]).replace(namespace_dir / flat_name)
        variant[key] = flat_name
        record[key] = flat_name
    record["status"] = "transcribed"
    (namespace_dir / midi_artifact_name("drums")).write_bytes(b"orphaned drum midi")
    (namespace_dir / events_artifact_name("drums")).write_bytes(b"orphaned drum events")
    (namespace_dir / "transcription" / "_work-detection").mkdir(exist_ok=True)
    write_sidecar(project, sidecar)
    # A genuine flat record re-derives its own deterministic variant id at read
    # time (`sidecar._legacy_variant_id`); that migrated id is the one the
    # relocated artifacts must be named for.
    variant_id = read_sidecar(project)["analysis"]["transcription"]["targets"]["guitar"]["variant_order"][0]

    result = analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber=transcriber)

    transcription_dir = namespace_dir / "transcription"
    assert sorted(child.name for child in transcription_dir.iterdir()) == ["cache", "guitar"]
    assert sorted(child.name for child in (transcription_dir / "guitar").iterdir()) == [
        f"{variant_id}.csv", f"{variant_id}.mid",
    ]
    moved = _default_variant(result["analysis"]["transcription"]["targets"]["guitar"])
    assert moved["midi_file"] == f"transcription/guitar/{variant_id}.mid"
    assert moved["notes_file"] == f"transcription/guitar/{variant_id}.csv"
    assert moved["status"] == "transcribed"
    # Persisted, not just returned.
    persisted = read_sidecar(project)["analysis"]["transcription"]["targets"]["guitar"]
    assert persisted["variants"][variant_id]["midi_file"] == f"transcription/guitar/{variant_id}.mid"


def test_add_transcription_targets_dedupes_and_preserves_order(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    add_transcription_targets(project, ("bass",))
    result = add_transcription_targets(project, ("guitar", "bass", "vocals"))

    assert result["analysis"]["transcription"]["requested_targets"] == ["guitar", "bass", "vocals"]


def test_cli_transcribe_persists_a_target_across_later_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    calls: list[tuple[Any, Any]] = []

    def fake_analyze(*_args: object, stages: tuple[str, ...] | None = None, transcription_targets=None, **_kwargs: object) -> dict[str, object]:
        calls.append((stages, transcription_targets))
        return read_sidecar(project)

    monkeypatch.setattr("vgt.cli.analyze", fake_analyze)

    assert main(["analyze", "--transcribe", "bass", str(project)]) == 0
    assert read_sidecar(project)["analysis"]["transcription"]["requested_targets"] == ["guitar", "bass"]
    assert calls[-1] == (("chords", "transcription"), None)

    # A later run needs no flag: the persisted set already includes it.
    assert main(["analyze", str(project)]) == 0
    assert read_sidecar(project)["analysis"]["transcription"]["requested_targets"] == ["guitar", "bass"]


def test_cli_mode_persists_a_valid_target_profile_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    monkeypatch.setattr("vgt.cli.analyze", lambda *_args, **_kwargs: read_sidecar(project))

    assert main(["analyze", "--mode", "guitar=guitar-acoustic", str(project)]) == 0
    assert read_sidecar(project)["analysis"]["transcription"]["modes"] == {"guitar": "guitar-acoustic"}


def test_cli_mode_accepts_the_opt_in_bass_monophonic_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    monkeypatch.setattr("vgt.cli.analyze", lambda *_args, **_kwargs: read_sidecar(project))

    assert main(["analyze", "--mode", "bass=bass-monophonic", str(project)]) == 0
    assert read_sidecar(project)["analysis"]["transcription"]["modes"] == {"bass": "bass-monophonic"}


def test_cli_mode_rejects_an_invalid_profile_with_the_valid_choices(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    assert main(["analyze", "--mode", "guitar=banjo", str(project)]) == 2

    error = capsys.readouterr().err
    assert "profile for 'guitar' must be one of" in error
    assert "guitar-acoustic" in error


def test_cli_mode_rejects_a_profile_registered_for_another_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    assert main(["analyze", "--mode", "bass=guitar-acoustic", str(project)]) == 2

    error = capsys.readouterr().err
    assert "profile for 'bass' must be one of" in error
    assert "('default', 'bass', 'bass-pyin', 'bass-basic-pitch', 'bass-monophonic')" in error


def test_forget_transcription_targets_before_any_analysis_is_a_harmless_no_op(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    # No `vgt analyze` has ever run: there is no artifact namespace yet, and
    # thus nothing on disk to delete -- forgetting must still succeed.
    result = forget_transcription_targets(project, ("guitar",))

    assert result["analysis"]["transcription"]["requested_targets"] == []
    assert "guitar" not in result["analysis"]["transcription"]["targets"]


def test_cli_transcribe_only_does_not_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    calls: list[tuple[Any, Any]] = []

    def fake_analyze(*_args: object, stages: tuple[str, ...] | None = None, transcription_targets=None, **_kwargs: object) -> dict[str, object]:
        calls.append((stages, transcription_targets))
        return read_sidecar(project)

    monkeypatch.setattr("vgt.cli.analyze", fake_analyze)

    assert main(["analyze", "--transcribe-only", "bass", str(project)]) == 0
    assert calls[-1] == (("chords", "transcription"), ("bass",))
    assert read_sidecar(project)["analysis"]["transcription"]["requested_targets"] == ["guitar"]


def test_cli_no_transcribe_skips_the_stage_and_keeps_the_requested_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    calls: list[tuple[Any, Any]] = []

    def fake_analyze(*_args: object, stages: tuple[str, ...] | None = None, transcription_targets=None, **_kwargs: object) -> dict[str, object]:
        calls.append((stages, transcription_targets))
        return read_sidecar(project)

    monkeypatch.setattr("vgt.cli.analyze", fake_analyze)

    assert main(["analyze", "--no-transcribe", str(project)]) == 0
    assert calls[-1] == (("chords",), None)
    assert read_sidecar(project)["analysis"]["transcription"]["requested_targets"] == ["guitar"]


def test_cli_transcribe_only_rejects_no_transcribe_and_transcribe(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    assert main(["analyze", "--transcribe-only", "bass", "--no-transcribe", str(project)]) == 2
    assert main(["analyze", "--transcribe-only", "bass", "--transcribe", "vocals", str(project)]) == 2


def test_cli_forget_transcription_removes_entry_and_deletes_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    namespace = ensure_artifact_namespace(sidecar, project)
    sidecar["analysis"]["transcription"]["requested_targets"] = ["guitar"]
    sidecar["analysis"]["transcription"]["targets"]["guitar"] = {
        "status": "transcribed",
        "midi_file": midi_artifact_name("guitar"),
        "notes_file": notes_artifact_name("guitar"),
    }
    write_sidecar(project, sidecar)

    namespace_dir = artifact_namespace_dir(project, namespace)
    midi_path = namespace_dir / midi_artifact_name("guitar")
    notes_path = namespace_dir / notes_artifact_name("guitar")
    midi_path.parent.mkdir(parents=True, exist_ok=True)
    midi_path.write_bytes(b"fake-midi")
    notes_path.write_text("start_time_s,end_time_s,pitch_midi,velocity,pitch_bend\n")

    def fake_analyze(*_args: object, stages: tuple[str, ...] | None = None, **_kwargs: object) -> dict[str, object]:
        return read_sidecar(project)

    monkeypatch.setattr("vgt.cli.analyze", fake_analyze)

    assert main(["analyze", "--forget-transcription", "guitar", str(project)]) == 0

    result = read_sidecar(project)
    assert "guitar" not in result["analysis"]["transcription"]["requested_targets"]
    assert "guitar" not in result["analysis"]["transcription"]["targets"]
    assert not midi_path.is_file()
    assert not notes_path.is_file()


def test_forget_drums_removes_only_its_vgt_midi_and_event_artifacts(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    namespace = ensure_artifact_namespace(sidecar, project)
    sidecar["analysis"]["transcription"]["requested_targets"] = ["guitar", "drums"]
    sidecar["analysis"]["transcription"]["targets"] = {
        "guitar": {"status": "transcribed", "midi_file": midi_artifact_name("guitar"), "notes_file": notes_artifact_name("guitar")},
        "drums": {"status": "transcribed", "midi_file": midi_artifact_name("drums"), "events_file": events_artifact_name("drums")},
    }
    write_sidecar(project, sidecar)
    namespace_dir = artifact_namespace_dir(project, namespace)
    paths = [namespace_dir / name for name in (midi_artifact_name("guitar"), notes_artifact_name("guitar"), midi_artifact_name("drums"), events_artifact_name("drums"))]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"vgt")

    result = forget_transcription_targets(project, ("drums",))

    assert result["analysis"]["transcription"]["requested_targets"] == ["guitar"]
    assert "drums" not in result["analysis"]["transcription"]["targets"]
    assert paths[0].is_file() and paths[1].is_file()
    assert not paths[2].exists() and not paths[3].exists()


def _detection_groups(project: Path, namespace: str) -> set[str]:
    """Raw detection group directories present on disk."""
    root = detection_cache_root(artifact_namespace_dir(project, namespace))
    return {entry.name for entry in root.iterdir() if entry.is_dir()} if root.is_dir() else set()


def test_analyze_releases_a_raw_detection_group_a_retune_stranded(tmp_path: Path) -> None:
    """A routine re-analysis must collect the cache group it just orphaned.

    Reconciling a target at a new identity repoints its variant at a new
    detection hash, leaving the previous group unreferenced but still on disk
    (~700 KB of raw MIDI/CSV per group on a real stem). Before this, only
    `--forget-transcription` and `variant discard` ever collected it, so
    retuning a profile -- or switching a target's backend, as bass did -- leaked
    a group every time.
    """
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio")
    write_sidecar(project, sidecar)
    router = TargetTranscriberRouter(FakeTranscriber(), FakeTranscriber())

    first = analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber_router=router)
    namespace = first["analysis"]["stems"]["artifact_namespace"]
    original_hash = _default_variant(first["analysis"]["transcription"]["targets"]["guitar"])["detection_hash"]
    assert _detection_groups(project, namespace) == {original_hash}

    # Change guitar's detection identity the same way a retune would.
    set_transcription_modes(project, {"guitar": "guitar-acoustic-strict-chords"})
    second = analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber_router=router)
    retuned_hash = _default_variant(second["analysis"]["transcription"]["targets"]["guitar"])["detection_hash"]

    assert retuned_hash != original_hash
    assert set(second["analysis"]["transcription"]["detection_cache"]) == {retuned_hash}
    assert _detection_groups(project, namespace) == {retuned_hash}
    # Durable, not just in the returned dict.
    assert set(read_sidecar(project)["analysis"]["transcription"]["detection_cache"]) == {retuned_hash}


def test_analyze_keeps_a_group_belonging_to_a_target_that_did_not_run(tmp_path: Path) -> None:
    """`--transcribe-only bass` must not collect guitar's detection group.

    The sweep reference-counts against the complete targets index, not just the
    targets reconciled in this run -- otherwise running one target would throw
    away every other target's cached detection and force a re-run next time.
    """
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    for target in ("guitar", "bass"):
        _add_fake_stem(project, sidecar, target, f"{target}-audio".encode())
    write_sidecar(project, sidecar)
    router = TargetTranscriberRouter(FakeTranscriber(), FakeTranscriber())

    both = analyze(
        project, stages=("transcription",), transcription_targets=("guitar", "bass"), transcriber_router=router
    )
    namespace = both["analysis"]["stems"]["artifact_namespace"]
    targets = both["analysis"]["transcription"]["targets"]
    guitar_hash = _default_variant(targets["guitar"])["detection_hash"]
    bass_hash = _default_variant(targets["bass"])["detection_hash"]
    assert _detection_groups(project, namespace) == {guitar_hash, bass_hash}

    only_bass = analyze(
        project, stages=("transcription",), transcription_targets=("bass",), transcriber_router=router, force=True
    )

    assert set(only_bass["analysis"]["transcription"]["detection_cache"]) == {guitar_hash, bass_hash}
    assert _detection_groups(project, namespace) == {guitar_hash, bass_hash}


def test_analyze_keeps_every_group_a_retained_sibling_variant_still_references(tmp_path: Path) -> None:
    """Reconciling the default variant must not collect a group that another
    retained variant of the same target still points at."""
    from vgt.transcription_lifecycle import add_variant

    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    _add_fake_stem(project, sidecar, "guitar", b"guitar-audio")
    write_sidecar(project, sidecar)
    router = TargetTranscriberRouter(FakeTranscriber(), FakeTranscriber())

    analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber_router=router)
    add_variant(project, "guitar", label="strict", profile="guitar-acoustic-strict-chords", router=router)
    before = read_sidecar(project)
    namespace = before["analysis"]["stems"]["artifact_namespace"]
    hashes = {
        variant["detection_hash"]
        for variant in before["analysis"]["transcription"]["targets"]["guitar"]["variants"].values()
    }
    assert len(hashes) == 2

    after = analyze(
        project, stages=("transcription",), transcription_targets=("guitar",), transcriber_router=router, force=True
    )

    assert set(after["analysis"]["transcription"]["detection_cache"]) == hashes
    assert _detection_groups(project, namespace) == hashes
