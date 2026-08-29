"""package.json parser (normalized records).

Exact version specifications (``1.2.3``, ``v1.2.3``) become the record
version directly. Every other specification -- semver ranges (``^``/``~``),
``*``/``latest``, comparison ranges, workspace, Git/URL, and local-path
references -- resolves ONLY through sibling npm lock evidence
(npm-shrinkwrap.json preferred over package-lock.json, npm semantics);
without lock evidence the specification is kept in ``version_spec`` and a
structured warning is raised. Versions are never guessed from ranges.

The lock file is a SIBLING of the parsed package.json (same directory);
discovery never lists it -- like Directory.Packages.props for .NET, the
parser sees one manifest plus its siblings.
"""

import json
import re
from pathlib import Path

from ..mappings import _clean_version
from ..models import add_location, new_record, new_warning

_EXACT_VERSION_RE = re.compile(
    r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?(?:\+[0-9A-Za-z.\-]+)?$")

_SHRINKWRAP_SIBLING = "npm-shrinkwrap.json"
_LOCK_SIBLING = "package-lock.json"


def _sibling_rel(rel_path, filename):
    """Scan-root-relative path of a file next to rel_path."""
    dirpart = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    return f"{dirpart}/{filename}" if dirpart else filename


def _read_lock(directory, rel_path):
    """(data, rel_lock_path, warning) for package.json's sibling locks.

    npm semantics: npm-shrinkwrap.json wins over package-lock.json; a
    chosen file that is unreadable or malformed yields a parse_error
    warning naming it (no fallback to the other lock). Neither file
    existing is normal: (None, None, None), no warning.
    """
    for filename in (_SHRINKWRAP_SIBLING, _LOCK_SIBLING):
        candidate = Path(directory) / filename
        if not candidate.is_file():
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            return None, None, new_warning(
                "parse_error", rel_path, f"{filename} parse error: {exc}")
        return data, _sibling_rel(rel_path, filename), None
    return None, None, None


def _lock_version(info):
    """The "version" of one lock entry when it is a non-empty str."""
    if isinstance(info, dict):
        version = info.get("version")
        if isinstance(version, str) and version:
            return version
    return None


def _lock_lookup(data, name):
    """Locked version for name, or None.

    Tries the lockfileVersion 2/3 "packages" map first, then the legacy
    v1 "dependencies" tree (lockfileVersion 2 carries both). Scoped
    names ("@scope/pkg") are plain dict keys in both forms.
    """
    if not isinstance(data, dict):
        return None
    packages = data.get("packages")
    if isinstance(packages, dict):
        version = _lock_version(packages.get("node_modules/" + name))
        if version:
            return version
    dependencies = data.get("dependencies")
    if isinstance(dependencies, dict):
        version = _lock_version(dependencies.get(name))
        if version:
            return version
    return None


def _spec_warning(name, spec, rel_path):
    """Warning for a specification that lock evidence cannot resolve."""
    if spec.startswith("workspace:"):
        return new_warning(
            "workspace_dependency", rel_path,
            f"workspace reference {name}: {spec} has no lock evidence")
    if "://" in spec or spec.startswith("git+"):
        return new_warning(
            "url_dependency", rel_path,
            f"url reference {name}: {spec} has no lock evidence")
    if spec.startswith(("file:", "link:", "portal:")):
        return new_warning(
            "local_path_dependency", rel_path,
            f"local path reference {name}: {spec} has no lock evidence")
    return new_warning(
        "unresolved_version", rel_path,
        f"no lock evidence for {name} ({spec}); range preserved, not guessed")


def parse_package_json_records(path, rel_path):
    """Parse package.json; return (records, warnings)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return [], [new_warning(
            "parse_error", rel_path, f"package.json parse error: {exc}")]

    lock, lock_rel, lock_warning = _read_lock(Path(path).parent, rel_path)
    warnings = [lock_warning] if lock_warning else []
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
        for name, value in (data.get(section) or {}).items():
            spec = str(value).strip()
            exact = _EXACT_VERSION_RE.fullmatch(spec)
            locked = None if exact else _lock_lookup(lock, name)
            if exact:
                record = new_record("node", name, version=spec, scope=scope)
            elif locked:
                record = new_record("node", name, version=locked, scope=scope)
            else:
                record = new_record("node", name, version=None,
                                    version_spec=spec, scope=scope)
                warnings.append(_spec_warning(name, spec, rel_path))
            add_location(record, rel_path, "npm", locator=f"{section}.{name}")
            if locked:
                add_location(record, lock_rel, "npm", locator=f"lock:{name}")
            records.append(record)
    return records, warnings
