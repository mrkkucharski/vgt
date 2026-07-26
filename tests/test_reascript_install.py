from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest

from vgt.cli import main
from vgt.reascripts import ReaScriptInstallError, SCRIPT_NAMES, install_reascripts


def test_install_reascripts_copies_all_actions_and_is_idempotent(tmp_path: Path) -> None:
    destination = tmp_path / "Scripts" / "vgt"

    installed = install_reascripts(destination)

    assert [path.name for path in installed] == list(SCRIPT_NAMES)
    assert all(path.is_file() for path in installed)
    before = {path: path.stat().st_mtime_ns for path in installed}
    assert install_reascripts(destination) == installed
    assert {path: path.stat().st_mtime_ns for path in installed} == before


def test_install_reascripts_refuses_different_file_without_confirmation(tmp_path: Path) -> None:
    destination = tmp_path / "vgt"
    destination.mkdir()
    changed = destination / "vgt_sync.lua"
    changed.write_text("-- user edit\n")

    with pytest.raises(ReaScriptInstallError, match="--force"):
        install_reascripts(destination)

    assert changed.read_text() == "-- user edit\n"


def test_install_reascripts_force_replaces_different_file(tmp_path: Path) -> None:
    destination = tmp_path / "vgt"
    destination.mkdir()
    changed = destination / "vgt_sync.lua"
    changed.write_text("-- user edit\n")

    install_reascripts(destination, force=True)

    assert changed.read_bytes() != b"-- user edit\n"


def test_install_reascripts_dry_run_creates_nothing(tmp_path: Path) -> None:
    destination = tmp_path / "Scripts" / "vgt"

    installed = install_reascripts(destination, dry_run=True)

    assert [path.name for path in installed] == list(SCRIPT_NAMES)
    assert not destination.exists()


def test_cli_install_reports_paths_and_action_list_step(tmp_path: Path, capsys) -> None:
    destination = tmp_path / "vgt"

    assert main(["install-reascripts", "--destination", str(destination), "--dry-run"]) == 0

    output = capsys.readouterr().out
    for name in SCRIPT_NAMES:
        assert str(destination / name) in output
    assert "Action List" in output


def test_build_artifacts_contain_all_reascripts(tmp_path: Path) -> None:
    """The install command must work after `uv tool install`, not only in a checkout."""
    root = Path(__file__).parents[1]
    dist = tmp_path / "dist"
    subprocess.run(["uv", "build", "--out-dir", str(dist)], cwd=root, check=True, capture_output=True, text=True)

    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert {f"vgt/reascript/{name}" for name in SCRIPT_NAMES} <= names

    sdist = next(dist.glob("*.tar.gz"))
    with tarfile.open(sdist) as archive:
        names = set(archive.getnames())
    assert all(any(name.endswith(f"reascript/{script}") for name in names) for script in SCRIPT_NAMES)
