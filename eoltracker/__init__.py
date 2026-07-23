"""EOL tracker package.

The runtime, split from the original single ``lambda_function.py`` into a
small package: shared primitives in :mod:`eoltracker.core`, one file per
data-source provider under :mod:`eoltracker.parsers` (auto-registered), the
report formatters in :mod:`eoltracker.report`, notification channels in
:mod:`eoltracker.notify`, and the Lambda/CLI entry points in
:mod:`eoltracker.handler`.
"""

__version__ = "1.0.0"
