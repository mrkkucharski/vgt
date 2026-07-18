# vgt

Phase 0 of virtual guitar teacher: safe REAPER project plumbing. It reads an RPP project from the command line and uses a REAPER ReaScript action to add a small, vgt-owned practice area.

## Install and inspect

With Python 3.11 and `uv`:

```sh
uv tool install .
vgt "test/Reaper Project/Reaper Project.RPP"
```

`vgt [project.rpp]` (or the explicit `vgt inspect [project.rpp]`) accepts an explicit `.RPP` path. Without one it selects the only `.RPP` in the current directory; it refuses to guess if there are zero or multiple projects. It reports the project's sample rate, tempo/time signature, and track names/GUIDs.

## Apply inside REAPER

Open and save the target project in REAPER 7.x. In **Actions → Show action list → ReaScript: Load**, load [reascript/vgt_initialize.lua](reascript/vgt_initialize.lua), then run it.

The action is intentionally the mutation path: it uses REAPER's API and never text-edits an RPP. It:

- pops up a menu of the project's own (non-`[vgt]`) tracks and asks which one is the **reference** to mirror;
- creates a REAPER folder named `[vgt] <reference track name>` (e.g. `[vgt] The Seven Rivers (Full March - 3_00)`) with a `[vgt] Mirror` child;
- clones only the chosen reference track's file-backed media to the mirror track, without adding sends, changing source tracks, or changing project tempo/sample rate;
- writes the adjacent `.vgt` sidecar — named after the project, e.g. `Reaper Project.vgt` — with its two created track GUIDs and config recording the reference track;
- on re-apply, deletes only tracks whose GUID occurs in the sidecar **and** whose current name begins `[vgt]`, then recreates the same area, carrying forward any `analysis` block `vgt analyze` had already written (schema stays version 1 until analysis exists, then becomes version 2).

The folder/mirror are therefore idempotent and original tracks remain untouched. REAPER-native click-source items have no file to clone, so choosing a click track mirrors nothing. `vgt apply` validates the project path and directs you to this action rather than silently falling back to unsafe RPP editing.

Automation can skip the menu by setting the `vgt`/`reference_index` ExtState (a 0-based index over the non-`[vgt]` tracks) before running the action; interactive users are always prompted.

Use a copy of any project when experimenting; the included `test/Reaper Project` fixture is read-mostly.

For a repeatable live-REAPER run against a disposable copy of that fixture, see
[Phase 0 live verification](docs/phase0-live-verification.md). The verifier
checks the saved RPP and sidecar after both the first apply and re-apply.

## Analyze the reference track (Phase 1)

`vgt analyze [project.rpp]` requires a `.vgt` sidecar already written by the
apply action above. It resolves the reference track's source audio file,
runs the tempo/key/sections/chords detectors, and writes the results back
into the sidecar as schema version 2 — never inside REAPER itself.

Each detector's output is cached against a hash of the source audio and the
detector's settings, so re-running only recomputes stages whose inputs
changed. Any stage can be corrected by hand-editing its `value` in the
sidecar and setting `human_verified: true`; `vgt analyze` then leaves that
stage untouched on every later run, regardless of what the audio hash says.

### Tempo & beat/downbeat grid

The `tempo` stage detects BPM, downbeat offset, and time signature from the
reference track's full mix, and fits either a single constant tempo or a
piecewise-linear tempo map (mode + residual are both recorded):

- **Primary backend: madmom**'s DBN beat + downbeat trackers, installed via
  the optional `madmom` extra (`uv sync --extra madmom`). madmom's last
  release predates modern Python/NumPy and its build is fragile (Cython,
  a legacy NumPy ABI, a deprecated `pkg_resources` import) — it is isolated
  behind this extra precisely so a default install never needs it.
- **Fallback: librosa's `beat_track`** (installed by default) when madmom
  isn't installed, or fails to import. It gives beat times but no
  downbeats, so time signature falls back to a `time_signature_hint`
  setting (or `4/4`) rather than a detected bar length.

Every run also renders a **click-over-mix artifact** — `<project>.vgt-tempo-click.wav`,
next to the sidecar — so the detected grid can be checked by ear; it is
vgt-owned and regenerated each run, not committed alongside the project.

## Apply analysis (Phase 1)

Run the same ReaScript again after `vgt analyze`. It reads the sidecar and adds
`[vgt]` section regions plus a muted, locked `[vgt] Chords` track whose text
items carry the detected beat-aligned labels. It sets vgt-owned audio items to
time-based positioning before any tempo changes.

When REAPER still has exactly its single default 120 BPM / 4/4 tempo marker,
the action writes the detected tempo map. If the project already has any other
tempo map, it leaves that map untouched and instead creates a muted, locked
`[vgt] Beats` item track. Both the regions and vgt tracks are reconciled on
re-apply; no non-`[vgt]` track, region, or item is changed.

For a saved-project check after analysis, use:

```sh
uv run python scripts/verify_phase1_apply.py "$PROJECT" \
  --baseline "test/Reaper Project/Reaper Project.RPP"
```
