"""CLI entry point. RPP inspection is local; mutation is delegated to REAPER."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .analysis import AnalysisError, analyze
from .lalal import LalalError, LalalSeparator
from .project import ProjectError, locate_project, read_project
from .separation import GUITAR_TYPES, SeparationError, declared_guitar_type, separate, separation_preview
from .sidecar import read_sidecar, write_sidecar
from .status import StatusError, build_status, format_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vgt", description="Safely prepare a REAPER project for vgt.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="Report read-only REAPER project metadata.")
    inspect.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    apply = subparsers.add_parser("apply", help="Prepare the open project through the bundled ReaScript action.")
    apply.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    sync = subparsers.add_parser(
        "sync",
        help=(
            "Read manual REAPER edits -- [vgt] Chords track items and [vgt] section regions -- back into the "
            ".vgt sidecar as human-verified, in one action."
        ),
    )
    sync.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    analyze_parser = subparsers.add_parser(
        "analyze", help="Detect tempo/key/sections/chords for the reference track and persist them to the .vgt sidecar."
    )
    analyze_parser.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    analyze_parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute every stage, ignoring cached results (human-verified stages are still preserved).",
    )
    analyze_parser.add_argument(
        "--no-stems",
        action="store_true",
        help="Run free mix analysis only; do not attempt paid stem separation.",
    )
    analyze_parser.add_argument("--guitar", choices=GUITAR_TYPES, help="Persist and use the declared guitar type for stem separation.")
    analyze_parser.add_argument(
        "--force-stems", action="store_true", help="Deliberately repeat paid stem operations (requires cost confirmation)."
    )
    analyze_parser.add_argument(
        "--accept-stem-cost", action="store_true",
        help="Explicitly acknowledge the displayed paid stem-operation cost; required for non-interactive --force-stems.",
    )
    status_parser = subparsers.add_parser("status", help="Summarize the read-only vgt sidecar state for a project.")
    status_parser.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    status_parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    return parser


def _prompt_for_guitar_type() -> str:
    """Get an unambiguous declaration without making automation guess."""
    while True:
        answer = input("Guitar type for stem separation (electric/acoustic): ").strip().lower()
        if answer in GUITAR_TYPES:
            return answer
        print("Please enter 'electric' or 'acoustic'.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    # Phase 0's primary invocation is `vgt [project.rpp]`; retain explicit
    # subcommands for scripts that want to state their intent.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"inspect", "apply", "sync", "analyze", "status", "-h", "--help"}:
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

            # `--force` is intentionally local-only.  Paid work can only be
            # refreshed through the separate, conspicuous --force-stems path.
            # Tempo must precede both separation and chords.  Do the other
            # free detectors now, attempt optional separation, then decode
            # chords once from whichever artifacts actually arrived.
            local_result = analyze(project, progress=report, force=args.force, stages=("tempo", "key", "sections"))
            resolved_guitar_type: str | None = None
            separation_error: Exception | None = None
            if not args.no_stems and (not args.force or args.force_stems):
                try:
                    resolved_guitar_type = declared_guitar_type(local_result, args.guitar)
                except SeparationError as exc:
                    # Keep the established interactive first-run flow: a
                    # user can declare the guitar type once and have this
                    # invocation attempt separation before chord decoding.
                    # Automation remains non-blocking: without an explicit
                    # declaration it falls through to mix-only chords.
                    if sys.stdin.isatty() and args.guitar is None:
                        resolved_guitar_type = _prompt_for_guitar_type()
                    else:
                        separation_error = exc

            # Keep the ReaScript first-run setting and the stem cache setting
            # aligned.  The CLI override is deliberately persistent.
            if resolved_guitar_type and (
                local_result["analysis"]["stems"].get("guitar_type") != resolved_guitar_type
                or local_result.get("config", {}).get("guitar_type") != resolved_guitar_type
            ):
                local_result["analysis"]["stems"]["guitar_type"] = resolved_guitar_type
                local_result.setdefault("config", {})["guitar_type"] = resolved_guitar_type
                write_sidecar(project, local_result)

            if resolved_guitar_type:
                try:
                    preview = separation_preview(project, guitar_type=resolved_guitar_type, force=args.force_stems)
                    cached = preview["cached_operations"]
                    outstanding = preview["outstanding_operations"]
                    report(
                        "stem recipe: "
                        f"cached operations ({len(cached)}): {', '.join(cached) or 'none'}; "
                        f"outstanding operations ({len(outstanding)}): {', '.join(outstanding) or 'none'}"
                    )
                    if args.force_stems:
                        report(
                            f"PAID refresh requested for {len(outstanding)} operations; "
                            "LALAL's authoritative balance and minute estimate will be shown before confirmation."
                        )
                        if not args.accept_stem_cost and not sys.stdin.isatty():
                            raise AnalysisError("--force-stems in non-interactive mode requires --accept-stem-cost")
                    if outstanding:
                        def confirm_paid_refresh(operation_count: int) -> None:
                            # `separate` invokes this only after the free LALAL
                            # preflight has printed the current balance and the
                            # duration-derived estimate, and before any split
                            # request can be submitted.
                            if not args.force_stems or args.accept_stem_cost:
                                return
                            answer = input(
                                f"Repeat {operation_count} paid LALAL split operations at the displayed estimate? "
                                "Type 'yes' to continue: "
                            )
                            if answer.strip().lower() != "yes":
                                raise SeparationError("paid stem refresh cancelled")

                        with LalalSeparator() as backend:
                            separate(
                                project,
                                backend,
                                guitar_type=resolved_guitar_type,
                                force=args.force_stems,
                                progress=report,
                                before_submit=confirm_paid_refresh if args.force_stems else None,
                            )
                except (LalalError, SeparationError, AnalysisError) as exc:
                    separation_error = exc
            elif args.no_stems:
                report("stem separation skipped (--no-stems)")
            else:
                report("stem separation skipped (--force never spends credits; use --force-stems to opt in)")

            if separation_error is not None:
                report(f"stem separation unavailable; continuing with available sources: {separation_error}")
            result = analyze(project, progress=report, force=args.force, stages=("chords",))
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "status":
            status = build_status(project)
            print(json.dumps(status, indent=2) if args.json else format_status(status))
            return 0
        # Deliberately no RPP/sidecar text-edit fallback for these commands:
        # only the ReaScript actions mutate REAPER projects or read live
        # REAPER item state back into the sidecar.
        actions = {
            "sync": (
                "vgt_sync.lua",
                "reads the [vgt] Chords track's items and [vgt]-owned section regions back into the .vgt sidecar as human-verified",
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
    except (ProjectError, AnalysisError, StatusError, SeparationError) as exc:
        print(f"vgt: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
