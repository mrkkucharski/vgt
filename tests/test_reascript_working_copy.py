from pathlib import Path
import os
import subprocess


WORKING_COPY_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_working_copy.lua"
LUA = os.environ.get("VGT_TEST_LUA", "lua")


def _run(lua_program: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [LUA, "-", *args], input=lua_program, text=True, capture_output=True, check=True
    )


def test_working_copy_is_never_vgt_owned() -> None:
    """The whole non-destructive contract rests on a working copy being invisible
    to vgt reconciliation: it must be named `[work]` (never `[vgt]`) and must have
    its durable ownership mark cleared. This locks both halves in the source."""
    script = WORKING_COPY_SCRIPT.read_text()
    assert 'local WORK_PREFIX = "[work]"' in script
    assert 'local EXT_STATE_KEY = "P_EXT:vgt_managed"' in script
    # The copy's name comes from working_name (always `[work] ...`) ...
    assert 'reaper.GetSetMediaTrackInfo_String(track, "P_NAME", working_name(source_name), true)' in script
    # ... and its ownership mark is explicitly cleared.
    assert 'reaper.GetSetMediaTrackInfo_String(track, EXT_STATE_KEY, "", true)' in script
    # Discard only ever removes `[work]` tracks, never `[vgt]` or user tracks.
    assert "starts_with(track_name(track), WORK_PREFIX)" in script


def test_working_name_reprefixes_into_the_work_namespace() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function replace_track_guid")
    lua_program = "\n".join(
        [
            script[:helpers_end],
            "io.write(working_name('[vgt] Guitar Ref (MIDI)'), '|')",
            "io.write(working_name('[work] Guitar Ref (MIDI)'), '|')",  # no [work] [work] pile-up
            "io.write(working_name('My Track'), '|')",
            "io.write(working_name('[vgt]'), '|')",  # empty remainder falls back to a name
        ]
    )
    result = _run(lua_program)
    assert result.stdout == (
        "[work] Guitar Ref (MIDI)|[work] Guitar Ref (MIDI)|[work] My Track|[work] Track|"
    )


def test_replace_track_guid_swaps_only_the_first_trackid() -> None:
    """A chunk carries the source's TRACKID; the copy must get a fresh, unique
    GUID (a shared GUID would make the copy and its source indistinguishable to
    vgt's GUID-based ownership)."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function folder_last_child_index")
    chunk = "<TRACK\\nTRACKID {AAAA-1111}\\nNAME foo\\nTRACKID {SHOULD-STAY}\\n>"
    lua_program = "\n".join(
        [
            script[:helpers_end],
            f"local chunk = '{chunk}'",
            "io.write(replace_track_guid(chunk, '{NEW-2222}'))",
        ]
    )
    result = _run(lua_program)
    assert "TRACKID {NEW-2222}" in result.stdout
    assert "{AAAA-1111}" not in result.stdout
    # Only the first TRACKID is the track's identity; a later literal is untouched.
    assert "{SHOULD-STAY}" in result.stdout


def _folder_mock(depths_literal: str) -> str:
    return "\n".join(
        [
            f"local depths = {depths_literal}",
            "reaper = {}",
            "function reaper.CountTracks() return #depths end",
            "function reaper.GetTrack(_, index) return index end",
            "function reaper.GetMediaTrackInfo_Value(index, key)",
            "  if key == 'I_FOLDERDEPTH' then return depths[index + 1] end",
            "  return 0",
            "end",
        ]
    )


def test_folder_last_child_index_walks_folder_depth() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function find_work_folder")
    lua_program = "\n".join(
        [
            # folder(+1), child(0), closer(-1), unrelated(0)
            _folder_mock("{1, 0, -1, 0}"),
            script[:helpers_end],
            "io.write(folder_last_child_index(0))",
        ]
    )
    assert _run(lua_program).stdout == "2"


def test_folder_last_child_index_handles_a_single_child_folder() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function find_work_folder")
    lua_program = "\n".join(
        [
            _folder_mock("{1, -1, 0}"),  # folder then its one closing child
            script[:helpers_end],
            "io.write(folder_last_child_index(0))",
        ]
    )
    assert _run(lua_program).stdout == "1"


def test_find_work_folder_matches_only_a_top_level_work_folder_track() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function selected_source_tracks")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name = '[vgt] Guitar', depth = 1},",
            "  {name = '[work]', depth = 1},",  # the real work folder
            "  {name = '[work] Guitar', depth = 0},",  # a child, not the folder
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return key == 'I_FOLDERDEPTH' and track.depth or 0 end",
            script[:helpers_end],
            "local folder, index = find_work_folder()",
            "io.write(folder.name, ':', index)",
        ]
    )
    assert _run(lua_program).stdout == "[work]:1"


def test_find_work_folder_ignores_a_flat_work_named_track() -> None:
    """A `[work]`-named track that is not a folder (depth 0) must not be reused
    as the container -- otherwise copies would be appended in the wrong place."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function selected_source_tracks")
    lua_program = "\n".join(
        [
            "local tracks = {{name = '[work]', depth = 0}}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return key == 'I_FOLDERDEPTH' and track.depth or 0 end",
            script[:helpers_end],
            "io.write(tostring(find_work_folder()))",
        ]
    )
    assert _run(lua_program).stdout == "nil"


def _build_copy_mock() -> str:
    return "\n".join(
        [
            "reaper = {}",
            "local tracks = {}",
            "local guid_counter = 0",
            "function reaper.InsertTrackAtIndex(index) tracks[index + 1] = {values = {}, ext = {}, items = {}, selected = false} end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.genGuid() guid_counter = guid_counter + 1; return '{GEN-' .. guid_counter .. '}' end",
            # Applying the source chunk restores the copy's items -- here two
            # that start out locked, so the unlock step has something to clear.
            "function reaper.SetTrackStateChunk(track, chunk) track.chunk = chunk; track.items = {{C_LOCK = 1}, {C_LOCK = 1}} end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set)",
            "  if set then if key == 'P_NAME' then track.name = value else track.ext[key] = value end return true end",
            "  return true, ''",
            "end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values[key] = value end",
            "function reaper.SetTrackSelected(track, selected) track.selected = selected end",
            "function reaper.CountTrackMediaItems(track) return #track.items end",
            "function reaper.GetTrackMediaItem(track, index) return track.items[index + 1] end",
            "function reaper.SetMediaItemInfo_Value(item, key, value) item[key] = value end",
            "_G.__tracks = tracks",
        ]
    )


def test_build_working_copy_produces_an_editable_user_owned_track() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function create()")
    lua_program = "\n".join(
        [
            _build_copy_mock(),
            script[:helpers_end],
            "build_working_copy(0, 'TRACKID {OLD-1}\\n', '[vgt] Guitar Ref (MIDI)', -1)",
            "local t = reaper.GetTrack(0, 0)",
            "io.write(t.name, '|', tostring(t.values.B_MUTE), '|', tostring(t.values.I_FOLDERDEPTH), '|', tostring(t.ext['P_EXT:vgt_managed']), '|', tostring(t.selected), '|', tostring(t.items[1].C_LOCK), tostring(t.items[2].C_LOCK), '|', t.chunk)",
        ]
    )
    result = _run(lua_program)
    name, mute, depth, mark, selected, locks, chunk = result.stdout.split("|")
    assert name == "[work] Guitar Ref (MIDI)"  # user namespace, not [vgt]
    assert mute == "0"  # unmuted so it is audible/visible while editing
    assert depth == "-1"  # closes the folder as requested
    assert mark == ""  # ownership mark cleared -> vgt ignores it
    assert selected == "true"  # new copy becomes the selection
    assert locks == "00"  # every item unlocked -> immediately editable
    assert "TRACKID {GEN-1}" in chunk and "{OLD-1}" not in chunk  # fresh unique GUID


def test_discard_removes_only_work_tracks() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name = '[work]'}, {name = '[work] Guitar Ref (MIDI)'},",
            "  {name = '[vgt] Guitar Ref (MIDI)'}, {name = 'My Keeper'},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.DeleteTrack(track) for i, t in ipairs(tracks) do if t == track then table.remove(tracks, i); return end end end",
            "function reaper.Undo_BeginBlock() end",
            "function reaper.Undo_EndBlock() end",
            "function reaper.PreventUIRefresh() end",
            "function reaper.TrackList_AdjustWindows() end",
            "function reaper.UpdateArrange() end",
            "function reaper.MarkProjectDirty() end",
            "function reaper.ShowMessageBox() error('should not warn when [work] tracks were removed') end",
            script[:helpers_end],
            "discard()",
            "for _, t in ipairs(tracks) do io.write(t.name, ';') end",
        ]
    )
    assert _run(lua_program).stdout == "[vgt] Guitar Ref (MIDI);My Keeper;"


def test_discard_warns_when_there_is_nothing_to_remove() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {{name = '[vgt] Guitar'}, {name = 'User'}}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.DeleteTrack() error('nothing should be deleted') end",
            "function reaper.Undo_BeginBlock() end",
            "function reaper.Undo_EndBlock() end",
            "function reaper.PreventUIRefresh() end",
            "function reaper.TrackList_AdjustWindows() end",
            "function reaper.UpdateArrange() end",
            "function reaper.MarkProjectDirty() error('must not dirty when nothing changed') end",
            "function reaper.ShowMessageBox(text) io.write('WARNED:', text) end",
            script[:helpers_end],
            "discard()",
        ]
    )
    assert _run(lua_program).stdout.startswith("WARNED:")
    assert "no longer starts with [work]" in _run(lua_program).stdout


def test_working_copy_uses_reaper_api_and_never_touches_the_sidecar_or_rpp_text() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    assert "reaper.SetTrackStateChunk" in script
    assert "reaper.GetTrackStateChunk" in script
    # This action manipulates the live project only: it must not read or write
    # the sidecar (no analysis/ownership state lives there for working copies).
    assert ".vgt" not in script
    assert "GetSetProjectInfo_String" not in script
