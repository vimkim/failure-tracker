# Show available commands.
default:
    @just --list

alias install := sync

# Run tests and smoke-check the CLI entrypoint.
check: test
    uv run failure-tracker --help >/dev/null
    uv run failure-tracker --version >/dev/null

# Run behavior tests.
test *args:
    uv run python -m unittest discover -s tests -v {{args}}

# Execute failure-tracker once.
run *args:
    uv run failure-tracker {{args}}

# Install or refresh failure-tracker on user PATH.
sync:
    #!/usr/bin/env bash
    set -euo pipefail
    uv tool install --reinstall .
    failure-tracker --help >/dev/null
    failure-tracker --version >/dev/null
