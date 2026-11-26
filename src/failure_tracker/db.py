"""SQLite persistence layer for failure patterns, occurrences, and improvement attempts."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from failure_tracker.domain import (
    ACTIONABLE_PATTERN_STATUSES,
    ATTEMPT_OUTCOMES,
    CODE_OR_DOC_FIX_LAYERS,
    FAILURE_KINDS,
    FIX_LAYERS,
    PATTERN_STATUSES,
    RESOLVABLE_STATUSES,
    SCHEMA_VERSION,
    SEVERITIES,
    compute_fingerprint,
    format_ts,
    normalize_optional,
    normalize_required,
    utc_now,
    validate_choice,
    validate_repo,
)

_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_SECS = 5.0
_SEVERITY_RANK = {"high": 0, "normal": 1, "low": 2}
_RELEASE_CLAIM_OUTCOMES = ("failed", "blocked", "abandoned")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS failure_patterns (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint        TEXT NOT NULL UNIQUE,
    target_repo        TEXT NOT NULL CHECK (length(trim(target_repo)) > 0),
    target_ref         TEXT NOT NULL CHECK (length(trim(target_ref)) > 0),
    failure_kind       TEXT NOT NULL CHECK (
        failure_kind IN ('tool_bug', 'rule_drift', 'skill_gap', 'hook_failure', 'env_drift', 'agent_mistake')
    ),
    title              TEXT NOT NULL CHECK (length(trim(title)) > 0),
    root_cause         TEXT,
    fix_layer          TEXT CHECK (
        fix_layer IS NULL OR fix_layer IN ('code_or_check', 'delete_or_fix_doc', 'add_doc', 'wontfix')
    ),
    status             TEXT NOT NULL CHECK (
        status IN ('open', 'claimed', 'fixed_unverified', 'verified', 'wontfix', 'duplicate')
    ),
    severity           TEXT NOT NULL DEFAULT 'normal' CHECK (severity IN ('high', 'normal', 'low')),
    occurrence_count   INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    claimed_by         TEXT,
    claim_expires_at   TEXT,
    duplicate_of_id    INTEGER REFERENCES failure_patterns(id),
    fix_commit         TEXT,
    verification_cmd   TEXT,
    work_item_id       INTEGER
);

CREATE TABLE IF NOT EXISTS failure_occurrences (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id         INTEGER NOT NULL REFERENCES failure_patterns(id) ON DELETE CASCADE,
    occurred_at        TEXT NOT NULL,
    actor              TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    workdir            TEXT,
    work_item_id       INTEGER,
    conversation_id    TEXT,
    command            TEXT,
    exit_code          INTEGER,
    symptom            TEXT NOT NULL CHECK (length(trim(symptom)) > 0),
    context_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS improvement_attempts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id         INTEGER NOT NULL REFERENCES failure_patterns(id) ON DELETE CASCADE,
    actor              TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    outcome            TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed', 'blocked', 'abandoned')),
    branch_or_commit   TEXT,
    summary            TEXT,
    note               TEXT
);

CREATE INDEX IF NOT EXISTS idx_failure_patterns_queue
    ON failure_patterns(status, target_repo, severity, occurrence_count DESC, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_failure_patterns_lease
    ON failure_patterns(status, claim_expires_at);
CREATE INDEX IF NOT EXISTS idx_failure_occurrences_pattern
    ON failure_occurrences(pattern_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_improvement_attempts_pattern
    ON improvement_attempts(pattern_id, id DESC);
"""

_PATTERN_SELECT = """
SELECT
    p.id,
    p.fingerprint,
    p.target_repo,
    p.target_ref,
    p.failure_kind,
    p.title,
    p.root_cause,
    p.fix_layer,
    p.status,
    p.severity,
    p.occurrence_count,
    p.first_seen_at,
    p.last_seen_at,
    p.claimed_by,
    p.claim_expires_at,
    p.duplicate_of_id,
    p.fix_commit,
    p.verification_cmd,
    p.work_item_id,
    (
        SELECT count(*)
        FROM improvement_attempts a
        WHERE a.pattern_id = p.id AND a.outcome = 'failed'
    ) AS failed_attempts
FROM failure_patterns p
"""


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _enable_wal(connection: sqlite3.Connection) -> None:
    deadline = time.monotonic() + _BUSY_TIMEOUT_SECS
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if _is_busy_error(exc) and time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            raise


def _normalize_context_json(context: str | dict[str, Any] | None) -> str:
    if context is None:
        return "{}"
    if isinstance(context, dict):
        return json.dumps(context, ensure_ascii=False, sort_keys=True)
    raw = context.strip()
    if not raw:
        return "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"context_json must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("context_json must be a JSON object")
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


def _row_to_pattern(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "fingerprint": row["fingerprint"],
        "target_repo": row["target_repo"],
        "target_ref": row["target_ref"],
        "failure_kind": row["failure_kind"],
        "title": row["title"],
        "root_cause": row["root_cause"],
        "fix_layer": row["fix_layer"],
        "status": row["status"],
        "severity": row["severity"],
        "occurrence_count": row["occurrence_count"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "claimed_by": row["claimed_by"],
        "claim_expires_at": row["claim_expires_at"],
        "duplicate_of_id": row["duplicate_of_id"],
        "fix_commit": row["fix_commit"],
        "verification_cmd": row["verification_cmd"],
        "work_item_id": row["work_item_id"],
        "failed_attempts": row["failed_attempts"],
    }


def _row_to_occurrence(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "pattern_id": row["pattern_id"],
        "occurred_at": row["occurred_at"],
        "actor": row["actor"],
        "workdir": row["workdir"],
        "work_item_id": row["work_item_id"],
        "conversation_id": row["conversation_id"],
        "command": row["command"],
        "exit_code": row["exit_code"],
        "symptom": row["symptom"],
        "context": json.loads(row["context_json"]),
    }


def _row_to_attempt(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "pattern_id": row["pattern_id"],
        "actor": row["actor"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "outcome": row["outcome"],
        "branch_or_commit": row["branch_or_commit"],
        "summary": row["summary"],
        "note": row["note"],
    }


class FailureStore:
    """Transactional SQLite store for failure patterns and improvement history."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self.connection = connection
        self.path = path

    @classmethod
    def open(cls, path: Path | str) -> "FailureStore":
        target = Path(path)
        if str(target) != ":memory:":
            target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(target),
            timeout=_BUSY_TIMEOUT_SECS,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        if str(target) != ":memory:":
            _enable_wal(connection)
        store = cls(connection, target)
        store._migrate()
        return store

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "FailureStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        deadline = time.monotonic() + _BUSY_TIMEOUT_SECS
        while True:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if _is_busy_error(exc) and time.monotonic() < deadline:
                    time.sleep(0.01)
                    continue
                raise
        try:
            yield
            if self.connection.in_transaction:
                self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def _migrate(self) -> None:
        with self._immediate():
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {version} is newer than supported version {SCHEMA_VERSION}"
                )
            self.connection.executescript(_SCHEMA_SQL)
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _get_pattern(self, pattern_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            f"{_PATTERN_SELECT} WHERE p.id = ?",
            (pattern_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"failure pattern {pattern_id} not found")
        return _row_to_pattern(row)

    def record(
        self,
        *,
        repo: str,
        ref: str,
        kind: str,
        title: str,
        symptom: str,
        actor: str,
        root_cause: str | None = None,
        severity: str | None = None,
        fingerprint: str | None = None,
        fingerprint_key: str | None = None,
        work_item_id: int | None = None,
        workdir: str | None = None,
        conversation_id: str | None = None,
        command: str | None = None,
        exit_code: int | None = None,
        context_json: str | dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clean_repo = validate_repo(repo)
        clean_ref = normalize_required(ref, "ref")
        clean_kind = validate_choice(kind, FAILURE_KINDS, "kind")
        clean_title = normalize_required(title, "title")
        clean_symptom = normalize_required(symptom, "symptom")
        clean_actor = normalize_required(actor, "actor")
        clean_root_cause = normalize_optional(root_cause)
        explicit_severity = (
            validate_choice(severity, SEVERITIES, "severity")
            if severity is not None
            else None
        )
        clean_fp = (
            normalize_required(fingerprint, "fingerprint")
            if fingerprint is not None
            else compute_fingerprint(
                clean_repo,
                clean_ref,
                clean_kind,
                normalize_optional(fingerprint_key) or clean_title,
            )
        )
        clean_workdir = normalize_optional(workdir)
        clean_conv = normalize_optional(conversation_id)
        clean_cmd = normalize_optional(command)
        clean_context = _normalize_context_json(context_json)
        ts = format_ts(now or utc_now())

        with self._immediate():
            existing = self.connection.execute(
                "SELECT * FROM failure_patterns WHERE fingerprint = ?",
                (clean_fp,),
            ).fetchone()
            if existing is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO failure_patterns (
                        fingerprint, target_repo, target_ref, failure_kind,
                        title, root_cause, status, severity,
                        occurrence_count, first_seen_at, last_seen_at, work_item_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, 1, ?, ?, ?)
                    """,
                    (
                        clean_fp,
                        clean_repo,
                        clean_ref,
                        clean_kind,
                        clean_title,
                        clean_root_cause,
                        explicit_severity or "normal",
                        ts,
                        ts,
                        work_item_id,
                    ),
                )
                pattern_id = int(cursor.lastrowid)
                created = True
                reopened = False
            else:
                pattern_id = int(existing["id"])
                created = False
                reopened = existing["status"] in ("fixed_unverified", "verified")
                next_status = "open" if reopened else existing["status"]
                next_claimed_by = None if reopened else existing["claimed_by"]
                next_claim_expires = None if reopened else existing["claim_expires_at"]
                next_root_cause = (
                    clean_root_cause
                    if clean_root_cause is not None
                    else existing["root_cause"]
                )
                next_severity = existing["severity"]
                if explicit_severity is not None and (
                    _SEVERITY_RANK[explicit_severity] < _SEVERITY_RANK[next_severity]
                ):
                    next_severity = explicit_severity
                next_work_item = (
                    existing["work_item_id"]
                    if existing["work_item_id"] is not None
                    else work_item_id
                )
                self.connection.execute(
                    """
                    UPDATE failure_patterns
                    SET occurrence_count = occurrence_count + 1,
                        last_seen_at = ?,
                        status = ?,
                        claimed_by = ?,
                        claim_expires_at = ?,
                        root_cause = ?,
                        severity = ?,
                        work_item_id = ?
                    WHERE id = ?
                    """,
                    (
                        ts,
                        next_status,
                        next_claimed_by,
                        next_claim_expires,
                        next_root_cause,
                        next_severity,
                        next_work_item,
                        pattern_id,
                    ),
                )

            occ_cursor = self.connection.execute(
                """
                INSERT INTO failure_occurrences (
                    pattern_id, occurred_at, actor, workdir, work_item_id,
                    conversation_id, command, exit_code, symptom, context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern_id,
                    ts,
                    clean_actor,
                    clean_workdir,
                    work_item_id,
                    clean_conv,
                    clean_cmd,
                    exit_code,
                    clean_symptom,
                    clean_context,
                ),
            )
            occ_id = int(occ_cursor.lastrowid)
            occ_row = self.connection.execute(
                "SELECT * FROM failure_occurrences WHERE id = ?",
                (occ_id,),
            ).fetchone()
            pattern = self._get_pattern(pattern_id)
        return {
            "created": created,
            "reopened": reopened,
            "pattern": pattern,
            "occurrence": _row_to_occurrence(occ_row),
        }

    def list_patterns(
        self,
        *,
        status: str | None = None,
        repo: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        min_count: int = 1,
        include_closed: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if min_count < 1:
            raise ValueError("min_count must be at least 1")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        clauses: list[str] = ["p.occurrence_count >= ?"]
        params: list[Any] = [min_count]
        if status is not None:
            clean_status = validate_choice(status, PATTERN_STATUSES, "status")
            clauses.append("p.status = ?")
            params.append(clean_status)
        elif not include_closed:
            placeholders = ", ".join("?" for _ in ACTIONABLE_PATTERN_STATUSES)
            clauses.append(f"p.status IN ({placeholders})")
            params.extend(ACTIONABLE_PATTERN_STATUSES)

        if repo is not None:
            clauses.append("p.target_repo = ?")
            params.append(validate_repo(repo))
        if kind is not None:
            clauses.append("p.failure_kind = ?")
            params.append(validate_choice(kind, FAILURE_KINDS, "kind"))
        if severity is not None:
            clauses.append("p.severity = ?")
            params.append(validate_choice(severity, SEVERITIES, "severity"))

        where_sql = " AND ".join(clauses)
        params.append(limit)
        rows = self.connection.execute(
            f"""
            {_PATTERN_SELECT}
            WHERE {where_sql}
            ORDER BY
                CASE p.severity
                    WHEN 'high' THEN 0
                    WHEN 'normal' THEN 1
                    ELSE 2
                END,
                p.occurrence_count DESC,
                p.last_seen_at DESC,
                p.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_row_to_pattern(row) for row in rows]

    def show_pattern(
        self,
        pattern_id: int,
        *,
        occurrences_limit: int = 10,
        attempts_limit: int = 20,
    ) -> dict[str, Any]:
        if occurrences_limit < 0 or attempts_limit < 0:
            raise ValueError("limits cannot be negative")
        pattern = self._get_pattern(pattern_id)
        occ_rows = self.connection.execute(
            """
            SELECT * FROM failure_occurrences
            WHERE pattern_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (pattern_id, occurrences_limit),
        ).fetchall()
        att_rows = self.connection.execute(
            """
            SELECT * FROM improvement_attempts
            WHERE pattern_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (pattern_id, attempts_limit),
        ).fetchall()
        return {
            "pattern": pattern,
            "occurrences": [_row_to_occurrence(row) for row in occ_rows],
            "attempts": [_row_to_attempt(row) for row in att_rows],
        }

    def claim(
        self,
        *,
        actor: str,
        ttl: timedelta,
        repo: str | None = None,
        pattern_id: int | None = None,
        max_failed_attempts: int = 2,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        clean_actor = normalize_required(actor, "actor")
        if ttl.total_seconds() <= 0:
            raise ValueError("ttl must be positive")
        if max_failed_attempts < 1:
            raise ValueError("max_failed_attempts must be at least 1")
        clean_repo = validate_repo(repo) if repo is not None else None
        now_dt = now or utc_now()
        now_ts = format_ts(now_dt)
        expires_ts = format_ts(now_dt + ttl)

        with self._immediate():
            if pattern_id is not None:
                row = self.connection.execute(
                    f"{_PATTERN_SELECT} WHERE p.id = ?",
                    (pattern_id,),
                ).fetchone()
                if row is None:
                    raise LookupError(f"failure pattern {pattern_id} not found")
                if clean_repo is not None and row["target_repo"] != clean_repo:
                    raise ValueError(
                        f"failure pattern {pattern_id} belongs to repo {row['target_repo']}, not {clean_repo}"
                    )
                claimable = row["status"] == "open" or (
                    row["status"] == "claimed"
                    and row["claim_expires_at"] is not None
                    and row["claim_expires_at"] <= now_ts
                )
                if not claimable:
                    raise ValueError(
                        f"failure pattern {pattern_id} is not claimable (status={row['status']})"
                    )
                if row["failed_attempts"] >= max_failed_attempts:
                    raise ValueError(
                        f"failure pattern {pattern_id} reached failed attempt limit ({row['failed_attempts']}/{max_failed_attempts})"
                    )
                target_id = int(row["id"])
            else:
                row = self.connection.execute(
                    f"""
                    {_PATTERN_SELECT}
                    WHERE (?1 IS NULL OR p.target_repo = ?1)
                      AND (
                          p.status = 'open'
                          OR (p.status = 'claimed' AND p.claim_expires_at <= ?2)
                      )
                      AND (
                          SELECT count(*)
                          FROM improvement_attempts a
                          WHERE a.pattern_id = p.id AND a.outcome = 'failed'
                      ) < ?3
                    ORDER BY
                        CASE p.severity
                            WHEN 'high' THEN 0
                            WHEN 'normal' THEN 1
                            ELSE 2
                        END,
                        p.occurrence_count DESC,
                        p.last_seen_at DESC,
                        p.id DESC
                    LIMIT 1
                    """,
                    (clean_repo, now_ts, max_failed_attempts),
                ).fetchone()
                if row is None:
                    return None
                target_id = int(row["id"])

            self.connection.execute(
                """
                UPDATE failure_patterns
                SET status = 'claimed',
                    claimed_by = ?,
                    claim_expires_at = ?
                WHERE id = ?
                """,
                (clean_actor, expires_ts, target_id),
            )
        return self.show_pattern(target_id)

    def record_attempt(
        self,
        *,
        pattern_id: int,
        outcome: str,
        actor: str,
        branch_or_commit: str | None = None,
        summary: str | None = None,
        note: str | None = None,
        release_claim: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clean_outcome = validate_choice(outcome, ATTEMPT_OUTCOMES, "outcome")
        clean_actor = normalize_required(actor, "actor")
        clean_ref = normalize_optional(branch_or_commit)
        clean_summary = normalize_optional(summary)
        clean_note = normalize_optional(note)
        ts = format_ts(now or utc_now())

        with self._immediate():
            current = self.connection.execute(
                "SELECT id, status FROM failure_patterns WHERE id = ?",
                (pattern_id,),
            ).fetchone()
            if current is None:
                raise LookupError(f"failure pattern {pattern_id} not found")
            cursor = self.connection.execute(
                """
                INSERT INTO improvement_attempts (
                    pattern_id, actor, started_at, finished_at, outcome,
                    branch_or_commit, summary, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern_id,
                    clean_actor,
                    ts,
                    ts,
                    clean_outcome,
                    clean_ref,
                    clean_summary,
                    clean_note,
                ),
            )
            attempt_id = int(cursor.lastrowid)
            if (
                release_claim
                and clean_outcome in _RELEASE_CLAIM_OUTCOMES
                and current["status"] == "claimed"
            ):
                self.connection.execute(
                    """
                    UPDATE failure_patterns
                    SET status = 'open',
                        claimed_by = NULL,
                        claim_expires_at = NULL
                    WHERE id = ?
                    """,
                    (pattern_id,),
                )
            attempt_row = self.connection.execute(
                "SELECT * FROM improvement_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            pattern = self._get_pattern(pattern_id)
        return {
            "pattern": pattern,
            "attempt": _row_to_attempt(attempt_row),
        }

    def resolve(
        self,
        *,
        pattern_id: int,
        actor: str,
        status: str = "fixed_unverified",
        fix_layer: str | None = None,
        commit: str | None = None,
        verify_cmd: str | None = None,
        duplicate_of: int | None = None,
        note: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clean_actor = normalize_required(actor, "actor")
        clean_status = validate_choice(status, RESOLVABLE_STATUSES, "status")
        clean_fix_layer = (
            validate_choice(fix_layer, FIX_LAYERS, "fix_layer")
            if fix_layer is not None
            else None
        )
        clean_commit = normalize_optional(commit)
        clean_verify = normalize_optional(verify_cmd)
        clean_note = normalize_optional(note)
        ts = format_ts(now or utc_now())

        with self._immediate():
            current = self.connection.execute(
                "SELECT * FROM failure_patterns WHERE id = ?",
                (pattern_id,),
            ).fetchone()
            if current is None:
                raise LookupError(f"failure pattern {pattern_id} not found")

            next_fix_layer = clean_fix_layer or current["fix_layer"]
            next_commit = clean_commit or current["fix_commit"]
            next_verify = clean_verify or current["verification_cmd"]
            next_duplicate_of = duplicate_of

            if clean_status == "duplicate":
                if duplicate_of is None:
                    raise ValueError("duplicate resolution requires --duplicate-of")
                if duplicate_of == pattern_id:
                    raise ValueError("a failure pattern cannot be a duplicate of itself")
                target = self.connection.execute(
                    "SELECT id FROM failure_patterns WHERE id = ?",
                    (duplicate_of,),
                ).fetchone()
                if target is None:
                    raise LookupError(f"duplicate target pattern {duplicate_of} not found")
            else:
                next_duplicate_of = None

            if clean_status in ("fixed_unverified", "verified"):
                if next_fix_layer not in CODE_OR_DOC_FIX_LAYERS:
                    allowed = ", ".join(CODE_OR_DOC_FIX_LAYERS)
                    raise ValueError(
                        f"resolving as {clean_status} requires --fix-layer in ({allowed})"
                    )
            elif clean_status == "wontfix":
                next_fix_layer = "wontfix"

            self.connection.execute(
                """
                UPDATE failure_patterns
                SET status = ?,
                    fix_layer = ?,
                    fix_commit = ?,
                    verification_cmd = ?,
                    duplicate_of_id = ?,
                    claimed_by = NULL,
                    claim_expires_at = NULL
                WHERE id = ?
                """,
                (
                    clean_status,
                    next_fix_layer,
                    next_commit,
                    next_verify,
                    next_duplicate_of,
                    pattern_id,
                ),
            )

            attempt = None
            if clean_status in ("fixed_unverified", "verified"):
                cursor = self.connection.execute(
                    """
                    INSERT INTO improvement_attempts (
                        pattern_id, actor, started_at, finished_at, outcome,
                        branch_or_commit, summary, note
                    ) VALUES (?, ?, ?, ?, 'succeeded', ?, ?, ?)
                    """,
                    (
                        pattern_id,
                        clean_actor,
                        ts,
                        ts,
                        next_commit,
                        f"resolved as {clean_status}",
                        clean_note,
                    ),
                )
                att_row = self.connection.execute(
                    "SELECT * FROM improvement_attempts WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                attempt = _row_to_attempt(att_row)

            pattern = self._get_pattern(pattern_id)
        return {
            "pattern": pattern,
            "attempt": attempt,
        }


def doctor(db_path: Path, actor_env: str | None) -> dict[str, Any]:
    """Read-only database, schema, integrity, and actor inspection."""
    checks: list[dict[str, Any]] = []
    if not db_path.exists():
        checks.append(
            {
                "name": "database",
                "status": "warn",
                "required": False,
                "detail": f"{db_path} does not exist yet; created on first write",
            }
        )
    else:
        connection: sqlite3.Connection | None = None
        try:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_SECS)
            checks.append(
                {
                    "name": "database",
                    "status": "ok",
                    "required": True,
                    "detail": str(db_path),
                }
            )
            checks.append(_check_schema(connection))
            checks.append(_check_integrity(connection))
            checks.append(_check_journal(connection))
        except sqlite3.Error as exc:
            checks.append(
                {
                    "name": "database",
                    "status": "fail",
                    "required": True,
                    "detail": f"cannot open {db_path}: {exc}",
                }
            )
        finally:
            if connection is not None:
                connection.close()

    if actor_env is not None and actor_env.strip():
        checks.append(
            {
                "name": "actor",
                "status": "ok",
                "required": False,
                "detail": f"FAILURE_TRACKER_ACTOR={actor_env.strip()}",
            }
        )
    else:
        checks.append(
            {
                "name": "actor",
                "status": "warn",
                "required": False,
                "detail": "FAILURE_TRACKER_ACTOR is unset; writes fall back to $USER",
            }
        )

    ok = all(not (c["required"] and c["status"] == "fail") for c in checks)
    return {"ok": ok, "checks": checks}


def _check_schema(connection: sqlite3.Connection) -> dict[str, Any]:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        return {
            "name": "schema",
            "status": "fail",
            "required": True,
            "detail": f"cannot read schema version: {exc}",
        }
    if version == SCHEMA_VERSION:
        return {
            "name": "schema",
            "status": "ok",
            "required": True,
            "detail": f"version {version}",
        }
    if version > SCHEMA_VERSION:
        return {
            "name": "schema",
            "status": "fail",
            "required": True,
            "detail": f"version {version} is newer than supported {SCHEMA_VERSION}; upgrade failure-tracker",
        }
    return {
        "name": "schema",
        "status": "warn",
        "required": True,
        "detail": f"version {version}; the next write command migrates it to {SCHEMA_VERSION}",
    }


def _check_integrity(connection: sqlite3.Connection) -> dict[str, Any]:
    try:
        result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error as exc:
        return {
            "name": "integrity",
            "status": "fail",
            "required": True,
            "detail": f"quick_check failed: {exc}",
        }
    if result == "ok":
        return {
            "name": "integrity",
            "status": "ok",
            "required": True,
            "detail": "quick_check ok",
        }
    return {
        "name": "integrity",
        "status": "fail",
        "required": True,
        "detail": f"quick_check: {result}",
    }


def _check_journal(connection: sqlite3.Connection) -> dict[str, Any]:
    try:
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    except sqlite3.Error as exc:
        return {
            "name": "journal",
            "status": "warn",
            "required": False,
            "detail": f"cannot read journal mode: {exc}",
        }
    if mode.lower() == "wal":
        return {
            "name": "journal",
            "status": "ok",
            "required": False,
            "detail": "wal",
        }
    return {
        "name": "journal",
        "status": "warn",
        "required": False,
        "detail": f"{mode}; concurrent agents expect wal, which the next write command enables",
    }
