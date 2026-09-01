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

Unresolved MSBuild expressions never become versions: the record is
emitted without a version (spec kept in version_spec) and a warning is
raised — mirrors the Maven/Gradle parsers.

Central versions and lock files are resolved from the SAME directory as
the project file (the parser sees one file plus its siblings; scanning
parent directories is discovery's business).
"""

import json
import re
from pathlib import Path

from ..models import (
    add_location,
    guarded_local_file,
    load_safe_xml,
    new_record,
    new_warning,
    scan_root_for,
)
from ..redact import redact_urls

_PROP_RE = re.compile(r"\$\(([^)]+)\)")
_NUGET_EXACT_VERSION_RE = re.compile(
    r"^\d+(?:\.\d+){1,3}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_SDK_EXACT_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

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


def _child_versions(elem):
    """All <Version> child values, case-insensitive; empty stays empty."""
    versions = []
    for child in elem:
        if _local(child.tag).lower() == "version":
            versions.append((child.text or "").strip())
    return versions


def _child_version(elem):
    """Text of the first <Version> child, or None when absent."""
    versions = _child_versions(elem)
    return versions[0] if versions else None


def _child_version_is_conditional(elem):
    """Whether a <Version> metadata child carries an MSBuild condition."""
    return any(
        _local(child.tag).lower() == "version"
        and bool(_attrs_ci(child).get("condition"))
        for child in elem)


def _resolve_props(value, props):
    """One-pass $(Name) substitution, case-insensitive; unresolved stay."""
    if not value:
        return value
    return _PROP_RE.sub(
        lambda m: props.get(m.group(1).strip().lower(), m.group(0)), value)


def _has_msbuild_expression(value):
    """Whether value still contains property, item, or metadata syntax."""
    return bool(value) and any(
        marker in value for marker in ("$(", "@(", "%("))


def _sibling_rel(rel_path, filename):
    """Scan-root-relative path of a file next to rel_path."""
    dirpart = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    return f"{dirpart}/{filename}" if dirpart else filename


def _nearest_sidecar(project_dir, scan_root, filename):
    """Nearest filename from the project directory up to the scan root."""
    current = Path(project_dir).resolve()
    boundary = Path(scan_root).resolve()
    while True:
        candidate = current / filename
        if candidate.exists():
            try:
                rel = candidate.relative_to(boundary).as_posix()
            except ValueError:
                return candidate, filename
            return candidate, rel
        if current == boundary or boundary not in current.parents:
            return Path(project_dir) / filename, _sibling_rel("", filename)
        current = current.parent


def _load_xml(path, rel_path, what):
    """ET root or (None, warning) — callers must handle the None."""
    return load_safe_xml(path, rel_path, what)


def _collect_properties(root):
    """Unambiguous properties safe to resolve without evaluating MSBuild."""
    declarations = {}

    def visit(elem, conditional_context=False):
        tag = _local(elem.tag).lower()
        conditional = (
            conditional_context
            or bool(_attrs_ci(elem).get("condition"))
            or tag in ("when", "otherwise", "target"))
        if tag == "propertygroup":
            for child in elem:
                name = _local(child.tag).lower()
                if name:
                    child_conditional = conditional or bool(
                        _attrs_ci(child).get("condition"))
                    declarations.setdefault(name, []).append((
                        (child.text or "").strip(), child_conditional))
        for child in elem:
            visit(child, conditional)

    visit(root)
    props = {}
    for name, values in declarations.items():
        if not any(conditional for _, conditional in values):
            props[name] = values[-1][0]
            continue
        unconditional = [value for value, conditional in values
                         if not conditional]
        distinct = {value for value, _ in values}
        if unconditional and len(distinct) == 1:
            props[name] = unconditional[-1]
    return props


def _collect_central_versions(root):
    """Conservatively select PackageVersion declarations.

    MSBuild conditions cannot be evaluated without the build context.  A
    conditional declaration is therefore exact only when an unconditional
    declaration exists and every declaration has the same non-empty value.
    The returned tuple is (display name, exact value, unresolved spec).
    """
    declarations = {}

    def visit(elem, conditional_context=False):
        tag = _local(elem.tag).lower()
        conditional = (
            conditional_context
            or bool(_attrs_ci(elem).get("condition"))
            or tag in ("when", "otherwise", "target"))
        if tag == "packageversion":
            attrs = _attrs_ci(elem)
            name = attrs.get("include") or attrs.get("update")
            if name:
                version = attrs.get("version") or _child_version(elem)
                child_conditional = _child_version_is_conditional(elem)
                declarations.setdefault(name.lower(), []).append(
                    (name, version, conditional or child_conditional))
        for child in elem:
            visit(child, conditional)

    visit(root)
    selected = {}
    for key, values in declarations.items():
        display = values[0][0]
        versions = [version for _, version, _ in values if version]
        has_conditional = any(conditional for _, _, conditional in values)
        unconditional = [
            version for _, version, conditional in values
            if not conditional and version]
        distinct = list(dict.fromkeys(versions))
        all_nonempty = all(bool(version) for _, version, _ in values)
        if not has_conditional:
            selected[key] = (display, versions[0] if versions else None, None)
        elif unconditional and all_nonempty and len(distinct) == 1:
            selected[key] = (display, unconditional[-1], None)
        else:
            details_values = list(distinct)
            if not all_nonempty:
                details_values.append("no version")
            details = " | ".join(details_values) if details_values else (
                "no version")
            selected[key] = (
                display, None,
                f"conditional PackageVersion: {redact_urls(details)}")
    return selected


def _tfm_version(tfm):
    """'net8.0-windows' -> '8.0'; 'net48' -> '4.8'; None when empty."""
    base = tfm.split("-", 1)[0].lower()
    match = re.fullmatch(r"(netcoreapp|netstandard|net)([0-9]+(?:\.[0-9]+){0,2})", base)
    if not match:
        return None
    prefix, rest = match.groups()
    if not rest:
        return None
    if prefix == "net" and "." not in rest:
        if len(rest) == 2:
            return rest[0] + "." + rest[1]
        if len(rest) >= 3:
            return rest[0] + "." + rest[1] + "." + rest[2:]
    return rest


# ---------------------------------------------------------------------------
# Version sources: Directory.Packages.props and packages.lock.json
# ---------------------------------------------------------------------------

def _read_central_versions(props_abs, root, rel_path):
    """Central versions from Directory.Packages.props.

    Values are ``(declared-name, exact-version, unresolved-spec)``.  First
    unconditional declaration wins when casing differs; conditional ambiguity
    remains visible instead of being guessed.
    """
    guarded, warning = guarded_local_file(props_abs, root, rel_path)
    if guarded is None:
        return {}, warning
    document, parse_warning = load_safe_xml(
        guarded, rel_path, "Directory.Packages.props")
    if document is None:
        return {}, parse_warning
    return _collect_central_versions(document), None


def _requested_lower_bound(spec):
    """'[2.0.1, )' -> '2.0.1' (NuGet range lower bound)."""
    spec = (spec or "").strip().lstrip("[(")
    token = spec.split(",", 1)[0].split("]", 1)[0].strip()
    return token or None


def _is_exact_nuget_version(version):
    return bool(version and _NUGET_EXACT_VERSION_RE.fullmatch(version))


def _read_lock_versions(lock_abs, root, rel_path):
    """{lower-name: version} from packages.lock.json.

    Direct entries beat Transitive ones; within a pass, target
    frameworks are visited in sorted order and the first occurrence
    wins — deterministic regardless of JSON ordering.
    """
    guarded, warning = guarded_local_file(lock_abs, root, rel_path)
    if guarded is None:
        return {}, warning
    try:
        with open(guarded, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return {}, new_warning(
            "parse_error", rel_path, f"packages.lock.json parse error: {exc}")
    if not isinstance(data, dict):
        return {}, new_warning(
            "parse_error", rel_path,
            "packages.lock.json top-level value is not an object")
    if "dependencies" not in data:
        return {}, new_warning(
            "parse_error", rel_path,
            "packages.lock.json has no dependencies object")
    dependencies = data["dependencies"]
    if not isinstance(dependencies, dict):
        return {}, new_warning(
            "parse_error", rel_path,
            "packages.lock.json dependencies value is not an object")
    if any(not isinstance(tfm, str) for tfm in dependencies):
        return {}, new_warning(
            "parse_error", rel_path,
            "packages.lock.json dependency group name is not a string")
    tfms = sorted(dependencies)
    lock = {}
    for wanted in ("Direct", "Transitive"):
        for tfm in tfms:
            packages = dependencies[tfm]
            if not isinstance(packages, dict):
                return {}, new_warning(
                    "parse_error", rel_path,
                    f"packages.lock.json dependency group {tfm!r} is not an object")
            if any(not isinstance(pkg, str) for pkg in packages):
                return {}, new_warning(
                    "parse_error", rel_path,
                    f"packages.lock.json dependency group {tfm!r} has a "
                    "non-string package name")
            for pkg in sorted(packages):
                key = pkg.lower()
                if key in lock:
                    continue
                info = packages[pkg]
                if not isinstance(info, dict):
                    return {}, new_warning(
                        "parse_error", rel_path,
                        f"packages.lock.json package {pkg!r} is not an object")
                package_type = info.get("type")
                if not isinstance(package_type, str) or not package_type:
                    return {}, new_warning(
                        "parse_error", rel_path,
                        f"packages.lock.json package {pkg!r} type is not a string")
                version = info.get("resolved")
                if not isinstance(version, str) or not version:
                    return {}, new_warning(
                        "parse_error", rel_path,
                        f"packages.lock.json package {pkg!r} resolved version "
                        "is not a non-empty string")
                if not _is_exact_nuget_version(version):
                    return {}, new_warning(
                        "parse_error", rel_path,
                        f"packages.lock.json package {pkg!r} resolved version "
                        f"{redact_urls(version)!r} is not exact")
                if package_type != wanted:
                    continue
                lock[key] = version
    return lock, None


# ---------------------------------------------------------------------------
# Project file parser (csproj / fsproj / vbproj)
# ---------------------------------------------------------------------------

def parse_csproj_records(path, rel_path, root=None):
    """Parse a .csproj/.fsproj/.vbproj; return (records, warnings)."""
    document, warning = _load_xml(path, rel_path, "project file")
    if document is None:
        return [], [warning]

    records = []
    warnings = []
    props = _collect_properties(document)
    project_dir = Path(path).parent
    scan_root = Path(root).resolve() if root is not None else scan_root_for(
        path, rel_path)
    central_path, central_rel = _nearest_sidecar(
        project_dir, scan_root, _PROJECT_SIBLING)
    lock_path, lock_rel = _nearest_sidecar(
        project_dir, scan_root, _LOCK_SIBLING)
    central, central_warning = _read_central_versions(
        central_path, scan_root, central_rel)
    lock, lock_warning = _read_lock_versions(
        lock_path, scan_root, lock_rel)
    warnings.extend(w for w in (central_warning, lock_warning) if w)

    for elem in document.iter():
        tag = _local(elem.tag)
        tag_ci = tag.lower()

        if tag_ci in ("targetframework", "targetframeworks"):
            text = (elem.text or "").strip()
            if _has_msbuild_expression(text):
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"unresolved property expression in <{tag}> "
                    f"({redact_urls(text)})"))
                continue
            for tfm in (t.strip() for t in text.split(";")):
                if not tfm:
                    continue
                version = _tfm_version(tfm)
                if not version:
                    warnings.append(new_warning(
                        "unresolved_version", rel_path,
                        f"unrecognized target framework {redact_urls(tfm)!r}"))
                    continue
                record = new_record("dotnet", "dotnet", version=version,
                                    kind="runtime")
                add_location(record, rel_path, "dotnet", locator=tag)
                records.append(record)
            continue

        if tag_ci != "packagereference":
            continue

        attrs = _attrs_ci(elem)
        name = attrs.get("include") or attrs.get("update")
        if not name:
            warnings.append(new_warning(
                "parse_error", rel_path,
                "PackageReference without Include/Update"))
            continue

        attribute_version = attrs.get("version")
        child_version = _child_version(elem)
        raw_version = redact_urls(attribute_version or child_version)
        version = redact_urls(
            _resolve_props(raw_version, props)) if raw_version else None
        if _child_version_is_conditional(elem):
            version_spec = version
            child_versions = _child_versions(elem)
            if attribute_version or len(child_versions) > 1:
                raw_candidates = (
                    ([attribute_version] if attribute_version else [])
                    + child_versions)
                candidates = [
                    redact_urls(
                        (_resolve_props(candidate, props) if candidate
                         else "no version") or "no version")
                    for candidate in raw_candidates]
                details = " | ".join(dict.fromkeys(candidates))
                version_spec = (
                    f"conditional PackageReference Version: {details}")
            locked = lock.get(name.lower())
            record = new_record(
                "dotnet", name, version=locked, version_spec=version_spec)
            add_location(record, rel_path, "dotnet",
                         locator=f"PackageReference:{name}")
            if locked:
                add_location(record, lock_rel, "dotnet",
                             locator=f"lock:{name}")
            else:
                detail = (f" ({version_spec})" if version_spec
                          else " is empty")
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"PackageReference {name} has conditional Version "
                    f"metadata{detail}; not guessed"))
            records.append(record)
            continue
        if _has_msbuild_expression(version):
            locked = lock.get(name.lower())
            record = new_record(
                "dotnet", name, version=locked, version_spec=version)
            add_location(record, rel_path, "dotnet",
                         locator=f"PackageReference:{name}")
            if locked:
                add_location(record, lock_rel, "dotnet",
                             locator=f"lock:{name}")
            else:
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"unresolved MSBuild expression for {name} ({version})"))
            records.append(record)
            continue

        if version and _is_exact_nuget_version(version):
            record = new_record("dotnet", name, version=version)
            add_location(record, rel_path, "dotnet",
                         locator=f"PackageReference:{name}")
            records.append(record)
            continue
        if version:
            locked = lock.get(name.lower())
            record = new_record(
                "dotnet", name, version=locked, version_spec=version)
            add_location(record, rel_path, "dotnet",
                         locator=f"PackageReference:{name}")
            if locked:
                add_location(record, lock_rel, "dotnet",
                             locator=f"lock:{name}")
            else:
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"PackageReference {name} has no exact version "
                    f"({version}) and no lock resolution"))
            records.append(record)
            continue

        # No explicit version: central package versions, then lock file.
        hit = central.get(name.lower())
        if hit:
            declared, version, conditional_spec = hit
            version_spec = redact_urls(conditional_spec or version)
            unresolved = (conditional_spec is not None
                          or not _is_exact_nuget_version(version)
                          or _has_msbuild_expression(version))
            locked = lock.get(name.lower()) if unresolved else None
            record = new_record(
                "dotnet", name,
                version=locked if unresolved else version,
                version_spec=version_spec if unresolved else None)
            add_location(record, rel_path, "dotnet",
                         locator=f"PackageReference:{name}")
            add_location(record, central_rel, "dotnet",
                         locator=f"PackageVersion:{declared}")
            if locked:
                add_location(record, lock_rel, "dotnet",
                             locator=f"lock:{name}")
            records.append(record)
            if unresolved and not locked:
                warnings.append(new_warning(
                    "unresolved_version", central_rel,
                    f"PackageVersion {declared} has no exact version "
                    f"({version_spec}) and no lock resolution"))
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
    for name, version, conditional_spec in _collect_central_versions(
            root).values():
        if (conditional_spec is None and version
                and _is_exact_nuget_version(version)
                and not _has_msbuild_expression(version)):
            record = new_record("dotnet", name, version=version)
        else:
            version_spec = redact_urls(conditional_spec or version)
            record = new_record(
                "dotnet", name, version=None,
                version_spec=version_spec if version_spec else None)
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"PackageVersion {name} "
                + (f"has no exact version ({version_spec})"
                   if version_spec else "has no Version")))
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
    if not isinstance(data, dict):
        return [], [new_warning(
            "parse_error", rel_path,
            "global.json top-level value is not an object")]
    sdk = data.get("sdk")
    if sdk is None:
        sdk = {}
    if not isinstance(sdk, dict):
        return [], [new_warning(
            "parse_error", rel_path,
            "global.json sdk value is not an object")]
    if "version" not in sdk or sdk["version"] is None:
        return [], []
    version = sdk["version"]
    if not isinstance(version, str):
        return [], [new_warning(
            "parse_error", rel_path,
            "global.json sdk.version value is not a string")]
    version = version.strip()
    if not version:
        return [], []
    version = redact_urls(version)
    exact = bool(_SDK_EXACT_VERSION_RE.fullmatch(version))
    record = new_record(
        "dotnet", "dotnet-sdk", version=version if exact else None,
        version_spec=None if exact else version, kind="runtime")
    add_location(record, rel_path, "dotnet", locator="sdk.version")
    if exact:
        return [record], []
    return [record], [new_warning(
        "unresolved_version", rel_path,
        f"global.json sdk.version is not exact ({version})")]
