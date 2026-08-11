"""Offline coverage for MT3 backend provisioning (issue #285).

Every scenario fakes the checkout, build/command, and downloader seams so
this suite never clones a repository, invokes `uv`, or touches the network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vgt.mt3_provision import (
    MT3_HF_CHECKPOINT_DIR,
    MT3_HF_REPO_ID,
    MT3_HF_REVISION,
    MT3_PINNED_COMMIT,
    MT3_PINNED_TAG,
    MT3_REPO_URL,
    Mt3ProvisionError,
    RuntimeProbe,
    default_cache_dir,
    diagnose_runtime,
    mt3_status,
    provision_mt3,
    require_mt3_provisioned,
)

GOOD_PROBE = RuntimeProbe(system="Darwin", machine="arm64", ffmpeg_path="/opt/homebrew/bin/ffmpeg", uv_version=(0, 9, 20), python311_available=True)


def _ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> SimpleNamespace:
    return SimpleNamespace(returncode=1, stdout="", stderr=stderr)


def _fake_checkout(commit: str = MT3_PINNED_COMMIT):
    calls: list[tuple[str, str, Path]] = []

    def checkout(repo_url: str, ref: str, dest: Path) -> str:
        calls.append((repo_url, ref, dest))
        dest.mkdir(parents=True, exist_ok=True)
        return commit

    return checkout, calls


def _write_model_files(model_dir: Path, contents: dict[str, bytes]) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        path = model_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _counting(result: SimpleNamespace):
    calls: list[tuple] = []

    def fn(*args):
        calls.append(args)
        return result

    return fn, calls


def _patch_good_environment(monkeypatch: pytest.MonkeyPatch, *, checkout=None, build=None, download=None) -> None:
    monkeypatch.setattr("vgt.mt3_provision.probe_runtime", lambda: GOOD_PROBE)
    if checkout is not None:
        monkeypatch.setattr("vgt.mt3_provision._checkout_repo", checkout)
    if build is not None:
        monkeypatch.setattr("vgt.mt3_provision._build_environment", build)
    if download is not None:
        monkeypatch.setattr("vgt.mt3_provision._download_model", download)


def test_first_install_writes_a_manifest_and_reports_not_already_provisioned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "cache"
    checkout, checkout_calls = _fake_checkout()
    build_fn, build_calls = _counting(_ok())

    def download(repo_dir: Path, model_dir: Path) -> SimpleNamespace:
        _write_model_files(model_dir, {"checkpoint_0/model.ckpt": b"weights", "checkpoint_0/config.json": b"{}"})
        return _ok()

    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download)

    result = provision_mt3(cache_dir=cache_dir)

    assert result.already_provisioned is False
    assert checkout_calls == [(MT3_REPO_URL, MT3_PINNED_TAG, cache_dir / "repo")]
    assert len(build_calls) == 1
    assert result.manifest.commit == MT3_PINNED_COMMIT
    assert result.manifest.tag == MT3_PINNED_TAG
    assert {record.relative_path for record in result.manifest.files} == {"checkpoint_0/config.json", "checkpoint_0/model.ckpt"}
    assert all(record.size_bytes > 0 for record in result.manifest.files)
    manifest_path = cache_dir / "checkpoint-manifest.json"
    assert manifest_path.is_file()
    assert mt3_status(cache_dir) == result.manifest
    assert require_mt3_provisioned(cache_dir) == result.manifest


def test_repeated_provisioning_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "cache"
    checkout, _ = _fake_checkout()
    build_fn, build_calls = _counting(_ok())
    download_fn, download_calls = _counting(_ok())

    def download(repo_dir: Path, model_dir: Path) -> SimpleNamespace:
        download_calls.append((repo_dir, model_dir))
        _write_model_files(model_dir, {"checkpoint_0/model.ckpt": b"weights"})
        return _ok()

    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download)

    first = provision_mt3(cache_dir=cache_dir)
    second = provision_mt3(cache_dir=cache_dir)

    assert first.already_provisioned is False
    assert second.already_provisioned is True
    assert second.manifest == first.manifest
    assert len(build_calls) == 1
    assert len(download_calls) == 1


def test_interrupted_partial_model_state_is_resumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "cache"
    checkout, _ = _fake_checkout()
    build_fn, _ = _counting(_ok())
    attempts = {"count": 0}

    def download(repo_dir: Path, model_dir: Path) -> SimpleNamespace:
        attempts["count"] += 1
        if attempts["count"] == 1:
            # Simulate an interrupted download: partial file, nonzero exit.
            _write_model_files(model_dir, {"checkpoint_0/model.ckpt": b"partial"})
            return _fail("connection reset")
        _write_model_files(model_dir, {"checkpoint_0/model.ckpt": b"complete-weights"})
        return _ok()

    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download)

    with pytest.raises(Mt3ProvisionError, match="connection reset"):
        provision_mt3(cache_dir=cache_dir)
    # No manifest was persisted for the failed attempt, so a retry does not
    # think it is already provisioned.
    assert not (cache_dir / "checkpoint-manifest.json").is_file()

    result = provision_mt3(cache_dir=cache_dir)

    assert attempts["count"] == 2
    assert result.already_provisioned is False
    assert result.manifest.files[0].sha256 == __import__("hashlib").sha256(b"complete-weights").hexdigest()


def test_wrong_commit_is_rejected_before_any_build_or_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, _ = _fake_checkout(commit="0" * 40)
    build_fn, build_calls = _counting(_ok())
    download_fn, download_calls = _counting(_ok())
    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download_fn)

    with pytest.raises(Mt3ProvisionError, match="does not match the pinned commit"):
        provision_mt3(cache_dir=tmp_path / "cache")

    assert build_calls == []
    assert download_calls == []


def test_corrupt_model_triggers_a_re_download_on_the_next_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "cache"
    checkout, _ = _fake_checkout()
    build_fn, build_calls = _counting(_ok())
    download_calls: list[Path] = []

    def download(repo_dir: Path, model_dir: Path) -> SimpleNamespace:
        download_calls.append(model_dir)
        _write_model_files(model_dir, {"checkpoint_0/model.ckpt": b"good-weights"})
        return _ok()

    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download)
    first = provision_mt3(cache_dir=cache_dir)

    # Corrupt the on-disk checkpoint after a successful provision.
    (cache_dir / "models" / "checkpoint_0" / "model.ckpt").write_bytes(b"corrupted")

    second = provision_mt3(cache_dir=cache_dir)

    assert len(download_calls) == 2  # re-invoked because the manifest no longer matches disk
    assert second.already_provisioned is False
    assert second.manifest == first.manifest  # download fixture rewrites the good bytes each time


def test_empty_checkpoint_file_is_reported_as_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, _ = _fake_checkout()
    build_fn, _ = _counting(_ok())

    def download(repo_dir: Path, model_dir: Path) -> SimpleNamespace:
        _write_model_files(model_dir, {"checkpoint_0/model.ckpt": b""})
        return _ok()

    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download)

    with pytest.raises(Mt3ProvisionError, match="empty"):
        provision_mt3(cache_dir=tmp_path / "cache")


def test_no_files_downloaded_is_reported_as_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, _ = _fake_checkout()
    build_fn, _ = _counting(_ok())
    download_fn, _ = _counting(_ok())
    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download_fn)

    with pytest.raises(Mt3ProvisionError, match="no checkpoint files"):
        provision_mt3(cache_dir=tmp_path / "cache")


@pytest.mark.parametrize(
    "probe",
    [
        RuntimeProbe(system="Linux", machine="x86_64", ffmpeg_path="/usr/bin/ffmpeg", uv_version=(0, 9, 20), python311_available=True),
        RuntimeProbe(system="Darwin", machine="arm64", ffmpeg_path=None, uv_version=(0, 9, 20), python311_available=True),
        RuntimeProbe(system="Darwin", machine="arm64", ffmpeg_path="/opt/homebrew/bin/ffmpeg", uv_version=None, python311_available=True),
        RuntimeProbe(system="Darwin", machine="arm64", ffmpeg_path="/opt/homebrew/bin/ffmpeg", uv_version=(0, 8, 0), python311_available=True),
        RuntimeProbe(system="Darwin", machine="arm64", ffmpeg_path="/opt/homebrew/bin/ffmpeg", uv_version=(0, 9, 20), python311_available=False),
    ],
)
def test_unsupported_runtime_is_rejected_before_any_network_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: RuntimeProbe) -> None:
    monkeypatch.setattr("vgt.mt3_provision.probe_runtime", lambda: probe)
    checkout_fn, checkout_calls = _counting("unused")
    monkeypatch.setattr("vgt.mt3_provision._checkout_repo", checkout_fn)

    with pytest.raises(Mt3ProvisionError):
        provision_mt3(cache_dir=tmp_path / "cache")

    assert checkout_calls == []


def test_diagnose_runtime_reports_every_problem_for_a_maximally_bad_probe() -> None:
    bad_probe = RuntimeProbe(system="Linux", machine="x86_64", ffmpeg_path=None, uv_version=None, python311_available=False)

    problems = diagnose_runtime(bad_probe)

    assert len(problems) == 4
    assert diagnose_runtime(GOOD_PROBE) == ()


def test_build_failure_is_reported_and_no_manifest_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "cache"
    checkout, _ = _fake_checkout()
    build_fn, _ = _counting(_fail("uv sync exploded"))
    download_fn, download_calls = _counting(_ok())
    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download_fn)

    with pytest.raises(Mt3ProvisionError, match="uv sync exploded"):
        provision_mt3(cache_dir=cache_dir)

    assert download_calls == []
    assert not (cache_dir / "checkpoint-manifest.json").is_file()


def test_download_failure_is_reported_and_no_manifest_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "cache"
    checkout, _ = _fake_checkout()
    build_fn, _ = _counting(_ok())

    def download(repo_dir: Path, model_dir: Path) -> SimpleNamespace:
        model_dir.mkdir(parents=True, exist_ok=True)
        return _fail("checksum mismatch against the official bucket")

    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download)

    with pytest.raises(Mt3ProvisionError, match="checksum mismatch"):
        provision_mt3(cache_dir=cache_dir)

    assert not (cache_dir / "checkpoint-manifest.json").is_file()


def test_force_rebuilds_and_redownloads_even_when_already_provisioned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "cache"
    checkout, _ = _fake_checkout()
    build_fn, build_calls = _counting(_ok())
    download_calls: list[Path] = []

    def download(repo_dir: Path, model_dir: Path) -> SimpleNamespace:
        download_calls.append(model_dir)
        _write_model_files(model_dir, {"checkpoint_0/model.ckpt": b"weights"})
        return _ok()

    _patch_good_environment(monkeypatch, checkout=checkout, build=build_fn, download=download)
    provision_mt3(cache_dir=cache_dir)
    provision_mt3(cache_dir=cache_dir, force=True)

    assert len(build_calls) == 2
    assert len(download_calls) == 2


def test_mt3_status_and_require_are_local_only_when_never_provisioned(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"

    assert mt3_status(cache_dir) is None
    with pytest.raises(Mt3ProvisionError, match="not provisioned"):
        require_mt3_provisioned(cache_dir)


def test_default_cache_dir_honours_the_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("vgt.mt3_provision.os.environ", {"VGT_MT3_CACHE_DIR": str(tmp_path / "custom")})
    assert default_cache_dir() == tmp_path / "custom"

    monkeypatch.setattr("vgt.mt3_provision.os.environ", {})
    assert default_cache_dir() == Path.home() / "Library" / "Caches" / "vgt" / "mt3"


def test_downloader_pins_the_requested_4s_hugging_face_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vgt.mt3_provision import _download_model

    calls: list[tuple[list[str], Path]] = []

    def fake_run(argv: list[str], *, cwd: Path) -> SimpleNamespace:
        calls.append((argv, cwd))
        return _ok()

    monkeypatch.setattr("vgt.mt3_provision._run", fake_run)
    repo_dir, model_dir = tmp_path / "repo", tmp_path / "models"

    _download_model(repo_dir, model_dir)

    argv, cwd = calls[0]
    assert cwd == repo_dir
    assert argv[:5] == ["uv", "run", "--project", str(repo_dir), "python"]
    assert argv[-4:] == [str(model_dir), MT3_HF_REPO_ID, MT3_HF_REVISION, MT3_HF_CHECKPOINT_DIR]
    assert "snapshot_download" in argv[6]
    assert "checkpoint_0" in argv[6]
