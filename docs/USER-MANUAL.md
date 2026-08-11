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
   List, use `ReaScript: Load` to register the five installed action files once:
   `vgt_initialize.lua` initializes and applies vgt-managed objects,
   `vgt_sync.lua` saves section corrections;
   `vgt_sync_tempo_map.lua` is the separate, confirmation-gated action for
   adopting a REAPER tempo-map correction; and
   `vgt_create_working_copy.lua` creates protected user-owned `[work]` copies
   of generated reference MIDI, chords, or key tracks; and
   `vgt_promote_working_copy.lua` promotes finished copies into `[clean]`.
   This step does not require retaining a source checkout. `--dry-run` previews
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
   `drums`, a pYIN pitch tracker transcribes `bass`, and Basic Pitch
   transcribes every other target. This is free and needs no confirmation;
   guitar is requested by default.
   When MT3 has been provisioned, it also transcribes the complete instrumental
   stem and retains every predicted MIDI track for review.
5. Run `vgt_initialize.lua` again to apply analysis and import available stems
   and reference MIDI tracks.
6. To keep a chord or key correction, select its `[vgt]` track, use
   `vgt_create_working_copy.lua` to create a `[work]` copy, edit that copy,
   then use `vgt_promote_working_copy.lua` to promote it into `[clean]`. To
   save a section correction, rename or move the
   managed region and run `vgt_sync.lua` from the Action List. If you
   deliberately corrected the tempo map for this reference, run the separate
   `vgt_sync_tempo_map.lua` action and confirm its prompt.
7. Inspect persisted state with `vgt status "Song.RPP"` or `--json`.

`vgt [project.rpp]` and `vgt inspect [project.rpp]` are read-only. Without a
path, vgt uses the only `.RPP` in the current directory and refuses to guess
when there are zero or multiple candidates. `vgt apply` and `vgt sync` point to
the required REAPER actions; they never text-edit an RPP.

## Objects vgt creates in REAPER

Original tracks, items, and regions are never renamed, deleted, or changed.
At the bottom of the project, initialize maintains this four-position layout:
loose root tracks, `[clean] <reference name>`, `[work] <reference name>`, then
`[vgt] <reference name>` (and, when available, `[vgt] MT3`). It encodes a bottom-up workflow: vgt generates into
`[vgt]`; copy down into `[work]` to edit; then promote up into `[clean]` to
keep. vgt may add:

| Object | When | State and purpose |
| --- | --- | --- |
| `[clean] <reference name>` | Initialization | User-content container for promoted finished tracks. Automatic initialize/apply may create, rename, recolour, and reposition the container track, but never touches anything inside it. |
| `[work] <reference name>` | Initialization | User-content container for editable working copies. Automatic initialize/apply may create, rename, recolour, and reposition the container track, but never touches anything inside it. |
| `[vgt] <reference name>` | Initialization | Folder when it has children; otherwise a plain track. |
| `[vgt] Chords` | Chord analysis | Unmuted but silent text-item machine draft. Every `vgt analyze` regenerates it from audio; initialize/apply re-creates its vgt-managed track. Copy it into `[work]` before making a correction you want to keep. |
| `[vgt] Key` | Valid key analysis | Unmuted, silent text-item machine draft with one take name showing the effective root and scale. Every `vgt analyze` regenerates it from audio; initialize/apply re-creates its vgt-managed track. Copy it into `[work]` before making a correction you want to keep. |
| `[vgt] Beats` | Tempo analysis | Unmuted, silent text-item track; beat items are locked. vgt never turns them into project tempo/measure markers. |
| `[vgt] Click` | Tempo-click artifact exists | Muted audio track; unmute temporarily to check the beat grid. |
| Vocals, Instrumental, Bass, Drums, Guitar, Backing | Standard separation | Unmuted, time-based audio tracks. |
| Strings, Keys / Piano | Explicitly requested | Unmuted, time-based optional stem tracks. |
| `[vgt] <Target> Ref — <Label> (MIDI)` | A retained transcription variant was transcribed | Unmuted, time-based MIDI item directly beneath the stem it was transcribed from, in default/neutral track colour; every retained variant is a peer, ordered only for stable presentation, with none marked preferred. The item spans the reference track, whether or not the transcription's last note reaches the end of the song, and never loops to fill it. It has no sound without an instrument; muting it would only dim the notes meant to be read. |
| `[vgt] MT3` | Instrumental stem and provisioned MT3 backend | Last managed folder, after `[vgt]`; one unmuted MIDI track per note-bearing MT3 prediction. These are deliberately unfiltered review candidates, not selected variants or ground truth. |
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

The always-available section fallback looks for changes in the longer-term
character of the music: it pools short-time chroma/timbre descriptors, measures
novelty over several seconds of context, and suppresses nearby weaker phrase
changes. Its generic regions remain a draft for human correction; see
`docs/section-detection-findings.md` for the measured tuning and limitations.

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

vgt always presents the analyzed beat grid on `[vgt] Beats`. Applying analysis
never creates, updates, or deletes REAPER tempo/time-signature markers and
never claims measure numbering on the project ruler. This applies whether the
analysis detected a downbeat or only a phase-free beat sequence. The separate
`vgt_sync_tempo_map.lua` action remains available when you deliberately want
an existing, user-authored REAPER tempo map to become analysis evidence.

`[vgt] Chords` and `[vgt] Key` are disposable machine drafts. Each `vgt analyze`
regenerates them from audio, and the next initialize/apply replaces their
vgt-managed tracks. To keep a chord or key correction, select the generated
track, run **Create working copy from selected tracks** in
`vgt_create_working_copy.lua`, edit the resulting `[work]` copy, then run
**Promote selected `[work]` tracks to `[clean]`** in
`vgt_promote_working_copy.lua`. The promoted `[clean]` track is
user-owned and survives later analysis and apply. For a key copy, keep one item
named with a pitch class and mode, such as `E minor` or `F# major`. For a chord
copy, you can rename takes, move/resize, split, delete, or add named items.

Rename or move only vgt-created section regions, then run `vgt_sync.lua` from
REAPER's Action List. It preserves the machine-detected section baseline while
saving the effective edited sections as human-verified. Re-applying before sync
discards unsynchronized edits to vgt-managed regions.

`vgt_sync.lua` synchronizes only sections; it does not read chord or key tracks
and never reads tempo markers. To adopt a tempo correction,
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
| `drums` | DrumScript on the raw stem by default (`raw`); `hpss` adds analysis-only gentle HPSS, and `adtof` uses ADTOF |
| `bass` | pYIN, a monophonic pitch tracker (see [Bass](#pyin-bass) below) |
| `guitar` | Basic Pitch with analysis-only harmonic HPSS by default; explicit `default` opts out to raw guitar |
| `vocals`, `instrumental`, `backing`, `strings`, `piano`, `original` | Basic Pitch |

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
  `guitar-acoustic`, `guitar-harmonic`, and (for `drums`)
  `raw`, `hpss`, and `adtof`. For example, `vgt analyze --mode guitar=guitar-acoustic
  "Song.RPP"` or `vgt analyze --mode drums=hpss "Song.RPP"` (see
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

Analysis-frontends for a transcription experiment use schema version 2 and
derive an aligned, cached WAV without altering the LALAL stem. For example:

```toml
schema_version = 2

[profiles.guitar-bandpass]
target = "guitar"
extends = "guitar-acoustic-clean"

[profiles.guitar-bandpass.audio_frontend]
stages = [{ type = "bandpass", low_hz = 70, high_hz = 5000, order = 4 }]
```

Processed-input WAVs are temporary, content-addressed transcription cache
artifacts. They are never loaded into REAPER; only the resulting MIDI variant
appears beside the original stem.

Drum transcription uses the built-in `raw` profile by default and feeds the
unaltered `stems/drums.wav` to DrumScript. The optional `hpss` profile blends
35% percussive HPSS into an analysis-only WAV and applies conservative event
cleanup; it never changes the stem. `adtof` selects the alternative ADTOF
backend. Older sidecars using `default`, `drums-clean`, `drums-hpss-gentle`, or
`drums-adtof` remain readable, but status and newly created variants use the
canonical names `raw`, `hpss`, and `adtof`.

Guitar transcription likewise uses `guitar-harmonic` by default: 50% harmonic
HPSS feeds an analysis-only WAV into the acoustic-clean profile. The raw guitar
stem is unchanged; use `--mode guitar=default` or `--profile default` to opt
out to the raw, tuned guitar path.

Use `vgt transcription profile validate "Song.RPP"`, then add it with
`vgt transcription variant add guitar --name "my clean" --profile
my-clean-guitar "Song.RPP"`. Profiles inherit from built-ins; only overrides
belong in TOML. Unknown keys, incompatible targets, invalid bounds, cycles,
and reordering cleanup stages are rejected. All output-changing settings,
including spectral FFT/hop values, are recorded in the resolved variant
snapshot and hashes.

Bass profiles may extend `bass` or `bass-pyin` and tune the pYIN tracker
without exposing Basic Pitch-only settings:

```toml
[profiles.low-bass]
target = "bass"
extends = "bass-pyin"
description = "Five-string, low-tuned bass"

[profiles.low-bass.detection]
minimum_frequency_hz = 25
maximum_frequency_hz = 280
frame_length = 4096
hop_length = 512
median_filter_frames = 7
rearticulation_rise_db = 1.2
rearticulation_minimum_spacing_beats = 0.25
```

pYIN accepts only `minimum_note_length_ms`, the two frequency bounds,
`sample_rate_hz`, `frame_length`, `hop_length`, `median_filter_frames`,
`rearticulation_span_frames`, `rearticulation_rise_db`, and
`rearticulation_minimum_spacing_beats`; Basic Pitch-only thresholds and toggles
are rejected. Raise `rearticulation_rise_db` if a part is being split into more
notes than were played, lower it if repeated notes on one string still arrive as
a single held note, and set it to `0` to turn splitting off entirely. Lower
`rearticulation_minimum_spacing_beats` (0.375 by default, a dotted sixteenth)
for a part that genuinely repeats notes faster than that — it is the floor on
how close two splits inside one held pitch may fall.
Cleanup stages retain their canonical order even when TOML declares them in a
different order. Changing a detection setting invalidates that profile's raw
cache once; changing only cleanup reuses the raw pYIN detection and derives a
new result. This one-time, local cache invalidation is expected and costs no
network or paid work.

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
so no "keep one note" filter can recover the right note. In the final shipped
profile evaluation, switching to pYIN took frame-level F-measure from 7.2% to
78.9% on that stem; 10.9% of graded frames were octave errors. These are
relative measurements against an estimated CQT reference, not a general
accuracy guarantee. The full comparison, including the two independent
estimators used as a reference, is in
[docs/bass-transcription-findings.md](bass-transcription-findings.md) — indexed,
with every other instrument's evidence, from
[docs/instrument-transcription-findings.md](instrument-transcription-findings.md).

A tracker reports pitch, and playing one fret several times running does not
change pitch — so the raw tracker emitted one held note wherever a string was
plucked repeatedly, finding only **46%** of the notes actually played. vgt cuts
those runs where the frame energy restarts, and requires a minimum musical
spacing between cuts, which takes onset F-measure from 57.1% to 75.6% against a
full-length hand-corrected reference. Every frame-level figure is
unchanged by this, because a frame-level metric cannot see the difference; the
measurement and its limits are in the findings doc. Adjust it per project with
`rearticulation_rise_db` (above).

Because a tracker emits one line by construction, the `bass` profile needs no
voice cap: its cleanup is only `merge_fragments`, `drop_isolated_notes`, and
`clamp_sustain`. Its supported 35–330 Hz window is the tracker's fundamental
search range. It excludes a five-string low B fundamental (30.9 Hz) and lower
drop tunings, so material below 35 Hz needs a separately measured profile.

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
last. If no trustworthy beat array remains after a manually corrected tempo
map is synchronized, vgt instead detects whether DrumScript returned a
uniformly quantized clock and moves its slots to strong nearby audio onsets.
Unquantized events and weak or ambiguous evidence remain unchanged. If neither
alignment path is trustworthy, DrumScript's own times are used unchanged.
Because the beat grid is an input to the result, re-running tempo analysis re-transcribes the drums
rather than reusing MIDI aligned to the old grid.

The optional **`hpss`** profile is also available:

```sh
vgt analyze --mode drums=hpss "Song.RPP"
# or, to retain raw and HPSS side by side:
vgt transcription variant add drums --name raw --profile raw "Song.RPP"
vgt transcription variant add drums --name hpss --profile hpss "Song.RPP"
```

`hpss` applies a small, conservative post-processing pass to
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
run, not a rerun of the first. **Retain both and compare them**: `raw`
is DrumScript's unfiltered read and the more complete audit trail;
`hpss` is easier to read at a glance but, like any automatic cleanup,
can occasionally suppress or nudge something a human ear would have kept.
Neither is authoritative. `vgt transcription profile show hpss
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
vgt transcription variant add drums --name baseline --profile raw "Song.RPP"
vgt transcription variant add drums --name adtof --profile adtof "Song.RPP"
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

### MT3 backend provisioning

vgt can provision a pinned MT3 runtime for the opt-in `guitar-mt3`/`bass-mt3`
profiles below. This is provisioning only: `vgt analyze` never clones,
builds, or downloads anything implicitly, no matter which profiles a project
has retained. Fetch it explicitly, once, ahead of time:

```sh
vgt transcription backend provision mt3
```

This clones [Marek's MT3 fork](https://github.com/mrkkucharski/mt3)'s pinned
`v0.1.1` tag, verifies the checkout resolves to the exact contracted commit,
builds its isolated environment with its own committed `uv.lock`
(`uv sync --project ... --frozen`), and downloads the checkpoint
(`mt3-download-model --output-dir ... --json`). MT3's TensorFlow/JAX/T5X
dependency graph is never installed into vgt's own environment.

**Location and disk/network expectations.** Everything lands under
`~/Library/Caches/vgt/mt3` (override with `VGT_MT3_CACHE_DIR`) — the
repository checkout and its `.venv` under `repo/`, the downloaded checkpoint
under `models/`, and a `checkpoint-manifest.json` fingerprint (file paths,
sizes, and sha256 over the downloaded checkpoint) that a later MT3 backend
will use as part of its cache identity. Nothing is written into the REAPER
project or the `.vgt` sidecar. First provisioning clones a repository, builds
a Python environment, and downloads a multi-instrument transcription
checkpoint — expect real disk space and a real download.

**Requirements.** The fork is pinned to Apple Silicon macOS, Python 3.11, a
`uv` version in the fork's declared range, and `ffmpeg` on `PATH` (`brew
install ffmpeg`). Missing any of these produces an actionable diagnostic
before vgt attempts any clone, build, or download.

**Re-running is idempotent.** A second `provision mt3` with nothing changed
recomputes the checkpoint's hashes, confirms they still match the recorded
manifest, and does nothing further — it does not rebuild the environment or
redownload the checkpoint. If the on-disk checkpoint no longer matches the
manifest (corrupted, partially deleted), or the prior attempt was
interrupted, the command rebuilds/redownloads to converge on a verified
state again. `mt3-download-model` is resumable, so an interrupted download
picks up where it left off rather than starting over. Pass `--force` to
rebuild the environment and redownload the checkpoint unconditionally.

**Removal.** There is no separate removal command; delete
`~/Library/Caches/vgt/mt3` (or your `VGT_MT3_CACHE_DIR` override) and rerun
`provision mt3` to start clean.

**Offline behavior.** Outside of `provision mt3` itself, nothing in vgt
touches the network for MT3. `guitar-mt3`/`bass-mt3` need the provisioned
checkpoint and find it missing report instructions to run
`vgt transcription backend provision mt3`; they do not attempt to provision
it automatically. The offline test suite fakes the checkout, build, and
download steps and never clones a repository, runs `uv`, or downloads a
model.

### MT3 alternative (guitar/bass, opt-in, experimental)

`guitar-mt3` and `bass-mt3` are opt-in, experimental profiles that run the
provisioned MT3 backend instead of Basic Pitch/pYIN. They never replace or
change the current guitar/bass defaults, and there is no automatic fallback
between them -- add one as a separate retained candidate beside the default:

```sh
# Provision the backend once (see above), then add MT3 beside the default.
vgt transcription backend provision mt3
vgt transcription variant add guitar --name mt3 --profile guitar-mt3 "Song.RPP"
vgt transcription variant add bass --name mt3 --profile bass-mt3 "Song.RPP"
```

`guitar-mt3` only accepts the `guitar` target and `bass-mt3` only accepts
`bass` (`--mode guitar=bass-mt3` or the reverse is rejected before anything
runs). Both feed the raw separated stem into MT3 (no HPSS frontend) and
retain only its **dominant MIDI track** through vgt's normalizer, in two
steps: every track on General MIDI's percussion channel is excluded (a
structural MIDI convention, not an instrument guess); and every remaining
track MT3 *explicitly named* (every track but one always is, by MT3's own
MIDI-writing convention) whose declared GM program falls outside the
target's instrument family (guitar/bass) is also excluded -- reading MT3's
own declared classification for a track it named, not vgt guessing an
instrument from audio. Among the survivors, the one with the most total note
*duration* is kept (not raw note count, which a fragmented, wrong-instrument
track can otherwise win). There is deliberately **no cleanup pipeline** here
(no note-dropping, voice-cap, or force-monophony): these profiles exist to
show MT3's own output honestly, before any measured evidence would motivate
a derived cleanup profile. There is also no fallback to another MT3 track,
Basic Pitch, or pYIN -- and the file's one always-unnamed track is never
excluded by the family rule (there is no label to check it against), so if
that specific track is the wrong instrument, or MT3 is not provisioned, or
the backend fails, the variant records that error and nothing else is
substituted.

`guitar-mt3` and `bass-mt3` use the `checkpoint_1116020_it3_4s` guitar-pilot
checkpoint with a 512-frame (~4.1 s) encoder window and 256 frames of
lookahead: a 50% overlap. `vgt transcription profile show guitar-mt3` reports
the full pinned identity (fork repository, tag, commit, runtime, checkpoint
model id, inference window/overlap, and both normalization-stage versions)
instead of Basic Pitch/pYIN detector settings, which these profiles have none
of. `vgt status`/`--json` report the variant like any other: backend `mt3`,
note count, pitch range, and (once
provisioned) the checkpoint fingerprint as part of its cache identity.
Rename, discard, purge, force refresh, and reconciliation behave exactly like
any other retained note variant -- MT3 is admitted into the same two-level
raw-detection/derived-cleanup cache Basic Pitch/pYIN already use, just under
its own `transcription/cache/mt3/` cache directory so it never shares or
invalidates their cache entries.

**Known limitation: instrument leakage.** MT3 is a genuine multi-instrument
model; its raw output can and does contain other instruments as separate MIDI
tracks. Because selection is deliberately the *dominant non-drum* track and
nothing more, if that track is not the requested instrument, the variant will
contain whatever MT3's most note-populous non-drum track actually is -- there
is no "find the guitar" filter. This is not arbitrary (every drum-channel
track is excluded outright, and note-count dominance is a stronger signal
than the original "whichever track decodes first" rule it replaced -- issue
#290; see finding 7 in docs/instrument-transcription-findings.md for the
verified mechanism and why it changed), but it is still not a content
guarantee: on measured songs so far the correct instrument has always been
dominant, while a second, related instrument's content (e.g. a second
guitar-family track) has still been silently dropped by the
dominant-track-only rule. Treat these profiles as an experimental,
single-song-at-a-time comparison against the current default (see
docs/instrument-transcription-findings.md), not a production replacement.

## Working copies and promotion

Generated reference MIDI and the `[vgt] Chords` and `[vgt] Key` machine drafts
are regenerated by analysis and re-created by initialize/apply, so they are
references rather than editing surfaces. `vgt_create_working_copy.lua` makes a
**user-owned** copy you can edit freely, side by side with the vgt references,
that no later apply or sync will ever touch; `vgt_promote_working_copy.lua`
promotes a completed copy.

Install them with `vgt install-reascripts` (they ship alongside the other three
actions) and register both from REAPER's Action List. The separate actions are:

- **Create working copy from selected tracks** — select the track(s) you want
  to work with (for example, a `[vgt] <Target> Ref — <Label> (MIDI)`,
  `[vgt] Chords`, or `[vgt] Key`), then run this. Each selected track is duplicated
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

  A MIDI copy is fully detached from its source: the copy gets its own item,
  take and source identity and carries no reference to the source's MIDI pool,
  so editing notes in `[work]` never reaches back into the `[vgt]` reference or
  a sibling copy. Audio takes still reference the same stem file on disk — that
  sharing is deliberate and costs nothing. If a selected track's own notes live
  in *another* item (REAPER shows "MIDI edits are pooled with other media
  items"), create refuses the whole selection and asks you to un-pool that item
  first, since a copy of it would be empty.
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
  - One case recovers automatically rather than stopping: an apply that was
    interrupted after it finished rebuilding and marking the entire `[vgt]`
    tree (including the root's own durable per-track mark) but before it
    reached its own final manifest write leaves the on-disk manifest naming a
    GUID that no longer matches the live root. Because the live root itself
    -- not some other track -- already carries first-hand ownership evidence
    (its sidecar GUID or its durable P_EXT mark), the next apply resyncs the
    manifest from the live marks and proceeds normally instead of stopping.
    The hard stop above is reserved for a root with no such first-hand
    evidence on itself.
- Project mutation uses REAPER's API, never RPP text editing.
- Heavy analysis runs in the CLI, not inside REAPER.
- vgt-owned audio is time-based and does not stretch with tempo-map changes.
- Human-synchronized section corrections survive analysis and apply. Chord and
  key corrections survive only when promoted into a user-owned `[clean]` copy.
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
