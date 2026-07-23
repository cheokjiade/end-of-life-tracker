"""EOL Checker Lambda — shim entry point.

The runtime lives in the :mod:`eoltracker` package (one file per parser, with
auto-registration). This module preserves the Lambda handler path
``lambda_function.lambda_handler`` by re-exporting it, and provides the local
CLI (``python lambda_function.py <config.json>``).

See ``docs/adding-a-provider.md`` and ``CLAUDE.md`` for the architecture.
"""

from eoltracker.handler import lambda_handler, run_local  # noqa: F401  (Lambda handler entry)

if __name__ == "__main__":
    import sys
    run_local(sys.argv[1] if len(sys.argv) > 1 else "eol_config.a.json")
