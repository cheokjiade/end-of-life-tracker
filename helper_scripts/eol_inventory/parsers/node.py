"""package.json parser (normalized records).

Version ranges are still cleaned the same way as the original generator
(`^1.2.3` -> `1.2.3`); resolving ranges through npm lock data is a later,
separate change. Lock evidence therefore does not exist yet — this parser
keeps the cleaned form and never fabricates a version.
"""

import json
import sys

from ..mappings import _clean_version
from ..models import add_location, new_record, new_warning


def parse_package_json_records(path, rel_path):
    """Parse package.json; return (records, warnings)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return [], [new_warning(
            "parse_error", rel_path, f"package.json parse error: {exc}")]

    records = []

    engines = data.get("engines") or {}
    if engines.get("node"):
        record = new_record(
            "node", "node", version=_clean_version(engines["node"]),
            kind="runtime",
        )
        add_location(record, rel_path, "npm", locator="engines.node")
        records.append(record)

    for section, scope in (("dependencies", "runtime"),
                           ("devDependencies", "dev")):
        for name, version in (data.get(section) or {}).items():
            record = new_record(
                "node", name, version=_clean_version(version), scope=scope,
            )
            add_location(record, rel_path, "npm", locator=f"{section}.{name}")
            records.append(record)
    return records, []
