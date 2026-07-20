#!/usr/bin/env python3
"""Run a disposable live-REAPER proof of the Phase 1 correction workflow.

``--run-live`` copies the real fixture, applies deliberately distinct machine
baselines, edits one chord item and one vgt-owned section region through
REAPER's API, runs ``vgt_sync.lua``, saves, and verifies the resulting sidecar.
The fixture itself is never opened for writing.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vgt.project import ProjectError, locate_project  # noqa: E402


REAPER = Path("/Applications/REAPER.app/Contents/MacOS/REAPER")
APPLY = ROOT / "reascript" / "vgt_initialize.lua"
SYNC = ROOT / "reascript" / "vgt_sync.lua"
CHORD_LABEL = "C# minor live correction"
SECTION_LABEL = "live-verified bridge"
CHORD_START_DELTA = 0.5
CHORD_LENGTH = 3.25
SECTION_DELTA = 1.0


class VerificationError(ValueError):
    """The disposable project did not complete the expected live workflow."""


def _analysis() -> dict[str, object]:
    """Small, deterministic machine results that make changed values obvious."""
    chords = {
        "segments": [
            {"chord": "Am", "start_seconds": 0.0, "end_seconds": 2.4},
            {"chord": "F", "start_seconds": 2.4, "end_seconds": 4.8},
        ]
    }
    sections = [
        {"label": "intro", "start_seconds": 0.0, "end_seconds": 8.0},
        {"label": "verse", "start_seconds": 8.0, "end_seconds": 16.0},
    ]
    return {
        "tempo": {"value": {"bpm": 100.0, "downbeat_offset_seconds": 0.0, "time_signature": "4/4", "mode": "constant"}},
        "sections": {"value": copy.deepcopy(sections), "detected": copy.deepcopy(sections), "human_verified": False},
        "chords": {"value": copy.deepcopy(chords), "detected": copy.deepcopy(chords), "human_verified": False},
    }


def _write_script(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _run_reaper(project: Path, scripts: list[Path], completion: Path) -> None:
    """Run startup actions until the final script has saved the project.

    REAPER deliberately remains open after a command-line action sequence on
    current macOS builds.  A completion file lets the verifier wait for the
    save, then terminate only the disposable REAPER process instead of
    depending on a GUI-only quit action.
    """
    # REAPER accepts one startup ReaScript.  Chain the small setup/edit/save
    # scripts explicitly so their order is part of the proof rather than an
    # accident of command-line argument handling.
    driver = completion.with_suffix(".lua")
    _write_script(driver, "".join(f"dofile({str(script)!r})\n" for script in scripts))
    process = subprocess.Popen([str(REAPER), "-newinst", "-nosplash", str(project), str(driver)])
    try:
        deadline = time.monotonic() + 90
        while not completion.exists():
            if process.poll() is not None:
                raise VerificationError(f"REAPER exited before saving the project (status {process.returncode})")
            if time.monotonic() >= deadline:
                raise VerificationError("REAPER did not finish its action sequence within 90 seconds")
            time.sleep(0.1)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def _save_script(path: Path, completion: Path) -> None:
    _write_script(
        path,
        "reaper.Main_SaveProject(0, false)\n"
        + "local done = assert(io.open(" + repr(str(completion)) + ", 'w'))\n"
        + "done:write('saved')\ndone:close()\n",
    )


def _assert_corrected_sidecar(path: Path, baseline_analysis: dict[str, object]) -> dict[str, object]:
    try:
        sidecar = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot read a valid sidecar: {error}") from error
    analysis = sidecar.get("analysis")
    if not isinstance(analysis, dict):
        raise VerificationError("sidecar lost its analysis block")

    for stage_name in ("chords", "sections"):
        stage = analysis.get(stage_name)
        if not isinstance(stage, dict) or stage.get("human_verified") is not True:
            raise VerificationError(f"{stage_name} was not marked human_verified")
        if not isinstance(stage.get("verified_at"), str):
            raise VerificationError(f"{stage_name} has no verification timestamp")
        if stage.get("detected") != baseline_analysis[stage_name]["detected"]:
            raise VerificationError(f"vgt sync changed the {stage_name} machine-detected baseline")

    segments = analysis["chords"].get("value", {}).get("segments")
    if not isinstance(segments, list) or not segments:
        raise VerificationError("vgt sync did not write corrected chord segments")
    chord = segments[0]
    if chord != {"start_seconds": CHORD_START_DELTA, "end_seconds": CHORD_START_DELTA + CHORD_LENGTH, "chord": CHORD_LABEL}:
        raise VerificationError(f"corrected chord differs from the live edit: {chord!r}")

    sections = analysis["sections"].get("value")
    if not isinstance(sections, list) or not sections:
        raise VerificationError("vgt sync did not write corrected sections")
    section = sections[0]
    expected_section = {"start_seconds": SECTION_DELTA, "end_seconds": 8.0 + SECTION_DELTA, "label": SECTION_LABEL}
    if section != expected_section:
        raise VerificationError(f"corrected section differs from the live edit: {section!r}")
    return sidecar


def run_live(baseline: Path) -> dict[str, object]:
    if not REAPER.is_file():
        raise VerificationError(f"REAPER not found at {REAPER}")
    root = Path(tempfile.mkdtemp(prefix="vgt-phase1-sync-"))
    try:
        fixture_dir = root / "Reaper Project"
        shutil.copytree(baseline.parent, fixture_dir)
        project = fixture_dir / baseline.name
        sidecar_path = project.with_suffix(".vgt")

        select = root / "select-reference.lua"
        _write_script(select, 'reaper.SetExtState("vgt", "reference_index", "1", false)\n')
        save = root / "save.lua"
        first_done = root / "first-saved"
        _save_script(save, first_done)
        # Establish Phase 0 state before placing analysis into its sidecar.
        _run_reaper(project, [select, APPLY, save], first_done)

        analysis = _analysis()
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar.update(schema_version=7, analysis=analysis)
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

        # Re-apply the analysis, then use REAPER's own item/region APIs to
        # make the two manual corrections before invoking the sync action.
        edit = root / "edit-corrections.lua"
        _write_script(
            edit,
            "local chords\n"
            "for i = 0, reaper.CountTracks(0) - 1 do\n"
            "  local track = reaper.GetTrack(0, i)\n"
            "  local _, name = reaper.GetTrackName(track, '')\n"
            "  if name == '[vgt] Chords' then chords = track break end\n"
            "end\n"
            "assert(chords, 'missing [vgt] Chords track')\n"
            "local item = reaper.GetTrackMediaItem(chords, 0)\n"
            "assert(item, 'missing first chord item')\n"
            f"reaper.SetMediaItemInfo_Value(item, 'D_POSITION', reaper.GetMediaItemInfo_Value(item, 'D_POSITION') + {CHORD_START_DELTA})\n"
            f"reaper.SetMediaItemInfo_Value(item, 'D_LENGTH', {CHORD_LENGTH})\n"
            "local take = assert(reaper.GetActiveTake(item), 'missing chord take')\n"
            f"reaper.GetSetMediaItemTakeInfo_String(take, 'P_NAME', '{CHORD_LABEL}', true)\n"
            "local edited_region = false\n"
            "for i = 0, reaper.CountProjectMarkers(0) - 1 do\n"
            "  local _, is_region, start_time, end_time, name, region_id = reaper.EnumProjectMarkers3(0, i)\n"
            "  if is_region and name == '[vgt] intro' then\n"
            f"    reaper.SetProjectMarker3(0, region_id, true, start_time + {SECTION_DELTA}, end_time + {SECTION_DELTA}, '[vgt] {SECTION_LABEL}', 0)\n"
            "    edited_region = true\n"
            "    break\n"
            "  end\n"
            "end\n"
            "assert(edited_region, 'missing [vgt] intro region')\n",
        )
        sync_done = root / "sync-saved"
        _save_script(save, sync_done)
        _run_reaper(project, [select, APPLY, edit, SYNC, save], sync_done)
        result = _assert_corrected_sidecar(sidecar_path, analysis)
        return {"chord": result["analysis"]["chords"]["value"]["segments"][0], "section": result["analysis"]["sections"]["value"][0]}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true", help="copy the fixture and prove vgt sync in REAPER")
    parser.add_argument("--baseline", default=str(ROOT / "test" / "Reaper Project" / "Reaper Project.RPP"))
    args = parser.parse_args(argv)
    if not args.run_live:
        parser.error("--run-live is required; this verifier intentionally has no synthetic proof mode")
    try:
        result = run_live(locate_project(args.baseline))
    except (VerificationError, ProjectError, subprocess.SubprocessError, OSError, json.JSONDecodeError) as error:
        print(f"Phase 1 sync verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
