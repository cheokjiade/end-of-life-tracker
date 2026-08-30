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

from ..models import (
    add_location,
    guarded_local_file,
    new_record,
    new_warning,
    scan_root_for,
)

_EXACT_VERSION_RE = re.compile(
    r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?(?:\+[0-9A-Za-z.\-]+)?$")
_CONCRETE_NODE_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+){0,2}$")

_SHRINKWRAP_SIBLING = "npm-shrinkwrap.json"
_LOCK_SIBLING = "package-lock.json"


def _sibling_rel(rel_path, filename):
    """Scan-root-relative path of a file next to rel_path."""
    dirpart = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    return f"{dirpart}/{filename}" if dirpart else filename


def _read_lock(directory, rel_path, root):
    """(data, rel_lock_path, warning) for package.json's sibling locks.

    npm semantics: npm-shrinkwrap.json wins over package-lock.json; a
    chosen file that is unreadable or malformed yields a parse_error
    warning naming it (no fallback to the other lock). Neither file
    existing is normal: (None, None, None), no warning.
    """
    for filename in (_SHRINKWRAP_SIBLING, _LOCK_SIBLING):
        candidate = Path(directory) / filename
        guarded, warning = guarded_local_file(
            candidate, root, _sibling_rel(rel_path, filename))
        if warning:
            return None, None, warning
        if guarded is None:
            continue
        try:
            with open(guarded, encoding="utf-8") as f:
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


def parse_package_json_records(path, rel_path, root=None):
    """Parse package.json; return (records, warnings)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return [], [new_warning(
            "parse_error", rel_path, f"package.json parse error: {exc}")]
    if not isinstance(data, dict):
        return [], [new_warning(
            "parse_error", rel_path,
            "package.json top-level value is not an object")]

    root_abs = Path(root).resolve() if root is not None else scan_root_for(
        path, rel_path)
    lock, lock_rel, lock_warning = _read_lock(
        Path(path).parent, rel_path, root_abs)
    warnings = [lock_warning] if lock_warning else []
    records = []

    engines = data.get("engines")
    if engines is None:
        engines = {}
    elif not isinstance(engines, dict):
        warnings.append(new_warning(
            "parse_error", rel_path,
            "package.json engines value is not an object; skipped"))
        engines = {}
    node_engine = engines.get("node")
    if node_engine is not None and not isinstance(node_engine, str):
        warnings.append(new_warning(
            "parse_error", rel_path,
            "package.json engines.node value is not a string; skipped"))
    elif node_engine:
        spec = node_engine.strip()
        if not spec:
            warnings.append(new_warning(
                "parse_error", rel_path,
                "package.json engines.node value is empty; skipped"))
        elif _CONCRETE_NODE_VERSION_RE.fullmatch(spec):
            version = spec[1:] if spec[:1].lower() == "v" else spec
            record = new_record(
                "node", "node", version=version, kind="runtime")
        else:
            record = new_record(
                "node", "node", version=None, version_spec=spec,
                kind="runtime")
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"engines.node has no exact version ({spec}); "
                "range preserved, not guessed"))
        if spec:
            add_location(record, rel_path, "npm", locator="engines.node")
            records.append(record)

    for section, scope in (("dependencies", "runtime"),
                           ("optionalDependencies", "optional"),
                           ("peerDependencies", "peer"),
                           ("devDependencies", "dev")):
        dependencies = data.get(section)
        if dependencies is None:
            dependencies = {}
        elif not isinstance(dependencies, dict):
            warnings.append(new_warning(
                "parse_error", rel_path,
                f"package.json {section} value is not an object; skipped"))
            continue
        for name, value in dependencies.items():
            spec = str(value).strip()
            exact = _EXACT_VERSION_RE.fullmatch(spec)
            locked = None if exact else _lock_lookup(lock, name)
            if exact:
                version = spec[1:] if spec[:1].lower() == "v" else spec
                record = new_record("node", name, version=version, scope=scope)
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


def parse_nvmrc_records(path, rel_path):
    """Parse a concrete Node version from .nvmrc without executing nvm."""
    try:
        lines = Path(path).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path, f"could not read .nvmrc: {exc}")]
    value = next((line.strip() for line in lines if line.strip()), "")
    version = value[1:] if value[:1].lower() == "v" else value
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
        if not value:
            return [], []
        return [], [new_warning(
            "unresolved_version", rel_path,
            f".nvmrc value {value!r} is not a concrete Node version")]
    record = new_record("node", "node", version=version, kind="runtime")
    add_location(record, rel_path, "npm", locator=".nvmrc")
    return [record], []
