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

## Planned — stem transcription (reference MIDI)

The next committed capability is **multi-target transcription of separated
stems into reference MIDI**, using Basic Pitch. See
[the transcription plan](transcription-plan.md) for the full design; its
milestones are tracked as GitHub issues T-A through T-F.

Scope, in one paragraph: `vgt analyze` gains a `transcription` stage that
transcribes requested stems — guitar by default, any of bass, vocals,
strings, piano, instrumental, backing, drums, or the mix on request — to MIDI
with pitch bends. Each target is cached, kept, and refreshed independently;
the set of wanted targets persists in the sidecar. The ReaScript action
imports each transcription as an unmuted, time-based `[vgt] … Ref (MIDI)`
track placed directly beneath the stem it came from.

Two consequences worth stating in the goal:

- **Transcription is local and free.** It needs none of the paid-work
  machinery: no cost confirmation, no lease, no credentials. Basic Pitch runs
  as an isolated `uvx` subprocess (it cannot install into vgt's interpreter),
  so no ML dependency enters vgt's own environment.
- **Reference MIDI is not correctable in this scope.** There is no `vgt sync`
  read-back for MIDI, so a `[vgt] … Ref (MIDI)` track is recreated on every
  apply like any other vgt-owned object. Editing it requires copying it to a
  user-owned track first, and that must be documented.

Explicitly not in this scope: tablature or string/fret assignment,
performance scoring, and MIDI correction read-back.

## Later — practice workflow *(to be planned)*

The practice workflow starts from the delivered baseline plus transcription.
Its scope has not yet been defined. Likely directions include
practice-oriented stem muting, looping, tempo management, and recording
support, but none are committed by this goal.

Offline separation, performance scoring, and new practice controls remain out
of the delivered baseline until explicitly planned.

Subjective listening evaluation of real stems remains a user-owned activity: it
requires human ears and the user's audio, and is not an autonomous-agent task.
