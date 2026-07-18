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

## Scope of this project (phase 0)

**This project is phase 0: the REAPER-plumbing foundation only.** It establishes how vgt locates, opens, and
safely augments a REAPER project, and where vgt stores its own state. The full analysis pipeline described in
[phase1-song-prep-plan.md](phase1-song-prep-plan.md) — stem separation, beat/tempo maps, key/chord detection,
sections, MIDI reference generation, the manifest contract — is **out of scope for phase 0**. That plan is
context for where we are heading, not a description of what to build now.

Phase 0 delivers:
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
- **creating one track in the vgt-managed area** — initially this track simply mirrors the original song.

## Requirements

- **Non-destructive.** Never overwrite, rename, delete, or modify any track or object vgt did not create.
  Everything vgt creates carries the `[vgt]` prefix; only `[vgt]`-owned objects are ever touched. A re-apply
  first removes only its own `[vgt]` objects before recreating them.
- **Respect REAPER settings.** Open and read the REAPER project (and relevant preferences) and honor them —
  do not fight the project's existing sample rate, tempo, etc.
- **Prefer live, in-app manipulation.** The intended mechanism is to manipulate the project through the REAPER
  API via a ReaScript action (not by editing `.RPP` text), so the modifications appear immediately in the user's
  open project and REAPER handles project construction correctly. Phase 0 should establish this path; where a
  piece is not yet technically feasible in-app, note it explicitly rather than silently falling back to text
  edits.
- **CLI-initiated.** A command-line interface initiates the actions, with the intent that it be invoked from a
  ReaScript action inside REAPER in the future. Phase 0 builds the CLI; the ReaScript wrapper is a thin caller
  that later phases flesh out.
- **Idempotent.** Running the same command twice produces the same result — no duplicate tracks, folders, or
  settings. Re-running reconciles vgt's managed area to the intended state.
- **Documented.** There is documentation describing how vgt works and how to run it.
