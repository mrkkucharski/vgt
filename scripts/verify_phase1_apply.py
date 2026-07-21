#!/usr/bin/env python3
"""Verify Phase 1 apply state, or run the complete disposable REAPER proof.

REAPER is the sole RPP mutator.  ``--run-live`` copies the fixture, applies the
action twice, and then validates the two saved projects; ordinary invocation is
read-only and validates an already-saved disposable project.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vgt.project import ProjectError, _track_blocks, locate_project, read_project  # noqa: E402


PREFIX = "[vgt]"
CHORDS_NAME = f"{PREFIX} Chords"
BEATS_NAME = f"{PREFIX} Beats"
USER_REGION_NAME = f"{PREFIX} User-created region"
REAPER = Path("/Applications/REAPER.app/Contents/MacOS/REAPER")
APPLY = ROOT / "reascript" / "vgt_initialize.lua"
_MUTE = re.compile(r"^\s*MUTESOLO\s+1\b", re.MULTILINE)
_LOCK = re.compile(r"^\s*LOCK\s+1\b", re.MULTILINE)
_TEMPO_MARKER = re.compile(r"^\s*PT\s+([-+.\d]+)\s+([-+.\d]+)", re.MULTILINE)
_REGION = re.compile(r'^\s*MARKER\s+(?P<id>\d+)\s+(?P<start>[-+.\d]+)\s+"(?P<name>\[vgt\].*)"', re.MULTILINE)
_ITEM_START = re.compile(r"^\s*<ITEM\s*$", re.MULTILINE)
_POSITION = re.compile(r"^\s*POSITION\s+([-+.\d]+)", re.MULTILINE)
_LENGTH = re.compile(r"^\s*LENGTH\s+([-+.\d]+)", re.MULTILINE)
_TAKE_NAME = re.compile(r'^\s*NAME\s+(?:"(?P<quoted>.*)"|(?P<plain>\S.*))\s*$', re.MULTILINE)


class VerificationError(ValueError):
    """The saved project does not meet Phase 1's live-apply invariants."""


def _fail(message: str) -> None:
    raise VerificationError(message)


def _blocks(text: str, start_pattern: re.Pattern[str]) -> list[str]:
    """Return balanced RPP chunks beginning at each matching opening line."""
    result: list[str] = []
    for start in start_pattern.finditer(text):
        depth = 0
        for line in re.finditer(r".*(?:\n|$)", text[start.start() :]):
            stripped = line.group(0).strip()
            if stripped.startswith("<"):
                depth += 1
            elif stripped == ">":
                depth -= 1
                if depth == 0:
                    result.append(text[start.start() : start.start() + line.end()])
                    break
    return result


def _number(block: str, pattern: re.Pattern[str], what: str) -> float:
    match = pattern.search(block)
    if match is None:
        _fail(f"item is missing {what}")
    return float(match.group(1))


def _expected_sections(analysis: dict[str, object], reference_start: float) -> list[tuple[str, float, float]]:
    sections = ((analysis.get("sections") or {}) if isinstance(analysis, dict) else {}).get("value")
    if not isinstance(sections, list):
        return []
    return [
        (f"{PREFIX} {section.get('label') or section.get('name') or 'section'}", reference_start + float(section.get("start_seconds", 0)), reference_start + float(section.get("end_seconds", 0)))
        for section in sections
        if isinstance(section, dict) and float(section.get("end_seconds", 0)) > float(section.get("start_seconds", 0))
    ]


def _expected_chords(analysis: dict[str, object], reference_start: float) -> list[tuple[str, float, float]]:
    chord_value = ((analysis.get("chords") or {}) if isinstance(analysis, dict) else {}).get("value")
    segments = chord_value.get("segments", chord_value) if isinstance(chord_value, dict) else chord_value
    if not isinstance(segments, list):
        return []
    return [
        (str(chord.get("chord") or chord.get("label") or "N"), reference_start + float(chord.get("start_seconds", 0)), reference_start + float(chord.get("end_seconds", 0)))
        for chord in segments
        if isinstance(chord, dict) and float(chord.get("end_seconds", 0)) > float(chord.get("start_seconds", 0))
    ]


def _same_time(actual: float, expected: float, label: str) -> None:
    if abs(actual - expected) > 0.00001:
        _fail(f"{label} is {actual}, expected {expected}")


def _original_snapshot(project_path: Path, baseline: object) -> tuple[str, ...]:
    blocks = dict(_track_blocks(project_path.read_text(encoding="utf-8", errors="replace")))
    return tuple(blocks[track.guid] for track in baseline.tracks)


def verify(project_path: Path, baseline_path: Path, *, first_snapshot: dict[str, object] | None = None, expect_user_region: bool = False) -> dict[str, object]:
    try:
        baseline = read_project(baseline_path)
        project = read_project(project_path)
    except ProjectError as error:
        raise VerificationError(str(error)) from error

    original = tuple((track.name, track.guid) for track in baseline.tracks)
    if tuple((track.name, track.guid) for track in project.tracks[: len(original)]) != original:
        _fail("the original tracks' names, GUIDs, or ordering changed")
    original_blocks = _original_snapshot(project_path, baseline)
    if first_snapshot and original_blocks != first_snapshot["original_blocks"]:
        _fail("re-apply modified a non-[vgt] track block")

    try:
        sidecar = json.loads(project_path.with_suffix(".vgt").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot read a valid sidecar: {error}") from error
    analysis = sidecar.get("analysis")
    if sidecar.get("schema_version") != 4 or not isinstance(analysis, dict):
        _fail("Phase 1 sidecar analysis is missing")

    rpp_text = project_path.read_text(encoding="utf-8", errors="replace")
    blocks = dict(_track_blocks(rpp_text))
    names_by_guid = {track.guid: track.name for track in project.tracks}
    managed = sidecar.get("managed_track_guids")
    if not isinstance(managed, list) or len(set(managed)) != len(managed):
        _fail("managed_track_guids is missing or contains duplicates")
    if set(managed) != {track.guid for track in project.tracks if track.name.startswith(PREFIX)}:
        _fail("sidecar GUIDs do not exactly match the current [vgt] tracks")

    reference_guid = (sidecar.get("config") or {}).get("reference_track_guid")
    reference = next((track for track in project.tracks if track.guid == reference_guid), None)
    if reference is None:
        _fail("sidecar reference track no longer exists")
    reference_block = blocks[reference.guid]
    reference_items = _blocks(reference_block, _ITEM_START)
    reference_start = min((_number(item, _POSITION, "POSITION") for item in reference_items), default=0.0)

    chord_guid = next((guid for guid in managed if names_by_guid.get(guid) == CHORDS_NAME), None)
    if chord_guid is None:
        _fail(f"missing {CHORDS_NAME!r} track")
    chord_block = blocks[chord_guid]
    if _MUTE.search(chord_block):
        _fail("[vgt] Chords must stay unmuted so labels remain visible")
    chord_items = _blocks(chord_block, _ITEM_START)
    expected_chords = _expected_chords(analysis, reference_start)
    if len(chord_items) != len(expected_chords):
        _fail("[vgt] Chords item count does not match sidecar chord segments")
    for item, (label, start, end) in zip(chord_items, expected_chords):
        if _LOCK.search(item):
            _fail("[vgt] Chords contains a locked label item; chord items must stay editable")
        _same_time(_number(item, _POSITION, "POSITION"), start, f"chord {label!r} start")
        _same_time(_number(item, _LENGTH, "LENGTH"), end - start, f"chord {label!r} length")
        name = _TAKE_NAME.search(item)
        actual_label = (name.group("quoted") or name.group("plain")) if name else None
        if actual_label != label:
            _fail(f"chord item label is {actual_label!r}, expected {label!r}")

    managed_region_ids = sidecar.get("managed_region_ids")
    if not isinstance(managed_region_ids, list) or any(not isinstance(region_id, int) for region_id in managed_region_ids) or len(set(managed_region_ids)) != len(managed_region_ids):
        _fail("managed_region_ids is missing or contains duplicates")
    actual_regions = [(int(match.group("id")), match.group("name"), float(match.group("start"))) for match in _REGION.finditer(rpp_text)]
    actual_sections = [(name, start) for region_id, name, start in actual_regions if region_id in managed_region_ids]
    expected_sections = _expected_sections(analysis, reference_start)
    if len(actual_sections) != len(expected_sections):
        _fail("[vgt] section regions do not match sidecar sections")
    for actual, expected in zip(actual_sections, expected_sections):
        if actual[0] != expected[0]:
            _fail(f"section name is {actual[0]!r}, expected {expected[0]!r}")
        _same_time(actual[1], expected[1], f"section {expected[0]!r} start")
    user_region_present = any(name == USER_REGION_NAME for _, name, _ in actual_regions)
    if expect_user_region and not user_region_present:
        _fail("the user-created [vgt] region was removed")

    config = sidecar.get("config") or {}
    tempo_applied = config.get("tempo_map_applied")
    tempo_fingerprint = config.get("tempo_map_fingerprint")
    tempo = ((analysis.get("tempo") or {}) if isinstance(analysis, dict) else {}).get("value")
    beat_guid = next((guid for guid in managed if names_by_guid.get(guid) == BEATS_NAME), None)
    if tempo_applied is True:
        if not _TEMPO_MARKER.search(rpp_text):
            _fail("sidecar says the tempo map was applied, but RPP has no tempo markers")
        if tempo_fingerprint:
            # The recorded fingerprint still matches the live map (issue #27):
            # vgt owns it and refreshed it, so no non-invasive fallback is needed.
            if beat_guid is not None:
                _fail("a current vgt tempo map must not also create a [vgt] Beats fallback track")
        elif beat_guid is None:
            # A vgt-created map exists but has since diverged from what vgt
            # wrote (no live fingerprint match): it is left untouched and the
            # latest tempo data is only ever offered non-invasively.
            _fail("a diverged vgt tempo map needs the non-invasive [vgt] Beats fallback track")
    elif tempo_applied is False and isinstance(tempo, dict):
        if beat_guid is None:
            _fail("existing tempo map needs the non-invasive [vgt] Beats fallback track")
    if beat_guid is not None:
        beat_block = blocks[beat_guid]
        if _MUTE.search(beat_block) or any(not _LOCK.search(item) for item in _blocks(beat_block, _ITEM_START)):
            _fail("[vgt] Beats must stay unmuted with locked marker items")

    result = {"original_blocks": original_blocks, "managed_names": tuple(names_by_guid[guid] for guid in managed), "regions": tuple(actual_sections), "user_region_present": user_region_present, "tempo_markers": tuple(_TEMPO_MARKER.findall(rpp_text)), "tempo_map_applied": tempo_applied, "tempo_map_fingerprint": tempo_fingerprint}
    if first_snapshot and {key: value for key, value in result.items() if key != "original_blocks"} != {key: value for key, value in first_snapshot.items() if key != "original_blocks"}:
        _fail("re-apply did not persist the same Phase 1 state")
    return result


def _analysis() -> dict[str, object]:
    return {"tempo": {"value": {"bpm": 100.0, "downbeat_offset_seconds": 0.0, "time_signature": "4/4", "mode": "constant"}}, "sections": {"value": [{"label": "intro", "start_seconds": 0.0, "end_seconds": 8.0}, {"label": "verse", "start_seconds": 8.0, "end_seconds": 16.0}]}, "chords": {"value": {"segments": [{"chord": "Am", "start_seconds": 0.0, "end_seconds": 2.4}, {"chord": "F", "start_seconds": 2.4, "end_seconds": 4.8}]}}}


def _run_reaper(project: Path, run_dir: Path, *, existing_map: bool, user_region: bool = False) -> list[tuple[str, float, float]]:
    select = run_dir / "select.lua"
    save = run_dir / "save.lua"
    report = run_dir / "regions.tsv"
    select.write_text('reaper.SetExtState("vgt", "reference_index", "0", false)\n', encoding="utf-8")
    # The command-line REAPER process otherwise remains open after it has run
    # the action, which would make this verifier hang in CI.
    save.write_text(
        "local f = assert(io.open(" + repr(str(report)) + ", 'w'))\n"
        "for i = 0, reaper.CountProjectMarkers(0) - 1 do\n"
        "  local _, region, start, finish, name = reaper.EnumProjectMarkers3(0, i)\n"
        "  if region and name:sub(1, 5) == '[vgt]' then f:write(name, '\\t', start, '\\t', finish, '\\n') end\n"
        "end\n"
        "f:close()\nreaper.Main_SaveProject(0, false)\nreaper.Main_OnCommand(40004, 0)\n",
        encoding="utf-8",
    )
    arguments = [str(REAPER), "-newinst", str(project)]
    if existing_map:
        tempo = run_dir / "existing-tempo.lua"
        tempo.write_text("reaper.SetTempoTimeSigMarker(0, -1, 0, -1, -1, 140, 4, 4, false)\n", encoding="utf-8")
        arguments.append(str(tempo))
    if user_region:
        user_region_script = run_dir / "user-region.lua"
        user_region_script.write_text(
            f'reaper.AddProjectMarker2(0, true, 20, 24, "{USER_REGION_NAME}", -1, 0)\n', encoding="utf-8"
        )
        arguments.append(str(user_region_script))
    arguments.extend((str(select), str(APPLY), str(save)))
    subprocess.run(arguments, check=True, timeout=90)
    return [(name, float(start), float(end)) for name, start, end in (line.split("\t") for line in report.read_text(encoding="utf-8").splitlines())]


def run_live(baseline: Path) -> dict[str, object]:
    if not REAPER.is_file():
        raise VerificationError(f"REAPER not found at {REAPER}")
    root = Path(tempfile.mkdtemp(prefix="vgt-phase1-"))
    try:
        outcomes: dict[str, object] = {}
        for name, existing_map in (("default", False), ("existing-map", True)):
            run_dir = root / name
            fixture_dir = run_dir / "Reaper Project"
            shutil.copytree(baseline.parent, fixture_dir)
            project = fixture_dir / baseline.name
            _run_reaper(project, run_dir, existing_map=existing_map)  # Phase 0 creates its sidecar.
            sidecar_path = project.with_suffix(".vgt")
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar.update(schema_version=4, analysis=_analysis())
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
            api_sections = [region for region in _run_reaper(project, run_dir, existing_map=False, user_region=True) if region[0] != USER_REGION_NAME]
            first = verify(project, baseline, expect_user_region=True)
            # A pre-existing tempo map may display the reference item's RPP
            # position differently in seconds.  Its first section establishes
            # that live REAPER offset; all analysis boundaries must retain
            # their exact offsets relative to it.
            expected_relative = _expected_sections(_analysis(), 0.0)
            reference_start = api_sections[0][1] - expected_relative[0][1]
            expected = _expected_sections(_analysis(), reference_start)
            if len(api_sections) != len(expected):
                _fail(f"REAPER API section count differs: {api_sections!r} != {expected!r}")
            for actual, wanted in zip(api_sections, expected):
                if actual[0] != wanted[0]:
                    _fail(f"REAPER API section name differs: {actual!r} != {wanted!r}")
                _same_time(actual[1], wanted[1], f"REAPER API section {wanted[0]!r} start")
                _same_time(actual[2], wanted[2], f"REAPER API section {wanted[0]!r} end")
            api_sections_again = [region for region in _run_reaper(project, run_dir, existing_map=False) if region[0] != USER_REGION_NAME]
            outcomes[name] = verify(project, baseline, first_snapshot=first, expect_user_region=True)
            if api_sections_again != api_sections:
                _fail("re-apply changed REAPER API section boundaries")
        return outcomes
    finally:
        # Keep a failed disposable project available for inspection until this
        # process ends, then remove it with the rest of the temporary tree.
        shutil.rmtree(root, ignore_errors=True)


def _run_plain_scripts(project: Path, run_dir: Path, scripts: list[Path]) -> None:
    """Run REAPER against `project`, executing `scripts` in order, then save and
    close -- no vgt action involved. Used to simulate a human editing the
    project by hand between two vgt applies."""
    save = run_dir / "plain-save.lua"
    save.write_text("reaper.Main_SaveProject(0, false)\nreaper.Main_OnCommand(40004, 0)\n", encoding="utf-8")
    arguments = [str(REAPER), "-newinst", str(project), *(str(script) for script in scripts), str(save)]
    subprocess.run(arguments, check=True, timeout=90)


def run_live_tempo_refresh(baseline: Path) -> dict[str, object]:
    """Prove issue #27's rule live: re-apply refreshes a vgt-created tempo map
    with newly detected/corrected tempo data while the live map is still
    exactly what vgt wrote, but the moment a human edits it by hand, vgt must
    never touch it again -- the fresh data is only ever offered non-invasively
    via the `[vgt] Beats` track from then on."""
    if not REAPER.is_file():
        raise VerificationError(f"REAPER not found at {REAPER}")
    root = Path(tempfile.mkdtemp(prefix="vgt-phase1-tempo-refresh-"))
    try:
        run_dir = root / "refresh"
        fixture_dir = run_dir / "Reaper Project"
        shutil.copytree(baseline.parent, fixture_dir)
        project = fixture_dir / baseline.name
        sidecar_path = project.with_suffix(".vgt")

        def apply_with_tempo(bpm: float, *, first_snapshot: dict[str, object] | None = None) -> dict[str, object]:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8")) if sidecar_path.is_file() else {}
            analysis = _analysis()
            analysis["tempo"]["value"]["bpm"] = bpm
            sidecar.update(schema_version=4, analysis=analysis)
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
            _run_reaper(project, run_dir, existing_map=False)
            return verify(project, baseline, first_snapshot=first_snapshot)

        # 1. Phase 0 apply: no analysis yet, so no tempo map is written.
        _run_reaper(project, run_dir, existing_map=False)
        # 2. First tempo analysis lands on a fresh (default-tempo) project:
        #    vgt writes the map and records a fingerprint for it.
        created = apply_with_tempo(100.0)
        if created["tempo_map_applied"] is not True or not created["tempo_map_fingerprint"]:
            _fail("expected a fresh vgt tempo map with a recorded fingerprint")
        if BEATS_NAME in created["managed_names"]:
            _fail("a freshly-applied tempo map must not also create a [vgt] Beats track")

        # 3. Only the detected/corrected tempo changed; the live map is still
        #    byte-for-byte what vgt wrote, so re-apply must refresh it.
        refreshed = apply_with_tempo(128.0)
        if refreshed["tempo_map_applied"] is not True or not refreshed["tempo_map_fingerprint"]:
            _fail("expected the refreshed tempo map to still be vgt-owned with a recorded fingerprint")
        if refreshed["tempo_markers"] == created["tempo_markers"]:
            _fail("re-apply did not refresh the tempo map with the corrected tempo data")
        if BEATS_NAME in refreshed["managed_names"]:
            _fail("a refreshed tempo map must not also create a [vgt] Beats track")

        # 4. A human now edits the live map by hand -- outside of vgt entirely.
        # Changing marker 0's tempo shifts any beat-attached item in that
        # first span (e.g. the reference track's own audio), which throws off
        # verify()'s reference-relative chord/section math -- that drift is
        # an expected, inherent consequence of the human's own edit, not
        # something vgt could or should compensate for. Read the raw tempo
        # markers straight off the saved RPP instead of the full verify().
        edit_script = run_dir / "user-edits-tempo.lua"
        edit_script.write_text("reaper.SetTempoTimeSigMarker(0, 0, 0, -1, -1, 155, 3, 4, false)\n", encoding="utf-8")
        _run_plain_scripts(project, run_dir, [edit_script])
        edited_markers = tuple(_TEMPO_MARKER.findall(project.read_text(encoding="utf-8", errors="replace")))
        if edited_markers == refreshed["tempo_markers"]:
            _fail("test setup failed to simulate a human edit to the tempo map")

        # 5. Re-apply with yet another tempo value: the human's edit must
        #    survive completely untouched, and the new data must only be
        #    offered non-invasively via [vgt] Beats.
        diverged = apply_with_tempo(90.0)
        if diverged["tempo_markers"] != edited_markers:
            _fail("re-apply rewrote a tempo map the user had since edited by hand")
        if diverged["tempo_map_applied"] is not True:
            _fail("a diverged tempo map must still be recorded as vgt-created")
        if diverged["tempo_map_fingerprint"]:
            _fail("a diverged tempo map must not keep claiming a current fingerprint")
        if BEATS_NAME not in diverged["managed_names"]:
            _fail("a diverged vgt tempo map must fall back to the non-invasive [vgt] Beats track")

        # 6. One more re-apply with no further live edits: the diverged,
        #    Beats-fallback state must be stable, not oscillate.
        apply_with_tempo(90.0, first_snapshot=diverged)
        return {"created": created, "refreshed": refreshed, "edited_markers": {"tempo_markers": edited_markers}, "diverged": diverged}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", nargs="?", help="saved disposable .RPP copy to inspect")
    parser.add_argument("--baseline", required=True, help="unmodified source .RPP")
    parser.add_argument("--run-live", action="store_true", help="copy the fixture and prove first apply plus re-apply in REAPER")
    parser.add_argument(
        "--run-live-tempo-refresh",
        action="store_true",
        help="copy the fixture and prove issue #27's refresh-vs-preserve tempo map rule in REAPER",
    )
    args = parser.parse_args(argv)
    try:
        baseline = locate_project(args.baseline)
        if args.run_live_tempo_refresh:
            result = run_live_tempo_refresh(baseline)
        elif args.run_live:
            result = run_live(baseline)
        else:
            result = verify(locate_project(args.project), baseline)
    except (VerificationError, ProjectError, subprocess.SubprocessError) as error:
        print(f"Phase 1 verification failed: {error}", file=sys.stderr)
        return 1
    if args.run_live or args.run_live_tempo_refresh:
        result = {name: {key: value for key, value in outcome.items() if key != "original_blocks"} for name, outcome in result.items()}
    print(json.dumps(result, indent=2, default=list))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
