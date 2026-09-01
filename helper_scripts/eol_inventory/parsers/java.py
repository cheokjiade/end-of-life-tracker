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
from ..redact import redact_urls

_POM_NS = "{http://maven.apache.org/POM/4.0.0}"
_JAVA_EXACT_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
_JAVA_BAD_SEPARATOR_RE = re.compile(r"[._+-](?:[._+-]|$)")


def _is_exact_java_version(value):
    """Whether a Maven/Gradle token is a concrete registry version."""
    lowered = value.lower() if value else ""
    return bool(
        value
        and _JAVA_EXACT_VERSION_RE.fullmatch(value)
        and lowered not in ("latest", "release")
        and not lowered.startswith("latest.")
        and not _JAVA_BAD_SEPARATOR_RE.search(value))


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
        # Expressions, ranges, dynamics, and malformed tokens are retained as
        # warnings plus versionless records; they never become provider rows.
        version = redact_urls(version)
        if "${" in group or "${" in artifact:
            record = new_record(
                "java", f"{group}:{artifact}", version=None,
                kind=kind, group=group, artifact=artifact,
                version_spec=version,
            )
            warnings.append(new_warning(
                "unresolved_identifier", rel_path,
                f"unresolved Maven coordinate {group}:{artifact}; not guessed"))
        elif "${" in version or not _is_exact_java_version(version):
            record = new_record(
                "java", f"{group}:{artifact}", version=None,
                kind=kind, group=group, artifact=artifact,
                version_spec=version,
            )
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"no exact version for {group}:{artifact}"
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
            emit(resolve(pg), resolve(pa), resolve(pv), "parent", "parent")

    # Walk all <dependencies> blocks (both top-level and inside <dependencyManagement>)
    for deps_node in root.iter(f"{ns}dependencies"):
        for dep in deps_node.findall(f"{ns}dependency"):
            g, a, v = t(dep, "groupId"), t(dep, "artifactId"), t(dep, "version")
            scope = t(dep, "scope") or "compile"
            if scope in ("test", "provided", "system"):
                continue
            if g and a and v:
                group, artifact = resolve(g), resolve(a)
                emit(group, artifact, resolve(v), "dependency",
                     f"dependency:{group}:{artifact}")

    # Platform properties pinned via <properties> — only names with a
    # mapping become records; everything else stays invisible, as before.
    for prop_name, mapper in _POM_PROPERTY_MAPPINGS.items():
        if prop_name in props:
            value = redact_urls(props[prop_name])
            if "${" in value or not _is_exact_java_version(value):
                record = new_record(
                    "java", prop_name, version=None, kind="property",
                    version_spec=value,
                )
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"no exact version in <{prop_name}>"
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
    r'(?P<quote>["\'])(?P<group>[^:"\'\s]+):'
    r'(?P<artifact>[^:"\'\s]+):(?P<version>[^"\'\s]+)(?P=quote)'
)
_GRADLE_PATTERN_NAMED = re.compile(
    r'(?:implementation|api|compileOnly|runtimeOnly)\s*\(\s*'
    r'group\s*=\s*"([^"]+)"\s*,\s*name\s*=\s*"([^"]+)"\s*,\s*version\s*=\s*"([^"]+)"',
    re.DOTALL,
)


def _has_gradle_interpolation(value):
    """Detect unescaped Groovy/Kotlin $ templates, including Unicode names."""
    for index, char in enumerate(value):
        if char != "$":
            continue
        slashes = 0
        before = index - 1
        while before >= 0 and value[before] == "\\":
            slashes += 1
            before -= 1
        if slashes % 2:
            continue
        tail = value[index + 1:]
        if tail.startswith("{"):
            return True
        if tail and tail[0].isidentifier():
            return True
    return False


def parse_gradle_records(path, rel_path):
    """Parse build.gradle / build.gradle.kts; return (records, warnings)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path, f"could not read Gradle file: {exc}")]

    masked = list(text)
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    i = 0
    while i < len(text):
        char = text[i]
        pair = text[i:i + 2]
        if line_comment:
            if char in "\r\n":
                line_comment = False
            else:
                masked[i] = " "
        elif block_comment:
            if pair == "*/":
                masked[i:i + 2] = "  "
                block_comment = False
                i += 1
            elif char not in "\r\n":
                masked[i] = " "
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif pair == "//":
            masked[i:i + 2] = "  "
            line_comment = True
            i += 1
        elif pair == "/*":
            masked[i:i + 2] = "  "
            block_comment = True
            i += 1
        i += 1
    scan_text = "".join(masked)

    records = []
    warnings = []

    def emit(group, artifact, version, line, interpolates=True):
        # A Groovy/Kotlin string interpolation is retained as a warning and
        # a versionless record; it is never emitted as an exact product.
        version = redact_urls(version)
        if ((interpolates and _has_gradle_interpolation(version))
                or not _is_exact_java_version(version)):
            record = new_record(
                "java", f"{group}:{artifact}", version=None,
                group=group, artifact=artifact, version_spec=version,
            )
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"no exact version for {group}:{artifact}"
                f" ({version}) at line {line}"))
        else:
            if interpolates:
                version = version.replace("\\$", "$")
            record = new_record(
                "java", f"{group}:{artifact}", version=version,
                group=group, artifact=artifact,
            )
        add_location(record, rel_path, "gradle",
                     line=line, locator=f"dependency:{group}:{artifact}")
        records.append(record)

    for m in _GRADLE_PATTERN_QUOTED.finditer(scan_text):
        line = scan_text.count("\n", 0, m.start()) + 1
        emit(m.group("group"), m.group("artifact"), m.group("version"),
             line, interpolates=m.group("quote") == '"')
    for m in _GRADLE_PATTERN_NAMED.finditer(scan_text):
        line = scan_text.count("\n", 0, m.start()) + 1
        emit(m.group(1), m.group(2), m.group(3), line)
    return records, warnings
