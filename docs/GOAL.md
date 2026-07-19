# Goal

## Background
We are building a (to some extent still experimental) virtual guitar teacher. We are at the initial phase.
The user interacts with vgt either from the command line or from the REAPER desktop app.

In REAPER the user has typically one original song; in the future there is a possibility of having more than
one. That song may have adjusted pitch, be normalized, or have some marks — but otherwise it is not expected to
have significant modifications. This is the **reference song**.

The student works on separate tracks — typically split into several stems:
a) vocals
b) drums
c) bass
d) guitar(s)
e) rest of the instruments

For practicing, the student may want to mute the original track, change play speed (preserving pitch), adjust
the volume of specific parts (e.g. volume up on bass and drums, volume down on guitar) etc. — so they can play
along with specific track sets. And so on.

The whole initiative is divided into sub-projects or phases. Each phase builds an incremental piece of the
functionality.

## Phases

The initiative is built in incremental phases. Each adds a self-contained slice and builds on the `[vgt]`
conventions and the `<project>.vgt` sidecar established by the phases before it. "Done" for the whole project is
the union of every phase's deliverables below.

### Phase 0 — REAPER-plumbing foundation *(delivered)*

Establishes how vgt locates, opens, and safely augments a REAPER project, and where vgt stores its own state.
Shipped:
- **locating a REAPER project** — accept a project path as input;
- **defaulting to the current project** — when no project is given and there is already a REAPER project in the
  working directory, assume the user wants to work with that one;
- **opening the project and reading relevant information** — parse the project and read at least: sample rate,
  tempo/time-signature, and the existing track list (names and GUIDs) so vgt can tell which tracks are already
  vgt-managed;
- **storing vgt settings next to the REAPER project** — a single sidecar file living alongside the
  `.RPP` and sharing its name with a `.vgt` extension (e.g. `Reaper Project.RPP` → `Reaper Project.vgt`), holding
  vgt's own state (schema version, the vgt-managed track GUIDs it created, and any config);
- **preparing a "vgt managed" area for practice tracks** — a REAPER track folder (and a `[vgt]` name prefix on
  every track/region vgt creates) so a student can immediately identify what vgt owns;
- **choosing a reference track and mirroring it** — the apply action asks which of the project's own tracks is
  the reference, names the managed folder after it (`[vgt] <reference track name>`), and mirrors that one track's
  media into the managed area.

### Phase 1 — Reference analysis *(current)*

Analyze the chosen reference track — **the full mix; no stem separation yet** — and record what vgt learns about
the song, both in the sidecar and as `[vgt]`-owned objects in the project. From the reference audio, detect:
- **tempo & beat grid** — BPM, downbeat offset, and time signature;
- **key** — root + scale (major/minor);
- **sections** — intro / verse / chorus / … boundaries;
- **chords** — beat-aligned chord labels (maj/min vocabulary).

Phase 1 delivers:
- **an analysis stage in the CLI** — heavy analysis runs in the Python CLI, never inside REAPER; the ReaScript
  action passes the reference track's source-file path (already identified in phase 0) to the CLI;
- **persisting analysis in the sidecar** — extend the `<project>.vgt` schema (bump `schema_version`) to hold the
  detected tempo/key/sections/chords alongside phase 0's state;
- **reflecting analysis into the project**, additively and idempotently, as `[vgt]`-owned objects:
  - a **tempo map** — subject to the tempo-map rule: written only when the project still has a single default
    tempo marker; otherwise left untouched and offered non-invasively (a muted `[vgt]` beat-marker track);
  - `[vgt]` **section regions** (intro/verse/chorus/…), renamable by the user;
  - a **muted `[vgt] Chords`** track carrying the beat-aligned chord labels as text items, editable on the
    timeline and read back into the sidecar as human-verified corrections (see README's "Correcting chords in
    REAPER").

**Out of scope for phase 1** (deferred to later phases): stem separation, guitar MIDI reference generation,
practice controls (stem muting / looping / tempo-ramp), and performance scoring. The full song-prep pipeline in
[phase1-song-prep-plan.md](phase1-song-prep-plan.md) is the longer-range roadmap; phase 1 here is its analysis
slice with separation and the MIDI reference intentionally deferred.

## Requirements

- **Non-destructive.** Never overwrite, rename, delete, or modify any track or object vgt did not create.
  Everything vgt creates carries the `[vgt]` prefix; only `[vgt]`-owned objects are ever touched. A re-apply
  first removes only its own `[vgt]` objects before recreating them.
- **Respect REAPER settings.** Open and read the REAPER project (and relevant preferences) and honor them —
  do not fight the project's existing sample rate, tempo, etc.
- **Prefer live, in-app manipulation.** The intended mechanism is to manipulate the project through the REAPER
  API via a ReaScript action (not by editing `.RPP` text), so the modifications appear immediately in the user's
  open project and REAPER handles project construction correctly. Where a piece is not yet technically feasible
  in-app, note it explicitly rather than silently falling back to text edits.
- **Analysis stays out of the DAW process.** ML/DSP analysis runs in the Python CLI; REAPER only reads the
  reference source path and applies results through the API. The DAW never loads heavy analysis dependencies.
- **Correctable.** Auto-analysis will sometimes be wrong; every detected value (tempo, key, sections, chords) is
  overridable, and corrections survive re-runs of the analysis.
- **CLI-initiated.** A command-line interface initiates the actions, with the intent that it be invoked from a
  ReaScript action inside REAPER. The ReaScript wrapper is a thin caller; the CLI does the work.
- **Idempotent.** Running the same command twice produces the same result — no duplicate tracks, folders, regions,
  or settings. Re-running reconciles vgt's managed area to the intended state.
- **Documented.** There is documentation describing how vgt works and how to run it.
