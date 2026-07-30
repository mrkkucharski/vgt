"""Offline executable acceptance contract for the permanent invariants in
`docs/AGENTS.md`.

This is deliberately one workflow, not a replacement for focused unit tests.
It uses the real RPP fixture, deterministic analysis/separation/transcription
seams, and a small in-memory REAPER API implementation executed by Lua.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable

import pytest

from vgt import analysis as analysis_module
from vgt.analysis import analyze
from vgt.cli import main
from vgt.separation import FakeSeparator, separate
from vgt.sidecar import artifact_namespace_dir, read_sidecar, update_analysis
from vgt.transcribe import (
    DRUMSCRIPT_INSTRUMENTS,
    DrumScriptSpec,
    FakeAdtofTranscriber,
    FakeTranscriber,
    TargetTranscriberRouter,
    TranscriptionResult,
    _fitted_beat_period_s,
    _write_midi,
)
from vgt.drum_grid import reconcile_event_times
from vgt.transcription_lifecycle import add_variant, discard_variant, purge_discarded


ROOT = Path(__file__).parents[1]
FIXTURE_DIR = ROOT / "test" / "Reaper Project"
APPLY_SCRIPT = ROOT / "reascript" / "vgt_initialize.lua"
SYNC_SCRIPT = ROOT / "reascript" / "vgt_sync.lua"
TEMPO_SYNC_SCRIPT = ROOT / "reascript" / "vgt_sync_tempo_map.lua"
WORKING_COPY_SCRIPT = ROOT / "reascript" / "vgt_working_copy.lua"
LUA = __import__("os").environ.get("VGT_TEST_LUA", "lua")
REFERENCE_GUID = "{75418143-1F31-B548-B7D2-96815CB0297D}"


class CountingSeparator(FakeSeparator):
    """Make the paid-operation cache observable without changing its seam."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def split(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().split(*args, **kwargs)


class CountingTranscriber(FakeTranscriber):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def transcribe(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().transcribe(*args, **kwargs)

    def detect_raw(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        """Expose raw Basic Pitch work separately from legacy transcribe()."""
        self.raw_calls = getattr(self, "raw_calls", 0) + 1
        return super().detect_raw(*args, **kwargs)


class CountingPyinTranscriber(CountingTranscriber):
    """Deterministic pYIN seam with independently observable raw detection."""

    name = "pyin"


class TempoSkewedDrumScriptFake(FakeTranscriber):
    """A long DrumScript grid with the observed 7Rivers failure signature.

    The backend rounds played eighths onto its own zero-anchored clock.  Its
    small rate error eventually makes backend slot numbering run ahead of the
    project even though every reported onset is nearest to the played project
    line.  This fake keeps the goal contract offline while using FakeTranscriber's
    production-shaped reconciliation seam rather than pre-authoring MIDI.
    """

    name = "drumscript"
    backend_tempo = 60.1
    project_downbeat_s = 0.085333
    project_step_s = 30.0 / 120.004
    backend_step_s = 0.249615
    event_count = 640
    event_time_s = 150.0
    source_interval_s = 160.0

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def transcribe(
        self,
        source: Path,
        destination_dir: Path,
        spec: DrumScriptSpec,
        progress: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        self.calls += 1
        destination_dir.mkdir(parents=True, exist_ok=True)
        midi_path = destination_dir / "transcription.mid"
        events_path = destination_dir / "transcription.json"
        if spec.beat_grid is None:
            # Other goal-contract coverage intentionally models a synchronized
            # tempo map without a detected beat list. Keep that independent
            # scenario small; the long grid is specific to this regression.
            events = [{"time_sec": self.event_time_s, "instruments": ["kick"]}]
            note_ends = [self.source_interval_s]
        else:
            # These are played project-grid times observed by a backend that
            # quantizes them to its own clock.  At the tail its index differs
            # from the project index by a whole eighth -- precisely the
            # regression that nearest-line matching fixes.
            played = [self.project_downbeat_s + index * self.project_step_s for index in range(self.event_count)]
            raw_events = [
                {"time_sec": round(time / self.backend_step_s) * self.backend_step_s, "instruments": ["kick"]}
                for time in played
            ]
            events, report = reconcile_event_times(
                raw_events, beat_grid=spec.beat_grid, beat_period_s=_fitted_beat_period_s(spec)
            )
            assert report is not None, "the contract fake requires a usable analyzed grid"
            note_ends = [event["time_sec"] + 0.1 for event in events]
        _write_midi(
            midi_path,
            [(event["time_sec"], end, DRUMSCRIPT_INSTRUMENTS["kick"], 100) for event, end in zip(events, note_ends)],
            spec.midi_tempo or 120.0,
            channel=9,
            tempo_map=spec.tempo_map,
        )
        events_path.write_text(json.dumps(events), encoding="utf-8")
        return TranscriptionResult(
            note_count=len(events),
            pitch_range_midi=None,
            first_note_s=None,
            last_note_s=None,
            midi_path=midi_path,
            events_path=events_path,
            instrument_counts={"kick": len(events)},
            event_count=len(events),
            first_event_s=events[0]["time_sec"],
            last_event_s=events[-1]["time_sec"],
            backend_tempo=self.backend_tempo,
            midi_tempo=spec.midi_tempo,
        )


class OfflineLalalLedger:
    """Observe CLI-paid work while keeping the acceptance test fully offline."""

    def __init__(self, *, fail_stems_once: set[str] | None = None) -> None:
        self.backends: list[OfflineLalalSeparator] = []
        self.charged_stems: list[str] = []
        self.resumed_stems: list[str] = []
        self.quoted_operation_counts: list[int] = []
        self.fail_stems_once = set(fail_stems_once or ())

    def make_backend(self) -> "OfflineLalalSeparator":
        backend = OfflineLalalSeparator(self)
        self.backends.append(backend)
        return backend


class OfflineLalalSeparator(FakeSeparator):
    """LALAL-shaped fake with a durable paid-task ledger.

    A task id already in ``resume_state`` represents a task paid for by a
    previous process.  The fake then completes it without adding another
    charge, exactly like the real seam's resume path.
    """

    name = "lalal"
    api_version = "offline-contract-v1"

    def __init__(self, ledger: OfflineLalalLedger) -> None:
        super().__init__()
        self.ledger = ledger

    def __enter__(self) -> "OfflineLalalSeparator":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def preflight(
        self,
        *,
        sources: list[tuple[Path, int]],
        source_states: list[dict[str, Any]],
        checkpoint: Any,
    ) -> list[dict[str, Any]]:
        self.ledger.quoted_operation_counts.append(sum(count for _source, count in sources))
        states = [
            {
                "source_id": state.get("source_id") or f"offline-upload-{index}",
                "source_expires": 4_102_444_800,
                "source_duration_seconds": 1.0,
            }
            for index, state in enumerate(source_states, start=1)
        ]
        for index, state in enumerate(states):
            checkpoint(index, state)
        return states

    def split(self, source: Path, out_dir: Path, spec: Any, *, resume_state: dict[str, Any] | None, checkpoint: Any):
        state = resume_state or {}
        if state.get("task_id"):
            self.ledger.resumed_stems.append(spec.stem)
        else:
            self.ledger.charged_stems.append(spec.stem)
            if spec.stem in self.ledger.fail_stems_once:
                self.ledger.fail_stems_once.remove(spec.stem)
                checkpoint(
                    {
                        "idempotency_key": f"offline-key-{spec.stem}",
                        "task_id": f"offline-task-{spec.stem}",
                        "status": "submitted",
                    }
                )
                raise RuntimeError("offline interruption after paid-task checkpoint")
        return super().split(source, out_dir, spec, resume_state=resume_state, checkpoint=checkpoint)


def _copy_project(tmp_path: Path) -> Path:
    target = tmp_path / "Reaper Project"
    shutil.copytree(FIXTURE_DIR, target)
    return target / "Reaper Project.RPP"


@pytest.fixture
def deterministic_detectors(monkeypatch: pytest.MonkeyPatch):
    """Small fixture artifacts keep this acceptance contract offline and fast."""
    def tempo(project: Path, _source: Path, _settings: dict, _analysis: dict, namespace: str, **_kwargs: object) -> dict:
        click = artifact_namespace_dir(project, namespace) / "tempo-click.wav"
        click.parent.mkdir(parents=True, exist_ok=True)
        click.write_bytes(b"RIFFxxxxWAVEfmt ")
        return {"bpm": 120.0, "time_signature": "4/4", "mode": "constant", "backend": "contract", "beat_times": [0.0, 1.0, 2.0], "click_artifact_path": click.name}

    def key(*_args: object, **_kwargs: object) -> dict:
        return {"root": "C", "scale": "major", "confidence": 1.0, "backend": "contract"}

    def sections(project: Path, _source: Path, _settings: dict, _analysis: dict, namespace: str, **_kwargs: object) -> list[dict]:
        timeline = artifact_namespace_dir(project, namespace) / "sections.txt"
        timeline.parent.mkdir(parents=True, exist_ok=True)
        timeline.write_text("0\t1\tVerse\n1\t2\tChorus\n", encoding="utf-8")
        return [{"index": 0, "label": "Verse", "start_seconds": 0.0, "end_seconds": 1.0}, {"index": 1, "label": "Chorus", "start_seconds": 1.0, "end_seconds": 2.0}]

    def chords(_source: Path, beats: list[float], _settings: dict, **_kwargs: object) -> dict:
        return {"segments": [{"start_seconds": beats[0], "end_seconds": beats[1], "chord": "C:maj"}, {"start_seconds": beats[1], "end_seconds": beats[2], "chord": "G:maj"}], "vocabulary": "maj_min", "backend": "contract", "beat_times": beats}

    monkeypatch.setitem(analysis_module._DETECTORS, "tempo", tempo)
    monkeypatch.setitem(analysis_module._DETECTORS, "key", key)
    monkeypatch.setitem(analysis_module._DETECTORS, "sections", sections)
    monkeypatch.setattr(analysis_module, "_detect_chords", chords)
    monkeypatch.setattr(analysis_module, "_tempo_beat_times", lambda *_args: [0.0, 1.0, 2.0])


def _lua_state(project: Path) -> str:
    """A serializable, in-memory REAPER project for the whole contract workflow."""
    return f"""
local state = {{tracks={{
  {{guid='{{CLICK}}', name='Click', B_MUTE=0, items={{{{position=0,length=1,C_LOCK=0,take={{name='Count in',source=''}}}}}}}},
  {{guid='{REFERENCE_GUID}', name='The Seven Rivers (Full March - 3_00)', B_MUTE=0, items={{{{position=10,length=4,C_LOCK=0,take={{name='Original mix',source='Media/The Seven Rivers (Full March - 3_00).mp3'}}}}}}}},
  {{guid='{{PARIS}}', name='Paris Metro Punk', B_MUTE=1, items={{{{position=2,length=3,C_LOCK=1,take={{name='Paris source',source='Media/Paris Metro Punk.mp3'}}}}}}}}
}},regions={{{{id=900,start=11,finish=12,name='User region',color=42}}}},markers={{{{time=0,bpm=100,num=4,den=4}}}},next_guid=1,next_region=1000,tempo_writes=0,proj_ext={{}}}}
local tracks, regions, markers = state.tracks, state.regions, state.markers
local next_guid, next_region, tempo_writes, proj_ext = state.next_guid, state.next_region, state.tempo_writes, state.proj_ext
reaper = {{}}
function reaper.EnumProjects() return true, arg[1] end
function reaper.CountTracks() return #tracks end
function reaper.GetTrack(_, i) return tracks[i + 1] end
function reaper.GetTrackName(t) return true, t.name end
function reaper.GetTrackGUID(t) return t.guid end
function reaper.SetTrackSelected(t, selected) t.selected = selected and true or nil end
function reaper.CountSelectedTracks()
  local count = 0; for _, t in ipairs(tracks) do if t.selected then count = count + 1 end end; return count
end
function reaper.GetSelectedTrack(_, index)
  local selected_index = 0
  for _, t in ipairs(tracks) do
    if t.selected then
      if selected_index == index then return t end
      selected_index = selected_index + 1
    end
  end
  return nil
end
function reaper.ReorderSelectedTracks(before_index, _make_prev_folder)
  local selected, remaining = {{}}, {{}}
  local selected_before = 0
  for index, t in ipairs(tracks) do
    if t.selected then
      selected[#selected + 1] = t
      if index - 1 < before_index then selected_before = selected_before + 1 end
    else
      remaining[#remaining + 1] = t
    end
  end
  -- REAPER's target index is expressed in the pre-move project. Removing
  -- selected tracks above it shifts the insertion point, while the selected
  -- objects themselves (including I_FOLDERDEPTH) move unchanged and retain
  -- their project order.
  local insert_at = math.max(0, math.min(#remaining, before_index - selected_before))
  for offset, track in ipairs(selected) do table.insert(remaining, insert_at + offset, track) end
  tracks = remaining
end
function reaper.CountTrackMediaItems(t) return #t.items end
function reaper.GetTrackMediaItem(t, i) return t.items[i + 1] end
function reaper.GetActiveTake(i) return i.take end
function reaper.GetMediaItemTake_Source(t) return t.source end
function reaper.GetMediaSourceFileName(s) return s end
function reaper.GetMediaItemInfo_Value(i, key) return key == 'D_POSITION' and i.position or i.length end
function reaper.GetTakeName(t) return t.name end
function reaper.InsertTrackAtIndex(i) table.insert(tracks, i + 1, {{guid=string.format('{{00000000-0000-0000-0000-%012d}}', next_guid),name='',items={{}}}}); next_guid=next_guid+1 end
function reaper.DeleteTrack(t) for i,v in ipairs(tracks) do if v == t then table.remove(tracks,i); return end end end
function reaper.GetSetMediaTrackInfo_String(t, key, value, set)
  if set then
    if key == 'P_NAME' then t.name = value end
    t.ext = t.ext or {{}}
    t.ext[key] = value
    return
  end
  if key == 'P_NAME' then return true, t.name end
  return true, (t.ext and t.ext[key]) or ''
end
function reaper.GetMediaTrackInfo_Value(t, key) return t[key] or 0 end
function reaper.SetMediaTrackInfo_Value(t, key, value) t[key]=value end
function reaper.ColorToNative(red, green, blue) return blue * 65536 + green * 256 + red end
function reaper.AddMediaItemToTrack(t) local i={{position=0,length=0}}; table.insert(t.items,i); return i end
function reaper.SetMediaItemInfo_Value(i,key,value) if key == 'D_POSITION' then i.position=value elseif key == 'D_LENGTH' then i.length=value else i[key]=value end end
function reaper.GetSetMediaItemInfo_String(i,_,value) i.notes=value end
function reaper.AddTakeToMediaItem(i) i.take={{}}; return i.take end
function reaper.GetSetMediaItemTakeInfo_String(t,_,value) t.name=value end
function reaper.SetMediaItemTake_Source(t,s) t.source=s end
function reaper.PCM_Source_CreateFromFile(path) return {{path=path}} end
function reaper.GetMediaSourceLength(_) return _G.midi_source_length or 1 end
function reaper.CountProjectMarkers() return #regions end
function reaper.EnumProjectMarkers3(_,i) local r=regions[i+1]; return true,true,r.start,r.finish,r.name,r.id,0 end
function reaper.DeleteProjectMarker(_,id) for i,r in ipairs(regions) do if r.id==id then table.remove(regions,i); return end end end
function reaper.AddProjectMarker2(_,_,start,finish,name) local id=next_region; next_region=next_region+1; table.insert(regions,{{id=id,start=start,finish=finish,name=name}}); return id end
function reaper.SetProjExtState(_,section,key,value) proj_ext[section .. ':' .. key] = value end
function reaper.GetProjExtState(_,section,key) local value = proj_ext[section .. ':' .. key]; return value and 1 or 0, value or '' end
function reaper.CountTempoTimeSigMarkers() return #markers end
function reaper.GetTempoTimeSigMarker(_,i) local m=markers[i+1]; return true,m.time,0,0,m.bpm,m.num,m.den end
function reaper.SetTempoTimeSigMarker(_,_,time,_,_,bpm,num,den) tempo_writes=tempo_writes+1; markers={{{{time=time,bpm=bpm,num=num,den=den}}}} end
function reaper.DeleteTempoTimeSigMarker(_,i) table.remove(markers,i+1) end
function reaper.TimeMap_GetTimeSigAtTime() return 4,4,100 end
function reaper.GetExtState(_,key) return key == 'reference_index' and '0' or 'electric' end
function reaper.Undo_BeginBlock() end
function reaper.Undo_EndBlock() end
function reaper.PreventUIRefresh() end
function reaper.MarkProjectDirty() end
function reaper.UpdateArrange() end
function reaper.TrackList_AdjustWindows() end
function reaper.ShowConsoleMsg() end
function reaper.ShowMessageBox(msg) error(msg) end
function report()
  local names, user_items, vgt = {{}}, 0, 0
  for _,t in ipairs(tracks) do names[#names+1]=t.name; if t.name:sub(1,5)=='[vgt]' then vgt=vgt+1 end; if t.name=='The Seven Rivers (Full March - 3_00)' or t.name=='Paris Metro Punk' then user_items=user_items+#t.items end end
  io.write(table.concat(names,'|') .. '#' .. user_items .. '#' .. #regions .. '#' .. vgt .. '#' .. tempo_writes)
end
function lua_value(value)
  if type(value) == 'string' then return string.format('%q', value) end
  if type(value) == 'number' or type(value) == 'boolean' then return tostring(value) end
  local keys = {{}}
  for key in pairs(value) do keys[#keys + 1] = key end
  table.sort(keys, function(a,b) return tostring(a) < tostring(b) end)
  local parts = {{}}
  for _, key in ipairs(keys) do parts[#parts + 1] = '[' .. lua_value(key) .. ']=' .. lua_value(value[key]) end
  return '{{' .. table.concat(parts, ',') .. '}}'
end
function user_snapshot(managed_track_guids, managed_region_ids)
  local managed_tracks, managed_regions = {{}}, {{}}
  for _, guid in ipairs(managed_track_guids or {{}}) do managed_tracks[guid] = true end
  for _, id in ipairs(managed_region_ids or {{}}) do managed_regions[id] = true end
  local user_tracks, user_regions = {{}}, {{}}
  -- Filter by the immutable ownership records, rather than mutable labels or
  -- a fixture-specific ID.  A bug that renames or changes the GUID of a user
  -- object must therefore appear in the snapshot comparison.
  for _, track in ipairs(tracks) do
    -- Key by immutable GUID rather than retaining project-array position.
    -- Containers deliberately stay in this snapshot: their children are
    -- user-owned, and a promotion bug that changes one must remain visible.
    if not managed_tracks[track.guid] then user_tracks[track.guid] = track end
  end
  for _, region in ipairs(regions) do if not managed_regions[region.id] then user_regions[#user_regions + 1] = region end end
  return lua_value({{tracks=user_tracks,regions=user_regions,markers=markers}})
end
function emit_state()
  state.tracks, state.regions, state.markers = tracks, regions, markers
  state.next_guid, state.next_region, state.tempo_writes, state.proj_ext = next_guid, next_region, tempo_writes, proj_ext
  io.write('\\n__VGT_STATE__' .. lua_value(state))
end
"""


def _run(project: Path, state: str, module: str, program: str) -> tuple[str, str]:
    result = subprocess.run([LUA, "-", str(project)], input="\n".join([state, module, program, "emit_state()"]), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    output, persisted = result.stdout.rsplit("\n__VGT_STATE__", 1)
    api_start = _lua_state(project).index("reaper = {}")
    api = _lua_state(project)[api_start:]
    restored = "local state = " + persisted + "\nlocal tracks, regions, markers = state.tracks, state.regions, state.markers\nlocal next_guid, next_region, tempo_writes, proj_ext = state.next_guid, state.next_region, state.tempo_writes, state.proj_ext or {}\n"
    return restored + api, output


def _run_apply(project: Path, state: str) -> tuple[str, str]:
    module = APPLY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    return _run(project, state, module, "apply(); report()")


def _run_apply_key_snapshot(project: Path, state: str) -> tuple[str, str]:
    """Read the managed Key display from the same offline REAPER fixture."""
    module = APPLY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    program = """
apply()
local count, detail = 0, ''
for _, track in ipairs(tracks) do
  if track.name == '[vgt] Key' then
    count = count + 1
    local item = track.items[1]
    detail = tostring(track.B_MUTE) .. ':' .. tostring(item.notes) .. ':' .. tostring(item.C_LOCK)
  end
end
io.write(count .. '#' .. detail)
"""
    return _run(project, state, module, program)


def _run_sync(project: Path, state: str) -> str:
    module = SYNC_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    state, _ = _run(project, state, module, "sync()")
    return state


def _run_tempo_map_sync(project: Path, state: str) -> str:
    """Drive the separately-confirmed action in the offline REAPER harness."""
    module = TEMPO_SYNC_SCRIPT.read_text(encoding="utf-8").split("local ok,err=xpcall", 1)[0]
    state, _ = _run(project, state, module, "reaper.ShowMessageBox=function(_,_,kind) return kind == 4 and 6 or 0 end; sync_tempo_map()")
    return state


def _run_promote(project: Path, state: str, *guids: str) -> tuple[str, str]:
    """Promote selected simulated working copies through the real ReaScript."""
    module = WORKING_COPY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    selected_guids = "{" + ",".join(json.dumps(guid) for guid in guids) + "}"
    return _run(project, state, module, f"""
local selected_guids = {selected_guids}
local selected = {{}}
for _, guid in ipairs(selected_guids) do selected[guid] = true end
for _, track in ipairs(tracks) do reaper.SetTrackSelected(track, selected[track.guid]) end
reaper.ShowMessageBox = function() end
promote()
report()
""")


def _user_snapshot(project: Path, state: str, *, managed_tracks: list[str] | None = None, managed_regions: list[int] | None = None) -> str:
    """Return a stable byte-for-byte snapshot of every user-owned object."""
    tracks = "{" + ",".join(json.dumps(guid) for guid in managed_tracks or []) + "}"
    regions = "{" + ",".join(str(region_id) for region_id in managed_regions or []) + "}"
    _, snapshot = _run(project, state, "", f"io.write(user_snapshot({tracks}, {regions}))")
    return snapshot


def _track_snapshot(project: Path, state: str, guid: str) -> str:
    """Serialize one user-owned track exactly, independent of its position."""
    _, snapshot = _run(project, state, "", f"""
local found
for _, track in ipairs(tracks) do
  if track.guid == {guid!r} then found = track end
end
assert(found, 'missing track ' .. {guid!r})
io.write(lua_value(found))
""")
    return snapshot


def _container_children_snapshot(project: Path, state: str, container_guid: str) -> str:
    """Serialize one container's descendants in their exact project order."""
    _, snapshot = _run(project, state, "", f"""
local container_index
for index, track in ipairs(tracks) do
  if track.guid == {container_guid!r} then container_index = index break end
end
assert(container_index, 'missing container ' .. {container_guid!r})
local depth = tracks[container_index].I_FOLDERDEPTH or 0
local children = {{}}
if depth > 0 then
  for index = container_index + 1, #tracks do
    local child = tracks[index]
    children[#children + 1] = child
    depth = depth + (child.I_FOLDERDEPTH or 0)
    if depth == 0 then break end
  end
end
io.write(lua_value(children))
""")
    return snapshot


def _assert_container_layout(project: Path, state: str, *, copy_guid: str | None = None) -> None:
    """Assert the canonical top-level tail without hiding user-owned children."""
    copy_assertion = ""
    if copy_guid is not None:
        copy_assertion = f"""
local copy_index
for index, track in ipairs(tracks) do if track.guid == {copy_guid!r} then copy_index = index end end
assert(copy_index and work < copy_index, 'working copy is not below [work]')
"""
    _, output = _run(project, state, "", f"""
local clean_name = '[clean] The Seven Rivers (Full March - 3_00)'
local work_name = '[work] The Seven Rivers (Full March - 3_00)'
local vgt_name = '[vgt] The Seven Rivers (Full March - 3_00)'
local clean, work, vgt, click, reference, paris = nil, nil, nil, nil, nil, nil
for index, track in ipairs(tracks) do
  if track.name == clean_name then clean = index end
  if track.name == work_name then work = index end
  if track.name == vgt_name then vgt = index end
  if track.guid == '{{CLICK}}' then click = index end
  if track.guid == '{REFERENCE_GUID}' then reference = index end
  if track.guid == '{{PARIS}}' then paris = index end
end
assert(clean and work and vgt and clean < work and work < vgt, 'containers are not clean/work/vgt tail')
assert(click < clean and reference < clean and paris < clean and click < reference and reference < paris,
  'fixture user tracks lost their relative order above containers')
assert(reaper.GetMediaTrackInfo_Value(tracks[clean], 'I_CUSTOMCOLOR') == (41 * 65536 + 210 * 256 + 187 + 0x1000000), 'clean colour')
assert(reaper.GetMediaTrackInfo_Value(tracks[work], 'I_CUSTOMCOLOR') == (239 * 65536 + 175 * 256 + 68 + 0x1000000), 'work colour')
assert(tracks[clean].ext['P_EXT:vgt_container'] == 'clean' and tracks[work].ext['P_EXT:vgt_container'] == 'work', 'container marks')
{copy_assertion}
io.write('container layout ok')
    """)
    assert output == "container layout ok"
    sidecar = read_sidecar(project)
    _, inventory = _run(project, state, "", """
local container_guids = {}
for _, track in ipairs(tracks) do
  if track.ext and track.ext['P_EXT:vgt_container'] then container_guids[#container_guids + 1] = track.guid end
end
table.sort(container_guids)
io.write(table.concat(container_guids, ','))
""")
    assert not set(filter(None, inventory.split(","))) & set(sidecar["managed_track_guids"])


def _assert_managed_contract(project: Path, state: str) -> None:
    """Check the exact vgt inventory in the persistent offline project."""
    _, output = _run(project, state, "", r'''
local expected = {
  ['[vgt] The Seven Rivers (Full March - 3_00)']=true, ['[vgt] Beats']=true, ['[vgt] Click']=true,
  ['[vgt] Key']=true, ['[vgt] Chords']=true, ['[vgt] Vocals']=true,
  ['[vgt] Instrumental']=true, ['[vgt] Bass']=true, ['[vgt] Drums']=true,
  ['[vgt] Guitar']=true, ['[vgt] Backing (no guitar)']=true,
  ['[vgt] Guitar Ref — default (MIDI)']=true,
}
local expected_sources = {
  ['[vgt] Vocals']='stems/vocals.wav', ['[vgt] Instrumental']='stems/instrumental.wav',
  ['[vgt] Bass']='stems/bass.wav', ['[vgt] Drums']='stems/drums.wav',
  ['[vgt] Guitar']='stems/guitar.wav', ['[vgt] Backing (no guitar)']='stems/backing-no-guitar.wav',
  ['[vgt] Guitar Ref — default (MIDI)']='transcription/guitar/default-guitar.mid',
  ['[vgt] Click']='tempo-click.wav',
}
local expected_empty_tracks = {
  ['[vgt] The Seven Rivers (Full March - 3_00)']=true,
}
local seen_guids, seen_names, managed_count, managed_guids = {}, {}, 0, {}
for _, track in ipairs(tracks) do
  if track.name:sub(1, 5) == '[vgt]' then
    assert(expected[track.name] and not seen_names[track.name], 'unexpected or duplicate managed track: ' .. track.name)
    assert(not seen_guids[track.guid], 'duplicate managed GUID: ' .. track.guid)
    seen_guids[track.guid], seen_names[track.name], managed_count = true, true, managed_count + 1
    managed_guids[#managed_guids + 1] = track.guid
    if expected_empty_tracks[track.name] then
      assert(#track.items == 0, 'unexpected item on managed folder')
    elseif track.name == '[vgt] Beats' then
      assert(#track.items == 3, 'beat item count')
      for index, item in ipairs(track.items) do
        local expected_length = index < 3 and 1 or 2
        assert(item.position == 9 + index and item.length == expected_length and item.notes == ('Beat ' .. index)
          and item.take.name == ('Beat ' .. index) and item.C_LOCK == 1, 'beat annotations')
      end
    elseif track.name == '[vgt] Key' then
      assert(#track.items == 1 and track.items[1].position == 10 and track.items[1].length == 4
        and track.items[1].notes == 'E minor' and track.items[1].take.name == 'E minor'
        and track.items[1].C_LOCK == nil, 'key annotation')
    elseif track.name == '[vgt] Chords' then
      assert(#track.items == 1 and track.items[1].position == 10.25 and track.items[1].length == 0.75
        and track.items[1].notes == 'Dm' and track.items[1].take.name == 'Dm' and track.items[1].C_LOCK == nil, 'chord annotations')
    elseif expected_sources[track.name] then
      -- An audio item is as long as its source; a reference MIDI instead
      -- spans the whole reference track (10..14) however early its last note
      -- falls, and must not loop its transcription to fill that span.
      local expected_length = 1
      if track.name:match('%(MIDI%)$') then
        expected_length = 4
        assert(track.items[1].B_LOOPSRC == 0, 'reference MIDI must not loop: ' .. track.name)
      end
      assert(#track.items == 1 and track.items[1].position == 10 and track.items[1].length == expected_length,
        'source item placement: ' .. track.name)
      local source_path, expected_source = track.items[1].take.source.path, expected_sources[track.name]
      assert(source_path:match('/vgt/') and source_path:sub(-#expected_source) == expected_source,
        'wrong source association for ' .. track.name .. ': ' .. tostring(source_path))
      if track.name == '[vgt] Click' then assert(track.B_MUTE == 1, 'click must be muted') end
    end
  end
end
assert(managed_count == 12)
for name in pairs(expected) do assert(seen_names[name], 'missing managed track: ' .. name) end
local expected_regions = {
  ['[vgt] Bridge']={start=10.25, finish=11},
  ['[vgt] Chorus']={start=11, finish=12},
}
local seen_regions, seen_region_names, managed_regions, managed_region_ids = {}, {}, 0, {}
for _, region in ipairs(regions) do
  if region.name:sub(1, 5) == '[vgt]' then
    assert(not seen_regions[region.id], 'duplicate managed region ID: ' .. region.id)
    local expected_region = expected_regions[region.name]
    assert(expected_region and not seen_region_names[region.name], 'unexpected or duplicate managed region: ' .. region.name)
    assert(region.start == expected_region.start and region.finish == expected_region.finish,
      'wrong managed region geometry: ' .. region.name)
    seen_regions[region.id], seen_region_names[region.name], managed_regions = true, true, managed_regions + 1
    managed_region_ids[#managed_region_ids + 1] = tostring(region.id)
  end
end
assert(managed_regions == 2)
for name in pairs(expected_regions) do assert(seen_region_names[name], 'missing managed region: ' .. name) end
table.sort(managed_guids)
table.sort(managed_region_ids)
io.write('managed contract ok#' .. table.concat(managed_guids, ',') .. '#' .. table.concat(managed_region_ids, ','))
''')
    message, guids, region_ids = output.split("#")
    sidecar = read_sidecar(project)
    assert message == "managed contract ok"
    assert guids.split(",") == sorted(sidecar["managed_track_guids"])
    assert region_ids.split(",") == sorted(map(str, sidecar["managed_region_ids"]))


def test_goal_contract_is_offline_non_destructive_and_idempotent(tmp_path: Path, deterministic_detectors: None) -> None:
    project = _copy_project(tmp_path)

    # This baseline precedes even initialization, and includes every user
    # track/item/region plus the pre-existing tempo map.  It is deliberately
    # not name-based: the final comparison must catch user-object renames.
    state = _lua_state(project)
    initial_user_snapshot = _user_snapshot(project, state)

    # Initialization selects the real fixture's reference identity and writes only a sidecar.
    state, _ = _run_apply(project, state)
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID
    initialized = read_sidecar(project)
    # From this point the scaffold is part of the user-owned snapshot too.
    # It is compared by GUID, so subsequent legitimate block moves do not
    # mask any mutation to a container or one of its future children.
    before = _user_snapshot(
        project, state,
        managed_tracks=initialized["managed_track_guids"],
        managed_regions=initialized["managed_region_ids"],
    )
    assert "The Seven Rivers (Full March - 3_00)" in initial_user_snapshot

    separator, transcriber = CountingSeparator(), CountingTranscriber()
    analyze(project, stages=("tempo", "key", "sections"))
    separate(project, separator, guitar_type="electric")
    analyze(project, stages=("chords", "transcription"), transcription_targets=("guitar",), transcriber=transcriber)
    # A Basic Pitch target reaches the backend through `detect_raw` -- its
    # cleanup is derived from those raw notes -- so one guitar transcription is
    # one raw detection, never a legacy one-shot `transcribe()` (#223).
    assert (separator.calls, transcriber.calls, transcriber.raw_calls) == (5, 0, 1)

    # The existing 100 BPM map must remain untouched, so apply offers beat labels instead.
    state, first_apply = _run_apply(project, state)
    names, user_items, region_count, vgt_count, tempo_writes = first_apply.split("#")
    assert user_items == "2" and region_count == "3" and tempo_writes == "0"
    assert "[vgt] Beats" in names and "[vgt] Key" in names and "[vgt] Guitar Ref — default (MIDI)" in names
    assert int(vgt_count) == 12  # folder, beats/click/key/chords, six stems, MIDI

    sidecar = read_sidecar(project)
    # These are real edits to the state produced by apply, not a newly-built
    # approximation of it.  Sync must read them while preserving every other object.
    sync_module = SYNC_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    state, _ = _run(project, state, sync_module, """
for _, track in ipairs(tracks) do
  if track.name == '[vgt] Chords' then track.items = {{position=10.25,length=0.75,take={name='Dm'}}} end
  if track.name == '[vgt] Key' then track.items[1].take.name = 'E minor' end
end
for _, region in ipairs(regions) do
  if region.id == %d then region.start=10.25; region.finish=11; region.name='[vgt] Bridge' end
end
sync()
""" % sidecar["managed_region_ids"][0])
    synced = read_sidecar(project)
    assert synced["analysis"]["chords"]["value"]["segments"][0]["chord"] == "Dm"
    assert synced["analysis"]["sections"]["value"][0]["label"] == "Bridge"
    assert synced["analysis"]["key"]["value"] == {"root": "E", "scale": "minor"}
    assert synced["analysis"]["key"]["human_verified"] is True
    detected_chords = synced["analysis"]["chords"]["detected"]
    detected_sections = synced["analysis"]["sections"]["detected"]
    detected_key = synced["analysis"]["key"]["detected"]

    # Forced free re-analysis refreshes detected baselines but preserves human sync edits;
    # paid splits and the target MIDI cache must not run again.
    analyze(project, force=True, stages=("key", "sections", "chords"))
    analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber=transcriber)
    separate(project, separator, guitar_type="electric")
    reconciled = read_sidecar(project)
    assert reconciled["analysis"]["chords"]["value"]["segments"][0]["chord"] == "Dm"
    assert reconciled["analysis"]["sections"]["value"][0]["label"] == "Bridge"
    assert reconciled["analysis"]["chords"]["detected"] == detected_chords
    assert reconciled["analysis"]["sections"]["detected"] == detected_sections
    assert reconciled["analysis"]["key"]["value"] == {"root": "E", "scale": "minor"}
    assert reconciled["analysis"]["key"]["detected"] == detected_key
    # A Basic Pitch target reaches the backend through `detect_raw` -- its
    # cleanup is derived from those raw notes -- so one guitar transcription is
    # one raw detection, never a legacy one-shot `transcribe()` (#223).
    assert (separator.calls, transcriber.calls, transcriber.raw_calls) == (5, 0, 1)

    # The display is rebuilt from the synchronized effective key without
    # adding another track.
    state, second_apply = _run_apply(project, state)
    names, user_items, region_count, vgt_count, tempo_writes = second_apply.split("#")
    assert user_items == "2" and region_count == "3" and tempo_writes == "0"
    assert int(vgt_count) == 12 and names.split("|").count("[vgt] Guitar") == 1
    # The second apply must have reused the original persisted reference
    # rather than re-prompting or drifting onto another candidate (issue #136).
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID
    state, key_snapshot = _run_apply_key_snapshot(project, state)
    assert key_snapshot == "1#0:E minor:nil"
    _assert_managed_contract(project, state)
    _assert_container_layout(project, state)
    final_sidecar = read_sidecar(project)
    assert _user_snapshot(
        project,
        state,
        managed_tracks=final_sidecar["managed_track_guids"],
        managed_regions=final_sidecar["managed_region_ids"],
    ) == before

    # A re-apply is layout-idempotent: one marked, correctly coloured
    # container of each kind remains before the rebuilt [vgt] block.
    layout_before = _user_snapshot(
        project, state,
        managed_tracks=final_sidecar["managed_track_guids"],
        managed_regions=final_sidecar["managed_region_ids"],
    )
    state, _ = _run_apply(project, state)
    reapplied = read_sidecar(project)
    _assert_container_layout(project, state)
    assert _user_snapshot(
        project, state,
        managed_tracks=reapplied["managed_track_guids"],
        managed_regions=reapplied["managed_region_ids"],
    ) == layout_before

    # Model a populated workspace.  The selected, marked scratch copy is the
    # sole eligible promotion target; a selected unmarked lookalike and a
    # selected renamed/reclaimed copy must remain completely unchanged.
    work_guid = "{WORK-COPY-0001}"
    unselected_work_guid = "{WORK-UNSELECTED-0002}"
    unmarked_guid = "{WORK-UNMARKED-0002}"
    reclaimed_guid = "{WORK-RECLAIMED-0003}"
    state, _ = _run(project, state, "", f"""
for index, track in ipairs(tracks) do
  if track.ext and track.ext['P_EXT:vgt_container'] == 'work' then
    track.I_FOLDERDEPTH = 1
    table.insert(tracks, index + 1, {{guid='{work_guid}', name='[work] Guitar Ref — default (MIDI)', B_MUTE=0,
      I_FOLDERDEPTH=0, ext={{['P_EXT:vgt_working_copy']='1'}},
      items={{{{position=10,length=4,C_LOCK=0,take={{name='edited by user',source=''}}}}}}}})
    table.insert(tracks, index + 2, {{guid='{unselected_work_guid}', name='[work] Bass Ref — default (MIDI)', B_MUTE=0,
      I_FOLDERDEPTH=0, ext={{['P_EXT:vgt_working_copy']='1'}},
      items={{{{position=12,length=2,C_LOCK=0,take={{name='leave selected state alone',source='bass.mid'}}}}}}}})
    table.insert(tracks, index + 3, {{guid='{unmarked_guid}', name='[work] hand-made', B_MUTE=1,
      I_FOLDERDEPTH=0, ext={{custom='keep'}}, items={{{{position=13,length=2,take={{name='leave me',source='manual.wav'}}}}}}}})
    table.insert(tracks, index + 4, {{guid='{reclaimed_guid}', name='User reclaimed draft', B_MUTE=0,
      I_FOLDERDEPTH=-1, ext={{['P_EXT:vgt_working_copy']='1',custom='reclaimed'}},
      items={{{{position=16,length=3,take={{name='do not touch',source='user.wav'}}}}}}}})
    break
  end
end
""")
    copy_sidecar = read_sidecar(project)
    copy_snapshot = _user_snapshot(
        project, state,
        managed_tracks=copy_sidecar["managed_track_guids"],
        managed_regions=copy_sidecar["managed_region_ids"],
    )
    state, _ = _run_apply(project, state)
    applied_copy_sidecar = read_sidecar(project)
    _assert_container_layout(project, state, copy_guid=work_guid)
    assert _user_snapshot(
        project, state,
        managed_tracks=applied_copy_sidecar["managed_track_guids"],
        managed_regions=applied_copy_sidecar["managed_region_ids"],
    ) == copy_snapshot

    # Promotion moves exactly the eligible selected track. An eligible but
    # unselected copy, plus ineligible and reclaimed selected tracks, retain
    # their complete payloads; this catches accidental broad promotion or a
    # working-copy action that "cleans up" private marks.
    state, _ = _run(project, state, "", f"""
local selected = {{['{work_guid}']=true, ['{unmarked_guid}']=true, ['{reclaimed_guid}']=true}}
for _, track in ipairs(tracks) do reaper.SetTrackSelected(track, selected[track.guid]) end
""")
    unselected_work_snapshot = _track_snapshot(project, state, unselected_work_guid)
    unmarked_snapshot = _track_snapshot(project, state, unmarked_guid)
    reclaimed_snapshot = _track_snapshot(project, state, reclaimed_guid)
    state, _ = _run_promote(project, state, work_guid, unmarked_guid, reclaimed_guid)
    state, promotion = _run(project, state, "", f"""
local found
for _, track in ipairs(tracks) do if track.guid == '{work_guid}' then found = track end end
assert(found and found.name == '[clean] Guitar Ref — default (MIDI)', 'promoted name')
assert(found.ext['P_EXT:vgt_working_copy'] == '', 'working-copy mark survives promotion')
assert(#found.items == 1 and found.items[1].position == 10 and found.items[1].length == 4
  and found.items[1].take.name == 'edited by user', 'promoted items changed')
io.write('promotion ok')
""")
    assert promotion == "promotion ok"
    assert _track_snapshot(project, state, unselected_work_guid) == unselected_work_snapshot
    assert _track_snapshot(project, state, unmarked_guid) == unmarked_snapshot
    assert _track_snapshot(project, state, reclaimed_guid) == reclaimed_snapshot
    promoted_sidecar = read_sidecar(project)
    promoted_snapshot = _track_snapshot(project, state, work_guid)
    state, post_promotion_apply = _run_apply(project, state)
    assert post_promotion_apply.split("#", 1)[0].split("|").count("[vgt] The Seven Rivers (Full March - 3_00)") == 1
    post_promotion_sidecar = read_sidecar(project)
    assert _track_snapshot(project, state, work_guid) == promoted_snapshot

    # A later selected working copy must not be appended by rewriting the
    # existing promoted clean child (REAPER stores a folder closer on that
    # child). The explicit action therefore rejects this request atomically.
    later_work_guid = "{WORK-LATER-0004}"
    state, _ = _run(project, state, "", f"""
for index, track in ipairs(tracks) do
  if track.guid == '{reclaimed_guid}' then
    track.I_FOLDERDEPTH = 0
    table.insert(tracks, index + 1, {{guid='{later_work_guid}', name='[work] Later draft', B_MUTE=0,
      I_FOLDERDEPTH=-1, ext={{['P_EXT:vgt_working_copy']='1'}},
      items={{{{position=20,length=1,take={{name='later',source='later.mid'}}}}}}}})
    break
  end
end
""")
    state, _ = _run(project, state, "", f"""
for _, track in ipairs(tracks) do reaper.SetTrackSelected(track, track.guid == '{later_work_guid}') end
""")
    user_before_rejected_promotion = _user_snapshot(
        project, state,
        managed_tracks=post_promotion_sidecar["managed_track_guids"],
        managed_regions=post_promotion_sidecar["managed_region_ids"],
    )
    state, _ = _run_promote(project, state, later_work_guid)
    assert _user_snapshot(
        project, state,
        managed_tracks=post_promotion_sidecar["managed_track_guids"],
        managed_regions=post_promotion_sidecar["managed_region_ids"],
    ) == user_before_rejected_promotion


def _guid(seed: int) -> str:
    # GUIDs are matched by the reascript's `{[%x%-]+}` pattern, so only
    # hex/hyphen characters are valid here.
    return "{AAAAAAAA-0000-0000-0000-%012d}" % seed


def test_goal_contract_reconciles_the_7rivers_incident_fixture_into_a_single_root(tmp_path: Path) -> None:
    """Regression fixture derived from the actual saved 7Rivers.RPP evidence in
    issue #174: a 13-track managed folder (root + 12 children) whose sidecar
    `managed_track_guids`, per-track `P_EXT:vgt_managed` marks, and project
    root manifest all agree, plus 9 managed regions, sitting alongside a
    genuine user track/region that must survive untouched. Reconciling it must
    collapse to exactly one managed root -- and, because that first apply
    leaves a *flattened* root behind (no analysis, so nothing nests under it),
    applying a second time must still leave exactly one, proving the
    flattened-root recognition fix rather than just the folder-root case
    already covered by test_goal_contract_is_offline_non_destructive_and_idempotent."""
    project = tmp_path / "7Rivers.RPP"
    sidecar = tmp_path / "7Rivers.vgt"

    root_guid = _guid(0)
    child_roles = [
        "beats", "click", "key", "chords",
        "stem:vocals", "stem:instrumental", "stem:bass", "stem:drums",
        "stem:guitar", "stem:backing", "stem:strings", "stem:piano",
    ]
    child_guids = [_guid(index + 1) for index in range(len(child_roles))]
    managed_guids = [root_guid] + child_guids
    region_ids = list(range(1, 10))  # nine managed regions, matching the evidence
    folder_name = "[vgt] The Seven Rivers (Full March - 3_00)"

    sidecar.write_text(json.dumps({
        "schema_version": 4,
        "generation": 3,
        "managed_track_guids": managed_guids,
        "managed_region_ids": region_ids,
        "config": {
            "reference_track_name": "The Seven Rivers (Full March - 3_00)",
            "reference_track_guid": REFERENCE_GUID,
            "folder_name": folder_name,
            "tempo_map_applied": False,
            "tempo_map_fingerprint": "",
            "tempo_data_fingerprint": "",
            "guitar_type": "electric",
        },
    }), encoding="utf-8")

    manifest = ";".join(["root=" + root_guid] + [f"{guid}={role}" for guid, role in zip(child_guids, child_roles)])

    track_lines = [
        "{guid=%r, name=%r, I_FOLDERDEPTH=1, ext={['P_EXT:vgt_managed']='1', ['P_EXT:vgt_role']='managed-root'}, items={}}"
        % (root_guid, folder_name)
    ]
    for index, (guid, role) in enumerate(zip(child_guids, child_roles)):
        depth = -1 if index == len(child_roles) - 1 else 0
        track_lines.append(
            "{guid=%r, name='[vgt] Child %d', I_FOLDERDEPTH=%d, ext={['P_EXT:vgt_managed']='1', ['P_EXT:vgt_role']=%r}, items={}}"
            % (guid, index, depth, role)
        )
    track_lines.append(
        "{guid=%r, name='The Seven Rivers (Full March - 3_00)', B_MUTE=0, "
        "items={{position=10,length=4,C_LOCK=0,take={name='Original mix',source='Media/x.mp3'}}}}" % REFERENCE_GUID
    )
    track_lines.append(
        "{guid='{B00B0000-0000-0000-0000-000000000000}', name='Paris Metro Punk', B_MUTE=1, "
        "items={{position=2,length=3,C_LOCK=1,take={name='Paris source',source='Media/Paris Metro Punk.mp3'}}}}"
    )

    region_lines = [f"{{id={region_id}, start={region_id}, finish={region_id + 1}, name='[vgt] Section {region_id}'}}" for region_id in region_ids]
    region_lines.append("{id=900, start=50, finish=51, name='User region', color=42}")

    api_start = _lua_state(project).index("reaper = {}")
    api = _lua_state(project)[api_start:]
    prelude = f"""
local state = {{tracks={{{",".join(track_lines)}}},regions={{{",".join(region_lines)}}},markers={{{{time=0,bpm=100,num=4,den=4}}}},
  next_guid=1,next_region=2000,tempo_writes=0,proj_ext={{['vgt:managed_root_manifest']={manifest!r}}}}}
local tracks, regions, markers = state.tracks, state.regions, state.markers
local next_guid, next_region, tempo_writes, proj_ext = state.next_guid, state.next_region, state.tempo_writes, state.proj_ext
"""
    state = prelude + api

    state, first_apply = _run_apply(project, state)
    _, user_items, region_count, vgt_count, _ = first_apply.split("#")
    assert user_items == "2", "the reference and Paris Metro Punk items must survive untouched"
    assert region_count == "1", "only the unmanaged 'User region' should remain; all nine old managed regions are gone"
    assert vgt_count == "1", "the old 13-track folder must collapse into exactly one new root, never coexist with it"
    assert len(read_sidecar(project)["managed_track_guids"]) == 1
    assert set(managed_guids) & set(read_sidecar(project)["managed_track_guids"]) == set(), "none of the old 7Rivers GUIDs may survive"

    # The new root has nothing analyzed to nest under it, so apply() flattens
    # it back to I_FOLDERDEPTH 0 -- exactly the shape that escaped detection
    # before this fix. A second apply must still reconcile to one root.
    state, second_apply = _run_apply(project, state)
    _, user_items, region_count, vgt_count, _ = second_apply.split("#")
    assert user_items == "2"
    assert region_count == "1"
    assert vgt_count == "1", "a flattened root must be recognized and reconciled, not duplicated"
    assert len(read_sidecar(project)["managed_track_guids"]) == 1


def test_goal_contract_adopts_interleaved_handmade_containers_and_moves_their_blocks(tmp_path: Path) -> None:
    """Keep adoption and canonical block ordering in the offline workflow.

    The first apply establishes the real fixture's sidecar/reference.  The
    user then replaces its empty scaffold with old, unmarked folders placed
    between normal tracks; this models projects that predate the scaffold.
    """
    project = _copy_project(tmp_path)
    state, _ = _run_apply(project, _lua_state(project))
    state, _ = _run(project, state, "", """
for index = #tracks, 1, -1 do
  local ext = tracks[index].ext
  if ext and ext['P_EXT:vgt_container'] then table.remove(tracks, index) end
end
table.insert(tracks, 2, {guid='{HAND-CLEAN}', name='[clean] hand-made', I_FOLDERDEPTH=1,
  I_CUSTOMCOLOR=123, items={}})
table.insert(tracks, 3, {guid='{HAND-CLEAN-CHILD}', name='clean user child', I_FOLDERDEPTH=-1,
  items={{position=3,length=2,C_LOCK=1,take={name='keep clean item',source='hand.wav'}}}})
table.insert(tracks, #tracks + 1, {guid='{HAND-WORK}', name='[work] hand-made', I_FOLDERDEPTH=1,
  I_CUSTOMCOLOR=456, items={}})
table.insert(tracks, #tracks + 1, {guid='{HAND-WORK-CHILD}', name='work user child', I_FOLDERDEPTH=-1,
  items={{position=7,length=1,C_LOCK=0,take={name='keep work item',source='scratch.wav'}}}})
""")
    # Snapshot the full child payload and sequence before adoption.  Apply may
    # move the two container blocks, but automatic reconciliation cannot alter
    # any child or reorder siblings within either block.
    clean_children_before = _container_children_snapshot(project, state, "{HAND-CLEAN}")
    work_children_before = _container_children_snapshot(project, state, "{HAND-WORK}")

    state, _ = _run_apply(project, state)
    assert _container_children_snapshot(project, state, "{HAND-CLEAN}") == clean_children_before
    assert _container_children_snapshot(project, state, "{HAND-WORK}") == work_children_before
    # A later initialize pass has the same ownership boundary; adoption must
    # not be a one-pass exception to child payload/order preservation.
    state, _ = _run_apply(project, state)
    assert _container_children_snapshot(project, state, "{HAND-CLEAN}") == clean_children_before
    assert _container_children_snapshot(project, state, "{HAND-WORK}") == work_children_before
    _, output = _run(project, state, "", """
local positions, clean, work, vgt = {}, nil, nil, nil
for index, track in ipairs(tracks) do
  positions[track.guid] = index
  if track.guid == '{HAND-CLEAN}' then clean = track end
  if track.guid == '{HAND-WORK}' then work = track end
  if track.name == '[vgt] The Seven Rivers (Full March - 3_00)' then vgt = index end
end
assert(clean.ext['P_EXT:vgt_container'] == 'clean' and work.ext['P_EXT:vgt_container'] == 'work', 'unmarked folders not adopted')
assert(clean.I_CUSTOMCOLOR == 123 and work.I_CUSTOMCOLOR == 456, 'adoption recoloured a hand-made folder')
assert(positions['{CLICK}'] < positions['{75418143-1F31-B548-B7D2-96815CB0297D}']
  and positions['{75418143-1F31-B548-B7D2-96815CB0297D}'] < positions['{PARIS}'], 'user order changed')
assert(positions['{PARIS}'] < positions['{HAND-CLEAN}'] and positions['{HAND-CLEAN-CHILD}'] < positions['{HAND-WORK}']
  and positions['{HAND-WORK-CHILD}'] < vgt, 'container blocks not canonical tail')
io.write('adoption layout ok')
""")
    assert output == "adoption layout ok"


def test_goal_contract_exercises_cli_paid_stem_cost_controls_and_resume(
    tmp_path: Path,
    deterministic_detectors: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Prove the CLI's paid-work guardrails with no credential or network.

    This deliberately drives ``main`` rather than ``separate`` so the
    acceptance contract covers the non-interactive consent boundary as well
    as the lower-level durable cache/checkpoint behavior.
    """
    credential = "offline-contract-license-must-not-persist"
    monkeypatch.setenv("LALAL_LICENSE_KEY", credential)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    project = _copy_project(tmp_path / "cost-controls")
    _run_apply(project, _lua_state(project))
    ledger = OfflineLalalLedger()
    monkeypatch.setattr("vgt.cli.LalalSeparator", ledger.make_backend)
    output_fragments: list[str] = []

    # Establish the cached five-operation recipe through the actual CLI.
    assert main(["analyze", "--guitar", "electric", str(project)]) == 0
    assert len(ledger.charged_stems) == 5
    assert ledger.quoted_operation_counts == [5]
    captured = capsys.readouterr()
    output_fragments.extend((captured.out, captured.err))

    # --force refreshes local analysis only; it cannot reopen the paid recipe.
    assert main(["analyze", "--guitar", "electric", "--force", str(project)]) == 0
    assert len(ledger.backends) == 1
    assert len(ledger.charged_stems) == 5
    captured = capsys.readouterr()
    assert "--force never spends credits" in captured.err
    output_fragments.extend((captured.out, captured.err))

    # Neither forced nor opt-in paid work gets as far as creating a backend
    # (and therefore cannot submit) without the explicit non-interactive ack.
    assert main(["analyze", "--guitar", "electric", "--force-stems", str(project)]) == 2
    assert main(["analyze", "--guitar", "electric", "--extra-stem", "strings", str(project)]) == 2
    assert len(ledger.backends) == 1
    captured = capsys.readouterr()
    refusal_output = captured.err
    assert refusal_output.count("requires --accept-stem-cost") == 2
    output_fragments.extend((captured.out, captured.err))

    # The accepted two-stem request is quoted as two operations, creates both
    # requested artifacts, and is fully cached when retried without flags.
    assert main(
        [
            "analyze", "--guitar", "electric", "--extra-stem", "strings", "--extra-stem", "keys/piano",
            "--accept-stem-cost", str(project),
        ]
    ) == 0
    assert ledger.quoted_operation_counts[-1] == 2
    assert ledger.charged_stems[-2:] == ["strings", "piano"]
    stems = read_sidecar(project)["analysis"]["stems"]
    namespace = artifact_namespace_dir(project, stems["artifact_namespace"])
    assert stems["optional_stems"] == ["strings", "piano"]
    assert (namespace / "stems" / "strings.wav").is_file() and (namespace / "stems" / "piano.wav").is_file()
    charged_after_optional = list(ledger.charged_stems)
    captured = capsys.readouterr()
    assert "PAID stem operations requested for 2 operations" in captured.err
    output_fragments.extend((captured.out, captured.err))
    assert main(["analyze", "--guitar", "electric", "--accept-stem-cost", str(project)]) == 0
    assert ledger.charged_stems == charged_after_optional
    captured = capsys.readouterr()
    output_fragments.extend((captured.out, captured.err))

    # A second copied project isolates an interruption: the fake checkpoints
    # the strings task after charging it, then the CLI retry resumes that
    # exact task id without another submission/charge.
    interrupted = _copy_project(tmp_path / "checkpoint-resume")
    _run_apply(interrupted, _lua_state(interrupted))
    interrupted_ledger = OfflineLalalLedger(fail_stems_once={"strings"})
    monkeypatch.setattr("vgt.cli.LalalSeparator", interrupted_ledger.make_backend)
    assert main(
        ["analyze", "--guitar", "electric", "--extra-stem", "strings", "--accept-stem-cost", str(interrupted)]
    ) == 0
    interrupted_stems = read_sidecar(interrupted)["analysis"]["stems"]
    checkpointed = interrupted_stems["operations"]["strings-original"]
    assert checkpointed["backend_state"]["task_id"] == "offline-task-strings"
    assert interrupted_ledger.charged_stems.count("strings") == 1
    assert main(["analyze", "--guitar", "electric", "--accept-stem-cost", str(interrupted)]) == 0
    assert interrupted_ledger.charged_stems.count("strings") == 1
    assert interrupted_ledger.resumed_stems == ["strings"]
    assert read_sidecar(interrupted)["analysis"]["stems"]["operations"]["strings-original"]["status"] == "completed"

    # The only credential used by this test is a canary. It must never escape
    # into durable JSON/artifacts or CLI output.
    captured = capsys.readouterr()
    output_fragments.extend((captured.out, captured.err))
    persisted = [
        project.with_suffix(".vgt").read_text(encoding="utf-8"),
        interrupted.with_suffix(".vgt").read_text(encoding="utf-8"),
        *(path.read_bytes().decode("latin1") for path in namespace.rglob("*") if path.is_file()),
        *(
            path.read_bytes().decode("latin1")
            for path in artifact_namespace_dir(interrupted, interrupted_stems["artifact_namespace"]).rglob("*")
            if path.is_file()
        ),
        *output_fragments,
    ]
    assert all(credential not in value for value in persisted)


def test_goal_contract_reconciles_two_guitar_variants_without_touching_working_copies(
    tmp_path: Path, deterministic_detectors: None,
) -> None:
    """Exercise the complete multi-variant ownership contract offline.

    This intentionally uses the public lifecycle operations rather than
    constructing variant dictionaries: it is the integration proof that the
    sidecar model, raw-cache sharing, ReaScript apply reconciliation, direct
    discard semantics (#176 removed selection), and user-owned working
    copies agree.
    """
    project = _copy_project(tmp_path)
    state = _lua_state(project)
    original_user_state = _user_snapshot(project, state)

    # First apply initializes the real project's sidecar.  The fakes then
    # produce valid stem WAVs and MIDI entirely locally; no model, network, or
    # REAPER process is involved.
    state, _ = _run_apply(project, state)
    analyze(project, stages=("tempo", "key", "sections"))
    separator = CountingSeparator()
    separate(project, separator, guitar_type="acoustic")
    transcriber = CountingTranscriber()
    router = TargetTranscriberRouter(basic_pitch=transcriber, drumscript=transcriber, drumscript_targets=("drums",))

    detail = add_variant(
        project, "guitar", label="detail", profile="guitar-acoustic-detail", router=router,
    )
    clean = add_variant(
        project, "guitar", label="clean", profile="guitar-acoustic-clean", router=router,
    )
    sidecar = read_sidecar(project)
    guitar = sidecar["analysis"]["transcription"]["targets"]["guitar"]
    detail_id = next(variant_id for variant_id, variant in guitar["variants"].items() if variant["label"] == "detail")
    clean_id = next(variant_id for variant_id, variant in guitar["variants"].items() if variant["label"] == "clean")
    assert detail["detection_hash"] == clean["detection_hash"]
    assert transcriber.raw_calls == 1
    assert len(sidecar["analysis"]["transcription"]["detection_cache"]) == 1

    # Apply twice.  Both retained candidates persist as peers, each generated
    # track is rebuilt exactly once, and a simulated working copy is outside
    # vgt's durable ownership inventory.
    state, first_apply = _run_apply(project, state)
    first_names = first_apply.split("#", 1)[0].split("|")
    assert first_names.count("[vgt] Guitar Ref — detail (MIDI)") == 1
    assert first_names.count("[vgt] Guitar Ref — clean (MIDI)") == 1
    state, _ = _run(
        project,
        state,
        "",
        "table.insert(tracks, {guid='{WORK-0001}', name='[work] Guitar Ref — clean (MIDI)', B_MUTE=0, items={{position=10,length=1,C_LOCK=0,take={name='edited by user',source=''}}}})",
    )
    applied_sidecar = read_sidecar(project)
    work_snapshot = _user_snapshot(
        project,
        state,
        managed_tracks=applied_sidecar["managed_track_guids"],
        managed_regions=applied_sidecar["managed_region_ids"],
    )
    state, second_apply = _run_apply(project, state)
    second_names = second_apply.split("#", 1)[0].split("|")
    assert second_names.count("[vgt] Guitar Ref — detail (MIDI)") == 1
    assert second_names.count("[vgt] Guitar Ref — clean (MIDI)") == 1
    assert second_names.count("[work] Guitar Ref — clean (MIDI)") == 1
    guitar_after_apply = read_sidecar(project)["analysis"]["transcription"]["targets"]["guitar"]
    assert "selected_variant_id" not in guitar_after_apply
    assert set(guitar_after_apply["variant_order"]) == {detail_id, clean_id}

    namespace_dir = artifact_namespace_dir(project, read_sidecar(project)["analysis"]["stems"]["artifact_namespace"])
    detail_midi = namespace_dir / detail["midi_file"]
    clean_midi = namespace_dir / clean["midi_file"]
    assert detail_midi.is_file() and clean_midi.is_file()

    # Discarding one retained peer directly removes exactly its derived files
    # but keeps the shared raw cache for the other.  Reapply must remove only
    # that generated track and preserve the editable copy.
    discard_variant(project, "guitar", detail_id)
    assert not detail_midi.exists() and clean_midi.is_file()
    assert len(read_sidecar(project)["analysis"]["transcription"]["detection_cache"]) == 1
    state, after_detail_discard = _run_apply(project, state)
    names_after_detail_discard = after_detail_discard.split("#", 1)[0].split("|")
    assert "[vgt] Guitar Ref — detail (MIDI)" not in names_after_detail_discard
    assert names_after_detail_discard.count("[vgt] Guitar Ref — clean (MIDI)") == 1
    assert names_after_detail_discard.count("[work] Guitar Ref — clean (MIDI)") == 1

    # Discarding the final retained candidate is just as direct -- no
    # selection to clear -- and removes the now-unreferenced raw cache, while
    # leaving both original user tracks and the user's working copy
    # byte-for-byte intact.
    discard_variant(project, "guitar", clean_id)
    final_sidecar = read_sidecar(project)
    assert not clean_midi.exists()
    assert final_sidecar["analysis"]["transcription"]["detection_cache"] == {}
    assert "selected_variant_id" not in final_sidecar["analysis"]["transcription"]["targets"]["guitar"]
    state, final_apply = _run_apply(project, state)
    final_names = final_apply.split("#", 1)[0].split("|")
    assert "[vgt] Guitar Ref — detail (MIDI)" not in final_names
    assert "[vgt] Guitar Ref — clean (MIDI)" not in final_names
    assert final_names.count("[work] Guitar Ref — clean (MIDI)") == 1
    final_sidecar = read_sidecar(project)
    assert _user_snapshot(
        project,
        state,
        managed_tracks=final_sidecar["managed_track_guids"],
        managed_regions=final_sidecar["managed_region_ids"],
    ) == work_snapshot
    # The working-copy snapshot augments, rather than changes, the original
    # user state captured before vgt initialized the project.
    assert "The Seven Rivers (Full March - 3_00)" in original_user_state


def test_goal_contract_reconciles_pyin_bass_basic_pitch_comparison_and_drum_targets(
    tmp_path: Path, deterministic_detectors: None,
) -> None:
    """Prove the delivered multi-target transcription contract end to end.

    The three deterministic fakes are deliberately distinct: default bass
    must use pYIN, while its retained `bass-basic-pitch` comparison variant
    and guitar use Basic Pitch, and drums use DrumScript. Applying the
    resulting sidecar through the real ReaScript fixture proves each MIDI
    track stays adjacent to its own stem rather than following request order.
    """
    project = _copy_project(tmp_path)
    state = _lua_state(project)
    state, _ = _run_apply(project, state)
    initialized = read_sidecar(project)
    original_user_state = _user_snapshot(
        project, state,
        managed_tracks=initialized["managed_track_guids"],
        managed_regions=initialized["managed_region_ids"],
    )
    separator = CountingSeparator()
    separate(project, separator, guitar_type="electric")
    basic_pitch = CountingTranscriber()
    pyin = CountingPyinTranscriber()
    drumscript = CountingTranscriber()
    router = TargetTranscriberRouter(
        basic_pitch=basic_pitch,
        pyin=pyin,
        drumscript=drumscript,
        drumscript_targets=("drums",),
    )

    guitar_variant = add_variant(project, "guitar", label="lead", profile="default", router=router)
    bass_variant = add_variant(project, "bass", label="low-end", profile="default", router=router)
    bass_basic_pitch_variant = add_variant(
        project, "bass", label="comparison", profile="bass-basic-pitch", router=router,
    )
    drums_variant = add_variant(project, "drums", label="kit", profile="default", router=router)
    sidecar = read_sidecar(project)
    targets = sidecar["analysis"]["transcription"]["targets"]
    guitar_id = targets["guitar"]["variant_order"][0]
    bass_id, bass_basic_pitch_id = targets["bass"]["variant_order"]
    drums_id = targets["drums"]["variant_order"][0]

    # Default bass uses its dedicated tracker. Its Basic Pitch comparison
    # remains available, but backend-specific identities must keep their raw
    # detections distinct even though both read the same bass stem.
    assert separator.calls == 5
    assert (basic_pitch.raw_calls, basic_pitch.calls, pyin.raw_calls, pyin.calls, drumscript.calls) == (2, 0, 1, 0, 1)
    assert guitar_variant["backend"] == "basic-pitch"
    assert bass_variant["backend"] == "pyin"
    assert bass_basic_pitch_variant["backend"] == "basic-pitch"
    assert bass_variant["detection_hash"] != bass_basic_pitch_variant["detection_hash"]
    assert drums_variant["backend"] == "drumscript"
    assert "selected_variant_id" not in targets["guitar"]
    assert "selected_variant_id" not in targets["bass"]
    assert "selected_variant_id" not in targets["drums"]
    detection_cache = sidecar["analysis"]["transcription"]["detection_cache"]
    assert set(detection_cache) == {
        guitar_variant["detection_hash"], bass_variant["detection_hash"], bass_basic_pitch_variant["detection_hash"],
    }

    namespace_dir = artifact_namespace_dir(project, sidecar["analysis"]["stems"]["artifact_namespace"])
    guitar_midi = namespace_dir / guitar_variant["midi_file"]
    bass_midi = namespace_dir / bass_variant["midi_file"]
    bass_notes = namespace_dir / bass_variant["notes_file"]
    bass_raw = detection_cache[bass_variant["detection_hash"]]
    bass_raw_paths = [namespace_dir / bass_raw[key] for key in ("raw_midi_file", "raw_notes_file")]
    bass_basic_pitch_midi = namespace_dir / bass_basic_pitch_variant["midi_file"]
    drums_midi = namespace_dir / drums_variant["midi_file"]
    assert guitar_midi.is_file() and bass_midi.is_file() and bass_notes.is_file() and bass_basic_pitch_midi.is_file() and drums_midi.is_file()
    assert all(path.is_file() for path in bass_raw_paths)

    # Reconciliation preserves both pYIN's detection/cleanup artifacts and
    # the comparison candidate without another backend invocation.
    analyze(project, stages=("transcription",), transcription_targets=("bass",), transcriber_router=router)
    assert (basic_pitch.raw_calls, pyin.raw_calls) == (2, 1)
    assert bass_midi.is_file() and bass_notes.is_file() and bass_basic_pitch_midi.is_file()
    assert all(path.is_file() for path in bass_raw_paths)

    # Stem order is bass, drums, guitar in the fixture's managed block; MIDI
    # must follow each stem immediately, not the order variants were added.
    state, first_apply = _run_apply(project, state)
    names = first_apply.split("#", 1)[0].split("|")
    assert names.index("[vgt] Bass Ref — low-end (MIDI)") == names.index("[vgt] Bass") + 1
    assert names.index("[vgt] Bass Ref — comparison (MIDI)") == names.index("[vgt] Bass") + 2
    for stem, midi in (
        ("[vgt] Drums", "[vgt] Drums Ref — kit (MIDI)"),
        ("[vgt] Guitar", "[vgt] Guitar Ref — lead (MIDI)"),
    ):
        assert names.count(midi) == 1
        assert names.index(midi) == names.index(stem) + 1
    state, second_apply = _run_apply(project, state)
    second_names = second_apply.split("#", 1)[0].split("|")
    assert second_names == names

    # These intentionally user-owned copies cover both the target being
    # discarded and the targets that must survive that cleanup.
    applied_sidecar = read_sidecar(project)
    assert _user_snapshot(
        project,
        state,
        managed_tracks=applied_sidecar["managed_track_guids"],
        managed_regions=applied_sidecar["managed_region_ids"],
    ) == original_user_state
    state, _ = _run(project, state, "", """
for _, name in ipairs({
  '[work] Guitar Ref — lead (MIDI)', '[work] Bass Ref — low-end (MIDI)', '[work] Drums Ref — kit (MIDI)'
}) do
  table.insert(tracks, {guid=name, name=name, B_MUTE=0, items={{position=10,length=1,C_LOCK=0,take={name='edited by user',source=''}}}})
end
""")
    work_snapshot = _user_snapshot(
        project,
        state,
        managed_tracks=applied_sidecar["managed_track_guids"],
        managed_regions=applied_sidecar["managed_region_ids"],
    )

    # Discarding the default pYIN candidate is target-local: it removes only
    # its detection/cleanup artifacts and never touches its retained Basic
    # Pitch comparison, guitar/drums artifacts, or user-owned working copies.
    discard_variant(project, "bass", bass_id)
    assert not bass_midi.exists() and not bass_notes.exists()
    assert not any(path.exists() for path in bass_raw_paths)
    assert guitar_midi.is_file() and bass_basic_pitch_midi.is_file() and drums_midi.is_file()
    after_discard = read_sidecar(project)
    after_targets = after_discard["analysis"]["transcription"]["targets"]
    assert after_targets["bass"]["variant_order"] == [bass_basic_pitch_id]
    assert "selected_variant_id" not in after_targets["guitar"]
    assert "selected_variant_id" not in after_targets["drums"]
    assert set(after_discard["analysis"]["transcription"]["detection_cache"]) == {
        guitar_variant["detection_hash"], bass_basic_pitch_variant["detection_hash"],
    }

    # Purging the discarded pYIN audit is similarly local: retained targets
    # still have their artifacts and cache, while every user-owned object is
    # byte-for-byte identical to the snapshot taken after working copies.
    purge_discarded(project, "bass")
    after_purge = read_sidecar(project)
    assert after_purge["analysis"]["transcription"]["targets"]["bass"]["discarded_variants"] == []
    assert guitar_midi.is_file() and bass_basic_pitch_midi.is_file() and drums_midi.is_file()
    assert set(after_purge["analysis"]["transcription"]["detection_cache"]) == {
        guitar_variant["detection_hash"], bass_basic_pitch_variant["detection_hash"],
    }
    state, final_apply = _run_apply(project, state)
    final_names = final_apply.split("#", 1)[0].split("|")
    assert "[vgt] Bass Ref — low-end (MIDI)" not in final_names
    for name in (
        "[vgt] Guitar Ref — lead (MIDI)", "[vgt] Bass Ref — comparison (MIDI)", "[vgt] Drums Ref — kit (MIDI)",
        "[work] Guitar Ref — lead (MIDI)", "[work] Bass Ref — low-end (MIDI)", "[work] Drums Ref — kit (MIDI)",
    ):
        assert final_names.count(name) == 1
    state, reapplied = _run_apply(project, state)
    assert reapplied.split("#", 1)[0].split("|") == final_names
    final_sidecar = read_sidecar(project)
    assert _user_snapshot(
        project,
        state,
        managed_tracks=final_sidecar["managed_track_guids"],
        managed_regions=final_sidecar["managed_region_ids"],
    ) == work_snapshot


def test_goal_contract_exercises_every_remaining_basic_pitch_target_end_to_end(
    tmp_path: Path, deterministic_detectors: None,
) -> None:
    """Keep every advertised non-drum target on the complete artifact path.

    Guitar and the retained bass comparison already exercise this path above.
    This deliberately adds the remaining Basic Pitch targets in the opposite of their presentation
    order, so the assertions cover sidecar source association and immutable
    artifact identity as well as the ReaScript's stem-adjacent ordering.  The
    original mix is the important exception: it has no generated stem and
    must land safely after the imported stem block.
    """
    project = _copy_project(tmp_path)
    state = _lua_state(project)
    state, _ = _run_apply(project, state)
    initialized = read_sidecar(project)
    original_user_state = _user_snapshot(
        project,
        state,
        managed_tracks=initialized["managed_track_guids"],
        managed_regions=initialized["managed_region_ids"],
    )

    separator = CountingSeparator()
    separate(project, separator, guitar_type="electric", optional_stems=("strings", "piano"))
    transcriber = CountingTranscriber()
    router = TargetTranscriberRouter(
        basic_pitch=transcriber,
        drumscript=CountingTranscriber(),
        drumscript_targets=("drums",),
    )
    target_labels = {
        "vocals": "voice", "instrumental": "band", "backing": "without-guitar",
        "strings": "orchestra", "piano": "keys", "original": "mix",
    }
    # Reversed request order must not become the display/import order.
    added = {
        target: add_variant(project, target, label=target_labels[target], profile="default", router=router)
        for target in ("original", "piano", "strings", "backing", "instrumental", "vocals")
    }
    sidecar = read_sidecar(project)
    transcription = sidecar["analysis"]["transcription"]
    targets = transcription["targets"]
    namespace_dir = artifact_namespace_dir(project, sidecar["analysis"]["stems"]["artifact_namespace"])

    # Every target has exactly one opaque, stable identity. Labels are only
    # presentation, never part of the exact derived artifact paths.
    variant_ids = {target: targets[target]["variant_order"][:] for target in target_labels}
    assert all(len(ids) == 1 for ids in variant_ids.values())
    assert len({ids[0] for ids in variant_ids.values()}) == len(variant_ids)
    for target, label in target_labels.items():
        variant_id = variant_ids[target][0]
        variant = targets[target]["variants"][variant_id]
        assert variant == added[target]
        assert variant["source_role"] == target
        assert variant["backend"] == "basic-pitch"
        assert variant["status"] == "transcribed"
        assert variant["midi_file"] == f"transcription/{target}/{variant_id}.mid"
        assert variant["notes_file"] == f"transcription/{target}/{variant_id}.csv"
        assert (namespace_dir / variant["midi_file"]).is_file()
        assert (namespace_dir / variant["notes_file"]).is_file()
        assert variant["label"] == label
    assert transcriber.raw_calls == len(target_labels)

    stems = sidecar["analysis"]["stems"]["artifacts"]
    for target in ("vocals", "instrumental", "backing", "strings", "piano"):
        assert targets[target]["variants"][variant_ids[target][0]]["input_hash"] == stems[target]["sha256"]
    assert targets["original"]["variants"][variant_ids["original"][0]]["input_hash"] != stems["vocals"]["sha256"]

    # Apply twice, then reconcile the retained requests. Neither route may
    # change their IDs, paths, or presentation order.
    state, first_apply = _run_apply(project, state)
    names = first_apply.split("#", 1)[0].split("|")
    for stem, midi in (
        ("[vgt] Vocals", "[vgt] Vocals Ref — voice (MIDI)"),
        ("[vgt] Instrumental", "[vgt] Instrumental Ref — band (MIDI)"),
        ("[vgt] Backing (no guitar)", "[vgt] Backing (no guitar) Ref — without-guitar (MIDI)"),
        ("[vgt] Strings", "[vgt] Strings Ref — orchestra (MIDI)"),
        ("[vgt] Keys / Piano", "[vgt] Keys / Piano Ref — keys (MIDI)"),
    ):
        assert names.count(midi) == 1
        assert names.index(midi) == names.index(stem) + 1
    original_midi = "[vgt] Original Ref — mix (MIDI)"
    assert names.count(original_midi) == 1
    assert names.index(original_midi) > max(names.index(f"[vgt] {label}") for label in (
        "Vocals", "Instrumental", "Bass", "Drums", "Guitar", "Backing (no guitar)", "Strings", "Keys / Piano",
    ))
    state, second_apply = _run_apply(project, state)
    assert second_apply.split("#", 1)[0].split("|") == names
    analyze(
        project,
        stages=("transcription",),
        transcriber_router=router,
        transcription_targets=tuple(target_labels),
    )
    reconciled = read_sidecar(project)["analysis"]["transcription"]["targets"]
    assert {target: reconciled[target]["variant_order"] for target in target_labels} == variant_ids
    state, reconciled_apply = _run_apply(project, state)
    assert reconciled_apply.split("#", 1)[0].split("|") == names

    # Discard exactly one candidate. Its derived files and only its raw cache
    # go away; every other advertised target and a user-owned working copy
    # remain byte-for-byte and visually intact after reapply.
    discarded_target = "piano"
    discarded_id = variant_ids[discarded_target][0]
    discarded = targets[discarded_target]["variants"][discarded_id]
    discarded_paths = [namespace_dir / discarded[key] for key in ("midi_file", "notes_file")]
    discarded_cache = transcription["detection_cache"][discarded["detection_hash"]]
    discarded_cache_paths = [namespace_dir / discarded_cache[key] for key in ("raw_midi_file", "raw_notes_file")]
    state, _ = _run(project, state, "", """
table.insert(tracks, {guid='{WORK-EVERY-TARGET}', name='[work] Original Ref — mix (MIDI)', B_MUTE=0,
  items={{position=10,length=1,C_LOCK=0,take={name='edited by user',source=''}}}})
""")
    applied_sidecar = read_sidecar(project)
    work_snapshot = _user_snapshot(
        project, state,
        managed_tracks=applied_sidecar["managed_track_guids"],
        managed_regions=applied_sidecar["managed_region_ids"],
    )
    discard_variant(project, discarded_target, discarded_id)
    assert not any(path.exists() for path in [*discarded_paths, *discarded_cache_paths])
    after_discard = read_sidecar(project)["analysis"]["transcription"]
    assert after_discard["targets"][discarded_target]["variant_order"] == []
    assert discarded["detection_hash"] not in after_discard["detection_cache"]
    for target, ids in variant_ids.items():
        if target != discarded_target:
            variant = after_discard["targets"][target]["variants"][ids[0]]
            assert (namespace_dir / variant["midi_file"]).is_file()
            assert variant["detection_hash"] in after_discard["detection_cache"]
    state, final_apply = _run_apply(project, state)
    final_names = final_apply.split("#", 1)[0].split("|")
    assert "[vgt] Keys / Piano Ref — keys (MIDI)" not in final_names
    assert final_names.count("[work] Original Ref — mix (MIDI)") == 1
    final_sidecar = read_sidecar(project)
    assert _user_snapshot(
        project,
        state,
        managed_tracks=final_sidecar["managed_track_guids"],
        managed_regions=final_sidecar["managed_region_ids"],
    ) == work_snapshot
    assert "The Seven Rivers (Full March - 3_00)" in original_user_state


def test_goal_contract_retains_and_discards_adtof_beside_drumscript_offline(
    tmp_path: Path, deterministic_detectors: None,
) -> None:
    """The opt-in backend is a peer lifecycle, not a replacement or fallback."""
    assert not any(name == "torch" or name.startswith("torch.") for name in sys.modules)
    project = _copy_project(tmp_path)
    state = _lua_state(project)
    state, _ = _run_apply(project, state)
    separate(project, CountingSeparator(), guitar_type="electric")

    drumscript = CountingTranscriber()
    router = TargetTranscriberRouter(
        basic_pitch=CountingTranscriber(), drumscript=drumscript,
        drumscript_targets=("drums",), adtof=FakeAdtofTranscriber(),
    )
    baseline = add_variant(project, "drums", label="baseline", profile="default", router=router)
    sidecar = read_sidecar(project)
    namespace = artifact_namespace_dir(project, sidecar["analysis"]["stems"]["artifact_namespace"])
    baseline_path = namespace / baseline["midi_file"]
    baseline_bytes = baseline_path.read_bytes()

    # `add` reconciles immediately.  Retaining the alternative must neither
    # re-run nor alter DrumScript's existing artifact/identity.
    alternative = add_variant(project, "drums", label="adtof", profile="drums-adtof", router=router)
    record = read_sidecar(project)["analysis"]["transcription"]["targets"]["drums"]
    assert drumscript.calls == 1
    assert {variant["backend"] for variant in record["variants"].values()} == {"drumscript", "adtof"}
    assert baseline_path.read_bytes() == baseline_bytes
    assert (namespace / alternative["midi_file"]).is_file()

    state, applied = _run_apply(project, state)
    names = applied.split("#", 1)[0].split("|")
    assert names.count("[vgt] Drums Ref — baseline (MIDI)") == 1
    assert names.count("[vgt] Drums Ref — adtof (MIDI)") == 1

    discard_variant(project, "drums", "adtof")
    final = read_sidecar(project)["analysis"]["transcription"]["targets"]["drums"]
    assert [variant["label"] for variant in final["variants"].values()] == ["baseline"]
    assert baseline_path.read_bytes() == baseline_bytes
    assert not (namespace / alternative["midi_file"]).exists()
    assert not any(name == "torch" or name.startswith("torch.") for name in sys.modules)


def _midi_note_on_qns(path: Path) -> list[float]:
    """Decode note-on ticks without coupling this contract to the writer."""
    data = path.read_bytes()
    ticks_per_quarter = int.from_bytes(data[12:14], "big")
    index = 8 + int.from_bytes(data[4:8], "big")
    track_end = index + 8 + int.from_bytes(data[index + 4:index + 8], "big")
    index += 8
    tick, qns = 0, []
    while index < track_end:
        delta = 0
        while True:
            byte = data[index]
            index += 1
            delta = (delta << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        tick += delta
        status = data[index]
        index += 1
        if status == 0xFF:
            index += 1
            length = data[index]
            index += 1 + length
        elif 0x80 <= status <= 0x9F:
            _note, velocity = data[index:index + 2]
            index += 2
            if 0x90 <= status <= 0x9F and velocity:
                qns.append(tick / ticks_per_quarter)
        else:
            raise AssertionError(f"unexpected MIDI event {status:#x}")
    return qns


def _run_apply_with_midi_source_length(project: Path, state: str, source_length: float) -> tuple[str, str]:
    """Run apply with the fixture's MIDI source reporting its real interval."""
    module = APPLY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    return _run(project, state, module, f"_G.midi_source_length = {source_length}; apply(); report()")


def test_goal_contract_keeps_drumscript_variants_on_the_project_timeline(
    tmp_path: Path, deterministic_detectors: None,
) -> None:
    """Exercise #193/#218 through lifecycle and the real apply script.

    Unlike the older single-event contract, this passes a long zero-anchored,
    slightly skewed DrumScript event grid through reconciliation.  A focused
    backend test cannot prove that retained variants preserve those corrected
    events, are imported safely, and invalidate when their effective grid
    changes.
    """
    project = _copy_project(tmp_path)
    # An untouched fixture map would make apply preserve its user-owned 100
    # BPM marker. This contract needs the analysis grid itself to become the
    # live project grid, just as a newly prepared project does.
    state = _lua_state(project).replace("markers={{time=0,bpm=100,num=4,den=4}}", "markers={}")
    state, _ = _run_apply(project, state)
    initialized = read_sidecar(project)
    original_user_state = _user_snapshot(
        project, state,
        managed_tracks=initialized["managed_track_guids"],
        managed_regions=initialized["managed_region_ids"],
    )
    analyze(project, stages=("tempo",))
    update_analysis(project, lambda current: current["tempo"].__setitem__("value", {
        **current["tempo"]["value"],
        "mode": "piecewise",
        "bpm": 120.004,
        "spans": [{"start_seconds": 0.0, "bpm": 120.004}],
        "beat_times": [TempoSkewedDrumScriptFake.project_downbeat_s + index * (2 * TempoSkewedDrumScriptFake.project_step_s) for index in range(400)],
        "downbeat_offset_seconds": TempoSkewedDrumScriptFake.project_downbeat_s,
    }))
    separate(project, CountingSeparator(), guitar_type="electric")

    drumscript = TempoSkewedDrumScriptFake()
    router = TargetTranscriberRouter(basic_pitch=CountingTranscriber(), drumscript=drumscript, drumscript_targets=("drums",))
    default = add_variant(project, "drums", label="default", profile="default", router=router)
    clean = add_variant(project, "drums", label="clean", profile="drums-clean", router=router)
    sidecar = read_sidecar(project)
    variants = sidecar["analysis"]["transcription"]["targets"]["drums"]["variants"]
    namespace = artifact_namespace_dir(project, sidecar["analysis"]["stems"]["artifact_namespace"])

    # Both retained artifacts must contain the nearest fitted project-grid
    # lines, rather than backend slot indices. At the tail the backend clock
    # has advanced a whole eighth: index mapping would put it one slot late.
    # This reads the persisted event artifact, proving the variant lifecycle
    # retained the reconciled result rather than merely testing the fake.
    assert drumscript.calls == 2
    expected_times = [
        TempoSkewedDrumScriptFake.project_downbeat_s + index * TempoSkewedDrumScriptFake.project_step_s
        for index in range(TempoSkewedDrumScriptFake.event_count)
    ]
    for variant in (default, clean):
        assert variant["backend_tempo"] == pytest.approx(60.1)
        assert variant["midi_tempo"] == pytest.approx(120.004)
        persisted_events = json.loads((namespace / variant["events_file"]).read_text(encoding="utf-8"))
        assert [event["time_sec"] for event in persisted_events] == pytest.approx(expected_times, abs=1e-6)
        assert _midi_note_on_qns(namespace / variant["midi_file"]) == pytest.approx(
            [time * 120.004 / 60.0 for time in expected_times], abs=1 / 480
        )
    tail = TempoSkewedDrumScriptFake.event_count - 1
    # The raw tail rounds to backend slot 640, while its event position is
    # nearest project line 639.  Backend-index mapping would be a whole
    # subdivision late, despite drift already exceeding half a subdivision.
    raw_tail = round(expected_times[tail] / TempoSkewedDrumScriptFake.backend_step_s) * TempoSkewedDrumScriptFake.backend_step_s
    backend_slot = round(raw_tail / TempoSkewedDrumScriptFake.backend_step_s)
    assert backend_slot == tail + 1
    assert expected_times[tail] != pytest.approx(
        TempoSkewedDrumScriptFake.project_downbeat_s + backend_slot * TempoSkewedDrumScriptFake.project_step_s
    )
    assert abs(
        tail * TempoSkewedDrumScriptFake.backend_step_s - expected_times[tail]
    ) > TempoSkewedDrumScriptFake.project_step_s / 2

    state, first_apply = _run_apply_with_midi_source_length(project, state, drumscript.source_interval_s)
    names = first_apply.split("#", 1)[0].split("|")
    assert names.count("[vgt] Drums Ref — default (MIDI)") == 1
    assert names.count("[vgt] Drums Ref — clean (MIDI)") == 1
    _, item_report = _run(project, state, "", """
for _, track in ipairs(tracks) do
  if track.name == '[vgt] Drums Ref — default (MIDI)' or track.name == '[vgt] Drums Ref — clean (MIDI)' then
    local item = track.items[1]
    io.write(track.name .. ':' .. item.position .. ':' .. item.length .. '|')
  end
end
""")
    # The item spans the reference track (10..14), *not* the long MIDI
    # source reports: for a MIDI source that number is quarter notes rather
    # than seconds, and it stops at the last note. Timing is proven by the
    # authored QN above, not by the item's length.
    assert item_report.split("|")[:2] == [
        "[vgt] Drums Ref — default (MIDI):10:4",
        "[vgt] Drums Ref — clean (MIDI):10:4",
    ]
    applied_sidecar = read_sidecar(project)
    assert _user_snapshot(
        project, state,
        managed_tracks=applied_sidecar["managed_track_guids"],
        managed_regions=applied_sidecar["managed_region_ids"],
    ) == original_user_state

    state, _ = _run(project, state, "", """
table.insert(tracks, {guid='work-drums', name='[work] Drums Ref — default (MIDI)', B_MUTE=0,
  items={{position=10,length=150.1,C_LOCK=0,take={name='edited by user',source=''}}}})
""")
    work_snapshot = _user_snapshot(
        project, state,
        managed_tracks=applied_sidecar["managed_track_guids"],
        managed_regions=applied_sidecar["managed_region_ids"],
    )
    state, second_apply = _run_apply_with_midi_source_length(project, state, drumscript.source_interval_s)
    second_names = second_apply.split("#", 1)[0].split("|")
    assert second_names.count("[vgt] Drums Ref — default (MIDI)") == 1
    assert second_names.count("[vgt] Drums Ref — clean (MIDI)") == 1
    assert second_names.count("[work] Drums Ref — default (MIDI)") == 1
    state, reapplied = _run_apply_with_midi_source_length(project, state, drumscript.source_interval_s)
    assert reapplied.split("#", 1)[0].split("|") == second_names
    final_sidecar = read_sidecar(project)
    assert _user_snapshot(
        project, state,
        managed_tracks=final_sidecar["managed_track_guids"],
        managed_regions=final_sidecar["managed_region_ids"],
    ) == work_snapshot

    # Changing the detected project grid changes the variant spec identity;
    # analyze must re-author the affected MIDI instead of accepting the old
    # 120-BPM-only bytes as current. The first retained variant is the
    # compatibility target refreshed by analyze's existing lifecycle rule.
    before = (namespace / default["midi_file"]).read_bytes()
    before_qns = _midi_note_on_qns(namespace / default["midi_file"])
    before_hash = variants[next(iter(variants))]["settings_hash"]
    update_analysis(project, lambda current: current["tempo"].__setitem__("value", {
        **current["tempo"]["value"], "spans": [
            {"start_seconds": 0.0, "bpm": 120.004}, {"start_seconds": 100.0, "bpm": 60.0},
        ], "beat_times": [0.1 + index * 0.501 for index in range(400)], "downbeat_offset_seconds": 0.1,
    }))
    analyze(project, stages=("transcription",), transcriber_router=router)
    refreshed = read_sidecar(project)["analysis"]["transcription"]["targets"]["drums"]["variants"]
    refreshed_default = refreshed[next(iter(refreshed))]
    after = (namespace / refreshed_default["midi_file"]).read_bytes()
    assert drumscript.calls == 3
    assert refreshed_default["settings_hash"] != before_hash
    assert after != before
    assert _midi_note_on_qns(namespace / refreshed_default["midi_file"]) != before_qns


def test_goal_contract_tempo_map_sync_drives_variable_grid_without_touching_user_map(
    tmp_path: Path, deterministic_detectors: None,
) -> None:
    project = _copy_project(tmp_path)
    state = _lua_state(project).replace(
        "markers={{time=0,bpm=100,num=4,den=4}}",
        "markers={{time=0,bpm=100,num=4,den=4},{time=10,bpm=120,num=4,den=4},{time=13,bpm=90,num=4,den=4},{time=18,bpm=160,num=4,den=4}}",
    )
    state, _ = _run_apply(project, state)
    initialized = read_sidecar(project)
    before = _user_snapshot(
        project, state, managed_tracks=initialized["managed_track_guids"], managed_regions=initialized["managed_region_ids"],
    )
    analyze(project, stages=("tempo",))

    state = _run_tempo_map_sync(project, state)
    synced = read_sidecar(project)
    tempo = synced["analysis"]["tempo"]
    assert tempo["human_verified"] is True
    assert tempo["detected"]["bpm"] == 120.0
    assert tempo["value"]["spans"] == [{"start_seconds": 3, "bpm": 90, "time_signature": "4/4"}]

    separate(project, CountingSeparator(), guitar_type="electric")
    drumscript = TempoSkewedDrumScriptFake()
    router = TargetTranscriberRouter(basic_pitch=CountingTranscriber(), drumscript=drumscript, drumscript_targets=("drums",))
    variant = add_variant(project, "drums", label="map", profile="default", router=router)
    namespace = artifact_namespace_dir(project, read_sidecar(project)["analysis"]["stems"]["artifact_namespace"])
    # 3 seconds at 120 BPM (6 QN), then 147 seconds at 90 BPM (220.5 QN).
    assert _midi_note_on_qns(namespace / variant["midi_file"]) == pytest.approx([226.5])

    # Sync itself has no REAPER marker mutator, and a following apply treats
    # this map as human-owned rather than refreshing it.
    state, report = _run_apply(project, state)
    assert report.split("#")[-1] == "0"
    applied = read_sidecar(project)
    assert _user_snapshot(project, state, managed_tracks=applied["managed_track_guids"], managed_regions=applied["managed_region_ids"]) == before


def test_goal_contract_sync_survives_a_concurrent_analyze_commit(tmp_path: Path, deterministic_detectors: None) -> None:
    """Extends the goal contract for the shared sidecar commit protocol
    (#138): a `vgt analyze` commit that lands in the narrow window between
    vgt_sync.lua's fresh read and its pre-rename generation check must not be
    silently discarded, and the human chord/section corrections vgt_sync.lua
    is committing in that same call must not be lost either -- both writers'
    changes must be present together in the final sidecar."""
    project = _copy_project(tmp_path)
    state = _lua_state(project)

    state, _ = _run_apply(project, state)
    analyze(project, stages=("tempo", "key", "sections", "chords"))
    # A second apply is what actually builds the `[vgt] Chords` track from
    # the analysis just committed -- the first apply ran before it existed.
    state, _ = _run_apply(project, state)
    before_bpm = read_sidecar(project)["analysis"]["tempo"]["value"]["bpm"]

    sync_module = SYNC_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    program = """
for _, track in ipairs(tracks) do
  if track.name == '[vgt] Chords' then track.items = {{position=10.25,length=0.75,take={name='Dm'}}} end
end

local sidecar_file = sidecar_path()
local real_open = io.open
local read_count = 0
io.open = function(path, mode)
  if path == sidecar_file and mode == "r" then
    read_count = read_count + 1
    if read_count == 2 then
      -- Simulates a concurrent `vgt analyze` commit landing in the gap
      -- between write_sync's fresh read and its pre-rename check: it bumps
      -- `generation` and refreshes tempo, a stage sync() never itself reads.
      local body = real_open(sidecar_file, "r"):read("*a")
      local data = decode_json(body)
      data.generation = (data.generation or 0) + 1
      data.analysis.tempo.value.bpm = 141.0
      local f = real_open(sidecar_file, "w")
      f:write(encode_json(data))
      f:close()
    end
  end
  return real_open(path, mode)
end

sync()
"""
    state, _ = _run(project, state, sync_module, program)

    synced = read_sidecar(project)
    # vgt_sync.lua's own change: the human chord correction.
    assert synced["analysis"]["chords"]["value"]["segments"][0]["chord"] == "Dm"
    assert synced["analysis"]["chords"]["human_verified"] is True
    # The concurrently-committed analyze() change, never itself read by sync().
    assert synced["analysis"]["tempo"]["value"]["bpm"] == 141.0
    assert synced["analysis"]["tempo"]["value"]["bpm"] != before_bpm
    assert synced["generation"] >= 3


def test_apply_recovers_managed_regions_after_an_interrupted_sidecar_commit(tmp_path: Path, deterministic_detectors: None) -> None:
    """Simulates a crash between building the `[vgt]` section regions and the
    final sidecar write: apply is interrupted right as write_settings tries to
    open its temp file, after every section region has already been created
    via AddProjectMarker2 -- exactly the window issue #137 is about. The
    sidecar is left stale (no record of the two new regions), but the durable
    ProjExtState record (see record_region_ids_ext_state in
    vgt_initialize.lua) still has them. Re-running apply must reconcile using
    the union of that stale sidecar and ProjExtState, producing exactly one
    managed region inventory rather than appending a duplicate block, while
    every user region remains byte-for-byte unchanged."""
    project = _copy_project(tmp_path)
    state = _lua_state(project)
    state, _ = _run_apply(project, state)
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID
    assert read_sidecar(project)["managed_region_ids"] == []
    initialized = read_sidecar(project)
    before = _user_snapshot(
        project, state,
        managed_tracks=initialized["managed_track_guids"],
        managed_regions=initialized["managed_region_ids"],
    )

    analyze(project, stages=("tempo", "key", "sections"))

    module = APPLY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    failure_injection = """
local real_open = io.open
io.open = function(path, mode)
  if mode == "w" and path:sub(-4) == ".tmp" then return nil, "simulated disk failure" end
  return real_open(path, mode)
end
local ok, err = pcall(apply)
assert(not ok, "apply should have failed")
assert(tostring(err):find("simulated disk failure"), tostring(err))
io.open = real_open
report()
"""
    state, interrupted = _run(project, state, module, failure_injection)
    _names, _user_items, region_count, _vgt_count, _tempo_writes = interrupted.split("#")
    # The user's pre-existing region plus both new section regions, created
    # in the live project but never committed to the sidecar.
    assert region_count == "3"

    # write_settings never got to rename its temp file into place, so the
    # sidecar on disk is exactly as stale as before this apply attempt.
    stale_sidecar = read_sidecar(project)
    assert stale_sidecar["managed_region_ids"] == []

    state, recovered = _run_apply(project, state)
    _names, _user_items, region_count, _vgt_count, _tempo_writes = recovered.split("#")
    assert region_count == "3"

    _, output = _run(project, state, "", r"""
local seen_names, managed = {}, 0
for _, region in ipairs(regions) do
  if region.name:sub(1, 5) == '[vgt]' then
    assert(not seen_names[region.name], 'duplicate managed region: ' .. region.name)
    seen_names[region.name], managed = true, managed + 1
  end
end
assert(managed == 2, 'expected exactly 2 managed regions, got ' .. managed)
assert(seen_names['[vgt] Verse'] and seen_names['[vgt] Chorus'], 'missing an expected managed region')
io.write('ok')
""")
    assert output == "ok"

    final_sidecar = read_sidecar(project)
    assert final_sidecar["managed_region_ids"] != []
    assert _user_snapshot(
        project,
        state,
        managed_tracks=final_sidecar["managed_track_guids"],
        managed_regions=final_sidecar["managed_region_ids"],
    ) == before


def test_second_apply_reuses_the_persisted_reference_without_prompting_in_a_multi_track_project(tmp_path: Path) -> None:
    """The fixture has two non-[vgt] file-backed tracks (The Seven Rivers and
    Paris Metro Punk), so a fresh selection would have to ask. Once the GUID
    is persisted by a first apply, a later apply must reuse it without ever
    touching the automation override or the gfx menu -- neither is defined in
    this stripped-down state, so any attempt to consult them fails loudly."""
    project = _copy_project(tmp_path)
    state = _lua_state(project)
    state, _ = _run_apply(project, state)
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID

    state_without_picker = state.replace(
        "function reaper.GetExtState(_,key) return key == 'reference_index' and '0' or 'electric' end",
        "function reaper.GetExtState(_,key) return key == 'guitar_type' and 'electric' or '' end",
    )
    state, second_apply = _run_apply(project, state_without_picker)
    _names, user_items, _region_count, _vgt_count, _tempo_writes = second_apply.split("#")
    assert user_items == "2"
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID


def test_reascript_uses_beats_not_a_tempo_map_when_bar_phase_is_unknown(tmp_path: Path) -> None:
    project = _copy_project(tmp_path)
    project.with_suffix(".vgt").write_text(json.dumps({
        "config": {"reference_track_guid": REFERENCE_GUID},
        "analysis": {"tempo": {"value": {
            "backend": "librosa", "bpm": 120.0, "time_signature": "4/4",
            "downbeat_detected": False, "downbeat_offset_seconds": None,
            "beat_times": [0.25, 0.78, 1.29],
        }}},
    }))
    # A default map would otherwise be eligible for vgt ownership.
    state = _lua_state(project).replace("time=0,bpm=100,num=4,den=4", "time=0,bpm=120,num=4,den=4")
    _, result = _run_apply(project, state)
    names, _user_items, _regions, _vgt, tempo_writes = result.split("#")

    assert "[vgt] Beats" in names
    assert tempo_writes == "0"
    assert read_sidecar(project)["config"]["tempo_map_applied"] is False


def test_reascript_keeps_tempo_map_behavior_for_detected_downbeats(tmp_path: Path) -> None:
    project = _copy_project(tmp_path)
    project.with_suffix(".vgt").write_text(json.dumps({
        "config": {"reference_track_guid": REFERENCE_GUID},
        "analysis": {"tempo": {"value": {
            "backend": "madmom", "bpm": 120.0, "time_signature": "4/4",
            "downbeat_detected": True, "downbeat_offset_seconds": 0.25,
            "beat_times": [0.25, 0.75, 1.25],
        }}},
    }))
    state = _lua_state(project).replace("time=0,bpm=100,num=4,den=4", "time=0,bpm=120,num=4,den=4")
    _, result = _run_apply(project, state)
    names, _user_items, _regions, _vgt, tempo_writes = result.split("#")

    assert "[vgt] Beats" not in names
    assert int(tempo_writes) >= 2
    assert read_sidecar(project)["config"]["tempo_map_applied"] is True


TEMPO_MAP_SIDECAR = {
    "config": {"reference_track_guid": REFERENCE_GUID},
    "analysis": {"tempo": {"value": {
        "backend": "madmom", "bpm": 120.0, "time_signature": "4/4",
        "downbeat_detected": True, "downbeat_offset_seconds": 0.25,
        "beat_times": [0.25, 0.75, 1.25],
    }}},
}


def _faithful_tempo_marker_state(project: Path) -> str:
    """`_lua_state`'s SetTempoTimeSigMarker collapses every call into a
    single-marker table (fine for the goal contract's coarse `tempo_writes`
    counter elsewhere in this file), which is too lossy to prove an
    interrupted tempo map is recovered without duplicate markers. Patch in an
    index-aware implementation -- mirroring REAPER's own ptidx=0 update /
    ptidx=-1 insert-by-time semantics -- for the interruption-recovery tests
    below."""
    state = _lua_state(project).replace("time=0,bpm=100,num=4,den=4", "time=0,bpm=120,num=4,den=4")
    patched = state.replace(
        "function reaper.SetTempoTimeSigMarker(_,_,time,_,_,bpm,num,den) tempo_writes=tempo_writes+1; markers={{time=time,bpm=bpm,num=num,den=den}} end",
        """function reaper.SetTempoTimeSigMarker(_, index, time, _, _, bpm, num, den)
  tempo_writes = tempo_writes + 1
  if index == 0 then
    markers[1] = {time = 0, bpm = bpm, num = num, den = den}
    return true
  end
  local marker = {time = time, bpm = bpm, num = num, den = den}
  local insert_at = #markers + 1
  for i = 2, #markers do
    if markers[i].time > time then insert_at = i break end
  end
  table.insert(markers, insert_at, marker)
  return true
end""",
    )
    assert patched != state, "SetTempoTimeSigMarker patch did not match the fixture source"
    return patched


def _interrupt_before_sidecar_commit(project: Path, state: str) -> tuple[str, str]:
    """Runs apply() with write_settings's temp-file write failing (the same
    disk-failure injection `test_apply_recovers_managed_regions_after_an_interrupted_sidecar_commit`
    uses for regions), after the tempo mutation itself has already completed
    against the live (fake) REAPER project. Returns the report plus the live
    marker count."""
    module = APPLY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    program = """
local real_open = io.open
io.open = function(path, mode)
  if mode == "w" and path:sub(-4) == ".tmp" then return nil, "simulated disk failure" end
  return real_open(path, mode)
end
local ok, err = pcall(apply)
assert(not ok, "apply should have failed")
assert(tostring(err):find("simulated disk failure"), tostring(err))
io.open = real_open
report()
io.write('#' .. #markers)
"""
    return _run(project, state, module, program)


def test_apply_recovers_a_completed_tempo_map_after_an_interrupted_sidecar_commit(tmp_path: Path) -> None:
    """Simulates a crash between a tempo mutation finishing and write_settings
    committing it (issue #139): apply_tempo_map has already written every
    marker in the live (fake) REAPER project when the disk failure hits, but
    the sidecar is left exactly as stale as before this attempt -- the bug
    described in #139, where the next run would see a non-default map with
    no ownership proof, leave it orphaned, and add `[vgt] Beats` on top
    regardless. The transaction recorded in ProjExtState before the mutation
    began proves the live map -- already correct -- belongs to this vgt
    transaction, so a retry mirrors it into the sidecar without touching
    REAPER's tempo markers again, and without ever offering Beats."""
    project = _copy_project(tmp_path)
    project.with_suffix(".vgt").write_text(json.dumps(TEMPO_MAP_SIDECAR))
    state = _faithful_tempo_marker_state(project)

    state, interrupted = _interrupt_before_sidecar_commit(project, state)
    _names, _user_items, _region_count, _vgt_count, tempo_writes, marker_count = interrupted.split("#")
    # index 0 updated in place, plus one inserted downbeat marker -- no spans.
    assert int(tempo_writes) >= 2
    assert marker_count == "2"

    stale_sidecar = read_sidecar(project)
    assert stale_sidecar["config"].get("tempo_map_applied") is not True

    module = APPLY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    state, recovered = _run(project, state, module, "apply(); report(); io.write('#' .. #markers)")
    names, _user_items, _region_count, _vgt_count, tempo_writes_after, marker_count_after = recovered.split("#")

    assert "[vgt] Beats" not in names
    # Recovery only mirrors the already-correct live map into the sidecar; it
    # must not mutate REAPER's tempo markers a second time.
    assert tempo_writes_after == tempo_writes
    assert marker_count_after == "2"

    final_sidecar = read_sidecar(project)
    assert final_sidecar["config"]["tempo_map_applied"] is True
    assert final_sidecar["config"]["tempo_map_fingerprint"] == "0.000000:120.000:4:4;10.250000:120.000:4:4"
    assert final_sidecar["config"]["tempo_data_fingerprint"] != ""


def test_apply_never_overwrites_a_user_tempo_edit_made_after_an_interrupted_commit(tmp_path: Path) -> None:
    """Same interruption as above, but the user edits the (already-complete,
    vgt-written) live tempo map in REAPER before the next apply runs. Even
    though a vgt transaction for that exact map was left pending, its
    recorded fingerprints no longer match what is live, so ownership cannot
    be proven -- the map must be preserved untouched and `[vgt] Beats`
    offered instead, never silently reverted to what vgt originally wrote."""
    project = _copy_project(tmp_path)
    project.with_suffix(".vgt").write_text(json.dumps(TEMPO_MAP_SIDECAR))
    state = _faithful_tempo_marker_state(project)

    state, interrupted = _interrupt_before_sidecar_commit(project, state)
    _names, _user_items, _region_count, _vgt_count, tempo_writes, _marker_count = interrupted.split("#")

    # The user retunes the downbeat marker by hand in REAPER -- no vgt code
    # runs here, so `tempo_writes` (vgt's own write counter) is untouched.
    state, _ = _run(project, state, "", "markers[2].bpm = 130.5")

    module = APPLY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    state, recovered = _run(project, state, module, "apply(); report(); io.write('#' .. markers[2].bpm)")
    names, _user_items, _region_count, _vgt_count, tempo_writes_after, edited_bpm = recovered.split("#")

    assert "[vgt] Beats" in names
    # Neither the interrupted recovery attempt nor the fallback path touched
    # REAPER's tempo markers -- the write count is exactly what the
    # interrupted run itself already produced.
    assert tempo_writes_after == tempo_writes
    assert edited_bpm == "130.5"

    final_sidecar = read_sidecar(project)
    assert final_sidecar["config"]["tempo_map_applied"] is False
    assert final_sidecar["config"]["tempo_map_fingerprint"] == ""
