"""CLI/status coverage for the variant lifecycle surface (issue #150, section
C of docs/transcription-variants-plan.md; selection removed by #176): profile
list/show/validate, and variant add/rename/discard/purge-discarded. Every
backend call runs through `FakeTranscriber` (via a monkeypatched
`production_transcriber_router`) so this suite never invokes real Basic
Pitch/DrumScript."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from vgt.cli import main
from vgt.sidecar import read_sidecar, write_sidecar
from vgt.transcribe import AdtofSpec, FakeAdtofTranscriber, FakeTranscriber, TargetTranscriberRouter
from vgt.transcription_profiles import profiles_path

FIXTURE_DIR = Path(__file__).parents[1] / "test" / "Reaper Project"
REFERENCE_GUID = "{75418143-1F31-B548-B7D2-96815CB0297D}"


def _project_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "Reaper Project"
    shutil.copytree(FIXTURE_DIR, destination)
    return destination / "Reaper Project.RPP"


def _write_wav(path: Path, content: bytes) -> None:
    """A real, librosa-loadable WAV: `guitar-acoustic-clean`'s
    `drop_harmonic_ghosts` cleanup stage reads the actual waveform for its
    spectral confirmation gate, so a stub byte string isn't decodable enough
    (see tests/test_transcription_variants.py's identical `_write_source`)."""
    import numpy as np
    import soundfile as sf

    seed = int.from_bytes(hashlib.sha256(content).digest()[:4], "big")
    rng = np.random.default_rng(seed)
    samples = (rng.standard_normal(66150) * 0.01).astype(np.float32)  # 3s @ 22050 Hz, quiet noise
    sf.write(str(path), samples, 22050, subtype="FLOAT")


def _init_project(tmp_path: Path) -> Path:
    """A project with a real `guitar` stem artifact and a detected tempo, so
    `variant add` can resolve a source and a sustain-clamp bar length exactly
    like a real `vgt analyze` run would."""
    project = _project_copy(tmp_path)
    content = b"fake-guitar-stem-bytes"
    _write_wav(project.parent / "guitar.wav", content)
    write_sidecar(project, {
        "schema_version": 13,
        "config": {"reference_track_guid": REFERENCE_GUID},
        "analysis": {
            "tempo": {"value": {"bpm": 120.0, "time_signature": "4/4"}},
            "stems": {"artifacts": {"guitar": {"file": "guitar.wav"}}},
            "transcription": {"requested_targets": ["guitar"], "modes": {}, "targets": {}, "detection_cache": {}},
        },
    })
    return project


@pytest.fixture(autouse=True)
def _fake_router(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTranscriber()
    monkeypatch.setattr(
        "vgt.transcription_lifecycle.production_transcriber_router",
        lambda: TargetTranscriberRouter(basic_pitch=fake, drumscript=fake, drumscript_targets=("drums",), adtof=FakeAdtofTranscriber()),
    )


def _variants(project: Path, target: str = "guitar") -> dict:
    return read_sidecar(project)["analysis"]["transcription"]["targets"][target]


def test_profile_list_and_show_and_validate(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)

    assert main(["transcription", "profile", "list", str(project)]) == 0
    listing = capsys.readouterr().out
    assert "guitar-acoustic-clean (builtin)" in listing
    assert "guitar-acoustic-detail (builtin)" in listing

    assert main(["transcription", "profile", "show", "guitar-acoustic-clean", str(project)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["name"] == "guitar-acoustic-clean"
    assert shown["is_builtin"] is True
    assert any(stage["name"] == "drop_harmonic_ghosts" for stage in shown["cleanup"])

    assert main(["transcription", "profile", "validate", str(project)]) == 0
    assert "0 project profile(s) valid." in capsys.readouterr().out


def test_profile_show_unknown_profile_fails_clearly(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)
    assert main(["transcription", "profile", "show", "does-not-exist", str(project)]) == 2
    assert "unknown profile" in capsys.readouterr().err


def _init_project_with_drums(tmp_path: Path) -> Path:
    project = _init_project(tmp_path)
    _write_wav(project.parent / "drums.wav", b"fake-drums-stem-bytes")

    def add_drums_artifact(current: dict) -> None:
        current["stems"]["artifacts"]["drums"] = {"file": "drums.wav"}
        requested = current["transcription"]["requested_targets"]
        if "drums" not in requested:
            requested.append("drums")

    from vgt.sidecar import update_analysis
    update_analysis(project, add_drums_artifact)
    return project


def _init_project_with_bass(tmp_path: Path) -> Path:
    project = _init_project(tmp_path)
    _write_wav(project.parent / "bass.wav", b"fake-bass-stem-bytes")

    def add_bass_artifact(current: dict) -> None:
        current["stems"]["artifacts"]["bass"] = {"file": "bass.wav"}
        requested = current["transcription"]["requested_targets"]
        if "bass" not in requested:
            requested.append("bass")

    from vgt.sidecar import update_analysis
    update_analysis(project, add_bass_artifact)
    return project


def test_drums_clean_profile_is_listed_shown_and_distinct_from_default(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)

    assert main(["transcription", "profile", "list", str(project)]) == 0
    listing = capsys.readouterr().out
    assert "drums-clean (builtin, drums)" in listing

    assert main(["transcription", "profile", "show", "drums-clean", str(project)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["name"] == "drums-clean"
    assert shown["target"] == "drums"
    assert shown["is_builtin"] is True
    assert shown["enabled"] is True


def test_mode_drums_equals_drums_clean_is_accepted_and_persisted(tmp_path: Path, capsys) -> None:
    project = _init_project_with_drums(tmp_path)

    assert main([
        "analyze", "--mode", "drums=drums-clean", str(project),
    ]) == 0
    capsys.readouterr()

    modes = read_sidecar(project)["analysis"]["transcription"]["modes"]
    assert modes["drums"] == "drums-clean"

    assert main(["status", "--json", str(project)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["transcription"]["targets"]["drums"]["effective_profile"] == "drums-clean"


def test_mode_drums_equals_unknown_profile_is_rejected(tmp_path: Path, capsys) -> None:
    project = _init_project_with_drums(tmp_path)

    assert main(["analyze", "--mode", "drums=not-a-real-profile", str(project)]) == 2
    assert "profile for 'drums'" in capsys.readouterr().err


def test_variant_add_target_drums_profile_drums_clean_writes_channel_10_midi(tmp_path: Path, capsys) -> None:
    from vgt.transcribe import _midi_has_non_percussion_notes

    project = _init_project_with_drums(tmp_path)

    assert main([
        "transcription", "variant", "add", "drums",
        "--name", "clean", "--profile", "drums-clean", str(project),
    ]) == 0
    clean = json.loads(capsys.readouterr().out)
    assert clean["status"] == "transcribed"
    assert clean["effective_profile"] == "drums-clean"

    assert main([
        "transcription", "variant", "add", "drums",
        "--name", "raw", "--profile", "default", str(project),
    ]) == 0
    raw = json.loads(capsys.readouterr().out)
    assert raw["settings_hash"] != clean["settings_hash"]

    namespace_dir = project.parent / "vgt" / read_sidecar(project)["analysis"]["stems"]["artifact_namespace"]
    clean_id = next(
        vid for vid, v in _variants(project, "drums")["variants"].items() if v["label"] == "clean"
    )
    midi_path = namespace_dir / "transcription" / "drums" / f"{clean_id}.mid"
    assert not _midi_has_non_percussion_notes(midi_path.read_bytes())


def test_variant_add_drums_adtof_coexists_with_drumscript_and_receives_the_project_grid(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _init_project_with_drums(tmp_path)

    from vgt.sidecar import update_analysis

    update_analysis(project, lambda current: current["tempo"].__setitem__("value", {
        **current["tempo"]["value"],
        "beat_times": [0.125, 0.625, 1.125],
        "downbeat_offset_seconds": 0.125,
    }))

    class CapturingAdtof(FakeAdtofTranscriber):
        spec: AdtofSpec | None = None

        def transcribe(self, source, destination_dir, spec, progress=None):  # type: ignore[no-untyped-def]
            assert isinstance(spec, AdtofSpec)
            self.spec = spec
            return super().transcribe(source, destination_dir, spec, progress)

    fake = FakeTranscriber()
    adtof_transcriber = CapturingAdtof()
    monkeypatch.setattr(
        "vgt.transcription_lifecycle.production_transcriber_router",
        lambda: TargetTranscriberRouter(
            basic_pitch=fake, drumscript=fake, drumscript_targets=("drums",), adtof=adtof_transcriber,
        ),
    )

    assert main(["transcription", "variant", "add", "drums", "--name", "raw", "--profile", "default", str(project)]) == 0
    raw = json.loads(capsys.readouterr().out)
    assert main(["transcription", "variant", "add", "drums", "--name", "adtof", "--profile", "drums-adtof", str(project)]) == 0
    adtof = json.loads(capsys.readouterr().out)
    assert adtof["backend"] == "adtof"
    assert adtof["settings_hash"] != raw["settings_hash"]
    assert adtof_transcriber.spec is not None
    assert adtof_transcriber.spec.beat_grid is not None
    assert adtof_transcriber.spec.beat_grid.beat_times == pytest.approx((0.125, 0.625, 1.125))
    assert adtof_transcriber.spec.beat_grid.downbeat_offset_s == pytest.approx(0.125)
    assert {variant["label"] for variant in _variants(project, "drums")["variants"].values()} == {"raw", "adtof"}


def test_variant_add_target_drums_rejects_an_unknown_profile(tmp_path: Path, capsys) -> None:
    project = _init_project_with_drums(tmp_path)

    assert main([
        "transcription", "variant", "add", "drums",
        "--name", "bogus", "--profile", "not-a-real-profile", str(project),
    ]) == 2
    assert "drums" in capsys.readouterr().err


def test_bass_project_pyin_profiles_validate_show_and_reuse_detection_cache(tmp_path: Path, capsys) -> None:
    project = _init_project_with_bass(tmp_path)
    profiles_path(project).write_text("""
schema_version = 1

[profiles.low-bass]
target = "bass"
extends = "bass-pyin"
[profiles.low-bass.detection]
minimum_frequency_hz = 25
frame_length = 4096
hop_length = 512
median_filter_frames = 7

[profiles.low-bass-short]
target = "bass"
extends = "low-bass"
[profiles.low-bass-short.cleanup.clamp_sustain]
max_bars = 1
""", encoding="utf-8")

    assert main(["transcription", "profile", "validate", str(project)]) == 0
    assert "2 project profile(s) valid." in capsys.readouterr().out
    assert main(["transcription", "profile", "show", "low-bass", str(project)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["backend"] == "pyin"
    assert shown["detection"]["frame_length"] == 4096
    assert "onset_threshold" not in shown["detection"]

    assert main([
        "transcription", "variant", "add", "bass",
        "--name", "low", "--profile", "low-bass", str(project),
    ]) == 0
    low = json.loads(capsys.readouterr().out)
    assert low["backend"] == "pyin"

    assert main([
        "transcription", "variant", "add", "bass",
        "--name", "short", "--profile", "low-bass-short", str(project),
    ]) == 0
    short = json.loads(capsys.readouterr().out)
    assert low["detection_hash"] == short["detection_hash"]
    assert len(read_sidecar(project)["analysis"]["transcription"]["detection_cache"]) == 1


def test_add_two_variants_share_detection_and_full_lifecycle(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)

    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "detail", "--profile", "guitar-acoustic-detail", str(project),
    ]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["status"] == "transcribed"
    assert detail["label"] == "detail"

    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "clean", "--profile", "guitar-acoustic-clean", str(project),
    ]) == 0
    clean = json.loads(capsys.readouterr().out)
    assert clean["status"] == "transcribed"

    # Detail and clean share one Basic Pitch detection group (see
    # docs/transcription-variants-plan.md's "why detail and clean share
    # detection"), so exactly one raw detection cache entry exists.
    record = _variants(project)
    assert detail["detection_hash"] == clean["detection_hash"]
    assert len(read_sidecar(project)["analysis"]["transcription"]["detection_cache"]) == 1
    assert set(record["variant_order"]) == {
        vid for vid, v in record["variants"].items() if v["label"] in ("detail", "clean")
    }
    assert "selected_variant_id" not in record

    detail_id = next(vid for vid, v in record["variants"].items() if v["label"] == "detail")
    clean_id = next(vid for vid, v in record["variants"].items() if v["label"] == "clean")

    # Rejects a duplicate label outright.
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "clean", "--profile", "guitar-acoustic-strict-chords", str(project),
    ]) == 2
    assert "already used" in capsys.readouterr().err

    # Rename never reruns transcription: same artifact files, new label.
    assert main([
        "transcription", "variant", "rename", "guitar", "detail", "--name", "detail take 2", str(project),
    ]) == 0
    renamed = json.loads(capsys.readouterr().out)
    assert renamed["variants"][detail_id]["label"] == "detail take 2"
    assert renamed["variants"][detail_id]["midi_file"] == detail["midi_file"]

    # Any retained variant may be discarded directly -- no replacement/clear
    # requirement, since retained variants are peers (#176).
    assert main(["transcription", "variant", "discard", "guitar", "clean", str(project)]) == 0
    after_discard = json.loads(capsys.readouterr().out)
    assert clean_id not in after_discard["variants"]
    assert clean_id not in after_discard["variant_order"]
    assert "selected_variant_id" not in after_discard
    assert [entry["id"] for entry in after_discard["discarded_variants"]] == [clean_id]
    archived = after_discard["discarded_variants"][0]
    assert archived["input_hash"] == clean["input_hash"]
    assert archived["detection_hash"] == clean["detection_hash"]
    assert archived["raw_notes_hash"] == clean["raw_notes_hash"]
    assert archived["cleanup_hash"] == clean["cleanup_hash"]
    assert archived["resolved_settings"] == clean["resolved_settings"]
    assert not {"midi_file", "notes_file", "events_file"}.intersection(archived)

    namespace_dir = project.parent / "vgt" / read_sidecar(project)["analysis"]["stems"]["artifact_namespace"]
    assert not (namespace_dir / "transcription" / "guitar" / f"{clean_id}.mid").exists()
    assert (namespace_dir / "transcription" / "guitar" / f"{detail_id}.mid").exists()

    # Purge clears the archived recipe list as a separate operation.
    assert main(["transcription", "variant", "purge-discarded", "guitar", str(project)]) == 0
    purged = json.loads(capsys.readouterr().out)
    assert purged["discarded_variants"] == []


def test_add_persists_a_new_target_in_requested_status_set(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)

    assert main([
        "transcription", "variant", "add", "original",
        "--name", "mix", "--profile", "default", str(project),
    ]) == 0
    capsys.readouterr()

    transcription = read_sidecar(project)["analysis"]["transcription"]
    assert transcription["requested_targets"] == ["guitar", "original"]
    assert "original" in transcription["targets"]

    assert main(["status", "--json", str(project)]) == 0
    assert "original" in json.loads(capsys.readouterr().out)["transcription"]["targets"]


def test_ambiguous_label_requires_immutable_id(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "take", "--profile", "guitar-acoustic-detail", str(project),
    ]) == 0
    capsys.readouterr()
    record = _variants(project)
    first_id = next(iter(record["variants"]))

    # Force a second variant with a colliding label directly in the sidecar
    # (bypassing add's own duplicate-label rejection) to exercise ambiguous
    # ref resolution on rename/discard.
    sidecar = read_sidecar(project)
    target = sidecar["analysis"]["transcription"]["targets"]["guitar"]
    duplicate = dict(target["variants"][first_id])
    target["variants"]["dup00000"] = duplicate
    target["variant_order"].append("dup00000")
    write_sidecar(project, sidecar)

    assert main(["transcription", "variant", "rename", "guitar", "take", "--name", "renamed", str(project)]) == 2
    assert "ambiguous" in capsys.readouterr().err

    assert main(["transcription", "variant", "rename", "guitar", first_id, "--name", "renamed", str(project)]) == 0


def test_discard_uses_recorded_artifact_paths_and_never_escapes_namespace(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "detail", "--profile", "guitar-acoustic-detail", str(project),
    ]) == 0
    capsys.readouterr()

    sidecar = read_sidecar(project)
    target = sidecar["analysis"]["transcription"]["targets"]["guitar"]
    variant_id = target["variant_order"][0]
    namespace_dir = project.parent / "vgt" / sidecar["analysis"]["stems"]["artifact_namespace"]
    generated_midi = namespace_dir / target["variants"][variant_id]["midi_file"]
    outside = project.parent / "must-not-delete.mid"
    outside.write_bytes(b"keep")
    target["variants"][variant_id]["midi_file"] = "../../must-not-delete.mid"
    write_sidecar(project, sidecar)

    assert main(["transcription", "variant", "discard", "guitar", variant_id, str(project)]) == 0
    assert outside.exists()
    # The normal path was not recorded by this malformed candidate, so it is
    # deliberately left alone instead of being inferred from the immutable id.
    assert generated_midi.exists()


def test_select_and_unselect_are_removed_and_cannot_mutate_state(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "detail", "--profile", "guitar-acoustic-detail", str(project),
    ]) == 0
    capsys.readouterr()
    before = read_sidecar(project)

    # `select`/`unselect` are ordinary unsupported CLI syntax now (#176):
    # argparse rejects the unknown subcommand before any sidecar mutation.
    with pytest.raises(SystemExit) as select_exit:
        main(["transcription", "variant", "select", "guitar", "detail", str(project)])
    assert select_exit.value.code == 2
    with pytest.raises(SystemExit) as unselect_exit:
        main(["transcription", "variant", "unselect", "guitar", str(project)])
    assert unselect_exit.value.code == 2
    capsys.readouterr()

    assert read_sidecar(project) == before


def test_discard_no_longer_accepts_selection_flags(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "detail", "--profile", "guitar-acoustic-detail", str(project),
    ]) == 0
    capsys.readouterr()
    before = read_sidecar(project)

    with pytest.raises(SystemExit) as select_flag_exit:
        main(["transcription", "variant", "discard", "guitar", "detail", "--select", "nope", str(project)])
    assert select_flag_exit.value.code == 2
    with pytest.raises(SystemExit) as clear_flag_exit:
        main(["transcription", "variant", "discard", "guitar", "detail", "--clear-selected", str(project)])
    assert clear_flag_exit.value.code == 2
    capsys.readouterr()

    assert read_sidecar(project) == before


def test_status_shows_ordered_variants_without_selection(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "detail", "--profile", "guitar-acoustic-detail", str(project),
    ]) == 0
    capsys.readouterr()
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "clean", "--profile", "guitar-acoustic-clean", str(project),
    ]) == 0
    capsys.readouterr()

    assert main(["status", str(project)]) == 0
    text = capsys.readouterr().out
    assert "2 retained variant(s)" in text
    assert "detail (" in text
    assert "clean (" in text
    assert "*" not in text

    assert main(["status", "--json", str(project)]) == 0
    status = json.loads(capsys.readouterr().out)
    guitar = status["transcription"]["targets"]["guitar"]
    assert guitar["variant_order"] == list(_variants(project)["variant_order"])
    assert "selected_variant_id" not in guitar
    ids = {variant["id"] for variant in guitar["variants"]}
    assert ids == set(_variants(project)["variants"])
    for variant in guitar["variants"]:
        assert "selected" not in variant
    first = next(variant for variant in guitar["variants"] if variant["label"] == "detail")
    # JSON status is the read-only comparison/debugging view, so it must
    # retain the source/detection/cleanup cache identity as well as the
    # displayed metrics.
    assert first["source_role"] == "guitar"
    assert first["input_hash"]
    assert first["detection_hash"]
    assert first["raw_notes_hash"]
    assert first["cleanup_hash"]
    assert first["resolved_settings"]
    assert first["max_note_duration_s"] is not None
    assert first["max_simultaneous_voices"] is not None


def test_forget_transcription_removes_variant_artifacts_and_gcs_cache(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "detail", "--profile", "guitar-acoustic-detail", str(project),
    ]) == 0
    capsys.readouterr()

    sidecar = read_sidecar(project)
    namespace = sidecar["analysis"]["stems"]["artifact_namespace"]
    variant_id = next(iter(sidecar["analysis"]["transcription"]["targets"]["guitar"]["variants"]))
    midi_path = project.parent / "vgt" / namespace / "transcription" / "guitar" / f"{variant_id}.mid"
    assert midi_path.exists()

    assert main(["analyze", "--forget-transcription", "guitar", "--no-transcribe", "--no-stems", str(project)]) == 0

    assert not midi_path.exists()
    after = read_sidecar(project)
    assert "guitar" not in after["analysis"]["transcription"]["targets"]
    assert after["analysis"]["transcription"]["detection_cache"] == {}
