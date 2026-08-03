"""Offline coverage for the real MT3 backend and its router/spec wiring
(issue #287). Every scenario fakes provisioning state and the
`mt3-transcribe` subprocess; nothing here clones MT3, downloads its model, or
imports TensorFlow/JAX. See test_mt3_provision.py (#285) and
test_mt3_normalize.py (#286) for those layers, and test_transcription_variants.py
for the cache/variant-integration surface.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import mido
import pytest

from vgt.mt3_provision import CheckpointManifest, Mt3ProvisionError
from vgt.transcribe import (
    FakeTranscriber,
    Mt3Spec,
    Mt3Transcriber,
    TargetTranscriberRouter,
    TranscriptionError,
    VALID_TARGETS,
    build_mt3_argv,
    default_spec_for_target,
    production_transcriber_router,
)


def _spec(**overrides) -> Mt3Spec:
    base = default_spec_for_target("guitar", backend="mt3", midi_tempo=120.0, mt3_checkpoint_fingerprint="fp-1")
    assert isinstance(base, Mt3Spec)
    return replace(base, **overrides) if overrides else base


def _write_valid_mt3_midi(path: Path, *, track_name: str = "Guitar", pitch: int = 60) -> None:
    midi = mido.MidiFile(ticks_per_beat=480)
    midi.tracks.append(mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500_000, time=0)]))
    track = mido.MidiTrack([mido.MetaMessage("track_name", name=track_name, time=0)])
    track.append(mido.Message("note_on", note=pitch, velocity=90, time=0))
    track.append(mido.Message("note_off", note=pitch, velocity=0, time=480))
    midi.tracks.append(track)
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(str(path))


def _provisioned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    cache_dir = tmp_path / "mt3-cache"
    repo_dir = cache_dir / "repo"
    checkpoint_dir = cache_dir / "models" / "checkpoint_0"
    checkpoint_dir.mkdir(parents=True)
    manifest = CheckpointManifest(commit="deadbeef", tag="v0.1.0", files=(), fingerprint="fp-1")
    monkeypatch.setattr("vgt.mt3_provision.default_cache_dir", lambda: cache_dir)
    monkeypatch.setattr("vgt.mt3_provision.require_mt3_provisioned", lambda cache_dir=None: manifest)
    return cache_dir, repo_dir, checkpoint_dir


def _fake_run_success(*, midi_writer=_write_valid_mt3_midi, note_count: int = 1, programs=(24,), drum_note_count: int = 0):
    def fake_run(argv, *, cwd, **kwargs):
        output = Path(argv[argv.index("--output") + 1])
        midi_writer(output)
        payload = {"output": str(output), "note_count": note_count, "programs": list(programs), "drum_note_count": drum_note_count}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    return fake_run


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "guitar.wav"
    source.write_bytes(b"fake-audio-bytes")
    return source


def test_build_mt3_argv_matches_the_forks_documented_invocation(tmp_path: Path) -> None:
    source, output = tmp_path / "in.wav", tmp_path / "out.mid"
    checkpoint_dir, repo_dir = tmp_path / "models" / "checkpoint_0", tmp_path / "repo"

    argv = build_mt3_argv(source, output, checkpoint_dir, repo_dir)

    assert argv == [
        "uv", "run", "--project", str(repo_dir), "mt3-transcribe",
        "--checkpoint", str(checkpoint_dir), "--input", str(source), "--output", str(output), "--json",
    ]


def test_detect_raw_first_install_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run_success())
    source = _source(tmp_path)

    raw = Mt3Transcriber().detect_raw(source, tmp_path / "dest", _spec())

    assert raw.midi_tempo == 120.0
    assert len(raw.notes) == 1
    assert raw.notes[0].pitch_midi == 60
    assert raw.raw_midi_path.is_file()
    assert raw.raw_notes_path.is_file()


def test_transcribe_derives_a_full_transcription_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run_success())
    source = _source(tmp_path)

    result = Mt3Transcriber().transcribe(source, tmp_path / "dest", _spec())

    assert result.note_count == 1
    assert result.pitch_range_midi == (60, 60)
    assert result.midi_path.is_file()
    assert result.notes_path.is_file()


def test_detect_raw_requires_an_mt3_spec(tmp_path: Path) -> None:
    with pytest.raises(TranscriptionError, match="requires an Mt3Spec"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", default_spec_for_target("guitar", backend="fake"))


def test_detect_raw_requires_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vgt.mt3_provision.default_cache_dir", lambda: tmp_path / "mt3-cache")
    monkeypatch.setattr(
        "vgt.mt3_provision.require_mt3_provisioned",
        lambda cache_dir=None: (_ for _ in ()).throw(Mt3ProvisionError("the mt3 backend is not provisioned")),
    )

    with pytest.raises(TranscriptionError, match="not provisioned"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_requires_the_checkpoint_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "mt3-cache"
    manifest = CheckpointManifest(commit="deadbeef", tag="v0.1.0", files=(), fingerprint="fp-1")
    monkeypatch.setattr("vgt.mt3_provision.default_cache_dir", lambda: cache_dir)
    monkeypatch.setattr("vgt.mt3_provision.require_mt3_provisioned", lambda cache_dir=None: manifest)
    # Deliberately do not create cache_dir/models/checkpoint_0.

    with pytest.raises(TranscriptionError, match="checkpoint directory is missing"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_reports_nonzero_exit_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(argv, *, cwd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="checkpoint restore failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError, match="checkpoint restore failed"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_reports_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired("uv", 12)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError, match="timed out after 12s"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_reports_malformed_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(argv, *, cwd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError, match="unparsable JSON"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_reports_missing_required_json_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(argv, *, cwd, **kwargs):
        output = Path(argv[argv.index("--output") + 1])
        _write_valid_mt3_midi(output)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"output": str(output)}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError, match="missing required fields"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_reports_a_declared_output_path_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(argv, *, cwd, **kwargs):
        output = Path(argv[argv.index("--output") + 1])
        _write_valid_mt3_midi(output)
        payload = {"output": "/somewhere/else.mid", "note_count": 1, "programs": [24], "drum_note_count": 0}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError, match="unexpected output path"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_reports_a_missing_output_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(argv, *, cwd, **kwargs):
        output = Path(argv[argv.index("--output") + 1])
        # Deliberately never write `output`.
        payload = {"output": str(output), "note_count": 0, "programs": [], "drum_note_count": 0}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError, match="wrote no output file"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_reports_malformed_midi_structure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(argv, *, cwd, **kwargs):
        output = Path(argv[argv.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"not a midi file")
        payload = {"output": str(output), "note_count": 1, "programs": [24], "drum_note_count": 0}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError, match="not a valid MIDI file"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_reports_no_note_bearing_track(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(argv, *, cwd, **kwargs):
        output = Path(argv[argv.index("--output") + 1])
        midi = mido.MidiFile(ticks_per_beat=480)
        midi.tracks.append(mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500_000, time=0)]))
        output.parent.mkdir(parents=True, exist_ok=True)
        midi.save(str(output))
        payload = {"output": str(output), "note_count": 0, "programs": [], "drum_note_count": 0}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError, match="no MIDI track contains note events"):
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())


def test_detect_raw_scrubs_the_temporary_work_directory_from_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provisioned(monkeypatch, tmp_path)

    def fake_run(argv, *, cwd, **kwargs):
        return SimpleNamespace(returncode=1, stdout=f"at {cwd}", stderr=f"failed at {cwd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TranscriptionError) as error:
        Mt3Transcriber().detect_raw(_source(tmp_path), tmp_path / "dest", _spec())
    assert "vgt-mt3-" not in str(error.value)


def test_router_routes_to_mt3_when_the_resolved_backend_is_mt3(monkeypatch: pytest.MonkeyPatch) -> None:
    mt3 = FakeTranscriber()
    router = TargetTranscriberRouter(FakeTranscriber(), FakeTranscriber(), mt3=mt3)
    monkeypatch.setattr("vgt.transcribe.backend_for_target_profile", lambda target, modes: "mt3")

    assert router.for_target("guitar") is mt3
    spec = router.spec_for_target("guitar", midi_tempo=120.0)
    assert isinstance(spec, Mt3Spec)


def test_router_raises_when_mt3_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    router = TargetTranscriberRouter(FakeTranscriber(), FakeTranscriber())
    monkeypatch.setattr("vgt.transcribe.backend_for_target_profile", lambda target, modes: "mt3")

    with pytest.raises(TranscriptionError, match="MT3 is not available"):
        router.for_target("guitar")


def test_production_router_wires_a_real_mt3_transcriber() -> None:
    router = production_transcriber_router()

    assert isinstance(router, TargetTranscriberRouter)
    assert isinstance(router.mt3, Mt3Transcriber)
    # No profile selects mt3 yet (issue #288), so every documented target
    # still resolves to its current backend.
    for target in VALID_TARGETS:
        assert router.for_target(target).name in {"drumscript", "pyin", "basic-pitch"}


def test_mt3_spec_serializes_its_full_pinned_identity() -> None:
    spec = _spec(tempo_map=None)

    data = spec.to_dict()

    assert data["backend"] == "mt3"
    assert data["repository"] == spec.repository
    assert data["tag"] == "v0.1.0"
    assert data["commit"] == spec.commit
    assert data["runtime_version"] == "python==3.11"
    assert data["model_id"] == "official-multitrack-v1"
    assert data["checkpoint_fingerprint"] == "fp-1"
    assert data["track_selection_version"] == spec.track_selection_version
    assert data["note_normalization_version"] == spec.note_normalization_version
    assert data["midi_tempo"] == 120.0
    assert "tempo_map" not in data
    assert data["cleanup"] == []


def test_mt3_spec_has_no_pip_package_pin_since_it_is_git_cloned() -> None:
    from vgt.transcribe import _spec_package_pin

    assert _spec_package_pin(_spec()) is None


def test_mt3_spec_checkpoint_fingerprint_is_none_until_a_caller_checks_provisioning() -> None:
    spec = default_spec_for_target("guitar", backend="mt3", midi_tempo=120.0)
    assert isinstance(spec, Mt3Spec)
    assert spec.checkpoint_fingerprint is None
