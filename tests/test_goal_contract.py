"""Offline executable acceptance contract for :mod:`docs.GOAL`.

This is deliberately one workflow, not a replacement for focused unit tests.
It uses the real RPP fixture, deterministic analysis/separation/transcription
seams, and a small in-memory REAPER API implementation executed by Lua.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from vgt import analysis as analysis_module
from vgt.analysis import analyze
from vgt.cli import main
from vgt.separation import FakeSeparator, separate
from vgt.sidecar import artifact_namespace_dir, read_sidecar
from vgt.transcribe import FakeTranscriber, TargetTranscriberRouter
from vgt.transcription_lifecycle import add_variant, discard_variant, purge_discarded, select_variant


ROOT = Path(__file__).parents[1]
FIXTURE_DIR = ROOT / "test" / "Reaper Project"
APPLY_SCRIPT = ROOT / "reascript" / "vgt_initialize.lua"
SYNC_SCRIPT = ROOT / "reascript" / "vgt_sync.lua"
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
function reaper.SetMediaTrackInfo_Value(t, key, value) t[key]=value end
function reaper.ColorToNative(red, green, blue) return blue * 65536 + green * 256 + red end
function reaper.AddMediaItemToTrack(t) local i={{position=0,length=0}}; table.insert(t.items,i); return i end
function reaper.SetMediaItemInfo_Value(i,key,value) if key == 'D_POSITION' then i.position=value elseif key == 'D_LENGTH' then i.length=value else i[key]=value end end
function reaper.GetSetMediaItemInfo_String(i,_,value) i.notes=value end
function reaper.AddTakeToMediaItem(i) i.take={{}}; return i.take end
function reaper.GetSetMediaItemTakeInfo_String(t,_,value) t.name=value end
function reaper.SetMediaItemTake_Source(t,s) t.source=s end
function reaper.PCM_Source_CreateFromFile(path) return {{path=path}} end
function reaper.GetMediaSourceLength(_) return 1 end
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
  for _, track in ipairs(tracks) do if not managed_tracks[track.guid] then user_tracks[#user_tracks + 1] = track end end
  for _, region in ipairs(regions) do if not managed_regions[region.id] then user_regions[#user_regions + 1] = region end end
  return lua_value({{tracks=user_tracks,regions=user_regions,markers=markers}})
end
function emit_state()
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


def _user_snapshot(project: Path, state: str, *, managed_tracks: list[str] | None = None, managed_regions: list[int] | None = None) -> str:
    """Return a stable byte-for-byte snapshot of every user-owned object."""
    tracks = "{" + ",".join(json.dumps(guid) for guid in managed_tracks or []) + "}"
    regions = "{" + ",".join(str(region_id) for region_id in managed_regions or []) + "}"
    _, snapshot = _run(project, state, "", f"io.write(user_snapshot({tracks}, {regions}))")
    return snapshot


def _assert_managed_contract(project: Path, state: str) -> None:
    """Check the exact vgt inventory in the persistent offline project."""
    _, output = _run(project, state, "", r'''
local expected = {
  ['[vgt] The Seven Rivers (Full March - 3_00)']=true, ['[vgt] Beats']=true, ['[vgt] Click']=true,
  ['[vgt] Key']=true, ['[vgt] Chords']=true, ['[vgt] Vocals']=true,
  ['[vgt] Instrumental']=true, ['[vgt] Bass']=true, ['[vgt] Drums']=true,
  ['[vgt] Guitar']=true, ['[vgt] Backing (no guitar)']=true,
  ['[vgt] Guitar Ref (MIDI)']=true,
}
local expected_sources = {
  ['[vgt] Vocals']='stems/vocals.wav', ['[vgt] Instrumental']='stems/instrumental.wav',
  ['[vgt] Bass']='stems/bass.wav', ['[vgt] Drums']='stems/drums.wav',
  ['[vgt] Guitar']='stems/guitar.wav', ['[vgt] Backing (no guitar)']='stems/backing-no-guitar.wav',
  ['[vgt] Guitar Ref (MIDI)']='transcription/guitar.mid', ['[vgt] Click']='tempo-click.wav',
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
        and track.items[1].C_LOCK == 1, 'key annotation')
    elseif track.name == '[vgt] Chords' then
      assert(#track.items == 1 and track.items[1].position == 10.25 and track.items[1].length == 0.75
        and track.items[1].notes == 'Dm' and track.items[1].take.name == 'Dm' and track.items[1].C_LOCK == nil, 'chord annotations')
    elseif expected_sources[track.name] then
      assert(#track.items == 1 and track.items[1].position == 10 and track.items[1].length == 1,
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
    before = _user_snapshot(project, state)

    # Initialization selects the real fixture's reference identity and writes only a sidecar.
    state, _ = _run_apply(project, state)
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID

    separator, transcriber = CountingSeparator(), CountingTranscriber()
    analyze(project, stages=("tempo", "key", "sections"))
    separate(project, separator, guitar_type="electric")
    analyze(project, stages=("chords", "transcription"), transcription_targets=("guitar",), transcriber=transcriber)
    assert (separator.calls, transcriber.calls) == (5, 1)

    # The existing 100 BPM map must remain untouched, so apply offers beat labels instead.
    state, first_apply = _run_apply(project, state)
    names, user_items, region_count, vgt_count, tempo_writes = first_apply.split("#")
    assert user_items == "2" and region_count == "3" and tempo_writes == "0"
    assert "[vgt] Beats" in names and "[vgt] Key" in names and "[vgt] Guitar Ref (MIDI)" in names
    assert int(vgt_count) == 12  # folder, beats/click/key/chords, six stems, MIDI

    sidecar = read_sidecar(project)
    # These are real edits to the state produced by apply, not a newly-built
    # approximation of it.  Sync must read them while preserving every other object.
    sync_module = SYNC_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    state, _ = _run(project, state, sync_module, """
for _, track in ipairs(tracks) do
  if track.name == '[vgt] Chords' then track.items = {{position=10.25,length=0.75,take={name='Dm'}}} end
end
for _, region in ipairs(regions) do
  if region.id == %d then region.start=10.25; region.finish=11; region.name='[vgt] Bridge' end
end
sync()
""" % sidecar["managed_region_ids"][0])
    synced = read_sidecar(project)
    assert synced["analysis"]["chords"]["value"]["segments"][0]["chord"] == "Dm"
    assert synced["analysis"]["sections"]["value"][0]["label"] == "Bridge"
    detected_chords = synced["analysis"]["chords"]["detected"]
    detected_sections = synced["analysis"]["sections"]["detected"]

    # Forced free re-analysis refreshes detected baselines but preserves human sync edits;
    # paid splits and the target MIDI cache must not run again.
    analyze(project, force=True, stages=("sections", "chords"))
    analyze(project, stages=("transcription",), transcription_targets=("guitar",), transcriber=transcriber)
    separate(project, separator, guitar_type="electric")
    reconciled = read_sidecar(project)
    assert reconciled["analysis"]["chords"]["value"]["segments"][0]["chord"] == "Dm"
    assert reconciled["analysis"]["sections"]["value"][0]["label"] == "Bridge"
    assert reconciled["analysis"]["chords"]["detected"] == detected_chords
    assert reconciled["analysis"]["sections"]["detected"] == detected_sections
    assert (separator.calls, transcriber.calls) == (5, 1)

    # The display is rebuilt from effective `key.value`, so a deliberate
    # sidecar override replaces the old label without adding another track.
    reconciled["analysis"]["key"]["value"] = {"root": "E", "scale": "minor", "backend": "human"}
    reconciled["analysis"]["key"]["human_verified"] = True
    project.with_suffix(".vgt").write_text(json.dumps(reconciled), encoding="utf-8")
    state, second_apply = _run_apply(project, state)
    names, user_items, region_count, vgt_count, tempo_writes = second_apply.split("#")
    assert user_items == "2" and region_count == "3" and tempo_writes == "0"
    assert int(vgt_count) == 12 and names.split("|").count("[vgt] Guitar") == 1
    # The second apply must have reused the original persisted reference
    # rather than re-prompting or drifting onto another candidate (issue #136).
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID
    state, key_snapshot = _run_apply_key_snapshot(project, state)
    assert key_snapshot == "1#0:E minor:1"
    _assert_managed_contract(project, state)
    final_sidecar = read_sidecar(project)
    assert _user_snapshot(
        project,
        state,
        managed_tracks=final_sidecar["managed_track_guids"],
        managed_regions=final_sidecar["managed_region_ids"],
    ) == before


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
    sidecar model, raw-cache sharing, ReaScript apply reconciliation, explicit
    selection/discard semantics, and user-owned working copies agree.
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

    # Apply twice.  The selected candidate persists, each generated track is
    # rebuilt exactly once, and a simulated working copy is outside vgt's
    # durable ownership inventory.
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
    select_variant(project, "guitar", clean_id)
    state, second_apply = _run_apply(project, state)
    second_names = second_apply.split("#", 1)[0].split("|")
    assert second_names.count("[vgt] Guitar Ref — detail (MIDI)") == 1
    assert second_names.count("[vgt] Guitar Ref — clean (MIDI)") == 1
    assert second_names.count("[work] Guitar Ref — clean (MIDI)") == 1
    selected = read_sidecar(project)["analysis"]["transcription"]["targets"]["guitar"]["selected_variant_id"]
    assert selected == clean_id

    namespace_dir = artifact_namespace_dir(project, read_sidecar(project)["analysis"]["stems"]["artifact_namespace"])
    detail_midi = namespace_dir / detail["midi_file"]
    clean_midi = namespace_dir / clean["midi_file"]
    assert detail_midi.is_file() and clean_midi.is_file()

    # Rejecting the unselected candidate removes exactly its derived files but
    # keeps the shared raw cache for the selected clean candidate.  Reapply
    # must remove only that generated track and preserve the editable copy.
    discard_variant(project, "guitar", detail_id)
    assert not detail_midi.exists() and clean_midi.is_file()
    assert len(read_sidecar(project)["analysis"]["transcription"]["detection_cache"]) == 1
    state, after_detail_discard = _run_apply(project, state)
    names_after_detail_discard = after_detail_discard.split("#", 1)[0].split("|")
    assert "[vgt] Guitar Ref — detail (MIDI)" not in names_after_detail_discard
    assert names_after_detail_discard.count("[vgt] Guitar Ref — clean (MIDI)") == 1
    assert names_after_detail_discard.count("[work] Guitar Ref — clean (MIDI)") == 1

    # Discarding the final selected candidate is explicit.  It clears the
    # selection and removes the now-unreferenced raw cache, while leaving both
    # original user tracks and the user's working copy byte-for-byte intact.
    discard_variant(project, "guitar", clean_id, clear_selected=True)
    final_sidecar = read_sidecar(project)
    assert not clean_midi.exists()
    assert final_sidecar["analysis"]["transcription"]["detection_cache"] == {}
    assert final_sidecar["analysis"]["transcription"]["targets"]["guitar"]["selected_variant_id"] is None
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


def test_goal_contract_reconciles_independent_guitar_bass_and_drum_targets(
    tmp_path: Path, deterministic_detectors: None,
) -> None:
    """Prove the delivered multi-target transcription contract end to end.

    The two fake backends are deliberately distinct instances: guitar and
    bass must use the Basic Pitch route (including its raw-detection cache),
    while drums must use the DrumScript route.  Applying the resulting
    sidecar through the real ReaScript fixture proves each MIDI track stays
    adjacent to its own stem rather than following target request order.
    """
    project = _copy_project(tmp_path)
    state = _lua_state(project)
    original_user_state = _user_snapshot(project, state)

    state, _ = _run_apply(project, state)
    separator = CountingSeparator()
    separate(project, separator, guitar_type="electric")
    basic_pitch = CountingTranscriber()
    drumscript = CountingTranscriber()
    router = TargetTranscriberRouter(
        basic_pitch=basic_pitch,
        drumscript=drumscript,
        drumscript_targets=("drums",),
    )

    guitar_variant = add_variant(project, "guitar", label="lead", profile="default", router=router)
    bass_variant = add_variant(project, "bass", label="low-end", profile="default", router=router)
    drums_variant = add_variant(project, "drums", label="kit", profile="default", router=router)
    sidecar = read_sidecar(project)
    targets = sidecar["analysis"]["transcription"]["targets"]
    guitar_id = targets["guitar"]["variant_order"][0]
    bass_id = targets["bass"]["variant_order"][0]
    drums_id = targets["drums"]["variant_order"][0]

    # Guitar and bass are independently detected through Basic Pitch; drums
    # use the separate DrumScript backend and never create a raw-note cache.
    assert separator.calls == 5
    assert (basic_pitch.raw_calls, basic_pitch.calls, drumscript.calls) == (2, 0, 1)
    assert guitar_variant["backend"] == bass_variant["backend"] == "basic-pitch"
    assert drums_variant["backend"] == "drumscript"
    assert targets["guitar"]["selected_variant_id"] == guitar_id
    assert targets["bass"]["selected_variant_id"] == bass_id
    assert targets["drums"]["selected_variant_id"] == drums_id
    detection_cache = sidecar["analysis"]["transcription"]["detection_cache"]
    assert set(detection_cache) == {guitar_variant["detection_hash"], bass_variant["detection_hash"]}

    namespace_dir = artifact_namespace_dir(project, sidecar["analysis"]["stems"]["artifact_namespace"])
    guitar_midi = namespace_dir / guitar_variant["midi_file"]
    bass_midi = namespace_dir / bass_variant["midi_file"]
    drums_midi = namespace_dir / drums_variant["midi_file"]
    assert guitar_midi.is_file() and bass_midi.is_file() and drums_midi.is_file()

    # Stem order is bass, drums, guitar in the fixture's managed block; MIDI
    # must follow each stem immediately, not the order variants were added.
    state, first_apply = _run_apply(project, state)
    names = first_apply.split("#", 1)[0].split("|")
    for stem, midi in (
        ("[vgt] Bass", "[vgt] Bass Ref — low-end (MIDI)"),
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

    # Discarding the selected bass candidate is explicit and target-local.
    # It removes only bass's MIDI/cache and never changes guitar or drums'
    # selections, artifacts, or user-owned working copies.
    discard_variant(project, "bass", bass_id, clear_selected=True)
    assert not bass_midi.exists() and guitar_midi.is_file() and drums_midi.is_file()
    after_discard = read_sidecar(project)
    after_targets = after_discard["analysis"]["transcription"]["targets"]
    assert after_targets["bass"]["selected_variant_id"] is None
    assert after_targets["guitar"]["selected_variant_id"] == guitar_id
    assert after_targets["drums"]["selected_variant_id"] == drums_id
    assert set(after_discard["analysis"]["transcription"]["detection_cache"]) == {guitar_variant["detection_hash"]}

    # Purging the discarded bass audit is similarly local: retained targets
    # still have their artifacts and cache, while every user-owned object is
    # byte-for-byte identical to the snapshot taken after working copies.
    purge_discarded(project, "bass")
    after_purge = read_sidecar(project)
    assert after_purge["analysis"]["transcription"]["targets"]["bass"]["discarded_variants"] == []
    assert guitar_midi.is_file() and drums_midi.is_file()
    assert set(after_purge["analysis"]["transcription"]["detection_cache"]) == {guitar_variant["detection_hash"]}
    state, final_apply = _run_apply(project, state)
    final_names = final_apply.split("#", 1)[0].split("|")
    assert "[vgt] Bass Ref — low-end (MIDI)" not in final_names
    for name in (
        "[vgt] Guitar Ref — lead (MIDI)", "[vgt] Drums Ref — kit (MIDI)",
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
    before = _user_snapshot(project, state)

    state, _ = _run_apply(project, state)
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID
    assert read_sidecar(project)["managed_region_ids"] == []

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
