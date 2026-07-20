from pathlib import Path
import json
import subprocess


VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase0_apply.py"
PHASE1_VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase1_apply.py"
STEM_VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_stem_apply.py"
APPLY_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_initialize.lua"
SYNC_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_sync.lua"


def test_apply_uses_reaper_api_and_never_edits_rpp_text() -> None:
    script = APPLY_SCRIPT.read_text()
    assert "reaper.InsertTrackAtIndex" in script
    assert "reaper.DeleteTrack" in script
    assert "reaper.AddMediaItemToTrack" in script
    assert 'local filename = reaper.GetMediaSourceFileName(source_media, "")' in script
    assert "local ok, filename = reaper.GetMediaSourceFileName" not in script
    assert "managed[reaper.GetTrackGUID(track)] and starts_with_vgt(track)" in script
    assert "GetSetProjectInfo_String" not in script


def test_apply_asks_for_a_reference_track_and_names_the_folder_after_it() -> None:
    script = APPLY_SCRIPT.read_text()
    # Interactive pick with a headless override, then the folder is named after the choice.
    assert "gfx.showmenu" in script
    assert 'reaper.GetExtState("vgt", "reference_index")' in script
    assert 'PREFIX .. " " .. track_name(reference)' in script
    # Only the chosen reference is mirrored, not every track.
    assert "copy_file_backed_items(reference, mirror)" in script


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


def test_beats_and_chords_tracks_are_both_unmuted() -> None:
    script = APPLY_SCRIPT.read_text()
    # Both are item-label-only tracks with no audio, so muting them would
    # only make their labels unreadable in REAPER (issue #41 regression).
    assert 'reaper.SetMediaTrackInfo_Value(track, "B_MUTE", muted and 1 or 0)' in script
    assert "local function offer_beats_track(index, tempo, reference_start, reference_end, managed_tracks)" in script
    assert "local beats = add_locked_track(index, BEATS_NAME, false)" in script
    assert "offer_beats_track(insert_at + 2, tempo, reference_start, reference_end, managed_tracks)" in script
    assert "add_locked_track(reaper.CountTracks(0), CHORDS_NAME, false)" in script


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
            "function reaper.InsertTrackAtIndex(index, defaults) tracks[index + 1] = {index = index, name = nil, mute = 0} end",
            "function reaper.GetTrack(proj, index) return tracks[index + 1] end",
            "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) if key == 'P_NAME' and set then track.name = value end end",
            "function reaper.SetMediaTrackInfo_Value(track, key, value) if key == 'B_MUTE' then track.mute = value end end",
            "function reaper.PCM_Source_CreateFromFile(path) return {path = path} end",
            "function reaper.GetMediaSourceLength(source) return 2.5, false end",
            "local items = {}",
            "function reaper.AddMediaItemToTrack(track) local item = {track = track, values = {}}; items[#items + 1] = item; return item end",
            "function reaper.SetMediaItemInfo_Value(item, key, value) item.values[key] = value end",
            "function reaper.AddTakeToMediaItem(item) local take = {}; item.take = take; return take end",
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
    result = subprocess.run(["lua", "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
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
    result = subprocess.run(["lua", "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
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
    result = subprocess.run(["lua", "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
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
    result = subprocess.run(["lua", "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
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
    result = subprocess.run(["lua", "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert "outside the expected stem namespace" not in result.stderr
    assert result.stdout == "1:[vgt] Vocals"


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
    result = subprocess.run(["lua", "-", str(rpp)], input=lua_program, text=True, capture_output=True, check=True)
    assert result.stdout == "0:0:0"
    assert "skipping stem vocals: sidecar file is outside the expected stem namespace" in result.stderr


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
    result = subprocess.run(["lua", "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True)
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
        ["lua", "-", str(tmp_path / "song.RPP")],
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
            script[:helpers_end],
            "remove_previous_managed_regions()",
            "io.write(deleted[1][1], ':', tostring(deleted[1][2]), ':', #deleted)",
        ]
    )
    result = subprocess.run(
        ["lua", "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
    )
    assert result.stdout == "17:true:1"


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
        ["lua", "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
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
        ["lua", "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
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
        ["lua", "-", str(tmp_path / "song.RPP")], input=lua_program, text=True, capture_output=True, check=True
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


def _run_lua_module(script: str, rpp_path: Path, program: str) -> subprocess.CompletedProcess[str]:
    driver_start = script.index("local ok, error_message = xpcall")
    module = script[:driver_start]
    return subprocess.run(
        ["lua", "-", str(rpp_path)],
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
  {{guid = "{other_guid}", name = "[vgt] Mirror", items = {{}}}},
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
    result = subprocess.run(["lua", "-", str(rpp)], input=full_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "No [vgt] Chords track found" in result.stdout
