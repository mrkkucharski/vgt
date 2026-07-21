"""Read/write the `<project>.vgt` sidecar: the shared state contract between
the Phase 0 ReaScript apply action and the Phase 1+ Python analysis CLI.

Schema versions:
  1 -- Phase 0: `managed_track_guids` + `config` (written by the ReaScript action).
  2 -- Phase 1 adds `analysis`: one entry per detector (tempo/key/sections/chords),
       each cached on an input+settings hash so re-running only recomputes stages
       whose inputs changed, and a `provenance` block recording the tool/version.
  3 -- Analysis stages record `analyzed_at` and human corrections record
       `verified_at`, both as UTC ISO-8601 timestamps. The `chords` stage also
       gains a `detected` sibling of `value` holding the pristine
       machine-detected chords, so a human correction to `value` never
       destroys the original detection (#19). Existing v2 sidecars are
       migrated by backfilling `detected` from `value` (best effort -- if a
       human had already corrected `value` under v2, the true original is
       gone and the backfill just seeds `detected` with the corrected chords).
  4 -- `managed_region_ids` records identities for section regions created by
       the ReaScript action. Older sidecars start with no recorded regions,
       which safely preserves any existing region on their first re-apply.
  5 -- The `sections` stage gains the same `detected`/`value` split as
       `chords` (#19's follow-up, consumed by `vgt sync`, see #33): a human
       correction to `value` never destroys the original detected section
       boundaries. Existing v4 sidecars are migrated the same way as v2 -> v3
       chords were: `detected` is backfilled from `value` (best effort).
  6 -- `analysis` gains a `stems` block (M1, see `separation.py`) alongside
       tempo/key/sections/chords. Unlike those stages, `stems` is not one
       cached value: it owns the fixed five-operation split DAG and the
       six-artifact index the DAG produces. Its shape is:
         {
           "backend": str | null,             # e.g. "fake", "lalal"
           "api_version": str | null,
           "recipe_version": int | null,
           "guitar_type": "electric" | "acoustic" | null,
           "artifact_namespace": str | null,  # opaque id; stable, never regenerated once set
           "operations": {
             "<operation_id>": {
               "source_role": str, "source_sha256": str | null, "spec_hash": str | null,
               "requested_presets": dict, "effective_presets": dict,
               "backend_state": dict,         # backend-opaque; published via `checkpoint`
               "status": "pending" | "in_progress" | "completed" | "error",
               "outputs": list[str],          # artifact names this operation owns
               "completed_at": str | null, "error": str | null,
             }, ...
           },
           "artifacts": {
             "<artifact_name>": {
               "operation": str, "side": "stem" | "back", "file": str,
               "sha256": str, "size_bytes": int, "duration_seconds": float,
               "separated_at": str,
             }, ...
           },
           "human_verified": bool,  # quality metadata only -- never revives a
                                     # stale/missing output, see separation.py.
           "verified_at": str | null,
         }
       Operation/artifact records are created lazily by `separation.py`, so
       `upgrade()` only guarantees the fixed top-level shape above -- the
       orchestrator, not this module, owns the recipe (per the plan).
  7 -- `analysis.stems.in_progress` is a durable, heartbeat-refreshed lease
       held by Python for the lifetime of a paid separation run.  ReaScript
       refuses a live lease and safely treats an expired one as stale.
  8 -- `analysis.stems.optional_stems` persists requested strings/piano
       additions so retries resume the same paid work safely.
  9 -- `analysis` gains a `transcription` block (T-A, see `transcribe.py`).
       Targets are independent -- retuning one target must never invalidate
       another's cached result -- so, like `stems`, it does not use the
       stage-level `input_hash`/`settings_hash` pair: it owns a `targets`
       index whose entries each carry their own hash pair. It is otherwise a
       *plain* stage: no `detected`/`value` split, because there is no
       read-back path for MIDI edits. Shape:
         {
           "requested_targets": ["guitar"],
           "targets": {
             "<target>": {
               "backend": str, "package_pin": str,
               "serialization": str | null, # Basic Pitch only; legacy v9 records retain it
               "source_role": str,
               "input_hash": str | null,      # the source stem's sha256
               "settings_hash": str | null,   # that target's TranscriptionSpec
               "status": "transcribed" | "skipped-missing-source" | "error",
               "midi_file": str | null, "notes_file": str | null, "events_file": str | null,
               "note_count": int | null, "pitch_range_midi": [int, int] | null,
               "first_note_s": float | null, "last_note_s": float | null,
               "midi_tempo": float | null, "settings": dict, # backend-specific settings
               "transcribed_at": str | null, "error": str | null,
             }, ...
           },
         }
       Older sidecars migrate to `requested_targets: ["guitar"]` and an empty
       `targets` index -- no data migration, since there is nothing to migrate.

Every stage entry has the same shape:
  {
    "value": <detector output, or null if never run>,
    "human_verified": bool,   # true once a human has corrected/confirmed it
    "input_hash": str | null, # hash of the analyzed audio at last (re)compute
    "settings_hash": str | null,
    "analyzed_at": str | null, # UTC time at which this value was detected
    "verified_at": str | null, # UTC time at which a human verified it
  }
A human correction is applied by setting "value" and "human_verified": true;
`refresh_stage` then leaves it untouched on every later re-run regardless of
whether the input or settings hash changed.

The `chords` and `sections` stages additionally carry:
  {
    "detected": <machine-detected value, independent of human corrections>,
    "detected_input_hash": str | null,    # hash `detected` was last computed against
    "detected_settings_hash": str | null,
  }
`detected` is never touched by `vgt sync`; only `vgt analyze`'s detectors
write it. Unlike `value`, `detected` keeps tracking the current audio and
settings via its own hash pair even once `value` is human-verified and
frozen -- it is the machine baseline, so it stays live, while the human's
`value` is what freezes (see `analysis.py`'s `_refresh_stage_with_detected`).

Schema 4 adds `managed_region_ids`, the REAPER region IDs created by vgt.
Older sidecars migrate with an empty list: without a prior identity record,
vgt preserves every existing region, including `[vgt]`-named regions a user
may have made.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import copy
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
import shutil
import tempfile
import uuid

SCHEMA_VERSION = 9
STEMS_LEASE_TIMEOUT = timedelta(minutes=30)

ANALYSIS_STAGES = ("tempo", "key", "sections", "chords", "transcription")

# Stages that carry the detected/value split (#19): a human correction to
# `value` never overwrites the pristine machine detection kept in `detected`.
DETECTED_SPLIT_STAGES = ("sections", "chords")

# `transcription` (like `stems`) owns a per-target index instead of the
# generic value/input_hash/settings_hash shape every other stage in
# ANALYSIS_STAGES uses; it is upgraded and reconciled separately.
DEFAULT_TRANSCRIPTION_TARGETS = ("guitar",)


class SidecarError(ValueError):
    """The sidecar file is missing or does not contain the data we need."""


def sidecar_path(project_path: str | Path) -> Path:
    path = Path(project_path)
    return path.with_suffix(".vgt")


def artifact_namespace_dir(project_path: str | Path, namespace: str) -> Path:
    """Directory for `namespace`'s regenerable artifacts: `vgt/<namespace>/`
    next to the project. The `.vgt` sidecar itself stays adjacent to the RPP,
    not here."""
    return Path(project_path).parent / "vgt" / namespace


def ensure_artifact_namespace(sidecar: dict[str, Any], project_path: str | Path) -> str:
    """Return `sidecar`'s stable artifact namespace, generating and
    persisting one into `analysis.stems` on first use.

    On that first use, migrate only the three exact filenames vgt used before
    the namespace layout.  Their presence is proof of vgt ownership; no glob
    search is performed, and unknown files are left alone.  Never regenerate
    the namespace once set, even if the project is later renamed (see module
    docstring, schema 6).

    The namespace is a bare opaque id, deliberately carrying no trace of the
    project name.  Only the id is ever matched on; a `<project-stem>-` prefix
    would read as a claim about which project owns the directory, and because
    the namespace is never regenerated, the first rename of the RPP would
    turn that claim into a lie (`7Rivers/vgt/Old Name-6a7745be/`).  Namespaces
    generated before this carry the old prefixed form and stay valid --
    nothing parses them.
    """
    stems = sidecar["analysis"]["stems"]
    namespace = stems.get("artifact_namespace")
    if namespace is None:
        namespace = uuid.uuid4().hex[:8]
        stems["artifact_namespace"] = namespace
        _migrate_legacy_analysis_artifacts(sidecar, Path(project_path), namespace)
    return namespace


def _migrate_legacy_analysis_artifacts(sidecar: dict[str, Any], project_path: Path, namespace: str) -> None:
    """Copy known pre-namespace analysis artifacts into ``namespace``.

    Metadata is changed only for the exact filenames emitted by prior vgt
    releases.  This retains harmless legacy orphans and never mistakes an
    arbitrary user file for a vgt artifact.
    """
    analysis = sidecar["analysis"]
    legacy_dir = project_path.parent
    destination_dir = artifact_namespace_dir(project_path, namespace)
    expected = {
        "click_artifact_path": f"{project_path.stem}.vgt-tempo-click.wav",
        "chord_sheet_path": f"{project_path.stem}.vgt-chords.txt",
    }
    artifacts: list[tuple[Path, str]] = []

    tempo_value = analysis["tempo"].get("value")
    if isinstance(tempo_value, dict) and tempo_value.get("click_artifact_path") == expected["click_artifact_path"]:
        artifacts.append((legacy_dir / expected["click_artifact_path"], "tempo-click.wav"))
        tempo_value["click_artifact_path"] = "tempo-click.wav"

    chords_value = analysis["chords"].get("value")
    if isinstance(chords_value, dict) and chords_value.get("chord_sheet_path") == expected["chord_sheet_path"]:
        artifacts.append((legacy_dir / expected["chord_sheet_path"], "chords.txt"))
        chords_value["chord_sheet_path"] = "chords.txt"

    # Unlike click/chords, old section timelines had no sidecar path field.
    # A non-null stage value is the exact schema-level evidence that vgt had
    # produced this fixed filename.
    if analysis["sections"].get("value") is not None:
        artifacts.append((legacy_dir / f"{project_path.stem}.vgt-sections.txt", "sections.txt"))

    for legacy_path, filename in artifacts:
        if legacy_path.is_file():
            destination_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_path, destination_dir / filename)


def _empty_stage() -> dict[str, Any]:
    return {
        "value": None,
        "human_verified": False,
        "input_hash": None,
        "settings_hash": None,
        "analyzed_at": None,
        "verified_at": None,
    }


def _empty_detected_split_stage() -> dict[str, Any]:
    return {**_empty_stage(), "detected": None, "detected_input_hash": None, "detected_settings_hash": None}


def _empty_stems_block() -> dict[str, Any]:
    return {
        "backend": None,
        "api_version": None,
        "recipe_version": None,
        "guitar_type": None,
        "optional_stems": [],
        "artifact_namespace": None,
        # A durable, short-lived ownership record for the paid separator.
        # It is deliberately inside ``stems`` so ReaScript can read it while
        # remaining oblivious to Python's implementation details.
        "in_progress": None,
        "operations": {},
        "artifacts": {},
        "human_verified": False,
        "verified_at": None,
    }


def _empty_transcription_block() -> dict[str, Any]:
    return {"requested_targets": list(DEFAULT_TRANSCRIPTION_TARGETS), "targets": {}}


def read_sidecar(project_path: str | Path) -> dict[str, Any]:
    """Read the sidecar for `project_path`, upgrading older schema versions in memory."""
    path = sidecar_path(project_path)
    if not path.is_file():
        raise SidecarError(f"No .vgt sidecar found at {path}; run the Phase 0 apply action first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return upgrade(data)


def upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """Return `data` with all older fields intact and a current-schema `analysis` block present."""
    upgraded = dict(data)
    upgraded["schema_version"] = SCHEMA_VERSION
    # Region names are presentation, not ownership. Do not infer ownership
    # from a `[vgt]` prefix while migrating an older sidecar.
    managed_region_ids = upgraded.get("managed_region_ids")
    upgraded["managed_region_ids"] = managed_region_ids if isinstance(managed_region_ids, list) else []
    analysis = dict(upgraded.get("analysis") or {})
    for stage in ANALYSIS_STAGES:
        if stage == "transcription":
            continue  # owns a per-target index, merged separately below like `stems`
        if stage in DETECTED_SPLIT_STAGES:
            merged = {**_empty_detected_split_stage(), **(analysis.get(stage) or {})}
            if merged["detected"] is None and merged["value"] is not None:
                # Best-effort backfill, see module docstring (v2 -> v3 for
                # chords, v4 -> v5 for sections). Assume `detected` was last
                # computed alongside `value`, so it inherits `value`'s hash
                # pair rather than starting stale.
                merged["detected"] = copy.deepcopy(merged["value"])
                merged["detected_input_hash"] = merged["input_hash"]
                merged["detected_settings_hash"] = merged["settings_hash"]
            analysis[stage] = merged
        else:
            analysis[stage] = {**_empty_stage(), **(analysis.get(stage) or {})}
    analysis.setdefault("provenance", {"tool": "vgt", "version": None, "settings": {}})
    stems = {**_empty_stems_block(), **(analysis.get("stems") or {})}
    stems["operations"] = dict(stems.get("operations") or {})
    stems["artifacts"] = dict(stems.get("artifacts") or {})
    stems["optional_stems"] = list(stems.get("optional_stems") or [])
    analysis["stems"] = stems

    transcription = {**_empty_transcription_block(), **(analysis.get("transcription") or {})}
    # An empty list is meaningful: it is the persisted state after a user
    # forgets their final target.  Only a missing/null or malformed value
    # needs the schema-v9 default.
    requested_targets = transcription.get("requested_targets")
    transcription["requested_targets"] = (
        list(requested_targets) if isinstance(requested_targets, list) else list(DEFAULT_TRANSCRIPTION_TARGETS)
    )
    transcription["targets"] = dict(transcription.get("targets") or {})
    analysis["transcription"] = transcription

    upgraded["analysis"] = analysis
    return upgraded


def write_sidecar(project_path: str | Path, data: dict[str, Any]) -> None:
    """Write the sidecar via temp-file + atomic replace so a crash mid-write
    never leaves a partially-written `.vgt` behind (required for the
    separation stage's paid, must-not-double-charge checkpoints; harmless
    for every other stage)."""
    path = sidecar_path(project_path)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _read_sidecar_unlocked(project_path: str | Path) -> dict[str, Any]:
    path = sidecar_path(project_path)
    if not path.is_file():
        raise SidecarError(f"No .vgt sidecar found at {path}; run the Phase 0 apply action first.")
    return upgrade(json.loads(path.read_text(encoding="utf-8")))


def atomic_update_sidecar(project_path: str | Path, update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Atomically read, merge, and replace a sidecar.

    Python writers use an advisory sibling lock and always reread immediately
    before writing.  This keeps a local-analysis update from replacing a
    separator checkpoint written by another Python process.  The ReaScript
    lease prevents its separate writer from entering this critical period.
    """
    path = sidecar_path(project_path)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_sidecar_unlocked(project_path)
            update(data)
            write_sidecar(project_path, data)
            return data
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def update_analysis(project_path: str | Path, update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Atomically update only Python-owned data below ``analysis``.

    Top-level fields are owned by the ReaScript and are intentionally kept
    byte-for-value from the most recently read sidecar.
    """
    return atomic_update_sidecar(project_path, lambda data: update(data["analysis"]))


def _lease_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stems_lease_is_live(stems: dict[str, Any], *, now: datetime | None = None) -> bool:
    lease = stems.get("in_progress")
    heartbeat = _lease_time(lease.get("heartbeat_at")) if isinstance(lease, dict) else None
    current = now or datetime.now(UTC)
    return heartbeat is not None and timedelta(0) <= current - heartbeat < STEMS_LEASE_TIMEOUT


def acquire_stems_lease(project_path: str | Path, owner_id: str | None = None) -> tuple[dict[str, Any], str]:
    """Claim the durable separation lease, replacing only a stale lease."""
    owner = owner_id or uuid.uuid4().hex
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def claim(analysis: dict[str, Any]) -> None:
        stems = analysis["stems"]
        if stems_lease_is_live(stems):
            raise SidecarError("Stem separation is already in progress; retry after it finishes.")
        stems["in_progress"] = {"owner_id": owner, "started_at": now, "heartbeat_at": now}

    return update_analysis(project_path, claim), owner


def update_stems_under_lease(project_path: str | Path, stems: dict[str, Any], owner_id: str) -> dict[str, Any]:
    """Persist a separator checkpoint and refresh its lease heartbeat."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def update(analysis: dict[str, Any]) -> None:
        current = analysis["stems"]
        lease = current.get("in_progress")
        if not isinstance(lease, dict) or lease.get("owner_id") != owner_id:
            raise SidecarError("Stem separation lease was lost; refusing to submit or checkpoint work.")
        replacement = copy.deepcopy(stems)
        replacement["in_progress"] = {
            "owner_id": owner_id,
            "started_at": lease.get("started_at", now),
            "heartbeat_at": now,
        }
        analysis["stems"] = replacement

    return update_analysis(project_path, update)


def release_stems_lease(project_path: str | Path, owner_id: str) -> dict[str, Any]:
    """Clear our lease without ever clearing a newer owner's lease."""
    def release(analysis: dict[str, Any]) -> None:
        lease = analysis["stems"].get("in_progress")
        if isinstance(lease, dict) and lease.get("owner_id") == owner_id:
            analysis["stems"]["in_progress"] = None

    return update_analysis(project_path, release)


def stage_is_current(stage: dict[str, Any], *, input_hash: str, settings_hash: str) -> bool:
    """True if `stage`'s cached value still stands -- either a human verified it,
    or the audio and settings that produced it are unchanged -- so a rerun would
    leave it untouched."""
    if stage.get("human_verified"):
        return True
    return stage.get("input_hash") == input_hash and stage.get("settings_hash") == settings_hash


def refresh_stage(
    stage: dict[str, Any],
    *,
    input_hash: str,
    settings_hash: str,
    compute: Callable[[], Any],
    force: bool = False,
    analyzed_at: str | None = None,
) -> dict[str, Any]:
    """Recompute a stage's cached value unless a human has verified it, or the
    inputs/settings that produced the cached value haven't changed.

    `force` recomputes even when the cache is current, but never overrides a
    human-verified stage -- that correction is preserved regardless."""
    if stage.get("human_verified"):
        return stage
    if not force and stage_is_current(stage, input_hash=input_hash, settings_hash=settings_hash):
        return stage
    return {
        "value": compute(),
        "human_verified": False,
        "input_hash": input_hash,
        "settings_hash": settings_hash,
        "analyzed_at": analyzed_at,
        "verified_at": None,
    }
