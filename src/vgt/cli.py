"""CLI entry point. RPP inspection is local; mutation is delegated to REAPER."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .analysis import AnalysisError, add_transcription_targets, analyze, forget_transcription_targets, set_transcription_modes
from .lalal import LalalError, LalalSeparator
from .project import ProjectError, locate_project, read_project
from .reascripts import ReaScriptInstallError, confirm_overwrite, default_destination, install_reascripts
from .separation import GUITAR_TYPES, OPTIONAL_STEMS, SeparationError, declared_guitar_type, separate, separation_preview
from .sidecar import atomic_update_sidecar
from .status import StatusError, build_status, format_status
from .transcribe import VALID_PROFILE_NAMES, VALID_TARGETS, TranscriptionError, validate_profile_for_target, validate_target


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
        "--extra-stem",
        action="append",
        choices=(*OPTIONAL_STEMS, "keys", "keys/piano"),
        help="Also separate this opt-in instrument (repeat for both strings and keys/piano).",
    )
    analyze_parser.add_argument(
        "--force-stems", action="store_true", help="Deliberately repeat paid stem operations (requires cost confirmation)."
    )
    analyze_parser.add_argument(
        "--accept-stem-cost", action="store_true",
        help="Explicitly acknowledge the displayed paid stem-operation cost; required for non-interactive forced or opt-in stem work.",
    )
    analyze_parser.add_argument(
        "--transcribe",
        action="append",
        choices=VALID_TARGETS,
        metavar="TARGET",
        help="Also transcribe this target's stem to MIDI; persists across future runs (repeat for multiple targets).",
    )
    analyze_parser.add_argument(
        "--mode",
        action="append",
        metavar="TARGET=PROFILE",
        help=f"Persist a transcription profile for one target (profiles: {', '.join(VALID_PROFILE_NAMES)}; repeatable).",
    )
    analyze_parser.add_argument(
        "--forget-transcription",
        action="append",
        choices=VALID_TARGETS,
        metavar="TARGET",
        help="Remove this target from the persisted transcription set and delete its MIDI/notes artifacts.",
    )
    analyze_parser.add_argument(
        "--transcribe-only",
        choices=VALID_TARGETS,
        metavar="TARGET",
        help="Transcribe only this target for this run, without changing the persisted set (for threshold tuning).",
    )
    analyze_parser.add_argument(
        "--no-transcribe",
        action="store_true",
        help="Skip the transcription stage this run; the persisted requested set is untouched.",
    )
    status_parser = subparsers.add_parser("status", help="Summarize the read-only vgt sidecar state for a project.")
    status_parser.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    status_parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    install_parser = subparsers.add_parser(
        "install-reascripts", help="Install bundled ReaScript actions into REAPER's Scripts directory."
    )
    install_parser.add_argument(
        "--destination", type=Path, help="Directory to install the actions into (defaults to REAPER's Scripts/vgt directory)."
    )
    install_parser.add_argument("--dry-run", action="store_true", help="Show target paths without creating or changing files.")
    install_parser.add_argument("--force", action="store_true", help="Replace differing destination files without prompting.")
    return parser


def _prompt_for_guitar_type() -> str:
    """Get an unambiguous declaration without making automation guess."""
    while True:
        answer = input("Guitar type for stem separation (electric/acoustic): ").strip().lower()
        if answer in GUITAR_TYPES:
            return answer
        print("Please enter 'electric' or 'acoustic'.", file=sys.stderr)


def _parse_modes(values: list[str] | None) -> dict[str, str]:
    modes: dict[str, str] = {}
    for value in values or []:
        target, separator, profile = value.partition("=")
        if not separator or not target or not profile:
            raise AnalysisError("--mode must be TARGET=PROFILE")
        try:
            validate_target(target)
            validate_profile_for_target(target, profile)
        except TranscriptionError as exc:
            raise AnalysisError(str(exc)) from exc
        modes[target] = profile
    return modes


def main(argv: list[str] | None = None) -> int:
    # Phase 0's primary invocation is `vgt [project.rpp]`; retain explicit
    # subcommands for scripts that want to state their intent.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"inspect", "apply", "sync", "analyze", "status", "install-reascripts", "-h", "--help"}:
        arguments.insert(0, "inspect")
    args = _parser().parse_args(arguments)
    try:
        if args.command == "install-reascripts":
            destination = args.destination or default_destination()
            installed = install_reascripts(
                destination, dry_run=args.dry_run, force=args.force, confirm=confirm_overwrite
            )
            action = "Would install" if args.dry_run else "Installed"
            for path in installed:
                print(f"{action}: {path}")
            if args.dry_run:
                print("Dry run: no files were changed.")
            print("In REAPER, open the Action List, then use ReaScript: Load to register both files once.")
            return 0
        project = locate_project(args.project)
        if args.command == "inspect":
            print(json.dumps(read_project(project).to_dict(), indent=2))
            return 0
        if args.command == "analyze":
            def report(message: str) -> None:
                print(f"vgt: {message}", file=sys.stderr, flush=True)

            if args.transcribe_only and (args.transcribe or args.forget_transcription):
                raise AnalysisError("--transcribe-only cannot be combined with --transcribe or --forget-transcription")
            if args.transcribe_only and args.no_transcribe:
                raise AnalysisError("--transcribe-only and --no-transcribe are mutually exclusive")
            if args.transcribe and args.forget_transcription and set(args.transcribe) & set(args.forget_transcription):
                raise AnalysisError("a target cannot be both --transcribe and --forget-transcription in the same run")
            modes = _parse_modes(args.mode)

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
            #
            # This must go through the same locked read-mutate-write cycle
            # every other Python writer uses (atomic_update_sidecar), rather
            # than writing the `local_result` snapshot taken at the top of
            # this run directly: that snapshot can already be stale by the
            # time separation runs (which can take minutes), so writing it
            # verbatim would silently roll back top-level fields -- including
            # `managed_track_guids` -- that a concurrent ReaScript apply
            # committed in between.
            if resolved_guitar_type:
                def persist_guitar_type(current: dict[str, Any]) -> None:
                    current["analysis"]["stems"]["guitar_type"] = resolved_guitar_type
                    current.setdefault("config", {})["guitar_type"] = resolved_guitar_type

                atomic_update_sidecar(project, persist_guitar_type)

            if resolved_guitar_type:
                try:
                    preview = separation_preview(project, guitar_type=resolved_guitar_type, optional_stems=args.extra_stem, force=args.force_stems)
                    cached = preview["cached_operations"]
                    outstanding = preview["outstanding_operations"]
                    report(
                        "stem recipe: "
                        f"cached operations ({len(cached)}): {', '.join(cached) or 'none'}; "
                        f"outstanding operations ({len(outstanding)}): {', '.join(outstanding) or 'none'}"
                    )
                    if preview["optional_stems"]:
                        report(f"opt-in stems: {', '.join(preview['optional_stems'])}")
                    # An opt-in extra creates new paid work. It therefore
                    # uses the same consent path as a forced refresh, after
                    # preflight has supplied LALAL's authoritative quote.
                    # An interrupted opt-in request remains in the sidecar so
                    # it can resume safely.  It must still use the opt-in
                    # confirmation path, even when this invocation omits
                    # --extra-stem; otherwise a declined request could turn
                    # into an unacknowledged charge on a later retry.
                    outstanding_optional_operations = {
                        f"{stem}-original" for stem in preview["optional_stems"]
                    }.intersection(outstanding)
                    requires_paid_confirmation = (
                        args.force_stems
                        or bool(args.extra_stem)
                        or bool(outstanding_optional_operations)
                    )
                    if requires_paid_confirmation and outstanding:
                        report(
                            f"PAID stem operations requested for {len(outstanding)} operations; "
                            "LALAL's authoritative balance and minute estimate will be shown before confirmation."
                        )
                        if not args.accept_stem_cost and not sys.stdin.isatty():
                            raise AnalysisError("a paid stem operation in non-interactive mode requires --accept-stem-cost")
                    if outstanding:
                        def confirm_paid_operations(operation_count: int) -> None:
                            # `separate` invokes this only after the free LALAL
                            # preflight has printed the current balance and the
                            # duration-derived estimate, and before any split
                            # request can be submitted.
                            if not requires_paid_confirmation or args.accept_stem_cost:
                                return
                            answer = input(
                                f"Run {operation_count} paid LALAL split operations at the displayed estimate? "
                                "Type 'yes' to continue: "
                            )
                            if answer.strip().lower() != "yes":
                                raise SeparationError("paid stem refresh cancelled")

                        with LalalSeparator() as backend:
                            separate(
                                project,
                                backend,
                                guitar_type=resolved_guitar_type,
                                optional_stems=args.extra_stem,
                                force=args.force_stems,
                                progress=report,
                                before_submit=confirm_paid_operations if requires_paid_confirmation else None,
                            )
                # A failed or unavailable separator is optional, but an
                # explicit paid-work safety refusal is not.  In particular,
                # non-interactive --force-stems without --accept-stem-cost
                # must retain its documented non-zero exit rather than being
                # silently converted into a successful mix-only run.
                except (LalalError, SeparationError) as exc:
                    separation_error = exc
            elif args.no_stems:
                report("stem separation skipped (--no-stems)")
            else:
                report("stem separation skipped (--force never spends credits; use --force-stems to opt in)")

            if separation_error is not None:
                report(f"stem separation unavailable; continuing with available sources: {separation_error}")

            if args.forget_transcription:
                forget_transcription_targets(project, tuple(args.forget_transcription))
                report(f"forgot transcription target(s): {', '.join(args.forget_transcription)}")
            if args.transcribe:
                add_transcription_targets(project, tuple(args.transcribe))
                report(f"transcription target(s) persisted: {', '.join(args.transcribe)}")
            if modes:
                set_transcription_modes(project, modes)
                report(f"transcription mode(s) persisted: {', '.join(f'{target}={profile}' for target, profile in modes.items())}")

            transcription_stages = () if args.no_transcribe else ("transcription",)
            analyze(
                project,
                progress=report,
                force=args.force,
                stages=("chords", *transcription_stages),
                transcription_targets=(args.transcribe_only,) if args.transcribe_only else None,
            )
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
    except (ProjectError, AnalysisError, StatusError, SeparationError, ReaScriptInstallError) as exc:
        print(f"vgt: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
