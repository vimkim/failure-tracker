"""Unit and CLI tests for failure-tracker."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from failure_tracker.db import FailureStore, doctor
from failure_tracker.domain import SCHEMA_VERSION, parse_ttl, validate_repo


class FailureStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "failures.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_schema_initialization_wal_and_user_version(self) -> None:
        with FailureStore.open(self.db_path) as store:
            version = store.connection.execute("PRAGMA user_version").fetchone()[0]
            mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
            fk = store.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(mode.lower(), "wal")
        self.assertEqual(fk, 1)

    def test_record_deduplicates_and_custom_fingerprint_key_groups(self) -> None:
        t0 = datetime(2026, 11, 26, 1, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=5)
        with FailureStore.open(self.db_path) as store:
            first = store.record(
                repo="my-skills",
                ref="skills/example/SKILL.md",
                kind="skill_gap",
                title="Missing preflight check",
                symptom="Config missing on first run",
                actor="agent-a",
                command="tool run",
                exit_code=2,
                context_json={"step": "preflight"},
                now=t0,
            )
            second = store.record(
                repo="my-skills",
                ref="skills/example/SKILL.md",
                kind="skill_gap",
                title="  missing   preflight check ",
                symptom="Config missing on second run",
                actor="agent-b",
                severity="high",
                root_cause="Preflight skipped when env var unset",
                work_item_id=42,
                now=t1,
            )
            third = store.record(
                repo="my-skills",
                ref="skills/example/SKILL.md",
                kind="skill_gap",
                title="Different wording for same bug",
                fingerprint_key="missing preflight check",
                symptom="Grouped via explicit fingerprint key",
                actor="agent-c",
                now=t1 + timedelta(minutes=1),
            )
            detail = store.show_pattern(first["pattern"]["id"], occurrences_limit=10)

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertFalse(third["created"])
        self.assertEqual(first["pattern"]["id"], second["pattern"]["id"])
        self.assertEqual(first["pattern"]["id"], third["pattern"]["id"])
        self.assertEqual(detail["pattern"]["occurrence_count"], 3)
        self.assertEqual(detail["pattern"]["severity"], "high")
        self.assertEqual(detail["pattern"]["work_item_id"], 42)
        self.assertEqual(
            detail["pattern"]["root_cause"],
            "Preflight skipped when env var unset",
        )
        self.assertEqual(len(detail["occurrences"]), 3)
        self.assertEqual(detail["occurrences"][-1]["context"], {"step": "preflight"})

    def test_record_auto_reopens_fixed_unverified_and_verified_patterns(self) -> None:
        t0 = datetime(2026, 11, 26, 2, 0, 0, tzinfo=timezone.utc)
        with FailureStore.open(self.db_path) as store:
            rec = store.record(
                repo="example-tool",
                ref="src/cli.py",
                kind="tool_bug",
                title="Crash on empty input",
                symptom="IndexError",
                actor="ci",
                now=t0,
            )
            pid = rec["pattern"]["id"]
            store.resolve(
                pattern_id=pid,
                actor="worker-1",
                status="fixed_unverified",
                fix_layer="code_or_check",
                commit="abc1234",
                verify_cmd="just check",
                now=t0 + timedelta(minutes=1),
            )
            recur_unverified = store.record(
                repo="example-tool",
                ref="src/cli.py",
                kind="tool_bug",
                title="Crash on empty input",
                symptom="IndexError again",
                actor="ci",
                now=t0 + timedelta(minutes=2),
            )
            self.assertTrue(recur_unverified["reopened"])
            self.assertEqual(recur_unverified["pattern"]["status"], "open")

            store.resolve(
                pattern_id=pid,
                actor="worker-2",
                status="verified",
                fix_layer="code_or_check",
                commit="def5678",
                verify_cmd="just check",
                now=t0 + timedelta(minutes=3),
            )
            recur_verified = store.record(
                repo="example-tool",
                ref="src/cli.py",
                kind="tool_bug",
                title="Crash on empty input",
                symptom="Regression after merge",
                actor="ci",
                now=t0 + timedelta(minutes=4),
            )
            self.assertTrue(recur_verified["reopened"])
            self.assertEqual(recur_verified["pattern"]["status"], "open")
            self.assertEqual(recur_verified["pattern"]["occurrence_count"], 3)

    def test_claim_priority_lease_expiry_and_failed_attempt_limit(self) -> None:
        t0 = datetime(2026, 11, 26, 3, 0, 0, tzinfo=timezone.utc)
        with FailureStore.open(self.db_path) as store:
            low_p = store.record(
                repo="my-skills",
                ref="a.md",
                kind="skill_gap",
                title="Low pattern",
                symptom="minor",
                severity="low",
                actor="user",
                now=t0,
            )["pattern"]["id"]
            high_p = store.record(
                repo="my-skills",
                ref="b.md",
                kind="tool_bug",
                title="High pattern",
                symptom="major",
                severity="high",
                actor="user",
                now=t0,
            )["pattern"]["id"]

            # Claim selects highest priority first
            claimed = store.claim(actor="w1", ttl=timedelta(minutes=30), repo="my-skills", now=t0)
            assert claimed is not None
            self.assertEqual(claimed["pattern"]["id"], high_p)
            self.assertEqual(claimed["pattern"]["status"], "claimed")

            # While lease is active, second claim gets the next open pattern
            claimed2 = store.claim(actor="w2", ttl=timedelta(minutes=30), repo="my-skills", now=t0 + timedelta(minutes=5))
            assert claimed2 is not None
            self.assertEqual(claimed2["pattern"]["id"], low_p)

            # No open or expired patterns left
            self.assertIsNone(
                store.claim(actor="w3", ttl=timedelta(minutes=30), repo="my-skills", now=t0 + timedelta(minutes=10))
            )

            # Explicit claim on actively claimed pattern raises ValueError
            with self.assertRaises(ValueError):
                store.claim(actor="w3", ttl=timedelta(minutes=10), pattern_id=high_p, now=t0 + timedelta(minutes=10))

            # Expired lease is reclaimed automatically
            reclaimed = store.claim(
                actor="w3",
                ttl=timedelta(minutes=15),
                repo="my-skills",
                now=t0 + timedelta(minutes=31),
            )
            assert reclaimed is not None
            self.assertEqual(reclaimed["pattern"]["id"], high_p)
            self.assertEqual(reclaimed["pattern"]["claimed_by"], "w3")

            # Two failed attempts put high_p over max_failed_attempts=2
            store.record_attempt(
                pattern_id=high_p,
                outcome="failed",
                actor="w3",
                note="attempt 1",
                now=t0 + timedelta(minutes=32),
            )
            reclaimed2 = store.claim(
                actor="w4",
                ttl=timedelta(minutes=15),
                pattern_id=high_p,
                now=t0 + timedelta(minutes=33),
            )
            assert reclaimed2 is not None
            store.record_attempt(
                pattern_id=high_p,
                outcome="failed",
                actor="w4",
                note="attempt 2",
                now=t0 + timedelta(minutes=34),
            )
            with self.assertRaises(ValueError):
                store.claim(
                    actor="w5",
                    ttl=timedelta(minutes=15),
                    pattern_id=high_p,
                    max_failed_attempts=2,
                    now=t0 + timedelta(minutes=35),
                )

    def test_concurrent_claims_yield_single_winner(self) -> None:
        with FailureStore.open(self.db_path) as store:
            pattern_id = store.record(
                repo="my-skills",
                ref="skills/demo/SKILL.md",
                kind="rule_drift",
                title="Contended pattern",
                symptom="failed check",
                actor="ci",
            )["pattern"]["id"]

        barrier = threading.Barrier(8)
        results: list[int | None] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            try:
                with FailureStore.open(self.db_path) as worker_store:
                    barrier.wait(timeout=5)
                    claimed = worker_store.claim(
                        actor=f"worker-{idx}",
                        ttl=timedelta(minutes=30),
                        repo="my-skills",
                    )
                    with lock:
                        results.append(claimed["pattern"]["id"] if claimed else None)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(results.count(pattern_id), 1)
        self.assertEqual(results.count(None), 7)

    def test_resolve_lifecycle_and_duplicate_validation(self) -> None:
        with FailureStore.open(self.db_path) as store:
            p1 = store.record(
                repo="workflow-scripts",
                ref="check.py",
                kind="tool_bug",
                title="Primary bug",
                symptom="boom",
                actor="ci",
            )["pattern"]["id"]
            p2 = store.record(
                repo="workflow-scripts",
                ref="check.py",
                kind="tool_bug",
                title="Alias bug",
                symptom="boom alias",
                actor="ci",
            )["pattern"]["id"]
            p3 = store.record(
                repo="workflow-scripts",
                ref="notes.md",
                kind="agent_mistake",
                title="One-off typo",
                symptom="ignored",
                actor="user",
            )["pattern"]["id"]

            # Self-duplicate rejected
            with self.assertRaises(ValueError):
                store.resolve(
                    pattern_id=p2,
                    actor="worker",
                    status="duplicate",
                    duplicate_of=p2,
                )

            # fixed_unverified requires fix_layer
            with self.assertRaises(ValueError):
                store.resolve(pattern_id=p1, actor="worker", status="fixed_unverified")

            res_dup = store.resolve(
                pattern_id=p2,
                actor="worker",
                status="duplicate",
                duplicate_of=p1,
                note="Same root cause as #1",
            )
            self.assertEqual(res_dup["pattern"]["status"], "duplicate")
            self.assertEqual(res_dup["pattern"]["duplicate_of_id"], p1)

            res_wontfix = store.resolve(
                pattern_id=p3,
                actor="worker",
                status="wontfix",
                note="Not worth automating",
            )
            self.assertEqual(res_wontfix["pattern"]["status"], "wontfix")
            self.assertEqual(res_wontfix["pattern"]["fix_layer"], "wontfix")

            res_fix = store.resolve(
                pattern_id=p1,
                actor="worker",
                status="fixed_unverified",
                fix_layer="code_or_check",
                commit="111aaaa",
                verify_cmd="just check",
            )
            self.assertEqual(res_fix["pattern"]["status"], "fixed_unverified")
            self.assertIsNotNone(res_fix["attempt"])
            self.assertEqual(res_fix["attempt"]["outcome"], "succeeded")

            res_ver = store.resolve(
                pattern_id=p1,
                actor="reviewer",
                status="verified",
            )
            self.assertEqual(res_ver["pattern"]["status"], "verified")
            self.assertEqual(res_ver["pattern"]["fix_layer"], "code_or_check")

    def test_doctor_is_readonly_and_detects_missing_newer_and_corrupt_dbs(self) -> None:
        missing = Path(self.tempdir.name) / "nonexistent.db"
        report_missing = doctor(missing, None)
        self.assertTrue(report_missing["ok"])
        self.assertFalse(missing.exists())
        checks_missing = {c["name"]: c for c in report_missing["checks"]}
        self.assertEqual(checks_missing["database"]["status"], "warn")
        self.assertEqual(checks_missing["actor"]["status"], "warn")

        with FailureStore.open(self.db_path):
            pass
        report_ok = doctor(self.db_path, "agent-1")
        self.assertTrue(report_ok["ok"])
        checks_ok = {c["name"]: c for c in report_ok["checks"]}
        self.assertEqual(checks_ok["schema"]["status"], "ok")
        self.assertEqual(checks_ok["integrity"]["status"], "ok")
        self.assertEqual(checks_ok["journal"]["status"], "ok")
        self.assertEqual(checks_ok["actor"]["status"], "ok")

        # Newer schema version fails
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()
        report_newer = doctor(self.db_path, "agent-1")
        self.assertFalse(report_newer["ok"])

        # Corrupt database file fails
        corrupt = Path(self.tempdir.name) / "corrupt.db"
        corrupt.write_text("not a sqlite database")
        report_corrupt = doctor(corrupt, "agent-1")
        self.assertFalse(report_corrupt["ok"])

    def test_domain_helpers(self) -> None:
        self.assertEqual(parse_ttl("30m"), timedelta(minutes=30))
        self.assertEqual(parse_ttl("2h"), timedelta(hours=2))
        with self.assertRaises(ValueError):
            parse_ttl("0m")
        with self.assertRaises(ValueError):
            parse_ttl("bad")
        self.assertEqual(validate_repo("owner/repo-name"), "owner/repo-name")
        with self.assertRaises(ValueError):
            validate_repo("bad repo")
        with self.assertRaises(ValueError):
            validate_repo("../escape")


class CliSubprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "cli-failures.db"
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(SRC_DIR)
        self.env["FAILURE_TRACKER_DB"] = str(self.db_path)
        self.env["FAILURE_TRACKER_ACTOR"] = "test-cli-actor"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "failure_tracker", *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_end_to_end_cli_json_and_human_outputs(self) -> None:
        path_proc = self.run_cli("path")
        self.assertEqual(path_proc.returncode, 0, path_proc.stderr)
        self.assertEqual(path_proc.stdout.strip(), str(self.db_path))

        doc_before = self.run_cli("--json", "doctor")
        self.assertEqual(doc_before.returncode, 0, doc_before.stderr)
        self.assertTrue(json.loads(doc_before.stdout)["ok"])
        self.assertFalse(self.db_path.exists())

        rec = self.run_cli(
            "--json",
            "record",
            "--repo",
            "my-skills",
            "--ref",
            "skills/demo/SKILL.md",
            "--kind",
            "skill_gap",
            "--title",
            "Missing guard",
            "--symptom",
            "Script errored on blank line",
            "--command",
            "demo --check",
            "--exit-code",
            "1",
        )
        self.assertEqual(rec.returncode, 0, rec.stderr)
        rec_payload = json.loads(rec.stdout)
        pid = rec_payload["pattern"]["id"]
        self.assertTrue(rec_payload["created"])

        lst_human = self.run_cli("list")
        self.assertEqual(lst_human.returncode, 0, lst_human.stderr)
        self.assertIn("Missing guard", lst_human.stdout)

        claim = self.run_cli("--json", "claim", "--repo", "my-skills", "--ttl", "10m")
        self.assertEqual(claim.returncode, 0, claim.stderr)
        claim_payload = json.loads(claim.stdout)
        self.assertEqual(claim_payload["pattern"]["id"], pid)
        self.assertEqual(claim_payload["pattern"]["claimed_by"], "test-cli-actor")

        att = self.run_cli(
            "attempt",
            str(pid),
            "--outcome",
            "blocked",
            "--note",
            "Waiting for upstream test fix",
        )
        self.assertEqual(att.returncode, 0, att.stderr)
        self.assertIn("status=open", att.stdout)

        res = self.run_cli(
            "--json",
            "resolve",
            str(pid),
            "--fix-layer",
            "code_or_check",
            "--commit",
            "abc9999",
            "--verify",
            "just check",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        res_payload = json.loads(res.stdout)
        self.assertEqual(res_payload["pattern"]["status"], "fixed_unverified")

        show_human = self.run_cli("show", str(pid))
        self.assertEqual(show_human.returncode, 0, show_human.stderr)
        self.assertIn("Occurrences (showing 1):", show_human.stdout)
        self.assertIn("Improvement attempts (showing 2):", show_human.stdout)

        # Missing pattern returns nonzero exit code and JSON error
        missing = self.run_cli("--json", "show", "9999")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("not found", json.loads(missing.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
