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
changed. Pass `--force` to recompute every stage regardless of the cache —
useful after changing a detector's code rather than its inputs. Any stage can
be corrected by hand-editing its `value` in the sidecar and setting
`human_verified: true`; `vgt analyze` then leaves that stage untouched on
every later run, regardless of what the audio hash says — even under `--force`.

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

Every run also renders a **click-only artifact** — `<project>.vgt-tempo-click.wav`,
next to the sidecar — a bare click on the detected beat grid (the reference
audio is not mixed in) so the grid can be checked by ear or lined up against
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
- **Last resort: a chroma + template classifier** (librosa chroma, the 24
  maj/min triad templates, majority-vote smoothed across neighboring beats)
  — always available, and what actually runs in most dev/CI environments
  since neither of the above is typically installed.

Only major/minor triads are ever recognized — 7ths, sus, add9, etc. all
collapse to their nearest maj/min match — so every result is flagged
`"vocabulary": "maj_min"`.

Every run that (re)computes `value` also renders a **chord-sheet artifact** —
`<project>.vgt-chords.txt`, next to the sidecar — a plain-text timestamp +
chord-label listing of `value`, for by-eye verification.

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
artifact** — `<project>.vgt-sections.txt`, next to the sidecar — so the
detected structure can be checked by eye; it is vgt-owned and regenerated
each run, not committed alongside the project.

## Apply analysis (Phase 1)

Run the same ReaScript again after `vgt analyze`. It reads the sidecar and adds
`[vgt]` section regions plus a muted `[vgt] Chords` track whose text items
carry the detected beat-aligned labels. Unlike every other vgt-owned label
track, chord items are left **unlocked** — they are the editing surface for
chord corrections (see below). It sets vgt-owned audio items to time-based
positioning before any tempo changes.

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

To run the full disposable REAPER proof (including both the default-map and
existing-map branches, each applied twice), use `--run-live` with the same
baseline argument instead of supplying a project path.

## Correcting chords in REAPER

Auto-detected chords are sometimes wrong, and correcting raw second-offsets
by hand in the sidecar is awkward. Instead, edit the chords directly on the
`[vgt] Chords` track's timeline — its items are unlocked (though still muted,
so they never play):

- **rename** an item's take to relabel a chord (e.g. `Am` → `A`);
- **move or resize** an item to adjust its boundaries;
- **split** an item (REAPER's native split action) to break one chord into two;
- **delete** an item to drop a spurious chord, or **add** a new item (with a
  take name) to insert one.

When done, load [reascript/vgt_read_chords.lua](reascript/vgt_read_chords.lua)
in REAPER's Action List and run it (or run `vgt read-chords [project.rpp]`
for the same pointer from the CLI). It scans the `[vgt] Chords` track's items
— position, length, take name — by GUID against the sidecar's
`managed_track_guids`, so it only ever reads objects vgt itself created, and
writes them back into the sidecar as `analysis.chords.value.segments` with
`chords.human_verified: true`. It never touches `analysis.chords.detected` —
the original machine detection stays available there — nor any other
analysis stage (tempo, key, sections) or the rest of the sidecar.

From then on: `vgt analyze` skips the chords stage (human-verified stages are
never recomputed), and re-running the apply ReaScript repaints `[vgt] Chords`
identically from the corrected sidecar — the round trip is idempotent.
