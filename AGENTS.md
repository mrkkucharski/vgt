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

3. **Do NOT set `status:blocked` because the issue has sub-tasks.** The orchestrator
   automatically holds a parent issue until all its sub-issues are closed; you do not
   need to (and must not) label it blocked for that reason. Using `status:blocked` for
   sub-task ordering will prevent the issue from being started even after sub-tasks
   finish. Only use `status:blocked` when you genuinely need a human decision or an
   external resource you cannot obtain yourself.

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
which issue to start next. **When you create sub-issues or follow-up issues, always
include a priority label:**

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

## Sub-issues express ordering

If an issue has sub-issues, the orchestrator will not start it until every one of its
sub-issues is closed. Break a large task into sub-issues when parts must be done first;
the sub-issues get picked up on their own, and the parent becomes startable once they're
all closed. **Never label a parent blocked just because its sub-issues are still open.**

## docs/ holds project docs

`docs/GOAL.md` defines what "done" means for the whole project -- read it before
deciding the project is finished or needs more work.

## .pm/ is orchestrator bookkeeping

`.pm/state.json` and `.pm/log.ndjson` are owned by the orchestrator. Don't hand-edit
these; the orchestrator owns them.

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
