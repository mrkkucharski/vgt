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
from .transcription_lifecycle import (
    add_variant,
    discard_variant,
    purge_discarded,
    rename_variant,
    select_variant,
)
from .transcription_profiles import (
    ProfileDefinitionError,
    load_project_profiles,
    resolve_profile,
    resolved_cleanup_identity,
    resolved_detection_identity,
    validate_project_profiles,
)


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

    transcription_parser = subparsers.add_parser(
        "transcription", help="Manage transcription profiles and retained per-target variants."
    )
    transcription_sub = transcription_parser.add_subparsers(dest="transcription_command", required=True)

    profile_parser = transcription_sub.add_parser("profile", help="Inspect built-in and project-local transcription profiles.")
    profile_sub = profile_parser.add_subparsers(dest="profile_command", required=True)
    profile_list = profile_sub.add_parser("list", help="List every built-in and project-local profile.")
    profile_list.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    profile_show = profile_sub.add_parser("show", help="Show one profile's fully resolved settings.")
    profile_show.add_argument("name", help="A built-in or project-local profile name.")
    profile_show.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")
    profile_validate = profile_sub.add_parser(
        "validate", help="Resolve every project-local profile, failing before any backend would run if one is invalid."
    )
    profile_validate.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")

    variant_parser = transcription_sub.add_parser("variant", help="Add, rename, select, discard, or purge retained variants.")
    variant_sub = variant_parser.add_subparsers(dest="variant_command", required=True)

    variant_add = variant_sub.add_parser("add", help="Create and reconcile a new named variant for one target.")
    variant_add.add_argument("target", choices=VALID_TARGETS)
    variant_add.add_argument("--name", required=True, dest="label", help="A unique label for this target's new variant.")
    variant_add.add_argument("--profile", required=True, help="A built-in or project-local profile name.")
    variant_add.add_argument("--force", action="store_true", help="Recompute even if an identical variant is already cached.")
    variant_add.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")

    variant_rename = variant_sub.add_parser("rename", help="Rename a retained variant without rerunning transcription.")
    variant_rename.add_argument("target", choices=VALID_TARGETS)
    variant_rename.add_argument("ref", metavar="VARIANT", help="The variant's immutable id or its current unambiguous label.")
    variant_rename.add_argument("--name", required=True, dest="new_label", help="The variant's new label.")
    variant_rename.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")

    # `ref` is required (not `nargs="?"`) so this never collides with the
    # trailing optional `project` positional -- two consecutive `nargs="?"`
    # positionals are ambiguous to argparse when only one token is given.
    # Clearing a selection is therefore the separate `variant unselect`
    # subcommand below, not this one with `ref` omitted.
    variant_select = variant_sub.add_parser("select", help="Set one target's selected variant.")
    variant_select.add_argument("target", choices=VALID_TARGETS)
    variant_select.add_argument("ref", metavar="VARIANT", help="The variant's immutable id or unambiguous label.")
    variant_select.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")

    variant_unselect = variant_sub.add_parser("unselect", help="Explicitly clear one target's selected variant.")
    variant_unselect.add_argument("target", choices=VALID_TARGETS)
    variant_unselect.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")

    variant_discard = variant_sub.add_parser(
        "discard", help="Delete one variant's generated artifacts and archive a compact recipe/metrics record."
    )
    variant_discard.add_argument("target", choices=VALID_TARGETS)
    variant_discard.add_argument("ref", metavar="VARIANT", help="The variant's immutable id or unambiguous label.")
    variant_discard.add_argument(
        "--select", metavar="VARIANT",
        help="Select this replacement variant if the one being discarded is currently selected.",
    )
    variant_discard.add_argument(
        "--clear-selected", action="store_true",
        help="Explicitly clear the selection if the one being discarded is currently selected.",
    )
    variant_discard.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")

    variant_purge = variant_sub.add_parser(
        "purge-discarded", help="Clear one target's archived discarded-variant recipe/metrics records."
    )
    variant_purge.add_argument("target", choices=VALID_TARGETS)
    variant_purge.add_argument("project", nargs="?", help="Path to a .RPP project (defaults to cwd's only .RPP).")

    return parser


def _normalize_transcription_project_argument(arguments: list[str]) -> list[str]:
    """Allow lifecycle commands to retain the documented trailing project.

    ``argparse`` does not revisit an optional positional once it encounters an
    option.  Consequently its usual ``TARGET --name LABEL PROJECT.RPP`` form
    leaves ``PROJECT.RPP`` unconsumed when ``project`` is ``nargs='?'``.  The
    public examples deliberately put the project last, so move a final RPP
    argument ahead of the first option before parsing.  This is restricted to
    the variant lifecycle commands; it neither changes their values nor the
    grammar of the existing commands.
    """
    if len(arguments) < 5 or arguments[:2] != ["transcription", "variant"]:
        return arguments
    if arguments[2] not in {"add", "rename", "select", "discard", "purge-discarded", "unselect"}:
        return arguments
    project = arguments[-1]
    if project.startswith("-") or not project.lower().endswith(".rpp"):
        return arguments
    try:
        first_option = next(index for index, value in enumerate(arguments[3:], start=3) if value.startswith("--"))
    except StopIteration:
        return arguments
    normalized = arguments[:-1]
    normalized.insert(first_option, project)
    return normalized


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


def _resolved_profile_dict(resolved: Any) -> dict[str, Any]:
    return {
        "name": resolved.name,
        "target": resolved.target,
        "backend": resolved.backend,
        "is_builtin": resolved.is_builtin,
        "profile_definition_hash": resolved.profile_definition_hash,
        "detection": resolved_detection_identity(resolved),
        "cleanup": resolved_cleanup_identity(resolved),
    }


def _dispatch_transcription(args: argparse.Namespace, project: Path) -> int:
    if args.transcription_command == "profile":
        project_profiles = load_project_profiles(project)
        if args.profile_command == "list":
            for name in VALID_PROFILE_NAMES:
                print(f"{name} (builtin)")
            for name in project_profiles:
                print(f"{name} (project)")
            return 0
        if args.profile_command == "show":
            try:
                resolved = resolve_profile(args.name, project_profiles)
            except ProfileDefinitionError as exc:
                print(f"vgt: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(_resolved_profile_dict(resolved), indent=2))
            return 0
        if args.profile_command == "validate":
            try:
                resolved = validate_project_profiles(project)
            except ProfileDefinitionError as exc:
                print(f"vgt: {exc}", file=sys.stderr)
                return 2
            print(f"{len(resolved)} project profile(s) valid.")
            return 0
    if args.variant_command == "add":
        def report(message: str) -> None:
            print(f"vgt: {message}", file=sys.stderr, flush=True)

        variant = add_variant(project, args.target, label=args.label, profile=args.profile, force=args.force, progress=report)
        print(json.dumps(variant, indent=2))
        return 0
    if args.variant_command == "rename":
        variant_record = rename_variant(project, args.target, args.ref, new_label=args.new_label)
        print(json.dumps(variant_record, indent=2))
        return 0
    if args.variant_command == "select":
        variant_record = select_variant(project, args.target, args.ref)
        print(json.dumps(variant_record, indent=2))
        return 0
    if args.variant_command == "unselect":
        variant_record = select_variant(project, args.target, None, clear=True)
        print(json.dumps(variant_record, indent=2))
        return 0
    if args.variant_command == "discard":
        variant_record = discard_variant(
            project, args.target, args.ref, select=args.select, clear_selected=args.clear_selected
        )
        print(json.dumps(variant_record, indent=2))
        return 0
    if args.variant_command == "purge-discarded":
        variant_record = purge_discarded(project, args.target)
        print(json.dumps(variant_record, indent=2))
        return 0
    raise AnalysisError(f"unknown transcription subcommand: {args.transcription_command}")


def main(argv: list[str] | None = None) -> int:
    # Phase 0's primary invocation is `vgt [project.rpp]`; retain explicit
    # subcommands for scripts that want to state their intent.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {
        "inspect", "apply", "sync", "analyze", "status", "install-reascripts", "transcription", "-h", "--help",
    }:
        arguments.insert(0, "inspect")
    arguments = _normalize_transcription_project_argument(arguments)
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
            print("In REAPER, open the Action List, then use ReaScript: Load to register these files once.")
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
        if args.command == "transcription":
            return _dispatch_transcription(args, project)
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
    except (ProjectError, AnalysisError, StatusError, SeparationError, ReaScriptInstallError, TranscriptionError) as exc:
        print(f"vgt: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
