"""Read/write the `<project>.vgt` sidecar: the shared state contract between
the Phase 0 ReaScript apply action and the Phase 1+ Python analysis CLI.

Schema versions:
  1 -- Phase 0: `managed_track_guids` + `config` (written by the ReaScript action).
  2 -- Phase 1 adds `analysis`: one entry per detector (tempo/key/sections/chords),
       each cached on an input+settings hash so re-running only recomputes stages
       whose inputs changed, and a `provenance` block recording the tool/version.

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
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import json

SCHEMA_VERSION = 2

ANALYSIS_STAGES = ("tempo", "key", "sections", "chords")


class SidecarError(ValueError):
    """The sidecar file is missing or does not contain the data we need."""


def sidecar_path(project_path: str | Path) -> Path:
    path = Path(project_path)
    return path.with_suffix(".vgt")


def _empty_stage() -> dict[str, Any]:
    return {"value": None, "human_verified": False, "input_hash": None, "settings_hash": None}


def read_sidecar(project_path: str | Path) -> dict[str, Any]:
    """Read the sidecar for `project_path`, upgrading schema v1 to v2 in memory."""
    path = sidecar_path(project_path)
    if not path.is_file():
        raise SidecarError(f"No .vgt sidecar found at {path}; run the Phase 0 apply action first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return upgrade(data)


def upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """Return `data` with all Phase 0 fields intact and a v2 `analysis` block present."""
    upgraded = dict(data)
    upgraded["schema_version"] = SCHEMA_VERSION
    analysis = dict(upgraded.get("analysis") or {})
    for stage in ANALYSIS_STAGES:
        analysis[stage] = {**_empty_stage(), **(analysis.get(stage) or {})}
    analysis.setdefault("provenance", {"tool": "vgt", "version": None, "settings": {}})
    upgraded["analysis"] = analysis
    return upgraded


def write_sidecar(project_path: str | Path, data: dict[str, Any]) -> None:
    path = sidecar_path(project_path)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def refresh_stage(
    stage: dict[str, Any],
    *,
    input_hash: str,
    settings_hash: str,
    compute: Callable[[], Any],
) -> dict[str, Any]:
    """Recompute a stage's cached value unless a human has verified it, or the
    inputs/settings that produced the cached value haven't changed."""
    if stage.get("human_verified"):
        return stage
    if stage.get("input_hash") == input_hash and stage.get("settings_hash") == settings_hash:
        return stage
    return {
        "value": compute(),
        "human_verified": False,
        "input_hash": input_hash,
        "settings_hash": settings_hash,
    }
