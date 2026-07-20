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
           "artifact_namespace": str | null,  # stable, never regenerated once set
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
import json
import os
import tempfile

SCHEMA_VERSION = 6

ANALYSIS_STAGES = ("tempo", "key", "sections", "chords")

# Stages that carry the detected/value split (#19): a human correction to
# `value` never overwrites the pristine machine detection kept in `detected`.
DETECTED_SPLIT_STAGES = ("sections", "chords")


class SidecarError(ValueError):
    """The sidecar file is missing or does not contain the data we need."""


def sidecar_path(project_path: str | Path) -> Path:
    path = Path(project_path)
    return path.with_suffix(".vgt")


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
        "artifact_namespace": None,
        "operations": {},
        "artifacts": {},
        "human_verified": False,
        "verified_at": None,
    }


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
    analysis["stems"] = stems
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
