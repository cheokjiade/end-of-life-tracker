"""Render a human-readable dependency inventory from an EOL tracker config.

Reads an eol_config JSON file locally (no network) and writes Markdown,
CSV, and self-contained HTML inventories by default. Reports include
inventory report: tracked products grouped by ecosystem and provider,
container images, unmapped dependencies, warnings, summary counts, and a
manual-review checklist. Optional CSV output supports spreadsheet
imports. Legacy configs without `_inventory` remain readable.
Standard-library only; project files are never executed.

Usage:
    python helper_scripts/generate_inventory_report.py <config> [--output FILE]
           [--csv [FILE]] [--html [FILE]] [--no-csv] [--no-html] [--force]

Examples:
    python helper_scripts/generate_inventory_report.py eol_config.demo.json
    python helper_scripts/generate_inventory_report.py old.json --output inv.md --force
"""

import argparse
import json
import os
import sys
import tempfile

from eol_inventory import report_writer

_CSV_UNSET = object()
_HTML_UNSET = object()


def _atomic_write_text(text, output):
    """Write UTF-8 text atomically: temp file in the target dir + os.replace."""
    dir_name = os.path.dirname(os.path.abspath(output))
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=dir_name, prefix=".inventory_report-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, output)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:", 1)[1] if "Usage:" in __doc__ else "",
    )
    parser.add_argument("config", help="Path to an eol_config JSON file")
    parser.add_argument("--output",
                        help="Markdown output file "
                             "(default: reports/inventory/<slug>-inventory.md)",
                        default=None)
    parser.add_argument("--csv", nargs="?", const=None, default=_CSV_UNSET,
                        metavar="FILE",
                        help="CSV output path (default file: "
                             "reports/inventory/<slug>-inventory.csv)")
    parser.add_argument("--html", nargs="?", const=None, default=_HTML_UNSET,
                        metavar="FILE", help="HTML output path (default file: "
                        "reports/inventory/<slug>-inventory.html)")
    parser.add_argument("--no-csv", action="store_true",
                        help="Do not generate CSV")
    parser.add_argument("--no-html", action="store_true",
                        help="Do not generate HTML")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing output files without asking")
    args = parser.parse_args(argv)

    config_path = args.config
    slug = report_writer.project_slug(config_path)
    output = args.output or os.path.join(
        "reports", "inventory", f"{slug}-inventory.md")
    if args.no_csv:
        csv_output = None
    elif args.csv is _CSV_UNSET or args.csv is None:
        csv_output = os.path.join(
            "reports", "inventory", f"{slug}-inventory.csv")
    else:
        csv_output = args.csv

    if args.no_html:
        html_output = None
    elif args.html is _HTML_UNSET or args.html is None:
        html_output = os.path.join(
            "reports", "inventory", f"{slug}-inventory.html")
    else:
        html_output = args.html

    targets = [output]
    targets.extend(t for t in (csv_output, html_output) if t)
    existing = [t for t in targets if os.path.exists(t)]
    if existing and not args.force:
        for target in existing:
            print(f"Refusing to overwrite existing file: {target}",
                  file=sys.stderr)
        print("Re-run with --force to overwrite it.", file=sys.stderr)
        return 2

    print(f"Reading {config_path!r}...")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"Could not read config file: {config_path}", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 2
    if not isinstance(config, dict):
        print(f"Could not read config file: {config_path}", file=sys.stderr)
        print("  top-level JSON value is not an object", file=sys.stderr)
        return 2

    view = report_writer.build_inventory_view(config, project_name=slug)
    product_count = len(view["products"]) + len(view["containers"])
    print(f"  Products            : {product_count}")
    print(f"  Unmapped items      : {len(view['unmapped'])}")
    print(f"  Warnings            : {view['meta']['warning_count']}")

    _atomic_write_text(report_writer.render_markdown(view), output)
    if csv_output:
        _atomic_write_text(report_writer.render_csv(view), csv_output)
    if html_output:
        _atomic_write_text(report_writer.render_html(view), html_output)

    print(f"\nWrote {output}")
    if csv_output:
        print(f"Wrote {csv_output}")
    if html_output:
        print(f"Wrote {html_output}")
    print("\nReview the 'Manual review checklist' section of the report "
          "before relying on this inventory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
