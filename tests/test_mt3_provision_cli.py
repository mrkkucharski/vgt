"""CLI wiring for `vgt transcription backend provision mt3` (issue #285).

This command deliberately runs without a REAPER project in scope, so these
tests run from a `tmp_path` that never contains a `.RPP` file. The actual
provisioning logic is exercised offline in test_mt3_provision.py; here we only
check that the CLI plumbs `--force` through and reports errors cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vgt.cli import main
from vgt.mt3_provision import CheckpointManifest, ModelFileRecord, Mt3ProvisionError, Mt3ProvisionResult


def _fake_result(cache_dir: Path, *, already_provisioned: bool = False) -> Mt3ProvisionResult:
    manifest = CheckpointManifest(
        commit="4b49f9b9d38549fcc0231efbff3f4e85b3690923", tag="v0.1.0",
        files=(ModelFileRecord(relative_path="checkpoint_0/model.ckpt", size_bytes=8, sha256="a" * 64),),
        fingerprint="b" * 64, model_id="model-1", hf_revision="rev-1", hf_checkpoint_dir="ckpt-1",
    )
    return Mt3ProvisionResult(
        cache_dir=cache_dir, repo_dir=cache_dir / "repo", model_dir=cache_dir / "models",
        manifest=manifest, already_provisioned=already_provisioned,
    )


def test_provision_mt3_does_not_require_a_reaper_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[dict] = []

    def fake_provision(*, force, progress=None):
        calls.append({"force": force})
        return _fake_result(tmp_path / "cache")

    monkeypatch.setattr("vgt.cli.provision_mt3", fake_provision)
    monkeypatch.chdir(tmp_path)  # no .RPP file anywhere nearby

    exit_code = main(["transcription", "backend", "provision", "mt3"])

    assert exit_code == 0
    assert calls == [{"force": False}]
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "mt3"
    assert payload["already_provisioned"] is False
    assert payload["commit"] == "4b49f9b9d38549fcc0231efbff3f4e85b3690923"
    assert payload["checkpoint_files"] == 1


def test_provision_mt3_force_flag_is_passed_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[dict] = []

    def fake_provision(*, force, progress=None):
        calls.append({"force": force})
        return _fake_result(tmp_path / "cache", already_provisioned=True)

    monkeypatch.setattr("vgt.cli.provision_mt3", fake_provision)
    monkeypatch.chdir(tmp_path)

    assert main(["transcription", "backend", "provision", "mt3", "--force"]) == 0
    assert calls == [{"force": True}]


def test_provision_mt3_error_is_reported_with_exit_code_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def fake_provision(*, force, progress=None):
        raise Mt3ProvisionError("ffmpeg was not found on PATH; install it with `brew install ffmpeg`")

    monkeypatch.setattr("vgt.cli.provision_mt3", fake_provision)
    monkeypatch.chdir(tmp_path)

    assert main(["transcription", "backend", "provision", "mt3"]) == 2
    assert "ffmpeg was not found" in capsys.readouterr().err


def test_provision_unknown_backend_name_is_rejected_by_argparse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["transcription", "backend", "provision", "not-a-backend"])
