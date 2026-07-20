#!/usr/bin/env python3
"""Run and verify the saved-project proof for ReaScript stem import.

The proof is deliberately opt-in because it starts REAPER.  It copies the
real fixture, writes six short valid WAVs into the recorded artifact namespace,
applies the action twice, saves after each apply, and inspects the resulting
RPP rather than assuming an API input path dictates serialization.
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
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vgt.project import _track_blocks, locate_project, read_project  # noqa: E402


REAPER = Path("/Applications/REAPER.app/Contents/MacOS/REAPER")
APPLY = ROOT / "reascript" / "vgt_initialize.lua"
PREFIX = "[vgt]"
STEMS = {
    "vocals": ("Vocals", "vocals.wav"),
    "instrumental": ("Instrumental", "instrumental.wav"),
    "bass": ("Bass", "bass.wav"),
    "drums": ("Drums", "drums.wav"),
    "guitar": ("Guitar", "guitar.wav"),
    "backing": ("Backing (no guitar)", "backing-no-guitar.wav"),
}
_ITEM = re.compile(r"^\s*<ITEM\s*$", re.MULTILINE)
_POSITION = re.compile(r"^\s*POSITION\s+([-+.\d]+)", re.MULTILINE)
_BEAT = re.compile(r"^\s*BEAT\s+0\b", re.MULTILINE)
_FILE = re.compile(r'^\s*FILE\s+"(?P<path>.*)"', re.MULTILINE)


class VerificationError(ValueError):
    pass


def _blocks(text: str, start: re.Pattern[str]) -> list[str]:
    result: list[str] = []
    for match in start.finditer(text):
        depth = 0
        for line in re.finditer(r".*(?:\n|$)", text[match.start() :]):
            value = line.group().strip()
            if value.startswith("<"):
                depth += 1
            elif value == ">":
                depth -= 1
                if depth == 0:
                    result.append(text[match.start() : match.start() + line.end()])
                    break
    return result


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)  # one second


def _run_reaper(project: Path, run_dir: Path, *, add_region: bool = False) -> None:
    select = run_dir / "select.lua"
    save = run_dir / "save.lua"
    select.write_text('reaper.SetExtState("vgt", "reference_index", "1", false)\n', encoding="utf-8")
    save.write_text("reaper.Main_SaveProject(0, false)\nreaper.Main_OnCommand(40004, 0)\n", encoding="utf-8")
    arguments = [str(REAPER), "-newinst", str(project)]
    if add_region:
        region = run_dir / "user-region.lua"
        region.write_text('reaper.AddProjectMarker2(0, true, 20, 24, "User region", -1, 0)\n', encoding="utf-8")
        arguments.append(str(region))
    arguments.extend((str(select), str(APPLY), str(save)))
    subprocess.run(arguments, check=True, timeout=90)


def verify(project: Path, baseline: Path, *, prior_snapshot: tuple[str, ...] | None = None) -> tuple[str, ...]:
    parsed = read_project(project)
    original = read_project(baseline)
    if tuple((track.name, track.guid) for track in parsed.tracks[: len(original.tracks)]) != tuple((track.name, track.guid) for track in original.tracks):
        raise VerificationError("user tracks were changed")
    text = project.read_text(encoding="utf-8", errors="replace")
    blocks = dict(_track_blocks(text))
    user_blocks = tuple(blocks[track.guid] for track in original.tracks)
    if prior_snapshot is not None and user_blocks != prior_snapshot:
        raise VerificationError("re-apply changed a user track")
    if 'MARKER ' not in text or '"User region"' not in text:
        raise VerificationError("user region was changed")
    sidecar = json.loads(project.with_suffix(".vgt").read_text(encoding="utf-8"))
    managed = sidecar.get("managed_track_guids")
    if not isinstance(managed, list) or len(managed) != 8 or len(set(managed)) != 8:
        raise VerificationError("sidecar does not track exactly folder, mirror, and six stems")
    by_name = {track.name: track.guid for track in parsed.tracks}
    reference_guid = sidecar["config"]["reference_track_guid"]
    reference_items = _blocks(blocks[reference_guid], _ITEM)
    reference_start = float(_POSITION.search(reference_items[0]).group(1))
    for artifact, (label, filename) in STEMS.items():
        guid = by_name.get(f"{PREFIX} {label}")
        if guid is None or guid not in managed:
            raise VerificationError(f"missing managed stem track {label}")
        item = _blocks(blocks[guid], _ITEM)
        if len(item) != 1 or not _BEAT.search(item[0]):
            raise VerificationError(f"{label} is not a single time-based item")
        if abs(float(_POSITION.search(item[0]).group(1)) - reference_start) > 0.00001:
            raise VerificationError(f"{label} was not offset-shifted to the reference")
        files = _FILE.findall(blocks[guid])
        expected = f"vgt/stem-proof/stems/{filename}"
        if files != [expected] or Path(files[0]).is_absolute():
            raise VerificationError(f"{label} did not serialize as project-relative {expected!r}: {files!r}")
    return user_blocks


def run_live(baseline: Path) -> None:
    if not REAPER.is_file():
        raise VerificationError(f"REAPER not found at {REAPER}")
    root = Path(tempfile.mkdtemp(prefix="vgt-stems-"))
    try:
        project_dir = root / "Reaper Project"
        shutil.copytree(baseline.parent, project_dir)
        project = project_dir / baseline.name
        _run_reaper(project, root, add_region=True)  # establishes the Phase 0 sidecar
        sidecar_path = project.with_suffix(".vgt")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        stem_dir = project.parent / "vgt" / "stem-proof" / "stems"
        stem_dir.mkdir(parents=True)
        artifacts = {}
        for name, (_, filename) in STEMS.items():
            _write_wav(stem_dir / filename)
            artifacts[name] = {"file": f"stems/{filename}", "size_bytes": (stem_dir / filename).stat().st_size, "duration_seconds": 1.0}
        sidecar["schema_version"] = 6
        sidecar.setdefault("analysis", {})["stems"] = {"artifact_namespace": "stem-proof", "artifacts": artifacts}
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        _run_reaper(project, root)
        snapshot = verify(project, baseline)
        _run_reaper(project, root)
        verify(project, baseline, prior_snapshot=snapshot)
    finally:
        shutil.rmtree(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--baseline", default=str(ROOT / "test" / "Reaper Project" / "Reaper Project.RPP"))
    args = parser.parse_args(argv)
    if not args.run_live:
        parser.error("--run-live is required; this verifier intentionally has no synthetic proof mode")
    try:
        run_live(locate_project(args.baseline))
    except (VerificationError, subprocess.SubprocessError, OSError, json.JSONDecodeError) as error:
        print(f"Stem apply verification failed: {error}", file=sys.stderr)
        return 1
    print("Stem apply verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
