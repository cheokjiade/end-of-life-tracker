"""Generate an EOL tracker config and inventory from a project's dependencies.

Scans a folder for Java, Node, Python, Go, and .NET manifests plus
Dockerfile and GitLab CI image declarations; emits an
eol_config.<project>.json file suitable for use with lambda_function.py.
Every mapped product carries structured provenance (`_found_in`), and the
config carries an ignored `_inventory` object with warnings and unmapped
items. Standard-library only; project files are never executed.

Supported formats:
    pom.xml, *.gradle.kts, build.gradle   — Maven / Gradle (multi-module)
    package.json                          — Node (lock files resolve ranges)
    requirements*.txt, pyproject.toml,
    Pipfile, Pipfile.lock, .python-version, runtime.txt — Python
    go.mod                                — Go
    *.csproj, *.fsproj, *.vbproj,
    Directory.Packages.props, global.json — .NET
    Dockerfile, Dockerfile.*, *.Dockerfile — container images
    .gitlab-ci.yml/.yaml, .gitlab/*.yml   — GitLab CI images

Usage:
    python generate_config.py <folder> [--name PROJECT] [--output FILE]
                              [--exclude PATTERN] [--update | --replace]
                              [--include-transitive] [--strict]

Examples:
    python generate_config.py "project-b" --name b
    python generate_config.py ssg-frontend --name frontend --update
"""

import argparse
import json
import os
import sys
import tempfile

from eol_inventory import generate_config, scan_folder


def _merge_identity(entry):
    """Identity excluding version, used for curation-preserving updates."""
    return tuple(entry.get(key) for key in (
        "source", "product", "package", "module", "group", "artifact",
        "sdk", "label" if entry.get("source") == "manual" else "_unused"))


def _merge_existing_config(existing, generated):
    """Merge scan evidence into an existing config without deleting curation."""
    fresh = [p for p in generated.get("products", [])
             if isinstance(p, dict) and not p.get("_section")]
    fresh_by_identity = {_merge_identity(p): p for p in fresh}
    used = set()
    products = []
    stats = {"added": 0, "changed": 0, "unchanged": 0,
             "retained_not_observed": 0}
    for old in existing.get("products", []):
        if not isinstance(old, dict) or old.get("_section"):
            products.append(old)
            continue
        identity = _merge_identity(old)
        new = fresh_by_identity.get(identity)
        if new is None:
            products.append(old)
            stats["retained_not_observed"] += 1
            continue
        used.add(identity)
        merged_entry = dict(old)
        merged_entry.update(new)
        for key in ("policy_note", "note", "reference_url", "eol_date",
                    "latest", "_comment"):
            if key in old:
                merged_entry[key] = old[key]
        products.append(merged_entry)
        if old.get("version") == new.get("version"):
            stats["unchanged"] += 1
        else:
            stats["changed"] += 1

    additions = [p for p in fresh if _merge_identity(p) not in used]
    if additions:
        products.append({"_section": "=== Newly Discovered ==="})
        products.extend(additions)
        stats["added"] = len(additions)

    merged = dict(generated)
    for key, value in existing.items():
        if key not in ("products", "_inventory"):
            merged[key] = value
    merged["products"] = products
    merged["_inventory"]["update_summary"] = stats
    return merged


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
    replacement = parser.add_mutually_exclusive_group()
    replacement.add_argument("--update", action="store_true",
                             help="Merge scan results into an existing config, preserving curation")
    replacement.add_argument("--replace", action="store_true",
                             help="Replace an existing config wholesale")
    parser.add_argument("--include-transitive", action="store_true",
                        help="Include indirect/lockfile dependencies (direct only by default)")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero when any scan warning is emitted (for CI)")
    args = parser.parse_args(argv)

    folder = args.folder
    project_name = args.name or os.path.basename(os.path.normpath(folder)).replace(" ", "-").lower()
    output = args.output or f"eol_config.{project_name}.json"

    if os.path.exists(output) and not (args.replace or args.update):
        print(f"Refusing to overwrite existing file: {output}", file=sys.stderr)
        print("Re-run with --update to preserve curation, or --replace to replace it.",
              file=sys.stderr)
        return 2

    print(f"Scanning {folder!r}...")
    scan = scan_folder(folder, exclude=args.exclude)

    print(f"  Files scanned        : {len(scan['files'])}")
    print(f"  Dependency records   : {len(scan['records'])}")
    print(f"  Scan warnings        : {len(scan['warnings'])}")

    config = generate_config(scan, project_name,
                             include_transitive=args.include_transitive)
    if args.update and os.path.exists(output):
        try:
            with open(output, encoding="utf-8") as existing_file:
                existing = json.load(existing_file)
            config = _merge_existing_config(existing, config)
        except (OSError, ValueError) as exc:
            print(f"Could not update existing config: {exc}", file=sys.stderr)
            return 2
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
