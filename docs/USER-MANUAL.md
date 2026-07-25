# vgt user manual

vgt prepares an existing REAPER project for guitar practice. It analyzes one
reference mix, optionally separates practice stems through LALAL.AI, can make
local reference MIDI from those stems, and adds only its own `[vgt]` objects to
the project. This is the current user-visible contract and a compact regression
checklist.

## Quick workflow

1. Install the CLI and bundled REAPER actions:

   ```sh
   uv tool install git+https://github.com/mrkkucharski/vgt.git
   vgt install-reascripts
   ```

   The actions are installed to
   `~/Library/Application Support/REAPER/Scripts/vgt`. In REAPER's Action
   List, use `ReaScript: Load` to register both installed Lua files once.
   This step does not require retaining a source checkout. `--dry-run` previews
   paths without changing them and `--destination DIR` is useful for a custom
   REAPER resource location or automated test. The installer leaves identical
   files alone and asks before replacing a different file; use `--force` only
   when you intend to replace it.
2. Save the target `.RPP` in REAPER 7.x.
3. Run `vgt_initialize.lua` from REAPER's Action List.
   On first use choose a file-backed reference track and declare whether its
   guitar is electric or acoustic. This writes an adjacent `Song.vgt` sidecar.
4. Run `vgt analyze "Song.RPP"` in a terminal. After any available separation,
   it also transcribes the requested stems locally: DrumScript transcribes
   `drums`, and Basic Pitch transcribes every other target. This is free and
   needs no confirmation; guitar is requested by default.
5. Run `vgt_initialize.lua` again to apply analysis and import available stems
   and reference MIDI tracks.
6. Correct chords and sections in REAPER, then run `vgt_sync.lua` from the
   Action List.
7. Inspect persisted state with `vgt status "Song.RPP"` or `--json`.

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
| `[vgt] Key` | Valid key analysis | Unmuted but silent text-item track with one locked item showing the effective root and scale, including a deliberate sidecar override. It is display-only. |
| `[vgt] Beats` | Existing/human-edited tempo map, or detected beats with unknown bar phase | Unmuted, silent text-item track; beat items are locked. |
| `[vgt] Click` | Tempo-click artifact exists | Muted audio track; unmute temporarily to check the beat grid. |
| Vocals, Instrumental, Bass, Drums, Guitar, Backing | Standard separation | Unmuted, time-based audio tracks. |
| Strings, Keys / Piano | Explicitly requested | Unmuted, time-based optional stem tracks. |
| `[vgt] <Target> Ref — <Label> (MIDI)` | A retained transcription variant was transcribed | Unmuted, time-based MIDI item directly beneath the stem it was transcribed from. The selected variant uses a warm-gold track colour solely as a visual cue; colour is not an ownership signal. It has no sound without an instrument; muting it would only dim the notes meant to be read. |
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
- `transcription/<target>.mid` — a cached reference MIDI for each successfully
  transcribed target.
- `transcription/<target>.csv` — its Basic Pitch note-events data (every
  target except `drums`).
- `transcription/drums.json` — DrumScript's percussion event data (instrument
  labels and onset times) for the `drums` target.

vgt writes a tempo map only when the project still has REAPER's default 120
BPM, 4/4 map and analysis detected a downbeat. Beat-only fallback analysis
(for example, librosa when madmom is unavailable) retains the detected BPM and
beat grid but records its bar phase as unknown; it always uses `[vgt] Beats`
instead of anchoring a tempo map to an arbitrary beat. It never overwrites
another map or a human-edited vgt map, and refreshes a vgt map only when it can
prove that the map is still untouched.

To correct chords, edit `[vgt] Chords` items: rename the take, move/resize,
split, delete, or add items with a take name. Rename or move only vgt-created
section regions. Then run `vgt_sync.lua` from REAPER's Action List. Sync preserves the
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

## Stem transcription (reference MIDI)

Transcription runs inside `vgt analyze`, after separation has made any stem
sources available. It runs entirely locally, so it is free, needs no LALAL
credentials, and never asks for cost confirmation. A target without a matching
stem is reported as skipped; it never silently falls back to transcribing the
full mix.

**Backend routing is per target and is not configurable:**

| Target | Backend |
| --- | --- |
| `drums` | DrumScript |
| `guitar`, `bass`, `vocals`, `instrumental`, `backing`, `strings`, `piano`, `original` | Basic Pitch |

Guitar is the default requested target. Add other targets with repeatable
`--transcribe`; this persists the target in the sidecar, so later analyses keep
its reference fresh without repeating the flag:

```sh
vgt analyze --transcribe bass --transcribe drums "Song.RPP"
```

- `--transcribe <target>` adds a target to the persisted requested set. Valid
  targets are `guitar`, `bass`, `vocals`, `drums`, `instrumental`, `backing`,
  `strings`, `piano`, and `original` (the mix).
- `--transcribe-only <target>` runs just that target this time, without changing
  the persisted set.
- `--forget-transcription <target>` removes a persisted target and deletes its
  MIDI and, for `drums`, its JSON event artifact (other targets keep a CSV
  instead).
- `--no-transcribe` skips transcription for this run without changing the
  persisted set.
- `--mode <target>=<profile>` persists a transcription profile for one target;
  repeat it to select several. Current profiles are `default`, `guitar`,
  `bass`, `bass-monophonic`, `vocals`, and `guitar-acoustic`. For example,
  `vgt analyze --mode guitar=guitar-acoustic "Song.RPP"`. A stale mode from
  an older sidecar safely falls back to the target default, but a profile named
  explicitly on the command line must be valid.
- `--mode bass=bass-monophonic` opts into a cleanup that allows only one
  sounding bass note at a time. It is not the default because a separated bass
  stem can contain bleed; use it only when that trade-off is right for the
  stem. No equivalent vocals profile exists: LALAL vocals stems can contain
  stacked backing vocals and harmonies.

The guitar declaration (`--guitar electric|acoustic`) remains a stem-separation
choice for LALAL. Existing acoustic declarations automatically retain the
equivalent `guitar-acoustic` transcription profile when their sidecar upgrades.

If a backend's execution or output validation fails, `drums` (or any other
target) is recorded with `status: error` and analysis continues for every
other requested target. There is no automatic fallback between backends: a
failed DrumScript run never silently substitutes Basic Pitch output, and vice
versa.

`vgt status "Song.RPP"` reports transcription as a multi-target block: how many
targets are requested and, for each one, its effective transcription profile,
note/event count, a missing-source skip, or an error. `vgt status --json` also
reports each target's persisted `requested_mode` (or `null`) and the resolved
`effective_profile`; stale modes and old acoustic declarations use the same
safe fallback and migration rules as analysis. For `drums`, the status line summarizes DrumScript's
per-instrument event counts (for example, `drums 428 events (kick 91, snare
87, hats 201, other 49), drumscript 0.1.6`) instead of a MIDI pitch range. The
status artifact list includes each target's MIDI and, depending on backend,
CSV or JSON when available.

### Basic Pitch (guitar, bass, vocals, piano, strings, instrumental, backing, original mix)

For a machine that must not build Basic Pitch's environment on first use,
prepare it separately with Python 3.11:

```sh
uv tool install --python 3.11 --with "setuptools<81" "basic-pitch[onnx]==0.4.0"
```

Then set `VGT_BASIC_PITCH_CMD` to the command for that installed
`basic-pitch` executable; it replaces vgt's normal isolated invocation.

Treat the result as a **draft reference**, not a transcription to trust note for
note. Guitar transcription remains an open research problem, especially for
distorted or polyphonic parts. The MIDI carries pitches and timing, but no
fretboard or string information, so vgt does not produce tablature.

### DrumScript (drums)

DrumScript detects percussive onsets in the `drums` stem, classifies one or
more drum instruments at each onset, and writes them as General MIDI
percussion notes on **MIDI channel 10** — not musical pitches. Several
instruments detected at the same onset become simultaneous channel-10 notes
rather than being collapsed to one. Its current instrument map covers kick,
snare, low/mid/high toms, closed/open hi-hat, crash, and ride.

Two limitations to know before reading the output:

- **No calibrated confidence.** DrumScript's events carry timestamps,
  instrument labels, and internal debug features, but no probability that can
  be honestly shown as a confidence percentage. `vgt status` and the JSON
  sidecar report `"confidence": null`.
- **Fixed velocity.** The exported MIDI uses a constant velocity (100) for
  every note, so it does not preserve playing dynamics.

Because these are GM percussion selectors rather than musical pitches, the
reference MIDI has no meaningful pitch range to display — treat the note names
in a piano-roll view as instrument identifiers, not melodic content.

For a machine that must not build DrumScript's environment on first use
(it pulls Torch, Torchaudio, and Demucs as dependencies), prepare it
separately with the pinned version:

```sh
uvx --python 3.12 --from drumscript==0.1.6 drumscript --help
```

Then set `VGT_DRUMSCRIPT_CMD` to the command for that installed `drumscript`
executable; it replaces vgt's normal isolated `uvx` invocation. vgt parses the
override with `shlex.split` and never runs it through a shell.

`[vgt] <Target> Ref — <Label> (MIDI)` is recreated on every apply, like every other
vgt-owned object, for both backends. Edits to it do not survive: make a working
copy before editing (see [Working copies](#working-copies-vgt_working_copylua)).
There is no `vgt sync` read-back for MIDI.

## Working copies (`vgt_working_copy.lua`)

Because every `[vgt]` object is regenerated on each apply, the reference MIDI is
a draft to read, not an editing surface. `vgt_working_copy.lua` makes a
**user-owned** copy you can edit freely, side by side with the vgt references,
that no later apply or sync will ever touch.

Install it with `vgt install-reascripts` (it ships alongside the other two
actions) and run it from REAPER's Action List. It offers two choices:

- **Create working copy from selected tracks** — select the track(s) you want
  to work with (typically the selected `[vgt] <Target> Ref — <Label> (MIDI)`, plus its stem and
  `[vgt] Chords` for context), then run this. Each selected track is duplicated
  into a `[work]` folder track — created if missing, reused if present. The
  copies are unmuted, unlocked, and immediately editable.
- **Discard all [work] copies** — deletes every track still named `[work] …`,
  clearing the scratch area in one step.

The copies are deliberately outside vgt's ownership: they are named `[work]`
(never `[vgt]`) and carry no vgt-managed mark, so `vgt_initialize.lua` leaves
them alone forever — it only ever deletes tracks that are both vgt-owned and
still `[vgt]`-named. To **keep** an edited copy past a discard (or promote it as
your finished part), drag it where you want and rename it so it no longer starts
with `[work]`; it is yours from then on, exactly like a `[vgt]` track you
reclaimed by renaming. The vgt reference it came from stays as the muted machine
baseline, regenerated on the next apply.

The action edits the live project only: it never reads or writes the `.vgt`
sidecar and never touches a `[vgt]` or user track.

### Human-owned verification checklist

Autonomous vgt work and its automated tests only cover static artifact
validation (readable MIDI, well-formed JSON, correct REAPER placement via
stubbed tests). They never open REAPER, run a live ReaScript, or judge whether
a transcription sounds right. Before trusting a drum reference in practice,
the user should, in a real REAPER session:

- listen to `[vgt] Drums Ref (MIDI)` against the `[vgt] Drums` audio stem with
  a drum-kit instrument loaded, and judge whether the detected kicks, snares,
  and cymbals line up with what is audible;
- check that the reference track sits directly below `[vgt] Drums` and stays
  aligned after `apply`/`sync`;
- treat any transcription, but especially drums, as a draft reference to
  correct by ear, not a ground truth.

## Permanent regression contract

- vgt changes only objects it created and recorded as `[vgt]`-managed.
- Re-running initialization or analysis creates no duplicate managed tracks,
  regions, or stems.
- Managed track and region ownership is durable against an interrupted apply:
  a crash, restored backup, or copied project folder between building the
  `[vgt]` area and the final sidecar write cannot cause a re-apply to append a
  duplicate block, because ownership is also recorded directly in the REAPER
  project (a per-track mark for tracks, project-scoped extended state for
  regions) and reconciled from the union of both records.
- Project mutation uses REAPER's API, never RPP text editing.
- Heavy analysis runs in the CLI, not inside REAPER.
- vgt-owned audio is time-based and does not stretch with tempo-map changes.
- Human-synchronized chord and section corrections survive analysis and apply.
- Ordinary `--force` makes no LALAL charges; paid work is cached, checkpointed,
  and explicitly confirmed when forced or optional.
- Transcription runs locally after separation, never triggers paid separation,
  and independently caches each requested target's MIDI plus its CSV (Basic
  Pitch targets) or JSON (`drums`) artifact.
- DrumScript backs `drums`; Basic Pitch backs every other target. A DrumScript
  or Basic Pitch failure records a per-target error and never falls back to
  the other backend.
- Reference MIDI tracks are unmuted, time-based, and paired with their source
  stem; their edits are not a supported sync workflow and must be copied to a
  user-owned track first.
- `vgt status` is read-only and never reveals the license key.

For repository checks, run `uv run pytest -q`.
