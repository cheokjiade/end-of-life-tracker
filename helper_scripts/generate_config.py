"""Generate an EOL tracker config and inventory from a project's dependencies.

Scans a folder for Maven, Gradle, and Node manifests; emits an
eol_config.<project>.json file suitable for use with lambda_function.py.
Every mapped product carries structured provenance (`_found_in`), and the
config carries an ignored `_inventory` object with warnings and unmapped
items. Standard-library only; project files are never executed.

Supported formats:
    pom.xml             — Maven (multi-module)
    *.gradle.kts        — Gradle Kotlin DSL
    build.gradle        — Gradle Groovy DSL (same regex patterns)
    package.json        — Node (node_modules and other build dirs excluded)

Usage:
    python generate_config.py <folder> [--name PROJECT] [--output FILE]
                              [--exclude PATTERN] [--force] [--strict]

Examples:
    python generate_config.py "project-b" --name b
    python generate_config.py ssg-frontend --name frontend --force
"""

import argparse
import json
import os
import sys
import tempfile

from eol_inventory import generate_config, scan_folder


def _atomic_write_json(config, output):
    """Write config JSON atomically: temp file in the target dir + os.replace.

    Output is deterministic ASCII (ensure_ascii=True, fixed indent).
    """
    dir_name = os.path.dirname(os.path.abspath(output))
    fd, tmp_path = tempfile.mkstemp(
        dir=dir_name, prefix=".eol_config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
            json.dump(config, f, indent=2, ensure_ascii=True)
            f.write("\n")
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
    parser.add_argument("folder", help="Folder to scan (recursively) for dependency files")
    parser.add_argument("--name", help="Project name (default: folder basename)", default=None)
    parser.add_argument("--output", help="Output file (default: eol_config.<name>.json)", default=None)
    parser.add_argument("--exclude", action="append", default=[],
                        help="Extra exclusion pattern (repeatable; same syntax as .eolignore)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing output file without asking")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero when any scan warning is emitted (for CI)")
    args = parser.parse_args(argv)

    folder = args.folder
    project_name = args.name or os.path.basename(os.path.normpath(folder)).replace(" ", "-").lower()
    output = args.output or f"eol_config.{project_name}.json"

    if os.path.exists(output) and not args.force:
        print(f"Refusing to overwrite existing file: {output}", file=sys.stderr)
        print("Re-run with --force to overwrite it.", file=sys.stderr)
        return 2

    print(f"Scanning {folder!r}...")
    scan = scan_folder(folder, exclude=args.exclude)

    print(f"  Files scanned        : {len(scan['files'])}")
    print(f"  Dependency records   : {len(scan['records'])}")
    print(f"  Scan warnings        : {len(scan['warnings'])}")

    config = generate_config(scan, project_name)
    inventory = config["_inventory"]

    _atomic_write_json(config, output)

    print(f"\nWrote {output}")
    print(f"  Tracker entries     : {inventory['summary']['products']}")
    print(f"  Unmapped items      : {inventory['summary']['unmapped']}"
          " (see _inventory.unmapped - review)")
    if inventory["warnings"]:
        print("  Warnings:")
        for warning in inventory["warnings"]:
            print(f"    - [{warning['category']}] {warning['path']}: "
                  f"{warning['message']}")

    if args.strict and inventory["warnings"]:
        print(f"\n--strict: {inventory['summary']['warnings']} warning(s) "
              "emitted; failing.", file=sys.stderr)
        return 1

    print("\nNext: review the file, then run")
    print(f"  python lambda_function.py {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
