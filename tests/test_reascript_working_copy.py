from pathlib import Path
import os
import subprocess


WORKING_COPY_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_working_copy.lua"
LUA = os.environ.get("VGT_TEST_LUA", "lua")


def _run(lua_program: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [LUA, "-", *args], input=lua_program, text=True, capture_output=True
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result


def test_working_copy_is_never_vgt_owned() -> None:
    """The whole non-destructive contract rests on a working copy being invisible
    to vgt reconciliation: it must be named `[work]` (never `[vgt]`), have its
    vgt ownership mark cleared, and use a separate working-copy provenance mark."""
    script = WORKING_COPY_SCRIPT.read_text()
    assert 'local WORK_PREFIX = "[work]"' in script
    assert 'local VGT_EXT_STATE_KEY = "P_EXT:vgt_managed"' in script
    assert 'local WORK_EXT_STATE_KEY = "P_EXT:vgt_working_copy"' in script
    # The copy's name comes from working_name (always `[work] ...`) ...
    assert 'reaper.GetSetMediaTrackInfo_String(track, "P_NAME", working_name(source_name), true)' in script
    # ... and its normal-vgt ownership mark is explicitly cleared while the
    # action's distinct provenance marker is stamped.
    assert 'reaper.GetSetMediaTrackInfo_String(track, VGT_EXT_STATE_KEY, "", true)' in script
    assert 'reaper.GetSetMediaTrackInfo_String(track, WORK_EXT_STATE_KEY, WORK_EXT_STATE_VALUE, true)' in script
    # Promotion requires both name and durable provenance, never just `[work]`.
    assert "is_marked_work_object(track) and starts_with(track_name(track), WORK_PREFIX)" in script


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


def test_find_work_folder_resolves_a_top_level_marked_container() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function selected_source_tracks")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name = '[vgt] Guitar', depth = 0},",
            "  {name = '[work] Guitar', depth = 1, ext={['P_EXT:vgt_container']='work'}},",  # the real work container
            "  {name = '[work] Guitar', depth = -1, ext={['P_EXT:vgt_working_copy']='1'}},",  # a marked closing child, not the folder
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return key == 'I_FOLDERDEPTH' and track.depth or 0 end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext = track.ext or {}; track.ext[key] = value; return true, value end; return true, track.ext and track.ext[key] or '' end",
            script[:helpers_end],
            "local folder, index = find_work_folder()",
            "io.write(folder.name, ':', index)",
        ]
    )
    assert _run(lua_program).stdout == "[work] Guitar:1"


def test_find_work_folder_prefers_recorded_guid_and_falls_back_when_it_is_stale() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function selected_source_tracks")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='[work] Recorded', guid='{RECORDED}', depth=0, ext={}},",
            "  {name='[work] Marked', guid='{MARKED}', depth=1, ext={['P_EXT:vgt_container']='work'}},",
            "  {name='[work] child', depth=-1, ext={['P_EXT:vgt_working_copy']='1'}},",
            "}",
            "local recorded = '{RECORDED}'",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackGUID(track) return track.guid end",
            "function reaper.GetProjExtState() return 1, recorded end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return key == 'I_FOLDERDEPTH' and track.depth or 0 end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) return true, track.ext[key] or '' end",
            script[:helpers_end],
            "io.write(find_work_folder().name, '|')",
            "recorded = '{STALE}'; io.write(find_work_folder().name)",
        ]
    )
    assert _run(lua_program).stdout == "[work] Recorded|[work] Marked"


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
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) return true, '' end",
            script[:helpers_end],
            "io.write(tostring(find_work_folder()))",
        ]
    )
    assert _run(lua_program).stdout == "nil"


def test_find_work_folder_preserves_unmarked_and_nested_work_collisions() -> None:
    """Names are not provenance: neither a legacy top-level folder nor a marked
    nested folder may be adopted as the action's reusable container."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function selected_source_tracks")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name = 'Parent', depth = 1},",
            "  {name = '[work]', depth = 1, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name = 'Nested child', depth = -1},",
            "  {name = '[work]', depth = 1},",  # legacy user folder
            "  {name = 'Legacy child', depth = -1},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return key == 'I_FOLDERDEPTH' and track.depth or 0 end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext = track.ext or {}; track.ext[key] = value; return true, value end; return true, track.ext and track.ext[key] or '' end",
            script[:helpers_end],
            "io.write(tostring(find_work_folder()))",
        ]
    )
    assert _run(lua_program).stdout == "nil"


def test_find_work_folder_resolves_marked_container_even_with_altered_folder_depth() -> None:
    """The marker is not permission to repair a structurally changed folder.
    Reusing this +2/-2 workspace as though it were vgt's +1/-1 shape would
    leave one folder level open after appending a new copy."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function selected_source_tracks")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name = '[work] Guitar', depth = 2, ext={['P_EXT:vgt_container']='work'}},",
            "  {name = '[work] altered copy', depth = -2, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name = 'Outside', depth = 0},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return key == 'I_FOLDERDEPTH' and track.depth or 0 end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext = track.ext or {}; track.ext[key] = value; return true, value end; return true, track.ext and track.ext[key] or '' end",
            script[:helpers_end],
            "local folder = find_work_folder(); io.write(folder.name)",
        ]
    )
    assert _run(lua_program).stdout == "[work] Guitar"


def test_create_reports_and_leaves_a_structurally_changed_container_untouched() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='[work] Guitar', values={I_FOLDERDEPTH=2}, ext={['P_EXT:vgt_container']='work'}, items={}},",
            "  {name='[work] altered', values={I_FOLDERDEPTH=-2}, ext={['P_EXT:vgt_working_copy']='1'}, items={}},",
            "  {name='Source', values={I_FOLDERDEPTH=0}, ext={}, items={}, selected=true},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext[key] = value; return true, value end; return true, track.ext[key] or '' end",
            "function reaper.CountSelectedTracks() return 1 end; function reaper.GetSelectedTrack() return tracks[3] end",
            "function reaper.GetTrackStateChunk() return true, 'TRACKID {SOURCE}' end",
            "function reaper.SetTrackSelected(track, selected) track.selected = selected end",
            "function reaper.InsertTrackAtIndex() error('must not append into a changed container') end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end; function reaper.PreventUIRefresh() end",
            "function reaper.ShowMessageBox(text) io.write(text) end",
            script[:helpers_end],
            "create(); for _, track in ipairs(tracks) do io.write('|', track.name, ':', track.values.I_FOLDERDEPTH) end",
        ]
    )
    result = _run(lua_program).stdout
    assert result.startswith("The [work] container has a changed folder structure")
    assert result.endswith("|[work] Guitar:2|[work] altered:-2|Source:0")


def test_create_appends_into_a_populated_work_container() -> None:
    """A populated-but-structurally-intact [work] folder is not a dead end:
    create reopens the current closing child (I_FOLDERDEPTH -1 -> 0) and
    appends the new copy after it, leaving the existing child's name, marker,
    and items otherwise untouched."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='[work] Guitar', values={I_FOLDERDEPTH=1}, ext={['P_EXT:vgt_container']='work'}, items={}},",
            "  {name='[work] Existing copy', values={I_FOLDERDEPTH=-1}, ext={['P_EXT:vgt_working_copy']='1',keep='payload'}, items={{C_LOCK=1}}},",
            "  {name='Source', values={I_FOLDERDEPTH=0}, ext={}, items={}, selected=true},",
            "  {name='Outside', values={I_FOLDERDEPTH=0}, ext={}, items={}},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values[key] = value end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set)",
            "  if set then if key == 'P_NAME' then track.name = value else track.ext[key] = value end; return true, value end",
            "  return true, track.ext[key] or ''",
            "end",
            "function reaper.CountSelectedTracks() return 1 end; function reaper.GetSelectedTrack() return tracks[3] end",
            "function reaper.GetTrackStateChunk() return true, 'TRACKID {SOURCE}' end",
            "function reaper.genGuid() return '{COPY}' end",
            "function reaper.InsertTrackAtIndex(index) table.insert(tracks, index + 1, {name='', values={}, ext={}, items={}}) end",
            "function reaper.SetTrackStateChunk(track, chunk) track.chunk=chunk end",
            "function reaper.SetTrackSelected(track, selected) track.selected=selected end",
            "function reaper.CountTrackMediaItems(track) return #track.items end",
            "function reaper.GetTrackMediaItem(track, index) return track.items[index + 1] end",
            "function reaper.SetMediaItemInfo_Value(item, key, value) item[key] = value end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end; function reaper.PreventUIRefresh() end",
            "function reaper.TrackList_AdjustWindows() end; function reaper.UpdateArrange() end; function reaper.MarkProjectDirty() end",
            "function reaper.ShowMessageBox(text) error('unexpected refusal: ' .. text) end",
            script[:helpers_end],
            "create()",
            "for _, track in ipairs(tracks) do io.write(track.name, ':', track.values.I_FOLDERDEPTH, ':', track.ext.keep or '', ';') end",
        ]
    )
    result = _run(lua_program).stdout
    assert result == (
        "[work] Guitar:1:;"
        "[work] Existing copy:0:payload;"
        "[work] Source:-1:;"
        "Source:0:;"
        "Outside:0:;"
    )


def test_create_reuses_an_empty_initialize_container() -> None:
    """Initialize intentionally leaves a newly made container flat until this
    action gives it its first child. That ordinary empty state is not damage."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='[work] Guitar', values={I_FOLDERDEPTH=0}, ext={['P_EXT:vgt_container']='work'}, items={}},",
            "  {name='Source', values={I_FOLDERDEPTH=0}, ext={}, items={}, selected=true},",
            "  {name='Outside', values={I_FOLDERDEPTH=0}, ext={}, items={}},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values[key] = value end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then if key == 'P_NAME' then track.name = value else track.ext[key] = value end; return true, value end; return true, track.ext[key] or '' end",
            "function reaper.CountSelectedTracks() return 1 end; function reaper.GetSelectedTrack() return tracks[2] end",
            "function reaper.GetTrackStateChunk() return true, 'TRACKID {SOURCE}' end",
            "function reaper.genGuid() return '{COPY}' end",
            "function reaper.InsertTrackAtIndex(index) table.insert(tracks, index + 1, {name='', values={}, ext={}, items={}}) end",
            "function reaper.SetTrackStateChunk(track, chunk) track.chunk=chunk end",
            "function reaper.SetTrackSelected(track, selected) track.selected=selected end",
            "function reaper.CountTrackMediaItems(track) return #track.items end",
            "function reaper.GetTrackMediaItem(track, index) return track.items[index + 1] end",
            "function reaper.SetMediaItemInfo_Value() end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end; function reaper.PreventUIRefresh() end",
            "function reaper.TrackList_AdjustWindows() end; function reaper.UpdateArrange() end; function reaper.MarkProjectDirty() end",
            "function reaper.ShowMessageBox() error('an empty initialized container must be reusable') end",
            script[:helpers_end],
            "create(); for _, track in ipairs(tracks) do io.write(track.name, ':', track.values.I_FOLDERDEPTH, ';') end",
        ]
    )
    assert _run(lua_program).stdout == "[work] Guitar:1;[work] Source:-1;Source:0;Outside:0;"


def test_find_work_folder_resolves_marked_container_even_with_nested_children() -> None:
    """A total depth of zero is insufficient: a marked child may have been
    made into a folder. Reusing a +1/0/+1/-2 layout would leave that nested
    level open after the old closer is demoted."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function selected_source_tracks")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name = '[work] Guitar', depth = 1, ext={['P_EXT:vgt_container']='work'}},",
            "  {name = '[work] first copy', depth = 0, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name = '[work] nested copy', depth = 1, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name = '[work] nested child', depth = -2, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name = 'Outside', depth = 0},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return key == 'I_FOLDERDEPTH' and track.depth or 0 end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext = track.ext or {}; track.ext[key] = value; return true, value end; return true, track.ext and track.ext[key] or '' end",
            script[:helpers_end],
            "local folder = find_work_folder(); io.write(folder.name)",
        ]
    )
    assert _run(lua_program).stdout == "[work] Guitar"


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
            "  if set then if key == 'P_NAME' then track.name = value else track.ext[key] = value end return true, value end",
            "  return true, track.ext[key] or ''",
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
            "build_working_copy(0, 'TRACKID {OLD-1}\\n', '[vgt] Guitar Ref — Clean (MIDI)', -1)",
            "local t = reaper.GetTrack(0, 0)",
            "io.write(t.name, '|', tostring(t.values.B_MUTE), '|', tostring(t.values.I_FOLDERDEPTH), '|', tostring(t.ext['P_EXT:vgt_managed']), '|', tostring(t.ext['P_EXT:vgt_working_copy']), '|', tostring(t.selected), '|', tostring(t.items[1].C_LOCK), tostring(t.items[2].C_LOCK), '|', t.chunk)",
        ]
    )
    result = _run(lua_program)
    name, mute, depth, mark, work_mark, selected, locks, chunk = result.stdout.split("|")
    assert name == "[work] Guitar Ref — Clean (MIDI)"  # user namespace, not [vgt]
    assert mute == "0"  # unmuted so it is audible/visible while editing
    assert depth == "-1"  # closes the folder as requested
    assert mark == ""  # ownership mark cleared -> vgt ignores it
    assert work_mark == "1"  # action-specific provenance enables safe promotion
    assert selected == "true"  # new copy becomes the selection
    assert locks == "00"  # every item unlocked -> immediately editable
    assert "TRACKID {GEN-1}" in chunk and "{OLD-1}" not in chunk  # fresh unique GUID


def test_create_does_not_reuse_an_unmarked_work_folder_and_creates_a_container() -> None:
    """A user-created `[work]` folder is a collision, not an invitation to
    mutate its closing child. The action creates a separate marked top-level
    container and keeps both folder-depth regions balanced."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='[work]', values={I_FOLDERDEPTH=1}, ext={}, items={}},",
            "  {name='User child', values={I_FOLDERDEPTH=-1}, ext={}, items={}},",
            "  {name='Source', values={I_FOLDERDEPTH=0}, ext={}, items={}, selected=true},",
            "  {name='[vgt] Reference Mix', values={I_FOLDERDEPTH=0}, ext={}, items={}},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackGUID(track) return track.guid or ('{TRACK-' .. tostring(track) .. '}') end",
            "function reaper.GetProjExtState() return 0, '' end; function reaper.SetProjExtState(_, _, _, value) _G.__work_guid = value end",
            "function reaper.ColorToNative(r, g, b) return r * 65536 + g * 256 + b end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values[key] = value end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set)",
            "  if set then if key == 'P_NAME' then track.name = value else track.ext[key] = value end; return true, value end",
            "  return true, track.ext[key] or ''",
            "end",
            "function reaper.CountSelectedTracks() local n=0; for _,t in ipairs(tracks) do if t.selected then n=n+1 end end; return n end",
            "function reaper.GetSelectedTrack(_, index) local n=0; for _,t in ipairs(tracks) do if t.selected then if n==index then return t end; n=n+1 end end end",
            "function reaper.GetTrackStateChunk() return true, 'TRACKID {SOURCE}' end",
            "function reaper.genGuid() return '{COPY}' end",
            "function reaper.InsertTrackAtIndex(index) table.insert(tracks, index + 1, {name='', values={}, ext={}, items={}}) end",
            "function reaper.SetTrackStateChunk(track, chunk) track.chunk=chunk end",
            "function reaper.SetTrackSelected(track, value) track.selected=value end",
            "function reaper.CountTrackMediaItems(track) return #track.items end",
            "function reaper.GetTrackMediaItem(track, index) return track.items[index + 1] end",
            "function reaper.SetMediaItemInfo_Value() end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end",
            "function reaper.PreventUIRefresh() end; function reaper.TrackList_AdjustWindows() end; function reaper.UpdateArrange() end",
            "function reaper.MarkProjectDirty() end; function reaper.ShowMessageBox() error('unexpected warning') end",
            script[:helpers_end],
            "create()",
            "for _,t in ipairs(tracks) do io.write(t.name, ':', t.values.I_FOLDERDEPTH or 0, ':', t.ext['P_EXT:vgt_container'] or '', ':', t.ext['P_EXT:vgt_working_copy'] or '', ':', t.values.I_CUSTOMCOLOR or 0, ';') end; io.write('GUID=', __work_guid)",
        ]
    )
    result = _run(lua_program).stdout
    assert result.startswith(
        "[work]:1:::0;User child:-1:::0;Source:0:::0;[work] Reference Mix:1:work::21278703;[work] Source:-1::1:0;[vgt] Reference Mix:0:::0;GUID={TRACK-"
    )


def _promote_mock(tracks_literal: str) -> str:
    return "\n".join(
        [
            f"local tracks = {tracks_literal}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackGUID(track) return track.guid end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values[key] = value end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then if key == 'P_NAME' then track.name=value else track.ext[key]=value end; return true,value end; return true,track.ext[key] or '' end",
            "function reaper.GetProjExtState(_, _, key) return 1, (_G.__project_ext and _G.__project_ext[key] or '') end",
            "function reaper.SetProjExtState(_, _, key, value) _G.__project_ext=_G.__project_ext or {}; _G.__project_ext[key]=value end",
            "function reaper.ColorToNative(r,g,b) return r*65536+g*256+b end",
            "function reaper.CountSelectedTracks() local n=0; for _,t in ipairs(tracks) do if t.selected then n=n+1 end end; return n end",
            "function reaper.GetSelectedTrack(_, wanted) local n=0; for _,t in ipairs(tracks) do if t.selected then if n==wanted then return t end; n=n+1 end end end",
            "function reaper.SetTrackSelected(track, value) track.selected=value end",
            "function reaper.InsertTrackAtIndex(index) table.insert(tracks,index+1,{name='',guid='{CLEAN}',values={I_FOLDERDEPTH=0},ext={},items={},takes={}}) end",
            "function reaper.ReorderSelectedTracks(before) local moved={}; for i=#tracks,1,-1 do if tracks[i].selected then table.insert(moved,1,tracks[i]); table.remove(tracks,i) end end; for i=#moved,1,-1 do table.insert(tracks,before + 1,moved[i]) end end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end; function reaper.PreventUIRefresh() end; function reaper.TrackList_AdjustWindows() end; function reaper.UpdateArrange() end; function reaper.MarkProjectDirty() _G.__dirty=true end",
            "function reaper.ShowMessageBox(text) _G.__message=text end",
            "_G.__tracks=tracks",
        ]
    )


def test_promote_moves_the_existing_track_and_reclaims_it() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            _promote_mock("{{name='[clean] Song',guid='{C}',values={I_FOLDERDEPTH=0},ext={['P_EXT:vgt_container']='clean'},items={}}, {name='[work] Song',guid='{W}',values={I_FOLDERDEPTH=1},ext={['P_EXT:vgt_container']='work'},items={}}, {name='[work] Guitar',guid='{GUITAR}',values={I_FOLDERDEPTH=-1},ext={['P_EXT:vgt_working_copy']='1',['P_EXT:vgt_managed']='1',['P_EXT:vgt_container']='work'},items={'item'},takes={'take'},selected=true}, {name='Outside',guid='{O}',values={I_FOLDERDEPTH=0},ext={},items={}}}"),
            script[:helpers_end],
            "promote(); local t=__tracks[2]; io.write(t.name,'|',t.guid,'|',t.items[1],'|',t.takes[1],'|',t.ext['P_EXT:vgt_working_copy'] or '', '|',t.ext['P_EXT:vgt_managed'] or '', '|',t.ext['P_EXT:vgt_container'] or '', '|',t.values.I_FOLDERDEPTH,'|',__tracks[3].values.I_FOLDERDEPTH)",
        ]
    )
    assert _run(lua_program).stdout == "[clean] Guitar|{GUITAR}|item|take||||-1|0"


def test_clean_name_reprefixes_without_namespace_pileup() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function replace_track_guid")
    lua_program = "\n".join(
        [
            script[:helpers_end],
            "io.write(clean_name('[vgt] A'),'|',clean_name('[work] A'),'|',clean_name('[clean] A'),'|',clean_name('A'),'|',clean_name('[work]'))",
        ]
    )
    assert _run(lua_program).stdout == "[clean] A|[clean] A|[clean] A|[clean] A|[clean] Track"


def test_promote_into_empty_clean_preserves_selection_order_and_flattens_work() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            _promote_mock("{{name='[clean] Song',guid='{C}',values={I_FOLDERDEPTH=0},ext={['P_EXT:vgt_container']='clean'},items={}}, {name='[work] Song',guid='{W}',values={I_FOLDERDEPTH=1},ext={['P_EXT:vgt_container']='work'},items={}}, {name='[work] First',guid='{1}',values={I_FOLDERDEPTH=0},ext={['P_EXT:vgt_working_copy']='1'},items={},selected=true}, {name='[work] Second',guid='{2}',values={I_FOLDERDEPTH=-1},ext={['P_EXT:vgt_working_copy']='1'},items={},selected=true}, {name='Outside',guid='{O}',values={I_FOLDERDEPTH=0},ext={},items={}}}"),
            script[:helpers_end],
            "promote(); for _,t in ipairs(__tracks) do io.write(t.name,':',t.values.I_FOLDERDEPTH,':',t.selected and 'S' or '', ';') end",
        ]
    )
    assert _run(lua_program).stdout == (
        "[clean] Song:1:;[clean] First:0:S;[clean] Second:-1:S;[work] Song:0:;Outside:0:;"
    )


def test_promote_creates_clean_above_work_with_initialize_metadata() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            _promote_mock("{{name='[work] Reference',guid='{W}',values={I_FOLDERDEPTH=1},ext={['P_EXT:vgt_container']='work'},items={}}, {name='[work] Draft',guid='{D}',values={I_FOLDERDEPTH=-1},ext={['P_EXT:vgt_working_copy']='1'},items={},selected=true}, {name='[vgt] Reference',guid='{V}',values={I_FOLDERDEPTH=0},ext={},items={}}}"),
            script[:helpers_end],
            "promote(); local c=__tracks[1]; io.write(c.name,'|',c.ext['P_EXT:vgt_container'],'|',c.values.I_CUSTOMCOLOR,'|',__project_ext.clean_container,'|',__tracks[3].name)",
        ]
    )
    assert _run(lua_program).stdout == "[clean] Reference|clean|29086249|{CLEAN}|[work] Reference"


def test_promote_refuses_renamed_or_unmarked_work_tracks_without_changes() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            _promote_mock("{{name='Kept by user',guid='{R}',values={I_FOLDERDEPTH=0},ext={['P_EXT:vgt_working_copy']='1'},items={},selected=true}, {name='[work] Hand made',guid='{U}',values={I_FOLDERDEPTH=0},ext={},items={},selected=true}}"),
            script[:helpers_end],
            "promote(); io.write(__message,'|',tostring(__dirty),'|',__tracks[1].ext['P_EXT:vgt_working_copy'],'|',__tracks[2].name)",
        ]
    )
    result = _run(lua_program).stdout
    assert result.startswith("Select [work] tracks created by vgt to promote them to [clean].|nil|1|[work] Hand made")


def test_promote_reassigns_the_work_closer_onto_a_remaining_sibling() -> None:
    """Promoting the [work] folder's closing child while an unselected
    sibling remains reassigns the closing flag (0 -> -1) onto that sibling
    instead of refusing. This mirrors the reassignment already used whenever
    any non-closer child is promoted; only the closer-specific case used to
    be refused, and that refusal is now gone. The unselected sibling's name,
    marker, and items are otherwise untouched."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    tracks = "{{name='[clean] Song',guid='{C}',values={I_FOLDERDEPTH=0},ext={['P_EXT:vgt_container']='clean'},items={}}, {name='[work] Song',guid='{W}',values={I_FOLDERDEPTH=1},ext={['P_EXT:vgt_container']='work'},items={}}, {name='User reclaimed',guid='{R}',values={I_FOLDERDEPTH=0},ext={['P_EXT:vgt_working_copy']='1',keep='reclaimed'},items={'keep'},selected=false}, {name='[work] Draft',guid='{D}',values={I_FOLDERDEPTH=-1},ext={['P_EXT:vgt_working_copy']='1'},items={'draft'},selected=true}}"
    lua_program = "\n".join([
        _promote_mock(tracks), script[:helpers_end],
        "promote(); for _, t in ipairs(__tracks) do io.write(t.name, ':', t.values.I_FOLDERDEPTH, ':', t.ext.keep or '', ':', t.items[1] or '', ':', t.ext['P_EXT:vgt_working_copy'] or '', ';') end",
    ])
    assert _run(lua_program).stdout == (
        "[clean] Song:1:::;"
        "[clean] Draft:-1::draft:;"
        "[work] Song:1:::;"
        "User reclaimed:-1:reclaimed:keep:1;"
    )


def test_promote_appends_into_a_populated_clean_container() -> None:
    """A populated-but-structurally-intact [clean] folder is not a dead end:
    promote reopens its current closing child (I_FOLDERDEPTH -1 -> 0) and
    appends the newly promoted track after it, leaving the existing child's
    name, marker, and items otherwise untouched."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    tracks = "{{name='[clean] Song',guid='{C}',values={I_FOLDERDEPTH=1},ext={['P_EXT:vgt_container']='clean'},items={}}, {name='Old clean',guid='{OLD}',values={I_FOLDERDEPTH=-1},ext={keep='yes'},items={'keep'},selected=false}, {name='[work] Song',guid='{W}',values={I_FOLDERDEPTH=1},ext={['P_EXT:vgt_container']='work'},items={}}, {name='[work] Draft',guid='{D}',values={I_FOLDERDEPTH=-1},ext={['P_EXT:vgt_working_copy']='1'},items={'draft'},selected=true}}"
    lua_program = "\n".join([
        _promote_mock(tracks), script[:helpers_end],
        "promote(); for _, t in ipairs(__tracks) do io.write(t.name, ':', t.values.I_FOLDERDEPTH, ':', t.ext.keep or '', ':', t.items[1] or '', ';') end",
    ])
    assert _run(lua_program).stdout == (
        "[clean] Song:1::;"
        "Old clean:0:yes:keep;"
        "[clean] Draft:-1::draft;"
        "[work] Song:0::;"
    )


def test_working_copy_uses_reaper_api_and_never_touches_the_sidecar_or_rpp_text() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    assert "reaper.SetTrackStateChunk" in script
    assert "reaper.GetTrackStateChunk" in script
    # This action manipulates the live project only: it must not read or write
    # the sidecar (no analysis/ownership state lives there for working copies).
    assert ".vgt" not in script
    assert "GetSetProjectInfo_String" not in script
    assert "reaper.ReorderSelectedTracks" in script
    assert "local function discard()" not in script
    assert "workspace_has_only_discardable_children" not in script
