"""Install the bundled REAPER actions without depending on a checkout."""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources
from pathlib import Path
import sys


SCRIPT_NAMES = (
    "vgt_initialize.lua",
    "vgt_sync.lua",
    "vgt_sync_tempo_map.lua",
    "vgt_create_working_copy.lua",
    "vgt_promote_working_copy.lua",
    "vgt_working_copy_common.lua",
)


class ReaScriptInstallError(RuntimeError):
    """The ReaScript files could not be installed safely."""


def default_destination() -> Path:
    """Return vgt's private directory in REAPER's standard macOS Scripts folder."""
    return Path.home() / "Library" / "Application Support" / "REAPER" / "Scripts" / "vgt"


def _script_bytes(name: str) -> bytes:
    """Read a packaged script, with a source-tree fallback for contributors."""
    packaged = resources.files("vgt").joinpath("reascript", name)
    try:
        return packaged.read_bytes()
    except FileNotFoundError:
        # Hatch maps the top-level `reascript/` directory into the installed
        # package.  Keeping this fallback makes `uv run vgt` work before a
        # wheel is built, without affecting installed-package behavior.
        source_tree = Path(__file__).resolve().parents[2] / "reascript" / name
        try:
            return source_tree.read_bytes()
        except FileNotFoundError as exc:
            raise ReaScriptInstallError(f"bundled ReaScript is missing: {name}") from exc


def install_reascripts(
    destination: Path | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    confirm: Callable[[list[Path]], bool] | None = None,
) -> tuple[Path, ...]:
    """Copy the bundled actions to *destination*, refusing unsafe replacements.

    Existing identical files are left untouched.  Different files require
    ``force`` or an affirmative callback confirmation.
    """
    directory = destination if destination is not None else default_destination()
    payloads = {name: _script_bytes(name) for name in SCRIPT_NAMES}
    targets = tuple(directory / name for name in SCRIPT_NAMES)
    conflicts = [target for target in targets if target.exists() and target.read_bytes() != payloads[target.name]]

    if conflicts and not force:
        if confirm is None or not confirm(conflicts):
            rendered = ", ".join(str(path) for path in conflicts)
            raise ReaScriptInstallError(
                f"refusing to overwrite differing ReaScript(s): {rendered}; rerun with --force to replace them"
            )

    if dry_run:
        return targets

    directory.mkdir(parents=True, exist_ok=True)
    for target in targets:
        payload = payloads[target.name]
        if not target.exists() or target.read_bytes() != payload:
            target.write_bytes(payload)
    return targets


def confirm_overwrite(conflicts: list[Path]) -> bool:
    """Ask the interactive user before replacing a locally changed action."""
    if not sys.stdin.isatty():
        return False
    answer = input(f"Replace {len(conflicts)} differing vgt ReaScript(s)? Type 'yes' to continue: ")
    return answer.strip().lower() == "yes"
