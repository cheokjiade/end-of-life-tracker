"""EOL Checker Lambda — shim entry point.

The runtime lives in the :mod:`eoltracker` package (one file per parser, with
auto-registration). This module preserves the Lambda handler path
``lambda_function.lambda_handler`` by re-exporting it, and provides the local
CLI (``python lambda_function.py <config.json>``) plus a network-free config
lint (``python lambda_function.py --validate <config.json>``).

See ``docs/adding-a-provider.md`` and ``CLAUDE.md`` for the architecture.
"""

from eoltracker.handler import lambda_handler, run_local  # noqa: F401  (Lambda handler entry)

if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if argv and (argv[0] == "--validate" or argv[0].startswith("--validate=")):
        # Network-free structural validation: python lambda_function.py
        # --validate eol_config.<project>.json   (exit 0 valid, 1 invalid)
        from eoltracker.validation import main as validate_main
        validate_args = argv[1:]
        if argv[0].startswith("--validate="):
            validate_args = [argv[0].split("=", 1)[1], *argv[1:]]
        sys.exit(validate_main(validate_args))
    run_local(argv[0] if argv else "eol_config.a.json")
