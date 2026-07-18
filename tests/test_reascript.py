from pathlib import Path


VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_phase0_apply.py"


def test_apply_uses_reaper_api_and_never_edits_rpp_text() -> None:
    script = (Path(__file__).parents[1] / "reascript" / "vgt_phase0_apply.lua").read_text()
    assert "reaper.InsertTrackAtIndex" in script
    assert "reaper.DeleteTrack" in script
    assert "reaper.AddMediaItemToTrack" in script
    assert 'local filename = reaper.GetMediaSourceFileName(source_media, "")' in script
    assert "local ok, filename = reaper.GetMediaSourceFileName" not in script
    assert "managed[reaper.GetTrackGUID(track)] and starts_with_vgt(track)" in script
    assert "GetSetProjectInfo_String" not in script


def test_live_verifier_requires_a_saved_baseline_and_is_read_only() -> None:
    script = VERIFY_SCRIPT.read_text()
    assert "--baseline" in script
    assert "read_text" in script
    assert "write_text" not in script
    assert "Main_SaveProject" not in script
