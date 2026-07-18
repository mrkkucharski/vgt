from pathlib import Path


def test_apply_uses_reaper_api_and_never_edits_rpp_text() -> None:
    script = (Path(__file__).parents[1] / "reascript" / "vgt_phase0_apply.lua").read_text()
    assert "reaper.InsertTrackAtIndex" in script
    assert "reaper.DeleteTrack" in script
    assert "reaper.AddMediaItemToTrack" in script
    assert "managed[reaper.GetTrackGUID(track)] and starts_with_vgt(track)" in script
    assert "GetSetProjectInfo_String" not in script
