"""EOL Checker — safe local HTML-only report runner (CLI shim).

Thin wrapper around :func:`eoltracker.html_runner.main`, mirroring the
``lambda_function.py`` shim style. Runs every requested config in one process
(provider caches are reused across configs), performs live checks, and writes
only HTML reports — console/SNS/SES from the configs are suppressed.

Usage:
    python run_html_report.py eol_config.beta.json
    python run_html_report.py a beta
    python run_html_report.py --all

See ``eoltracker/html_runner.py`` for the implementation.
"""

import sys

from eoltracker.html_runner import main

if __name__ == "__main__":
    sys.exit(main())
