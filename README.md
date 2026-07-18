# vgt

Phase 0 of virtual guitar teacher: safe REAPER project plumbing. It reads an RPP project from the command line and uses a REAPER ReaScript action to add a small, vgt-owned practice area.

## Install and inspect

With Python 3.11 and `uv`:

```sh
uv tool install .
vgt "test/Seven Rivers/Seven Rivers.RPP"
```

`vgt [project.rpp]` (or the explicit `vgt inspect [project.rpp]`) accepts an explicit `.RPP` path. Without one it selects the only `.RPP` in the current directory; it refuses to guess if there are zero or multiple projects. It reports the project's sample rate, tempo/time signature, and track names/GUIDs.

## Apply inside REAPER

Open and save the target project in REAPER 7.x. In **Actions → Show action list → ReaScript: Load**, load [reascript/vgt_phase0_apply.lua](reascript/vgt_phase0_apply.lua), then run it.

The action is intentionally the mutation path: it uses REAPER's API and never text-edits an RPP. It:

- creates `[vgt] Practice`, a REAPER folder, and its `[vgt] Mirror` child;
- clones file-backed source media to the mirror track without adding sends, changing source tracks, or changing project tempo/sample rate;
- writes the adjacent `vgt.json` sidecar with schema version 1, its two created track GUIDs, and minimal config;
- on re-apply, deletes only tracks whose GUID occurs in `vgt.json` **and** whose current name begins `[vgt]`, then recreates the same area.

The folder/mirror are therefore idempotent and original tracks remain untouched. REAPER-native click-source items have no file to clone and are deliberately skipped; the fixture's three audio stems are mirrored. `vgt apply` validates the project path and directs you to this action rather than silently falling back to unsafe RPP editing.

Use a copy of any project when experimenting; the included `test/Seven Rivers` fixture is read-mostly.
