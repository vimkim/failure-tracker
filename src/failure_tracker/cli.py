"""Command-line interface for failure-tracker."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from failure_tracker import __version__
from failure_tracker.db import FailureStore, doctor
from failure_tracker.domain import (
    FAILURE_KINDS,
    FIX_LAYERS,
    PATTERN_STATUSES,
    RESOLVABLE_STATUSES,
    SEVERITIES,
    ATTEMPT_OUTCOMES,
    default_database_path,
    parse_ttl,
    resolve_actor,
)
from failure_tracker.output import (
    format_attempt,
    format_claim,
    format_doctor,
    format_list,
    format_record_result,
    format_resolve,
    format_show,
    to_json,
)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--database",
        type=Path,
        default=argparse.SUPPRESS,
        help="SQLite database path (defaults to FAILURE_TRACKER_DB or XDG data dir)",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON",
    )

    parser = argparse.ArgumentParser(
        prog="failure-tracker",
        description="Track recurring workflow, skill, and tool failures in SQLite.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path (defaults to FAILURE_TRACKER_DB or XDG data dir)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    rec = subparsers.add_parser("record", parents=[common], help="Record a failure occurrence")
    rec.add_argument("--repo", required=True, help="Target repository name or owner/repo")
    rec.add_argument("--ref", required=True, help="Target file, skill, or module path")
    rec.add_argument("--kind", required=True, choices=FAILURE_KINDS, help="Failure category")
    rec.add_argument("--title", required=True, help="Short canonical pattern title")
    rec.add_argument("--symptom", required=True, help="Observed error or misbehavior")
    rec.add_argument("--root-cause", default=None, help="Known or suspected root cause")
    rec.add_argument("--severity", choices=SEVERITIES, default=None, help="Pattern severity")
    rec.add_argument("--fingerprint", default=None, help="Explicit deduplication fingerprint")
    rec.add_argument("--fingerprint-key", default=None, help="Custom key for hashing fingerprint")
    rec.add_argument("--work-item", type=int, default=None, help="Optional work-tracker item ID")
    rec.add_argument("--workdir", default=None, help="Working directory where failure occurred")
    rec.add_argument("--conversation-id", default=None, help="Agent session or conversation ID")
    rec.add_argument("--command", default=None, help="Command that failed")
    rec.add_argument("--exit-code", type=int, default=None, help="Process exit code")
    rec.add_argument("--context-json", default=None, help="JSON object with extra context")
    rec.add_argument("--actor", default=None, help="Human or agent identity")

    lst = subparsers.add_parser("list", parents=[common], help="List failure patterns")
    lst.add_argument("--status", choices=PATTERN_STATUSES, default=None)
    lst.add_argument("--repo", default=None)
    lst.add_argument("--kind", choices=FAILURE_KINDS, default=None)
    lst.add_argument("--severity", choices=SEVERITIES, default=None)
    lst.add_argument("--min-count", type=int, default=1)
    lst.add_argument("--all", action="store_true", help="Include verified, wontfix, and duplicate patterns")
    lst.add_argument("--limit", type=int, default=100)

    show = subparsers.add_parser("show", parents=[common], help="Show one failure pattern with occurrences and attempts")
    show.add_argument("pattern_id", type=int)
    show.add_argument("--occurrences", type=int, default=10)
    show.add_argument("--attempts", type=int, default=20)

    claim = subparsers.add_parser("claim", parents=[common], help="Lease an open or expired-lease failure pattern")
    claim.add_argument("--repo", default=None, help="Filter queue by target repository")
    claim.add_argument("--id", dest="pattern_id", type=int, default=None, help="Claim one specific pattern ID")
    claim.add_argument("--actor", default=None, help="Worker actor identity")
    claim.add_argument("--ttl", default="30m", help="Lease duration such as 30m or 2h")
    claim.add_argument("--max-failed-attempts", type=int, default=2)

    attempt = subparsers.add_parser("attempt", parents=[common], help="Record an improvement attempt outcome")
    attempt.add_argument("pattern_id", type=int)
    attempt.add_argument("--outcome", required=True, choices=ATTEMPT_OUTCOMES)
    attempt.add_argument("--actor", default=None)
    attempt.add_argument("--commit", dest="branch_or_commit", default=None)
    attempt.add_argument("--summary", default=None)
    attempt.add_argument("--note", default=None)
    attempt.add_argument(
        "--keep-claim",
        action="store_true",
        help="Keep claimed status even on failed/blocked/abandoned outcomes",
    )

    resolve = subparsers.add_parser("resolve", parents=[common], help="Resolve a failure pattern")
    resolve.add_argument("pattern_id", type=int)
    resolve.add_argument("--status", choices=RESOLVABLE_STATUSES, default="fixed_unverified")
    resolve.add_argument("--fix-layer", choices=FIX_LAYERS, default=None)
    resolve.add_argument("--commit", default=None)
    resolve.add_argument("--verify", dest="verify_cmd", default=None)
    resolve.add_argument("--duplicate-of", type=int, default=None)
    resolve.add_argument("--note", default=None)
    resolve.add_argument("--actor", default=None)

    subparsers.add_parser("doctor", parents=[common], help="Check database, schema, integrity, and actor without writing")
    subparsers.add_parser("path", parents=[common], help="Print the SQLite database path in use")
    return parser


def _emit(payload: Any, human_text: str, as_json: bool) -> None:
    if as_json:
        print(to_json(payload))
    else:
        print(human_text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = args.database if args.database is not None else default_database_path()
    as_json = bool(args.json)

    try:
        if args.subcommand == "path":
            _emit({"database": str(db_path)}, str(db_path), as_json)
            return 0

        if args.subcommand == "doctor":
            report = doctor(db_path, os.environ.get("FAILURE_TRACKER_ACTOR"))
            _emit(report, format_doctor(report), as_json)
            return 0 if report["ok"] else 1

        with FailureStore.open(db_path) as store:
            if args.subcommand == "record":
                result = store.record(
                    repo=args.repo,
                    ref=args.ref,
                    kind=args.kind,
                    title=args.title,
                    symptom=args.symptom,
                    actor=resolve_actor(args.actor),
                    root_cause=args.root_cause,
                    severity=args.severity,
                    fingerprint=args.fingerprint,
                    fingerprint_key=args.fingerprint_key,
                    work_item_id=args.work_item,
                    workdir=args.workdir,
                    conversation_id=args.conversation_id,
                    command=args.command,
                    exit_code=args.exit_code,
                    context_json=args.context_json,
                )
                _emit(result, format_record_result(result), as_json)
                return 0

            if args.subcommand == "list":
                patterns = store.list_patterns(
                    status=args.status,
                    repo=args.repo,
                    kind=args.kind,
                    severity=args.severity,
                    min_count=args.min_count,
                    include_closed=args.all,
                    limit=args.limit,
                )
                _emit(patterns, format_list(patterns), as_json)
                return 0

            if args.subcommand == "show":
                detail = store.show_pattern(
                    args.pattern_id,
                    occurrences_limit=args.occurrences,
                    attempts_limit=args.attempts,
                )
                _emit(detail, format_show(detail), as_json)
                return 0

            if args.subcommand == "claim":
                detail = store.claim(
                    actor=resolve_actor(args.actor),
                    ttl=parse_ttl(args.ttl),
                    repo=args.repo,
                    pattern_id=args.pattern_id,
                    max_failed_attempts=args.max_failed_attempts,
                )
                _emit(detail, format_claim(detail), as_json)
                return 0

            if args.subcommand == "attempt":
                result = store.record_attempt(
                    pattern_id=args.pattern_id,
                    outcome=args.outcome,
                    actor=resolve_actor(args.actor),
                    branch_or_commit=args.branch_or_commit,
                    summary=args.summary,
                    note=args.note,
                    release_claim=not args.keep_claim,
                )
                _emit(result, format_attempt(result), as_json)
                return 0

            if args.subcommand == "resolve":
                result = store.resolve(
                    pattern_id=args.pattern_id,
                    actor=resolve_actor(args.actor),
                    status=args.status,
                    fix_layer=args.fix_layer,
                    commit=args.commit,
                    verify_cmd=args.verify_cmd,
                    duplicate_of=args.duplicate_of,
                    note=args.note,
                )
                _emit(result, format_resolve(result), as_json)
                return 0

        parser.error(f"unknown subcommand: {args.subcommand}")
        return 2
    except (ValueError, LookupError, RuntimeError, OSError) as exc:
        if as_json:
            print(to_json({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
