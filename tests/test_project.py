from pathlib import Path
import json

import pytest

from vgt.cli import main
from vgt.project import ProjectError, locate_project, read_project, track_source_path


FIXTURE = Path(__file__).parents[1] / "test" / "Reaper Project" / "Reaper Project.RPP"
GUID = "{A11CE000-0000-0000-0000-000000000001}"


def test_reads_the_real_fixture() -> None:
    project = read_project(FIXTURE)

    assert project.sample_rate == 44100
    assert (project.tempo, project.time_signature_numerator, project.time_signature_denominator) == (120.0, 4, 4)
    assert [track.name for track in project.tracks] == [
        "Click",
        "The Seven Rivers (Full March - 3_00)",
        "Paris Metro Punk",
    ]
    assert len({track.guid for track in project.tracks}) == 3


def test_locates_the_only_project_in_working_directory() -> None:
    assert locate_project(None, FIXTURE.parent) == FIXTURE.resolve()


def test_requires_explicit_path_when_multiple_projects(tmp_path: Path) -> None:
    (tmp_path / "one.RPP").touch()
    (tmp_path / "two.rpp").touch()
    with pytest.raises(ProjectError, match="More than one"):
        locate_project(None, tmp_path)


def test_bare_cli_invocation_inspects_an_explicit_project(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(FIXTURE)]) == 0
    assert json.loads(capsys.readouterr().out)["sample_rate"] == 44100


def test_apply_command_points_to_the_apply_reascript(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["apply", str(FIXTURE)]) == 2
    assert "reascript/vgt_initialize.lua" in capsys.readouterr().err


def test_sync_command_points_to_the_sync_reascript(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sync", str(FIXTURE)]) == 2
    err = capsys.readouterr().err
    assert "reascript/vgt_sync.lua" in err
    assert "human-verified" in err


def _write_rpp(tmp_path: Path, *, items: list[str]) -> Path:
    """A minimal synthetic RPP with one track carrying the given ITEM chunks."""
    item_chunks = "\n".join(items)
    project = tmp_path / "song.RPP"
    project.write_text(
        f"""<REAPER_PROJECT 0.1 "6.0" 0
  SAMPLERATE 44100
  TEMPO 120 4 4
  <TRACK {GUID}
    NAME "Reference"
{item_chunks}
  >
>
""",
        encoding="utf-8",
    )
    return project


def _item_chunk(filename: str) -> str:
    return f"""    <ITEM
      <SOURCE WAVE
        FILE "{filename}"
      >
    >"""


def test_track_source_path_resolves_the_one_file_backed_item(tmp_path: Path) -> None:
    project = _write_rpp(tmp_path, items=[_item_chunk("audio.wav")])
    assert track_source_path(project, GUID) == (project.parent / "audio.wav").resolve()


def test_track_source_path_rejects_a_track_with_no_file_backed_item(tmp_path: Path) -> None:
    project = _write_rpp(tmp_path, items=["    <ITEM\n    >"])
    with pytest.raises(ProjectError, match="no file-backed media source"):
        track_source_path(project, GUID)


def test_track_source_path_rejects_an_ambiguous_multi_item_track(tmp_path: Path) -> None:
    """A track with more than one file-backed item is rejected rather than
    silently resolving to whichever FILE happens to appear first (issue #136);
    vgt_initialize.lua's file_backed_item_count enforces the same rule before
    such a track is ever persisted as the reference."""
    project = _write_rpp(tmp_path, items=[_item_chunk("a.wav"), _item_chunk("b.wav")])
    with pytest.raises(ProjectError, match="ambiguous"):
        track_source_path(project, GUID)
