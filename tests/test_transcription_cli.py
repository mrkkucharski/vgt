"""CLI/status coverage for the variant lifecycle surface (issue #150, section
C of docs/transcription-variants-plan.md): profile list/show/validate, and
variant add/rename/select/discard/purge-discarded. Every backend call runs
through `FakeTranscriber` (via a monkeypatched `production_transcriber_router`)
so this suite never invokes real Basic Pitch/DrumScript."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from vgt.cli import main
from vgt.sidecar import read_sidecar, write_sidecar
from vgt.transcribe import FakeTranscriber, TargetTranscriberRouter

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
        lambda: TargetTranscriberRouter(basic_pitch=fake, drumscript=fake, drumscript_targets=("drums",)),
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
    # The first variant added becomes selected automatically; the second does not.
    assert record["selected_variant_id"] == record["variant_order"][0]

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

    # Select by label.
    assert main(["transcription", "variant", "select", "guitar", "clean", str(project)]) == 0
    after_select = json.loads(capsys.readouterr().out)
    assert after_select["selected_variant_id"] == clean_id

    # Discarding the selected variant without --select/--clear-selected is refused.
    assert main(["transcription", "variant", "discard", "guitar", "clean", str(project)]) == 2
    assert "selected variant" in capsys.readouterr().err

    # Discard with an explicit replacement succeeds and archives a compact record.
    assert main([
        "transcription", "variant", "discard", "guitar", "clean", "--select", "detail take 2", str(project),
    ]) == 0
    after_discard = json.loads(capsys.readouterr().out)
    assert clean_id not in after_discard["variants"]
    assert clean_id not in after_discard["variant_order"]
    assert after_discard["selected_variant_id"] == detail_id
    assert [entry["id"] for entry in after_discard["discarded_variants"]] == [clean_id]

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
    # ref resolution on rename/select/discard.
    sidecar = read_sidecar(project)
    target = sidecar["analysis"]["transcription"]["targets"]["guitar"]
    duplicate = dict(target["variants"][first_id])
    target["variants"]["dup00000"] = duplicate
    target["variant_order"].append("dup00000")
    write_sidecar(project, sidecar)

    assert main(["transcription", "variant", "select", "guitar", "take", str(project)]) == 2
    assert "ambiguous" in capsys.readouterr().err

    assert main(["transcription", "variant", "select", "guitar", first_id, str(project)]) == 0


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

    assert main([
        "transcription", "variant", "discard", "guitar", variant_id,
        "--clear-selected", str(project),
    ]) == 0
    assert outside.exists()
    # The normal path was not recorded by this malformed candidate, so it is
    # deliberately left alone instead of being inferred from the immutable id.
    assert generated_midi.exists()


def test_select_clear_and_reselect(tmp_path: Path, capsys) -> None:
    project = _init_project(tmp_path)
    assert main([
        "transcription", "variant", "add", "guitar",
        "--name", "detail", "--profile", "guitar-acoustic-detail", str(project),
    ]) == 0
    capsys.readouterr()

    assert main(["transcription", "variant", "unselect", "guitar", str(project)]) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["selected_variant_id"] is None

    assert main(["transcription", "variant", "select", "guitar", "detail", str(project)]) == 0
    reselected = json.loads(capsys.readouterr().out)
    assert reselected["selected_variant_id"] is not None


def test_status_shows_ordered_variants_and_selected_marker(tmp_path: Path, capsys) -> None:
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
    assert "* detail (" in text
    assert "  clean (" in text

    assert main(["status", "--json", str(project)]) == 0
    status = json.loads(capsys.readouterr().out)
    guitar = status["transcription"]["targets"]["guitar"]
    assert guitar["variant_order"] == list(_variants(project)["variant_order"])
    assert guitar["selected_variant_id"] == _variants(project)["selected_variant_id"]
    ids = {variant["id"] for variant in guitar["variants"]}
    assert ids == set(_variants(project)["variants"])
    selected = [variant for variant in guitar["variants"] if variant["selected"]]
    assert len(selected) == 1
    assert selected[0]["label"] == "detail"


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
