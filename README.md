# vgt

Virtual guitar teacher for non-destructively preparing REAPER projects for
practice. vgt analyzes a chosen reference mix, separates practice stems with
LALAL.AI, and uses a REAPER ReaScript to add its own `[vgt]`-managed tracks.

## Install and inspect

With Python 3.11 and `uv`:

```sh
uv tool install .
vgt "test/Reaper Project/Reaper Project.RPP"
```

`vgt [project.rpp]` (or the explicit `vgt inspect [project.rpp]`) accepts an explicit `.RPP` path. Without one it selects the only `.RPP` in the current directory; it refuses to guess if there are zero or multiple projects. It reports the project's sample rate, tempo/time signature, and track names/GUIDs.

## Tests and CI

Run the full offline test suite locally with:

```sh
uv run pytest -q
```

GitHub Actions runs this same command on Python 3.11 for every push and pull
request in [the test workflow](.github/workflows/tests.yml). It installs Lua
for the ReaScript fixture tests, then installs the locked Python environment
with uv. The suite uses mocked LALAL v1 fixtures; CI does not receive
credentials or make live LALAL API calls.

## Apply inside REAPER

Open and save the target project in REAPER 7.x. In **Actions → Show action list → ReaScript: Load**, load [reascript/vgt_initialize.lua](reascript/vgt_initialize.lua), then run it.

The action is intentionally the mutation path: it uses REAPER's API and never text-edits an RPP. It:

- pops up a menu of the project's own (non-`[vgt]`) tracks and asks which one is the **reference** to mirror;
- creates a REAPER folder named `[vgt] <reference track name>` (e.g. `[vgt] The Seven Rivers (Full March - 3_00)`) with a `[vgt] Mirror` child;
- clones only the chosen reference track's file-backed media to the mirror track, without adding sends, changing source tracks, or changing project tempo/sample rate;
- writes the adjacent `.vgt` sidecar — named after the project, e.g. `Reaper Project.vgt` — with its two created track GUIDs and config recording the reference track;
- on re-apply, deletes only tracks whose GUID occurs in the sidecar **and** whose current name begins `[vgt]`, then recreates the same area, carrying forward any `analysis` block `vgt analyze` had already written (schema stays version 1 until analysis exists, then becomes version 3).

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
into the sidecar as schema version 3 — never inside REAPER itself.

Each detector's output is cached against a hash of the source audio and the
detector's settings, so re-running only recomputes stages whose inputs
changed. Pass `--force` to recompute every stage regardless of the cache —
useful after changing a detector's code rather than its inputs. Any stage can
be corrected by hand-editing its `value` in the sidecar and setting
`human_verified: true`; `vgt analyze` then leaves that stage untouched on
every later run, regardless of what the audio hash says — even under `--force`.
Each detected stage records its UTC `analyzed_at` time; a correction records
`verified_at`, so the sidecar retains when vgt learned or a human confirmed it.

Progress is reported to stderr as each detector starts (the JSON result stays
on stdout, so `vgt analyze … | jq` and file redirects are unaffected).

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

Every run also renders a **click-only artifact** — `vgt/<namespace>/tempo-click.wav`,
under the project's `vgt/` subfolder — a bare click on the detected beat grid (the
reference audio is not mixed in) so the grid can be checked by ear or lined up against
the reference in REAPER; it is vgt-owned and regenerated each run, not
committed alongside the project.
The tempo stage's `value` also stores the raw detected `beat_times` — the
shared beat-synchronous grid the `chords` stage below aligns to.

### Key & beat-aligned chords

The `key` stage detects root + scale (major/minor) from the reference
track's full mix:

- **Primary backend: Essentia**'s `KeyExtractor`, used only if `essentia` is
  importable in the environment (Essentia wheels are fragile on Apple
  Silicon, so it's never a hard dependency and has no `uv` extra of its own).
- **Fallback: librosa chroma** correlated against the 24 Krumhansl–Schmuckler
  major/minor key templates — always available.

The `chords` stage detects a beat-aligned maj/min chord sequence, with every
segment boundary snapped onto the `tempo` stage's shared `beat_times` grid
(not a separately detected grid):

- **Primary backend: madmom**'s CNN chord recognizer, via the same `madmom`
  extra as the tempo stage.
- **Fallback: Chordino**, via the `sonic-annotator` vamp-plugin host, when
  `sonic-annotator` and the `nnls-chroma` plugin are installed system-side
  (neither ships as a pip package, so there's no `uv` extra for this either).
- **Last resort: a chroma + template classifier** (librosa chroma and the 24
  maj/min triad templates, decoded with a chord-duration prior; with a
  madmom-detected 4/4 downbeat it additionally pools template evidence per
  bar). The duration-prior and bar size are configurable as
  `chords.duration_prior` and `chords.bar_aggregation_beats`; bar pooling is
  deliberately disabled when downbeats are unavailable.
  — always available, and what actually runs in most dev/CI environments
  since neither of the above is typically installed.

Only major/minor triads are ever recognized — 7ths, sus, add9, etc. all
collapse to their nearest maj/min match — so every result is flagged
`"vocabulary": "maj_min"`.

Every run that (re)computes `value` also renders a **chord-sheet artifact** —
`vgt/<namespace>/chords.txt`, under the project's `vgt/` subfolder — a
plain-text timestamp + chord-label listing of `value`, for by-eye
verification.

The `chords` stage stores two parallel chord lists: `value` (the effective,
human-correctable chords used everywhere else — apply, the chord sheet) and
`detected` (the pristine machine detection, untouched by corrections). With
no corrections the two are equal. Once `value` is human-verified it freezes
for good, but `detected` keeps tracking the current reference audio and
detector settings on every `vgt analyze` run — it's the machine baseline, so
it stays live even while the human's `value` is frozen. Refreshing `detected`
in that state never touches the chord-sheet artifact, since that artifact
documents `value`, not the machine baseline. Keeping both around is what
makes a future "restore the original detection over this time range" action
possible.

### Sections

The `sections` stage detects song-structure boundaries + generic labels
("A", "B", …) from the reference track's full mix, stored in the sidecar as
`{"index", "start_seconds", "end_seconds", "label", "backend"}` entries
spanning the track end-to-end:

- **Primary backend: MSAF**'s novelty/structure segmentation, installed via
  the optional `msaf` extra (`uv sync --extra msaf`). Like madmom above,
  MSAF's maintenance is uncertain and its last release predates modern
  Python/NumPy/SciPy — isolated behind this extra so a default install
  never needs it.
- **Fallback: a self-similarity novelty + peak-picking heuristic** (built on
  librosa, installed by default) when MSAF isn't installed or fails to
  process the track: a Foote-style checkerboard-kernel novelty curve over
  chroma+MFCC features picks boundary candidates, then segments are
  agglomeratively matched against earlier segments' feature centroids so a
  recurring section (e.g. a chorus) gets the same label each time.

Labels are intentionally generic — renaming them (`"A"` → `"chorus"`) is a
normal correction, not a failure, and (like every other stage) persists once
`human_verified: true` is set. Every run also renders a **section-timeline
artifact** — `vgt/<namespace>/sections.txt`, under the project's `vgt/`
subfolder — so the detected structure can be checked by eye; it is vgt-owned
and regenerated each run, not committed alongside the project.

## Separate practice stems (Phase 2)

Stem separation is available only through the LALAL.AI v1 backend. Configure
`LALAL_LICENSE_KEY` in your shell or secret manager (never in a sidecar,
command history, fixture, or log), then declare whether the reference's guitar
is electric or acoustic:

```sh
vgt analyze --guitar electric "$PROJECT"
```

If the mix needs them, request additional paid separations explicitly. Use
`--extra-stem strings`, `--extra-stem piano` (or the alias `keys`), or repeat
the option for both. These are never included in the default recipe:

```sh
vgt analyze --guitar electric --extra-stem strings --extra-stem keys --accept-stem-cost "$PROJECT"
```

The first `vgt_initialize.lua` run also asks **Electric** or **Acoustic** and
persists the choice. Automation can set the `vgt`/`guitar_type` ExtState to
`electric` or `acoustic` before running that action. The CLI's `--guitar`
overrides and persists either saved value. If no value exists, an interactive
CLI prompts; non-interactive use must pass `--guitar` — it never silently
defaults before spending credits.

The command first saves the local tempo/key/sections/chords analysis, then
performs only the missing LALAL work. There is currently **no offline
backend**: Demucs, UVR, RoFormer, and similar local separation options are
explicitly deferred. If LALAL is unavailable or the key is missing, the local
analysis remains saved and the command reports the stem failure.

The standard paid recipe is fixed: five split operations produce these six artifacts.

| Operation | Source | Kept artifact(s) |
| --- | --- | --- |
| vocals | original mix | `vocals`, `instrumental` |
| bass | original mix | `bass` |
| drums | original mix | `drums` |
| declared electric/acoustic guitar | original mix | `guitar` |
| declared electric/acoustic guitar | `instrumental` | `backing` (no guitar) |

The final guitar-on-instrumental operation is the recipe's only cascade; every
other split uses the original mix. vgt checkpoints each operation in the
project's sidecar and validates its WAV artifacts before treating them as
cached. Re-running resumes incomplete work and reuses valid cached operations,
so it does not intentionally submit the same paid work twice.

Each opt-in extra adds one original-mix split and one artifact: `strings` →
`strings.wav`, and `keys`/`piano` → `piano.wav`. They appear in the same cost
preview and confirmation as the standard work. The sidecar persists selected
extras, so an interrupted request resumes rather than submitting it again.

`--force` is safe for normal analysis maintenance: it recomputes local analysis
only and **never spends LALAL credits**. To deliberately repeat paid stem
operations, use `--force-stems`. vgt shows the outstanding operation count and,
after LALAL's free preflight, its current balance and duration-based estimate
before asking for confirmation. Non-interactive use additionally requires the
explicit `--accept-stem-cost` acknowledgment for `--force-stems` and any
`--extra-stem` request:

```sh
vgt analyze --guitar acoustic --force-stems --accept-stem-cost "$PROJECT"
```

Generated output is project-local, not stored in this repository. The sidecar
records a stable namespace, and all generated artifacts stay below
`<project-folder>/vgt/<namespace>/` (including `stems/vocals.wav`,
`instrumental.wav`, `bass.wav`, `drums.wav`, `guitar.wav`, and
`backing-no-guitar.wav`, plus requested `strings.wav` and/or `piano.wav`). This keeps the media project-relative when the song
folder is moved or backed up.

After separation, run the same initialization ReaScript from **Actions → Show
action list → ReaScript: Load** again. Alongside the mirror and Phase 1
annotations, it imports the valid artifacts additively as `[vgt] Vocals`,
`[vgt] Instrumental`, `[vgt] Bass`, `[vgt] Drums`, `[vgt] Guitar`, and `[vgt]
Backing (no guitar)` tracks, plus `[vgt] Strings` and `[vgt] Keys / Piano`
when requested. Re-applying reconciles only vgt-owned tracks.

The live LALAL check is deliberately opt-in and never runs in CI because it
uses account credits. Account owners can follow the
[manual LALAL API v1 smoke-test procedure](docs/lalal-v1-smoke-test.md).

## Apply analysis (Phase 1)

Run the same ReaScript again after `vgt analyze`. It reads the sidecar and adds
`[vgt]` section regions plus a muted `[vgt] Chords` track whose text items
carry the detected beat-aligned labels. Unlike every other vgt-owned label
track, chord items are left **unlocked** — they are the editing surface for
chord corrections (see below). It sets vgt-owned audio items to time-based
positioning before any tempo changes.

When REAPER still has exactly its single default 120 BPM / 4/4 tempo marker,
the action writes the detected tempo map. If the project already has any other
tempo map, it leaves that map untouched and instead creates an unmuted, locked
`[vgt] Beats` item track so its labels remain readable. Both the regions and vgt tracks are reconciled on
re-apply; region ownership is recorded by REAPER region ID in the sidecar, so
even a user-created region named `[vgt] ...` is preserved.

For a saved-project check after analysis, use:

```sh
uv run python scripts/verify_phase1_apply.py "$PROJECT" \
  --baseline "test/Reaper Project/Reaper Project.RPP"
```

To run the full disposable REAPER proof (including both the default-map and
existing-map branches, each applied twice), use `--run-live` with the same
baseline argument instead of supplying a project path.

## Correcting chords and sections in REAPER

Auto-detected chords and sections are sometimes wrong, and correcting raw
second-offsets by hand in the sidecar is awkward. Instead, edit them directly
in REAPER, then run one action — `vgt sync` — to read every manual edit back
into the sidecar in a single pass.

**Chords**: edit items directly on the `[vgt] Chords` track's timeline — its
items are unlocked (though still muted, so they never play):

- **rename** an item's take to relabel a chord (e.g. `Am` → `A`);
- **move or resize** an item to adjust its boundaries;
- **split** an item (REAPER's native split action) to break one chord into two;
- **delete** an item to drop a spurious chord, or **add** a new item (with a
  take name) to insert one.

**Sections**: rename or move the `[vgt]` regions vgt created for detected
sections.

When done with either (or both), load
[reascript/vgt_sync.lua](reascript/vgt_sync.lua) in REAPER's Action List and
run it (or run `vgt sync [project.rpp]` for the same pointer from the CLI).
It:

- scans the `[vgt] Chords` track's items — position, length, take name — by
  GUID against the sidecar's `managed_track_guids`, so it only ever reads a
  chords track vgt itself created, and writes them back as
  `analysis.chords.value.segments` with `chords.human_verified: true`;
- scans regions by ID against the sidecar's `managed_region_ids`, never any
  other project region (including a user region whose name starts with
  `[vgt]`), and writes them back as `analysis.sections.value` with
  `sections.human_verified: true`;
- never touches `analysis.chords.detected` or `analysis.sections.detected` —
  the original machine detections stay available there (#19) — nor any other
  analysis stage (tempo, key) or the rest of the sidecar.

From then on: `vgt analyze` skips the chords and sections stages
(human-verified stages are never recomputed), and re-running the apply
ReaScript repaints `[vgt] Chords` and the section regions identically from
the corrected sidecar — the round trip is idempotent.

The correction round trip also has a disposable real-REAPER verifier. It
copies the included fixture, applies deterministic analysis, edits a chord
item and section region through REAPER's API, runs `vgt sync`, saves, and
checks that the human corrections changed only `value`, not `detected`:

```sh
uv run python scripts/verify_phase1_sync.py --run-live
```

## Check vgt state

`vgt status [project.rpp]` is read-only: it prints the project and sidecar,
reference track/source-file availability, managed area, each analysis stage,
timestamps, and the click/chord-sheet/section-timeline artifacts. It also
shows the selected guitar type; the five stem operations (including cached,
pending, in-progress, remote, or error state); and the six local stem artifact
paths and validity state. It never exposes the LALAL license key. In
particular, the `chords` line says whether it is only machine-detected or
human-corrected. Old sidecars that predate timestamps show `unknown` rather
than being changed.

Use `vgt status --json [project.rpp]` for the same information in a stable
machine-readable structure.
