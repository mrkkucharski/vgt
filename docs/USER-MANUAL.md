# vgt user manual

vgt prepares an existing REAPER project for guitar practice. It analyzes one
reference mix, optionally separates practice stems through LALAL.AI, and adds
only its own `[vgt]` objects to the project. This is the current user-visible
contract and a compact regression checklist.

## Quick workflow

1. Save the target `.RPP` in REAPER 7.x.
2. In REAPER's Action List, load and run `reascript/vgt_initialize.lua`.
   On first use choose a file-backed reference track and declare whether its
   guitar is electric or acoustic. This writes an adjacent `Song.vgt` sidecar.
3. Run `vgt analyze "Song.RPP"` in a terminal.
4. Run `vgt_initialize.lua` again to apply analysis and import available stems.
5. Correct chords and sections in REAPER, then run `reascript/vgt_sync.lua`.
6. Inspect persisted state with `vgt status "Song.RPP"` or `--json`.

`vgt [project.rpp]` and `vgt inspect [project.rpp]` are read-only. Without a
path, vgt uses the only `.RPP` in the current directory and refuses to guess
when there are zero or multiple candidates. `vgt apply` and `vgt sync` point to
the required REAPER actions; they never text-edit an RPP.

## Objects vgt creates in REAPER

Original tracks, items, and regions are never renamed, deleted, or changed.
vgt creates a `[vgt] <reference name>` container and may add:

| Object | When | State and purpose |
| --- | --- | --- |
| `[vgt] <reference name>` | Initialization | Folder when it has children; otherwise a plain track. |
| `[vgt] Chords` | Chord analysis | Unmuted but silent text-item track; chord items are unlocked for editing. |
| `[vgt] Beats` | Existing/human-edited tempo map | Unmuted, silent text-item track; beat items are locked. |
| `[vgt] Click` | Tempo-click artifact exists | Muted audio track; unmute temporarily to check the beat grid. |
| Vocals, Instrumental, Bass, Drums, Guitar, Backing | Standard separation | Unmuted, time-based audio tracks. |
| Strings, Keys / Piano | Explicitly requested | Unmuted, time-based optional stem tracks. |
| `[vgt]` section regions | Section analysis | Movable and renamable section markers. |

Chords and Beats are unmuted so labels stay visible, but contain no audible
media. Click is muted because it is audible media.

## Analysis, verification, and corrections

`vgt analyze` detects tempo/beat grid, key, sections, and beat-aligned
major/minor chords. Once available, instrumental, guitar, and backing stems
also inform chord detection.

Artifacts live under `vgt/<stable-id>/` beside the project:

- `tempo-click.wav` — compare by unmuting `[vgt] Click`.
- `chords.txt` — effective chord list.
- `sections.txt` — effective section timeline.

vgt writes a tempo map only when the project still has REAPER's default 120
BPM, 4/4 map. It never overwrites another map or a human-edited vgt map; it
uses `[vgt] Beats` instead. It refreshes a vgt map only when it can prove that
the map is still untouched.

To correct chords, edit `[vgt] Chords` items: rename the take, move/resize,
split, delete, or add items with a take name. Rename or move only vgt-created
section regions. Then run `reascript/vgt_sync.lua`. Sync preserves the
machine-detected chord/section baselines while saving the effective edited
values as human-verified. Re-applying before sync discards unsynchronized edits
to vgt-managed objects.

Tempo and key do not yet have a REAPER correction action. To override either,
edit its sidecar `value` deliberately and set `human_verified` to `true`; keep
a copy of the sidecar first.

## Stem separation and cost controls

Stem separation uses LALAL.AI API v1 only. Set `LALAL_LICENSE_KEY` in your
shell or secret manager; never put it in a sidecar, command, fixture, or log.
Offline separation is not available.

By default, `vgt analyze` saves local analysis and attempts missing standard
LALAL work. Use `--no-stems` for a guaranteed free, mix-only run. The standard
recipe has five paid operations and six retained artifacts:

| Source split | Kept artifact(s) |
| --- | --- |
| Original mix: vocals | `vocals`, `instrumental` |
| Original mix: bass | `bass` |
| Original mix: drums | `drums` |
| Original mix: declared electric/acoustic guitar | `guitar` |
| `instrumental`: declared electric/acoustic guitar | `backing` (no guitar) |

Only the final backing operation is a deliberate cascade. All reference stems
come from the original mix.

```sh
vgt analyze --guitar electric "Song.RPP"
vgt analyze --guitar electric --extra-stem strings --extra-stem keys \
  --accept-stem-cost "Song.RPP"
```

`keys` and `keys/piano` are aliases for `piano`; optional stems are never part
of the standard recipe. `--force` refreshes local analysis only and never
spends credits. `--force-stems` deliberately repeats paid work and shows the
outstanding operation count, balance, and duration estimate before confirmation.
Non-interactive paid refreshes and optional stems require `--accept-stem-cost`.

vgt checkpoints operations, validates WAVs before caching them, and resumes
known interrupted work rather than intentionally submitting it again. Generated
media stays under `<project-folder>/vgt/<stable-id>/`, so moving the project
folder keeps it portable.

## Permanent regression contract

- vgt changes only objects it created and recorded as `[vgt]`-managed.
- Re-running initialization or analysis creates no duplicate managed tracks,
  regions, or stems.
- Project mutation uses REAPER's API, never RPP text editing.
- Heavy analysis runs in the CLI, not inside REAPER.
- vgt-owned audio is time-based and does not stretch with tempo-map changes.
- Human-synchronized chord and section corrections survive analysis and apply.
- Ordinary `--force` makes no LALAL charges; paid work is cached, checkpointed,
  and explicitly confirmed when forced or optional.
- `vgt status` is read-only and never reveals the license key.

For repository checks, run `uv run pytest -q`.
