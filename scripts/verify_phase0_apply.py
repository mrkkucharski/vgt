#!/usr/bin/env python3
"""Check the persisted result of a live Phase 0 REAPER apply.

This intentionally reads the saved RPP and sidecar after REAPER has run the
ReaScript.  It does not invoke REAPER or modify the project.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vgt.project import ProjectError, _track_blocks, locate_project, read_project  # noqa: E402


FOLDER_NAME = "[vgt] Practice"
MIRROR_NAME = "[vgt] Mirror"
EXPECTED_CONFIG = {"folder_name": FOLDER_NAME, "mirror_name": MIRROR_NAME}
# REAPER persists the I_FOLDERDEPTH API value as the second ISBUS field in RPP.
_FOLDER_DEPTH = re.compile(r"^\s*ISBUS\s+\d+\s+(-?\d+)", re.MULTILINE)
_FILE = re.compile(r'^\s*FILE\s+"(?P<path>.*)"', re.MULTILINE)


class VerificationError(ValueError):
    """The saved project does not meet Phase 0's live-apply invariants."""


def _fail(message: str) -> None:
    raise VerificationError(message)


def _depth(block: str) -> int:
    match = _FOLDER_DEPTH.search(block)
    return int(match.group(1)) if match else 0


def _files(block: str) -> list[str]:
    return [match.group("path") for match in _FILE.finditer(block)]


def verify(project_path: Path, baseline_path: Path) -> dict[str, object]:
    """Validate a saved copy against its unmodified source project."""
    try:
        baseline = read_project(baseline_path)
        project = read_project(project_path)
    except ProjectError as error:
        raise VerificationError(str(error)) from error

    if (project.sample_rate, project.tempo, project.time_signature_numerator, project.time_signature_denominator) != (
        baseline.sample_rate,
        baseline.tempo,
        baseline.time_signature_numerator,
        baseline.time_signature_denominator,
    ):
        _fail("project sample rate, tempo, or time signature changed")

    original = tuple((track.name, track.guid) for track in baseline.tracks)
    actual_original = tuple((track.name, track.guid) for track in project.tracks[: len(original)])
    if actual_original != original:
        _fail("the original tracks' names, GUIDs, or ordering changed")

    sidecar_file = project_path.with_name("vgt.json")
    try:
        sidecar = json.loads(sidecar_file.read_text(encoding="utf-8"))
    except OSError as error:
        raise VerificationError(f"cannot read sidecar {sidecar_file}: {error}") from error
    except json.JSONDecodeError as error:
        raise VerificationError(f"invalid JSON in {sidecar_file}: {error}") from error

    if sidecar.get("schema_version") != 1:
        _fail("vgt.json schema_version is not 1")
    if sidecar.get("config") != EXPECTED_CONFIG:
        _fail("vgt.json config is not the Phase 0 practice/mirror config")
    managed_guids = sidecar.get("managed_track_guids")
    if not isinstance(managed_guids, list) or len(managed_guids) != 2 or len(set(managed_guids)) != 2:
        _fail("vgt.json must contain exactly two distinct managed_track_guids")

    blocks = _track_blocks(project_path.read_text(encoding="utf-8", errors="replace"))
    current_managed = [(track.name, track.guid) for track in project.tracks if track.name.startswith("[vgt]")]
    if len(current_managed) != 2 or {guid for _, guid in current_managed} != set(managed_guids):
        _fail("vgt.json GUIDs do not exactly match the two current [vgt] tracks")
    if [name for name, _ in current_managed] != [FOLDER_NAME, MIRROR_NAME]:
        _fail("expected exactly one [vgt] Practice folder and one [vgt] Mirror child")

    block_by_guid = dict(blocks)
    folder_guid, mirror_guid = managed_guids
    # GUID order is written by the script as folder then mirror.
    if _depth(block_by_guid.get(folder_guid, "")) != 1 or _depth(block_by_guid.get(mirror_guid, "")) != -1:
        _fail("the vgt tracks do not have folder depths +1 (Practice) and -1 (Mirror)")

    baseline_blocks = dict(_track_blocks(baseline_path.read_text(encoding="utf-8", errors="replace")))
    source_files = [filename for _, guid in original for filename in _files(baseline_blocks[guid])]
    mirror_files = _files(block_by_guid[mirror_guid])
    if mirror_files != source_files:
        _fail("mirror items are not exactly the original file-backed media items")
    for filename in mirror_files:
        media_path = Path(filename)
        if not media_path.is_absolute():
            media_path = project_path.parent / media_path
        if not media_path.is_file():
            _fail(f"mirror media file is missing: {filename}")

    return {
        "project": str(project_path),
        "original_tracks": len(original),
        "managed_track_guids": managed_guids,
        "mirror_file_backed_items": len(mirror_files),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="saved disposable .RPP copy to inspect")
    parser.add_argument("--baseline", required=True, help="unmodified source .RPP")
    args = parser.parse_args(argv)
    try:
        result = verify(locate_project(args.project), locate_project(args.baseline))
    except VerificationError as error:
        print(f"Phase 0 verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
