"""Maven POM and Gradle build-file parsers (normalized records).

Both parsers return (records, warnings):

    records   list of normalized dependency records (see eol_inventory.models)
    warnings  list of structured warnings for unreadable input, unsupported
              syntax, and unresolved property/gradle expressions

Behavior notes preserved from the original generator:
    - test/provided/system-scoped Maven dependencies never become tracker
      candidates, but are recorded with kind "<scope>-scope-dep" so the
      config declares them (config_writer._SKIPPED_KINDS);
    - versionless dependencies (managed by a parent/BOM) never become
      tracker candidates either — the parent or BOM entry already carries
      the platform — and are recorded with kind "unversioned-dep";
    - Gradle declarations match in both quote styles: the quoted
      "g:a:v" form (optionally wrapped in platform(...)), Groovy map
      notation (group: 'g', name: 'a', version: 'v'), the Kotlin DSL
      named-argument form (group = "g", ...), plugins-block entries
      (kind "plugin"), and libs.* version-catalog references.
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

    def declare_only(group, artifact, version, kind, scope, locator):
        """A parsed declaration that never becomes a tracker candidate.

        Test/provided/system-scope dependencies and dependencies without a
        <version> are recorded so the config's declaration list stays a
        complete picture of the pom, but they carry a kind that config
        assembly declares and then drops: no product row, no unmapped
        item, and no `unresolved_version` warning (there is nothing
        unresolved about a deliberately BOM-managed or test-only
        dependency).
        """
        record = new_record(
            "java", f"{group}:{artifact}",
            version=redact_urls(version) if version else None,
            scope=scope, kind=kind, group=group, artifact=artifact,
        )
        add_location(record, rel_path, "maven", locator=locator)
        records.append(record)

    # Walk all <dependencies> blocks (both top-level and inside <dependencyManagement>)
    for deps_node in root.iter(f"{ns}dependencies"):
        for dep in deps_node.findall(f"{ns}dependency"):
            g, a, v = t(dep, "groupId"), t(dep, "artifactId"), t(dep, "version")
            scope = t(dep, "scope") or "compile"
            if not (g and a):
                continue
            group, artifact = resolve(g), resolve(a)
            locator = f"dependency:{group}:{artifact}"
            if scope in ("test", "provided", "system"):
                declare_only(group, artifact, resolve(v) if v else None,
                             f"{scope}-scope-dep", scope, locator)
                continue
            if not v:
                # Version supplied by the parent or an imported BOM.
                declare_only(group, artifact, None, "unversioned-dep",
                             scope, locator)
                continue
            emit(group, artifact, resolve(v), "dependency", locator)

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

# The (?<!\w) guard pins configuration-name matching to word starts, so test
# configurations (testImplementation, testApi, ...) and any longer word that
# merely contains a configuration name never match — independent of case.
_GRADLE_PATTERN_QUOTED = re.compile(
    r'(?<!\w)(?:implementation|api|compileOnly|runtimeOnly|classpath)\s*'
    r'(?:\(\s*)?(?:platform\s*\(\s*)?([\'"])'
    r'([^:\'"\s]+):([^:\'"\s]+):([^\'"\s]+)\1'
)
# Groovy map notation (`group: 'g', name: 'a', version: 'v'`) and the Kotlin
# DSL named-args form (`group = "g", ...`) share a field grammar; match the
# whole three-field statement, then extract the fields by name. classpath
# (buildscript blocks) uses the same notation as the dependency
# configurations.
_GRADLE_PATTERN_MAP = re.compile(
    r'(?<!\w)(?:implementation|api|compileOnly|runtimeOnly|classpath)\s*\(?\s*'
    r'(?:(?:group|name|version)\s*[:=]\s*[\'"][^\'"]*[\'"]\s*,?\s*){3}',
    re.DOTALL,
)
_GRADLE_MAP_FIELD = re.compile(r'(group|name|version)\s*[:=]\s*[\'"]([^\'"]*)[\'"]')
# Plugins blocks: id("g.a") version "v" / id "g.a" version "v" — the id must
# contain a dot so bare plugin ids like `id 'java'` are ignored — and the
# Kotlin alias form kotlin("jvm") version "v".
_GRADLE_PATTERN_PLUGIN_ID = re.compile(
    r'\bid\s*\(?\s*([\'"])([^\'"\s]*\.[^\'"\s]*)\1\s*\)?\s*'
    r'version\s*([\'"])([^\'"\s]+)\3'
)
_GRADLE_PATTERN_PLUGIN_KOTLIN = re.compile(
    r'\bkotlin\s*\(\s*([\'"])([^\'"\s]+)\1\s*\)\s*'
    r'version\s*([\'"])([^\'"\s]+)\3'
)


# Known-exception table for plugin ids whose published Maven coordinates do
# NOT follow the generic rule (group = full plugin id, artifact = last
# segment + "-gradle-plugin"). Checked before the generic synthesis; the
# generic rule stays the best-effort fallback for unknown ids, so a wrong
# guess surfaces as a visible tracker-health error row ("Artifact ... not
# found on Maven Central or the declared repositories") on every run - the
# repo's fail-loud philosophy treats that as review signal; add newly
# diverged ids here.
_GRADLE_PLUGIN_COORD_ALIASES = {
    # id("io.spring.dependency-management") publishes as
    # io.spring.gradle:dependency-management-plugin (verified live); the
    # generic guess is a permanent not-found error row.
    "io.spring.dependency-management": ("io.spring.gradle",
                                        "dependency-management-plugin"),
}


def _gradle_plugin_coords(plugin_id):
    """Best-effort Maven coordinates for a Gradle plugin id.

    The full plugin id conventionally names the real plugin artifact's group
    (org.springframework.boot:spring-boot-gradle-plugin,
    io.gitlab.arturbosch.detekt:detekt-gradle-plugin, ...); Kotlin plugin ids
    and aliases all ship inside org.jetbrains.kotlin:kotlin-gradle-plugin.
    Ids in _GRADLE_PLUGIN_COORD_ALIASES publish elsewhere and map through
    that table first; every other id keeps the generic best-effort synthesis
    below, so a mismatching guess becomes a visible tracker-health error row
    to review rather than a silently wrong date.
    """
    if plugin_id == "org.jetbrains.kotlin" or plugin_id.startswith("org.jetbrains.kotlin."):
        return "org.jetbrains.kotlin", "kotlin-gradle-plugin"
    if plugin_id in _GRADLE_PLUGIN_COORD_ALIASES:
        return _GRADLE_PLUGIN_COORD_ALIASES[plugin_id]
    return plugin_id, plugin_id.rsplit(".", 1)[-1] + "-gradle-plugin"


def _split_gradle_version_suffix(version):
    """Split a Gradle version token into (base, suffix).

    A 4th colon field is a classifier ('1.0.0:jar' -> ('1.0.0', ':jar'))
    and '@ext' names an artifact extension ('1.0@jar' -> ('1.0', '@jar'));
    both describe the same published version as the base. Anything else
    returns (version, '').
    """
    for separator in (":", "@"):
        if separator in version:
            base, tail = version.split(separator, 1)
            return base, separator + tail
    return version, ""


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


def parse_gradle_records(path, rel_path, catalog=None):
    """Parse build.gradle / build.gradle.kts; return (records, warnings).

    *catalog* is the ``(aliases, bundles)`` pair from
    :func:`parse_version_catalog` (or None). When given, ``libs.*``
    references resolve to ordinary dependency records whose ``version_spec``
    keeps the reference text; a reference the catalog cannot resolve becomes
    a versionless record plus an ``unresolved_version`` warning, so it is
    visible for review instead of silently dropped.
    """
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

    def emit(group, artifact, version, line, interpolates=True,
             version_spec=None, kind="dependency", locator=None):
        # A Groovy/Kotlin string interpolation is retained as a warning and
        # a versionless record; it is never emitted as an exact product.
        # *version_spec* (a resolved catalog reference) is kept on the
        # exact record as provenance of where the version came from.
        version = redact_urls(version)
        # A trailing :classifier or @ext names the same published version
        # as its base; the base alone is the record's version when it is
        # exact (an interpolated or otherwise inexact base keeps the whole
        # token as the unresolved spec).
        base, _suffix = _split_gradle_version_suffix(version)
        interpolated = interpolates and _has_gradle_interpolation(base)
        if not interpolated and _is_exact_java_version(base):
            version = base
        if interpolated or not _is_exact_java_version(version):
            record = new_record(
                "java", f"{group}:{artifact}", version=None, kind=kind,
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
                "java", f"{group}:{artifact}", version=version, kind=kind,
                group=group, artifact=artifact, version_spec=version_spec,
            )
        add_location(record, rel_path, "gradle", line=line,
                     locator=locator or f"dependency:{group}:{artifact}")
        records.append(record)

    # Pass order mirrors the root generator: quoted, map/named, plugins-block
    # ids, kotlin(...) plugin aliases, then catalog references.
    for m in _GRADLE_PATTERN_QUOTED.finditer(scan_text):
        line = scan_text.count("\n", 0, m.start()) + 1
        emit(m.group(2), m.group(3), m.group(4), line,
             interpolates=m.group(1) == '"')
    for m in _GRADLE_PATTERN_MAP.finditer(scan_text):
        fields = {}
        version_quote = None
        for field in _GRADLE_MAP_FIELD.finditer(m.group(0)):
            fields[field.group(1)] = field.group(2)
            if field.group(1) == "version":
                version_quote = field.group(0)[-1]
        if len(fields) == 3:
            line = scan_text.count("\n", 0, m.start()) + 1
            emit(fields["group"], fields["name"], fields["version"], line,
                 interpolates=version_quote == '"')
    for m in _GRADLE_PATTERN_PLUGIN_ID.finditer(scan_text):
        line = scan_text.count("\n", 0, m.start()) + 1
        group, artifact = _gradle_plugin_coords(m.group(2))
        emit(group, artifact, m.group(4), line, interpolates=m.group(3) == '"',
             kind="plugin", locator=f"plugin:{m.group(2)}")
    for m in _GRADLE_PATTERN_PLUGIN_KOTLIN.finditer(scan_text):
        line = scan_text.count("\n", 0, m.start()) + 1
        emit("org.jetbrains.kotlin", "kotlin-gradle-plugin", m.group(4), line,
             interpolates=m.group(3) == '"', kind="plugin",
             locator=f"plugin:org.jetbrains.kotlin.{m.group(2)}")
    if catalog is not None:
        aliases, bundles = catalog
        for m in _CATALOG_REF_RE.finditer(scan_text):
            line = scan_text.count("\n", 0, m.start()) + 1
            reference = m.group(0)
            resolved = _resolve_catalog_ref(m.group(1), aliases, bundles)
            if resolved is None:
                continue
            if not resolved:
                record = new_record(
                    "java", reference, version=None, version_spec=reference)
                add_location(record, rel_path, "gradle", line=line,
                             locator=f"catalog:{reference}")
                records.append(record)
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"version catalog reference {reference} does not"
                    f" resolve at line {line}"))
                continue
            for group, artifact, version in resolved:
                emit(group, artifact, version, line, interpolates=False,
                     version_spec=reference)
    return records, warnings


# ---------------------------------------------------------------------------
# Gradle version catalogs (libs.versions.toml) — best-effort TOML subset:
# only the [versions]/[libraries]/[bundles] tables with quoted-string values.
# ---------------------------------------------------------------------------

_CATALOG_REF_RE = re.compile(r"\blibs\.([A-Za-z0-9_.\-]+)")
_TOML_FIELD_RE = re.compile(r'([A-Za-z0-9_.\-]+)\s*=\s*(?:\'([^\']*)\'|"([^"]*)")')


def _norm_alias(alias):
    """Normalize a catalog alias/reference for matching: TOML 'commons-lang3'
    and code reference 'commons.lang3' both -> 'commons.lang3'."""
    return re.sub(r"[-_]", ".", alias.lower())


def _strip_toml_comment(line):
    """Drop a trailing TOML comment, respecting quoted strings."""
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch == "#":
            break
        else:
            out.append(ch)
            if ch in "\"'":
                quote = ch
    return "".join(out)


def _toml_inline_fields(value):
    """Extract key = "string" pairs from a TOML inline-table blob."""
    value = re.sub(
        r"version\s*=\s*\{\s*ref\s*=\s*([\'\"])([^\'\"]+)\1\s*\}",
        r'version.ref = "\2"', value)
    return {k: (m or n) for k, m, n in _TOML_FIELD_RE.findall(value)}


def parse_version_catalog(path, rel_path):
    """Parse a libs.versions.toml catalog; return (aliases, bundles, warnings).

    aliases: normalized alias -> (group, artifact, version). Aliases with no
    resolvable version (unknown version.ref, no module/group) are skipped.
    bundles: normalized bundle name -> list of normalized aliases. TOML
    support is a best-effort subset: no multi-line arrays, no nested inline
    tables beyond version = { ref = "..." }. An unreadable file yields empty
    tables plus an ``unreadable_file`` warning.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, {}, [new_warning(
            "unreadable_file", rel_path,
            f"could not read version catalog: {exc}")]

    section = None
    versions = {}
    libraries = {}
    bundles = {}
    for raw in text.splitlines():
        line = _strip_toml_comment(raw).strip()
        if not line:
            continue
        m = re.match(r"^\[([A-Za-z0-9_.\-]+)\]$", line)
        if m:
            section = m.group(1).lower()
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*=\s*(.+)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if section == "versions":
            sm = re.match(r"^[\'\"]([^\'\"]*)[\'\"]$", value)
            if sm:
                versions[key] = sm.group(1)
        elif section == "libraries":
            fields = _toml_inline_fields(value)
            if fields:
                libraries[key] = fields
        elif section == "bundles":
            if value.startswith("[") and value.endswith("]"):
                bundles[key] = re.findall(r"[\'\"]([^\'\"]+)[\'\"]", value)

    aliases = {}
    for alias, fields in libraries.items():
        module = fields.get("module", "")
        if ":" in module:
            group, artifact = module.split(":", 1)
        else:
            group, artifact = fields.get("group", ""), fields.get("name", "")
        version = fields.get("version") or versions.get(fields.get("version.ref", ""))
        if not (group and artifact and version):
            continue
        aliases[_norm_alias(alias)] = (group, artifact, version)

    norm_bundles = {
        _norm_alias(name): [_norm_alias(member) for member in members]
        for name, members in bundles.items()
    }
    return aliases, norm_bundles, []


def _resolve_catalog_ref(segment, aliases, bundles):
    """Resolve one ``libs.<segment>`` reference against a catalog.

    Returns None for references that are not library lookups
    (``libs.versions.*`` and ``libs.plugins.*``), a list of ``(g, a, v)``
    triples for a library or bundle, and an empty list when the catalog has
    no such alias or bundle (or the bundle expands to nothing resolvable).
    """
    seg = segment.lower()
    if seg.startswith("versions.") or seg.startswith("plugins."):
        return None
    if seg.startswith("bundles."):
        members = bundles.get(_norm_alias(seg[len("bundles."):]), [])
        return [aliases[alias] for alias in members if alias in aliases]
    gav = aliases.get(_norm_alias(seg))
    return [gav] if gav else []


def nearest_catalog(catalogs, rel_path):
    """The catalog governing *rel_path*: the entry of *catalogs* (a mapping
    of "/"-separated directory -> (aliases, bundles), "" for the scan root)
    at or nearest above the file's directory, or None."""
    if not catalogs:
        return None
    parts = rel_path.split("/")[:-1]
    while True:
        catalog = catalogs.get("/".join(parts))
        if catalog is not None:
            return catalog
        if not parts:
            return None
        parts.pop()


def catalog_scope(rel_path):
    """The directory a catalog file governs: the parent of its ``gradle/``
    folder for the conventional ``gradle/libs.versions.toml`` placement,
    otherwise the file's own directory ("" for the scan root)."""
    parts = rel_path.split("/")[:-1]
    if parts and parts[-1] == "gradle":
        parts = parts[:-1]
    return "/".join(parts)
