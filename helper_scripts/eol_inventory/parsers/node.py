"""package.json parser.

Moved verbatim from the original root generate_config.py.
"""

import json
import sys

from ..mappings import _clean_version


def parse_package_json(path):
    """Parse package.json; return (npm_deps, source_path).

    npm_deps: list of (name, version)
    Engine constraint engines.node, when present, is yielded as ("node", version).
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  ! parse error in {path}: {exc}", file=sys.stderr)
        return []

    deps = []
    engines = data.get("engines") or {}
    if "node" in engines:
        deps.append(("node", _clean_version(engines["node"])))
    for key in ("dependencies", "devDependencies"):
        for name, version in (data.get(key) or {}).items():
            deps.append((name, _clean_version(version)))
    return deps
