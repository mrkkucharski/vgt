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

## Practice workflow (not planned)

A guided practice session (looping a section against a backing stem, muting
the reference mix, etc.) was designed twice (issues #89 and #105) and both
times spawned implementation sub-issues that closed without landing code on
`main`. It is intentionally **not** part of the current goal. See
[the retired milestone design](practice-workflow-milestone.md) for the
abandoned design-of-record; do not resurrect it into new issues without a
human decision to actually build it.

Subjective listening evaluation of real stems remains a user-owned activity: it
requires human ears and the user's audio, and is not an autonomous-agent task.
