"""Offline coverage for the on-demand single-track MT3 job runner
(docs/on-demand-track-transcription-plan.md). Every scenario fakes
provisioning state and the `mt3-transcribe` subprocess, exactly like
test_mt3_transcriber.py; nothing here clones MT3 or imports TensorFlow/JAX.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import subprocess

import mido
import pytest

from vgt.cli import main
from vgt.mt3_provision import CheckpointManifest, Mt3ProvisionError
from vgt.track_jobs import run_track_job, write_status


def _project_with_tempo(tmp_path: Path, *, bpm: float | None = 120.0) -> Path:
    project = tmp_path / "Song" / "Song.RPP"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text("dummy rpp")
    sidecar = {"schema_version": 1, "analysis": {}}
    if bpm is not None:
        sidecar["analysis"]["tempo"] = {"value": {"bpm": bpm, "mode": "constant"}}
    project.with_suffix(".vgt").write_text(json.dumps(sidecar))
    return project


def _job_dir(tmp_path: Path) -> Path:
    job_dir = tmp_path / "vgt" / "ns" / "track-jobs" / "job-1"
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _source(job_dir: Path) -> Path:
    source = job_dir / "source.wav"
    source.write_bytes(b"fake-audio-bytes")
    return source


def _provisioned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache_dir = tmp_path / "mt3-cache"
    checkpoint_dir = cache_dir / "models" / "checkpoint_0"
    checkpoint_dir.mkdir(parents=True)
    manifest = CheckpointManifest(commit="deadbeef", tag="main", files=(), fingerprint="fp-1", model_id="model-1", hf_revision="rev-1", hf_checkpoint_dir="ckpt-1")
    monkeypatch.setattr("vgt.mt3_provision.default_cache_dir", lambda: cache_dir)
    monkeypatch.setattr("vgt.mt3_provision.require_mt3_provisioned", lambda cache_dir=None: manifest)


def _write_forced_program_midi(path: Path) -> None:
    midi = mido.MidiFile(ticks_per_beat=480)
    midi.tracks.append(mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500_000, time=0)]))
    track = mido.MidiTrack([mido.Message("program_change", program=25, time=0)])
    track.append(mido.Message("note_on", note=60, velocity=90, time=0))
    track.append(mido.Message("note_off", note=60, velocity=0, time=480))
    midi.tracks.append(track)
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(str(path))


def _fake_run_success(*, midi_writer=_write_forced_program_midi, note_count: int = 1, programs=(25,)):
    def fake_run(argv, *, cwd, **kwargs):
        output = Path(argv[argv.index("--output") + 1])
        midi_writer(output)
        payload = {"output": str(output), "note_count": note_count, "programs": list(programs), "drum_note_count": 0}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    return fake_run


def test_run_track_job_success_writes_done_status_and_result_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_with_tempo(tmp_path)
    job_dir = _job_dir(tmp_path)
    source = _source(job_dir)
    write_status(job_dir, job_id="job-1", source_track_name="Guitar (stem)", requested_program=25)
    _provisioned(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run_success())

    status = run_track_job(project, "job-1", source=source, force_program=25, label="Guitar (stem)")

    assert status["status"] == "done"
    assert status["note_count"] == 1
    assert status["error"] is None
    # Selection fields the trigger script wrote earlier must survive the merge.
    assert status["source_track_name"] == "Guitar (stem)"
    assert status["requested_program"] == 25
    assert (job_dir / "result.mid").is_file()
    assert (job_dir / "result.csv").is_file()
    on_disk = json.loads((job_dir / "status.json").read_text())
    assert on_disk == status


def test_run_track_job_missing_tempo_records_error_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_with_tempo(tmp_path, bpm=None)
    job_dir = _job_dir(tmp_path)
    source = _source(job_dir)
    _provisioned(monkeypatch, tmp_path)

    status = run_track_job(project, "job-1", source=source, force_program=25)

    assert status["status"] == "error"
    assert "analyzed tempo" in status["error"]
    assert not (job_dir / "result.mid").exists()


def test_run_track_job_missing_provisioning_records_error_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_with_tempo(tmp_path)
    job_dir = _job_dir(tmp_path)
    source = _source(job_dir)
    monkeypatch.setattr("vgt.mt3_provision.default_cache_dir", lambda: tmp_path / "mt3-cache")
    monkeypatch.setattr(
        "vgt.mt3_provision.require_mt3_provisioned",
        lambda cache_dir=None: (_ for _ in ()).throw(Mt3ProvisionError("the mt3 backend is not provisioned")),
    )

    status = run_track_job(project, "job-1", source=source, force_program=25)

    assert status["status"] == "error"
    assert "not provisioned" in status["error"]


def test_run_track_job_nonzero_subprocess_exit_records_error_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_with_tempo(tmp_path)
    job_dir = _job_dir(tmp_path)
    source = _source(job_dir)
    _provisioned(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"))

    status = run_track_job(project, "job-1", source=source, force_program=25)

    assert status["status"] == "error"
    assert "boom" in status["error"]


def test_run_track_job_drums_only_output_records_error_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`merge_all_musical_tracks` raising `TranscriptionError` must also land
    as a clean `status: "error"`, not an unhandled exception."""
    def drums_only(path: Path) -> None:
        midi = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack([mido.Message("note_on", channel=9, note=36, velocity=100, time=0),
                                 mido.Message("note_off", channel=9, note=36, velocity=0, time=240)])
        midi.tracks.append(track)
        path.parent.mkdir(parents=True, exist_ok=True)
        midi.save(str(path))

    project = _project_with_tempo(tmp_path)
    job_dir = _job_dir(tmp_path)
    source = _source(job_dir)
    _provisioned(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run_success(midi_writer=drums_only))

    status = run_track_job(project, "job-1", source=source, force_program=25)

    assert status["status"] == "error"
    assert "no surviving" in status["error"]


def test_run_track_job_unexpected_exception_is_captured_as_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare, non-`TranscriptionError` exception must still land in
    `status.json` rather than propagating out of a detached process."""
    project = _project_with_tempo(tmp_path)
    job_dir = _job_dir(tmp_path)
    source = _source(job_dir)
    _provisioned(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run_success())
    monkeypatch.setattr(
        "vgt.track_jobs.write_normalized_mt3_artifacts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    status = run_track_job(project, "job-1", source=source, force_program=25)

    assert status["status"] == "error"
    assert "unexpected error" in status["error"]
    assert "disk full" in status["error"]


def test_write_status_merges_onto_existing_fields(tmp_path: Path) -> None:
    job_dir = tmp_path
    write_status(job_dir, job_id="job-1", source_track_name="Bass", requested_program=33)
    merged = write_status(job_dir, status="running", started_at="2026-08-17T00:00:00Z")

    assert merged == {
        "job_id": "job-1", "source_track_name": "Bass", "requested_program": 33,
        "status": "running", "started_at": "2026-08-17T00:00:00Z",
    }
    assert json.loads((job_dir / "status.json").read_text()) == merged


def test_cli_track_run_returns_nonzero_exit_on_job_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    project = _project_with_tempo(tmp_path, bpm=None)
    job_dir = _job_dir(tmp_path)
    source = _source(job_dir)
    _provisioned(monkeypatch, tmp_path)

    exit_code = main([
        "transcription", "track", "run", str(project), "job-1", "--source", str(source), "--force-program", "25",
    ])

    assert exit_code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "error"


def test_cli_track_run_returns_zero_exit_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    project = _project_with_tempo(tmp_path)
    job_dir = _job_dir(tmp_path)
    source = _source(job_dir)
    _provisioned(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run_success())

    exit_code = main([
        "transcription", "track", "run", str(project), "job-1", "--source", str(source), "--force-program", "25",
        "--label", "Guitar (stem)",
    ])

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "done"


def test_python_dash_m_vgt_actually_works() -> None:
    """Regression test for a real bug: `src/vgt/__main__.py` didn't exist,
    so `python -m vgt ...` -- exactly what vgt_transcribe_track.lua spawns
    (see docs/on-demand-track-transcription-plan.md's "Invoking the vgt CLI
    from Lua", which deliberately chose `-m vgt` over the console-script
    shim) -- failed immediately with "No module named vgt.__main__; 'vgt' is
    a package and cannot be directly executed", before ever reaching
    `run_track_job`. A real subprocess, not an import, because the bug is
    specifically about the package having no `__main__` module -- an import
    of `vgt.cli` would never have caught it."""
    result = subprocess.run([sys.executable, "-m", "vgt", "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "usage: vgt" in result.stdout
