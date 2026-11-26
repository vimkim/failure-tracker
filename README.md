# Failure Tracker

SQLite ledger and CLI for tracking recurring workflow, skill, hook, and tool failures so parallel agents can fix root causes structurally.

## Design

- **Deduplicated patterns (`failure_patterns`).** Failures with the same `(target_repo, target_ref, failure_kind, key)` fingerprint increment `occurrence_count` instead of creating duplicate work.
- **Append-only occurrences (`failure_occurrences`).** Every `record` call appends raw command, exit code, actor, workdir, and symptom evidence.
- **Improvement attempts (`improvement_attempts`).** Tracks agent attempts and blocks endless retries once failed attempts reach the limit.
- **Automatic reopen.** If a `fixed_unverified` or `verified` pattern recurs, `record` reopens it to `open`.
- **Lease-based parallel claiming.** `claim` acquires an expiring lease (`--ttl 30m`) inside an immediate SQLite transaction.

## Installation

```bash
uv tool install --reinstall .
# or via just:
just sync
```

## CLI Usage

```bash
# Record a failure occurrence
failure-tracker record \
  --repo my-skills \
  --ref skills/example/SKILL.md \
  --kind skill_gap \
  --title "Missing preflight check" \
  --symptom "Command failed when config file was absent" \
  --json

# List actionable patterns (open, claimed, fixed_unverified)
failure-tracker list --min-count 2 --json

# Claim the highest-priority open or expired-lease pattern for a repo
failure-tracker claim --repo my-skills --actor worker-1 --ttl 30m --json

# Record a failed or blocked attempt (releases the claim back to open)
failure-tracker attempt 1 --outcome failed --actor worker-1 --note "Patch broke unit tests" --json

# Resolve a pattern with fix layer, commit, and verification command
failure-tracker resolve 1 --fix-layer code_or_check --commit abc1234 --verify "just check" --actor worker-1 --json

# Read-only health check
failure-tracker doctor --json
```

## Development

```bash
just check
just test
just sync
```
