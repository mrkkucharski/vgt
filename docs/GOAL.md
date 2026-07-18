# Goal

Describe what "done" means for this project. Once all issues are closed, the
orchestrator's goal-check step reads this file to decide whether the project is
finished, or whether more work (new issues) is needed. Completion also requires a
machine-executable `verification_command` configured in proj-mgr's registry (for
new projects, pass it to `pm open --verification-command '...'`). The command runs
from this repository's root and must exit 0; no command means the project remains
active and proj-mgr creates a follow-up issue.

Be concrete and falsifiable, e.g.:

- "Ship a CLI that does X, Y, Z, with tests passing."
- "Not done until deployed and monitored in production for a week with no P1
  incidents."

Replace this placeholder before starting work on the project.
