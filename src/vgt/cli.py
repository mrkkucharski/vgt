"""CLI entry point. RPP inspection is local; mutation is delegated to REAPER."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .analysis import AnalysisError, analyze
from .project import ProjectError, locate_project, read_project
from .status import StatusError, build_status, format_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vgt", description="Safely prepare a REAPER project for vgt.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="Report read-only REAPER project metadata.")
    inspect.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    apply = subparsers.add_parser("apply", help="Prepare the open project through the bundled ReaScript action.")
    apply.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    read_chords = subparsers.add_parser(
        "read-chords",
        help="Read corrected chord items from the [vgt] Chords track in REAPER back into the .vgt sidecar as human-verified.",
    )
    read_chords.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    read_sections = subparsers.add_parser(
        "read-sections",
        help="Read corrected [vgt] section regions from REAPER back into the .vgt sidecar as human-verified.",
    )
    read_sections.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    analyze_parser = subparsers.add_parser(
        "analyze", help="Detect tempo/key/sections/chords for the reference track and persist them to the .vgt sidecar."
    )
    analyze_parser.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    analyze_parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute every stage, ignoring cached results (human-verified stages are still preserved).",
    )
    status_parser = subparsers.add_parser("status", help="Summarize the read-only vgt sidecar state for a project.")
    status_parser.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    status_parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Phase 0's primary invocation is `vgt [project.rpp]`; retain explicit
    # subcommands for scripts that want to state their intent.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"inspect", "apply", "read-chords", "read-sections", "analyze", "status", "-h", "--help"}:
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
        if args.command == "status":
            status = build_status(project)
            print(json.dumps(status, indent=2) if args.json else format_status(status))
            return 0
        # Deliberately no RPP/sidecar text-edit fallback for these commands:
        # only the ReaScript actions mutate REAPER projects or read live
        # REAPER item state back into the sidecar.
        actions = {
            "read-chords": (
                "vgt_read_chords.lua",
                "reads the [vgt] Chords track's items back into the .vgt sidecar as human-verified",
            ),
            "read-sections": (
                "vgt_read_sections.lua",
                "reads vgt-owned section regions back into the .vgt sidecar as human-verified",
            ),
        }
        script, description = actions.get(
            args.command,
            ("vgt_initialize.lua", "creates/updates the adjacent .vgt sidecar (named after the project, e.g. 'Reaper Project.vgt')"),
        )
        print(
            f"Open {project} in REAPER, then run reascript/{script} from REAPER's Action List. "
            f"That action uses REAPER's API and {description}.",
            file=sys.stderr,
        )
        return 2
    except (ProjectError, AnalysisError, StatusError) as exc:
        print(f"vgt: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
