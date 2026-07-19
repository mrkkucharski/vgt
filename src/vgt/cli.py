"""CLI entry point. RPP inspection is local; mutation is delegated to REAPER."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .analysis import AnalysisError, analyze
from .project import ProjectError, locate_project, read_project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vgt", description="Safely prepare a REAPER project for vgt.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="Report read-only REAPER project metadata.")
    inspect.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    apply = subparsers.add_parser("apply", help="Prepare the open project through the bundled ReaScript action.")
    apply.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    analyze_parser = subparsers.add_parser(
        "analyze", help="Detect tempo/key/sections/chords for the reference track and persist them to the .vgt sidecar."
    )
    analyze_parser.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    analyze_parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute every stage, ignoring cached results (human-verified stages are still preserved).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Phase 0's primary invocation is `vgt [project.rpp]`; retain explicit
    # subcommands for scripts that want to state their intent.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"inspect", "apply", "analyze", "-h", "--help"}:
        arguments.insert(0, "inspect")
    args = _parser().parse_args(arguments)
    try:
        project = locate_project(args.project)
        if args.command == "inspect":
            print(json.dumps(read_project(project).to_dict(), indent=2))
            return 0
        if args.command == "analyze":
            def report(message: str) -> None:
                # Progress goes to stderr so stdout stays a clean JSON document
                # for `vgt analyze ... | jq` and file redirects.
                print(f"vgt: {message}", file=sys.stderr, flush=True)

            print(json.dumps(analyze(project, progress=report, force=args.force), indent=2))
            return 0
        # Deliberately no RPP text-edit fallback: only the ReaScript mutates projects.
        print(
            f"Open {project} in REAPER, then run reascript/vgt_initialize.lua from REAPER's Action List. "
            "That action uses REAPER's API and creates/updates the adjacent .vgt sidecar "
            "(named after the project, e.g. 'Reaper Project.vgt').",
            file=sys.stderr,
        )
        return 2
    except (ProjectError, AnalysisError) as exc:
        print(f"vgt: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
