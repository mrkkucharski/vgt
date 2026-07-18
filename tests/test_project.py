from pathlib import Path
import json

import pytest

from vgt.cli import main
from vgt.project import ProjectError, locate_project, read_project


FIXTURE = Path(__file__).parents[1] / "test" / "Reaper Project" / "Reaper Project.RPP"


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
