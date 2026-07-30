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
               "event_count": int | null, "instrument_counts": dict | null,
               "first_note_s": float | null, "last_note_s": float | null,
               "first_event_s": float | null, "last_event_s": float | null,
               "backend_tempo": float | null, "midi_tempo": float | null,
               "confidence": float | null, "settings": dict, # backend-specific settings
               "transcribed_at": str | null, "error": str | null,
             }, ...
           },
         }
       Older sidecars migrate to `requested_targets: ["guitar"]` and an empty
       `targets` index -- no data migration, since there is nothing to migrate.
  10 -- `analysis.transcription.modes` stores target -> named transcription
      profile selections. A v9 `stems.guitar_type: acoustic` migrates to
      `{"guitar": "guitar-acoustic"}` so its transcription settings identity
      remains unchanged. `stems.guitar_type` still controls LALAL separation.
 11 -- Tempo values gain `downbeat_detected`: false means the detected beat
      grid has no known bar phase (the librosa fallback), so its first beat
      must not anchor a REAPER tempo map. Existing librosa values migrate to
      false; other legacy values retain their previous downbeat behavior.
 12 -- A top-level `generation` counter is bumped by every writer on every
      commit (Python and both ReaScript actions). It is the shared sidecar
      commit protocol's conflict signal (#138): neither ReaScript action can
      take Python's `fcntl` lock, so each instead re-reads the sidecar as late
      as possible -- immediately before its final write -- merges its own
      change onto whatever is currently on disk, and re-checks `generation`
      one last time right before the atomic rename. A mismatch there means
      another writer committed in the gap; the ReaScript action discards its
      temp file and retries the whole read-merge-write against the new
      latest state, up to a bounded number of attempts, rather than ever
      renaming a stale merge over a newer commit. Older sidecars migrate with
      `generation: 0`.
 13 -- Foundation for multiple transcription variants per target (#148, see
      docs/transcription-variants-plan.md). Each `analysis.transcription.
      targets[target]` entry gains `variants` (an index of retained
      candidates keyed by an opaque id), `variant_order` (presentation
      order; never rely on dict/JSON ordering), `selected_variant_id`, and
      `discarded_variants` (a compact audit list, empty until a later issue
      implements discard). `analysis.transcription` also gains
      `detection_cache`, the raw-Basic-Pitch-detection cache index keyed by
      `detection_hash` (empty until a later issue's two-level cache writes
      into it).

      This migration is additive, not a replacement: the pre-v13 flat
      per-target fields (`status`, `midi_file`, `settings_hash`, ...) are
      left exactly as they were, because `analysis.py`'s single-variant
      write/read path (`_refresh_target`) is unchanged by this issue --
      only schema/profile plumbing is in scope, not the multi-variant
      execution/selection flow (a later issue). `upgrade()` derives one
      `variants` entry per target from those flat fields on every read, so
      the migration is naturally idempotent: rerunning it over unchanged
      flat fields always regenerates the same id/content, and a legitimate
      re-transcription (which replaces the flat fields) simply produces a
      different derived variant on the next read, matching the flat state
      it was derived from. No artifact is moved or renamed by this
      migration -- the flat fields' existing `transcription/<target>.mid`
      style paths are preserved verbatim as the migrated variant's own
      paths (see the plan's "Preserve existing artifact paths ... as legacy
      paths until that variant is next recomputed").

      A legacy variant's id is `sha256(f"legacy:{target}:{settings_hash}")`
      truncated to 8 hex characters, so it stays stable across repeated
      upgrades of the same underlying record. `variant_order` is always
      `[id]`. The migrated variant's `requested_profile`/`effective_profile`
      come from mapping the legacy `analysis.transcription.modes[target]`
      selection through `vgt.transcribe.effective_profile_name_for_target`
      (migration step 6); for a Basic Pitch target this is also resolved
      through `vgt.transcription_profiles` to populate `resolved_settings`
      and the detection/cleanup hashes a genuinely profile-driven variant
      would carry. `profile_definition_hash` and `raw_notes_hash` are
      `None`: a legacy record was never resolved against a project profile
      file and has no raw-detection cache entry.

      Superseded by schema 14: this described `selected_variant_id`, which
      schema 14 removes; the shape above otherwise still applies.
 14 -- Selection removed (#176): retained transcription variants are peers,
      ordered only for stable presentation, with none designated preferred,
      active, best, or selected. `analysis.transcription.targets[target]`
      no longer carries `selected_variant_id` in any form -- not renamed,
      not replaced by a first-candidate default. Upgrading a schema-13
      sidecar drops that one field losslessly: every variant, its immutable
      id, `variant_order`, labels, recipes, metrics, paths, detection cache
      references, and `discarded_variants` audit records are preserved
      exactly. The upgrade never reruns transcription and never moves or
      deletes an artifact. Repeating the upgrade over an already-migrated
      sidecar is a no-op: the field is simply absent on every subsequent
      read.
 15 -- The `key` stage gains the same `detected`/`value` split as chords and
      sections. This is a lossless migration: existing `value` is copied to
      `detected` with its existing input/settings hashes, so a later REAPER
      correction can freeze the effective value while analysis keeps the
      machine baseline current.
 16 -- The `tempo` stage gains that detected/value split too.  The dedicated
      REAPER tempo-map sync action writes only `value` and marks it
      human-verified; `detected` retains the audio-derived tempo and beat
      grid as a separately refreshable baseline.
 17 -- `chords` and `key` return to ordinary cached stages. Their former
      human-verification sidecar fields are removed: corrected tracks now
      live durably in the `[clean]` working-copy area. Existing `value`s are
      retained and their detected-baseline fields are discarded.

Ordinary stages (`key` and `chords`) have this shape:
  {
    "value": <detector output, or null if never run>,
    "input_hash": str | null, # hash of the analyzed audio at last (re)compute
    "settings_hash": str | null,
    "analyzed_at": str | null, # UTC time at which this value was detected
  }

The human-correctable `tempo` and `sections` stages additionally carry
`human_verified` and `verified_at`. They also carry:
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
import hashlib
import json
import os
import shutil
import tempfile
import uuid

from . import transcription_profiles
from .transcribe import effective_profile_name_for_target

SCHEMA_VERSION = 17
STEMS_LEASE_TIMEOUT = timedelta(minutes=30)

ANALYSIS_STAGES = ("tempo", "key", "sections", "chords", "transcription")

# Stages that carry the detected/value split (#19): a human correction to
# `value` never overwrites the pristine machine detection kept in `detected`.
DETECTED_SPLIT_STAGES = ("tempo", "sections")

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
        "input_hash": None,
        "settings_hash": None,
        "analyzed_at": None,
    }


def _empty_verified_stage() -> dict[str, Any]:
    return {
        **_empty_stage(),
        "human_verified": False,
        "verified_at": None,
    }


def _empty_detected_split_stage() -> dict[str, Any]:
    return {**_empty_verified_stage(), "detected": None, "detected_input_hash": None, "detected_settings_hash": None}


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
    return {"requested_targets": list(DEFAULT_TRANSCRIPTION_TARGETS), "modes": {}, "targets": {}, "detection_cache": {}}


def _legacy_variant_id(target: str, settings_hash: str | None) -> str:
    """A stable id for the single variant a pre-v13 flat target record
    implies. Deterministic in `target` + `settings_hash` so repeated
    upgrades of the same underlying record -- or of an unchanged record
    across separate reads -- always derive the same id (see schema v13's
    module-docstring note)."""
    return hashlib.sha256(f"legacy:{target}:{settings_hash}".encode("utf-8")).hexdigest()[:8]


def _legacy_variant_resolved_settings(profile_name: str) -> tuple[dict[str, Any], str | None, str | None]:
    """Best-effort `resolved_settings`/detection+cleanup hashes for a legacy
    record's effective profile. Only meaningful for a Basic Pitch profile;
    DrumScript and any unrecognised legacy profile name resolve to an empty
    snapshot rather than failing the whole sidecar upgrade."""
    try:
        resolved = transcription_profiles.resolve_profile(profile_name)
    except transcription_profiles.ProfileDefinitionError:
        return {"detection": {}, "cleanup": []}, None, None
    if resolved.backend != "basic-pitch":
        return {"detection": {}, "cleanup": []}, None, None
    return (
        transcription_profiles.resolved_settings_snapshot(resolved),
        transcription_profiles.resolved_detection_hash(resolved),
        transcription_profiles.resolved_cleanup_hash(resolved),
    )


# The pre-v13 flat per-target fields `analysis.py`'s `_refresh_target`
# (via `transcribe.missing_source_entry`/`transcribed_entry`/`error_entry`)
# still writes today. Carried verbatim onto the derived legacy variant so no
# field is lost in the migration.
_LEGACY_TARGET_VARIANT_FIELDS: tuple[str, ...] = (
    "backend", "package_pin", "serialization", "source_role", "input_hash", "settings_hash",
    "status", "midi_file", "notes_file", "events_file", "note_count", "event_count",
    "instrument_counts", "pitch_range_midi", "first_note_s", "last_note_s", "first_event_s",
    "last_event_s", "backend_tempo", "midi_tempo", "confidence", "settings",
    "transcribed_at", "error",
)


def migrate_transcription_target(target: str, record: dict[str, Any], modes: dict[str, str]) -> dict[str, Any]:
    """Augment one flat pre-v13 `targets[target]` record with the schema
    v13 `variants` view, leaving the flat fields themselves untouched (see
    schema v13's module-docstring note for why this is additive, not a
    replacement).

    A record that has no top-level `status` is not a legacy flat record --
    either a full multi-variant writer (see `vgt.transcription_lifecycle`,
    issue #150) already produced the plan's variants-only shape, or the
    record is otherwise malformed -- so it is passed through unchanged
    rather than guessed at.

    Public (not `upgrade()`-only) because `vgt.transcription_lifecycle`'s
    variant operations must materialize this same view once, eagerly, before
    mutating a target that a pre-v13 `--transcribe`/`--mode` run last wrote,
    so a `variant add` onto an old flat record starts from its migrated
    variant rather than clobbering it.
    """
    if "status" not in record:
        return record

    migrated = dict(record)
    settings_hash = migrated.get("settings_hash")
    variant_id = _legacy_variant_id(target, settings_hash)
    effective_profile = effective_profile_name_for_target(target, modes)
    resolved_settings, detection_hash, cleanup_hash = _legacy_variant_resolved_settings(effective_profile)

    variant: dict[str, Any] = {
        "label": "default",
        "requested_profile": effective_profile,
        "profile_definition_hash": None,
        "effective_profile": effective_profile,
        "raw_notes_hash": None,
        "detection_hash": detection_hash,
        "cleanup_hash": cleanup_hash,
        "resolved_settings": resolved_settings,
    }
    for key in _LEGACY_TARGET_VARIANT_FIELDS:
        variant[key] = migrated.get(key)

    migrated["variants"] = {variant_id: variant}
    migrated["variant_order"] = [variant_id]
    migrated["discarded_variants"] = list(migrated.get("discarded_variants") or [])
    return migrated


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
    generation = upgraded.get("generation")
    upgraded["generation"] = generation if isinstance(generation, int) else 0
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
            merged = {**_empty_stage(), **(analysis.get(stage) or {})}
            if stage in ("chords", "key"):
                for legacy_field in (
                    "detected", "detected_input_hash", "detected_settings_hash",
                    "human_verified", "verified_at",
                ):
                    merged.pop(legacy_field, None)
            analysis[stage] = merged
    tempo_value = analysis["tempo"].get("value")
    if isinstance(tempo_value, dict) and "downbeat_detected" not in tempo_value:
        # Historical librosa payloads used their first beat as a fabricated
        # offset. Other legacy backends did not persist enough information to
        # safely distinguish their prior downbeat semantics, so retain them.
        tempo_value["downbeat_detected"] = tempo_value.get("backend") != "librosa"
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
    modes = transcription.get("modes")
    transcription["modes"] = dict(modes) if isinstance(modes, dict) else {}
    # This is a migration compatibility bridge, not a new coupling: the
    # declaration continues to choose LALAL's split target in separation.py.
    if stems.get("guitar_type") == "acoustic" and "guitar" not in transcription["modes"]:
        transcription["modes"]["guitar"] = "guitar-acoustic"
    detection_cache = transcription.get("detection_cache")
    transcription["detection_cache"] = dict(detection_cache) if isinstance(detection_cache, dict) else {}
    targets = {
        target: migrate_transcription_target(target, record, transcription["modes"])
        for target, record in transcription["targets"].items()
        if isinstance(record, dict)
    }
    # Schema 14 (#176) removes selection: a pre-14 sidecar's target record --
    # legacy flat or already variants-only -- may still carry the retired
    # `selected_variant_id` pointer. Drop it losslessly; every other field
    # (variants, variant_order, discarded_variants, ...) passes through
    # untouched.
    for record in targets.values():
        record.pop("selected_variant_id", None)
    transcription["targets"] = targets
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

    Every commit bumps the top-level ``generation`` counter -- the same
    conflict signal the ReaScript actions check before their own commit (see
    the module docstring, schema 12) -- so a concurrent ReaScript writer can
    tell its merge went stale even though it cannot take this lock itself.
    """
    path = sidecar_path(project_path)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_sidecar_unlocked(project_path)
            update(data)
            data["generation"] = int(data.get("generation") or 0) + 1
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
    """True if the audio and settings that produced a cached value are unchanged."""
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
    """Recompute an ordinary cached stage when its inputs or settings change."""
    if not force and stage_is_current(stage, input_hash=input_hash, settings_hash=settings_hash):
        return stage
    return {
        "value": compute(),
        "input_hash": input_hash,
        "settings_hash": settings_hash,
        "analyzed_at": analyzed_at,
    }
