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

import pytest

from vgt import analysis as analysis_module
from vgt.analysis import analyze
from vgt.separation import FakeSeparator, separate
from vgt.sidecar import artifact_namespace_dir, read_sidecar
from vgt.transcribe import FakeTranscriber


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


def _lua_state(
    project: Path, managed: list[tuple[str, str]] = [], correction: bool = False, managed_region_id: int | None = None
) -> str:
    """An in-memory REAPER project with user objects plus optionally stale vgt objects."""
    chord_items = "{{position=10.25,length=0.75,take={name='Dm'}}}" if correction else "{}"
    managed_lua = ",".join(
        "{guid=%s,name=%s,items=%s}" % (json.dumps(guid), json.dumps(name), chord_items if correction and name == "[vgt] Chords" else "{}")
        for guid, name in managed
    )
    regions = "{id=900,start=11,finish=12,name='User region'}"
    if correction:
        assert managed_region_id is not None
        regions = f"{{id=900,start=11,finish=12,name='User region'}},{{id={managed_region_id},start=10.25,finish=11.0,name='[vgt] Bridge'}}"
    return f"""
local tracks = {{
  {{guid='{{CLICK}}', name='Click', items={{{{position=0,length=1,take={{source=''}}}}}}}},
  {{guid='{REFERENCE_GUID}', name='The Seven Rivers (Full March - 3_00)', items={{{{position=10,length=4,take={{source='Media/The Seven Rivers (Full March - 3_00).mp3'}}}}}}}},
  {{guid='{{PARIS}}', name='Paris Metro Punk', items={{{{position=2,length=3,take={{source='Media/Paris Metro Punk.mp3'}}}}}}}},
  {managed_lua}
}}
local regions = {{{regions}}}
local markers = {{{{time=0,bpm=100,num=4,den=4}}}}
local next_guid, next_region, tempo_writes = 1, 1000, 0
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
function reaper.GetSetMediaTrackInfo_String(t, _, value) t.name=value end
function reaper.SetMediaTrackInfo_Value(t, key, value) t[key]=value end
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
function snapshot()
  local names, user_items, vgt = {{}}, 0, 0
  for _,t in ipairs(tracks) do names[#names+1]=t.name; if t.name:sub(1,5)=='[vgt]' then vgt=vgt+1 end; if t.name=='The Seven Rivers (Full March - 3_00)' or t.name=='Paris Metro Punk' then user_items=user_items+#t.items end end
  io.write(table.concat(names,'|') .. '#' .. user_items .. '#' .. #regions .. '#' .. vgt .. '#' .. tempo_writes)
end
"""


def _run_apply(project: Path, state: str) -> str:
    module = APPLY_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    result = subprocess.run([LUA, "-", str(project)], input="\n".join([state, module, "apply(); snapshot()"]), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _run_apply_key_snapshot(project: Path, state: str) -> str:
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
    result = subprocess.run([LUA, "-", str(project)], input="\n".join([state, module, program]), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _run_sync(project: Path, state: str) -> None:
    module = SYNC_SCRIPT.read_text(encoding="utf-8").split("local ok, error_message = xpcall", 1)[0]
    result = subprocess.run([LUA, "-", str(project)], input="\n".join([state, module, "sync()"]), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_goal_contract_is_offline_non_destructive_and_idempotent(tmp_path: Path, deterministic_detectors: None) -> None:
    project = _copy_project(tmp_path)

    # Initialization selects the real fixture's reference identity and writes only a sidecar.
    _run_apply(project, _lua_state(project))
    assert read_sidecar(project)["config"]["reference_track_guid"] == REFERENCE_GUID

    separator, transcriber = CountingSeparator(), CountingTranscriber()
    analyze(project, stages=("tempo", "key", "sections"))
    separate(project, separator, guitar_type="electric")
    analyze(project, stages=("chords", "transcription"), transcription_targets=("guitar",), transcriber=transcriber)
    assert (separator.calls, transcriber.calls) == (5, 1)

    # The existing 100 BPM map must remain untouched, so apply offers beat labels instead.
    first_apply = _run_apply(project, _lua_state(project))
    names, user_items, region_count, vgt_count, tempo_writes = first_apply.split("#")
    assert user_items == "2" and region_count == "3" and tempo_writes == "0"
    assert "[vgt] Beats" in names and "[vgt] Key" in names and "[vgt] Guitar Ref (MIDI)" in names
    assert int(vgt_count) == 12  # folder, beats/click/key/chords, six stems, MIDI

    sidecar = read_sidecar(project)
    managed = [(guid, "[vgt] Chords" if index == 3 else "[vgt] stale") for index, guid in enumerate(sidecar["managed_track_guids"])]
    _run_sync(project, _lua_state(project, managed, correction=True, managed_region_id=sidecar["managed_region_ids"][0]))
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
    managed = [(guid, "[vgt] stale") for guid in reconciled["managed_track_guids"]]
    second_apply = _run_apply(project, _lua_state(project, managed))
    names, user_items, region_count, vgt_count, tempo_writes = second_apply.split("#")
    assert user_items == "2" and region_count == "2" and tempo_writes == "0"
    assert int(vgt_count) == 12 and names.split("|").count("[vgt] Guitar") == 1
    assert _run_apply_key_snapshot(project, _lua_state(project, managed)) == "1#0:E minor:1"


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
    names, _user_items, _regions, _vgt, tempo_writes = _run_apply(project, state).split("#")

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
    names, _user_items, _regions, _vgt, tempo_writes = _run_apply(project, state).split("#")

    assert "[vgt] Beats" not in names
    assert int(tempo_writes) >= 2
    assert read_sidecar(project)["config"]["tempo_map_applied"] is True
