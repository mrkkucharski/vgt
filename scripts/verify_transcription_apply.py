#!/usr/bin/env python3
"""Opt-in saved-project proof for ReaScript transcription MIDI import.

This intentionally starts REAPER only with ``--run-live``. It copies the real
fixture, creates committed guitar/bass WAV and MIDI artifacts, applies twice
with a save after each invocation, and inspects the serialized RPP.
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
TARGETS = {"guitar": "Guitar", "bass": "Bass"}
_ITEM = re.compile(r"^\s*<ITEM\s*$", re.MULTILINE)
_POSITION = re.compile(r"^\s*POSITION\s+([-+.\d]+)", re.MULTILINE)
_BEAT = re.compile(r"^\s*BEAT\s+0\b", re.MULTILINE)
_MUTE = re.compile(r"^\s*MUTESOLO\s+(\d+)", re.MULTILINE)
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
        output.writeframes(b"\0\0" * 8000)


def _write_midi(path: Path) -> None:
    # Header (format 0, one track, 96 ticks/quarter) plus a one-beat C4 note.
    path.write_bytes(
        b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
        b"MTrk\x00\x00\x00\x0c\x00\x90\x3c\x64\x60\x80\x3c\x00\x00\xff\x2f\x00"
    )


def _run_reaper(project: Path, run_dir: Path) -> None:
    select = run_dir / "select.lua"
    save = run_dir / "save.lua"
    select.write_text('reaper.SetExtState("vgt", "reference_index", "0", false)\n', encoding="utf-8")
    save.write_text("reaper.Main_SaveProject(0, false)\nreaper.Main_OnCommand(40004, 0)\n", encoding="utf-8")
    subprocess.run([str(REAPER), "-newinst", str(project), str(select), str(APPLY), str(save)], check=True, timeout=90)


def verify(project: Path, baseline: Path, *, previous: tuple[str, ...] | None = None) -> tuple[str, ...]:
    parsed, original = read_project(project), read_project(baseline)
    if tuple((t.name, t.guid) for t in parsed.tracks[: len(original.tracks)]) != tuple((t.name, t.guid) for t in original.tracks):
        raise VerificationError("user tracks were changed")
    text = project.read_text(encoding="utf-8", errors="replace")
    blocks = dict(_track_blocks(text))
    user_snapshot = tuple(blocks[t.guid] for t in original.tracks)
    if previous is not None and user_snapshot != previous:
        raise VerificationError("re-apply changed a user track")
    sidecar = json.loads(project.with_suffix(".vgt").read_text(encoding="utf-8"))
    managed = sidecar.get("managed_track_guids")
    if not isinstance(managed, list) or len(managed) != 5 or len(set(managed)) != 5:
        raise VerificationError("sidecar does not record exactly folder, stems, and MIDI references")
    names = [track.name for track in parsed.tracks]
    reference_guid = sidecar["config"]["reference_track_guid"]
    reference_start = float(_POSITION.search(_blocks(blocks[reference_guid], _ITEM)[0]).group(1))
    midi_tracks = [track for track in parsed.tracks if track.name.endswith(" Ref (MIDI)") and track.name.startswith(PREFIX)]
    if len(midi_tracks) != 2:
        raise VerificationError("expected exactly two vgt MIDI reference tracks")
    for target, label in TARGETS.items():
        stem_name, midi_name = f"{PREFIX} {label}", f"{PREFIX} {label} Ref (MIDI)"
        if stem_name not in names or midi_name not in names or names.index(midi_name) != names.index(stem_name) + 1:
            raise VerificationError(f"{label} MIDI track does not immediately follow its stem")
        track = next(track for track in parsed.tracks if track.name == midi_name)
        if track.guid not in managed:
            raise VerificationError(f"{label} MIDI track is not managed")
        block = blocks[track.guid]
        if "<SOURCE MIDI" not in block:
            raise VerificationError(f"{label} reference track did not serialize a MIDI source")
        items = _blocks(block, _ITEM)
        if len(items) != 1 or not _BEAT.search(items[0]):
            raise VerificationError(f"{label} MIDI item is not time-based")
        if abs(float(_POSITION.search(items[0]).group(1)) - reference_start) > 0.00001:
            raise VerificationError(f"{label} MIDI item is not aligned to the reference")
        mute = _MUTE.search(block)
        if mute and int(mute.group(1)) != 0:
            raise VerificationError(f"{label} MIDI track is muted")
        expected = f"vgt/transcription-proof/transcription/{target}.mid"
        files = _FILE.findall(block)
        if files != [expected] or Path(files[0]).is_absolute():
            raise VerificationError(f"{label} MIDI path did not serialize project-relative: {files!r}")
    return user_snapshot


def run_live(baseline: Path) -> None:
    if not REAPER.is_file():
        raise VerificationError(f"REAPER not found at {REAPER}")
    root = Path(tempfile.mkdtemp(prefix="vgt-transcription-"))
    try:
        project_dir = root / "Reaper Project"
        shutil.copytree(baseline.parent, project_dir)
        project = project_dir / baseline.name
        _run_reaper(project, root)  # establish the sidecar
        sidecar_path = project.with_suffix(".vgt")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        namespace = project.parent / "vgt" / "transcription-proof"
        midi_dir = namespace / "transcription"
        midi_dir.mkdir(parents=True)
        artifacts: dict[str, dict[str, object]] = {}
        for target in TARGETS:
            stem = namespace / "stems" / f"{target}.wav"
            stem.parent.mkdir(parents=True, exist_ok=True)
            _write_wav(stem)
            _write_midi(midi_dir / f"{target}.mid")
            artifacts[target] = {"file": f"vgt/transcription-proof/stems/{target}.wav", "size_bytes": stem.stat().st_size, "duration_seconds": 1.0}
        sidecar.setdefault("analysis", {})["stems"] = {"artifact_namespace": "transcription-proof", "artifacts": artifacts}
        sidecar["analysis"]["transcription"] = {"requested_targets": list(TARGETS), "targets": {target: {"status": "transcribed", "midi_file": f"transcription/{target}.mid"} for target in TARGETS}}
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        _run_reaper(project, root)
        snapshot = verify(project, baseline)
        _run_reaper(project, root)
        verify(project, baseline, previous=snapshot)
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
        print(f"Transcription apply verification failed: {error}", file=sys.stderr)
        return 1
    print("Transcription apply verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
