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
uses a `[vgt]`-managed REAPER area for its mirrored reference, annotations, and
stems — all non-destructively and idempotently. See `docs/GOAL.md` for the
exact phase scope and `docs/stem-separation-plan.md` for the fixed Phase 2
recipe and its LALAL-only limitation.

## Environment assumptions

- **REAPER is installed on the machine.** You may assume a working REAPER
  install (target: 7.x; the test fixture was saved with 7.65) at the standard
  macOS location `/Applications/REAPER.app`, with the CLI binary at
  `/Applications/REAPER.app/Contents/MacOS/REAPER`. It is fine to invoke REAPER
  (e.g. to run a ReaScript action) or rely on its presence in tests; you do not
  need to bundle, download, or install it.
- Platform is macOS (Apple Silicon). Python 3.11 + `uv` per the plan's stack.

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
