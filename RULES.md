# Global rules for autonomously-managed projects

These rules apply to every project managed by proj-mgr, in addition to whatever is in
that project's own `AGENTS.md`. They are copied into each new project repo as
`RULES.md` at creation time.

1. **Scope discipline.** Only touch what the current issue requires. Don't refactor,
   reorganize, or "clean up" unrelated code while working an issue.
2. **Git hygiene.** Never force-push. Never amend or rewrite commits other than your
   own most recent uncommitted work. Commit in small, reviewable chunks.
3. **No silent scope changes.** If the issue as written seems wrong, too big, or
   ambiguous, say so in an issue comment and mark `status:blocked` rather than
   guessing.
4. **Secrets.** Never read, print, log, or commit secrets/credentials/`.env` files. If
   a task appears to require credentials you don't have, mark `status:blocked`.
5. **Logging.** Every meaningful state change (issue created/updated, artifact
   produced, decision made) should be visible in the issue thread and/or
   `.pm/log.ndjson` history -- not just implicit in code changes. Future ticks (and
   the human owner) rely on this trail.
6. **Done means verified + published.** Before considering an issue finished, run the
   relevant tests/checks, confirm they pass, commit the work, and push it. Only then
   close the issue. If tests fail or publish/close steps fail, don't treat the issue
   as done.
7. **Stay bounded.** Don't try to do the whole project in one sitting. Do a coherent
   chunk of work, leave the repo in a working state, and stop -- you'll be resumed
   automatically.
