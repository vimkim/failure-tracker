"""Human-readable and JSON formatters for failure-tracker."""

from __future__ import annotations

import json
from typing import Any


def to_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def format_pattern_summary(pattern: dict[str, Any]) -> str:
    return (
        f"#{pattern['id']} [{pattern['status']}] "
        f"({pattern['severity']}, x{pattern['occurrence_count']}, "
        f"failed={pattern['failed_attempts']}) "
        f"{pattern['target_repo']} :: {pattern['target_ref']} "
        f"[{pattern['failure_kind']}] {pattern['title']}"
    )


def format_record_result(result: dict[str, Any]) -> str:
    pattern = result["pattern"]
    occurrence = result["occurrence"]
    if result["created"]:
        action = "Created"
    elif result["reopened"]:
        action = "Reopened"
    else:
        action = "Recorded occurrence for"
    lines = [
        f"{action} pattern #{pattern['id']} (occurrence #{occurrence['id']})",
        format_pattern_summary(pattern),
    ]
    return "\n".join(lines)


def format_list(patterns: list[dict[str, Any]]) -> str:
    if not patterns:
        return "No failure patterns matched."
    return "\n".join(format_pattern_summary(pattern) for pattern in patterns)


def format_show(detail: dict[str, Any]) -> str:
    p = detail["pattern"]
    lines = [
        format_pattern_summary(p),
        f"  Fingerprint: {p['fingerprint']}",
        f"  First seen:  {p['first_seen_at']}",
        f"  Last seen:   {p['last_seen_at']}",
    ]
    if p["root_cause"]:
        lines.append(f"  Root cause:  {p['root_cause']}")
    if p["fix_layer"]:
        lines.append(f"  Fix layer:   {p['fix_layer']}")
    if p["fix_commit"]:
        lines.append(f"  Fix commit:  {p['fix_commit']}")
    if p["verification_cmd"]:
        lines.append(f"  Verify cmd:  {p['verification_cmd']}")
    if p["claimed_by"]:
        lines.append(
            f"  Claimed by:  {p['claimed_by']} (expires {p['claim_expires_at']})"
        )
    if p["duplicate_of_id"] is not None:
        lines.append(f"  Duplicate:   #{p['duplicate_of_id']}")
    if p["work_item_id"] is not None:
        lines.append(f"  Work item:   #{p['work_item_id']}")

    occurrences = detail["occurrences"]
    lines.append(f"\nOccurrences (showing {len(occurrences)}):")
    if not occurrences:
        lines.append("  none")
    for occ in occurrences:
        cmd_part = f" cmd={occ['command']!r}" if occ["command"] else ""
        exit_part = f" exit={occ['exit_code']}" if occ["exit_code"] is not None else ""
        lines.append(
            f"  - #{occ['id']} {occ['occurred_at']} by {occ['actor']}{cmd_part}{exit_part}: {occ['symptom']}"
        )

    attempts = detail["attempts"]
    lines.append(f"\nImprovement attempts (showing {len(attempts)}):")
    if not attempts:
        lines.append("  none")
    for att in attempts:
        ref_part = f" ref={att['branch_or_commit']}" if att["branch_or_commit"] else ""
        note_part = f" | {att['note']}" if att["note"] else ""
        lines.append(
            f"  - #{att['id']} {att['started_at']} [{att['outcome']}] by {att['actor']}{ref_part}{note_part}"
        )
    return "\n".join(lines)


def format_claim(detail: dict[str, Any] | None) -> str:
    if detail is None:
        return "No claimable failure patterns available."
    return "Claimed:\n" + format_show(detail)


def format_attempt(result: dict[str, Any]) -> str:
    p = result["pattern"]
    a = result["attempt"]
    return (
        f"Recorded attempt #{a['id']} [{a['outcome']}] for pattern #{p['id']} "
        f"(status={p['status']})"
    )


def format_resolve(result: dict[str, Any]) -> str:
    p = result["pattern"]
    return f"Resolved pattern #{p['id']} as {p['status']} ({p['fix_layer'] or 'n/a'})"


def format_doctor(report: dict[str, Any]) -> str:
    lines = [f"Doctor ok: {report['ok']}"]
    for check in report["checks"]:
        req = "required" if check["required"] else "optional"
        lines.append(
            f"  [{check['status'].upper()}] {check['name']} ({req}): {check['detail']}"
        )
    return "\n".join(lines)
