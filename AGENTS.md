# AGENTS.md

This repository is managed autonomously by [proj-mgr](https://github.com/mrkkucharski/proj-mgr).
An orchestrator periodically checks this repo's GitHub issues and resumes/starts an
agent (you) to work through them, without a human in the loop on every step.

## How work is organized

- **GitHub Issues are tasks.** Each open issue carries labels that tell the orchestrator
  and every agent its current state. Labels must be kept accurate at all times.

### Status labels

Every open issue must have exactly one `status:*` label at all times. These are set
by the orchestrator (when starting, reviewing, or fixing work) and by you (when
finishing, blocking, or handing off). Keep them in sync with reality:

| Label | Meaning | Who sets it |
|---|---|---|
| `status:queued` | Not started yet, waiting to be picked up | Orchestrator / humans |
| `status:working` | An agent is actively working on it right now | Orchestrator (on start) |
| `status:ready-for-review` | Implementation complete, waiting for a reviewer | Implementer agent |
| `status:needs-fix` | Reviewer rejected it; implementer must fix | Reviewer agent |
| `status:blocked` | Needs a human decision or external resource | Implementer/fix agent |

**IMPORTANT -- label cleanup rules you must follow:**

1. **When you finish and mark ready-for-review:** remove `status:working`.
   ```
   gh issue edit <N> --repo <repo> --add-label status:ready-for-review --remove-label status:working
   ```

2. **When you get blocked:** remove the current work label too, not just add blocked.
   ```
   gh issue edit <N> --repo <repo> --add-label status:blocked --remove-label status:working
   ```
   (Use `--remove-label status:needs-fix` instead if you were in a fix cycle and
   the orchestrator had already replaced `status:needs-fix` with `status:working`
   before you started -- the label on the issue when you receive it is `status:working`.)

3. **Do NOT set `status:blocked` to express ordering.** The orchestrator already
   holds an issue back while it has open "blocked by" dependencies or open
   sub-issues; you do not need to (and must not) label it blocked for that reason.
   Only use `status:blocked` when you genuinely need a human decision or an external
   resource you cannot obtain yourself. See "Ordering new issues" below.

4. **Reviewer agents -- when you accept and close an issue:** also remove `status:working`.
   The orchestrator sets `status:working` when it starts a review session. Closing the
   issue without removing the label leaves it visually stale.
   ```
   gh issue close <N> --repo <repo>
   gh issue edit <N> --repo <repo> --remove-label status:working
   ```

5. **Reviewer agents -- when you reject:** remove `status:working`, not `status:ready-for-review`
   (the orchestrator already removed that when it started your review session).
   ```
   gh issue edit <N> --repo <repo> --add-label status:needs-fix --remove-label status:working
   ```

### Priority labels

Every issue should carry a `priority:*` label. The orchestrator uses it to decide
which issue to start next. **Whenever you create an issue, always include a priority
label:**

```
gh issue create --repo <repo> --title "..." --body "..." \
  --label status:queued --label priority:medium
```

| Label | Meaning |
|---|---|
| `priority:high` | Blocker; start as soon as possible |
| `priority:medium` | Important but not an emergency |
| `priority:neutral` | Routine work (default when absent) |
| `priority:low` | Nice-to-have, defer when higher-priority work exists |
| `priority:optional` | Stretch goal; only if nothing else is queued |

Numeric labels (`priority:1` through `priority:5`) are also accepted, where 5 = high
and 1 = optional.

### Agent labels

`implementer:claude` and `implementer:codex` are set by the orchestrator to record
which agent last implemented or fixed an issue. `reviewer:claude` and
`reviewer:codex` record which agent last reviewed it. Do not set or remove these
yourself.

### Fix-count labels

`fix-count:N` is set by the orchestrator to track how many review/fix cycles an issue
has gone through. Do not set or remove these yourself.

## Ordering new issues

**Use GitHub's "blocked by" dependencies to express order. Do not create sub-issues.**

The orchestrator will not start an issue while any issue it is blocked by is still
open. That is the single ordering mechanism here: a flat set of issues plus explicit
dependencies, no hierarchy.

There is no `gh issue edit` flag for this -- it is a REST call, and it takes the
blocker's **numeric id, not its issue number**, so it is a two-step recipe:

```
BLOCKER_ID=$(gh api repos/<repo>/issues/<blocker-number> --jq .id)
gh api -X POST repos/<repo>/issues/<issue-number>/dependencies/blocked_by -F issue_id=$BLOCKER_ID
```

Use `-F` (typed), not `-f` (string) -- the API rejects a string id. To check what an
issue is blocked by:

```
gh api repos/<repo>/issues/<issue-number>/dependencies/blocked_by --jq '.[].number'
```

**Create issues in dependency order: the ones that depend on nothing first.** The
orchestrator may start any issue that currently looks unblocked, and it can begin
between two of your `gh` calls. So create a blocker before the issue it blocks, and
record the dependency immediately after creating the issue that has it -- do not
create a batch of issues and wire them up at the end. (Newly filed issues are held
briefly before they can start, which covers the gap between those two calls, but the
window is short and is not a substitute for creating things in the right order.)

**Never label an issue `status:blocked` for ordering.** Dependency-gated issues stay
`status:queued`; the orchestrator does the holding. `status:blocked` means a human
decision or an external resource is needed. (If an issue does end up blocked with
dependencies recorded, the orchestrator returns it to the queue once they all close,
so it will not stay stuck -- but do not rely on that instead of labeling correctly.)

Sub-issues are still honored if they exist: the orchestrator will not start a parent
while any of its sub-issues is open. Don't create new ones.

## docs/ holds project docs

`docs/AGENTS.md` holds this project's own context and conventions;
`docs/PRIORITIES.md` (if present) holds the local rules the orchestrator uses to
choose between queued issues. The scope of the project is expressed as GitHub
issues, not as a doc -- there is no project-level "done" file to consult.

## No hosted CI

GitHub Actions is disabled for this repository, deliberately. Do not enable it, do
not add files under `.github/workflows/`, and do not wire up any other hosted CI
service. You verify your own work by running the project's tests and checks during
your turn and reporting what you ran -- that is the whole verification story here.

The orchestrator re-asserts the disabled setting on every scheduler pass, so
turning Actions on will be reverted and recorded in `.pm/log.ndjson`. If an issue
seems to require CI, mark it `status:blocked` with an explanation instead of
setting one up.

## .pm/ is orchestrator bookkeeping

`.pm/state.json` and `.pm/log.ndjson` are owned by the orchestrator. Don't hand-edit
these; the orchestrator owns them.

`.pm/log.ndjson` and `.pm/runs/` (per-run agent transcripts) are gitignored: they are
written locally for post-mortems but never committed, since they grow without bound
and nothing reads them back from the repo. Don't add them to version control.

## RULES.md

`RULES.md` (if present) is the shared rule set inherited from proj-mgr: scope
discipline, git hygiene, no silent scope changes, no secrets, log everything, stay bounded.

## Ground rules

- Work only on the issue you were assigned. Read its title, body, and prior comments first.
- Commit as you go with clear messages; push when you reach a stable point.
- Keep labels accurate throughout your work (see Status labels section above).
- Never force-push, never rewrite other issues' history, never touch secrets/`.env` files.

## Project-specific rules

Read docs/AGENTS.md for the project context and any project-specific rules and
conventions. It is the place to record how *this particular* project works --
its architecture, file layout, naming conventions, domain rules, and any
per-command guidance -- as opposed to the orchestration rules above, which are
the same for every proj-mgr project.
