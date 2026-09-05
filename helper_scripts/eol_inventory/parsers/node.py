"""package.json parser (normalized records).

Exact version specifications (``1.2.3``, ``v1.2.3``) become the record
version directly. Every other specification -- semver ranges (``^``/``~``),
``*``/``latest``, comparison ranges, workspace, Git/URL, and local-path
references -- resolves ONLY through sibling npm lock evidence
(npm-shrinkwrap.json preferred over package-lock.json, npm semantics);
without lock evidence the specification is kept in ``version_spec`` and a
structured warning is raised. Package versions are never guessed from
ranges.

``engines.node`` is the one exception: a Node.js engine constraint names
the runtime's release line rather than a resolvable package, and its
lifecycle cycle is the leading major of the range. The leading concrete
version of the range is therefore recorded (``">=18 <21"`` -> ``18``,
``"^20.0.0"`` -> ``20.0.0``) while the original range stays in
``version_spec``; a range with no numeric leading segment (``"*"``,
``"latest"``) still yields no version and a structured warning.

The lock file is a SIBLING of the parsed package.json (same directory);
discovery never walks it as a candidate, but a lock the scan actually
read is reported as a consumed manifest (it lands in `_inventory.manifests`
via discovery's consumed-sidecar merge) even when it resolves nothing.
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
from ..redact import hosted_git_placeholder, ssh_placeholder

_EXACT_VERSION_RE = re.compile(
    r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?(?:\+[0-9A-Za-z.\-]+)?$")
_CONCRETE_NODE_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+){0,2}$")
_NODE_RANGE_LEAD_RE = re.compile(r"^[\^~>=<\s]*v?(\d+(?:\.\d+)*)")

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
    """The exact registry version of one lock entry, or None."""
    if isinstance(info, dict):
        version = info.get("version")
        if isinstance(version, str) and _EXACT_VERSION_RE.fullmatch(version):
            return version[1:] if version[:1].lower() == "v" else version
    return None


def _safe_spec(value):
    """Dependency spec safe to retain in generated inventory metadata."""
    spec = value.strip()
    if spec.startswith("workspace:") and not any(
            marker in spec for marker in ("://", "@", "?", "#")):
        return spec
    if re.fullmatch(
            r"npm:(?:@[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+|"
            r"[A-Za-z0-9._~-]+)(?:@[A-Za-z0-9.*+^~<>=| -]+)?", spec):
        return spec
    placeholder, _ = hosted_git_placeholder(spec)
    if placeholder is not None:
        return placeholder
    if spec.startswith(("git+ssh://", "ssh://")):
        return ssh_placeholder(spec)
    if spec.startswith("git+"):
        return "git+<redacted>"
    if "://" in spec:
        return "url:<redacted>"
    if "@" in spec:
        return "vcs:<redacted>"
    if "?" in spec or "#" in spec:
        return "spec:<redacted>"
    return spec


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
    if spec.startswith("<hosted-git:"):
        return new_warning(
            "url_dependency", rel_path,
            f"hosted-git shorthand {name}: {spec} has no lock evidence")
    if spec.startswith("<ssh:"):
        return new_warning(
            "url_dependency", rel_path,
            f"ssh reference {name}: {spec} has no lock evidence")
    if spec.startswith("workspace:"):
        return new_warning(
            "workspace_dependency", rel_path,
            f"workspace reference {name}: {spec} has no lock evidence")
    if spec.startswith(("git+", "url:", "vcs:")):
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


def _node_engine_range_version(spec):
    """Leading concrete version of an engines.node range, or None.

    ``">=18 <21"`` -> ``"18"``, ``"^20.0.0"`` -> ``"20.0.0"``,
    ``"18.x"`` -> ``"18"``, ``"^18 || ^20"`` -> ``"18"``. Specifications
    with no numeric leading segment (``"*"``, ``"latest"``, a URL) return
    None so the runtime stays unresolved rather than guessed.
    """
    match = _NODE_RANGE_LEAD_RE.match(spec or "")
    return match.group(1) if match else None


def parse_package_json_records(path, rel_path, root=None, consumed_locks=None):
    """Parse package.json; return (records, warnings).

    consumed_locks: optional set receiving the scan-root-relative path of
    every sibling lock file successfully read (npm-shrinkwrap.json wins
    over package-lock.json), so discovery can list consumed manifests
    even when the lock resolves zero specifications.
    """
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
    if lock_rel and consumed_locks is not None:
        consumed_locks.add(lock_rel)
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
    if node_engine is None:
        pass
    elif not isinstance(node_engine, str):
        warnings.append(new_warning(
            "parse_error", rel_path,
            "package.json engines.node value is not a string; skipped"))
    else:
        record = None
        spec = _safe_spec(node_engine)
        if not spec:
            warnings.append(new_warning(
                "parse_error", rel_path,
                "package.json engines.node value is empty; skipped"))
        elif _CONCRETE_NODE_VERSION_RE.fullmatch(spec):
            version = spec[1:] if spec[:1].lower() == "v" else spec
            record = new_record(
                "node", "node", version=version, kind="runtime")
        else:
            lead = _node_engine_range_version(spec)
            if lead is not None:
                # The engine constraint's release line is its leading major;
                # the range itself is preserved in version_spec.
                record = new_record(
                    "node", "node", version=lead, version_spec=spec,
                    kind="runtime")
            else:
                record = new_record(
                    "node", "node", version=None, version_spec=spec,
                    kind="runtime")
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"engines.node has no exact version ({spec}); "
                    "specification recorded, not guessed"))
        if record is not None:
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
            if not isinstance(value, str):
                spec = "non-string dependency value"
                record = new_record(
                    "node", name, version=None, version_spec=spec, scope=scope)
                add_location(
                    record, rel_path, "npm", locator=f"{section}.{name}")
                records.append(record)
                warnings.append(new_warning(
                    "parse_error", rel_path,
                    f"package.json {section}.{name} value is not a string; "
                    "contents not retained"))
                continue
            spec = _safe_spec(value)
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
    safe_value = _safe_spec(value)
    version = safe_value[1:] if safe_value[:1].lower() == "v" else safe_value
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
        if not value:
            return [], []
        return [], [new_warning(
            "unresolved_version", rel_path,
            f".nvmrc value {safe_value!r} is not a concrete Node version")]
    record = new_record("node", "node", version=version, kind="runtime")
    add_location(record, rel_path, "npm", locator=".nvmrc")
    return [record], []
