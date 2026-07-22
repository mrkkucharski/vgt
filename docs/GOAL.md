# Goal

vgt is a virtual guitar teacher that non-destructively prepares existing REAPER
projects for practice. A user selects one file-backed reference mix; vgt
analyzes it, optionally creates practice stems, and adds a clearly owned
`[vgt]` area to the live project.

## Delivered baseline

- **Phase 0 — project integration:** inspect and locate RPP projects, select a
  reference track, keep state in the adjacent `<project>.vgt` sidecar, and
  maintain an idempotent `[vgt]` area through REAPER.
- **Phase 1 — reference analysis:** detect tempo/beat grid, key, sections, and
  beat-aligned major/minor chords; show the results in REAPER; and preserve
  human chord/section corrections through `vgt sync`.
- **Supporting capability — stem separation:** LALAL.AI v1 separates the
  standard vocals/instrumental/bass/drums/guitar/backing set, with optional
  strings and piano. It is a delivered capability, not a numbered phase.
- **Supporting capability — reference transcription:** requested separated
  stems are transcribed locally into cached, per-target reference MIDI and
  imported beside their source stem. DrumScript transcribes `drums`; Basic
  Pitch transcribes every other supported target (guitar, bass, vocals,
  piano, strings, instrumental, backing, and original mix). Guitar is the
  default target; further targets can be kept independently. This is a
  delivered capability, not a numbered phase.

See [the user manual](USER-MANUAL.md) for commands, REAPER object states,
correction workflow, cost controls, and the current regression contract.

## Permanent invariants

- **Non-destructive:** vgt changes only objects it created and recorded as
  `[vgt]`-managed; it never changes user tracks, items, or regions.
- **Idempotent:** re-running a workflow reconciles vgt-owned state without
  duplicates or corruption.
- **Live REAPER mutation:** project changes use REAPER's API, never RPP text
  editing.
- **Analysis outside REAPER:** CPU-heavy DSP/ML stays in the Python CLI.
- **Correctable:** human-synchronized chord and section edits survive future
  runs; machine detections remain available as a baseline.
- **Respect the project:** vgt does not overwrite an existing or human-edited
  tempo map; it falls back to non-invasive beat labels.
- **Cost safe:** LALAL credentials are environment-only; paid work is cached,
  checkpointed, and explicitly confirmed when forced or optional.

## Phase 2 — first practice workflow

The first practice session is intentionally narrow: with an analysed section
and a guitar-less `Backing` stem available, the user manually mutes their
original reference mix, starts a vgt-owned section loop, and practises against
the backing stem. A new REAPER action persists the `guitar-backing` preference,
temporarily mutes only vgt-owned audio stems, applies a selected effective
section as the time-selection loop, and can restore exactly the previous loop
and vgt stem-mute state. It never modifies a user track, routing, tempo map,
playback speed, recording setup, or transport state.

The detailed ownership, reconciliation, timing, testing, and human-owned
verification contract is [the first practice-workflow milestone design](practice-workflow-milestone.md).

Stem soloing, arbitrary time-selection looping, vgt-controlled playback speed
or tempo, and recording preparation are explicitly deferred from Phase 2.
Offline separation, tablature/string-fret assignment, performance scoring,
MIDI correction read-back, and unrelated DrumScript improvements remain outside
this milestone.

Subjective listening evaluation of real stems remains a user-owned activity: it
requires human ears and the user's audio, and is not an autonomous-agent task.
