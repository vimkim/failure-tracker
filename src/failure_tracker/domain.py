"""Domain types, validation, fingerprinting, and path resolution."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1

FAILURE_KINDS = (
    "tool_bug",
    "rule_drift",
    "skill_gap",
    "hook_failure",
    "env_drift",
    "agent_mistake",
)

PATTERN_STATUSES = (
    "open",
    "claimed",
    "fixed_unverified",
    "verified",
    "wontfix",
    "duplicate",
)

ACTIONABLE_PATTERN_STATUSES = ("open", "claimed", "fixed_unverified")

RESOLVABLE_STATUSES = ("fixed_unverified", "verified", "wontfix", "duplicate")

SEVERITIES = ("high", "normal", "low")

FIX_LAYERS = ("code_or_check", "delete_or_fix_doc", "add_doc", "wontfix")

CODE_OR_DOC_FIX_LAYERS = ("code_or_check", "delete_or_fix_doc", "add_doc")

ATTEMPT_OUTCOMES = ("succeeded", "failed", "blocked", "abandoned")

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_TTL_PATTERN = re.compile(r"^([1-9][0-9]*)([smhd])$")
_TTL_MULTIPLIERS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    millis = utc_value.microsecond // 1000
    return f"{utc_value:%Y-%m-%dT%H:%M:%S}.{millis:03d}Z"


def parse_ts(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_ttl(spec: str) -> timedelta:
    match = _TTL_PATTERN.fullmatch(spec.strip())
    if not match:
        raise ValueError(
            f"invalid ttl {spec!r}: expected positive integer with unit s, m, h, or d (for example 30m)"
        )
    amount = int(match.group(1))
    unit = match.group(2)
    return timedelta(seconds=amount * _TTL_MULTIPLIERS[unit])


def normalize_required(value: str, field: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{field} cannot be empty")
    return trimmed


def normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed if trimmed else None


def validate_repo(value: str) -> str:
    trimmed = normalize_required(value, "repo")
    if ".." in trimmed.split("/") or trimmed.endswith("/") or not _REPO_PATTERN.fullmatch(trimmed):
        raise ValueError(
            f"invalid target repo {value!r}: expected repository name or owner/repo without spaces"
        )
    return trimmed


def validate_choice(value: str, allowed: tuple[str, ...], field: str) -> str:
    normalized = normalize_required(value, field)
    if normalized not in allowed:
        joined = ", ".join(allowed)
        raise ValueError(f"invalid {field} {value!r}: expected one of {joined}")
    return normalized


def _canonical_token(value: str) -> str:
    return " ".join(value.split()).lower()


def compute_fingerprint(repo: str, ref: str, kind: str, key: str) -> str:
    parts = [
        _canonical_token(repo),
        _canonical_token(ref),
        _canonical_token(kind),
        _canonical_token(key),
    ]
    payload = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def default_database_path() -> Path:
    explicit = os.environ.get("FAILURE_TRACKER_DB", "").strip()
    if explicit:
        return Path(explicit)
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return Path(xdg_data) / "failure-tracker" / "failures.db"
    return Path.home() / ".local" / "share" / "failure-tracker" / "failures.db"


def resolve_actor(explicit: str | None = None) -> str:
    for candidate in (
        explicit,
        os.environ.get("FAILURE_TRACKER_ACTOR"),
        os.environ.get("USER"),
    ):
        if candidate is not None and candidate.strip():
            return candidate.strip()
    return "unknown"
