"""Maven POM and Gradle build-file parsers.

Moved verbatim from the original root generate_config.py.
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


_POM_NS = "{http://maven.apache.org/POM/4.0.0}"


# ---------------------------------------------------------------------------
# POM parser
# ---------------------------------------------------------------------------

def _t(elem, name, ns=_POM_NS):
    """Read text of <ns:name> child, or None."""
    if elem is None:
        return None
    n = elem.find(f"{ns}{name}")
    if n is None:
        n = elem.find(name)  # fallback for POMs without namespace
    return n.text.strip() if n is not None and n.text else None


def parse_pom(path):
    """Parse pom.xml; return (deps, properties, source_path).

    deps:       list of (group, artifact, version, kind) — kind in {"parent","dep"}
    properties: dict of property name -> resolved value
    """
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        print(f"  ! parse error in {path}: {exc}", file=sys.stderr)
        return [], {}

    root = tree.getroot()
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

    deps = []

    parent = root.find(f"{ns}parent")
    if parent is not None:
        pg, pa, pv = t(parent, "groupId"), t(parent, "artifactId"), t(parent, "version")
        if pg and pa and pv:
            deps.append((pg, pa, resolve(pv), "parent"))

    # Walk all <dependencies> blocks (both top-level and inside <dependencyManagement>)
    for deps_node in root.iter(f"{ns}dependencies"):
        for dep in deps_node.findall(f"{ns}dependency"):
            g, a, v = t(dep, "groupId"), t(dep, "artifactId"), t(dep, "version")
            scope = t(dep, "scope") or "compile"
            if scope in ("test", "provided", "system"):
                continue
            if g and a and v:
                deps.append((g, a, resolve(v), "dep"))

    return deps, props


# ---------------------------------------------------------------------------
# Gradle parser (regex — covers the common patterns, not every edge case)
# ---------------------------------------------------------------------------

_GRADLE_PATTERN_QUOTED = re.compile(
    r'(?:implementation|api|compileOnly|runtimeOnly|classpath)\s*\(?\s*'
    r'"([^:"\s]+):([^:"\s]+):([^"\s]+)"'
)
_GRADLE_PATTERN_NAMED = re.compile(
    r'(?:implementation|api|compileOnly|runtimeOnly)\s*\(\s*'
    r'group\s*=\s*"([^"]+)"\s*,\s*name\s*=\s*"([^"]+)"\s*,\s*version\s*=\s*"([^"]+)"',
    re.DOTALL,
)


def parse_gradle(path):
    """Parse build.gradle / build.gradle.kts; return [(g, a, v, "gradle"), ...]."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  ! read error on {path}: {exc}", file=sys.stderr)
        return []
    deps = []
    for m in _GRADLE_PATTERN_QUOTED.finditer(text):
        deps.append((m.group(1), m.group(2), m.group(3), "gradle"))
    for m in _GRADLE_PATTERN_NAMED.finditer(text):
        deps.append((m.group(1), m.group(2), m.group(3), "gradle"))
    return deps
