"""CLI entry point. RPP inspection is local; mutation is delegated to REAPER."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .project import ProjectError, locate_project, read_project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vgt", description="Safely prepare a REAPER project for vgt.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="Report read-only REAPER project metadata.")
    inspect.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    apply = subparsers.add_parser("apply", help="Prepare the open project through the bundled ReaScript action.")
    apply.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Phase 0's primary invocation is `vgt [project.rpp]`; retain explicit
    # subcommands for scripts that want to state their intent.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"inspect", "apply", "-h", "--help"}:
        arguments.insert(0, "inspect")
    args = _parser().parse_args(arguments)
    try:
        project = locate_project(args.project)
        if args.command == "inspect":
            print(json.dumps(read_project(project).to_dict(), indent=2))
            return 0
        # Deliberately no RPP text-edit fallback: only the ReaScript mutates projects.
        print(
            f"Open {project} in REAPER, then run reascript/vgt_phase0_apply.lua from REAPER's Action List. "
            "That action uses REAPER's API and creates/updates the adjacent vgt.json.",
            file=sys.stderr,
        )
        return 2
    except ProjectError as exc:
        print(f"vgt: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
