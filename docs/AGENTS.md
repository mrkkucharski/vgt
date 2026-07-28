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
all non-destructively and idempotently. See `docs/GOAL.md` for scope and
`docs/USER-MANUAL.md` for current user-visible behavior, including the
LALAL-only stem workflow.

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

## Domain rules

- **Non-destructive & idempotent** are hard invariants — never overwrite,
  rename, or delete tracks/objects vgt didn't create; re-running a command must
  not duplicate or corrupt anything.
- Prefer manipulating the project through the REAPER API (ReaScript action) over
  editing `.RPP` text, so changes appear live in the user's open project and
  REAPER handles construction correctly.
