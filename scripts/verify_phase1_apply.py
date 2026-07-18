#!/usr/bin/env python3
"""Check the persisted result of a live Phase 1 REAPER apply.

The verifier is deliberately read-only: REAPER performs every project mutation
through ``reascript/vgt_initialize.lua`` and this script examines the saved RPP
and its sidecar after first apply or re-apply.
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


PREFIX = "[vgt]"
CHORDS_NAME = f"{PREFIX} Chords"
BEATS_NAME = f"{PREFIX} Beats"
_MUTE = re.compile(r"^\s*MUTESOLO\s+1\b", re.MULTILINE)
_LOCK = re.compile(r"^\s*LOCK\s+1\b", re.MULTILINE)
_NOTES = re.compile(r"^\s*<NOTES\s*$", re.MULTILINE)
_TEMPO_MARKER = re.compile(r"^\s*PT\s+[-+.\d]+\s+[-+.\d]+", re.MULTILINE)
_REGION = re.compile(r'^\s*MARKER\s+\d+\s+[-+.\d]+\s+"(?P<name>\[vgt\].*)"\s+[-+.\d]+', re.MULTILINE)


class VerificationError(ValueError):
    """The saved project does not meet Phase 1's live-apply invariants."""


def _fail(message: str) -> None:
    raise VerificationError(message)


def verify(project_path: Path, baseline_path: Path) -> dict[str, object]:
    try:
        baseline = read_project(baseline_path)
        project = read_project(project_path)
    except ProjectError as error:
        raise VerificationError(str(error)) from error

    original = tuple((track.name, track.guid) for track in baseline.tracks)
    actual_original = tuple((track.name, track.guid) for track in project.tracks[: len(original)])
    if actual_original != original:
        _fail("the original tracks' names, GUIDs, or ordering changed")

    try:
        sidecar = json.loads(project_path.with_suffix(".vgt").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot read a valid sidecar: {error}") from error
    analysis = sidecar.get("analysis")
    if sidecar.get("schema_version") != 2 or not isinstance(analysis, dict):
        _fail("Phase 1 sidecar analysis is missing")

    blocks = dict(_track_blocks(project_path.read_text(encoding="utf-8", errors="replace")))
    names_by_guid = {track.guid: track.name for track in project.tracks}
    managed = sidecar.get("managed_track_guids")
    if not isinstance(managed, list) or len(set(managed)) != len(managed):
        _fail("managed_track_guids is missing or contains duplicates")
    current_vgt = {track.guid for track in project.tracks if track.name.startswith(PREFIX)}
    if set(managed) != current_vgt:
        _fail("sidecar GUIDs do not exactly match the current [vgt] tracks")

    chord_guid = next((guid for guid in managed if names_by_guid.get(guid) == CHORDS_NAME), None)
    if chord_guid is None:
        _fail(f"missing {CHORDS_NAME!r} track")
    chord_block = blocks[chord_guid]
    if not _MUTE.search(chord_block) or not _LOCK.search(chord_block):
        _fail("[vgt] Chords is not both muted and locked")

    chord_value = (analysis.get("chords") or {}).get("value")
    expected_chords = chord_value.get("segments", chord_value) if isinstance(chord_value, dict) else chord_value
    if isinstance(expected_chords, list) and expected_chords:
        notes = _NOTES.findall(chord_block)
        if len(notes) != len(expected_chords):
            _fail("[vgt] Chords item count does not match sidecar chord segments")

    rpp_text = project_path.read_text(encoding="utf-8", errors="replace")
    sections = (analysis.get("sections") or {}).get("value")
    if isinstance(sections, list) and sections:
        regions = _REGION.findall(rpp_text)
        if len(regions) != len(sections):
            _fail("[vgt] section regions do not match sidecar sections")

    config = sidecar.get("config") or {}
    tempo_applied = config.get("tempo_map_applied")
    if tempo_applied is True:
        if not _TEMPO_MARKER.search(rpp_text):
            _fail("sidecar says the tempo map was applied, but RPP has no tempo markers")
        if BEATS_NAME in names_by_guid.values():
            _fail("tempo-map case must not also create a [vgt] Beats fallback track")
    elif tempo_applied is False and isinstance((analysis.get("tempo") or {}).get("value"), dict):
        beat_guid = next((guid for guid in managed if names_by_guid.get(guid) == BEATS_NAME), None)
        if beat_guid is None:
            _fail("existing tempo map needs the non-invasive [vgt] Beats fallback track")
        beat_block = blocks[beat_guid]
        if not _MUTE.search(beat_block) or not _LOCK.search(beat_block):
            _fail("[vgt] Beats is not both muted and locked")

    return {
        "project": str(project_path),
        "managed_tracks": [names_by_guid[guid] for guid in managed],
        "tempo_map_applied": tempo_applied,
        "section_count": len(sections) if isinstance(sections, list) else 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="saved disposable .RPP copy to inspect")
    parser.add_argument("--baseline", required=True, help="unmodified source .RPP")
    args = parser.parse_args(argv)
    try:
        result = verify(locate_project(args.project), locate_project(args.baseline))
    except VerificationError as error:
        print(f"Phase 1 verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
