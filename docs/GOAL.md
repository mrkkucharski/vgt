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
  human chord, section, and key corrections through `vgt sync`. A separate,
  confirmation-gated ReaScript synchronizes a deliberate REAPER tempo-map
  correction as a reference-relative grid; ordinary sync never reads tempo
  markers.
- **Supporting capability — stem separation:** LALAL.AI v1 separates the
  standard vocals/instrumental/bass/drums/guitar/backing set, with optional
  strings and piano. It is a delivered capability, not a numbered phase.
- **Supporting capability — reference transcription:** requested separated
  stems are transcribed locally into cached reference MIDI variants and
  imported beside their source stem. A target can retain several generated
  candidates with immutable IDs and editable labels, ordered only for stable
  presentation -- none is preferred, active, best, or selected; cleanup-only
  guitar variants share raw Basic Pitch detection. DrumScript transcribes
  `drums` by default; an opt-in ADTOF profile can be retained beside it as a
  separate drums variant. Basic Pitch transcribes every other supported target (guitar,
  bass, vocals, piano, strings, instrumental, backing, and original mix).
  Generated variants are draft
  references, not chord-analysis inputs or ground truth; `[work]` copies are
  the separate user-owned editing boundary. Guitar is the default target;
  further targets can be kept independently. This is a delivered capability,
  not a numbered phase.

See [the user manual](USER-MANUAL.md) for commands, REAPER object states,
correction workflow, cost controls, and the current regression contract.

## Executable acceptance test

`tests/test_goal_contract.py` is the offline executable acceptance test for
this goal. It copies the real RPP fixture and drives initialization, fake
LALAL separation, fake transcription, variant lifecycle operations, ReaScript
apply and sync, CLI paid-stem consent/quote/cache/checkpoint-recovery paths,
and reconciliation passes. It proves the
non-destructive/idempotent object contract, correction and detected-baseline
behavior, non-invasive tempo fallback, raw-detection cache reuse, exact
discarded-artifact/cache cleanup, paid-work refusal without explicit
non-interactive consent, no duplicate paid submission after
checkpoint recovery, credential non-persistence, and preservation of user
tracks plus a simulated `[work]` copy, canonical `[clean]`/`[work]`/`[vgt]`
container layout and ordering, and promotion into `[clean]`. It is run locally
as the source of truth
for the offline acceptance suite. Hosted GitHub Actions is intentionally not
part of this hobby project's verification path because hosted runners are
billing-blocked; run the complete suite locally instead. It deliberately
requires no credentials, network access, model downloads, or live REAPER.

The current committed local certification, including the exact source commit,
commands, environment, pass counts, package evidence, and executable-coverage
audit, is [the offline goal-verification record](GOAL-VERIFICATION-2026-07-29.md).

## Permanent invariants

- **Non-destructive, with an explicit working-copy boundary:** automatic
  initialize/apply reconciliation changes only `[vgt]`-managed objects. It
  may create, rename, recolour, and reposition the `[clean]` and `[work]`
  container tracks, and reposition their blocks as a unit, but it never
  modifies, renames, deletes, or reorders anything inside either container.
  The separately user-invoked working-copy action is the sole exception: create
  may affect only the copies it creates, and promote may affect only selected
  tracks that both retain its durable working-copy mark and still start with
  `[work]`. Promotion may move and rename those selected tracks into `[clean]`;
  every unselected, ineligible, or reclaimed track remains untouched. If the
  requested create or move would require changing an existing container child
  merely to maintain REAPER folder structure, the action refuses unchanged.
- **Idempotent:** re-running a workflow reconciles vgt-owned state without
  duplicates or corruption.
- **Live REAPER mutation:** project changes use REAPER's API, never RPP text
  editing.
- **Analysis outside REAPER:** CPU-heavy DSP/ML stays in the Python CLI.
- **Correctable:** human-synchronized chord, section, and key edits survive
  future runs; machine detections remain available as a baseline. A deliberate
  tempo-map correction is synchronized only by the separate
  confirmation-gated action, never by ordinary correction sync.
- **Separate ownership and evidence:** generated variants can be reconciled or
  discarded and are peers -- ordered only for stable presentation, never
  preferred, active, best, or selected -- while `[work]` copies remain
  user-owned, distinguished by ownership/provenance, never color. Automatic
  chord analysis stays audio-based; clean MIDI is a useful draft, never
  ground truth.
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
In particular, the round-three spectral ghost gate has synthetic coverage but
has not been re-measured on the real 7Rivers stem; the manual records the
human listening/live-REAPER checklist for that limitation.
