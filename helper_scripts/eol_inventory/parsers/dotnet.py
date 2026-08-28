"""MSBuild .NET project parsers (normalized records).

Pure stdlib; never runs dotnet/msbuild and never touches the network.

Covers:
    - *.csproj / *.fsproj / *.vbproj PackageReference elements (Include
      and Update forms, Version attribute or <Version> child element);
    - Directory.Packages.props central package versions (PackageVersion);
    - packages.lock.json as a fallback version source for project
      references that carry no explicit version;
    - TargetFramework / TargetFrameworks as "dotnet" runtime records;
    - global.json SDK pins as "dotnet-sdk" runtime records.

MSBuild is case-insensitive, so all name/property/attribute lookups are
case-insensitive and deterministic: file order wins, and the record's
name keeps the casing of the file that declared it.

Unresolved $(Property) expressions never become versions: the record is
emitted without a version (spec kept in version_spec) and a warning is
raised — mirrors the Maven/Gradle parsers.

Central versions and lock files are resolved from the SAME directory as
the project file (the parser sees one file plus its siblings; scanning
parent directories is discovery's business).
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..models import add_location, new_record, new_warning

_PROP_RE = re.compile(r"\$\(([^)]+)\)")

_PROJECT_SIBLING = "Directory.Packages.props"
_LOCK_SIBLING = "packages.lock.json"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _local(tag):
    """Tag without the XML namespace: '{ns}Name' -> 'Name'."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attrs_ci(elem):
    """Attributes keyed case-insensitively (MSBuild semantics)."""
    return {k.lower(): (v or "").strip() for k, v in elem.attrib.items()}


def _child_version(elem):
    """Text of a <Version> child element, case-insensitive, or None."""
    for child in elem:
        if _local(child.tag).lower() == "version":
            return (child.text or "").strip()
    return None


def _resolve_props(value, props):
    """One-pass $(Name) substitution, case-insensitive; unresolved stay."""
    if not value:
        return value
    return _PROP_RE.sub(
        lambda m: props.get(m.group(1).strip().lower(), m.group(0)), value)


def _sibling_rel(rel_path, filename):
    """Scan-root-relative path of a file next to rel_path."""
    dirpart = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    return f"{dirpart}/{filename}" if dirpart else filename


def _load_xml(path, rel_path, what):
    """ET root or (None, warning) — callers must handle the None."""
    try:
        return ET.parse(path).getroot(), None
    except ET.ParseError as exc:
        return None, new_warning(
            "parse_error", rel_path, f"{what} parse error: {exc}")
    except OSError as exc:
        return None, new_warning(
            "unreadable_file", rel_path, f"could not read {what}: {exc}")


def _collect_properties(root):
    """All PropertyGroup children as {lower-name: text}; last one wins."""
    props = {}
    for elem in root.iter():
        if _local(elem.tag) != "PropertyGroup":
            continue
        for child in elem:
            name = _local(child.tag).lower()
            if name:
                props[name] = (child.text or "").strip()
    return props


def _tfm_version(tfm):
    """'net8.0-windows' -> '8.0'; 'net48' -> '4.8'; None when empty."""
    base = tfm.split("-", 1)[0]
    for prefix in ("netcoreapp", "netstandard", "net"):
        if base.startswith(prefix):
            rest = base[len(prefix):]
            break
    else:
        rest = base
    if not rest:
        return None
    if "." not in rest and rest.isdigit() and len(rest) == 2:
        return rest[0] + "." + rest[1]
    return rest


# ---------------------------------------------------------------------------
# Version sources: Directory.Packages.props and packages.lock.json
# ---------------------------------------------------------------------------

def _read_central_versions(props_abs):
    """{lower-name: (declared-name, version)} from Directory.Packages.props.

    First declaration wins when casing differs (deterministic file order).
    """
    if not props_abs.is_file():
        return {}
    try:
        root = ET.parse(props_abs).getroot()
    except (ET.ParseError, OSError):
        return {}
    central = {}
    for elem in root.iter():
        if _local(elem.tag) != "PackageVersion":
            continue
        attrs = _attrs_ci(elem)
        name = attrs.get("include") or attrs.get("update")
        if not name:
            continue
        version = attrs.get("version") or _child_version(elem)
        key = name.lower()
        if version and key not in central:
            central[key] = (name, version)
    return central


def _requested_lower_bound(spec):
    """'[2.0.1, )' -> '2.0.1' (NuGet range lower bound)."""
    spec = (spec or "").strip().lstrip("[(")
    token = spec.split(",", 1)[0].split("]", 1)[0].strip()
    return token or None


def _read_lock_versions(lock_abs):
    """{lower-name: version} from packages.lock.json.

    Direct entries beat Transitive ones; within a pass, target
    frameworks are visited in sorted order and the first occurrence
    wins — deterministic regardless of JSON ordering.
    """
    if not lock_abs.is_file():
        return {}
    try:
        with open(lock_abs, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    dependencies = data.get("dependencies") or {}
    tfms = sorted(dependencies)
    lock = {}
    for wanted in ("Direct", "Transitive"):
        for tfm in tfms:
            packages = dependencies.get(tfm) or {}
            for pkg in sorted(packages):
                key = pkg.lower()
                if key in lock:
                    continue
                info = packages.get(pkg) or {}
                if info.get("type") != wanted:
                    continue
                version = info.get("resolved") or _requested_lower_bound(
                    info.get("requested"))
                if version:
                    lock[key] = version
    return lock


# ---------------------------------------------------------------------------
# Project file parser (csproj / fsproj / vbproj)
# ---------------------------------------------------------------------------

def parse_csproj_records(path, rel_path):
    """Parse a .csproj/.fsproj/.vbproj; return (records, warnings)."""
    root, warning = _load_xml(path, rel_path, "project file")
    if root is None:
        return [], [warning]

    records = []
    warnings = []
    props = _collect_properties(root)
    project_dir = Path(path).parent
    central = _read_central_versions(project_dir / _PROJECT_SIBLING)
    central_rel = _sibling_rel(rel_path, _PROJECT_SIBLING)
    lock = _read_lock_versions(project_dir / _LOCK_SIBLING)
    lock_rel = _sibling_rel(rel_path, _LOCK_SIBLING)

    for elem in root.iter():
        tag = _local(elem.tag)

        if tag in ("TargetFramework", "TargetFrameworks"):
            text = (elem.text or "").strip()
            if "$(" in text:
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"unresolved property expression in <{tag}> ({text})"))
                continue
            for tfm in (t.strip() for t in text.split(";")):
                if not tfm:
                    continue
                version = _tfm_version(tfm)
                if not version:
                    continue
                record = new_record("dotnet", "dotnet", version=version,
                                    kind="runtime")
                add_location(record, rel_path, "dotnet", locator=tag)
                records.append(record)
            continue

        if tag != "PackageReference":
            continue

        attrs = _attrs_ci(elem)
        name = attrs.get("include") or attrs.get("update")
        if not name:
            warnings.append(new_warning(
                "parse_error", rel_path,
                "PackageReference without Include/Update"))
            continue

        raw_version = attrs.get("version") or _child_version(elem)
        version = _resolve_props(raw_version, props) if raw_version else None
        if version and "$(" in version:
            version = None  # unresolved property — fall through

        if version:
            record = new_record("dotnet", name, version=version)
            add_location(record, rel_path, "dotnet",
                         locator=f"PackageReference:{name}")
            records.append(record)
            continue

        # No explicit version: central package versions, then lock file.
        hit = central.get(name.lower())
        if hit:
            declared, version = hit
            record = new_record("dotnet", name, version=version)
            add_location(record, rel_path, "dotnet",
                         locator=f"PackageReference:{name}")
            add_location(record, central_rel, "dotnet",
                         locator=f"PackageVersion:{declared}")
            records.append(record)
            continue

        version = lock.get(name.lower())
        if version:
            record = new_record("dotnet", name, version=version)
            add_location(record, rel_path, "dotnet",
                         locator=f"PackageReference:{name}")
            add_location(record, lock_rel, "dotnet", locator=f"lock:{name}")
            records.append(record)
            continue

        record = new_record("dotnet", name, version=None,
                            version_spec=raw_version or None)
        add_location(record, rel_path, "dotnet",
                     locator=f"PackageReference:{name}")
        records.append(record)
        warnings.append(new_warning(
            "unresolved_version", rel_path,
            f"no version for {name}"
            + (f" ({raw_version})" if raw_version else "")
            + "; not in Directory.Packages.props or packages.lock.json"))

    return records, warnings


# ---------------------------------------------------------------------------
# Directory.Packages.props and global.json
# ---------------------------------------------------------------------------

def parse_directory_packages_props(path, rel_path):
    """Parse central package versions; return (records, warnings)."""
    root, warning = _load_xml(path, rel_path, "Directory.Packages.props")
    if root is None:
        return [], [warning]

    records = []
    warnings = []
    seen = set()
    for elem in root.iter():
        if _local(elem.tag) != "PackageVersion":
            continue
        attrs = _attrs_ci(elem)
        name = attrs.get("include") or attrs.get("update")
        if not name or name.lower() in seen:
            # MSBuild is case-insensitive: the first declaration wins.
            continue
        seen.add(name.lower())
        version = attrs.get("version") or _child_version(elem)
        if version:
            record = new_record("dotnet", name, version=version)
        else:
            record = new_record("dotnet", name, version=None)
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"PackageVersion {name} has no Version"))
        add_location(record, rel_path, "dotnet",
                     locator=f"PackageVersion:{name}")
        records.append(record)
    return records, warnings


def parse_global_json_records(path, rel_path):
    """Parse global.json; return (records, warnings)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return [], [new_warning(
            "parse_error", rel_path, f"global.json parse error: {exc}")]

    version = (data.get("sdk") or {}).get("version")
    if not version:
        return [], []
    record = new_record("dotnet", "dotnet-sdk", version=version,
                        kind="runtime")
    add_location(record, rel_path, "dotnet", locator="sdk.version")
    return [record], []
