from pathlib import Path
import json
import os
import subprocess


VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase0_apply.py"
PHASE1_VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase1_apply.py"
STEM_VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_stem_apply.py"
TRANSCRIPTION_VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_transcription_apply.py"
SYNC_VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase1_sync.py"
APPLY_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_initialize.lua"
SYNC_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_sync.lua"
LUA = os.environ.get("VGT_TEST_LUA", "lua")


def test_apply_uses_reaper_api_and_never_edits_rpp_text() -> None:
    script = APPLY_SCRIPT.read_text()
    assert "reaper.InsertTrackAtIndex" in script
    assert "reaper.DeleteTrack" in script
    assert "reaper.AddMediaItemToTrack" in script
    assert "(managed[reaper.GetTrackGUID(track)] or track_is_marked_managed(track)) and starts_with_vgt(track)" in script
    assert "GetSetProjectInfo_String" not in script


def test_apply_asks_for_a_reference_track_and_names_the_folder_after_it() -> None:
    script = APPLY_SCRIPT.read_text()
    # Interactive pick with a headless override, then the folder is named after the choice.
    assert "gfx.showmenu" in script
    assert 'reaper.GetExtState("vgt", "reference_index")' in script
    assert 'PREFIX .. " " .. track_name(reference)' in script


def test_apply_prefers_the_persisted_reference_over_the_candidate_picker() -> None:
    """The persisted GUID is authoritative once recorded (issue #136): apply
    must check it first and only fall through to choose_reference/candidate_tracks
    when nothing has been persisted yet."""
    script = APPLY_SCRIPT.read_text()
    apply_start = script.index("local function apply()")
    resolution = script[apply_start : script.index("local reference_guid = reaper.GetTrackGUID(reference)", apply_start)]
    assert "local persisted_guid = persisted_reference_guid()" in resolution
    assert "resolve_persisted_reference(persisted_guid)" in resolution
    assert resolution.index("persisted_reference_guid()") < resolution.index("choose_reference(candidate_tracks())")


def test_has_file_backed_media_requires_a_real_file_backed_item() -> None:
    """REAPER-native generators (e.g. a count-in `<SOURCE CLICK>` track) have
    an active take but no underlying file, so they must not count."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function read_sidecar_body()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "local tracks = {",
            "  {items = {{take = {source = 'song.mp3'}}}},",  # file-backed
            "  {items = {{take = {source = ''}}}},",  # click-source, no file
            "  {items = {}},",  # no items at all
            "}",
            "function reaper.CountTrackMediaItems(track) return #track.items end",
            "function reaper.GetTrackMediaItem(track, index) return track.items[index + 1] end",
            "function reaper.GetActiveTake(item) return item.take end",
            "function reaper.GetMediaItemTake_Source(take) return take.source end",
            "function reaper.GetMediaSourceFileName(source, buf) return source end",
            script[:helpers_end],
            "io.write(tostring(has_file_backed_media(tracks[1])), ':', tostring(has_file_backed_media(tracks[2])), ':', tostring(has_file_backed_media(tracks[3])))",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "true:false:false"


def test_candidate_tracks_excludes_vgt_and_non_file_backed_tracks() -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function read_sidecar_body()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "local tracks = {",
            "  {name = 'Click', items = {{take = {source = ''}}}},",
            "  {name = 'Song A', items = {{take = {source = 'a.mp3'}}}},",
            "  {name = '[vgt] Beats', items = {{take = {source = 'b.wav'}}}},",
            "  {name = 'Song B', items = {{take = {source = 'b.mp3'}}}},",
            "}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.CountTrackMediaItems(track) return #track.items end",
            "function reaper.GetTrackMediaItem(track, index) return track.items[index + 1] end",
            "function reaper.GetActiveTake(item) return item.take end",
            "function reaper.GetMediaItemTake_Source(take) return take.source end",
            "function reaper.GetMediaSourceFileName(source, buf) return source end",
            script[:helpers_end],
            "local names = {}",
            "for _, track in ipairs(candidate_tracks()) do names[#names + 1] = track.name end",
            "io.write(table.concat(names, ','))",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "Song A,Song B"


def test_choose_reference_skips_the_menu_for_a_lone_candidate() -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function read_sidecar_body()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "function reaper.GetExtState() return '' end",
            "gfx = setmetatable({}, {__index = function() error('menu should not be shown for a lone candidate') end})",
            script[:helpers_end],
            "local track = {name = 'Only Song'}",
            "io.write(tostring(choose_reference({track}) == track))",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "true"


def test_choose_reference_still_honors_an_automation_override_with_one_candidate() -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function read_sidecar_body()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "function reaper.GetExtState() return '0' end",
            "gfx = setmetatable({}, {__index = function() error('menu should not be shown when forced') end})",
            script[:helpers_end],
            "local track = {name = 'Only Song'}",
            "io.write(tostring(choose_reference({track}) == track))",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "true"


def test_has_file_backed_media_rejects_a_track_with_more_than_one_file_backed_item() -> None:
    """An unambiguous reference is exactly one file-backed mix item (issue #136):
    two file-backed items on the same track is rejected, not silently accepted
    as a candidate whose first item wins."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function read_sidecar_body()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "local track = {items = {{take = {source = 'a.mp3'}}, {take = {source = 'b.mp3'}}}}",
            "function reaper.CountTrackMediaItems(t) return #t.items end",
            "function reaper.GetTrackMediaItem(t, index) return t.items[index + 1] end",
            "function reaper.GetActiveTake(item) return item.take end",
            "function reaper.GetMediaItemTake_Source(take) return take.source end",
            "function reaper.GetMediaSourceFileName(source, buf) return source end",
            script[:helpers_end],
            "io.write(file_backed_item_count(track), ':', tostring(has_file_backed_media(track)))",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "2:false"


def test_persisted_reference_guid_reads_the_sidecar_config_field(tmp_path: Path) -> None:
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"config": {"reference_track_guid": "{A11CE-0001}"}}))
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function read_managed_guids()")
    lua_program = "\n".join(
        [
            "reaper = {EnumProjects = function() return true, arg[1] end}",
            script[:helpers_end],
            "io.write('[', persisted_reference_guid(), ']')",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "[{A11CE-0001}]"


def test_persisted_reference_guid_is_empty_before_first_initialization(tmp_path: Path) -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function read_managed_guids()")
    lua_program = "\n".join(
        [
            "reaper = {EnumProjects = function() return true, arg[1] end}",
            script[:helpers_end],
            "io.write('[', persisted_reference_guid(), ']')",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "[]"


def _resolve_persisted_reference_lua_mock(tracks_literal: str) -> str:
    return "\n".join(
        [
            f"local tracks = {tracks_literal}",
            "reaper = {}",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackGUID(track) return track.guid end",
            "function reaper.GetTrackName(track) return true, track.name end",
            "function reaper.CountTrackMediaItems(track) return #track.items end",
            "function reaper.GetTrackMediaItem(track, index) return track.items[index + 1] end",
            "function reaper.GetActiveTake(item) return item.take end",
            "function reaper.GetMediaItemTake_Source(take) return take.source end",
            "function reaper.GetMediaSourceFileName(source, buf) return source end",
        ]
    )


def test_resolve_persisted_reference_reuses_a_valid_non_vgt_single_item_track() -> None:
    """Later applies in a multi-track project must reuse the persisted GUID
    without prompting -- reference resolution alone must never touch gfx."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function add_labeled_item(")
    lua_program = "\n".join(
        [
            _resolve_persisted_reference_lua_mock(
                "{"
                "{name = 'Song A', guid = '{A}', items = {{take = {source = 'a.mp3'}}}},"
                "{name = 'Song B', guid = '{B}', items = {{take = {source = 'b.mp3'}}}},"
                "}"
            ),
            "gfx = setmetatable({}, {__index = function() error('must not prompt for a persisted reference') end})",
            script[:helpers_end],
            "local reference = resolve_persisted_reference('{B}')",
            "io.write(tostring(reference.guid))",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "{B}"


def test_resolve_persisted_reference_stops_with_a_recovery_message_when_the_track_is_missing() -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function add_labeled_item(")
    lua_program = "\n".join(
        [
            _resolve_persisted_reference_lua_mock("{}"),
            script[:helpers_end],
            "resolve_persisted_reference('{GONE}')",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True)
    assert result.returncode != 0
    assert "no longer exists" in result.stderr
    assert "reference_track_guid" in result.stderr


def test_resolve_persisted_reference_stops_when_the_track_is_now_vgt_managed() -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function add_labeled_item(")
    lua_program = "\n".join(
        [
            _resolve_persisted_reference_lua_mock(
                "{{name = '[vgt] Beats', guid = '{A}', items = {}}}"
            ),
            script[:helpers_end],
            "resolve_persisted_reference('{A}')",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True)
    assert result.returncode != 0
    assert "[vgt]-managed" in result.stderr


def test_resolve_persisted_reference_stops_when_the_track_lost_its_file_backed_media() -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function add_labeled_item(")
    lua_program = "\n".join(
        [
            _resolve_persisted_reference_lua_mock(
                "{{name = 'Song A', guid = '{A}', items = {}}}"
            ),
            script[:helpers_end],
            "resolve_persisted_reference('{A}')",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True)
    assert result.returncode != 0
    assert "no longer has any file-backed media" in result.stderr


def test_resolve_persisted_reference_stops_on_an_ambiguous_multi_item_track() -> None:
    """Reject a track with more than one file-backed item rather than silently
    picking the first (issue #136)."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function add_labeled_item(")
    lua_program = "\n".join(
        [
            _resolve_persisted_reference_lua_mock(
                "{{name = 'Song A', guid = '{A}', items = {{take = {source = 'a1.mp3'}}, {take = {source = 'a2.mp3'}}}}}"
            ),
            script[:helpers_end],
            "resolve_persisted_reference('{A}')",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True)
    assert result.returncode != 0
    assert "ambiguous" in result.stderr
    assert "exactly one file-backed mix item" in result.stderr


def test_reference_start_and_end_derives_from_the_single_file_backed_item_only() -> None:
    """A reference track's placements must span only its one file-backed item,
    never every item on the track (issue #136)."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function parse_time_signature(")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "local reference = {items = {"
            "{position = 100, length = 1, take = {source = ''}},"  # non-file-backed decoy far outside the real span
            "{position = 10, length = 4, take = {source = 'song.mp3'}},"
            "}}",
            "function reaper.CountTrackMediaItems(t) return #t.items end",
            "function reaper.GetTrackMediaItem(t, index) return t.items[index + 1] end",
            "function reaper.GetActiveTake(item) return item.take end",
            "function reaper.GetMediaItemTake_Source(take) return take.source end",
            "function reaper.GetMediaSourceFileName(source, buf) return source end",
            "function reaper.GetMediaItemInfo_Value(item, key) if key == 'D_POSITION' then return item.position else return item.length end end",
            script[:helpers_end],
            "local start_time, end_time = reference_start_and_end(reference)",
            "io.write(start_time, ':', end_time)",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "10:14"


def test_apply_declares_and_persists_guitar_type_with_an_automation_override() -> None:
    script = APPLY_SCRIPT.read_text()
    assert 'reaper.GetExtState("vgt", "guitar_type")' in script
    assert 'gfx.showmenu("Electric|Acoustic")' in script
    assert '"guitar_type": "%s"' in script
    assert "local guitar_type = choose_guitar_type()" in script


def test_phase1_apply_reads_analysis_and_uses_only_reaper_api() -> None:
    script = APPLY_SCRIPT.read_text()
    assert "decode_json" in script
    assert "SetTempoTimeSigMarker" in script
    assert "AddProjectMarker2" in script
    assert '"C_BEATATTACHMODE", 0' in script
    assert 'SetMediaItemInfo_Value(item, "C_LOCK", 1)' in script
    assert 'local CHORDS_NAME = PREFIX .. " Chords"' in script
    assert 'local BEATS_NAME = PREFIX .. " Beats"' in script
    assert "read_managed_region_ids" in script
    assert "managed[region_id]" in script
    assert "reaper.DeleteProjectMarker(0, region_id, true)" in script
    assert '"managed_region_ids"' in script
    assert "tempo.downbeat_detected ~= true" in script
    assert "type(tempo.beat_times) == \"table\"" in script


def test_beats_and_chords_tracks_are_both_unmuted() -> None:
    script = APPLY_SCRIPT.read_text()
    # Both are item-label-only tracks with no audio, so muting them would
    # only make their labels unreadable in REAPER (issue #41 regression).
    assert 'reaper.SetMediaTrackInfo_Value(track, "B_MUTE", muted and 1 or 0)' in script
    assert "local function offer_beats_track(index, tempo, reference_start, reference_end, managed_tracks)" in script
    assert "local beats = add_locked_track(index, BEATS_NAME, false)" in script
    assert "offer_beats_track(insert_at + 1, tempo, reference_start, reference_end, managed_tracks)" in script
    assert "add_locked_track(reaper.CountTracks(0), CHORDS_NAME, false)" in script


def test_key_display_uses_the_effective_value_as_a_locked_unmuted_label() -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join(
        [
            _click_track_lua_mock(Path("song.RPP")),
            script[:helpers_end],
            "local managed_tracks = {}",
            "add_key_track(0, {root = 'E', scale = 'minor'}, 10, 14, managed_tracks)",
            "local track, item = __tracks[1], __items[1]",
            "io.write(track.name, ':', track.mute, ':', item.notes, ':', item.values.C_LOCK, ':', item.values.D_POSITION, ':', item.values.D_LENGTH, ':', #managed_tracks)",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "[vgt] Key:0:E minor:1:10:4:1"


def test_key_display_requires_a_complete_key_value() -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join(
        [
            _click_track_lua_mock(Path("song.RPP")),
            script[:helpers_end],
            "local managed_tracks = {}",
            "add_key_track(0, {root = 'E'}, 10, 14, managed_tracks)",
            "add_key_track(0, {scale = 'minor'}, 10, 14, managed_tracks)",
            "io.write(#__tracks, ':', #__items, ':', #managed_tracks)",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "0:0:0"


def test_chord_items_are_added_unlocked_so_they_stay_editable() -> None:
    script = APPLY_SCRIPT.read_text()
    # Chord items are the editing surface (issue #17): unlike beats (locked,
    # default arg), the chords loop explicitly passes locked = false.
    assert 'add_labeled_item(chords_track, reference_start + (tonumber(chord.start_seconds) or 0), reference_start + (tonumber(chord.end_seconds) or 0), tostring(chord.chord or chord.label or "N"), false)' in script
    assert "if locked ~= false then reaper.SetMediaItemInfo_Value(item, \"C_LOCK\", 1) end" in script


def test_click_track_is_muted_and_named_distinctly_from_beats() -> None:
    script = APPLY_SCRIPT.read_text()
    assert 'local CLICK_NAME = PREFIX .. " Click"' in script
    assert "add_locked_track(index, CLICK_NAME, true)" in script
    assert "add_click_track(reaper.CountTracks(0), tempo, reference_start, managed_tracks, artifact_namespace)" in script


def _click_track_lua_mock(rpp_path: Path) -> str:
    return "\n".join(
        [
            "reaper = {}",
            "function reaper.EnumProjects(idx, buf) return true, arg[1] end",
            "local tracks = {}",
            "function reaper.InsertTrackAtIndex(index, defaults) tracks[index + 1] = {index = index, name = nil, mute = 0, values = {}} end",
            "function reaper.GetTrack(proj, index) return tracks[index + 1] end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set)",
            "  if set then",
            "    if key == 'P_NAME' then track.name = value end",
            "    track.ext = track.ext or {}",
            "    track.ext[key] = value",
            "    return",
            "  end",
            "  if key == 'P_NAME' then return true, track.name end",
            "  return true, (track.ext and track.ext[key]) or ''",
            "end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) track.values[key] = value; if key == 'B_MUTE' then track.mute = value end end",
            "function reaper.ColorToNative(red, green, blue) _G.__color_request = {red, green, blue}; return 0x3A9BD8 end",
            "function reaper.PCM_Source_CreateFromFile(path) return {path = path} end",
            "function reaper.GetMediaSourceLength(source) return 2.5, false end",
            "local items = {}",
            "function reaper.AddMediaItemToTrack(track) local item = {track = track, values = {}}; items[#items + 1] = item; return item end",
            "function reaper.SetMediaItemInfo_Value(item, key, value) item.values[key] = value end",
            "function reaper.GetSetMediaItemInfo_String(item, key, value) if key == 'P_NOTES' then item.notes = value end end",
            "function reaper.AddTakeToMediaItem(item) local take = {}; item.take = take; return take end",
            "function reaper.GetSetMediaItemTakeInfo_String(take, key, value) if key == 'P_NAME' then take.name = value end end",
            "function reaper.SetMediaItemTake_Source(take, source) take.source = source end",
            "_G.__items = items",
            "_G.__tracks = tracks",
        ]
    )


def test_add_click_track_imports_the_rendered_click_wav_muted(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    namespace_dir = tmp_path / "vgt" / "song-abc123"
    namespace_dir.mkdir(parents=True)
    click_wav = namespace_dir / "tempo-click.wav"
    click_wav.write_bytes(b"RIFF....WAVEfmt ")

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join(
        [
            _click_track_lua_mock(rpp),
            script[:helpers_end],
            "local managed_tracks = {}",
            "add_click_track(5, {click_artifact_path = 'tempo-click.wav'}, 1.5, managed_tracks, 'song-abc123')",
            "local track = __tracks[6]",
            "local item = __items[1]",
            "io.write(track.name, ':', track.mute, ':', item.values.D_POSITION, ':', item.values.D_LENGTH, ':', item.values.C_BEATATTACHMODE, ':', #managed_tracks)",
        ]
    )
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "[vgt] Click:1:1.5:2.5:0:1"


def test_add_click_track_is_absent_when_no_click_artifact_exists(tmp_path: Path) -> None:
    """Only added when `vgt analyze` has produced the click WAV -- absent gracefully otherwise."""
    rpp = tmp_path / "song.RPP"

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join(
        [
            _click_track_lua_mock(rpp),
            script[:helpers_end],
            "local managed_tracks = {}",
            "add_click_track(5, {click_artifact_path = 'tempo-click.wav'}, 1.5, managed_tracks, 'song-abc123')",
            "io.write(#managed_tracks, ':', #__tracks, ':', #__items)",
        ]
    )
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "0:0:0"


def test_add_click_track_is_absent_when_no_artifact_namespace_recorded(tmp_path: Path) -> None:
    """Absent gracefully when the sidecar has no `artifact_namespace` yet (pre-#44 sidecars, or before the
    first `vgt analyze` run), even if a same-named file happens to sit next to the RPP."""
    rpp = tmp_path / "song.RPP"
    (tmp_path / "tempo-click.wav").write_bytes(b"RIFF....WAVEfmt ")

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join(
        [
            _click_track_lua_mock(rpp),
            script[:helpers_end],
            "local managed_tracks = {}",
            "add_click_track(5, {click_artifact_path = 'tempo-click.wav'}, 1.5, managed_tracks, nil)",
            "io.write(#managed_tracks, ':', #__tracks, ':', #__items)",
        ]
    )
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "0:0:0"


def test_add_stem_tracks_imports_only_committed_namespace_wavs_time_based_and_offset(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    stems_dir = tmp_path / "vgt" / "song-abc123" / "stems"
    stems_dir.mkdir(parents=True)
    for filename in ("vocals.wav", "instrumental.wav", "bass.wav", "drums.wav", "guitar.wav", "backing-no-guitar.wav"):
        (stems_dir / filename).write_bytes(b"RIFF....WAVEfmt ")

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    # `file` carries the song-folder-relative path the separator commits.
    artifacts = ", ".join(f"{name} = {{file = 'vgt/song-abc123/stems/{filename}', size_bytes = 16, duration_seconds = 2.5}}" for name, filename in (
        ("vocals", "vocals.wav"), ("instrumental", "instrumental.wav"), ("bass", "bass.wav"),
        ("drums", "drums.wav"), ("guitar", "guitar.wav"), ("backing", "backing-no-guitar.wav"),
    ))
    lua_program = "\n".join([
        _click_track_lua_mock(rpp),
        script[:helpers_end],
        "local managed_tracks = {}",
        f"add_stem_tracks(3, {{artifact_namespace = 'song-abc123', artifacts = {{{artifacts}}}}}, 12.25, managed_tracks)",
        "for i = 4, 9 do local track = __tracks[i]; local item = __items[i - 3]; io.write(track.name, ':', item.values.D_POSITION, ':', item.values.D_LENGTH, ':', item.values.C_BEATATTACHMODE, ';') end",
        "io.write('#', #managed_tracks)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == (
        "[vgt] Vocals:12.25:2.5:0;[vgt] Instrumental:12.25:2.5:0;[vgt] Bass:12.25:2.5:0;"
        "[vgt] Drums:12.25:2.5:0;[vgt] Guitar:12.25:2.5:0;[vgt] Backing (no guitar):12.25:2.5:0;#6"
    )


def test_add_stem_tracks_accepts_the_file_form_the_separator_actually_commits(tmp_path: Path) -> None:
    """The Lua reader and the Python writer must agree on how `artifact.file`
    is spelled. Deriving the fixture from separation.py rather than restating
    the reader's own convention is what makes this catch a drift between them
    -- a hand-written fixture just re-encodes whichever side is wrong."""
    from vgt.separation import ARTIFACT_FILENAMES, stems_dir

    rpp = tmp_path / "song.RPP"
    namespace = "song-abc123"
    committed = stems_dir(rpp, namespace) / ARTIFACT_FILENAMES["vocals"]
    committed.parent.mkdir(parents=True)
    committed.write_bytes(b"RIFF....WAVEfmt ")
    recorded_file = str(committed.relative_to(rpp.parent))

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp),
        "function reaper.ShowConsoleMsg(message) io.stderr:write(message) end",
        script[:helpers_end],
        "local managed_tracks = {}",
        f"add_stem_tracks(3, {{artifact_namespace = '{namespace}', artifacts = {{vocals = "
        f"{{file = '{recorded_file}', size_bytes = 16, duration_seconds = 2.5}}}}}}, 0, managed_tracks)",
        "io.write(#managed_tracks, ':', __tracks[4].name)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert "outside the expected stem namespace" not in result.stderr
    assert result.stdout == "1:[vgt] Vocals"


def test_add_stem_tracks_imports_opt_in_strings_and_piano(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    stem_dir = tmp_path / "vgt" / "song-abc123" / "stems"
    stem_dir.mkdir(parents=True)
    for filename in ("strings.wav", "piano.wav"):
        (stem_dir / filename).write_bytes(b"RIFF....WAVEfmt ")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp), script[:helpers_end], "local managed_tracks = {}",
        "add_stem_tracks(3, {artifact_namespace = 'song-abc123', artifacts = {strings = {file = 'vgt/song-abc123/stems/strings.wav', size_bytes = 16, duration_seconds = 2.5}, piano = {file = 'vgt/song-abc123/stems/piano.wav', size_bytes = 16, duration_seconds = 2.5}}}, 0, managed_tracks)",
        "io.write(__tracks[4].name, ':', __tracks[5].name, ':', #managed_tracks)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "[vgt] Strings:[vgt] Keys / Piano:2"


def test_add_stem_tracks_skips_missing_or_outside_namespace_artifacts_with_a_warning(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp),
        "function reaper.ShowConsoleMsg(message) io.stderr:write(message) end",
        script[:helpers_end],
        "local managed_tracks = {}",
        "add_stem_tracks(3, {artifact_namespace = 'song-abc123', artifacts = {vocals = {file = '../user.wav'}}}, 0, managed_tracks)",
        "io.write(#managed_tracks, ':', #__tracks, ':', #__items)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "0:0:0"
    assert "skipping stem vocals: sidecar file is outside the expected stem namespace" in result.stderr


def test_transcription_tracks_follow_their_stems_and_are_unmuted_time_based(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    namespace = tmp_path / "vgt" / "song-abc123"
    (namespace / "stems").mkdir(parents=True)
    (namespace / "transcription").mkdir()
    for name in ("guitar", "bass"):
        (namespace / "stems" / f"{name}.wav").write_bytes(b"RIFF....WAVEfmt ")
        (namespace / "transcription" / f"{name}.mid").write_bytes(b"MThd")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp), script[:helpers_end], "local managed_tracks = {}",
        "add_stem_tracks(0, {artifact_namespace = 'song-abc123', artifacts = {bass = {file = 'vgt/song-abc123/stems/bass.wav', size_bytes = 16, duration_seconds = 2.5}, guitar = {file = 'vgt/song-abc123/stems/guitar.wav', size_bytes = 16, duration_seconds = 2.5}}}, {targets = {guitar = {status = 'transcribed', midi_file = 'transcription/guitar.mid'}, bass = {status = 'transcribed', midi_file = 'transcription/bass.mid'}}}, 12.25, managed_tracks)",
        "for i, track in ipairs(__tracks) do local item = __items[i]; io.write(track.name, ':', track.mute, ':', item.values.D_POSITION, ':', item.values.C_BEATATTACHMODE, ';') end io.write('#', #managed_tracks)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == (
        "[vgt] Bass:0:12.25:0;[vgt] Bass Ref (MIDI):0:12.25:0;"
        "[vgt] Guitar:0:12.25:0;[vgt] Guitar Ref (MIDI):0:12.25:0;#4"
    )


def test_drumscript_record_imports_channel_10_polyphony_immediately_after_drums(tmp_path: Path) -> None:
    """The generic reference-MIDI importer is also the drums contract.

    This remains an offline Lua-stub test: it verifies the exact source handed
    to REAPER, rather than trying to interpret MIDI in a live project.
    """
    rpp = tmp_path / "song.RPP"
    namespace = tmp_path / "vgt" / "song-abc123"
    (namespace / "stems").mkdir(parents=True)
    (namespace / "transcription").mkdir()
    (namespace / "stems" / "drums.wav").write_bytes(b"RIFF....WAVEfmt ")
    # Two zero-delta note-ons on 0x99 are simultaneous GM percussion hits.
    drum_midi = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk\x00\x00\x00\x0c\x00\x99\x24\x64\x00\x99\x26\x64\x00\xff\x2f\x00"
    midi_path = namespace / "transcription" / "drums.mid"
    midi_path.write_bytes(drum_midi)

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp), script[:helpers_end], "local managed_tracks = {}",
        "add_stem_tracks(0, {artifact_namespace = 'song-abc123', artifacts = {drums = {file = 'vgt/song-abc123/stems/drums.wav', size_bytes = 16, duration_seconds = 2.5}}}, {targets = {drums = {status = 'transcribed', midi_file = 'transcription/drums.mid', events_file = 'transcription/drums.json'}}}, 7.75, managed_tracks)",
        "local item = __items[2]; io.write(__tracks[1].name, ';', __tracks[2].name, ':', item.values.D_POSITION, ':', item.values.C_BEATATTACHMODE, ':', item.take.source.path, ':', #managed_tracks)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)

    assert result.stdout == f"[vgt] Drums;[vgt] Drums Ref (MIDI):7.75:0:{midi_path}:2"
    # The importer receives the recorded artifact itself; it neither rewrites
    # MIDI bytes nor has a MIDI-event transformation step that could collapse
    # the simultaneous channel-10 hits.
    assert midi_path.read_bytes() == drum_midi
    assert b"\x00\x99\x24\x64\x00\x99\x26\x64" in midi_path.read_bytes()


def test_removal_touches_only_the_recorded_vgt_drum_reference(tmp_path: Path) -> None:
    """A stale managed drum reference is reconciled without touching lookalikes."""
    rpp = tmp_path / "song.RPP"
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"managed_track_guids": ["{A11CE-0001}"]}), encoding="utf-8")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        "local tracks = {{name = '[vgt] Drums Ref (MIDI)', guid = '{A11CE-0001}'}, {name = '[vgt] Drums Ref (MIDI)', guid = '{B00B-0002}'}, {name = 'User Drums', guid = '{C0DE-0003}'}}",
        "reaper = {}",
        "function reaper.EnumProjects() return true, arg[1] end",
        "function reaper.CountTracks() return #tracks end",
        "function reaper.GetTrack(_, index) return tracks[index + 1] end",
        "function reaper.GetTrackGUID(track) return track.guid end",
        "function reaper.GetTrackName(track) return true, track.name end",
        "function reaper.GetSetMediaTrackInfo_String(track, key, buf, set) if key == 'P_EXT:vgt_managed' and not set then return true, '' end end",
        "function reaper.DeleteTrack(track) for i, candidate in ipairs(tracks) do if candidate == track then table.remove(tracks, i); return end end end",
        script[:helpers_end],
        "remove_previous_managed_tracks()",
        "for _, track in ipairs(tracks) do io.write(track.name, ':', track.guid, ';') end",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)

    assert result.stdout == "[vgt] Drums Ref (MIDI):{B00B-0002};User Drums:{C0DE-0003};"


def _mark_lua_mock() -> str:
    return "\n".join(
        [
            "function reaper.GetSetMediaTrackInfo_String(track, key, buf, set)",
            "  if key == 'P_EXT:vgt_managed' and not set then return true, track.managed and '1' or '' end",
            "end",
        ]
    )


def test_removal_falls_back_to_the_durable_per_track_mark_when_the_sidecar_guid_list_is_stale(tmp_path: Path) -> None:
    """A GUID list that never reached disk (crash between building the [vgt]
    block and write_settings, a restored Backups/ copy, or a copied project
    folder) must not cause a second [vgt] folder to be appended on re-apply:
    the durable per-track mark set at creation time (see mark_track_managed)
    is independent evidence of ownership, honored even when
    managed_track_guids is empty."""
    rpp = tmp_path / "song.RPP"
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"managed_track_guids": []}), encoding="utf-8")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join(
        [
            "local tracks = {"
            "{name = '[vgt] The Song', guid = '{A11CE-0001}', managed = true},"
            "{name = '[vgt] Vocals', guid = '{A11CE-0002}', managed = true},"
            "{name = 'User Song', guid = '{C0DE-0003}', managed = false},"
            "}",
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackGUID(track) return track.guid end",
            "function reaper.GetTrackName(track) return true, track.name end",
            _mark_lua_mock(),
            "function reaper.DeleteTrack(track) for i, candidate in ipairs(tracks) do if candidate == track then table.remove(tracks, i); return end end end",
            script[:helpers_end],
            "remove_previous_managed_tracks()",
            "for _, track in ipairs(tracks) do io.write(track.name, ':', track.guid, ';') end",
        ]
    )
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)

    assert result.stdout == "User Song:{C0DE-0003};"


def test_removal_preserves_a_marked_track_the_user_renamed_away_from_the_vgt_prefix(tmp_path: Path) -> None:
    """The durable mark alone is still not enough: a track the user has since
    renamed away from the [vgt] prefix is theirs now and must survive, exactly
    like the existing GUID-based guard (see remove_previous_managed_tracks)."""
    rpp = tmp_path / "song.RPP"
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"managed_track_guids": []}), encoding="utf-8")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join(
        [
            "local tracks = {"
            "{name = 'My Reclaimed Track', guid = '{A11CE-0001}', managed = true},"
            "{name = '[vgt] Vocals', guid = '{A11CE-0002}', managed = true},"
            "}",
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackGUID(track) return track.guid end",
            "function reaper.GetTrackName(track) return true, track.name end",
            _mark_lua_mock(),
            "function reaper.DeleteTrack(track) for i, candidate in ipairs(tracks) do if candidate == track then table.remove(tracks, i); return end end end",
            script[:helpers_end],
            "remove_previous_managed_tracks()",
            "for _, track in ipairs(tracks) do io.write(track.name, ':', track.guid, ';') end",
        ]
    )
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)

    assert result.stdout == "My Reclaimed Track:{A11CE-0001};"


def test_variant_reconciliation_removes_generated_tracks_but_preserves_a_working_copy(tmp_path: Path) -> None:
    """Discard is represented by omitting its variant on the next apply. The
    reconciliation removal phase may clear old generated variants, but it must
    leave a `[work]` copy of the selected candidate alone before retained
    variants are rebuilt in their stable order."""
    rpp = tmp_path / "song.RPP"
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"managed_track_guids": []}), encoding="utf-8")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join(
        [
            "local tracks = {"
            "{name = '[vgt] Guitar Ref — Discarded (MIDI)', guid = '{A11CE-0001}', managed = true},"
            "{name = '[work] Guitar Ref — Clean (MIDI)', guid = '{C0DE-0002}', managed = false},"
            "{name = '[vgt] Guitar Ref — Clean (MIDI)', guid = '{A11CE-0003}', managed = true},"
            "}",
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.CountTracks() return #tracks end",
            "function reaper.GetTrack(_, index) return tracks[index + 1] end",
            "function reaper.GetTrackGUID(track) return track.guid end",
            "function reaper.GetTrackName(track) return true, track.name end",
            _mark_lua_mock(),
            "function reaper.DeleteTrack(track) for i, candidate in ipairs(tracks) do if candidate == track then table.remove(tracks, i); return end end end",
            script[:helpers_end],
            "remove_previous_managed_tracks()",
            "for _, track in ipairs(tracks) do io.write(track.name, ';') end",
        ]
    )
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "[work] Guitar Ref — Clean (MIDI);"


def test_add_locked_track_marks_the_track_with_a_durable_ext_state() -> None:
    """`mark_track_managed` must be the thing add_locked_track (and thus every
    [vgt]-owned track it builds) actually calls, so ownership survives even if
    the sidecar write that normally records it never happens."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function reference_start_and_end")
    lua_program = "\n".join(
        [
            _click_track_lua_mock(Path("song.RPP")),
            script[:helpers_end],
            "add_locked_track(0, '[vgt] Test', false)",
            "local track = __tracks[1]",
            "io.write(tostring(track.ext['P_EXT:vgt_managed']))",
        ]
    )
    result = subprocess.run([LUA, "-", "song.RPP"], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "1"


def test_apply_marks_the_top_level_folder_track_as_managed_before_write_settings() -> None:
    """The one managed track add_locked_track does not build -- the top-level
    [vgt] folder -- must be marked directly in apply(), or a folder-only
    re-apply (nothing nested under it) would still lose its durable mark."""
    script = APPLY_SCRIPT.read_text()
    apply_start = script.index("local function apply()")
    write_settings_call = script.index("write_settings(managed_tracks", apply_start)
    folder_section = script[apply_start:write_settings_call]
    assert "mark_track_managed(folder)" in folder_section
    assert folder_section.index("mark_track_managed(folder)") > folder_section.index('reaper.GetSetMediaTrackInfo_String(folder, "P_NAME"')


def test_transcription_skips_expected_states_and_appends_orphans_in_target_order(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    namespace = tmp_path / "vgt" / "song-abc123" / "transcription"
    namespace.mkdir(parents=True)
    for name in ("guitar", "bass", "original"):
        (namespace / f"{name}.mid").write_bytes(b"MThd")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp), "function reaper.ShowConsoleMsg(message) io.stderr:write(message) end", script[:helpers_end], "local managed_tracks = {}",
        "add_stem_tracks(0, {artifact_namespace = 'song-abc123', artifacts = {}}, {targets = {guitar = {status = 'transcribed', midi_file = 'transcription/guitar.mid'}, bass = {status = 'transcribed', midi_file = 'transcription/bass.mid'}, original = {status = 'transcribed', midi_file = 'transcription/original.mid'}, vocals = {status = 'skipped-missing-source'}, drums = {status = 'error'}}}, 2, managed_tracks)",
        "for _, track in ipairs(__tracks) do io.write(track.name, ';') end io.write('#', #managed_tracks)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "[vgt] Guitar Ref (MIDI);[vgt] Bass Ref (MIDI);[vgt] Original Ref (MIDI);#3"
    assert result.stderr == ""


def test_transcription_rejects_outside_namespace_midi_path(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp), "function reaper.ShowConsoleMsg(message) io.stderr:write(message) end", script[:helpers_end], "local managed_tracks = {}",
        "add_stem_tracks(0, {artifact_namespace = 'song-abc123', artifacts = {}}, {targets = {guitar = {status = 'transcribed', midi_file = '../user.mid'}}}, 0, managed_tracks)",
        "io.write(#managed_tracks, ':', #__tracks)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "0:0"
    assert "skipping transcription guitar: sidecar MIDI file is outside the expected transcription namespace" in result.stderr


def test_drum_transcription_rejects_any_path_except_its_recorded_namespace_file(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp), "function reaper.ShowConsoleMsg(message) io.stderr:write(message) end", script[:helpers_end], "local managed_tracks = {}",
        "add_stem_tracks(0, {artifact_namespace = 'song-abc123', artifacts = {}}, {targets = {drums = {status = 'transcribed', midi_file = 'transcription/guitar.mid'}}}, 0, managed_tracks)",
        "io.write(#managed_tracks, ':', #__tracks)",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)

    assert result.stdout == "0:0"
    assert "skipping transcription drums: sidecar MIDI file is outside the expected transcription namespace" in result.stderr


def test_transcription_import_source_and_opt_in_verifier_are_present() -> None:
    script = APPLY_SCRIPT.read_text()
    assert "local function add_reference_midi_tracks(index, target, transcription, reference_start, managed_tracks, artifact_namespace)" in script
    assert "local function add_reference_midi_variant(index, target, variant_id" in script
    assert "variant.status ~= \"transcribed\"" in script
    assert '" Ref — " .. label .. " (MIDI)"' in script
    assert 'record.midi_file ~= expected_filename' in script
    assert "analysis and analysis.transcription" in script
    assert script.index("remove_previous_managed_tracks()") < script.index("add_stem_tracks(reaper.CountTracks(0)")
    assert TRANSCRIPTION_VERIFY_SCRIPT.is_file()


def test_variant_transcriptions_import_in_order_after_their_source_and_mark_selection(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    namespace = tmp_path / "vgt" / "song-abc123"
    (namespace / "transcription" / "guitar").mkdir(parents=True)
    for variant_id in ("detail", "clean"):
        (namespace / "transcription" / "guitar" / f"{variant_id}.mid").write_bytes(b"MThd")
    guitar_wav = namespace / "stems" / "guitar.wav"
    guitar_wav.parent.mkdir()
    guitar_wav.write_bytes(b"RIFF....WAVE")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp), script[:helpers_end], "local managed_tracks = {}",
        "add_stem_tracks(0, {artifact_namespace = 'song-abc123', artifacts = {guitar = {file = 'vgt/song-abc123/stems/guitar.wav', size_bytes = 12, duration_seconds = 1}}}, {targets = {guitar = {selected_variant_id = 'clean', variant_order = {'detail', 'clean'}, variants = {detail = {label = 'Detail', status = 'transcribed', midi_file = 'transcription/guitar/detail.mid'}, clean = {label = 'Clean', status = 'transcribed', midi_file = 'transcription/guitar/clean.mid'}}}}}, 2, managed_tracks)",
        "for _, track in ipairs(__tracks) do io.write(track.name, ':', track.mute, ':', tostring(track.values.I_CUSTOMCOLOR), ';') end io.write('rgb=', table.concat(__color_request, ','))",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == (
        "[vgt] Guitar:0:nil;[vgt] Guitar Ref — Detail (MIDI):0:nil;"
        "[vgt] Guitar Ref — Clean (MIDI):0:20618200;rgb=216,155,58"
    )


def test_variant_transcriptions_skip_missing_and_error_and_reject_non_scoped_paths(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    namespace = tmp_path / "vgt" / "song-abc123" / "transcription" / "guitar"
    namespace.mkdir(parents=True)
    (namespace / "good.mid").write_bytes(b"MThd")
    original = tmp_path / "vgt" / "song-abc123" / "transcription" / "original"
    original.mkdir()
    (original / "mix.mid").write_bytes(b"MThd")
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        _click_track_lua_mock(rpp), "function reaper.ShowConsoleMsg(message) io.stderr:write(message) end", script[:helpers_end], "local managed_tracks = {}",
        "add_stem_tracks(0, {artifact_namespace = 'song-abc123', artifacts = {}}, {targets = {guitar = {variant_order = {'missing', 'error', 'bad', 'good'}, selected_variant_id = 'good', variants = {missing = {label = 'Missing', status = 'skipped-missing-source'}, error = {label = 'Error', status = 'error'}, bad = {label = 'Bad', status = 'transcribed', midi_file = '../user.mid'}, good = {label = 'Good', status = 'transcribed', midi_file = 'transcription/guitar/good.mid'}}}, original = {variant_order = {'mix'}, selected_variant_id = 'mix', variants = {mix = {label = 'Mix', status = 'transcribed', midi_file = 'transcription/original/mix.mid'}}}}}, 0, managed_tracks)",
        "for _, track in ipairs(__tracks) do io.write(track.name, ';') end",
    ])
    result = subprocess.run([LUA, "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "[vgt] Guitar Ref — Good (MIDI);[vgt] Original Ref — Mix (MIDI);"
    assert "variant bad: sidecar MIDI file is outside the expected transcription namespace" in result.stderr


def test_live_stem_lease_is_detected_without_mutating_the_project(tmp_path: Path) -> None:
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_regions()")
    lua_program = "\n".join([
        "reaper = {}",
        script[:helpers_end],
        "local now = os.date('!*t')",
        "local stamp = string.format('%04d-%02d-%02dT%02d:%02d:%02dZ', now.year, now.month, now.day, now.hour, now.min, now.sec)",
        "io.write(tostring(stems_lease_is_live({in_progress = {heartbeat_at = stamp}})), ':', tostring(stems_lease_is_live({in_progress = {heartbeat_at = '2000-01-01T00:00:00Z'}})))",
    ])
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "true:false"


def test_apply_preserves_analysis_json_with_braces_inside_strings(tmp_path: Path) -> None:
    """The Lua sidecar reader must not mistake corrected text for JSON syntax."""
    sidecar = tmp_path / "song.vgt"
    analysis = {
        "tempo": {"value": {"label": 'Verse {A} with a \\"quote\\"'}, "human_verified": True},
        "sections": {"value": [{"name": "{intro}"}]},
    }
    sidecar.write_text(json.dumps({"schema_version": 2, "analysis": analysis}))

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function remove_previous_managed_tracks()")
    lua_program = "\n".join(
        [
            "reaper = {EnumProjects = function() return true, arg[1] end}",
            script[:helpers_end],
            "io.write(read_analysis_block() or '')",
        ]
    )
    result = subprocess.run(
        [LUA, "-", str(tmp_path / "song.RPP")],
        input=lua_program,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == analysis


def test_apply_removes_only_sidecar_recorded_region_ids(tmp_path: Path) -> None:
    """A `[vgt]` name alone never grants vgt permission to delete a region."""
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"managed_region_ids": [17]}))
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function write_settings")
    lua_program = "\n".join(
        [
            "local deleted = {}",
            "local regions = {{id = 17, name = '[vgt] generated'}, {id = 18, name = '[vgt] user-created'}, {id = 19, name = 'user'}}",
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.CountProjectMarkers() return #regions end",
            "function reaper.EnumProjectMarkers3(_, index) local region = regions[index + 1]; return true, true, 0, 1, region.name, region.id, 0 end",
            "function reaper.DeleteProjectMarker(_, id, is_region) deleted[#deleted + 1] = {id, is_region} end",
            "function reaper.GetProjExtState() return 0, '' end",
            script[:helpers_end],
            "remove_previous_managed_regions()",
            "io.write(deleted[1][1], ':', tostring(deleted[1][2]), ':', #deleted)",
        ]
    )
    result = subprocess.run(
        [LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
    )
    assert result.stdout == "17:true:1"


def test_removal_falls_back_to_the_durable_proj_ext_state_when_the_sidecar_region_list_is_stale(tmp_path: Path) -> None:
    """A `managed_region_ids` list that never reached disk (crash between
    building the section regions and write_settings, a restored Backups/
    copy, or a copied project folder) must not leave the stale block behind
    to be duplicated on re-apply: the durable ProjExtState record set at
    creation time (see record_region_ids_ext_state) is independent evidence
    of ownership, honored even when the sidecar's list is empty."""
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"managed_region_ids": []}))
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function write_settings")
    lua_program = "\n".join(
        [
            "local deleted = {}",
            "local regions = {{id = 17, name = '[vgt] Verse'}, {id = 18, name = 'Chorus'}, {id = 19, name = 'user'}}",
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.CountProjectMarkers() return #regions end",
            "function reaper.EnumProjectMarkers3(_, index) local region = regions[index + 1]; return true, true, 0, 1, region.name, region.id, 0 end",
            "function reaper.DeleteProjectMarker(_, id, is_region) deleted[#deleted + 1] = {id, is_region} end",
            "function reaper.GetProjExtState() return 0, '17,18' end",
            script[:helpers_end],
            "remove_previous_managed_regions()",
            "table.sort(deleted, function(a, b) return a[1] < b[1] end)",
            "io.write(deleted[1][1], ',', deleted[2][1], ':', #deleted)",
        ]
    )
    result = subprocess.run(
        [LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
    )
    assert result.stdout == "17,18:2"


def test_removal_tolerates_a_recorded_region_id_whose_region_no_longer_exists(tmp_path: Path) -> None:
    """Both ownership records can outlive the region they refer to (the user
    deleted it by hand, or a prior reconciliation already removed it) --
    reconciliation must silently skip an ID that matches no live region rather
    than erroring, and must still delete every other recorded ID normally."""
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"managed_region_ids": [999]}))
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function write_settings")
    lua_program = "\n".join(
        [
            "local deleted = {}",
            "local regions = {{id = 17, name = '[vgt] Verse'}, {id = 19, name = 'user'}}",
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.CountProjectMarkers() return #regions end",
            "function reaper.EnumProjectMarkers3(_, index) local region = regions[index + 1]; return true, true, 0, 1, region.name, region.id, 0 end",
            "function reaper.DeleteProjectMarker(_, id, is_region) deleted[#deleted + 1] = {id, is_region} end",
            "function reaper.GetProjExtState() return 1, '17,999' end",
            script[:helpers_end],
            "remove_previous_managed_regions()",
            "io.write(deleted[1][1], ':', tostring(deleted[1][2]), ':', #deleted)",
        ]
    )
    result = subprocess.run(
        [LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
    )
    assert result.stdout == "17:true:1"


def test_add_sections_records_each_region_id_in_proj_ext_state_immediately(tmp_path: Path) -> None:
    """Every region must be durably recorded as soon as it is created --
    before write_settings ever runs -- so a crash partway through leaves
    ProjExtState matching exactly what was actually built so far."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function write_settings")
    lua_program = "\n".join(
        [
            "local calls = {}",
            "local next_id = 100",
            "reaper = {}",
            "function reaper.AddProjectMarker2(_, _, start, finish, name) next_id = next_id + 1; return next_id end",
            "function reaper.SetProjExtState(_, section, key, value) calls[#calls + 1] = section .. ':' .. key .. '=' .. value end",
            script[:helpers_end],
            "local sections = {{label = 'A', start_seconds = 0, end_seconds = 1}, {label = 'B', start_seconds = 1, end_seconds = 2}}",
            "add_sections(sections, 0)",
            "io.write(table.concat(calls, ';'))",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "vgt:managed_region_ids=101;vgt:managed_region_ids=101,102;vgt:managed_region_ids=101,102"


def test_add_sections_clears_proj_ext_state_when_no_sections_remain(tmp_path: Path) -> None:
    """A re-apply that ends up with zero (or fewer) sections must refresh
    ProjExtState to match, not leave a prior run's now-deleted IDs behind."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function write_settings")
    lua_program = "\n".join(
        [
            "local calls = {}",
            "reaper = {}",
            "function reaper.AddProjectMarker2() error('no region should be created') end",
            "function reaper.SetProjExtState(_, section, key, value) calls[#calls + 1] = section .. ':' .. key .. '=' .. value end",
            script[:helpers_end],
            "add_sections({}, 0)",
            "io.write(table.concat(calls, ';'))",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "vgt:managed_region_ids="


def test_current_tempo_fingerprint_reflects_every_live_marker(tmp_path: Path) -> None:
    """The fingerprint must capture every marker's time/bpm/timesig, in order,
    so any live edit (add, remove, or change one) is detectable on re-apply."""
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function write_settings")
    lua_program = "\n".join(
        [
            "local markers = {{time = 0, bpm = 120, num = 4, den = 4}, {time = 10.5, bpm = 140.25, num = 3, den = 4}}",
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.CountTempoTimeSigMarkers() return #markers end",
            "function reaper.GetTempoTimeSigMarker(_, index)",
            "  local marker = markers[index + 1]",
            "  return true, marker.time, 0, 0, marker.bpm, marker.num, marker.den",
            "end",
            script[:helpers_end],
            "io.write(current_tempo_fingerprint())",
        ]
    )
    result = subprocess.run(
        [LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
    )
    assert result.stdout == "0.000000:120.000:4:4;10.500000:140.250:3:4"


def test_prior_tempo_map_fingerprint_reads_the_sidecar_config_field(tmp_path: Path) -> None:
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"config": {"tempo_map_fingerprint": "0.000000:120.000:4:4"}}))
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function write_settings")
    lua_program = "\n".join(
        [
            "reaper = {EnumProjects = function() return true, arg[1] end}",
            script[:helpers_end],
            "io.write(prior_tempo_map_fingerprint())",
        ]
    )
    result = subprocess.run(
        [LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
    )
    assert result.stdout == "0.000000:120.000:4:4"


def test_prior_tempo_map_fingerprint_is_empty_when_never_recorded(tmp_path: Path) -> None:
    """Older sidecars (pre-issue-#27) recorded tempo_map_applied but no
    fingerprint -- treated as unverifiable, never as a match."""
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"config": {"tempo_map_applied": True}}))
    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function write_settings")
    lua_program = "\n".join(
        [
            "reaper = {EnumProjects = function() return true, arg[1] end}",
            script[:helpers_end],
            "io.write('[', prior_tempo_map_fingerprint(), ']')",
        ]
    )
    result = subprocess.run(
        [LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
    )
    assert result.stdout == "[]"


def test_live_verifier_requires_a_saved_baseline_and_is_read_only() -> None:
    script = VERIFY_SCRIPT.read_text()
    assert "--baseline" in script
    assert "read_text" in script
    assert "write_text" not in script
    assert "Main_SaveProject" not in script


def test_phase1_live_verifier_checks_fallback_and_has_an_opt_in_reaper_proof() -> None:
    script = PHASE1_VERIFY_SCRIPT.read_text()
    assert "--baseline" in script
    assert "--run-live" in script
    assert "subprocess.run" in script
    assert "[vgt] Chords" in script
    assert "[vgt] Beats" in script
    assert "read_text" in script
    assert "EnumProjectMarkers3" in script
    assert "User-created region" in script
    assert "managed_region_ids" in script


def test_phase1_live_verifier_has_an_opt_in_tempo_refresh_proof() -> None:
    """Issue #27: re-apply must refresh a still-untouched vgt tempo map with
    fresh tempo data, but freeze it (and fall back to [vgt] Beats) forever
    once a human has edited it by hand."""
    script = PHASE1_VERIFY_SCRIPT.read_text()
    assert "--run-live-tempo-refresh" in script
    assert "def run_live_tempo_refresh(" in script
    assert "tempo_map_fingerprint" in script
    assert "SetTempoTimeSigMarker(0, 0, 0, -1, -1, 155, 3, 4, false)" in script


def test_stem_live_verifier_requires_saved_rpp_relative_path_proof_and_reapply() -> None:
    script = STEM_VERIFY_SCRIPT.read_text()
    assert "--run-live" in script
    assert "Main_SaveProject" in script
    assert "vgt/stem-proof/stems/" in script
    assert "Path(files[0]).is_absolute()" in script
    assert "_run_reaper(project, root)" in script
    assert "verify(project, baseline, prior_snapshot=snapshot)" in script


def test_sync_only_reads_reaper_state_and_never_mutates_the_rpp() -> None:
    script = SYNC_SCRIPT.read_text()
    # This action's sole job is REAPER-state -> sidecar bookkeeping; it must
    # never touch project/track/item/region state through the REAPER API.
    for forbidden in (
        "reaper.InsertTrackAtIndex",
        "reaper.DeleteTrack",
        "reaper.AddMediaItemToTrack",
        "reaper.SetMediaItemInfo_Value",
        "reaper.SetTempoTimeSigMarker",
        "reaper.AddProjectMarker2",
        "reaper.DeleteProjectMarker",
        "reaper.MarkProjectDirty",
    ):
        assert forbidden not in script


def test_sync_only_touches_vgt_owned_chords_track_and_regions() -> None:
    script = SYNC_SCRIPT.read_text()
    assert 'local CHORDS_NAME = PREFIX .. " Chords"' in script
    # Ownership is checked by identity -- GUID for the chords track, region ID
    # for regions -- against the sidecar's managed_track_guids /
    # managed_region_ids, not just by name, so same-named user objects are
    # never touched.
    assert "managed[reaper.GetTrackGUID(track)]" in script
    assert "read_managed_region_ids" in script
    assert "managed[region_id]" in script
    assert "reaper.EnumProjectMarkers3" in script


def test_sync_reports_success_without_a_blocking_dialog() -> None:
    script = SYNC_SCRIPT.read_text()
    # A ShowMessageBox on the success path would block headless/automated
    # runs waiting for a click that never comes (see vgt_initialize.lua,
    # which only shows one on failure); success uses ShowConsoleMsg instead.
    assert "reaper.ShowConsoleMsg" in script
    assert script.count("reaper.ShowMessageBox") == 1


def test_phase1_sync_has_an_opt_in_live_reaper_round_trip_verifier() -> None:
    script = SYNC_VERIFY_SCRIPT.read_text()
    assert "--run-live" in script
    assert "shutil.copytree" in script
    assert "vgt_sync.lua" in script
    assert "SetMediaItemInfo_Value" in script
    assert "SetProjectMarker3" in script
    assert "human_verified" in script
    assert 'stage.get("detected") != baseline_analysis[stage_name]["detected"]' in script
    assert "Main_SaveProject" in script


def _run_lua_module(script: str, rpp_path: Path, program: str) -> subprocess.CompletedProcess[str]:
    driver_start = script.index("local ok, error_message = xpcall")
    module = script[:driver_start]
    return subprocess.run(
        [LUA, "-", str(rpp_path)],
        input="\n".join([module, program]),
        text=True,
        capture_output=True,
    )


def test_sync_writes_corrected_chords_and_sections_in_one_invocation(tmp_path: Path) -> None:
    """End-to-end: fake REAPER [vgt] Chords items and [vgt]-owned regions,
    relative to a reference track's start, round-trip into sidecar segments
    and sections in a single `sync()` call -- other analysis stages and
    sidecar fields are left byte-identical, and each stage's own `detected`
    baseline is untouched (#19, extended to sections by #33)."""
    rpp = tmp_path / "song.RPP"
    sidecar = tmp_path / "song.vgt"
    chords_guid = "{AAAAAAAA-1111-2222-3333-444444444444}"
    other_guid = "{BBBBBBBB-1111-2222-3333-444444444444}"
    reference_guid = "{CCCCCCCC-1111-2222-3333-444444444444}"
    analysis = {
        "tempo": {"value": {"bpm": 120.0}, "human_verified": True, "input_hash": "h1", "settings_hash": "h2"},
        "chords": {
            "value": {"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "Am"}], "vocabulary": "maj_min", "backend": "librosa"},
            "detected": {"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "Am"}], "vocabulary": "maj_min", "backend": "librosa"},
            "human_verified": False,
            "input_hash": "old-chords-hash",
            "settings_hash": "old-chords-settings",
        },
        "sections": {
            "value": [{"label": "A", "start_seconds": 0.0, "end_seconds": 2.0}],
            "detected": [{"label": "A", "start_seconds": 0.0, "end_seconds": 2.0}],
            "human_verified": False,
            "input_hash": "old-sections-hash",
            "settings_hash": "old-sections-settings",
        },
    }
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "managed_track_guids": [chords_guid, other_guid],
                "managed_region_ids": [17, 18],
                "config": {"reference_track_guid": reference_guid},
                "analysis": analysis,
            }
        )
    )

    lua_mock = f"""
local tracks = {{
  {{guid = "{reference_guid}", name = "Reference", items = {{{{position = 10.0, length = 5.0}}}}}},
  {{guid = "{chords_guid}", name = "[vgt] Chords", items = {{
    {{position = 10.5, length = 1.0, take_name = "Am"}},
    {{position = 11.5, length = 1.5, take_name = "F"}},
  }}}},
  {{guid = "{other_guid}", name = "[vgt] Beats", items = {{}}}},
}}
local regions = {{
  {{id = 17, start = 10.5, finish = 12.0, name = "[vgt] Verse {{A}}"}},
  {{id = 18, start = 12.0, finish = 14.25, name = "Chorus"}},
  {{id = 19, start = 14.25, finish = 16.0, name = "[vgt] User region"}},
}}

reaper = {{}}
function reaper.EnumProjects(idx, buf) return true, arg[1] end
function reaper.CountTracks(proj) return #tracks end
function reaper.GetTrack(proj, index) return tracks[index + 1] end
function reaper.GetTrackName(track, buf) return true, track.name end
function reaper.GetTrackGUID(track) return track.guid end
function reaper.CountTrackMediaItems(track) return #track.items end
function reaper.GetTrackMediaItem(track, index) return track.items[index + 1] end
function reaper.GetMediaItemInfo_Value(item, key)
  if key == "D_POSITION" then return item.position end
  if key == "D_LENGTH" then return item.length end
  error("unexpected key " .. key)
end
function reaper.GetActiveTake(item) return item end
function reaper.GetTakeName(item) return item.take_name end
function reaper.CountProjectMarkers() return #regions end
function reaper.EnumProjectMarkers3(_, index)
  local region = regions[index + 1]
  return true, true, region.start, region.finish, region.name, region.id, 0
end
local messages = {{}}
function reaper.ShowConsoleMsg(msg) messages[#messages + 1] = msg end
_G.__messages = messages
"""

    result = _run_lua_module(SYNC_SCRIPT.read_text(), rpp, lua_mock + "\nsync()\nio.write(__messages[1])")
    assert result.returncode == 0, result.stderr
    assert "synced 2 chord item(s) and 2 section region(s)" in result.stdout

    data = json.loads(sidecar.read_text())

    assert data["analysis"]["chords"] == {
        "value": {
            "segments": [
                {"start_seconds": 0.5, "end_seconds": 1.5, "chord": "Am"},
                {"start_seconds": 1.5, "end_seconds": 3.0, "chord": "F"},
            ],
            "vocabulary": "maj_min",
            "backend": "librosa",
        },
        "detected": analysis["chords"]["detected"],
        "human_verified": True,
        "input_hash": "old-chords-hash",
        "settings_hash": "old-chords-settings",
        "verified_at": data["analysis"]["chords"]["verified_at"],
    }
    assert data["analysis"]["chords"]["verified_at"].endswith("Z")

    assert data["analysis"]["sections"] == {
        "value": [
            {"label": "Verse {A}", "start_seconds": 0.5, "end_seconds": 2.0},
            {"label": "Chorus", "start_seconds": 2.0, "end_seconds": 4.25},
        ],
        "detected": analysis["sections"]["detected"],
        "human_verified": True,
        "input_hash": "old-sections-hash",
        "settings_hash": "old-sections-settings",
        "verified_at": data["analysis"]["sections"]["verified_at"],
    }
    assert data["analysis"]["sections"]["verified_at"].endswith("Z")

    # Each stage's own `detected` (the original machine detection) is
    # untouched by the correction, and the untouched tempo stage round-trips
    # byte-for-byte in structure.
    assert data["analysis"]["chords"]["detected"] == analysis["chords"]["detected"]
    assert data["analysis"]["sections"]["detected"] == analysis["sections"]["detected"]
    assert data["analysis"]["tempo"] == analysis["tempo"]


def test_sync_fails_clearly_when_no_chords_track_exists(tmp_path: Path) -> None:
    rpp = tmp_path / "song.RPP"
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({"schema_version": 2, "managed_track_guids": [], "config": {"reference_track_guid": "{X}"}, "analysis": {}}))

    lua_mock = """
reaper = {}
function reaper.EnumProjects(idx, buf) return true, arg[1] end
function reaper.CountTracks(proj) return 0 end
function reaper.GetTrack(proj, index) return nil end
local messages = {}
function reaper.ShowMessageBox(msg, title, kind) messages[#messages + 1] = msg end
_G.__messages = messages
"""
    # Run the full script, including its xpcall driver, so a missing-track
    # error is reported through ShowMessageBox exactly as it would be live.
    full_program = "\n".join([lua_mock, SYNC_SCRIPT.read_text(), "io.write(__messages[1] or '')"])
    result = subprocess.run([LUA, "-", str(rpp)], input=full_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "No [vgt] Chords track found" in result.stdout


def test_write_settings_merges_a_concurrent_analyze_commit_via_generation_retry(tmp_path: Path) -> None:
    """Deterministic interleaving for analyze-versus-apply (#138): between
    write_settings' fresh read and its pre-rename generation check, simulate
    a concurrent `vgt analyze` commit landing (bumping `generation` and
    adding an analysis block write_settings never itself read). The retry
    must pick up that newer analysis rather than silently rolling it back,
    and must still carry forward its own change (managed_track_guids)."""
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({
        "schema_version": 4, "generation": 1,
        "managed_track_guids": [], "managed_region_ids": [], "config": {},
    }))

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function apply()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.GetTrackGUID(track) return track.guid end",
            "function reaper.GetTrackName(track) return true, track.name end",
            script[:helpers_end],
            "local sidecar_file = sidecar_path()",
            "local real_open = io.open",
            "local read_count = 0",
            "io.open = function(path, mode)",
            "  if path == sidecar_file and mode == 'r' then",
            "    read_count = read_count + 1",
            "    if read_count == 2 then",
            # Simulates a concurrent `vgt analyze` commit landing in the gap
            # between write_settings' fresh read and its pre-rename check.
            "      local concurrent = real_open(sidecar_file, 'w')",
            "      concurrent:write([[{\"schema_version\": 4, \"generation\": 2, \"managed_track_guids\": [], \"managed_region_ids\": [], \"config\": {}, \"analysis\": {\"tempo\": {\"value\": {\"bpm\": 140}}}}]])",
            "      concurrent:close()",
            "    end",
            "  end",
            "  return real_open(path, mode)",
            "end",
            "local track = {guid = '{TRACK-GUID}', name = 'Reference'}",
            "write_settings({track}, {}, track, false, '', '', 'electric')",
            "io.write(tostring(read_count))",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "4"  # two attempts, each doing one build-read and one verification-read

    data = json.loads(sidecar.read_text())
    # Its own change (the reference track's GUID) survives...
    assert data["managed_track_guids"] == ["{TRACK-GUID}"]
    # ...alongside the concurrently-committed analysis it never itself read.
    assert data["analysis"]["tempo"]["value"]["bpm"] == 140
    # generation: 1 (initial) -> 2 (simulated concurrent commit) -> 3 (this apply's retry).
    assert data["generation"] == 3


def test_write_settings_gives_up_after_the_retry_limit_leaving_the_prior_sidecar_intact(tmp_path: Path) -> None:
    """A conflict must fail cleanly rather than silently discard another
    writer's update: if the sidecar keeps changing out from under it, apply
    must error after a bounded number of attempts and leave the last valid
    sidecar on disk untouched."""
    sidecar = tmp_path / "song.vgt"
    original = json.dumps({"schema_version": 4, "generation": 1, "managed_track_guids": [], "managed_region_ids": [], "config": {}})
    sidecar.write_text(original)

    script = APPLY_SCRIPT.read_text()
    helpers_end = script.index("local function apply()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            "function reaper.GetTrackGUID(track) return track.guid end",
            "function reaper.GetTrackName(track) return true, track.name end",
            script[:helpers_end],
            "local sidecar_file = sidecar_path()",
            "local real_open = io.open",
            "io.open = function(path, mode)",
            "  if path == sidecar_file and mode == 'r' then",
            # Every read observes a fresh generation bump: a pathologically
            # unlucky (or malicious) concurrent writer wins every single time.
            "    local body = real_open(sidecar_file, 'r'):read('*a')",
            "    local generation = tonumber(body:match('\"generation\"%s*:%s*(%d+)')) or 1",
            "    local bumped = real_open(sidecar_file, 'w')",
            "    bumped:write((body:gsub('\"generation\"%s*:%s*%d+', '\"generation\": ' .. (generation + 1))))",
            "    bumped:close()",
            "  end",
            "  return real_open(path, mode)",
            "end",
            "local track = {guid = '{TRACK-GUID}', name = 'Reference'}",
            "local ok, err = pcall(write_settings, {track}, {}, track, false, '', '', 'electric')",
            "io.write(tostring(ok), ':', tostring(err):find('concurrently') ~= nil and 'retryable' or tostring(err))",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "false:retryable"

    # No temp file left behind, and the sidecar is exactly the last valid
    # state -- never a partial write from an abandoned attempt.
    assert not (tmp_path / "song.vgt.tmp").exists()
    assert json.loads(sidecar.read_text())["managed_track_guids"] == []


def test_write_sync_merges_a_concurrent_analyze_commit_via_generation_retry(tmp_path: Path) -> None:
    """Deterministic interleaving for analyze-versus-sync (#138): between
    write_sync's fresh read and its pre-rename generation check, simulate a
    concurrent `vgt analyze` commit landing (bumping `generation` and
    refreshing the tempo stage). The retry must merge the human correction
    onto that newer sidecar rather than clobbering the concurrent commit with
    a stale re-read of chords/sections."""
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({
        "schema_version": 5, "generation": 1,
        "managed_track_guids": [], "managed_region_ids": [], "config": {},
        "analysis": {
            "tempo": {"value": {"bpm": 120}},
            "chords": {"value": {"segments": []}, "human_verified": False},
            "sections": {"value": [], "human_verified": False},
        },
    }))

    script = SYNC_SCRIPT.read_text()
    helpers_end = script.index("local function sync()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            script[:helpers_end],
            "local sidecar_file = sidecar_path()",
            "local real_open = io.open",
            "local read_count = 0",
            "io.open = function(path, mode)",
            "  if path == sidecar_file and mode == 'r' then",
            "    read_count = read_count + 1",
            "    if read_count == 2 then",
            # Simulates a concurrent `vgt analyze` tempo refresh landing in
            # the gap between write_sync's fresh read and its pre-rename check.
            "      local concurrent = real_open(sidecar_file, 'w')",
            "      concurrent:write([[{\"schema_version\": 5, \"generation\": 2, \"managed_track_guids\": [], \"managed_region_ids\": [], \"config\": {}, \"analysis\": {\"tempo\": {\"value\": {\"bpm\": 140}}, \"chords\": {\"value\": {\"segments\": []}, \"human_verified\": false}, \"sections\": {\"value\": [], \"human_verified\": false}}}]])",
            "      concurrent:close()",
            "    end",
            "  end",
            "  return real_open(path, mode)",
            "end",
            'local segments = {{start_seconds = 0, end_seconds = 1, chord = "Am"}}',
            'local sections = {{start_seconds = 0, end_seconds = 1, label = "Verse"}}',
            "write_sync(segments, sections)",
            "io.write(tostring(read_count))",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "4"  # two attempts, each doing one build-read and one verification-read

    data = json.loads(sidecar.read_text())
    # The human correction this sync() call made survives...
    assert data["analysis"]["chords"]["value"]["segments"][0]["chord"] == "Am"
    assert data["analysis"]["chords"]["human_verified"] is True
    assert data["analysis"]["sections"]["value"][0]["label"] == "Verse"
    # ...alongside the concurrently-committed tempo refresh it never itself wrote.
    assert data["analysis"]["tempo"]["value"]["bpm"] == 140
    # generation: 1 (initial) -> 2 (simulated concurrent commit) -> 3 (this sync's retry).
    assert data["generation"] == 3


def test_write_sync_interrupted_write_leaves_the_previous_valid_sidecar_intact(tmp_path: Path) -> None:
    """vgt_sync.lua used to write the sidecar directly with no temp file, so
    a crash mid-write could truncate it. It must now use the same
    temp-file-plus-atomic-replace discipline as vgt_initialize.lua: if the
    process is interrupted right as the OS rename would happen, the
    previously valid sidecar must survive untouched, and no partial temp
    file may be left behind masquerading as the sidecar."""
    sidecar = tmp_path / "song.vgt"
    original = json.dumps({
        "schema_version": 5, "generation": 1,
        "managed_track_guids": [], "managed_region_ids": [], "config": {},
        "analysis": {
            "tempo": {"value": {"bpm": 120}},
            "chords": {"value": {"segments": []}, "human_verified": False},
            "sections": {"value": [], "human_verified": False},
        },
    })
    sidecar.write_text(original)

    script = SYNC_SCRIPT.read_text()
    helpers_end = script.index("local function sync()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            script[:helpers_end],
            # Simulate a crash exactly at the atomic-replace step: the temp
            # file is fully written, but the rename never completes.
            "local real_rename = os.rename",
            "os.rename = function(...) return nil, 'simulated crash before replace' end",
            'local segments = {{start_seconds = 0, end_seconds = 1, chord = "Am"}}',
            'local sections = {{start_seconds = 0, end_seconds = 1, label = "Verse"}}',
            "local ok, err = pcall(write_sync, segments, sections)",
            "io.write(tostring(ok), ':', tostring(err):find('simulated crash') ~= nil and 'crashed' or tostring(err))",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "false:crashed"

    # The prior valid sidecar is exactly as it was -- never truncated or
    # partially overwritten -- and no leftover temp file remains.
    assert sidecar.read_text() == original
    assert not (tmp_path / "song.vgt.tmp").exists()


def test_write_sync_gives_up_after_the_retry_limit_leaving_the_prior_sidecar_intact(tmp_path: Path) -> None:
    """Symmetric with write_settings' retry-exhaustion case: a sidecar that
    keeps changing out from under vgt_sync.lua must fail cleanly rather than
    silently discard another writer's update, leaving the last valid sidecar
    on disk untouched."""
    sidecar = tmp_path / "song.vgt"
    original = json.dumps({
        "schema_version": 5, "generation": 1,
        "managed_track_guids": [], "managed_region_ids": [], "config": {},
        "analysis": {
            "tempo": {"value": {"bpm": 120}},
            "chords": {"value": {"segments": []}, "human_verified": False},
            "sections": {"value": [], "human_verified": False},
        },
    })
    sidecar.write_text(original)

    script = SYNC_SCRIPT.read_text()
    helpers_end = script.index("local function sync()")
    lua_program = "\n".join(
        [
            "reaper = {}",
            "function reaper.EnumProjects() return true, arg[1] end",
            script[:helpers_end],
            "local sidecar_file = sidecar_path()",
            "local real_open = io.open",
            "io.open = function(path, mode)",
            "  if path == sidecar_file and mode == 'r' then",
            # Every read observes a fresh generation bump: a pathologically
            # unlucky concurrent writer wins every single time.
            "    local body = real_open(sidecar_file, 'r'):read('*a')",
            "    local generation = tonumber(body:match('\"generation\"%s*:%s*(%d+)')) or 1",
            "    local bumped = real_open(sidecar_file, 'w')",
            "    bumped:write((body:gsub('\"generation\"%s*:%s*%d+', '\"generation\": ' .. (generation + 1))))",
            "    bumped:close()",
            "  end",
            "  return real_open(path, mode)",
            "end",
            'local segments = {{start_seconds = 0, end_seconds = 1, chord = "Am"}}',
            'local sections = {{start_seconds = 0, end_seconds = 1, label = "Verse"}}',
            "local ok, err = pcall(write_sync, segments, sections)",
            "io.write(tostring(ok), ':', tostring(err):find('concurrently') ~= nil and 'retryable' or tostring(err))",
        ]
    )
    result = subprocess.run([LUA, "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "false:retryable"

    assert not (tmp_path / "song.vgt.tmp").exists()
    assert json.loads(sidecar.read_text())["analysis"]["chords"]["human_verified"] is False
