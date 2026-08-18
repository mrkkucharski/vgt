# On-demand single-track MT3 transcription — design plan

Status: implemented 2026-08-17 (waves 1-3 below), directly in an interactive
session rather than through the issue-tracked workflow, then verified live
end-to-end against a real project the same day. All offline-testable pieces
have passing tests (`uv run pytest`).

Fixed during live verification (each is now covered by a regression test):
`reaper.ExecProcess` never actually detached a process on a real install
(silently unreachable fallback logic) — spawn now uses `os.execute` alone;
`python -m vgt` failed outright (`src/vgt/__main__.py` didn't exist); `uv`
itself was unreachable from a REAPER-spawned process (bare `"uv"`, no `PATH`
in a GUI app's environment) — see `mt3_provision.resolve_uv_executable`;
`RENDER_FORMAT` was truncated to 4 bytes instead of REAPER's real 7-byte
value, silently producing correctly-sized but exactly-silent WAV renders;
the render-validation silence check (`reaper.CreateAudioAccessor`) never
once refused a genuinely silent render — replaced with a self-contained WAV
parser with no REAPER audio API dependency at all. `RENDER_SETTINGS=3`,
`RENDER_BOUNDSFLAG=0`, and action id `42230` were all confirmed correct as
originally guessed. Also added: a refusal when the selected track is
muted (a muted track renders as correct-but-confusing silence under "stems
via master"), and the imported result track is now named after its source
verbatim + " (MT3)" rather than always re-prefixed `[vgt]` (see
`track_job_name` in vgt_common.lua) — a deliberate deviation from this
plan's original §9 wording, made after seeing the `[vgt]`-forced name in
practice.

## Goal

Today MT3 transcription ("mode 2" from the current docs) only runs against
vgt's own fixed instrument **targets** (`guitar`, `bass`, ...), each backed by
a stem file that LALAL separation already produced. This plan adds a second,
independent entry point:

1. The user selects **any track** in the open REAPER project (a separated
   stem, a raw recording, a working-copy edit — anything with audio).
2. A ReaScript action renders just that track's audio and spawns an MT3
   transcription job on it, in the background, without blocking REAPER.
3. MT3 is forced (via the new upstream `--force-program` flag, see below) to
   decode everything onto **one** instrument, so the job produces a single
   clean MIDI track instead of a multi-instrument dump the user has to sift
   through.
4. The result is imported back as a `[vgt]`-owned MIDI track once ready, at
   the project's own tempo, so it drops in next to the source track with no
   further alignment work — and the user is notified.

This is a genuinely new capability, not a variant of the existing target
system: there is no "target" here, no pre-existing stem, and (for the first
time) a transcription job that outlives the CLI invocation that started it.

## Current behavior and the gap

Everything MT3-related in vgt today assumes a fixed target:

- `refresh_mt3_instrumental_review()` (`src/vgt/analysis.py:592`) transcribes
  the `instrumental` stem specifically, keeping every predicted track as an
  unfiltered review dump — a `[vgt] MT3` folder built by
  `add_mt3_review_folder()` in `reascript/vgt_initialize.lua:1342`.
- `guitar-mt3`/`bass-mt3` profiles (`src/vgt/transcribe.py:604-631`) drive
  `Mt3Transcriber.detect_raw()`/`.transcribe()` for one of `VALID_TARGETS`,
  selecting a single dominant track via
  `mt3_normalize.select_dominant_musical_track()` and writing it into the
  ordinary per-target/variant artifact and import machinery
  (`transcription/<target>/<variant>.mid`, `transcription_lifecycle.py`,
  `vgt transcription variant add`).

Both paths need audio that already exists as a named stem file, and both run
**synchronously** inside a `vgt` CLI invocation the user (or `analyze`) runs
from a terminal. Neither has any notion of "this arbitrary track, right now,
in the background, with a result I'll notice later." That's the entire gap
this plan closes.

It also depends on a capability MT3 didn't have until now:
`--force-program`, merged into the pinned `mt3` fork's `main` at
`1e5d143a8c2d33d1845df7f05b9bef7246ad1b2e`. It pins every decoded note onto
one GM program and ignores the model's own program-change predictions —
exactly what turns MT3's normal multi-instrument dump into a single usable
track for an arbitrary selected source. See that repo's `mt3/cli.py`,
`mt3/transcription.py`, and `mt3/note_sequences.py` for the implementation.

## Desired user-visible behavior

**Trigger.** User selects one track (e.g. `[vgt] Guitar (stem)`, or any other
track) and runs a new REAPER action, "vgt: Transcribe selected track (MT3)."
A small dialog (`reaper.GetUserInputs`) asks for the target GM program,
pre-filled with a best-effort guess from the track name (see "Program
resolution" below) and always editable/overridable. The script renders the
track, kicks off a detached background job, and returns control to REAPER
immediately — no blocking, no modal wait.

**Progress.** A second action, "vgt: Get transcription," can be run at
any time and reports each pending job's status (`running` / `done` / `error`)
via `reaper.ShowConsoleMsg` or a small `reaper.MB`. It also *imports* any job
that has finished since the last check. The same logic runs automatically
for a few minutes right after the trigger script starts a job, via a
`reaper.defer()` polling loop, so the common case ("start it, keep working,
get told when it's ready") needs no manual re-checking — see "Notification"
below for why this is a *bounded* defer loop, not an unbounded one. Crucially,
checking and importing is its own small, standalone action — it is not a
side effect of `vgt apply`, does not run `vgt apply`'s reconciliation pass,
and touches nothing beyond the one (or few) jobs it finds ready. This was an
explicit requirement; see "Independence from `vgt apply`" under Architecture.

**Result.** A finished job appears as one new `[vgt] <label> (MT3)` MIDI
track, positioned to match the source track, authored at the analyzed
project tempo (so it drops in aligned, exactly like every other `[vgt]`
reference/variant MIDI track already does), and the user gets an OS-level
notification even if they've closed REAPER or switched apps.

## Architecture

### 1. Upstream MT3: repin, and thread `force_program` through vgt

`src/vgt/mt3_provision.py:43-62` pins `MT3_PINNED_TAG = "main"` at commit
`d937756...`, predating `--force-program`. This plan requires:

- Bumping `MT3_PINNED_COMMIT` to `1e5d143a8c2d33d1845df7f05b9bef7246ad1b2e`
  (or a later stable point once the fork tags a release), and recomputing
  `MT3_LOCK_SHA256` per the `gh api .../uv.lock?ref=...` recipe already
  documented in that file's comment.
- Adding `force_program: int | None = None` to `Mt3Spec`
  (`src/vgt/transcribe.py:1117`) — optional so every existing spec's
  identity/hash is untouched when it's absent.
- Threading it through `build_mt3_argv()` (`transcribe.py:3487`) as
  `--force-program N` when set, and through `Mt3Transcriber.detect_raw()` /
  `.transcribe_all_tracks()` (`transcribe.py:3578`, `3533`).
- A new, much simpler normalization path alongside
  `mt3_normalize.select_dominant_musical_track()`. That function's whole job
  is picking *one* track out of MT3's ambiguous multi-instrument dump by
  duration and family elimination — machinery this job doesn't need, because
  `force_program` already guarantees every non-drum note shares one program.
  What's still needed: MT3's own MIDI writer still splits notes across
  *rhythm* (guitar-lead vs. guitar-rhythm; the pinned checkpoint is
  `guitar-pilot-it3-4s`, a guitar-specialized model that predicts this
  distinction independently of program — see `mt3_provision.py:68`), so a
  forced-program file can still be two MIDI tracks. Add
  `mt3_normalize.merge_all_musical_tracks(path)`: concatenate every non-drum
  track's notes (no elimination, no duration comparison — there's nothing
  left to disambiguate) into one `Mt3SelectedTrack`. `ParsedNote` already
  carries no program/rhythm field, so `write_normalized_mt3_artifacts()` is
  reused unchanged once notes are merged this way.

### 2. Job identity and artifact layout

This is not a `target`, so it must not be shoehorned into
`VALID_TARGETS`/`transcription/<target>/...`. New, parallel, per-job layout:

```text
vgt/<namespace>/track-jobs/<job_id>/
  source.wav        # the rendered selected-track audio
  status.json        # {"status": "running"|"done"|"error", ...}
  result.mid          # present once status == "done"
  result.csv
```

`job_id` is a short random/timestamp id (mirrors how variant ids are already
generated in `transcription_lifecycle.py`) — not derived from track GUID or
name, so re-running on the same track twice is just two independent jobs
that both eventually import as separate tracks; nothing here claims a
"latest per track" identity. (An "abandon a job" or "make it exclusive per
track" behavior is explicitly out of scope for v1 — see "Prohibited scope.")

#### Exactly one writer per piece of state

The obvious design — mirror job state into the sidecar as it changes — is
wrong here, and worth stating explicitly so it isn't reintroduced. The
background job outlives the `vgt` process that spawned it, so it would be
writing the sidecar *concurrently with* REAPER actions and other `vgt`
invocations. `atomic_update_sidecar` (`sidecar.py:613`) does take an
`fcntl` lock, and the ReaScript side has its own `generation`-counter
protocol (`sidecar.py:102-113`, schema 12 / #138) — but the safest use of
both is to not need them. So:

- **`status.json` is the single source of truth for in-flight job state**,
  and the background job is its **only** writer. Nothing else ever writes
  it; the ReaScripts only read it. No lock, no merge, no protocol — one
  writer per file, by construction. Written with a temp-file + atomic
  `os.replace`, so a reader never observes a half-written file.
- **The sidecar records only the terminal, *imported* outcome**, written
  once by the import action at the moment it creates the track — a moment
  when REAPER is the only writer anyway. In-flight `running` state never
  reaches the sidecar at all.

```json
"status.json": {
  "status": "running" | "done" | "error",
  "job_id": "...",
  "source_track_name": "...",
  "source_track_guid": "{...}",
  "item_start_s": 0.0,
  "item_end_s": 214.5,
  "requested_program": 25,
  "midi_tempo": 128.4,
  "started_at": "...",
  "finished_at": null,
  "note_count": null,
  "error": null
}
```

`item_start_s`/`item_end_s`/`source_track_guid` are written by the *trigger
script* when it creates the job directory (before spawning), not by the job
— they describe the selection, which only REAPER can see. The job runner
appends only its own outcome fields, so even within `status.json` the two
writers never touch the same keys, and the trigger script has finished
writing before the job process starts.

#### Sidecar block and schema migration

The imported record is a new `analysis["track_jobs"]` block, sibling to
`analysis["mt3_review"]` (`sidecar.py:584`):

```json
"track_jobs": {
  "<job_id>": {
    "status": "imported" | "error",
    "source_track_name": "...",
    "requested_program": 25,
    "midi_tempo": 128.4,
    "midi_file": "track-jobs/<job_id>/result.mid",
    "notes_file": "track-jobs/<job_id>/result.csv",
    "note_count": 812,
    "imported_at": "...",
    "error": null
  }
}
```

This is a schema change and must follow the same discipline every prior
block addition did (schemas 6, 9, 10, 11, 12, 13, 14 are each documented
with a migration): bump `SCHEMA_VERSION` from **18 to 19**
(`sidecar.py:241`), add an `_empty_track_jobs_block()` alongside
`_empty_mt3_review_block()` (`sidecar.py:381`), merge it in the migration
function next to the `mt3_review` line at `sidecar.py:584`, and add a `19 --`
entry to the module docstring. Older sidecars migrate to an empty `{}`.

Because the import action writes this from **Lua**, it must use the shared
sidecar commit protocol, not a naive write: re-read the sidecar as late as
possible, merge onto what is currently on disk, re-check `generation`
immediately before the atomic rename, and retry the whole read-merge-write a
bounded number of times on mismatch (`sidecar.py:102-113`;
`vgt_initialize.lua:436` `read_generation` and its commit helper around
`:1425-1465` are the existing implementation to move into the shared module).
A ReaScript that renames a stale merge over a newer commit is exactly the
bug that protocol exists to prevent.

### 3. ReaScript: select, render, spawn (`vgt_transcribe_track.lua`, new)

1. Validate exactly one track is selected and it is not already a
   `[vgt]`-owned/locked track (reuse the `starts_with_vgt`/lock-checking
   helpers already in `vgt_initialize.lua`, mirrored into the shared
   `vgt_common.lua` module described under "Independence from `vgt apply`"
   below).
2. Prompt for the target GM program via `reaper.GetUserInputs`, pre-filled
   per "Program resolution" below.
3. Capture the selection's geometry **before** rendering — source track
   name, GUID, and the start/end of its item span — and write them into
   `status.json` (see §2). The import step needs them to position the
   result, and after the render the selection may have changed.
4. **Render the selected track's audio without mutating it.** REAPER's
   built-in render source has a "selected tracks (stems)" mode driven by
   `GetSetProjectInfo`/`GetSetProjectInfo_String`'s `RENDER_*` keys and
   executed via `Main_OnCommand(42230, 0)`; unlike freezing
   (`Main_OnCommand(41824, 0)`, which persists FX/render state onto the
   track) it touches no track-level or FX state at all — REAPER computes
   what would be audible from just the selection and bounces that. Chosen
   deliberately over freeze *because* of the project's non-destructive
   invariant (`docs/AGENTS.md` — "changes only `[vgt]`-managed objects");
   freezing a user's arbitrary selected track, even reversibly, is exactly
   the kind of persisted mutation that invariant exists to prevent. Render
   format must be **WAV** (MT3's CLI reads its input via
   `wav_data_to_samples_librosa` on the raw bytes); sample rate and channel
   count are free, since librosa resamples to MT3's own 16 kHz mono.
   *Spike:* the precise `RENDER_SETTINGS` bitfield for "stems, selected
   tracks" is a REAPER API detail to confirm against the ReaScript
   documentation during implementation — not asserted here.
5. **Validate the render before spending an inference run on it.** This is
   not defensive boilerplate: the sibling `transcription/mt3` repo's
   guardrails record that a REAPER bounce via this same action 42230 has
   silently truncated audio partway through — going dead for the remainder
   while still writing a file of the *correct nominal total duration*,
   padded with silence. That was observed under headless `-newinst`, and the
   suspected cause (preset-slot GUIDs resolving only in a live instance)
   should not apply to an interactive in-instance render like this one — but
   "should not apply" is a poor bet ahead of a ~10-minute inference run on a
   possibly-dead file. Check that the WAV exists, is non-trivial in size,
   and that its duration matches the captured item span within a tolerance.
   Note explicitly, per that repo's own hard-won guardrail, that a
   **whole-file** silence check is *insufficient* — a file that is 60% real
   audio and 40% dead silence passes it. A cheap windowed check (RMS per
   N-second window, flagging a long trailing run of silence that reaches
   end-of-file) is the meaningful version. Refuse to spawn, with a clear
   message, rather than transcribing a truncated render.
6. Spawn `<vgt-cli> transcription track run <project> <job_id> --source
   .../source.wav --force-program <N>` **detached**, using the resolved CLI
   path from "Invoking the vgt CLI from Lua" below (never a bare `vgt`).
   *Spike:* `reaper.ExecProcess(cmdline, timeout)`'s timeout semantics — which
   of `0`/`-1`/`-2` means "start it and return immediately without capturing
   output" — must be confirmed against the ReaScript API documentation, at
   the same level of rigor as the `RENDER_SETTINGS` spike above; the intent
   is a fire-and-forget spawn that survives the calling script returning, and
   the plan does not assert which constant provides it. A raw
   `os.execute(cmd .. " &")` shell trick is the explicit fallback if
   `ExecProcess` cannot detach, but is second choice (no quoting help, no
   documented lifetime guarantee).
7. Start a bounded `reaper.defer()` polling loop (see "Notification") that
   watches this one job.

### 4. Invoking the vgt CLI from Lua

**This direction is new to the project and is the most likely thing to fail
on first run.** Every existing integration runs Python → REAPER (vgt shells
out to the REAPER binary to execute an action). Nothing has ever gone the
other way: `grep -n "ExecProcess\|os.execute\|io.popen" reascript/*.lua`
returns no hits today. There is therefore no established pattern to copy,
and one specific trap to avoid.

The trap: a macOS GUI application does not inherit a login shell's
environment. REAPER's `PATH` is typically just `/usr/bin:/bin:/usr/sbin:
/sbin` — it will **not** contain `~/.local/bin`, a `uv` tool directory, or a
project virtualenv's `bin/`. A ReaScript that spawns a bare `vgt ...` will
fail with "command not found" on a machine where `vgt` works perfectly in
the user's terminal, which reads as a baffling bug rather than a
configuration issue.

Resolution: **vgt records its own absolute entry point in the sidecar, and
the ReaScripts read it — they never search `PATH`.**

- `vgt analyze` (and any other command that already writes the sidecar)
  persists a small top-level `runtime` block: the absolute path of the
  running interpreter (`sys.executable`) and the argv0/console-script path
  it was invoked as. Python always knows this about itself for free; Lua
  cannot discover it at all.
- The trigger script reads that block and invokes the recorded interpreter
  explicitly — `<python> -m vgt transcription track run ...` — rather than
  relying on a console script being on `PATH`. `python -m vgt` also sidesteps
  the case where the console-script shim exists but its shebang points at a
  moved/rebuilt venv.
- If the block is absent (a sidecar written before this feature) or its path
  no longer exists on disk, the trigger action **refuses with an actionable
  message** ("run `vgt analyze` once to register vgt's location") rather
  than guessing, silently doing nothing, or spawning something that fails
  invisibly in a detached process whose output nobody captures.
- Every path interpolated into the command line must be quoted/escaped —
  project paths routinely contain spaces (`test/Reaper Project/`, in this
  repo's own fixture). This is worth a single shared quoting helper in
  `vgt_common.lua` rather than ad hoc concatenation at each call site.

Because the spawned process is detached, its stdout/stderr go nowhere the
user will see. The job runner must therefore treat `status.json` as its
*only* reporting channel — including for early, pre-transcription failures
(bad arguments, missing provisioning). A crash before it can write
`status.json` at all is covered by the stale-job watchdog under "Failure
modes."

### 5. Program resolution

REAPER tracks have no inherent "GM program" field once they're audio (which
is the normal case here — the whole point is transcribing an audio track).
Best-effort default, always overridable:

- If the selected track's name matches one of vgt's own known target labels
  (`[vgt] Guitar ...`, `[vgt] Bass ...`, etc. — the same label set
  `TRANSCRIPTION_TARGETS` already declares in `vgt_initialize.lua`), pre-fill
  a small built-in target→GM-program table (guitar → 25/nylon or 26/steel,
  bass → 33/acoustic bass — exact defaults are a product decision, not an
  engineering one; flagged under "Open questions").
- Otherwise pre-fill nothing meaningful (program 0) and rely entirely on the
  user's input. Matching an arbitrary, non-vgt-owned track's instrument can't
  be guessed reliably in general, and "if possible" in the request reads as
  best-effort, not mandatory inference.

### 6. Tempo matching

Fully solved by existing machinery — no new tempo logic needed:

- The Python job reads the project's already-analyzed tempo/tempo-map from
  the sidecar the same way `default_spec_for_target()` already does for
  every other MT3 call (`transcribe.py:1250-1330`,
  `tempo_map_reference()`), and passes it to
  `write_normalized_mt3_artifacts(..., tempo_bpm=..., tempo_map=...)`
  exactly like `Mt3Transcriber.detect_raw()` already does.
- On import, the watcher script reuses
  `set_take_ignores_project_tempo(item, midi_tempo)` — already implemented
  in `vgt_initialize.lua` for reference-MIDI variants — to make the new
  item's take ignore the *current* project tempo map and always play back at
  the tempo it was authored against (`variant.midi_tempo`, sourced from
  `track_jobs[job_id].midi_tempo`). This is the identical mechanism that
  already keeps every other `[vgt]` reference MIDI track correctly aligned
  regardless of later tempo edits; nothing new to invent here.
- Precondition: `vgt analyze` must have already run on this project (so a
  tempo/tempo-map exists to read). The trigger script should refuse with a
  clear message rather than silently falling back to a bare 120 BPM guess if
  no analyzed tempo is on record.

### 7. Python: the job runner (`vgt transcription track run`, new subcommand)

A new CLI subcommand, not exposed through the existing `transcription
variant`/`profile` subtrees (this isn't a target/profile/variant):

```
vgt transcription track run <project> <job_id> --source <wav> --force-program N [--label TEXT]
```

- Updates `status.json` to `{"status": "running", "started_at": ...}`
  immediately, before doing any work — so a poller checking right after
  spawn sees `running`, not a half-initialized file. Written by temp-file +
  atomic `os.replace`, merging onto the selection fields the trigger script
  already wrote (see §2). **Not** `atomic_update_sidecar` — that function
  writes the *sidecar*, which this command deliberately never touches; the
  job's own `status.json` is a single-writer file needing no lock.
- Builds an `Mt3Spec` with `force_program=N` and the project's tempo/map.
  Family elimination is moot once program is forced, so `target` carries no
  meaning here — note that `Mt3Spec.target` is currently typed `str`
  (`transcribe.py:1154`) and must be widened to `str | None`, which touches
  `to_dict()`/`spec_hash`. That is a cache-key-bearing change, so make it
  deliberately rather than discovering it mid-implementation: an absent
  target must serialize distinctly from any real target name.
- Calls `Mt3Transcriber.detect_raw()`'s inner pieces directly (subprocess
  invocation + `merge_all_musical_tracks` instead of
  `select_dominant_musical_track` + `write_normalized_mt3_artifacts`) rather
  than the whole `detect_raw()`/`transcribe()` path, since those assume a
  fixed target and the ordinary `destination_dir` layout.
- On success: writes `result.mid`/`result.csv`, updates `status.json` to
  `"done"` with `finished_at`/`note_count`. On any `TranscriptionError`:
  `"error"` with the message (same scrubbing behavior `Mt3Transcriber`
  already applies elsewhere — no local temp paths leaked). Because the
  process is detached, `status.json` is the *only* channel the user will
  ever see: wrap the whole body so that even an unexpected exception lands
  there as `"error"` rather than vanishing with the process.
- Fires the OS notification (below) as its very last step, success or
  failure, so the user is told either way.
- This command's whole process *is* the backgrounded job — nothing inside
  vgt itself needs to daemonize; the detachment happened once, at the
  REAPER-API boundary when it was spawned (§3 step 6).
- Reads the sidecar (for tempo/tempo-map and namespace) but never writes it,
  and never calls `analysis.analyze()` — see "Independence from `vgt apply`."

### 8. Notification

Two independent layers, because there's no IPC channel from a plain
background OS process back into a running ReaScript — polling a file is the
only channel REAPER-side, but it shouldn't depend on REAPER being open at
all for the user to find out:

- **OS-level (works even if REAPER/the watcher isn't running):** the job
  runner's last step shells out to `osascript -e 'display notification "..."
  with title "vgt"'` (macOS-only, matching this project's stated macOS-only
  environment assumption in `docs/AGENTS.md`). Cheap, independent of REAPER
  entirely. **This is a best-effort channel, never the only one:** a
  `display notification` is attributed to the calling process and is
  silently suppressed if the user has not granted that app notification
  permission — it can fail with no error and no visible banner. So the
  terminal state must *always* also be durable in `status.json` (it is, by
  construction) and reported by the check action. Treat a missing banner as
  expected on some machines, not as a bug to chase; do not build any
  behavior that depends on the notification having been seen.
- **In-REAPER (also imports automatically):** the `reaper.defer()` loop the
  trigger script starts polls `status.json` every few seconds (throttled —
  `defer` re-fires every UI frame, so gate on `reaper.time_precise()`, not
  every tick) for a **bounded** window (e.g. stop deferring after ~15
  minutes, well past `MT3_TIMEOUT_SECONDS = 600`), then goes quiet rather
  than polling forever in the background — an unbounded defer loop is a
  standing resource cost and a footgun if the user forgets it's running. The
  standalone "vgt: Get transcription" action (§9) is the supported way to
  check on a job after the bounded window elapses, or after reopening
  REAPER.

### 9. ReaScript: import (shared by the defer loop and the manual check action)

On `status == "done"`, reuse the *logic* of
`add_reference_midi_variant()`/`add_mt3_review_folder()` from
`vgt_initialize.lua` — but not the file. See "Independence from `vgt apply`"
below for why this has to be a copy into a new shared module, not a call
into `vgt_initialize.lua` itself:

- `add_locked_track(...)` with a `[vgt] <label> (MT3)` name and a
  `track-job:<job_id>` lock tag (extending the existing
  `mt3-root`/`mt3-track:N`/`variant:<target>:<id>` tagging convention so the
  non-destructive-invariant checks that already scan for `[vgt]`-owned
  objects recognize it).
- `reaper.PCM_Source_CreateFromFile(result.mid path)` →
  `AddMediaItemToTrack`/`AddTakeToMediaItem`/`SetMediaItemTake_Source`,
  positioned at the `item_start_s`/`item_end_s` the trigger script captured
  before rendering (§3 step 3) and stashed in `status.json` alongside
  `midi_tempo`, so the importer never re-derives them from a selection that
  may since have changed.
- `set_take_ignores_project_tempo(item, midi_tempo)`.
- Record the job in `analysis["track_jobs"][job_id]` with
  `status: "imported"` — **using the shared sidecar commit protocol**
  (re-read late, merge, re-check `generation`, atomic rename, bounded
  retries; see §2's "Sidecar block and schema migration"). This record is
  what makes the import idempotent: a job already present as `"imported"` is
  skipped, so re-running the action never creates a duplicate track.

Idempotency is deliberately anchored in the sidecar rather than in
`status.json`, because the sidecar is the thing that travels with the
project and is already the authority for what `[vgt]` objects exist. A
`status.json` still reading `"done"` after import is not a bug — it is the
job's own terminal state, and the importer's own record is what gates a
second import.

On `status == "error"`, surface the error via `reaper.MB` (or the console)
once, then record it in `track_jobs` the same way (`status: "error"`), so it
isn't re-reported on every future check.

This action touches nothing beyond scanning `track_jobs` and, for each
finished/unimported one, adding its single track. It does not run tempo/key/
section/chord detection, does not touch stem separation or `mt3_review`, and
does not reconcile or reposition any other `[vgt]`-managed container — all of
that is `vgt apply`'s job, not this one's.

### Independence from `vgt apply` / full initialize

This was an explicit requirement, not an implementation convenience: running
"vgt: Get transcription" (or letting the trigger script's own defer
loop auto-import) must **never** run `vgt apply`'s full reconciliation pass
— no re-running stem separation, tempo/key/section/chord detection, the
`mt3_review` refresh, or working-copy reconciliation, just to pick up one
finished job.

The risk this guards against is mechanical, not just organizational:
`vgt_initialize.lua` is one large script whose entire reconciliation body
runs unconditionally, top to bottom, whenever the file executes as a REAPER
action — Lua/EEL ReaScript has no `if __name__ == "__main__"`-style gate.
`add_locked_track`, the MIDI-import helpers, and the lock-tag scanning logic
this plan wants to reuse are defined *inside* that file, not factored out.
`dofile`-ing or `require`-ing `vgt_initialize.lua` from the new scripts to
reach those helpers would therefore also execute its full reconciliation —
exactly the outcome to avoid.

Resolution: extract the needed pieces into a shared module with no
reconciliation body of its own — the same pattern
`vgt_working_copy_common.lua` already establishes for
`vgt_create_working_copy.lua`/`vgt_promote_working_copy.lua`.

The full extraction list is larger than the import helpers alone, and
under-scoping it is the main way this step gets mis-estimated:

| From `vgt_initialize.lua` | Why the new scripts need it |
|---|---|
| `decode_json` (`:341`, ~85 lines) | Reading `status.json` at all. Lua has no built-in JSON. |
| `read_analysis_block` / `read_analysis` (`:428`) | Reading tempo, namespace, and `track_jobs` from the sidecar. |
| `read_generation` (`:436`) + the commit/retry helper (`:1425-1465`) | Writing the imported record under the #138 protocol. |
| `add_locked_track` | Creating the `[vgt]`-owned result track. |
| `PCM_Source_CreateFromFile`/item/take wiring (`:1216-1245`) | Building the MIDI item. |
| `set_take_ignores_project_tempo` (`:1179`) | Tempo alignment. |
| `track_name`/`starts_with`/`starts_with_vgt` (`:67-78`) | Selection validation. |
| `project_dir` / sidecar path derivation | Locating everything above. |

Note this is a *copy* out of `vgt_initialize.lua` into the shared module,
not a move: per the "deliberately not pursued" note below,
`vgt_initialize.lua` keeps its own local definitions for now, so the
duplication is temporary and acknowledged rather than accidental. A future
cleanup issue collapses it.

**Consolidated, rather than added as a second `_common.lua`.** The actual
overlap between the working-copy feature and this one is small — just
`track_name`/`starts_with`, which is *already* independently duplicated a
third time in `vgt_initialize.lua` and a fourth time in the ad hoc
`vgt_consolidate_midi_tracks.lua`. `vgt_working_copy_common.lua`'s
remaining content (container/GUID tracking, chunk detachment, promote/create
logic) is working-copy-specific and irrelevant here. Rather than adding a
second, narrowly-named shared file, **rename `vgt_working_copy_common.lua`
to a feature-neutral `vgt_common.lua`** and add this plan's helpers to it
alongside the existing working-copy logic (left otherwise unchanged). One
shared module, not two, and its name no longer misdescribes half its
contents. Update the two `dofile(directory .. "vgt_working_copy_common.lua")`
call sites (`vgt_create_working_copy.lua:7`, `vgt_promote_working_copy.lua:7`)
to the new filename, and both new scripts (`vgt_transcribe_track.lua` and
`vgt_get_transcription.lua`) `dofile` the same `vgt_common.lua` —
never `vgt_initialize.lua`. Update `SCRIPT_NAMES`
(`src/vgt/reascripts.py:10-17`): drop `vgt_working_copy_common.lua`, add
`vgt_common.lua` plus the two new scripts — net two new files registered,
not three, and `install_reascripts`' existing conflict-detection means a
locally-modified installed copy of the old name is simply left behind as an
inert stray rather than silently overwritten (worth a one-line note in the
issue that does the rename, but not a migration mechanism worth building).

Deliberately **not** pursued in this same pass: also folding
`vgt_initialize.lua`'s own local `track_name`/`starts_with`/`add_locked_track`
into `vgt_common.lua`. That script is the highest-blast-radius file in the
project (the automatic-reconciliation entry point every non-destructive
invariant is about) and today has zero external `dofile` dependencies;
giving it one is a bigger, separable change with its own risk profile, not
something to bundle into a plan whose actual goal is one on-demand
transcription feature. Worth a future cleanup issue on its own, not blocked
on or blocking this plan.

The same independence holds Python-side: `vgt transcription track run` and
the CLI-side status check it powers must not call `analysis.analyze()` or
anything that triggers stem separation. They only ever read the project's
already-persisted sidecar (tempo/tempo-map, namespace) and write to the new
`track_jobs` section — see "Tempo matching"'s precondition above (fail
clearly if no analyzed tempo exists yet; never silently re-run `analyze()`
to get one).

## Safety: the non-destructive invariant

The one genuinely new risk this feature introduces, beyond every prior
`[vgt]` action: it operates on an arbitrary, **user-owned** track it did not
create. Every existing automatic-reconciliation invariant in
`docs/AGENTS.md` assumes vgt only ever touches its own `[vgt]`-prefixed
objects; this is explicitly a new, separate, user-invoked action, the same
category exception the working-copy actions already carry ("the separately
user-invoked working-copy action is the sole exception..."). Concretely:

- Rendering must never leave the selected track soloed/muted/frozen if the
  script errors partway through. Note the chosen "stems (selected tracks)"
  render mode should require no transient track-state changes *at all* — so
  the correct handling is a `pcall` that restores whatever project-level
  `RENDER_*` settings the script itself changed, **not** an
  `Undo_BeginBlock`/`Undo_EndBlock` pair: an undo block around an operation
  that mutates no project state just pushes a confusing empty entry onto the
  user's undo stack. If the render spike (§3 step 4) finds the mode *does*
  require touching track state, revisit this and add the undo block then.
- The trigger action must refuse (not silently proceed) if the current
  selection is empty, is more than one track, or is itself a `[vgt]`-locked
  track — mirroring the guard rails the working-copy actions already apply
  to their own selection-driven behavior (today in
  `vgt_working_copy_common.lua`, to become `vgt_common.lua`).
- Nothing about this feature may write `.RPP` text directly; both import and
  any transient render-state handling go through the ReaScript API only,
  per the existing "Live REAPER mutation" invariant.

## Failure modes and edge cases

- **REAPER closes while a job is running.** The Python job is detached (§3
  step 6), so it keeps running and still writes
  `status.json`/fires the OS notification. Reopening REAPER and running
  "vgt: Get transcription" (or the next `analyze`/`apply`) picks it
  up and imports it — no result is lost, only the in-REAPER defer-loop
  auto-import is missed, which the manual check action recovers.
- **Job crashes / MT3 provisioning missing / checkpoint missing.** Same
  `TranscriptionError` surface every other MT3 call already uses; recorded
  as `status: "error"` with a scrubbed message, never left silently
  `"running"` forever. A watchdog is still required, not optional, because
  a detached process's output goes nowhere: if `status.json` says
  `"running"` but more than `MT3_TIMEOUT_SECONDS` (600) plus a margin has
  elapsed since `started_at`, the check action reports it as stale rather
  than polling forever. This covers the two cases the `except` branch
  cannot — a hard crash (segfault/OOM/kill) that never reaches Python's
  exception handling, and a spawn that failed outright so that no
  `status.json` was ever written by the job at all (in which case the file
  still holds only the trigger script's selection fields, with no
  `started_at`/`status` — treat that as "never started").
- **The spawn silently failed.** The single most likely first-run failure
  (see §4). Because `ExecProcess` is fire-and-forget, a bad CLI path
  produces no error anywhere. Mitigated by resolving the path from the
  sidecar and refusing up front when it is missing or non-existent, and by
  the "never started" watchdog case above — the user gets a clear stale-job
  report instead of a job that appears to hang forever.
- **Disk growth.** Every job leaves a full-length rendered WAV plus its
  MIDI/CSV. At a typical 44.1 kHz stereo 24-bit render that is roughly
  15 MB per minute of audio — about 80 MB for a five-minute song, per job.
  Jobs are never cleaned up automatically in v1 (see "Prohibited scope"),
  so this accumulates silently. At minimum the check action should report
  the total size of `track-jobs/` when it is large, so the user has some
  signal before a disk fills. A `vgt transcription track purge` is the
  natural follow-up, deliberately out of scope here.
- **Two jobs on the same track, or overlapping renders.** No exclusivity in
  v1 (see "Prohibited scope"); each job has its own directory and job id, so
  they cannot corrupt each other's artifacts even if triggered concurrently.
- **User discards/deletes the source track before the job finishes.** Fine —
  the rendered `source.wav` already stands alone in
  `track-jobs/<job_id>/`; the job never touches the live track again until
  the (separate) import step, which only needs the *item bounds* stashed
  earlier, not the track's continued existence. If it's gone, importing
  should still succeed (just re-anchored at the stashed position) with a
  clear log line noting the source track name if it's still resolvable.

## Testing and verification

Per `docs/AGENTS.md`'s CI/verification rules (no hosted CI; local `pytest`
is the evidence) and its "human-owned REAPER verification" rule (agents must
not claim to have verified live REAPER behavior themselves):

- **Offline/unit-testable (agent-verifiable):** `Mt3Spec`'s new
  `force_program` field and its identity/hash behavior, plus the widened
  `target: str | None` serializing distinctly from a real target;
  `build_mt3_argv()`'s new flag; `mt3_normalize.merge_all_musical_tracks()`
  against a synthetic forced-program MIDI fixture with *two* non-drum tracks
  and one drum track, proving the rhythm split merges and the drum channel
  is excluded (mirrors the existing `select_dominant_musical_track` tests);
  the job runner's `status.json` state machine (running → done /
  running → error / unexpected-exception → error) against a fake/stub
  `Mt3Transcriber`; the schema 18 → 19 migration round-trip, including an
  older sidecar migrating to an empty `track_jobs`; the render-validation
  helper against a fixture WAV that is half real audio and half trailing
  silence (it must *fail* — the case a whole-file check misses); and the
  stale-job watchdog's classification given a synthetic old `started_at`.
- **Requires a human running REAPER (not agent-claimable):** the render
  step's actual audio correctness, `reaper.ExecProcess` detachment
  behavior, the `defer()` polling loop's UI-thread behavior, the OS
  notification firing, and the final imported track's alignment/tempo by
  ear/eye against the source. Any issue covering these must say so
  explicitly and route final acceptance through the human, per the existing
  rule — do not mark such an issue `status:ready-for-review` on the
  assumption a script "looked right."

## Suggested issue breakdown (dependency order)

Filed only on explicit go-ahead — listed here in the order `AGENTS.md`'s
issue-creation convention requires (blockers before what they block):

Three of these have no dependency on each other and can proceed in parallel
(1, 3, 5) — the Lua extraction in particular does *not* need the CLI to
exist first, so putting it early shortens the critical path.

1. Repin the MT3 fork to the `force_program` commit; bump `MT3_LOCK_SHA256`.
   *(blocks 2 — nothing MT3-side works against the old pin)*
2. `Mt3Spec.force_program`, widen `Mt3Spec.target` to `str | None` (a
   cache-key change — see §7), `build_mt3_argv` threading, and
   `mt3_normalize.merge_all_musical_tracks()`, with offline tests.
   *(blocked by 1)*
3. Sidecar **schema 18 → 19**: `analysis["track_jobs"]` block,
   `_empty_track_jobs_block()`, migration, and module-docstring entry,
   with offline tests. *(no dependency; can start immediately)*
4. Persist the `runtime` block (interpreter path) on `vgt analyze`, so Lua
   can find the CLI at all — see §4. Small, but nothing spawns without it.
   *(blocked by 3, since it adds another sidecar field; fold into 3 if
   convenient)*
5. Rename `vgt_working_copy_common.lua` → `vgt_common.lua` and copy in the
   **full** extraction list from "Independence from `vgt apply`" — including
   `decode_json`, the sidecar read/`generation`-commit helpers, and
   `add_locked_track`, not just the import wiring. Update the two existing
   `dofile` call sites and `SCRIPT_NAMES` (`src/vgt/reascripts.py`).
   *(no dependency on 1-4; can start immediately. Larger than it first
   appears — see the extraction table. No behavior change to the existing
   working-copy actions, which is what keeps the rename low-risk.)*
6. `vgt transcription track run` CLI subcommand (job runner), including the
   single-writer `status.json` contract and catch-all error capture.
   *(blocked by 2, 3)*
7. `vgt_transcribe_track.lua` (select/capture-bounds/render/**validate**/
   spawn/prompt). Includes both API spikes (`RENDER_SETTINGS` bitfield,
   `ExecProcess` detach semantics) and the render-truncation check — budget
   for them explicitly. *(blocked by 5, 6; requires human REAPER
   verification per the rule above)*
8. `vgt_get_transcription.lua` (check + import + `generation`-protocol
   sidecar write + stale-job watchdog). *(blocked by 5, 6; requires human
   REAPER verification)*
9. OS notification + bounded defer-loop wiring in the trigger script,
   calling into 8's import logic on completion. *(blocked by 7, 8; requires
   human REAPER verification)*

## Prohibited scope for v1

- No "latest job per track" identity, cancellation, or job-queue UI — each
  run is an independent, disposable job; discard/cleanup of old
  `track-jobs/<id>/` directories is manual for now (a natural future variant
  of `transcription variant purge-discarded`, not built here).
- No true progress percentage — `mt3-transcribe` reports nothing mid-run
  today; "running vs. done vs. error" is the only granularity available
  without a separate upstream change to add incremental progress output
  (out of scope here; note as a possible future mt3-side enhancement).
- No non-macOS notification path (the project's own environment assumption
  is macOS-only).
- No automatic program-number inference beyond the small vgt-target-name
  guess table — no audio-content-based instrument classification.
- No GitHub Actions / hosted CI wiring, per the standing repo rule.

## Open questions for a human decision

1. **Default GM program per vgt target name** (guitar → nylon (24) vs. steel
   (25) vs. clean electric (27); bass → acoustic (32) vs. fingered electric
   (33)) — a product taste call, not resolved here.
2. **Two REAPER API spikes**, both to confirm against the ReaScript docs
   during implementation rather than assert now: the exact `RENDER_SETTINGS`
   bitfield for "stems, selected tracks via master" (§3 step 4), and
   `ExecProcess`'s timeout semantics for a fire-and-forget detached spawn
   (§3 step 6). Neither is a decision so much as a lookup, but both are on
   the critical path and should be budgeted as real work.
3. **Bounded defer-loop window length** (proposed ~15 minutes) and whether a
   visible progress indicator (e.g. a transient console line) is wanted
   during that window, vs. fully silent until done/timeout.
4. Whether this should also become reachable from `vgt analyze`'s
   `--transcribe`/`--mode` flags eventually (i.e., merge back into the
   target system once proven), or stay a permanently separate ad hoc path —
   explicitly not decided here; this plan only covers the ad hoc path.

## References

- `mt3/cli.py`, `mt3/transcription.py`, `mt3/note_sequences.py` (pinned fork
  at `mrkkucharski/mt3`, commit `1e5d143a8c2d33d1845df7f05b9bef7246ad1b2e`) —
  the new `--force-program` flag this plan depends on.
- `src/vgt/mt3_provision.py:33-77` — current pin, to be bumped.
- `src/vgt/transcribe.py:1117-1182` (`Mt3Spec`), `3487-3576`
  (`build_mt3_argv`, `Mt3Transcriber`).
- `src/vgt/mt3_normalize.py` — `select_dominant_musical_track`, the model
  for the new `merge_all_musical_tracks`.
- `src/vgt/analysis.py:592-625` — `refresh_mt3_instrumental_review`, the
  closest existing precedent for a review-style MT3 pass.
- `reascript/vgt_initialize.lua:1129-1250` (MIDI import via
  `PCM_Source_CreateFromFile`/`set_take_ignores_project_tempo`),
  `:1339-1374` (`add_mt3_review_folder`) — the logic the new shared module
  extracts *from*, without depending on this file at runtime.
- `reascript/vgt_working_copy_common.lua` (to be renamed `vgt_common.lua`,
  gaining this plan's helpers alongside its existing working-copy logic),
  `src/vgt/reascripts.py:10-17` (`SCRIPT_NAMES`) — the precedent for a
  shared, `dofile`-able module with no reconciliation body of its own, and
  where scripts get registered for installation.
- `docs/AGENTS.md` — non-destructive invariant, human-owned REAPER
  verification rule, no-hosted-CI rule.
- `docs/transcription-variants-plan.md`, `docs/drumscript-plan.md` — prior
  plans this one mirrors in structure and in reusing existing artifact/
  lifecycle conventions rather than inventing new ones.

## Implementation plan

Concrete, file-level detail for each item in the issue breakdown above,
using the same numbering. Waves are groups that can proceed concurrently;
within a wave, items are independent of each other.

**Standing rules for every item:** run the offline suite locally and paste
results into the issue (`uv run pytest`, per `docs/AGENTS.md` — there is no
CI); never mark an item ready-for-review on the strength of a script
"looking right"; and for any item marked *human-verified*, say so explicitly
in the issue and route final acceptance through the user.

**Offline Lua testing is available and expected.** `tests/
test_reascript_working_copy.py` establishes the harness: read the `.lua`
source, prepend `reaper = {}` with just the stubs a function needs, execute
via `lua -` (`VGT_TEST_LUA` overrides the interpreter), and assert on
stdout. Pure-logic Lua — `decode_json`, status parsing, render validation
arithmetic, job-id and quoting helpers — is therefore genuinely
agent-testable. Only the parts that need a live REAPER (actual render
output, `ExecProcess` detachment, `defer` scheduling, on-screen results)
fall under human verification.

### Wave 1 — no dependencies, start immediately

**Item 1 — Repin MT3.**
`src/vgt/mt3_provision.py`: set `MT3_PINNED_COMMIT` to
`1e5d143a8c2d33d1845df7f05b9bef7246ad1b2e`; recompute `MT3_LOCK_SHA256` via
the `gh api "repos/mrkkucharski/mt3/contents/uv.lock?ref=<commit>"` recipe
in that file's own comment. Leave `MT3_MODEL_ID`,
`MT3_INPUT_LENGTH_FRAMES`, and `MT3_LOOKAHEAD_FRAMES` unchanged — the
checkpoint is not changing, only the code.
*Tests:* `tests/test_mt3_provision.py` pin assertions.
*Done when:* `vgt transcription backend provision mt3 --force` succeeds and
the checked-out commit matches the pin.

**Item 3 — Sidecar schema 18 → 19.**
`src/vgt/sidecar.py`: add `_empty_track_jobs_block()` returning `{}` beside
`_empty_mt3_review_block()` (`:381`); merge it in the migration next to the
`mt3_review` line (`:584`); bump `SCHEMA_VERSION` (`:241`); add a `19 --`
module-docstring entry describing the block and why in-flight state is
*not* stored there.
*Tests:* extend `tests/test_analysis.py`'s migration coverage — a v18
sidecar migrates to v19 with an empty `track_jobs`, and an existing
`track_jobs` survives a re-migration unchanged.

**Item 4 — Persist the `runtime` block.** (Fold into item 3 if convenient.)
Record `sys.executable` and the invoked argv0 into a top-level `runtime`
block whenever `vgt analyze` writes the sidecar. Python knows this for
free; Lua cannot discover it at all (§4).
*Tests:* `tests/test_analysis.py` — the block appears after `analyze`, and
holds an absolute existing path.

**Item 5 — `vgt_common.lua`.** The largest single item; see the extraction
table in "Independence from `vgt apply`".
- `git mv reascript/vgt_working_copy_common.lua reascript/vgt_common.lua`,
  update its header comment to describe a general shared module.
- Copy in the table's helpers from `vgt_initialize.lua` (`decode_json:341`,
  `read_analysis_block:302`/`read_analysis:428`, `read_generation:436` and
  the commit/retry helper `:1425-1465`, `add_locked_track:912`,
  `set_take_ignores_project_tempo:1179`, the MIDI item/take wiring
  `:1216-1245`, `track_name:67`/`starts_with:72`/`starts_with_vgt:76`,
  `project_dir:53`, `PREFIX:5`, `warn:603`). Copy, do not move —
  `vgt_initialize.lua` keeps its own definitions this pass.
- Add a shell-quoting helper for command-line construction (§4).
- Update the two `dofile` call sites (`vgt_create_working_copy.lua:7`,
  `vgt_promote_working_copy.lua:7`) and `SCRIPT_NAMES`
  (`src/vgt/reascripts.py:11-19`).
*Tests:* update `tests/test_reascript_working_copy.py` and
`tests/test_reascript_install.py` for the new filename; add direct
executable-Lua tests for `decode_json` and the quoting helper (paths with
spaces, quotes, and non-ASCII).
*Done when:* the working-copy tests pass unchanged in behavior, proving the
rename is inert.

### Wave 2 — Python feature work

**Item 2 — `force_program` through vgt.** *(needs 1)*
- `src/vgt/transcribe.py`: add `force_program: int | None = None` to
  `Mt3Spec` (`:1117`); widen `target: str` → `str | None` (`:1154`) and
  update `to_dict()` so an absent target serializes distinctly from any real
  target name; emit `--force-program N` from `build_mt3_argv()` (`:3487`)
  when set; thread the parameter through `detect_raw()` (`:3578`) and
  `transcribe_all_tracks()` (`:3533`).
- `src/vgt/mt3_normalize.py`: add `merge_all_musical_tracks(path)` returning
  one `Mt3SelectedTrack` whose notes are every non-drum track's notes merged
  and re-sorted by `(start_s, pitch_midi)`. Reuse `_tempo_events`,
  `_extract_track_notes`, and `_is_drum_track` verbatim; apply **no** family
  elimination and **no** duration comparison. Bump
  `MT3_TRACK_SELECTION_VERSION` only if the existing selection path changes
  (it should not).
*Tests:* `tests/test_mt3_transcriber.py` for the argv flag and spec
identity; `tests/test_mt3_normalize.py` for a synthetic fixture with two
non-drum tracks (differing rhythm, same program) plus one channel-9 drum
track — the two merge, the drums are excluded.

**Item 6 — `vgt transcription track run`.** *(needs 2, 3)*
`src/vgt/cli.py`: new `track` subparser under `transcription` (`:131`),
with a `run` command taking `<project> <job_id> --source --force-program
[--label]`. Implement the runner in a new `src/vgt/track_jobs.py` rather
than growing `transcribe.py` (already 4k lines):
- `write_status(job_dir, **fields)` — merge-and-replace via temp file +
  `os.replace`; the job process is the only writer (§2).
- Set `running`, build the `Mt3Spec`, invoke MT3, run
  `merge_all_musical_tracks` + `write_normalized_mt3_artifacts`, set `done`.
- Wrap the entire body so *any* exception lands as `status: "error"` — a
  detached process has no other channel.
- Fire the `osascript` notification last, best-effort, never fatal.
- Read the sidecar for tempo/namespace; never write it; never call
  `analysis.analyze()`.
*Tests:* new `tests/test_track_jobs.py` — the full state machine
(`running → done`, `running → error`, unexpected-exception → `error`)
against a stub transcriber; `status.json` is valid JSON after every
transition; a missing analyzed tempo fails clearly.

### Wave 3 — ReaScripts (all human-verified)

**Item 7 — `vgt_transcribe_track.lua`.** *(needs 5, 6)*
Selection validation → program prompt → capture name/GUID/item bounds into
`status.json` → set `RENDER_*` and render (**spike**) → validate the render
(duration vs. item span; windowed RMS, *not* whole-file silence) → resolve
the CLI from the sidecar `runtime` block → spawn detached (**spike**) →
start the bounded defer loop. Restore any `RENDER_*` settings it changed via
`pcall`; no undo block (§ Safety).
*Offline tests:* validation arithmetic and the refusal paths, via the Lua
harness. *Human-verified:* that the render is correct audio and the spawn
actually detaches.

**Item 8 — `vgt_get_transcription.lua`.** *(needs 5, 6)*
Scan `track-jobs/`, read each `status.json`; on `done` and not already
recorded, create the `[vgt] <label> (MT3)` track with a `track-job:<id>`
lock tag, build the item at the stashed bounds, apply
`set_take_ignores_project_tempo`, and record `status: "imported"` in
`analysis.track_jobs` **through the `generation` commit protocol**. Report
`error` once and record it. Apply the stale-job watchdog. Report total
`track-jobs/` size when large.
*Offline tests:* status classification, the watchdog's staleness rule, and
skip-if-already-imported, via the Lua harness. *Human-verified:* the
imported track's placement and tempo alignment.

**Item 9 — Notification and defer wiring.** *(needs 7, 8)*
Throttle the defer loop on `reaper.time_precise()`, bound it (~15 min), and
call item 8's import path on completion so there is exactly one importer.
*Human-verified:* end to end, including that closing REAPER mid-job still
yields a correct import on the next manual check.

### Suggested verification sweep before calling the feature done

1. `uv run pytest` — full offline suite green, pasted into the final issue.
2. Manual, in REAPER (user): transcribe a `[vgt]`-owned stem and an
   arbitrary user track; confirm one MIDI track appears per job, aligned,
   on the requested program.
3. Manual: start a job, close REAPER before it finishes, reopen, run
   "vgt: Get transcription" — the result imports.
4. Manual: point the sidecar `runtime` path at a non-existent file and
   confirm the trigger action refuses with a clear message rather than
   spawning nothing silently.
5. Confirm `vgt apply` still behaves identically — nothing in this feature
   changed its reconciliation.
