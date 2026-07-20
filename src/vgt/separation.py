"""Stem separation orchestrator (M1-A, see docs/stem-separation-plan.md).

Owns the fixed five-operation split DAG, content-identity hashing, cache
checks, output validation/naming, and durable sidecar checkpoints. A backend
(`Separator`) only executes one split at a time -- it never decides *what* to
split, *which side* to keep, or *whether* a cached result is still valid.
That separation is deliberate: it lets a network-free `FakeSeparator` exercise
every recipe/cache/partial-resume/sidecar code path that the real LALAL
backend (a later issue) will also drive.

The fixed recipe (docs/stem-separation-plan.md, "Separation recipe"):
  1. vocals-original     split(vocals) from the original  -> keep vocals (stem)
                                                          + instrumental (back)
  2. bass-original        split(bass) from the original    -> keep bass (stem)
  3. drums-original       split(drum) from the original     -> keep drums (stem)
  4. guitar-original      split(<guitar_type>_guitar) from the original
                                                          -> keep guitar (stem)
  5. guitar-instrumental  split(<guitar_type>_guitar) from the *instrumental*
                          artifact (1's back side)         -> keep backing (back)
guitar-instrumental is the *only* cascade (split of a split); everything else
is always split from the original source, per the plan's correctness
invariant. `guitar_type` is declared by the caller, never detected here.

Two invariants this module exists to guarantee (docs/stem-separation-plan.md
Acceptance criteria, and repeated in the issue as "must-have"):
  - Never double-charge: an operation already `in_progress` resumes with its
    persisted `backend_state` (which a real backend uses to recover a task id)
    instead of silently starting a fresh paid task.
  - Never treat a partial/corrupt output as valid: every retained side is
    validated (readable WAV, non-zero duration) before it is committed as an
    artifact, and a cache hit re-checks the artifact still exists on disk at
    its recorded size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol
import hashlib
import json
import shutil
import uuid
import wave

from .analysis import reference_source_path
from .project import ProjectError, locate_project
from .sidecar import (
    SidecarError,
    artifact_namespace_dir,
    ensure_artifact_namespace,
    read_sidecar,
    write_sidecar,
)

RECIPE_VERSION = 1

# Fixed operation identities and their dependency-respecting run order. Only
# "guitar-instrumental" depends on another operation (vocals-original's
# instrumental output); the rest always split the original source.
OPERATION_ORDER: tuple[str, ...] = (
    "vocals-original",
    "bass-original",
    "drums-original",
    "guitar-original",
    "guitar-instrumental",
)

# Final artifact filenames, fixed by the plan's artifact layout.
ARTIFACT_FILENAMES: dict[str, str] = {
    "vocals": "vocals.wav",
    "instrumental": "instrumental.wav",
    "bass": "bass.wav",
    "drums": "drums.wav",
    "guitar": "guitar.wav",
    "backing": "backing-no-guitar.wav",
}

GUITAR_TYPES = ("electric", "acoustic")


class SeparationError(ValueError):
    """The project, its sidecar, or a split request cannot be processed."""


@dataclass(frozen=True)
class SplitSpec:
    """One paid split operation. `keep` names which side(s) of the split are
    retained: `("stem",)`, `("back",)`, or `("stem", "back")` for the one
    operation (vocals-original) that keeps both."""

    stem: str
    source_role: str  # "original" | "instrumental"
    keep: tuple[str, ...]
    splitter: str = "auto"
    dereverb_enabled: bool = False
    extraction_level: str = "deep_extraction"  # "deep_extraction" | "clear_cut"
    encoder_format: str = "wav"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stem": self.stem,
            "source_role": self.source_role,
            "keep": list(self.keep),
            "splitter": self.splitter,
            "dereverb_enabled": self.dereverb_enabled,
            "extraction_level": self.extraction_level,
            "encoder_format": self.encoder_format,
        }


def spec_hash(spec: SplitSpec) -> str:
    return hashlib.sha256(json.dumps(spec.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OperationDef:
    operation_id: str
    spec: SplitSpec
    depends_on: str | None
    # side -> artifact name this operation owns on that side.
    artifact_names: dict[str, str]


def build_recipe(guitar_type: str) -> dict[str, OperationDef]:
    """The fixed five-operation DAG for one `guitar_type`. `guitar_type`
    selects the requested stem for the two guitar operations only; changing
    it therefore changes only their `spec_hash`, which is what makes cache
    invalidation naturally scoped to exactly those two operations."""
    if guitar_type not in GUITAR_TYPES:
        raise SeparationError(f"guitar_type must be one of {GUITAR_TYPES}, got {guitar_type!r}")
    guitar_stem = f"{guitar_type}_guitar"
    return {
        "vocals-original": OperationDef(
            operation_id="vocals-original",
            spec=SplitSpec(stem="vocals", source_role="original", keep=("stem", "back")),
            depends_on=None,
            artifact_names={"stem": "vocals", "back": "instrumental"},
        ),
        "bass-original": OperationDef(
            operation_id="bass-original",
            spec=SplitSpec(stem="bass", source_role="original", keep=("stem",)),
            depends_on=None,
            artifact_names={"stem": "bass"},
        ),
        "drums-original": OperationDef(
            operation_id="drums-original",
            spec=SplitSpec(stem="drum", source_role="original", keep=("stem",)),
            depends_on=None,
            artifact_names={"stem": "drums"},
        ),
        "guitar-original": OperationDef(
            operation_id="guitar-original",
            spec=SplitSpec(stem=guitar_stem, source_role="original", keep=("stem",)),
            depends_on=None,
            artifact_names={"stem": "guitar"},
        ),
        "guitar-instrumental": OperationDef(
            operation_id="guitar-instrumental",
            spec=SplitSpec(stem=guitar_stem, source_role="instrumental", keep=("back",)),
            depends_on="vocals-original",
            artifact_names={"back": "backing"},
        ),
    }


@dataclass
class SplitResult:
    """What a `Separator` hands back after a successful split. `outputs` maps
    each retained side to a *local* file it has already validated and
    committed on its own side (e.g. streamed to `.part` then atomically
    renamed) -- the orchestrator re-validates and renames into the final
    artifact location, but does not trust an un-committed backend file."""

    outputs: dict[str, Path]
    backend_state: dict[str, Any]
    effective_presets: dict[str, Any] = field(default_factory=dict)


class Separator(Protocol):
    """Thin backend seam. The orchestrator owns the recipe and cache policy;
    a backend only executes one split and reports back what happened.

    `resume_state` is whatever this backend last published via `checkpoint`
    for this operation (or `None` on a first attempt) -- backend-opaque, so
    the orchestrator never interprets it. `checkpoint` lets the backend
    durably publish remote identity/state (e.g. a task id) the moment it is
    known, before the split has finished, so a crash mid-split resumes
    instead of risking a duplicate paid submission.
    """

    name: str
    api_version: str

    def split(
        self,
        source: Path,
        out_dir: Path,
        spec: SplitSpec,
        *,
        resume_state: dict[str, Any] | None,
        checkpoint: Callable[[dict[str, Any]], None],
    ) -> SplitResult: ...


class FakeSeparatorError(RuntimeError):
    """An injected failure point was reached (test-only)."""


class FakeSeparator:
    """Writes valid short WAVs instead of calling a network API, so recipe,
    cache, partial-resume, and sidecar behavior can all be tested without
    network access.

    `fail_at` names a single injection point at which `split` raises
    `FakeSeparatorError` instead of completing, letting tests assert that a
    crash there is safe to resume from:
      - "before_submit"      -- before anything happens (e.g. upload never went out)
      - "after_submit"       -- a task id was minted but not yet checkpointed
                                 (simulates an ambiguous submission)
      - "after_checkpoint"   -- the task id was durably checkpointed, then the
                                 process died before any output was produced
      - "before_download:<side>" -- before writing that side's audio at all
      - "after_download:<side>"  -- after the `.part` file exists but before
                                     it is validated and atomically renamed
    A backend resumed with a `resume_state` that already carries a `task_id`
    never mints a new one -- the fake's proof that resuming never
    double-charges.
    """

    name = "fake"
    api_version = "fake-v1"

    def __init__(self, fail_at: str | None = None, sample_rate: int = 8000, duration_seconds: float = 0.2) -> None:
        self.fail_at = fail_at
        self.sample_rate = sample_rate
        self.duration_seconds = duration_seconds

    def _maybe_fail(self, point: str) -> None:
        if self.fail_at == point:
            raise FakeSeparatorError(f"injected failure at {point!r}")

    def split(
        self,
        source: Path,
        out_dir: Path,
        spec: SplitSpec,
        *,
        resume_state: dict[str, Any] | None,
        checkpoint: Callable[[dict[str, Any]], None],
    ) -> SplitResult:
        resume_state = resume_state or {}
        self._maybe_fail("before_submit")

        idempotency_key = resume_state.get("idempotency_key") or str(uuid.uuid4())
        task_id = resume_state.get("task_id")
        if task_id is None:
            self._maybe_fail("after_submit")
            task_id = f"fake-task-{idempotency_key}"
            checkpoint({"idempotency_key": idempotency_key, "task_id": task_id, "status": "submitted"})
            self._maybe_fail("after_checkpoint")
        else:
            checkpoint({"idempotency_key": idempotency_key, "task_id": task_id, "status": "resumed"})

        out_dir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}
        for side in spec.keep:
            self._maybe_fail(f"before_download:{side}")
            part_path = out_dir / f"{side}.wav.part"
            final_path = out_dir / f"{side}.wav"
            _write_fake_wav(part_path, self.sample_rate, self.duration_seconds, seed=_content_seed(source, spec, side))
            self._maybe_fail(f"after_download:{side}")
            part_path.replace(final_path)
            outputs[side] = final_path

        checkpoint({"idempotency_key": idempotency_key, "task_id": task_id, "status": "completed"})
        return SplitResult(
            outputs=outputs,
            backend_state={"idempotency_key": idempotency_key, "task_id": task_id, "status": "completed"},
            effective_presets={"splitter": "Andromeda" if spec.splitter == "auto" else spec.splitter},
        )


def _content_seed(source: Path, spec: SplitSpec, side: str) -> int:
    """Derive a small amplitude value from `source`'s bytes, the spec, and
    the requested side, so `FakeSeparator`'s otherwise-fabricated output is
    still content-addressed: the same input reliably reproduces the same
    output bytes (needed for cache-hit tests), and different input reliably
    changes them (needed for cache-invalidation tests) -- unlike constant
    silence, which would make every operation look identical regardless of
    its source."""
    digest = hashlib.sha256()
    digest.update(source.read_bytes())
    digest.update(json.dumps(spec.to_dict(), sort_keys=True).encode("utf-8"))
    digest.update(side.encode("utf-8"))
    return int.from_bytes(digest.digest()[:2], "big") % 30000 + 100


def _write_fake_wav(path: Path, sample_rate: int, duration_seconds: float, *, seed: int = 0) -> None:
    frame_count = max(1, int(sample_rate * duration_seconds))
    sample = int(seed).to_bytes(2, "little", signed=False)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(sample * frame_count)


def hash_audio_content(path: Path) -> str:
    """Full SHA-256 of `path`'s bytes -- content identity for paid work.
    Deliberately not `analysis.hash_source_file`'s cheap path/size/mtime
    hash: moving the project must never trigger a re-bill, and different
    bytes at the same path must never reuse someone else's stems."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_wav(path: Path) -> float:
    """Raise `SeparationError` unless `path` is a readable WAV with a
    plausible (non-zero) duration; otherwise return that duration in
    seconds. A `.part` file is never passed here -- only a backend's already
    -committed output is."""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except (wave.Error, EOFError, OSError) as exc:
        raise SeparationError(f"{path}: not a valid WAV file: {exc}") from exc
    if frames <= 0 or rate <= 0:
        raise SeparationError(f"{path}: WAV has no audio frames")
    return frames / rate


def stems_dir(project_path: Path, namespace: str) -> Path:
    return artifact_namespace_dir(project_path, namespace) / "stems"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _empty_operation_record(op_def: OperationDef) -> dict[str, Any]:
    return {
        "source_role": op_def.spec.source_role,
        "source_sha256": None,
        "spec_hash": None,
        "requested_presets": op_def.spec.to_dict(),
        "effective_presets": {},
        "backend_state": {},
        "status": "pending",
        "outputs": [],
        "completed_at": None,
        "error": None,
    }


def artifact_path(project_path: Path, artifact: dict[str, Any]) -> Path:
    """Resolve an artifact record's `file` (stored relative to the song
    folder, i.e. `project_path.parent`) to an absolute path. Storing it
    relative -- rather than the absolute path it was written to -- is what
    lets moving the whole project folder keep the cache current instead of
    every artifact looking "missing"."""
    return project_path.parent / artifact["file"]


def _artifacts_on_disk(project_path: Path, record: dict[str, Any], artifacts: dict[str, Any]) -> bool:
    """A completed operation is only still cached if every artifact it
    produced still exists at its recorded size -- an operation is never
    treated as current just because the sidecar says so."""
    for artifact_name in record.get("outputs", []):
        artifact = artifacts.get(artifact_name)
        if not artifact:
            return False
        path = artifact_path(project_path, artifact)
        if not path.is_file() or path.stat().st_size != artifact.get("size_bytes"):
            return False
    return True


def _operation_is_current(
    project_path: Path, record: dict[str, Any], *, source_sha256: str, current_spec_hash: str, artifacts: dict[str, Any]
) -> bool:
    if record.get("status") != "completed":
        return False
    if record.get("source_sha256") != source_sha256 or record.get("spec_hash") != current_spec_hash:
        return False
    return _artifacts_on_disk(project_path, record, artifacts)


def _run_operation(
    *,
    project_path: Path,
    sidecar: dict[str, Any],
    op_def: OperationDef,
    backend: Separator,
    source_path: Path,
    source_sha256: str,
    namespace: str,
    force: bool,
    persist: Callable[[], None],
    emit: Callable[[str], None],
) -> None:
    stems = sidecar["analysis"]["stems"]
    operations = stems["operations"]
    artifacts = stems["artifacts"]
    record = dict(operations.get(op_def.operation_id) or _empty_operation_record(op_def))
    operations[op_def.operation_id] = record

    current_spec_hash = spec_hash(op_def.spec)
    if not force and _operation_is_current(
        project_path, record, source_sha256=source_sha256, current_spec_hash=current_spec_hash, artifacts=artifacts
    ):
        emit(f"{op_def.operation_id}: cached, skipping")
        return

    # Resume a previously checkpointed attempt of the *same* work -- whether
    # it was left "in_progress" (process died mid-split) or "error" (the
    # backend raised, but had already durably published a task id via
    # `checkpoint` before doing so): either way that backend_state is the
    # only record of a possibly-already-submitted paid task, and must be
    # handed back rather than dropped, or a retry could double-charge. A
    # changed source or spec, by contrast, invalidates any in-flight
    # backend_state -- a stale task id from a different request must never
    # be reused as if it were for this one.
    matches_prior_attempt = record.get("source_sha256") == source_sha256 and record.get("spec_hash") == current_spec_hash
    resumable = matches_prior_attempt and bool(record.get("backend_state"))
    resume_state = dict(record.get("backend_state") or {}) if resumable else None
    if resumable:
        emit(f"{op_def.operation_id}: resuming previous attempt")
    else:
        emit(f"{op_def.operation_id}: starting")
        record["backend_state"] = {}

    record["status"] = "in_progress"
    record["source_sha256"] = source_sha256
    record["spec_hash"] = current_spec_hash
    record["requested_presets"] = op_def.spec.to_dict()
    record["error"] = None
    # Durably record intent to run this operation *before* calling the
    # backend: if the process dies before the backend's own first checkpoint,
    # the next run still sees "in_progress" with no backend_state, not
    # "pending" (which would look untouched).
    persist()

    def checkpoint(backend_state: dict[str, Any]) -> None:
        record["backend_state"] = {**record.get("backend_state", {}), **backend_state}
        persist()

    work_dir = stems_dir(project_path, namespace) / "_work" / op_def.operation_id
    try:
        try:
            result = backend.split(source_path, work_dir, op_def.spec, resume_state=resume_state, checkpoint=checkpoint)
        except Exception as exc:  # noqa: BLE001 -- recorded, then re-raised for the caller
            record["status"] = "error"
            record["error"] = str(exc)
            persist()
            raise

        committed: list[str] = []
        try:
            for side, local_path in result.outputs.items():
                artifact_name = op_def.artifact_names[side]
                duration_seconds = _validate_wav(local_path)
                final_path = stems_dir(project_path, namespace) / ARTIFACT_FILENAMES[artifact_name]
                final_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_final = final_path.with_suffix(final_path.suffix + ".part")
                shutil.move(str(local_path), str(tmp_final))
                tmp_final.replace(final_path)
                artifacts[artifact_name] = {
                    "operation": op_def.operation_id,
                    "side": side,
                    "file": str(final_path.relative_to(project_path.parent)),
                    "sha256": hash_audio_content(final_path),
                    "size_bytes": final_path.stat().st_size,
                    "duration_seconds": duration_seconds,
                    "separated_at": _now(),
                }
                committed.append(artifact_name)
        except Exception as exc:  # noqa: BLE001
            # A partial/corrupt output must never be recorded as a valid
            # artifact, and the operation must not be left looking "completed".
            record["status"] = "error"
            record["error"] = str(exc)
            persist()
            raise SeparationError(f"{op_def.operation_id}: {exc}") from exc
    finally:
        if work_dir.is_dir():
            shutil.rmtree(work_dir, ignore_errors=True)

    record["status"] = "completed"
    record["outputs"] = committed
    record["effective_presets"] = result.effective_presets
    record["completed_at"] = _now()
    record["error"] = None
    persist()
    emit(f"{op_def.operation_id}: completed ({', '.join(committed)})")


def separate(
    project: str | Path | None,
    backend: Separator,
    *,
    guitar_type: str | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run (or resume/refresh) the fixed five-operation split recipe for
    `project` against `backend`, persisting durable checkpoints into the
    `.vgt` sidecar as each operation progresses.

    Idempotent and safe to interrupt: unchanged source bytes and unchanged
    per-operation specs leave completed operations untouched (`force`
    recomputes every operation regardless of cache, but a paid recompute is
    still gated by the caller -- this function does not itself decide
    whether spending is acceptable, matching the plan's `--force` vs
    `--force-stems` split, which is CLI-layer (issue C)).
    """
    emit = progress or (lambda _message: None)
    project_path = locate_project(project)
    try:
        sidecar = read_sidecar(project_path)
    except SidecarError as exc:
        raise SeparationError(str(exc)) from exc

    try:
        source = reference_source_path(project_path, sidecar)
    except ProjectError as exc:
        raise SeparationError(str(exc)) from exc
    if not source.is_file():
        raise SeparationError(f"Reference source file not found: {source}")

    stems = sidecar["analysis"]["stems"]
    resolved_guitar_type = guitar_type or stems.get("guitar_type") or "electric"
    if resolved_guitar_type not in GUITAR_TYPES:
        raise SeparationError(f"guitar_type must be one of {GUITAR_TYPES}, got {resolved_guitar_type!r}")
    stems["guitar_type"] = resolved_guitar_type
    stems["backend"] = backend.name
    stems["api_version"] = backend.api_version
    stems["recipe_version"] = RECIPE_VERSION
    namespace = ensure_artifact_namespace(sidecar, project_path)

    def persist() -> None:
        write_sidecar(project_path, sidecar)

    recipe = build_recipe(resolved_guitar_type)
    # Every fixed operation gets a visible "pending" record up front (e.g.
    # for `vgt status`), even one this run never reaches because its
    # dependency didn't complete.
    for operation_id in OPERATION_ORDER:
        stems["operations"].setdefault(operation_id, _empty_operation_record(recipe[operation_id]))
    persist()

    source_sha256 = hash_audio_content(source)
    total = len(OPERATION_ORDER)
    errors: list[str] = []
    for position, operation_id in enumerate(OPERATION_ORDER, start=1):
        op_def = recipe[operation_id]
        emit(f"[{position}/{total}] {operation_id}")
        if op_def.depends_on:
            instrumental = stems["artifacts"].get("instrumental")
            op_source_path = artifact_path(project_path, instrumental) if instrumental else None
            if not instrumental or not op_source_path.is_file():
                emit(f"{operation_id}: waiting on {op_def.depends_on}'s instrumental output; skipping this run")
                continue
            op_source_sha256 = instrumental["sha256"]
        else:
            op_source_path = source
            op_source_sha256 = source_sha256
        try:
            _run_operation(
                project_path=project_path,
                sidecar=sidecar,
                op_def=op_def,
                backend=backend,
                source_path=op_source_path,
                source_sha256=op_source_sha256,
                namespace=namespace,
                force=force,
                persist=persist,
                emit=emit,
            )
        except Exception as exc:  # noqa: BLE001 -- one operation's failure must not
            # block the other, independent operations (only guitar-instrumental
            # actually depends on another operation, and that dependency is
            # already handled above); every failure is still recorded on its
            # own operation record via `_run_operation`'s own persist() calls.
            errors.append(f"{operation_id}: {exc}")

    persist()
    if errors:
        raise SeparationError("; ".join(errors))
    return sidecar
