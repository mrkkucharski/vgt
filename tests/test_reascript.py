from pathlib import Path
import json
import subprocess


VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase0_apply.py"
PHASE1_VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase1_apply.py"
APPLY_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_initialize.lua"
READ_CHORDS_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_read_chords.lua"


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
    assert '"B_MUTE", 1' in script
    assert 'SetMediaItemInfo_Value(item, "C_LOCK", 1)' in script
    assert 'local CHORDS_NAME = PREFIX .. " Chords"' in script
    assert 'local BEATS_NAME = PREFIX .. " Beats"' in script
    assert "DeleteProjectMarkerByIndex" in script


def test_chord_items_are_added_unlocked_so_they_stay_editable() -> None:
    script = APPLY_SCRIPT.read_text()
    # Chord items are the editing surface (issue #17): unlike beats (locked,
    # default arg), the chords loop explicitly passes locked = false.
    assert 'add_labeled_item(chords_track, reference_start + (tonumber(chord.start_seconds) or 0), reference_start + (tonumber(chord.end_seconds) or 0), tostring(chord.chord or chord.label or "N"), false)' in script
    assert "if locked ~= false then reaper.SetMediaItemInfo_Value(item, \"C_LOCK\", 1) end" in script


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


def test_read_chords_only_reads_reaper_state_and_never_mutates_the_rpp() -> None:
    script = READ_CHORDS_SCRIPT.read_text()
    # This action's sole job is REAPER-state -> sidecar bookkeeping; it must
    # never touch project/track/item state through the REAPER API.
    for forbidden in (
        "reaper.InsertTrackAtIndex",
        "reaper.DeleteTrack",
        "reaper.AddMediaItemToTrack",
        "reaper.SetMediaItemInfo_Value",
        "reaper.SetTempoTimeSigMarker",
        "reaper.AddProjectMarker2",
        "reaper.MarkProjectDirty",
    ):
        assert forbidden not in script


def test_read_chords_only_touches_the_vgt_owned_chords_track() -> None:
    script = READ_CHORDS_SCRIPT.read_text()
    assert 'local CHORDS_NAME = PREFIX .. " Chords"' in script
    # Ownership is checked by GUID against the sidecar's managed_track_guids,
    # not just by name, so a same-named user track is never touched.
    assert "managed[reaper.GetTrackGUID(track)]" in script


def test_read_chords_reports_success_without_a_blocking_dialog() -> None:
    script = READ_CHORDS_SCRIPT.read_text()
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


def test_read_chords_writes_corrected_segments_as_human_verified(tmp_path: Path) -> None:
    """End-to-end: fake REAPER items on [vgt] Chords, relative to a reference
    track's start, round-trip into sidecar segments -- other analysis stages
    and sidecar fields are left byte-identical."""
    rpp = tmp_path / "song.RPP"
    sidecar = tmp_path / "song.vgt"
    chords_guid = "{AAAAAAAA-1111-2222-3333-444444444444}"
    other_guid = "{BBBBBBBB-1111-2222-3333-444444444444}"
    reference_guid = "{CCCCCCCC-1111-2222-3333-444444444444}"
    analysis = {
        "tempo": {"value": {"bpm": 120.0}, "human_verified": True, "input_hash": "h1", "settings_hash": "h2"},
        "chords": {
            "value": {"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "chord": "Am"}], "vocabulary": "maj_min", "backend": "librosa"},
            "human_verified": False,
            "input_hash": "old-hash",
            "settings_hash": "old-settings",
        },
    }
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "managed_track_guids": [chords_guid, other_guid],
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
local messages = {{}}
function reaper.ShowConsoleMsg(msg) messages[#messages + 1] = msg end
_G.__messages = messages
"""

    result = _run_lua_module(READ_CHORDS_SCRIPT.read_text(), rpp, lua_mock + "\nread_chords()\nio.write(__messages[1])")
    assert result.returncode == 0, result.stderr
    assert "read 2 chord item(s)" in result.stdout

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
        "human_verified": True,
        "input_hash": "old-hash",
        "settings_hash": "old-settings",
        "verified_at": data["analysis"]["chords"]["verified_at"],
    }
    assert data["analysis"]["chords"]["verified_at"].endswith("Z")
    # The tempo stage (untouched) round-trips byte-for-byte in structure.
    assert data["analysis"]["tempo"] == analysis["tempo"]


def test_read_chords_fails_clearly_when_no_chords_track_exists(tmp_path: Path) -> None:
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
    full_program = "\n".join([lua_mock, READ_CHORDS_SCRIPT.read_text(), "io.write(__messages[1] or '')"])
    result = subprocess.run(["lua", "-", str(rpp)], input=full_program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "No [vgt] Chords track found" in result.stdout
