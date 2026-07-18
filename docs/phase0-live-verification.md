# Phase 0 live REAPER verification

This procedure proves the persisted result of the ReaScript, rather than only
inspecting its Lua source. It uses a disposable copy and leaves the committed
fixture untouched.

## Prerequisites

- macOS with REAPER 7.x at `/Applications/REAPER.app`.
- Python 3.11 and `uv`.
- REAPER may already be open; the command below starts an isolated REAPER
  instance with `-newinst`.

## Run

From the repository root, make a temporary copy that preserves the relative
`Media/` directory. The helper save script is deliberately outside the repo;
it makes REAPER save after the apply action.

```sh
RUN_DIR="$(mktemp -d /tmp/vgt-phase0.XXXXXX)"
cp -R "test/Seven Rivers" "$RUN_DIR/Seven Rivers"
PROJECT="$RUN_DIR/Seven Rivers/Seven Rivers.RPP"
SAVE_SCRIPT="$RUN_DIR/save.lua"
printf '%s\n' 'reaper.Main_SaveProject(0, false)' > "$SAVE_SCRIPT"

/Applications/REAPER.app/Contents/MacOS/REAPER -newinst \
  "$PROJECT" "$PWD/reascript/vgt_phase0_apply.lua" "$SAVE_SCRIPT"
uv run python scripts/verify_phase0_apply.py "$PROJECT" \
  --baseline "test/Seven Rivers/Seven Rivers.RPP"

# Repeat the exact apply/save sequence, then check it again for idempotency.
/Applications/REAPER.app/Contents/MacOS/REAPER -newinst \
  "$PROJECT" "$PWD/reascript/vgt_phase0_apply.lua" "$SAVE_SCRIPT"
uv run python scripts/verify_phase0_apply.py "$PROJECT" \
  --baseline "test/Seven Rivers/Seven Rivers.RPP"
```

REAPER runs command-line project and script arguments in order; its bundled Lua
runtime needs no external scripting dependency. The verifier reads only the
saved `.RPP` and adjacent `vgt.json`. On each pass it checks:

- the original four name/GUID pairs and project sample rate, tempo, and time
  signature match the baseline;
- `vgt.json` has schema version 1, exactly two distinct managed GUIDs, and the
  expected config;
- the sidecar GUIDs are exactly the two `[vgt]` tracks: one `Practice` folder
  with depth `+1`, followed by one `Mirror` child with depth `-1`;
- the mirror has exactly the source project's file-backed media items, all of
  which resolve on disk. Thus the second pass also rejects duplicate managed
  tracks or items.

## Recorded run

On 2026-07-18, REAPER 7.77 on macOS ran the two command-line applies above
against a fresh temporary copy. Both verifier passes returned zero and reported
four original tracks, two managed GUIDs, and three mirrored file-backed items.
The first and second pass used different managed GUIDs (expected because
re-apply deletes only the sidecar-listed, still `[vgt]`-prefixed tracks before
recreating them); each saved sidecar exactly matched the newly created pair.
