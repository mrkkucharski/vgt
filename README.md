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
- writes the adjacent `.vgt` sidecar — named after the project, e.g. `Reaper Project.vgt` — with schema version 1, its two created track GUIDs, and config recording the reference track;
- on re-apply, deletes only tracks whose GUID occurs in the sidecar **and** whose current name begins `[vgt]`, then recreates the same area.

The folder/mirror are therefore idempotent and original tracks remain untouched. REAPER-native click-source items have no file to clone, so choosing a click track mirrors nothing. `vgt apply` validates the project path and directs you to this action rather than silently falling back to unsafe RPP editing.

Automation can skip the menu by setting the `vgt`/`reference_index` ExtState (a 0-based index over the non-`[vgt]` tracks) before running the action; interactive users are always prompted.

Use a copy of any project when experimenting; the included `test/Reaper Project` fixture is read-mostly.

For a repeatable live-REAPER run against a disposable copy of that fixture, see
[Phase 0 live verification](docs/phase0-live-verification.md). The verifier
checks the saved RPP and sidecar after both the first apply and re-apply.
