"""HTML-only local report runner.

A safe, cross-platform way to produce HTML reports locally for one or many
per-project configs in a single Python process:

* One process handles every config, so provider-level caches (endoflife.date
  product fetches, Maven Central lookups, ...) are reused across configs
  instead of being paid once per invocation.
* Configs are loaded unchanged (:func:`eoltracker.handler.load_config_from_file`)
  and are never mutated; checks always go live through the same bounded runner
  used by the Lambda handler.
* Console, SNS and SES are suppressed no matter what the config says — reports
  are delivered through the public ``send_notifications`` with a temporary
  html_file-only notification view of each config (never private helpers), so
  running this tool can never print a report to stdout or touch AWS services.
* When a config declares no usable ``html_file`` entry, a safe default output
  is derived from the config's own filename (``eol_config.beta.json`` ->
  ``eol_report_beta.html``, matching the repo's eol_report_<name> convention)
  so concurrently run configs never overwrite one another's reports;
  unmatchable names fall back to plain ``eol_report.html``.

This tool intentionally differs from ``run.sh`` / ``run.ps1``, which honour the
configured channels (including console printing and any SNS/SES routing).
"""

import argparse
import glob
import os
from datetime import date

from .core import logger
from .handler import build_subject, load_config_from_file
from .notify import send_notifications
from .report import analyse_results, format_report_html
from .runner import run_checks


DEFAULT_HTML_PATH = "eol_report.html"
CONFIG_GLOB = "eol_config.*.json"
SAMPLE_CONFIG_NAME = "eol_config.sample.json"

# ---------------------------------------------------------------------------
# Discovery and argument resolution
# ---------------------------------------------------------------------------

def discover_configs(directory="."):
    """Find every ``eol_config.*.json`` in *directory*, sorted, sample excluded.

    ``eol_config.sample.json`` is the template, not a live inventory, so it is
    never included here; passing it explicitly on the command line still works.
    """
    matches = glob.glob(os.path.join(directory, CONFIG_GLOB))
    names = sorted(
        m for m in matches
        if os.path.basename(m) != SAMPLE_CONFIG_NAME and os.path.isfile(m)
    )
    return [os.path.normpath(n) for n in names]


def expand_config_arg(arg):
    """Resolve a CLI argument to a config path, or None if unresolvable.

    Accepts an existing path as-is, or a shorthand ("beta" -> eol_config.beta.json),
    mirroring run.sh / run.ps1. Explicitly naming ``eol_config.sample.json`` is
    permitted; only bulk --all discovery excludes it.
    """
    if os.path.isfile(arg):
        return os.path.normpath(arg)
    shorthand = f"eol_config.{arg}.json"
    if os.path.isfile(shorthand):
        return os.path.normpath(shorthand)
    return None


# ---------------------------------------------------------------------------
# HTML-only config view
# ---------------------------------------------------------------------------

def fallback_output_for(config_path):
    """Derive a collision-free default html_file path from the config filename.

    'eol_config.beta.json'  -> 'eol_report_beta.html'
    '/any/where/team.json'  -> 'eol_report_team.html'
    unmatchable names       -> DEFAULT_HTML_PATH

    Following the repo's eol_report_<name> convention, each derived base gets
    its own reports/<project>/ folder and timestamped filename, so two
    configs that both omit an html_file path cannot overwrite one another's
    reports when --all runs them in the same minute.
    """
    stem = os.path.splitext(os.path.basename(str(config_path)))[0]
    if stem == "eol_config":
        return DEFAULT_HTML_PATH
    if stem.startswith("eol_config."):
        stem = stem[len("eol_config."):]
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)
    if not safe or safe == "sample":
        return DEFAULT_HTML_PATH
    return f"eol_report_{safe}.html"


def _configured_html_path(config):
    """First usable html_file 'path' declared by *config*, else None."""
    for notif in config.get("notifications") or []:
        if isinstance(notif, dict) and notif.get("type") == "html_file":
            return notif.get("path")
    return None


def build_html_only_config(config, default_path=DEFAULT_HTML_PATH):
    """Return an HTML-only view of *config* suitable for send_notifications.

    A shallow copy carries a replaced ``notifications`` list holding exactly
    one html_file entry: the configured path when present and non-empty,
    otherwise *default_path*. Console/SNS/SES entries therefore cannot fire
    regardless of the source config, and the source config itself is left
    untouched — caller-owned dicts are shared reference-wise but never edited.
    """
    run_cfg = dict(config)
    run_cfg["notifications"] = [
        {"type": "html_file",
         "path": _configured_html_path(config) or default_path}
    ]
    return run_cfg


# ---------------------------------------------------------------------------
# Running one config
# ---------------------------------------------------------------------------

def run_config(path, today=None, notifier=send_notifications):
    """Check every product in *path* and deliver an HTML-only report.

    Loads the config unchanged, performs live provider checks, renders the
    HTML report, and hands it to the public notification dispatcher with an
    html_file-only view (so console/SNS/SES stay silent even if configured).
    Returns a summary dict with the written path, status counts, lifecycle and
    tracker-health flags, and unfinished count. Raises on unreadable/invalid
    configs.
    """
    today = today or date.today()
    config = load_config_from_file(path)

    products = config.get("products") if isinstance(config, dict) else None
    if not isinstance(products, list):
        raise ValueError("config must contain a 'products' array")

    thresholds = config.get("alert_thresholds_days", [30, 60, 90])
    results, run_meta = run_checks(products, today)

    analysis = analyse_results(results, thresholds)
    report_html, _has_alerts = format_report_html(results, thresholds, today)

    subject = build_subject(analysis, None, today)
    outcomes = notifier(
        build_html_only_config(config, fallback_output_for(path)),
        "", report_html, subject,
    ) or []
    output = next((
        outcome.get("output")
        for outcome in outcomes
        if outcome.get("channel") == "html_file" and outcome.get("delivered")
    ), None)

    return {
        "config": path,
        "output": output,
        "checked": len(results),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "unknown": sum(1 for r in results if r.get("status") == "unknown"),
        "has_alerts": analysis["has_lifecycle_alerts"],
        "has_health_failures": analysis["has_health_failures"],
        "unfinished": run_meta["unfinished"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI body: resolve configs, check them in-process, print summaries."""
    parser = argparse.ArgumentParser(
        prog="run_html_report.py",
        description=(
            "Run EOL checks for one or more local configs and write HTML-only "
            "reports. Console/SNS/SES are suppressed; nothing leaves this "
            "machine but the provider APIs themselves."
        ),
    )
    parser.add_argument(
        "configs", nargs="*", metavar="CONFIG",
        help="a config path, or a shorthand name (demo -> eol_config.demo.json); repeatable",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="also check every local eol_config.*.json (eol_config.sample.json excluded)",
    )
    args = parser.parse_args(argv)

    paths, seen, problems = [], set(), []

    def add(resolved):
        key = os.path.normcase(os.path.abspath(resolved))
        if key not in seen:
            seen.add(key)
            paths.append(resolved)

    for raw in args.configs:
        resolved = expand_config_arg(raw)
        if resolved is None:
            problems.append(f"no config matching '{raw}' (tried '{raw}' and 'eol_config.{raw}.json')")
        else:
            add(resolved)

    if args.all:
        discovered = discover_configs(".")
        if discovered:
            for p in discovered:
                add(p)
        else:
            problems.append("--all: no eol_config.*.json files found in the current directory")

    if problems:
        for p in problems:
            logger.error("%s", p)
        parser.print_usage()
        return 1
    if not paths:
        parser.print_usage()
        logger.error("nothing to do: pass one or more CONFIG paths, or use --all")
        return 1

    today = date.today()
    generated, failed = 0, 0

    for p in paths:
        try:
            s = run_config(p, today=today)
        except Exception as exc:
            logger.error("Config '%s' failed: %s", p, exc)
            failed += 1
            continue
        if not s["output"]:
            logger.error("Config '%s': no HTML report was written (dispatcher reported no output)", p)
            failed += 1
            continue
        generated += 1
        print(
            f"[OK] {p} -> {s['output']} "
            f"(checked={s['checked']}, error={s['errors']}, unknown={s['unknown']})"
        )

    print(f"Done: {generated} report(s) generated, {failed} config(s) failed.")
    return 0 if failed == 0 else 1
