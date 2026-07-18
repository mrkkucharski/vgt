from pathlib import Path
import json
import subprocess


VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase0_apply.py"
PHASE1_VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase1_apply.py"
APPLY_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_initialize.lua"


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


def test_phase1_live_verifier_is_read_only_and_checks_fallback() -> None:
    script = PHASE1_VERIFY_SCRIPT.read_text()
    assert "--baseline" in script
    assert "[vgt] Chords" in script
    assert "[vgt] Beats" in script
    assert "read_text" in script
    assert "write_text" not in script
