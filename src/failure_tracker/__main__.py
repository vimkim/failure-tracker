"""Module entrypoint for python -m failure_tracker."""

import sys

from failure_tracker.cli import main

if __name__ == "__main__":
    sys.exit(main())
