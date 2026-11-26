# Failure Tracker Agent Guide

## Architecture

- `src/failure_tracker/domain.py` owns enums, normalization, fingerprinting, and path/actor resolution.
- `src/failure_tracker/db.py` owns SQLite schema, `BEGIN IMMEDIATE` transactions, lease claims, and read-only `doctor` checks.
- `src/failure_tracker/output.py` owns human-readable and JSON formatters.
- `src/failure_tracker/cli.py` owns the CLI contract and nonzero error exits.

## Invariants

- SQLite is the single source of truth; enable WAL and `PRAGMA foreign_keys = ON`.
- `record` deduplicates by `fingerprint`, appends an immutable row to `failure_occurrences`, and automatically reopens `fixed_unverified` or `verified` patterns on recurrence.
- `claim` uses `BEGIN IMMEDIATE` and `claim_expires_at` leases so parallel workers never claim the same pattern concurrently and crashed workers' leases expire automatically.
- `doctor` is strictly read-only and never creates or mutates the database file.

## Verification

- Run `just check` before committing changes.
