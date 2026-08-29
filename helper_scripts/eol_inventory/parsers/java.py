"""Maven POM and Gradle build-file parsers (normalized records).

Both parsers return (records, warnings):

    records   list of normalized dependency records (see eol_inventory.models)
    warnings  list of structured warnings for unreadable input, unsupported
              syntax, and unresolved property/gradle expressions

Behavior notes preserved from the original generator:
    - test/provided/system-scoped Maven dependencies are skipped entirely;
    - versionless dependencies (managed by a parent/BOM) are skipped
      silently — the parent or BOM entry already carries the platform;
    - Gradle declarations are only matched when double-quoted, and the
      named form only with `group = "..."` syntax.
"""

import re
import sys
from pathlib import Path

from ..mappings import _POM_PROPERTY_MAPPINGS
from ..models import add_location, load_safe_xml, new_record, new_warning

_POM_NS = "{http://maven.apache.org/POM/4.0.0}"


def _t(elem, name, ns=_POM_NS):
    """Read text of <ns:name> child, or None."""
    if elem is None:
        return None
    n = elem.find(f"{ns}{name}")
    if n is None:
        n = elem.find(name)  # fallback for POMs without namespace
    return n.text.strip() if n is not None and n.text else None


def parse_pom_records(path, rel_path):
    """Parse pom.xml; return (records, warnings)."""
    root, warning = load_safe_xml(path, rel_path, "POM")
    if root is None:
        return [], [warning]
    # Detect namespace by inspecting root tag
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    def t(elem, name):
        return _t(elem, name, ns=ns)

    # Properties (for resolving ${name} refs)
    props = {}
    props_node = root.find(f"{ns}properties")
    if props_node is not None:
        for child in props_node:
            tag = child.tag.replace(ns, "")
            if child.text:
                props[tag] = child.text.strip()

    project_version = t(root, "version")
    if project_version:
        props["project.version"] = project_version

    def resolve(s):
        if not s:
            return s
        # One pass; ${prop} substitution. Doesn't recurse but covers normal usage.
        return re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), s)

    records = []
    warnings = []

    def emit(group, artifact, version, kind, locator):
        # ${...} that survived resolution is retained as a warning + a
        # record without a version; it is never emitted as a product.
        if "${" in version:
            record = new_record(
                "java", f"{group}:{artifact}", version=None,
                kind=kind, group=group, artifact=artifact,
                version_spec=version,
            )
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"unresolved property expression in {group}:{artifact} version"
                f" ({version})"))
        else:
            record = new_record(
                "java", f"{group}:{artifact}", version=version,
                kind=kind, group=group, artifact=artifact,
            )
        add_location(record, rel_path, "maven", locator=locator)
        records.append(record)

    parent = root.find(f"{ns}parent")
    if parent is not None:
        pg, pa, pv = t(parent, "groupId"), t(parent, "artifactId"), t(parent, "version")
        if pg and pa and pv:
            emit(pg, pa, resolve(pv), "parent", "parent")

    # Walk all <dependencies> blocks (both top-level and inside <dependencyManagement>)
    for deps_node in root.iter(f"{ns}dependencies"):
        for dep in deps_node.findall(f"{ns}dependency"):
            g, a, v = t(dep, "groupId"), t(dep, "artifactId"), t(dep, "version")
            scope = t(dep, "scope") or "compile"
            if scope in ("test", "provided", "system"):
                continue
            if g and a and v:
                emit(g, a, resolve(v), "dependency", f"dependency:{g}:{a}")

    # Platform properties pinned via <properties> — only names with a
    # mapping become records; everything else stays invisible, as before.
    for prop_name, mapper in _POM_PROPERTY_MAPPINGS.items():
        if prop_name in props:
            value = props[prop_name]
            if "${" in value:
                record = new_record(
                    "java", prop_name, version=None, kind="property",
                    version_spec=value,
                )
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"unresolved property expression in <{prop_name}>"
                    f" ({value})"))
            else:
                record = new_record(
                    "java", prop_name, version=value, kind="property",
                )
            add_location(record, rel_path, "maven", locator=f"property:{prop_name}")
            records.append(record)

    return records, warnings


# ---------------------------------------------------------------------------
# Gradle parser (regex — covers the common patterns, not every edge case)
# ---------------------------------------------------------------------------

_GRADLE_PATTERN_QUOTED = re.compile(
    r'(?:implementation|api|compileOnly|runtimeOnly|classpath)\s*\(?\s*'
    r'["\']([^:"\'\s]+):([^:"\'\s]+):([^"\'\s]+)["\']'
)
_GRADLE_PATTERN_NAMED = re.compile(
    r'(?:implementation|api|compileOnly|runtimeOnly)\s*\(\s*'
    r'group\s*=\s*"([^"]+)"\s*,\s*name\s*=\s*"([^"]+)"\s*,\s*version\s*=\s*"([^"]+)"',
    re.DOTALL,
)


def parse_gradle_records(path, rel_path):
    """Parse build.gradle / build.gradle.kts; return (records, warnings)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path, f"could not read Gradle file: {exc}")]

    records = []
    warnings = []

    def emit(group, artifact, version, line):
        # A ${...} expression is retained as a warning + a record without
        # a version; it is never emitted as a product.
        if "${" in version:
            record = new_record(
                "java", f"{group}:{artifact}", version=None,
                group=group, artifact=artifact, version_spec=version,
            )
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"unresolved expression in {group}:{artifact} version"
                f" ({version}) at line {line}"))
        else:
            record = new_record(
                "java", f"{group}:{artifact}", version=version,
                group=group, artifact=artifact,
            )
        add_location(record, rel_path, "gradle",
                     line=line, locator=f"dependency:{group}:{artifact}")
        records.append(record)

    for m in _GRADLE_PATTERN_QUOTED.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        emit(m.group(1), m.group(2), m.group(3), line)
    for m in _GRADLE_PATTERN_NAMED.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        emit(m.group(1), m.group(2), m.group(3), line)
    return records, warnings
