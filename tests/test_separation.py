from pathlib import Path
from typing import Any, Callable
import json
import shutil
import wave

import pytest

from vgt.sidecar import read_sidecar, update_analysis, write_sidecar
from vgt.separation import (
    ARTIFACT_FILENAMES,
    OPERATION_ORDER,
    FakeSeparator,
    FakeSeparatorError,
    SeparationError,
    SplitResult,
    artifact_path,
    build_recipe,
    hash_audio_content,
    separate,
    spec_hash,
    stems_dir,
)


FIXTURE_DIR = Path(__file__).parents[1] / "test" / "Reaper Project"
FIXTURE = FIXTURE_DIR / "Reaper Project.RPP"
REFERENCE_GUID = "{75418143-1F31-B548-B7D2-96815CB0297D}"  # "The Seven Rivers ..." track


def _project_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "Reaper Project"
    shutil.copytree(FIXTURE_DIR, destination)
    return destination / "Reaper Project.RPP"


def _write_v1_sidecar(project: Path) -> Path:
    sidecar = project.with_suffix(".vgt")
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "managed_track_guids": ["{AAAA}", "{BBBB}"],
                "config": {
                    "reference_track_name": "The Seven Rivers (Full March - 3_00)",
                    "reference_track_guid": REFERENCE_GUID,
                    "folder_name": "[vgt] The Seven Rivers (Full March - 3_00)",
                    "mirror_name": "[vgt] Mirror",
                },
            }
        )
    )
    return sidecar


class _RecordingSeparator:
    """Wraps another `Separator`, counting calls and optionally raising
    `RuntimeError` (after checkpointing a task id, like a real backend that
    has already durably submitted work) the first time a given stem is
    requested -- lets tests exercise "crashed after checkpoint" resume
    without depending on `FakeSeparator`'s own single global `fail_at`."""

    name = "fake"
    api_version = "fake-v1"

    def __init__(self, inner: FakeSeparator, fail_stems_once: set[str] | None = None) -> None:
        self.inner = inner
        self.fail_stems_once = set(fail_stems_once or set())
        self.calls: list[str] = []

    def split(self, source: Path, out_dir: Path, spec, *, resume_state, checkpoint) -> SplitResult:
        self.calls.append(spec.stem)
        if spec.stem in self.fail_stems_once:
            self.fail_stems_once.discard(spec.stem)
            idempotency_key = (resume_state or {}).get("idempotency_key") or "fixed-idempotency-key"
            task_id = (resume_state or {}).get("task_id") or f"task-{spec.stem}"
            checkpoint({"idempotency_key": idempotency_key, "task_id": task_id, "status": "submitted"})
            raise RuntimeError("simulated crash after checkpoint")
        return self.inner.split(source, out_dir, spec, resume_state=resume_state, checkpoint=checkpoint)


class _SourceRecordingSeparator:
    """Small network-free model of LALAL's upload reuse behavior."""

    name = "lalal"
    api_version = "v1"

    def __init__(self) -> None:
        self.resume_states: list[dict[str, Any]] = []
        self._inner = FakeSeparator()

    def split(self, source: Path, out_dir: Path, spec, *, resume_state, checkpoint) -> SplitResult:
        state = dict(resume_state or {})
        self.resume_states.append(state)
        if not state.get("source_id"):
            checkpoint({"source_id": f"upload-{len(self.resume_states)}", "source_expires": 4102444800})
        return self._inner.split(source, out_dir, spec, resume_state=resume_state, checkpoint=checkpoint)


class _PreflightRecordingSeparator(_SourceRecordingSeparator):
    """Records the run-level credit gate without making network calls."""

    def __init__(self) -> None:
        super().__init__()
        self.preflight_sources: list[tuple[Path, int]] = []

    def preflight(
        self,
        *,
        sources: list[tuple[Path, int]],
        source_states: list[dict[str, Any]],
        checkpoint: Callable[[int, dict[str, Any]], None],
    ) -> list[dict[str, Any]]:
        self.preflight_sources = sources
        states = [
            {"source_id": f"preflight-upload-{index}", "source_expires": 4102444800, "source_duration_seconds": 42}
            for index, _entry in enumerate(sources, start=1)
        ]
        for index, state in enumerate(states):
            checkpoint(index, state)
        return states


def test_build_recipe_fixed_dag_shape() -> None:
    recipe = build_recipe("electric")
    assert set(recipe) == set(OPERATION_ORDER)
    assert recipe["vocals-original"].spec.keep == ("stem", "back")
    assert recipe["vocals-original"].artifact_names == {"stem": "vocals", "back": "instrumental"}
    assert recipe["guitar-instrumental"].depends_on == "vocals-original"
    assert recipe["guitar-instrumental"].spec.source_role == "instrumental"
    for operation_id in ("bass-original", "drums-original", "guitar-original"):
        assert recipe[operation_id].depends_on is None
        assert recipe[operation_id].spec.source_role == "original"


def test_guitar_type_only_changes_the_two_guitar_specs() -> None:
    electric = build_recipe("electric")
    acoustic = build_recipe("acoustic")
    assert electric["guitar-original"].spec.stem == "electric_guitar"
    assert acoustic["guitar-original"].spec.stem == "acoustic_guitar"
    for operation_id in ("vocals-original", "bass-original", "drums-original"):
        assert spec_hash(electric[operation_id].spec) == spec_hash(acoustic[operation_id].spec)
    assert spec_hash(electric["guitar-original"].spec) != spec_hash(acoustic["guitar-original"].spec)
    assert spec_hash(electric["guitar-instrumental"].spec) != spec_hash(acoustic["guitar-instrumental"].spec)


def test_invalid_guitar_type_rejected() -> None:
    with pytest.raises(SeparationError):
        build_recipe("banjo")


def test_full_run_completes_all_five_operations_and_six_artifacts(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    result = separate(project, FakeSeparator(), guitar_type="electric")

    stems = result["analysis"]["stems"]
    assert stems["backend"] == "fake"
    assert stems["guitar_type"] == "electric"
    namespace = stems["artifact_namespace"]
    assert namespace

    for operation_id in OPERATION_ORDER:
        operation = stems["operations"][operation_id]
        assert operation["status"] == "completed", operation
        assert operation["error"] is None
        assert operation["completed_at"]

    assert set(stems["artifacts"]) == set(ARTIFACT_FILENAMES)
    for artifact_name, filename in ARTIFACT_FILENAMES.items():
        artifact = stems["artifacts"][artifact_name]
        path = artifact_path(project, artifact)
        assert path.name == filename
        assert path.is_file()
        assert artifact["size_bytes"] == path.stat().st_size
        assert artifact["sha256"] == hash_audio_content(path)
        assert artifact["duration_seconds"] > 0
        with wave.open(str(path), "rb") as handle:
            assert handle.getnframes() > 0

    # vocals-original is billed once but retains both artifacts it produced.
    assert set(stems["operations"]["vocals-original"]["outputs"]) == {"vocals", "instrumental"}
    assert stems["artifacts"]["vocals"]["operation"] == "vocals-original"
    assert stems["artifacts"]["instrumental"]["operation"] == "vocals-original"
    assert stems["artifacts"]["backing"]["operation"] == "guitar-instrumental"

    work_dir = stems_dir(project, namespace) / "_work"
    for operation_id in OPERATION_ORDER:
        assert not (work_dir / operation_id).exists()  # per-operation scratch dirs are cleaned up after commit


def test_live_project_lease_refuses_a_second_separator_before_submission(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    from vgt.sidecar import acquire_stems_lease, release_stems_lease

    _claimed, owner = acquire_stems_lease(project)
    backend = _RecordingSeparator(FakeSeparator())
    try:
        with pytest.raises(SeparationError, match="already in progress"):
            separate(project, backend, guitar_type="electric")
    finally:
        release_stems_lease(project, owner)
    assert backend.calls == []


def test_stale_project_lease_is_replaced_and_does_not_block_recovery(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    sidecar = read_sidecar(project)
    sidecar["analysis"]["stems"]["in_progress"] = {
        "owner_id": "crashed-process",
        "started_at": "2000-01-01T00:00:00Z",
        "heartbeat_at": "2000-01-01T00:00:00Z",
    }
    write_sidecar(project, sidecar)

    result = separate(project, FakeSeparator(), guitar_type="electric")

    assert result["analysis"]["stems"]["in_progress"] is None
    assert all(result["analysis"]["stems"]["operations"][op]["status"] == "completed" for op in OPERATION_ORDER)


def test_analysis_update_during_split_preserves_paid_checkpoint(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    class ConcurrentAnalysisSeparator:
        name = "fake"
        api_version = "fake-v1"

        def __init__(self) -> None:
            self.inner = FakeSeparator()
            self.did_update = False

        def split(self, source: Path, out_dir: Path, spec, *, resume_state, checkpoint) -> SplitResult:
            if not self.did_update:
                self.did_update = True
                # Model a local detector finishing while LALAL has an active
                # paid task.  It changes only its own analysis stage.
                checkpoint({"task_id": f"paid-{spec.stem}", "idempotency_key": f"key-{spec.stem}"})
                update_analysis(project, lambda analysis: analysis["key"].update({"value": {"root": "A"}}))
                checkpointed = read_sidecar(project)["analysis"]["stems"]["operations"]["vocals-original"]
                assert checkpointed["backend_state"]["task_id"] == "paid-vocals"
            result = self.inner.split(source, out_dir, spec, resume_state=resume_state, checkpoint=checkpoint)
            return result

    result = separate(project, ConcurrentAnalysisSeparator(), guitar_type="electric")

    assert result["analysis"]["key"]["value"] == {"root": "A"}
    assert result["analysis"]["stems"]["operations"]["vocals-original"]["status"] == "completed"


def test_operations_reuse_uploads_only_for_identical_source_bytes(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    backend = _SourceRecordingSeparator()

    separate(project, backend, guitar_type="electric")

    # vocals creates the original upload; the other three original-source
    # operations reuse it. The cascade consumes different (instrumental)
    # bytes, so it gets the recipe's one other upload.
    assert [state.get("source_id") for state in backend.resume_states] == [
        None,
        "upload-1",
        "upload-1",
        "upload-1",
        None,
    ]


def test_preflight_reserves_all_five_operations_and_reuses_its_original_upload(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    backend = _PreflightRecordingSeparator()

    separate(project, backend, guitar_type="electric")

    # Before the first split, the full recipe is priced as five source
    # durations; the cascade is reserved against the original's duration
    # until its instrumental source exists. The preflight upload is then the
    # shared original upload, not a throwaway third upload.
    assert [count for _path, count in backend.preflight_sources] == [5]
    assert [state.get("source_id") for state in backend.resume_states[:4]] == [
        "preflight-upload-1",
        "preflight-upload-1",
        "preflight-upload-1",
        "preflight-upload-1",
    ]


def test_cost_consent_hook_runs_after_preflight_and_before_any_split(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    backend = _PreflightRecordingSeparator()
    disclosed: list[int] = []

    def decline(operation_count: int) -> None:
        disclosed.append(operation_count)
        raise SeparationError("paid stem refresh cancelled")

    with pytest.raises(SeparationError, match="cancelled"):
        separate(project, backend, guitar_type="electric", before_submit=decline)

    # Upload/preflight is free and establishes the exact quoted duration; no
    # paid split may start until the callback returns.
    assert disclosed == [5]
    assert backend.preflight_sources
    assert backend.resume_states == []


def test_second_run_is_fully_cached_no_backend_calls(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    separate(project, FakeSeparator(), guitar_type="electric")

    counting = _RecordingSeparator(FakeSeparator())
    result = separate(project, counting, guitar_type="electric")

    assert counting.calls == []
    for operation_id in OPERATION_ORDER:
        assert result["analysis"]["stems"]["operations"][operation_id]["status"] == "completed"


def test_force_stems_recomputes_every_operation(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    separate(project, FakeSeparator(), guitar_type="electric")

    counting = _RecordingSeparator(FakeSeparator())
    separate(project, counting, guitar_type="electric", force=True)

    assert sorted(counting.calls) == sorted(build_recipe("electric")[op].spec.stem for op in OPERATION_ORDER)


def test_changing_guitar_type_invalidates_only_the_two_guitar_operations(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    first = separate(project, FakeSeparator(), guitar_type="electric")
    unrelated_completed_at = {
        operation_id: first["analysis"]["stems"]["operations"][operation_id]["completed_at"]
        for operation_id in ("vocals-original", "bass-original", "drums-original")
    }
    unrelated_shas = {
        name: first["analysis"]["stems"]["artifacts"][name]["sha256"] for name in ("vocals", "instrumental", "bass", "drums")
    }

    counting = _RecordingSeparator(FakeSeparator())
    second = separate(project, counting, guitar_type="acoustic")

    assert counting.calls == ["acoustic_guitar", "acoustic_guitar"]
    for operation_id, completed_at in unrelated_completed_at.items():
        assert second["analysis"]["stems"]["operations"][operation_id]["completed_at"] == completed_at
    for name, sha in unrelated_shas.items():
        assert second["analysis"]["stems"]["artifacts"][name]["sha256"] == sha
    assert second["analysis"]["stems"]["operations"]["guitar-original"]["requested_presets"]["stem"] == "acoustic_guitar"


def test_changing_source_bytes_invalidates_every_operation_including_the_cascade(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    first = separate(project, FakeSeparator(), guitar_type="electric")
    original_backing_sha = first["analysis"]["stems"]["artifacts"]["backing"]["sha256"]

    from vgt.project import track_source_path

    source = track_source_path(project, REFERENCE_GUID)
    audio = bytearray(source.read_bytes())
    audio[0] ^= 0xFF  # content identity is a raw byte hash; separate() never parses the source file
    source.write_bytes(bytes(audio))

    counting = _RecordingSeparator(FakeSeparator())
    second = separate(project, counting, guitar_type="electric")

    # Every original-source operation reruns, and the cascade reruns too
    # (it depends on vocals-original's freshly recomputed instrumental).
    assert sorted(counting.calls) == sorted(["vocals", "bass", "drum", "electric_guitar", "electric_guitar"])
    assert second["analysis"]["stems"]["artifacts"]["backing"]["sha256"] != original_backing_sha


def test_moving_the_project_folder_keeps_the_cache_current(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    separate(project, FakeSeparator(), guitar_type="electric")

    moved_root = tmp_path / "moved"
    shutil.move(str(project.parent), str(moved_root))
    moved_project = moved_root / project.name

    counting = _RecordingSeparator(FakeSeparator())
    result = separate(moved_project, counting, guitar_type="electric")

    assert counting.calls == []
    for operation_id in OPERATION_ORDER:
        assert result["analysis"]["stems"]["operations"][operation_id]["status"] == "completed"


def test_missing_artifact_on_disk_forces_a_recompute_not_a_stale_cache_hit(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    first = separate(project, FakeSeparator(), guitar_type="electric")
    bass_path = artifact_path(project, first["analysis"]["stems"]["artifacts"]["bass"])
    bass_path.unlink()

    counting = _RecordingSeparator(FakeSeparator())
    result = separate(project, counting, guitar_type="electric")

    assert counting.calls == ["bass"]
    assert artifact_path(project, result["analysis"]["stems"]["artifacts"]["bass"]).is_file()


def test_a_failed_operation_does_not_block_independent_operations(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    backend = _RecordingSeparator(FakeSeparator(), fail_stems_once={"vocals"})

    with pytest.raises(SeparationError):
        separate(project, backend, guitar_type="electric")

    sidecar = read_sidecar(project)
    stems = sidecar["analysis"]["stems"]
    assert stems["operations"]["vocals-original"]["status"] == "error"
    for operation_id in ("bass-original", "drums-original", "guitar-original"):
        assert stems["operations"][operation_id]["status"] == "completed"
    # The cascade depending on vocals-original's instrumental output never ran.
    assert stems["operations"]["guitar-instrumental"]["status"] == "pending"
    assert "instrumental" not in stems["artifacts"]


def test_resuming_after_a_crash_right_after_checkpoint_never_double_charges(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)
    crashing = _RecordingSeparator(FakeSeparator(), fail_stems_once={"vocals"})

    with pytest.raises(SeparationError):
        separate(project, crashing, guitar_type="electric")

    crashed_state = read_sidecar(project)["analysis"]["stems"]["operations"]["vocals-original"]
    assert crashed_state["status"] == "error"
    submitted_task_id = crashed_state["backend_state"]["task_id"]
    assert submitted_task_id

    resuming = _RecordingSeparator(FakeSeparator())
    result = separate(project, resuming, guitar_type="electric")

    # vocals-original resumes (one call); bass/drums/guitar-original are
    # already cached from the first run so they make no call at all; and
    # guitar-instrumental, newly unblocked now that vocals-original has an
    # instrumental output, runs for the first time (one call) -- two calls
    # total, not a resubmission of vocals-original's already-submitted task.
    assert resuming.calls == ["vocals", "electric_guitar"]
    completed = result["analysis"]["stems"]["operations"]["vocals-original"]
    assert completed["status"] == "completed"
    assert completed["backend_state"]["task_id"] == submitted_task_id  # same task id all the way through


@pytest.mark.parametrize(
    "fail_at",
    [
        "before_submit",
        "after_submit",
        "after_checkpoint",
        "before_download:stem",
        "after_download:stem",
        "before_download:back",
        "after_download:back",
    ],
)
def test_fake_separator_injected_failure_points_are_recoverable(tmp_path: Path, fail_at: str) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    with pytest.raises(SeparationError):
        separate(project, FakeSeparator(fail_at=fail_at), guitar_type="electric")

    stems_before = read_sidecar(project)["analysis"]["stems"]
    assert stems_before["operations"]["vocals-original"]["status"] == "error"
    # No half-written artifact is ever recorded as valid.
    assert "vocals" not in stems_before["artifacts"] or artifact_path(project, stems_before["artifacts"]["vocals"]).is_file()

    result = separate(project, FakeSeparator(), guitar_type="electric")
    stems_after = result["analysis"]["stems"]
    for operation_id in OPERATION_ORDER:
        assert stems_after["operations"][operation_id]["status"] == "completed"


def test_fake_separator_raises_its_own_error_type_at_the_named_point() -> None:
    calls: list[dict[str, Any]] = []

    def checkpoint(state: dict[str, Any]) -> None:
        calls.append(state)

    backend = FakeSeparator(fail_at="before_submit")
    from vgt.separation import build_recipe as _build_recipe

    spec = _build_recipe("electric")["bass-original"].spec
    with pytest.raises(FakeSeparatorError):
        backend.split(Path("unused"), Path("unused"), spec, resume_state=None, checkpoint=checkpoint)
    assert calls == []


def test_corrupt_downloaded_wav_is_never_committed_as_a_valid_artifact(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _write_v1_sidecar(project)

    class _CorruptingSeparator:
        name = "fake"
        api_version = "fake-v1"

        def split(self, source: Path, out_dir: Path, spec, *, resume_state, checkpoint) -> SplitResult:
            checkpoint({"idempotency_key": "k", "task_id": "t", "status": "submitted"})
            out_dir.mkdir(parents=True, exist_ok=True)
            outputs = {}
            for side in spec.keep:
                path = out_dir / f"{side}.wav"
                path.write_bytes(b"not a real wav file")
                outputs[side] = path
            return SplitResult(outputs=outputs, backend_state={"task_id": "t"})

    with pytest.raises(SeparationError):
        separate(project, _CorruptingSeparator(), guitar_type="electric")

    stems = read_sidecar(project)["analysis"]["stems"]
    assert stems["operations"]["vocals-original"]["status"] == "error"
    assert "vocals" not in stems["artifacts"]
    assert "instrumental" not in stems["artifacts"]
