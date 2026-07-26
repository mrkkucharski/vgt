"""Regression checks for the repository's offline Tests workflow contract."""

from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_tests_workflow_runs_automatically_and_keeps_manual_dispatch() -> None:
    workflow = _workflow_text()
    triggers = workflow.split("on:\n", maxsplit=1)[1].split("\npermissions:", maxsplit=1)[0]

    for event in ("push", "pull_request", "workflow_dispatch"):
        assert re.search(rf"^  {event}:\s*$", triggers, flags=re.MULTILINE), event


def test_linux_workflow_job_covers_the_complete_goal_contract_suite() -> None:
    workflow = _workflow_text()
    linux_job = workflow.split("  test:\n", maxsplit=1)[1].split(
        "  installed-distribution-smoke:", maxsplit=1
    )[0]

    assert (ROOT / "tests" / "test_goal_contract.py").is_file()
    assert "uv run pytest -q tests" in linux_job
    assert "--ignore=tests/test_goal_contract.py" not in linux_job


def test_tests_workflow_bounds_superseded_runs_and_each_job() -> None:
    workflow = _workflow_text()

    assert "cancel-in-progress: true" in workflow
    assert workflow.count("timeout-minutes:") >= 2
