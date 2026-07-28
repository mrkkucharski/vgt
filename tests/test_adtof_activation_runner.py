"""Offline coverage for Phase 2's Torch-isolated ADTOF activation runner."""

from __future__ import annotations

import json
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
import subprocess

import numpy as np
import pytest

from vgt.transcribe import (
    ADTOF_CLASS_NAMES,
    ADTOF_PACKAGE_PIN,
    ADTOF_RUNTIME_VERSION,
    ADTOF_LOCK_SHA256,
    ADTOF_LOCK_FILENAME,
    ADTOF_TORCH_VERSION,
    AdtofActivationRunner,
    TranscriptionError,
    adtof_activation_cache_key,
    build_adtof_argv,
    default_spec_for_target,
)


def _spec():
    return default_spec_for_target("drums", backend="adtof")


def _metadata(spec) -> dict[str, object]:
    return {
        "package_version": spec.package_version, "model_version": spec.model_version,
        "weights_sha256": spec.weights_sha256, "runtime_version": "3.11",
        "torch_version": spec.torch_version.removeprefix("torch=="), "lock_sha256": spec.lock_sha256,
        "device": "cpu", "sample_rate": 44100,
        "n_fft": 2048, "hop_samples": 441, "fps": 100,
        "class_names": list(ADTOF_CLASS_NAMES), "gm_labels": [35, 38, 47, 42, 49],
    }


def _write_dump(path: Path, spec, activations: np.ndarray | None = None, metadata: dict[str, object] | None = None) -> None:
    np.savez_compressed(
        path, activations=activations if activations is not None else np.ones((3, 5), dtype=np.float32),
        metadata=json.dumps(metadata or _metadata(spec)),
    )


def _successful_run(spec):
    def fake_run(argv, *, cwd, **kwargs):
        output = Path(argv[-1])
        _write_dump(output, spec)
        return SimpleNamespace(returncode=0, stdout="done", stderr="")
    return fake_run


def test_argv_is_pinned_offline_and_runs_only_the_temp_helper(tmp_path: Path) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"audio")
    helper, output, lock = tmp_path / "helper.py", tmp_path / "output.npz", tmp_path / "requirements.lock"

    argv = build_adtof_argv(source, output, _spec(), helper, lock)

    assert argv == [
        "uv", "run", "--offline", "--isolated", "--no-project", "--python", "3.11",
        "--with-requirements", str(lock), "python", str(helper), str(source.resolve()), str(output), ADTOF_LOCK_SHA256,
    ]
    assert ADTOF_RUNTIME_VERSION == "python==3.11"
    assert ADTOF_TORCH_VERSION == "torch==2.13.0"


def test_committed_lock_is_complete_and_matches_the_runtime_identity() -> None:
    lock = Path(__file__).parents[1] / "src" / "vgt" / ADTOF_LOCK_FILENAME

    assert hashlib.sha256(lock.read_bytes()).hexdigest() == ADTOF_LOCK_SHA256
    contents = lock.read_text(encoding="utf-8")
    assert ADTOF_PACKAGE_PIN in contents
    assert "torch==2.13.0" in contents
    for dependency in ("librosa==", "pretty-midi==", "numpy==", "scipy=="):
        assert dependency in contents


def test_runner_validates_child_dump_and_cache_reuses_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"audio")
    calls = 0
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/uv")

    def fake_run(argv, *, cwd, **kwargs):
        nonlocal calls
        calls += 1
        _write_dump(Path(argv[-2]), _spec())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = AdtofActivationRunner(tmp_path / "cache")
    first = runner.run(source, _spec())
    second = runner.run(source, _spec())

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls == 1
    assert first.activations.shape == (3, 5)
    assert first.metadata["fps"] == 100
    assert (tmp_path / "cache" / f"{first.cache_key}.npz").is_file()
    assert len(first.cache_key) == 64
    assert first.cache_key != adtof_activation_cache_key(_spec(), "different-stem-hash")


def test_cache_key_includes_the_locked_runtime_identity() -> None:
    spec = _spec()

    assert adtof_activation_cache_key(spec, "stem") != adtof_activation_cache_key(
        replace(spec, lock_sha256="different-lock"), "stem"
    )
    assert adtof_activation_cache_key(spec, "stem") != adtof_activation_cache_key(
        replace(spec, torch_version="torch==2.13.1"), "stem"
    )


@pytest.mark.parametrize(
    ("activations", "metadata", "message"),
    [
        (np.ones((3, 4), dtype=np.float32), None, "4 classes"),
        (np.array([[float("nan")] * 5], dtype=np.float32), None, "non-finite"),
        (np.ones((3, 5), dtype=np.float64), None, "float32"),
        (None, {"package_version": "wrong"}, "unexpected package_version"),
    ],
)
def test_runner_rejects_invalid_raw_activations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, activations, metadata, message: str) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/uv")

    def fake_run(argv, *, cwd, **kwargs):
        _write_dump(Path(argv[-2]), _spec(), activations, metadata)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TranscriptionError, match=message) as error:
        AdtofActivationRunner().run(source, _spec())
    assert "vgt-adtof-" not in str(error.value)


def test_runner_scrubs_temporary_path_from_child_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/uv")

    def fake_run(argv, *, cwd, **kwargs):
        return SimpleNamespace(returncode=7, stdout=f"at {cwd}", stderr=f"bad at {cwd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TranscriptionError, match="pinned package, model, or bundled weights") as error:
        AdtofActivationRunner().run(source, _spec())
    assert "vgt-adtof-" not in str(error.value)
    assert "<temporary output>" in str(error.value)


def test_runner_timeout_is_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "drums.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/uv")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired("uv", 12)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TranscriptionError, match="timed out after 12s"):
        AdtofActivationRunner(timeout_seconds=12).run(source, _spec())
