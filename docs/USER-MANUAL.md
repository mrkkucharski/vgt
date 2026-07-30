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
   List, use `ReaScript: Load` to register all four installed Lua files once:
   `vgt_initialize.lua` initializes and applies vgt-managed objects,
   `vgt_sync.lua` saves chord, section, and key corrections;
   `vgt_sync_tempo_map.lua` is the separate, confirmation-gated action for
   adopting a REAPER tempo-map correction; and
   `vgt_working_copy.lua` creates protected user-owned `[work]` copies of
   generated reference MIDI and promotes finished copies into `[clean]`. This
   step does not require retaining a source checkout. `--dry-run` previews
   paths without changing them and
   `--destination DIR` is useful for a custom REAPER resource location or
   automated test. The installer leaves identical files alone and asks before
   replacing a different file; use `--force` only when you intend to replace
   it.
2. Save the target `.RPP` in REAPER 7.x.
3. Run `vgt_initialize.lua` from REAPER's Action List.
   On first use choose a file-backed reference track and declare whether its
   guitar is electric or acoustic. This writes an adjacent `Song.vgt` sidecar,
   creates the `[clean] <reference name>` and `[work] <reference name>`
   containers, and puts the loose root tracks, `[clean]`, `[work]`, and `[vgt]`
   folders in that order.
4. Run `vgt analyze "Song.RPP"` in a terminal. After any available separation,
   it also transcribes the requested stems locally: DrumScript transcribes
   `drums`, and Basic Pitch transcribes every other target. This is free and
   needs no confirmation; guitar is requested by default.
5. Run `vgt_initialize.lua` again to apply analysis and import available stems
   and reference MIDI tracks.
6. Correct chords, sections, or key in REAPER, then run `vgt_sync.lua` from
   the Action List. If you deliberately corrected the tempo map for this
   reference, run the separate `vgt_sync_tempo_map.lua` action and confirm its
   prompt.
7. Inspect persisted state with `vgt status "Song.RPP"` or `--json`.

`vgt [project.rpp]` and `vgt inspect [project.rpp]` are read-only. Without a
path, vgt uses the only `.RPP` in the current directory and refuses to guess
when there are zero or multiple candidates. `vgt apply` and `vgt sync` point to
the required REAPER actions; they never text-edit an RPP.

## Objects vgt creates in REAPER

Original tracks, items, and regions are never renamed, deleted, or changed.
At the bottom of the project, initialize maintains this four-position layout:
loose root tracks, `[clean] <reference name>`, `[work] <reference name>`, then
`[vgt] <reference name>`. It encodes a bottom-up workflow: vgt generates into
`[vgt]`; copy down into `[work]` to edit; then promote up into `[clean]` to
keep. vgt may add:

| Object | When | State and purpose |
| --- | --- | --- |
| `[clean] <reference name>` | Initialization | User-content container for promoted finished tracks. Automatic initialize/apply may create, rename, recolour, and reposition the container track, but never touches anything inside it. |
| `[work] <reference name>` | Initialization | User-content container for editable working copies. Automatic initialize/apply may create, rename, recolour, and reposition the container track, but never touches anything inside it. |
| `[vgt] <reference name>` | Initialization | Folder when it has children; otherwise a plain track. |
| `[vgt] Chords` | Chord analysis | Unmuted but silent text-item track; chord items are unlocked for editing. |
| `[vgt] Key` | Valid key analysis | Unmuted, silent text-item track with one editable take name showing the effective root and scale. Use the strict `E minor` format (a pitch class followed by `major` or `minor`). |
| `[vgt] Beats` | Existing/human-edited tempo map, or detected beats with unknown bar phase | Unmuted, silent text-item track; beat items are locked. |
| `[vgt] Click` | Tempo-click artifact exists | Muted audio track; unmute temporarily to check the beat grid. |
| Vocals, Instrumental, Bass, Drums, Guitar, Backing | Standard separation | Unmuted, time-based audio tracks. |
| Strings, Keys / Piano | Explicitly requested | Unmuted, time-based optional stem tracks. |
| `[vgt] <Target> Ref — <Label> (MIDI)` | A retained transcription variant was transcribed | Unmuted, time-based MIDI item directly beneath the stem it was transcribed from, in default/neutral track colour; every retained variant is a peer, ordered only for stable presentation, with none marked preferred. The item spans the reference track, whether or not the transcription's last note reaches the end of the song, and never loops to fill it. It has no sound without an instrument; muting it would only dim the notes meant to be read. |
| `[vgt]` section regions | Section analysis | Movable and renamable section markers. |

Chords and Beats are unmuted so labels stay visible, but contain no audible
media. Click is muted because it is audible media.

REAPER has no valid empty folder, so an empty `[clean]` or `[work]` container
appears as a plain track until it receives its first child. Seeing a plain
`[clean] <song>` on a fresh project is therefore expected. Only the container
tracks are coloured: `[clean]` is yellow-green `rgb(187,210,41)`, `[work]` is
light blue `rgb(68,175,239)`, and `[vgt]` is mauve `rgb(189,100,175)`. vgt sets
that colour once when it creates a container; a hand-made container keeps its
existing colour when adopted, and no later manual recolour is overridden.
Colour is presentation only: ownership is tracked through provenance rather
than color, and generated reference MIDI remains in the default/neutral track
colour.

## Analysis, verification, and corrections

`vgt analyze` detects tempo/beat grid, key, sections, and beat-aligned
major/minor chords. Once available, instrumental, guitar, and backing stems
also inform chord detection.

Artifacts live under `vgt/<stable-id>/` beside the project:

- `tempo-click.wav` — compare by unmuting `[vgt] Click`.
- `chords.txt` — effective chord list.
- `sections.txt` — effective section timeline.
- `transcription/<target>/<variant-id>.mid` — a generated reference MIDI for
  each retained successful variant. The opaque ID is stable; a label is not a
  filename.
- `transcription/<target>/<variant-id>.csv` — that Basic Pitch variant's
  derived note-events data (every target except `drums`).
- `transcription/cache/basic-pitch/<detection-hash>/raw.csv` (and raw MIDI) —
  shared raw detection data. Cleanup-only guitar variants can reuse it.
- `transcription/drums/<variant-id>.json` — a DrumScript variant's percussion
  event data (instrument labels and onset times).

Every transcription artifact lives under `transcription/<target>/`, for every
target; only those per-target directories and `cache/` sit directly inside
`transcription/`. Projects transcribed by an older vgt kept a target's first
result at a flat `transcription/<target>.mid` instead. The next `vgt analyze`
(or `vgt transcription variant add`) relocates those files to the current
layout, updates the sidecar to match, and deletes any flat leftover no retained
variant still points at. Nothing is re-transcribed: relocating a file changes
no cache identity. Run REAPER's initialize action afterwards if the project was
already open — its imported MIDI items still reference the old paths until the
managed tracks are rebuilt.

vgt writes a tempo map only when the project still has REAPER's default 120
BPM, 4/4 map and analysis detected a downbeat. Beat-only fallback analysis
(for example, librosa when madmom is unavailable) retains the detected BPM and
beat grid but records its bar phase as unknown; it always uses `[vgt] Beats`
instead of anchoring a tempo map to an arbitrary beat. It never overwrites
another map or a human-edited vgt map, and refreshes a vgt map only when it can
prove that the map is still untouched.

To correct the key, rename the sole `[vgt] Key` item's take using exactly a
pitch class and mode, for example `E minor` or `F# major`. Flats are accepted
and normalized to their sharp pitch class; the mode must be lowercase `major`
or `minor`. Keep exactly one key item. To correct chords, edit `[vgt] Chords` items: rename the take, move/resize,
split, delete, or add items with a take name. Rename or move only vgt-created
section regions. Then run `vgt_sync.lua` from REAPER's Action List. Sync preserves the
machine-detected chord/section baselines while saving the effective edited
values as human-verified. Re-applying before sync discards unsynchronized edits
to vgt-managed objects.

`vgt_sync.lua` rejects a missing, ambiguous, or invalid key label without
changing the sidecar. It never reads tempo markers. To adopt a tempo correction,
run `vgt_sync_tempo_map.lua` and explicitly confirm: it reads the live
tempo/time-signature markers over the selected reference item only, stores a
constant or piecewise reference-relative grid as human-verified, and never
writes, claims, or changes the project map. Markers outside the reference are
ignored; empty, invalid, or linear-ramped maps within the reference are rejected.
Later transcription and chord analysis use the synchronized grid. Re-run
`vgt analyze --force` only to refresh the
separate machine-detected baseline; to return the effective tempo to machine
detection, deliberately replace `analysis.tempo.value` with
`analysis.tempo.detected` and set `human_verified` to `false` in the sidecar,
then run `vgt_initialize.lua`. This reset is intentionally manual so vgt never
silently discards a verified correction.

Live REAPER verification remains human-owned: after synchronizing, inspect the
map and generated reference MIDI in your project and confirm that neither user
tracks nor the map itself changed.

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

**Backend routing is per target by default:**

| Target | Backend |
| --- | --- |
| `drums` | DrumScript (the default baseline); the opt-in `drums-adtof` variant profile uses ADTOF |
| `bass` | pYIN, a monophonic pitch tracker (see [Bass](#pyin-bass) below) |
| `guitar`, `vocals`, `instrumental`, `backing`, `strings`, `piano`, `original` | Basic Pitch |

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
  `bass`, `bass-pyin`, `bass-basic-pitch`, `bass-monophonic`, `vocals`,
  `guitar-acoustic`, and (for `drums`)
  `drums-clean`, and `drums-adtof`. For example, `vgt analyze --mode guitar=guitar-acoustic
  "Song.RPP"` or `vgt analyze --mode drums=drums-clean "Song.RPP"` (see
  [DrumScript](#drumscript-drums) below). A stale mode from an older sidecar
  safely falls back to the target default, but a profile named explicitly on
  the command line must be valid.
- `--mode bass=bass-basic-pitch` (or `bass-monophonic`) runs bass through Basic
  Pitch instead of the pitch tracker. These are the pre-tracker profiles, kept
  for comparison only — on a real stem Basic Pitch does not produce a usable
  bass line at any setting (see [Bass](#pyin-bass)). No monophonic vocals
  profile exists: LALAL vocals stems can contain stacked backing vocals and
  harmonies, which are genuinely polyphonic.

The guitar declaration (`--guitar electric|acoustic`) remains a stem-separation
choice for LALAL. Existing acoustic declarations automatically retain the
equivalent `guitar-acoustic` transcription profile when their sidecar upgrades.

### Retaining several variants per target

`--transcribe`/`--mode` keep working exactly as above during the compatibility
transition; they create or update a target's first retained (default) variant.
New workflows should use a variant-level `--profile`. To retain and compare several
independently configured candidates for the same target (for example a
detail-preserving pass alongside a clean, chord-oriented one), use the
`vgt transcription` command group instead:

```sh
# List, inspect, or validate built-in and project-local (<project>.vgt-profiles.toml) profiles.
vgt transcription profile list "Song.RPP"
vgt transcription profile show guitar-acoustic-clean "Song.RPP"
vgt transcription profile validate "Song.RPP"

# Create a retained candidate and reconcile it immediately.
vgt transcription variant add guitar --name detail --profile guitar-acoustic-detail "Song.RPP"
vgt transcription variant add guitar --name clean --profile guitar-acoustic-clean "Song.RPP"

# Rename without ever rerunning transcription.
vgt transcription variant rename guitar clean --name "clean chords" "Song.RPP"

# Discard a rejected candidate directly (its own generated artifacts and any
# now-unreferenced raw detection cache are removed).
vgt transcription variant discard guitar detail "Song.RPP"

# Clear the compact discarded-variant recipe/metrics history as a separate step.
vgt transcription variant purge-discarded guitar "Song.RPP"
```

A variant reference (`clean`, `"clean chords"`, ...) may be its label whenever
that label is unambiguous for the target, or its immutable id always. Two
variants that only differ in cleanup (like `guitar-acoustic-detail` and
`guitar-acoustic-clean`) share one Basic Pitch detection pass; only a variant
whose detection settings actually changed reruns it. `vgt status` reports
every target's retained variants in persisted order, and each one's
requested/effective profile, cache identity, metrics, and errors.

Generated variants are reproducible machine outputs and peers: retained
variants are ordered only for stable presentation, and none is designated
preferred, active, best, or selected. A `[work]` copy is a separate
user-owned editable track; it is not a variant, is never synchronized back
into vgt, and survives variant reconciliation.

### Authoring project profiles

Put advanced, auditable profile definitions in the project-adjacent
`Song.vgt-profiles.toml`, then validate them before adding a variant:

```toml
schema_version = 1

[profiles.my-clean-guitar]
target = "guitar"
extends = "guitar-acoustic-clean"
description = "Conservative chord-reading candidate"

[profiles.my-clean-guitar.cleanup.drop_harmonic_ghosts]
enabled = true
spectral_n_fft = 4096
spectral_hop_length = 512
spectral_max_harmonic_order = 8
spectral_freq_tolerance_semitones = 0.5
spectral_independent_energy_ratio = 1.5
```

Use `vgt transcription profile validate "Song.RPP"`, then add it with
`vgt transcription variant add guitar --name "my clean" --profile
my-clean-guitar "Song.RPP"`. Profiles inherit from built-ins; only overrides
belong in TOML. Unknown keys, incompatible targets, invalid bounds, cycles,
and reordering cleanup stages are rejected. All output-changing settings,
including spectral FFT/hop values, are recorded in the resolved variant
snapshot and hashes.

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

For example, retained variants are listed in persisted order, with no marker
implying preference:

```text
transcription: 1 target, 2 retained variants
  guitar
    clean   443 notes, MIDI 40-76, guitar-acoustic-clean
    detail  1060 notes, MIDI 40-88, guitar-acoustic-detail
```

<a id="pyin-bass"></a>
### pYIN (bass)

Bass is transcribed by a monophonic pitch tracker rather than by Basic Pitch. It
runs in-process through librosa, which vgt already depends on, so it needs no
separate install, no `uvx` subprocess, and no model download — and it works
offline on first use.

The reason is measured, not stylistic. On a real separated bass stem Basic Pitch
latches onto sustained low-frequency energy and emits a permanent chord under
the whole track: 966 notes, **22** simultaneous voices, two notes held for ~120
seconds, and ≥17 voices sounding for 98% of the song. No combination of onset
threshold, frame threshold, note-length floor, melodia setting, frequency
ceiling, or cleanup ordering fixed it — the model's note *boundaries* are wrong,
so no "keep one note" filter can recover the right note. Switching to a tracker
took frame-level F-measure from 7% to 79% on that stem. The full comparison,
including the two independent estimators used as a reference, is in
[docs/bass-transcription-findings.md](bass-transcription-findings.md).

Because a tracker emits one line by construction, the `bass` profile needs no
voice cap: its cleanup is only `merge_fragments`, `drop_isolated_notes`, and
`clamp_sustain`. Its 35–330 Hz window is the tracker's fundamental search range,
so a bass tuned below ~35 Hz needs a different profile.

This is still a **draft reference**. It tracks one pitch at a time, so a chord,
a double-stop, or bleed from another instrument is resolved to a single note, and
onsets are quantized to ~12 ms analysis frames rather than snapped to the beat
grid. Only one stem has been measured; a synth, fretless, or heavily distorted
bass may need its settings revisited.

For the retired Basic Pitch behaviour, select `--mode bass=bass-basic-pitch` (or
`bass-monophonic`). Those remain available for comparison, and an older sidecar
naming them still resolves.

### Basic Pitch (guitar, vocals, piano, strings, instrumental, backing, original mix)

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

The clean acoustic profile's round-three spectral ghost gate is conservative:
it has synthetic regression coverage, but has not been re-measured on the real
7Rivers stem. It can make a candidate easier to read; it is not evidence that
the clean MIDI is ground truth or that intentional octave shapes were kept.

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
- **DrumScript's own tempo detection is not used.** DrumScript's beat tracker
  can make gross octave errors (e.g. detecting half the real tempo), so vgt
  never authors the drum MIDI timeline at DrumScript's self-detected tempo.
  vgt re-authors every drum note at the project's own tempo from DrumScript's
  real-second onsets instead, the same way it does for every other target —
  DrumScript supplies onset/instrument detections, not the final timeline.

`default` re-authors DrumScript's raw events (onset time, instrument, and
velocity recovered from DrumScript's own MIDI) at the project tempo; it does
not filter or reclassify anything DrumScript detected. When a note's
velocity can't be matched back to DrumScript's MIDI closely enough to trust,
it falls back to a fixed velocity of 100 for that note only, not for the
whole take.

Before either profile runs, vgt reconciles DrumScript's timeline with the
beat grid from tempo analysis. DrumScript quantizes its onsets onto a grid it
fits itself, anchored at the very start of the stem and at its own tempo
estimate, so its "absolute seconds" begin at the item edge rather than at the
song's first beat and drift from there. vgt moves each event to the nearest
line of the analyzed grid, at the subdivision the backend was using, which is
what makes the reference MIDI line up with the beat from the first bar to the
last. If tempo analysis has not run, or the two grids cannot be matched
confidently, DrumScript's own times are used unchanged. Because the beat grid
is an input to the result, re-running tempo analysis re-transcribes the drums
rather than reusing MIDI aligned to the old grid.

An opt-in **`drums-clean`** profile is also available:

```sh
vgt analyze --mode drums=drums-clean "Song.RPP"
# or, to retain default and clean side by side:
vgt transcription variant add drums --name raw --profile default "Song.RPP"
vgt transcription variant add drums --name clean --profile drums-clean "Song.RPP"
```

`drums-clean` applies a small, conservative post-processing pass to
DrumScript's raw events, all within fixed, documented bounds — it never
assumes a repeated groove, a fixed number of hits per bar, or that a role
from one measure should be copied to the next:

- Coalesces events DrumScript reports a MIDI tick or two apart into one
  aligned onset (an 8ms window).
- Nudges each onset toward a nearby audio transient when the evidence is
  strong and unambiguous, bounded to ±30ms; otherwise leaves the timing
  untouched. No implicit global latency correction is ever applied — a
  correction this conservative for one measure can be wrong for another
  (see `docs/drums-clean-profile.md` for the measurement behind that
  choice).
- Shapes each note's velocity from local transient strength when that
  evidence is reliable, falling back to a bounded, per-instrument default
  (not a flat 100) when it isn't.
- Suppresses a note only when reproducible local audio evidence shows it's
  weak; every other event, however unusual next to its neighbours, is kept.

Both profiles have their own settings identity, cache, and artifacts, so
retaining both (as in the second example above) costs one extra DrumScript
run, not a rerun of the first. **Retain both and compare them**: `default`
is DrumScript's unfiltered read and the more complete audit trail;
`drums-clean` is easier to read at a glance but, like any automatic cleanup,
can occasionally suppress or nudge something a human ear would have kept.
Neither is authoritative. `vgt transcription profile show drums-clean
"Song.RPP"` prints its exact windows/thresholds; the generated
`transcription/drums/<variant-id>.json` for a clean variant records, per
note, its raw DrumScript time/instrument alongside the cleanup decision
(timing adjustment, velocity source, suppression) applied to it, so a
surprising result is always traceable back to what DrumScript actually
detected.

### ADTOF alternative (drums, opt-in)

**DrumScript remains the default baseline.** ADTOF is a second, opt-in drum
backend for comparison; it never replaces an existing DrumScript variant and
there is no automatic fallback between them. Add it as a separate retained
candidate:

```sh
# Keep the default DrumScript candidate and add ADTOF beside it.
vgt transcription variant add drums --name baseline --profile default "Song.RPP"
vgt transcription variant add drums --name adtof --profile drums-adtof "Song.RPP"
```

ADTOF runs its pinned Torch model in an isolated, pre-fetched environment,
then vgt peak-picks its raw activations, associates hits to the project beat
grid, derives velocities, and writes the same channel-10 MIDI plus JSON event
contract as DrumScript. It is therefore heavier than the baseline and needs
the pinned ADTOF environment and weights available locally; normal offline
tests use a fake and never download or import Torch.

Both candidates remain peers. Use `vgt status --json "Song.RPP"` to find each
variant's generated MIDI/JSON path, then score either event JSON (or MIDI)
against the same local corrected reference with the offline scorer:

```sh
uv run python scripts/drum_midi_score.py \
  /path/to/adtof.json /path/to/corrected-reference.json --window-seconds 2
uv run python scripts/drum_midi_score.py \
  /path/to/baseline.json /path/to/corrected-reference.json --window-seconds 2
```

Compare onset F1 and median timing error from the two reports; use the same
reference, tolerance, and window for both. The scorer is local-only and does
not invoke either transcription backend. Discard a candidate you do not want
to retain with `vgt transcription variant discard drums adtof "Song.RPP"`.

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
copy before editing (see [Working copies and promotion](#working-copies-and-promotion-vgt_working_copylua)).
There is no `vgt sync` read-back for MIDI.

## Working copies and promotion (`vgt_working_copy.lua`)

Because every `[vgt]` object is regenerated on each apply, the reference MIDI is
a draft to read, not an editing surface. `vgt_working_copy.lua` makes a
**user-owned** copy you can edit freely, side by side with the vgt references,
that no later apply or sync will ever touch.

Install it with `vgt install-reascripts` (it ships alongside the other three
actions) and run it from REAPER's Action List. It offers create and promote:

- **Create working copy from selected tracks** — select the track(s) you want
  to work with (typically the selected `[vgt] <Target> Ref — <Label> (MIDI)`, plus its stem and
  `[vgt] Chords` for context), then run this. Each selected track is duplicated
  into the initialize-owned `[work]` container. The action puts a durable
  private working-copy marker on the copies; the copies are unmuted, unlocked,
  and immediately editable. If that container is absent, the action creates
  the same marked `[work]` scaffold itself, ready for initialize to reconcile.
  If `[work]` already has children, create appends the new copies after them,
  reopening the folder's closing edge (REAPER stores it as a flag on the
  current last child) and moving it onto the new last copy. Only that
  folder-depth flag is touched; existing children's names, items, FX, and
  other content are left alone. If the container's folder structure has been
  changed outside this action, create refuses and leaves it untouched rather
  than guess how to repair it.
- **Promote selected `[work]` tracks to `[clean]`** — a selected track is
  eligible only when it has the durable working-copy marker and its name still
  starts with `[work]`. Promotion moves, rather than copies, only those eligible
  selected tracks into `[clean]`, so their identity, items, FX, routing, and
  other contents remain attached. Each is renamed `[clean] …` and has its vgt
  working-copy/container/managed marks removed, leaving it entirely user-owned.
  Unselected, ineligible, and reclaimed tracks keep their name, items, FX, and
  marks untouched. REAPER represents a folder's closing edge as a flag on its
  final child, so promotion may need to move that flag (never anything else)
  off a track that changed folder: if `[clean]` already has children, it
  reopens `[clean]`'s current closing edge and moves it onto the newly
  promoted track; if promoting empties out `[work]`'s current closing child
  while an unselected sibling remains, that sibling's flag becomes the new
  closing edge instead. Promotion still refuses, without changing anything,
  when `[clean]` or `[work]`'s folder structure doesn't match vgt's expected
  shape — it won't guess how to repair a folder edited outside this action.

The copies are deliberately outside normal vgt ownership: they are named
`[work]` (never `[vgt]`), carry no `vgt_managed` mark, and instead carry a
separate private working-copy marker used only by this action. Normal
reconciliation never touches them. Older unmarked `[work]` objects are
conservatively treated as user-owned. There is no discard action: delete an
unwanted `[work]` track with ordinary REAPER operations. Renaming a copy so it
no longer starts with `[work]` reclaims it for ordinary use: neither automatic
reconciliation nor promotion will touch it. Restoring the scratch name and
explicitly selecting it is a new deliberate request to promote it. The vgt
reference it came from remains as the unmuted machine baseline, regenerated on
the next apply.

The action edits the live project only: it never reads or writes the `.vgt`
sidecar and never touches a `[vgt]` or user track.

### Human-owned verification checklist

Autonomous vgt work and its automated tests only cover static artifact
validation (readable MIDI, well-formed JSON, correct REAPER placement via
stubbed tests). They never open REAPER, run a live ReaScript, or judge whether
a transcription sounds right. This checklist is human-owned, not an
autonomous-agent acceptance task, and never a reason to block closing an
issue. Before trusting a reference in practice, the user should, in a real
REAPER session:

- listen to `[vgt] Drums Ref (MIDI)` against the `[vgt] Drums` audio stem with
  a drum-kit instrument loaded, and judge whether the detected kicks, snares,
  and cymbals line up with what is audible. The reference should span the
  full stem and stay aligned throughout — a whole-take drift where the
  reference runs increasingly ahead of or behind the audio (the DrumScript
  half-tempo authoring bug, issue #193) is fixed and should not recur. What
  can still legitimately vary note-to-note is onset/detection quality: a
  missed hit, an extra hit, a misclassified instrument, or a few tens of
  milliseconds of per-note timing looseness, all inherent to DrumScript's
  detection rather than a vgt timeline bug;
- check that the reference track sits directly below `[vgt] Drums` and stays
  aligned after `apply`/`sync`;
- treat any transcription, but especially drums, as a draft reference to
  correct by ear, not a ground truth.
- compare guitar detail and clean candidates against the same stem; verify the
  spectral gate did not remove intentional octave chord shapes, and use
  `scripts/guitar_transcription_probe.py` to compare ghost share, polyphony,
  fragmentation, and both chord-tone metrics;
- create a `[work]` copy from the preferred generated candidate, edit it, run
  analyze/apply, and confirm it remains intact; then discard a rejected
  generated variant and confirm only that generated track/artifacts disappear;
- run initialize on a disposable copy whose folders are out of order (or with
  hand-made unmarked `[clean]`/`[work]` folders), and confirm the three
  containers end up in order, are adopted rather than duplicated, and have
  nothing inside them moved or changed;
- promote a `[work]` track and confirm it lands in `[clean]`, keeps its items
  and FX, is renamed, and survives a following initialize untouched;
- confirm an empty `[clean]` does not swallow the tracks below it.

This is also the place to exercise initialize's duplicate-`[vgt]`-folder
protection (issue #174) live, since the offline fake-REAPER harness in
`tests/test_reascript.py` and `tests/test_goal_contract.py` can prove the
reconciliation logic but never opens REAPER itself. This checklist item is
human-owned, not an autonomous-agent acceptance gate, and its absence must
never block closing an issue:

- run initialize twice in a row on a disposable copy of a real project and
  confirm REAPER shows exactly one `[vgt]` folder both times;
- add a transcription variant (or run `vgt analyze`/`vgt transcribe` again
  with different settings), initialize once more, and confirm the same root
  folder is reconciled in place rather than duplicated;
- save the project, close and reopen REAPER, and repeat both checks once to
  exercise the persisted project manifest and per-track marks rather than
  only in-session state.

## Permanent regression contract

- **Automatic reconciliation versus explicit working-copy actions:**
  initialize/apply changes only `[vgt]`-managed objects. It may create, rename,
  recolour, and reposition `[clean]`/`[work]` container tracks and move their
  blocks as a unit, but never modifies, renames, deletes, or reorders their
  contents. The explicitly invoked working-copy action may affect only newly
  created copies or selected tracks that retain both the durable working-copy
  mark and their `[work]` name. Promotion alone may move and rename those
  selected eligible tracks into `[clean]`; all other tracks stay untouched.
  A create or promotion request that would require a folder-depth rewrite on
  an existing, unselected container child is rejected unchanged.
- Initialize maintains the bottom layout and ordering: loose root tracks,
  `[clean]`, `[work]`, then `[vgt]`; it moves each populated user-content
  container only as a whole block.
- Re-running initialization or analysis creates no duplicate managed tracks,
  regions, or stems.
- Managed track and region ownership is durable against an interrupted apply:
  a crash, restored backup, or copied project folder between building the
  `[vgt]` area and the final sidecar write cannot cause a re-apply to append a
  duplicate block, because ownership is also recorded directly in the REAPER
  project (a per-track mark for tracks, project-scoped extended state for
  regions) and reconciled from the union of both records.
- Track ownership also records a stable role (`managed-root`, `beats`, `key`,
  `chords`, `stem:<name>`, or `variant:<target>:<id>`) and the project stores
  a managed-root manifest. If apply finds a `[vgt]` folder but cannot
  authenticate it, or finds multiple candidate roots, it stops before changing
  the project and reports the project/sidecar paths and every ownership count.
  This is intentional: a `[vgt]` name never grants deletion permission. To
  recover a duplicated project, save a backup, use the reported GUIDs to keep
  the authenticated root, and rename each unauthenticated folder to a clearly
  non-container, non-`[vgt]` name (for example `[archive] duplicate`) before
  applying again. The renamed folder is
  then preserved as user-owned; apply never removes it automatically.
- Project mutation uses REAPER's API, never RPP text editing.
- Heavy analysis runs in the CLI, not inside REAPER.
- vgt-owned audio is time-based and does not stretch with tempo-map changes.
- Human-synchronized chord and section corrections survive analysis and apply.
- Ordinary `--force` makes no LALAL charges; paid work is cached, checkpointed,
  and explicitly confirmed when forced or optional.
- Transcription runs locally after separation, never triggers paid separation,
  and caches raw Basic Pitch detection separately from each retained derived
  variant's MIDI/CSV (or a DrumScript variant's MIDI/JSON) artifact.
- Generated variants (peers, ordered only for presentation) and user-owned
  `[work]` copies have distinct ownership, tracked through provenance rather
  than color: apply reconciles generated `[vgt]` tracks, and working copies
  are preserved.
- Automatic chord analysis remains audio-based (original mix plus available
  instrumental/guitar/backing stems). Generated or clean MIDI is never fed
  back into chord analysis and is not ground truth.
- DrumScript backs `drums`; Basic Pitch backs every other target. A DrumScript
  or Basic Pitch failure records a per-target error and never falls back to
  the other backend.
- Reference MIDI tracks are unmuted, time-based, and paired with their source
  stem; their edits are not a supported sync workflow and must be copied to a
  user-owned track first.
- `vgt status` is read-only and never reveals the license key.

For repository checks, run `uv run pytest -q`.
