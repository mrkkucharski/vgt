# Project-specific agent instructions

This file provides project-specific guidance to the coding agent (you) when
working in this repository, in addition to the AGENTS.md located at the repo
root. The root AGENTS.md covers how proj-mgr orchestration works and is the same
for every managed project; this file is where *this* project's own context and
rules live.

## Project overview

**vgt** (virtual guitar teacher) is a CLI that prepares REAPER projects for
guitar practice. Delivered work covers project plumbing, reference analysis,
and LALAL.AI stem separation; vgt persists its own state beside the project and
uses a `[vgt]`-managed REAPER area for its reference annotations and stems —
all non-destructively and idempotently. See `docs/USER-MANUAL.md` for scope
and current user-visible behavior, including the LALAL-only stem workflow.

**Before changing how any instrument is transcribed**, read
`docs/instrument-transcription-findings.md`. It indexes the per-instrument
evidence (which backend each target uses, how good it measurably is, what is
still unmeasured), records the lessons that already generalize across
instruments, and defines the measurement method and probe scripts a new
instrument's investigation is expected to follow. Retuning a profile without
measuring against a reference — or measuring with a metric that does not
penalize over-detection — has produced wrong conclusions here before; that
document exists to stop it recurring.

## Environment assumptions

- **REAPER is installed on the machine.** You may assume a working REAPER
  install (target: 7.x; the test fixture was saved with 7.65) at the standard
  macOS location `/Applications/REAPER.app`, with the CLI binary at
  `/Applications/REAPER.app/Contents/MacOS/REAPER`. It is fine to invoke REAPER
  (e.g. to run a ReaScript action) or rely on its presence in tests; you do not
  need to bundle, download, or install it.
- Platform is macOS (Apple Silicon). Python 3.11 + `uv` per the plan's stack.

## Human-owned REAPER verification

Live verification in REAPER is always performed by the human/user. Agents must
not create, start, or execute issues whose purpose or acceptance criteria
require opening REAPER, running a ReaScript against a live project, or manually
checking REAPER output. Keep such work out of the autonomous issue queue and
document any required user verification in the relevant user-facing guidance.

## Do not rely on GitHub Actions / CI

**GitHub Actions is non-functional on this account**: hosted-runner jobs fail
instantly on an account billing block. The owner has decided that this
non-commercial hobby project will not rely on GitHub Actions; see issue #195.

- **Do not depend on CI to prove your work.** Run the full offline suite
  **locally** (`pytest`, the goal contract in `tests/test_goal_contract.py`,
  linters/type-checks) and paste the local results into the issue as evidence.
  Never mark an issue ready-for-review on the assumption that "CI will catch it."
- **Do not add, expand, or re-wire GitHub Actions workflows** as a way to
  satisfy acceptance criteria, and do not treat a green/red Actions run as the
  bar — there are no runs. Editing workflow YAML changes nothing while billing
  is blocked.
- Keep tests runnable and fast **offline** (no network, no hosted runner), so
  local execution is the source of truth.
- If an issue's acceptance genuinely requires a passing Actions run, it is
  blocked on #195 — say so and stop, rather than working around it.

## Test project

A real REAPER project fixture lives at **`test/Reaper Project/`** — use it for
developing and testing project locating, reading, and non-destructive
augmentation against a genuine `.RPP` (not a synthetic one).

- `Reaper Project.RPP` — REAPER 7.74 project, 3 tracks: `Click`, and two song
  tracks `The Seven Rivers (Full March - 3_00)` and `Paris Metro Punk`.
- `Media/*.mp3` — the two song audio files, referenced by the `.RPP` via
  **relative** paths (`FILE "Media/..."`), so they resolve regardless of
  checkout location. `Click` is a REAPER-native click source with no file.
- REAPER-generated peak caches (`Media/peaks/`, `*.reapeaks`) and auto-backups
  (`Backups/`) are **git-ignored** — REAPER regenerates peaks on open, so don't
  rely on them being present and don't commit them.

Treat this as a **read-mostly fixture**: exercise vgt against a copy, and never
commit vgt's mutations back into `test/`. The tracks here are the user's own —
per the non-destructive rule, vgt must only ever touch `[vgt]`-prefixed objects
it creates.

## Conventions

- Everything vgt creates in a REAPER project carries a `[vgt]` prefix (tracks,
  regions, folders); only `[vgt]`-owned objects may be modified or removed.
- Commit style: conventional commits (see git history).

## Permanent invariants

These are hard rules, not preferences. Source comments in `src/vgt/` and
`reascript/` refer back to them by name, and `tests/test_goal_contract.py` is
their offline executable contract: it drives the real RPP fixture through
initialization, separation, transcription, variant lifecycle, apply/sync, and
reconciliation to prove they hold. Change one of these only on an explicit human
decision, and update the contract in the same change.

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
- **Live REAPER mutation:** project changes use REAPER's API (a ReaScript
  action), never `.RPP` text editing, so changes appear live in the user's open
  project and REAPER handles construction correctly.
- **Analysis outside REAPER:** CPU-heavy DSP/ML stays in the Python CLI.
- **Correctable:** human-synchronized section edits survive future runs and
  retain a machine-detected baseline. Chord and key tracks are disposable
  audio-derived drafts; preserve a correction by promoting a working copy into
  `[clean]`. A deliberate tempo-map correction is synchronized only by the
  separate confirmation-gated action, never by ordinary correction sync.
- **Separate ownership and evidence:** generated variants can be reconciled or
  discarded and are peers -- ordered only for stable presentation, never
  preferred, active, best, or selected -- while `[work]` copies remain
  user-owned, distinguished by ownership/provenance, never color. Automatic
  chord analysis stays audio-based; clean MIDI is a useful draft, never
  ground truth.
- **Respect the project:** automatic initialize/apply never creates, updates,
  deletes, or claims a REAPER tempo map; analyzed beats are presented only as
  non-invasive labels on `[vgt] Beats`. A deliberate user-authored tempo map
  may be read only by the separate confirmation-gated synchronization action.
- **Cost safe:** LALAL credentials are environment-only; paid work is cached,
  checkpointed, and explicitly confirmed when forced or optional.

## Retired designs

The first practice-workflow milestone (a guided practice session: looping a
section against a backing stem, muting the reference mix) was designed twice
(issues #89 and #105) and both times spawned implementation sub-issues that
closed without landing code on `main`. It is **not** planned work. See
[the retired milestone design](practice-workflow-milestone.md) for the abandoned
design-of-record; do not resurrect it into new issues without an explicit human
decision to actually build it.
