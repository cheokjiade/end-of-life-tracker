"""Folder walking and manifest discovery.

Moved verbatim from the original root generate_config.py.
"""

from pathlib import Path

from .parsers import parse_gradle, parse_package_json, parse_pom


def scan_folder(folder):
    """Walk folder; return parsed-results dict keyed by language."""
    folder = Path(folder)
    if not folder.is_dir():
        raise SystemExit(f"Not a directory: {folder}")

    java_deps = []          # list of (group, artifact, version, source_file, kind)
    pom_properties = []     # list of (props_dict, source_file)
    node_deps = []          # list of (name, version, source_file)
    files_seen = []

    for p in sorted(folder.rglob("pom*.xml")):
        files_seen.append(str(p))
        deps, props = parse_pom(p)
        for g, a, v, kind in deps:
            java_deps.append((g, a, v, str(p), kind))
        if props:
            pom_properties.append((props, str(p)))

    for pattern in ("*.gradle.kts", "build.gradle"):
        for p in sorted(folder.rglob(pattern)):
            files_seen.append(str(p))
            for g, a, v, kind in parse_gradle(p):
                java_deps.append((g, a, v, str(p), kind))

    for p in sorted(folder.rglob("package.json")):
        if "node_modules" in p.parts:
            continue
        files_seen.append(str(p))
        for name, v in parse_package_json(p):
            node_deps.append((name, v, str(p)))

    return {
        "java":           java_deps,
        "pom_properties": pom_properties,
        "node":           node_deps,
        "files":          files_seen,
    }
