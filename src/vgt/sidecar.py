"""Read/write the `<project>.vgt` sidecar: the shared state contract between
the Phase 0 ReaScript apply action and the Phase 1+ Python analysis CLI.

Schema versions:
  1 -- Phase 0: `managed_track_guids` + `config` (written by the ReaScript action).
  2 -- Phase 1 adds `analysis`: one entry per detector (tempo/key/sections/chords),
       each cached on an input+settings hash so re-running only recomputes stages
       whose inputs changed, and a `provenance` block recording the tool/version.
  3 -- The `chords` stage gains a `detected` sibling of `value` holding the
       pristine machine-detected chords, so a human correction to `value`
       never destroys the original detection (#19). Existing v2 sidecars are
       migrated by backfilling `detected` from `value` (best effort -- if a
       human had already corrected `value` under v2, the true original is
       gone and the backfill just seeds `detected` with the corrected chords).

Every stage entry has the same shape:
  {
    "value": <detector output, or null if never run>,
    "human_verified": bool,   # true once a human has corrected/confirmed it
    "input_hash": str | null, # hash of the analyzed audio at last (re)compute
    "settings_hash": str | null,
  }
A human correction is applied by setting "value" and "human_verified": true;
`refresh_stage` then leaves it untouched on every later re-run regardless of
whether the input or settings hash changed.

The `chords` stage additionally carries:
  {
    "detected": <machine-detected chords, independent of human corrections>,
    "detected_input_hash": str | null,    # hash `detected` was last computed against
    "detected_settings_hash": str | null,
  }
`detected` is never touched by `read-chords`; only `vgt analyze`'s detector
writes it. Unlike `value`, `detected` keeps tracking the current audio and
settings via its own hash pair even once `value` is human-verified and
frozen -- it is the machine baseline, so it stays live, while the human's
`value` is what freezes (see `analysis.py`'s `_refresh_chords_stage`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import copy
import json

SCHEMA_VERSION = 3

ANALYSIS_STAGES = ("tempo", "key", "sections", "chords")


class SidecarError(ValueError):
    """The sidecar file is missing or does not contain the data we need."""


def sidecar_path(project_path: str | Path) -> Path:
    path = Path(project_path)
    return path.with_suffix(".vgt")


def _empty_stage() -> dict[str, Any]:
    return {"value": None, "human_verified": False, "input_hash": None, "settings_hash": None}


def _empty_chords_stage() -> dict[str, Any]:
    return {**_empty_stage(), "detected": None, "detected_input_hash": None, "detected_settings_hash": None}


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
    analysis = dict(upgraded.get("analysis") or {})
    for stage in ANALYSIS_STAGES:
        if stage == "chords":
            merged = {**_empty_chords_stage(), **(analysis.get(stage) or {})}
            if merged["detected"] is None and merged["value"] is not None:
                # v2 -> v3 migration: best-effort backfill, see module docstring.
                # Assume `detected` was last computed alongside `value`, so it
                # inherits `value`'s hash pair rather than starting stale.
                merged["detected"] = copy.deepcopy(merged["value"])
                merged["detected_input_hash"] = merged["input_hash"]
                merged["detected_settings_hash"] = merged["settings_hash"]
            analysis[stage] = merged
        else:
            analysis[stage] = {**_empty_stage(), **(analysis.get(stage) or {})}
    analysis.setdefault("provenance", {"tool": "vgt", "version": None, "settings": {}})
    upgraded["analysis"] = analysis
    return upgraded


def write_sidecar(project_path: str | Path, data: dict[str, Any]) -> None:
    path = sidecar_path(project_path)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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
    }
