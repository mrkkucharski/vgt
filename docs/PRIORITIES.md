# Issue Priorities

<!-- Optional. When this file exists, the orchestrator asks an agent to pick the
     next queued issue using the rules below, instead of falling back to the
     highest priority: label alone. Delete this file to use label-based ordering. -->

When no review or fix is pending, choose the next issue in this order:

1. **High-priority label** (`priority:high` / `priority:5`) — urgent fixes and blockers.
2. ... add project-specific rules here ...
3. **Low-priority label** (`priority:low` / `priority:1`) — optional enhancements.

If no issue fits a category above, fall back to the highest `priority:` label.
