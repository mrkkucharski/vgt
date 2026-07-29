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
    assert 'local function forget_reclaimed_work_objects()' in script
    # Discard requires both name and durable provenance, never just `[work]`.
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
    assert work_mark == "1"  # action-specific provenance enables safe discard
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


def test_discard_removes_only_marked_work_tracks() -> None:
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name = '[work]', values={I_FOLDERDEPTH=1}, ext={['P_EXT:vgt_container']='work'}},",
            "  {name = '[work] Guitar Ref (MIDI)', values={I_FOLDERDEPTH=-1}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name = '[work] User folder', values={I_FOLDERDEPTH=0}}, {name = '[work] User track', values={I_FOLDERDEPTH=0}},",
            "  {name = '[work] Reclaimed', ext={['P_EXT:vgt_working_copy']='1'}, renamed='Kept by user'},",
            "  {name = '[vgt] Guitar Ref (MIDI)'}, {name = 'My Keeper'},",
            "}",
            "tracks[5].name = tracks[5].renamed",  # a renamed marked copy is retained
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext = track.ext or {}; track.ext[key] = value; return true, value end; return true, track.ext and track.ext[key] or '' end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values and track.values[key] or 0 end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values = track.values or {}; track.values[key] = value end",
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
            "for _, t in ipairs(tracks) do io.write(t.name, ':', t.ext and (t.ext['P_EXT:vgt_working_copy'] or '') or '', ';') end",
        ]
    )
    assert _run(lua_program).stdout == "[work]:;[work] User folder:;[work] User track:;Kept by user:;[vgt] Guitar Ref (MIDI):;My Keeper:;"


def test_discard_preserves_a_mixed_marked_workspace_without_breaking_its_folder() -> None:
    """One user track in a marked workspace makes the entire workspace
    ineligible.  In particular, vgt must not delete its marked closing child
    and leave the user's folder open over the following track."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='[work]', values={I_FOLDERDEPTH=1}, ext={['P_EXT:vgt_container']='work'}},",
            "  {name='[work] disposable', values={I_FOLDERDEPTH=0}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name='User addition', values={I_FOLDERDEPTH=-1}, ext={}},",
            "  {name='Outside', values={I_FOLDERDEPTH=0}, ext={}},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values[key] = value end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext[key] = value; return true, value end; return true, track.ext[key] or '' end",
            "function reaper.DeleteTrack() error('a mixed workspace must remain intact') end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end",
            "function reaper.PreventUIRefresh() end; function reaper.TrackList_AdjustWindows() end; function reaper.UpdateArrange() end",
            "function reaper.MarkProjectDirty() error('nothing changed') end",
            "function reaper.ShowMessageBox(text) io.write('WARNED:', text) end",
            script[:helpers_end],
            "discard()",
            "for _,t in ipairs(tracks) do io.write(t.name, ':', t.values.I_FOLDERDEPTH, ';') end",
        ]
    )
    result = _run(lua_program)
    assert result.stdout.endswith("[work]:1;[work] disposable:0;User addition:-1;Outside:0;")


def test_discard_preserves_a_marked_workspace_with_altered_folder_depth() -> None:
    """A durable marker only identifies vgt's original scratch shape; it does
    not authorize deleting a workspace whose nesting has been changed."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='[work]', values={I_FOLDERDEPTH=2}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name='[work] altered copy', values={I_FOLDERDEPTH=-2}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name='Outside', values={I_FOLDERDEPTH=0}, ext={}},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext[key] = value; return true, value end; return true, track.ext[key] or '' end",
            "function reaper.DeleteTrack() error('an altered workspace must remain intact') end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end",
            "function reaper.PreventUIRefresh() end; function reaper.TrackList_AdjustWindows() end; function reaper.UpdateArrange() end",
            "function reaper.MarkProjectDirty() error('nothing changed') end",
            "function reaper.ShowMessageBox(text) io.write('WARNED:', text) end",
            script[:helpers_end],
            "discard()",
            "for _,t in ipairs(tracks) do io.write(t.name, ':', t.values.I_FOLDERDEPTH, ';') end",
        ]
    )
    result = _run(lua_program)
    assert result.stdout.endswith("[work]:2;[work] altered copy:-2;Outside:0;")


def test_discard_preserves_a_marked_workspace_with_nested_children() -> None:
    """Discard applies the same exact-shape guard as reuse, so it cannot
    remove a marker-bearing workspace after the user creates nested folders."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='[work]', values={I_FOLDERDEPTH=1}, ext={['P_EXT:vgt_container']='work'}},",
            "  {name='[work] first copy', values={I_FOLDERDEPTH=0}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name='[work] nested copy', values={I_FOLDERDEPTH=1}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name='[work] nested child', values={I_FOLDERDEPTH=-2}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name='Outside', values={I_FOLDERDEPTH=0}, ext={}},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext[key] = value; return true, value end; return true, track.ext[key] or '' end",
            "function reaper.DeleteTrack() error('a nested workspace must remain intact') end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end",
            "function reaper.PreventUIRefresh() end; function reaper.TrackList_AdjustWindows() end; function reaper.UpdateArrange() end",
            "function reaper.MarkProjectDirty() error('nothing changed') end",
            "function reaper.ShowMessageBox(text) io.write('WARNED:', text) end",
            script[:helpers_end],
            "discard()",
            "for _,t in ipairs(tracks) do io.write(t.name, ':', t.values.I_FOLDERDEPTH, ';') end",
        ]
    )
    result = _run(lua_program)
    assert result.stdout.endswith(
        "[work]:1;[work] first copy:0;[work] nested copy:1;[work] nested child:-2;Outside:0;"
    )


def test_discard_preserves_reclaimed_copy_permanently_and_keeps_folder_depths() -> None:
    """A renamed copy has its private marker cleared before disposal.  Even if
    the user subsequently gives it a `[work]` name again, it remains a user
    track; the adjacent marked scratch folder is still removed cleanly."""
    script = WORKING_COPY_SCRIPT.read_text()
    helpers_end = script.index("local function choose_action")
    lua_program = "\n".join(
        [
            "local tracks = {",
            "  {name='User folder', values={I_FOLDERDEPTH=1}, ext={}},",
            "  {name='User child', values={I_FOLDERDEPTH=-1}, ext={}},",
            "  {name='[work]', values={I_FOLDERDEPTH=1}, ext={['P_EXT:vgt_container']='work'}},",
            "  {name='[work] disposable', values={I_FOLDERDEPTH=-1}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "  {name='Kept part', values={I_FOLDERDEPTH=0}, ext={['P_EXT:vgt_working_copy']='1'}},",
            "}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.GetMediaTrackInfo_Value(track, key) return track.values[key] or 0 end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values[key] = value end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if set then track.ext[key] = value; return true, value end; return true, track.ext[key] or '' end",
            "function reaper.DeleteTrack(track) for i, t in ipairs(tracks) do if t == track then table.remove(tracks, i); return end end end",
            "function reaper.Undo_BeginBlock() end; function reaper.Undo_EndBlock() end",
            "function reaper.PreventUIRefresh() end; function reaper.TrackList_AdjustWindows() end; function reaper.UpdateArrange() end",
            "function reaper.MarkProjectDirty() end; function reaper.ShowMessageBox() error('unexpected warning') end",
            script[:helpers_end],
            "discard()",
            "tracks[4].name = '[work] Kept part again'",
            "for _,t in ipairs(tracks) do io.write(t.name, ':', t.values.I_FOLDERDEPTH, ':', t.ext['P_EXT:vgt_working_copy'] or '', ';') end",
        ]
    )
    assert _run(lua_program).stdout == "User folder:1:;User child:-1:;[work]:0:;[work] Kept part again:0:;"


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
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) return true, '' end",
            "function reaper.GetMediaTrackInfo_Value() return 0 end",
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
