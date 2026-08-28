"""Generate an EOL tracker config from a project's dependency files.

Scans a folder for Maven, Gradle, and Node manifests; emits an
eol_config.<project>.json file suitable for use with lambda_function.py.

Supported formats:
    pom.xml             — Maven (multi-module supported via rglob); versioned
                          deps map, <dependencyManagement> declarations are
                          tagged managed-dep, version-less deps are tagged
                          unversioned-dep (recorded, not mapped); root-level
                          <repositories> URLs are collected
    *.gradle.kts        — Gradle Kotlin DSL: quoted GAV strings (optionally
                          wrapped in platform(...)), named-arg form, plugins
                          blocks, libs.* version-catalog references, and
                          dependency `repositories { }` block URLs
    build.gradle        — Gradle Groovy DSL (same patterns; ' or " quotes,
                          map notation, repositories block URLs)
    libs.versions.toml  — Gradle version catalogs, resolved into
                          "gradle-catalog" entries (best-effort TOML subset)
    package.json        — Node (skips node_modules)

Mapping strategy:
    Java deps   -> known group:artifact patterns map to specific tracker
                   providers (endoflife.date Spring Boot/Framework/Tomcat/
                   Log4j, jackson_lifecycle, aws_sdk_lifecycle); everything
                   else falls back to maven_central staleness. Shibboleth
                   groups (org.opensaml, net.shibboleth.*) are emitted
                   against the Shibboleth repository, not Maven Central.
                   Versions that no registry resolves — -SNAPSHOT,
                   ${property} / Groovy $var placeholders, Maven ranges
                   ([2.0,) or an unterminated [2.0), classifier variants
                   (1.0:ext), and Gradle dynamic versions (2.+, 1.0+eap,
                   latest.*) — are skipped; ext suffixes (1.0@jar) are
                   truncated to 1.0.
    Gradle plugin ids -> best-effort Maven coordinates, then the normal
                   java mapping (kind "gradle-plugin").
    Declared repos -> artifact-repository URLs (pom <repositories>,
                   gradle `repositories { }` blocks; publishing blocks and
                   profile-conditional pom repositories ignored) are
                   collected, deduped, and emitted as the config-level
                   "maven_repositories" key — the runtime stamps them onto
                   maven_central entries lacking an explicit 'repository'
                   as fallback lookups when an artifact is not on Maven
                   Central.
    POM props   -> known names (tomcat.version, netty.version, logback.version,
                   quartz.version, kotlin.version, java.version) produce the
                   matching tracker entry — catches transitively-managed
                   platforms not declared as explicit <dependency>s.
    Node deps   -> known package names map to endoflife.date entries
                   (react, vue, angular, next, nuxt, node, express,
                   ckeditor); unmapped packages are listed in
                   _skipped_npm_packages for manual review (vue bare-major
                   specs like '^3' and non-numeric minor specs like '3.x'
                   are skipped there too — no such cycle).

Complete picture vs runnable set:
    products stays the deduped runnable set (first declaration wins).
    Every parsed declaration (tracked, duplicate, skipped, or unmapped)
    is additionally recorded in the top-level _discovered_dependencies
    list (records: decl, file, kind, outcome) so the generated config
    shows the complete picture of what the manifests contained, not just
    what runs. Section markers are not declarations and get no record.

Usage:
    python generate_config.py <folder> [--name PROJECT] [--output FILE]

Examples:
    python generate_config.py "project-b" --name b
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path


_POM_NS = "{http://maven.apache.org/POM/4.0.0}"


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def _major(v):
    """'3.5.7' -> '3'.  Used for products with major-only EOL cycles."""
    return v.split(".")[0]


def _major_minor(v):
    """'3.5.7' -> '3.5'.  Most endoflife.date cycles are major.minor."""
    parts = v.split(".")
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else v


def _clean_version(v):
    """Strip semver range prefixes (^, ~, >=) and common Maven qualifiers."""
    if not v:
        return v
    v = re.sub(r"^[\^~>=<\s]+", "", v).strip()
    v = re.sub(r"\s+.*$", "", v)  # take first token if range like ">=1.0.0 <2.0.0"
    return v


# ---------------------------------------------------------------------------
# Tracker entry builders
# ---------------------------------------------------------------------------

def _eol_entry(product, version, label):
    return {"product": product, "version": version, "label": label}


def _mc_entry(group, artifact, version, label):
    return {
        "source":   "maven_central",
        "group":    group,
        "artifact": artifact,
        "version":  version,
        "label":    label,
    }


_SHIBBOLETH_REPOSITORY = (
    "https://build.shibboleth.net/nexus/content/repositories/releases")


def _jackson_artifact_title(artifact):
    """'jackson-databind' -> 'Databind'; 'jackson-bom' -> 'BOM'; 'foo' -> 'Foo'."""
    body = artifact[len("jackson-"):] if artifact.startswith("jackson-") else artifact
    part = body.split("-", 1)[0] if body else artifact
    if part.lower() == "bom":
        return "BOM"
    return part.capitalize() if part else artifact


def _shibboleth_mc_entry(group, artifact, version):
    entry = _mc_entry(group, artifact, version, f"{artifact} {version}")
    entry["repository"] = _SHIBBOLETH_REPOSITORY
    note = ("Hosted on the Shibboleth repository, not Maven Central; each "
            "major version's support ends with its Shibboleth IdP release "
            "train")
    if group == "org.opensaml":
        note += " (OpenSAML 4 EOL 2024-09-01)"
    entry["policy_note"] = note + "."
    return entry


# ---------------------------------------------------------------------------
# Java group:artifact -> tracker entry mappings
#
# Order matters — specific patterns first, generic fallback last.
# Each tuple: (predicate(group, artifact), handler(group, artifact, version)).
# Handler may return None to skip a dep entirely.
# ---------------------------------------------------------------------------

_JAVA_MAPPINGS = [
    (
        lambda g, a: g == "org.springframework.boot",
        lambda g, a, v: _eol_entry("spring-boot", _major_minor(v),
                                   f"Spring Boot {_major_minor(v)}"),
    ),
    (
        lambda g, a: g == "org.springframework" and a.startswith("spring-"),
        lambda g, a, v: _eol_entry("spring-framework", _major_minor(v),
                                   f"Spring Framework {_major_minor(v)}"),
    ),
    (
        lambda g, a: g == "org.springframework.security",
        lambda g, a, v: _eol_entry("spring-security", _major_minor(v),
                                   f"Spring Security {_major_minor(v)}"),
    ),
    (
        lambda g, a: g.startswith("org.apache.tomcat"),
        lambda g, a, v: _eol_entry("tomcat", _major_minor(v),
                                   f"Apache Tomcat {_major_minor(v)}"),
    ),
    (
        lambda g, a: g.startswith("org.apache.logging.log4j"),
        lambda g, a, v: _eol_entry("log4j", _major(v),
                                   f"Apache Log4j {_major(v)}.x"),
    ),
    (
        lambda g, a: g.startswith("com.fasterxml.jackson"),
        lambda g, a, v: {
            "source":   "jackson_lifecycle",
            "group":    g,
            "artifact": a,
            "version":  _major_minor(v),
            "label":    f"Jackson {_jackson_artifact_title(a)} {_major_minor(v)}",
        },
    ),
    (
        lambda g, a: g == "software.amazon.awssdk",
        lambda g, a, v: {
            "source": "aws_sdk_lifecycle",
            "sdk":    "SDK for Java",
            "major":  "2.x",
            "label":  "AWS SDK for Java v2",
        },
    ),
    (
        lambda g, a: g == "com.amazonaws" and a.startswith("aws-java-sdk"),
        lambda g, a, v: {
            "source": "aws_sdk_lifecycle",
            "sdk":    "SDK for Java",
            "major":  "1.x",
            "label":  "AWS SDK for Java v1 (legacy)",
        },
    ),
    (
        lambda g, a: g == "org.jetbrains.kotlin",
        lambda g, a, v: _eol_entry("kotlin", _major_minor(v),
                                   f"Kotlin {_major_minor(v)}"),
    ),
    # OpenSAML / Shibboleth artifacts are distributed from the Shibboleth
    # repository, not Maven Central (since OpenSAML 3).
    (
        lambda g, a: (g == "org.opensaml" or g == "net.shibboleth"
                      or g.startswith("net.shibboleth.")),
        lambda g, a, v: _shibboleth_mc_entry(g, a, v),
    ),
    # Skip junk we don't want to track
    (
        lambda g, a: a in ("junit", "junit-vintage-engine", "junit-jupiter",
                            "mockito-inline", "awaitility", "spring-boot-starter-test",
                            "spring-security-test", "gson"),
        lambda g, a, v: None,
    ),
    # webjars: bootstrap and jquery have endoflife.date entries (major-only cycles)
    (
        lambda g, a: g.startswith("org.webjars") and a == "bootstrap",
        lambda g, a, v: _eol_entry("bootstrap", _major(v), f"Bootstrap {_major(v)} (using {v})"),
    ),
    (
        lambda g, a: g.startswith("org.webjars") and a == "jquery",
        lambda g, a, v: _eol_entry("jquery", _major(v), f"jQuery {_major(v)} (using {v})"),
    ),
    # Other webjars (chartjs, popper, dompurify, ...) have no useful upstream
    (
        lambda g, a: g.startswith("org.webjars"),
        lambda g, a, v: None,
    ),
    # Default fallback: Maven Central staleness for any other Java dep
    (
        lambda g, a: True,
        lambda g, a, v: _mc_entry(g, a, v, f"{a} {v}"),
    ),
]


# ---------------------------------------------------------------------------
# POM property name -> tracker entry mappings
#
# These catch transitively-managed platforms that the team pins via a
# property override (e.g. <tomcat.version>10.1.54</tomcat.version>) but
# never declares as an explicit <dependency>.
# ---------------------------------------------------------------------------

_POM_PROPERTY_MAPPINGS = {
    "java.version":           lambda v: _eol_entry("amazon-corretto", _major(v),
                                                   f"Amazon Corretto (OpenJDK) {_major(v)}"),
    "maven.compiler.release": lambda v: _eol_entry("amazon-corretto", _major(v),
                                                   f"Amazon Corretto (OpenJDK) {_major(v)}"),
    "tomcat.version":         lambda v: _eol_entry("tomcat", _major_minor(v),
                                                   f"Apache Tomcat {_major_minor(v)}"),
    "netty.version":          lambda v: _mc_entry("io.netty", "netty-codec-http", v,
                                                  f"Netty Codec HTTP {v}"),
    "logback.version":        lambda v: _mc_entry("ch.qos.logback", "logback-classic", v,
                                                  f"Logback Classic {v}"),
    "quartz.version":         lambda v: _mc_entry("org.quartz-scheduler", "quartz", v,
                                                  f"Quartz {v}"),
    "kotlin.version":         lambda v: _eol_entry("kotlin", _major_minor(v),
                                                   f"Kotlin {_major_minor(v)}"),
    "scala.version":          lambda v: _eol_entry("scala", _major_minor(v),
                                                   f"Scala {_major_minor(v)}"),
}


# ---------------------------------------------------------------------------
# npm package name -> tracker entry mappings
#
# Returns None to skip / un-mapped (those go into _skipped_npm_packages).
# Only packages with endoflife.date coverage are mapped — there's no npm
# staleness provider yet.
# ---------------------------------------------------------------------------

def _vue_entry(version):
    """vue -> endoflife.date entry, or None when the spec must be skipped.

    endoflife.date's vue cycles are major.minor ('3.5', '3.4', '3.3',
    '2.7', ... '2.0') plus the bare-major cycle '1'; there are no cycles
    '3', '2' or '1.0' (verified live against /api/vue.json). A bare-major
    spec ('^3', '3', '2') must therefore not be guessed into a cycle —
    return None so the package lands in _skipped_npm_packages — while a
    numeric 1.x.y pin ('1.0', '1.2.3') maps to the bare-major cycle '1'
    (label 'Vue 1'), since no 1.x minor cycles exist. Both the major and
    minor segments must be numeric before any mapping: a range-style spec
    ('3.x', '3.X', '2.x', '1.x', '1.x.y') has no matching cycle at all,
    and a v-prefixed spec ('v3.5.3') splits into a non-numeric major
    segment 'v3' (_clean_version does not strip a leading 'v') — skipping
    both is safer than a doomed row.
    """
    parts = (version or "").split(".")
    if len(parts) < 2:
        return None
    if not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    if parts[0] == "1":
        return _eol_entry("vue", "1", "Vue 1")
    return _eol_entry("vue", _major_minor(version), f"Vue {_major_minor(version)}")


_NPM_MAPPINGS = {
    "react":                       lambda v: _eol_entry("react", _major(v),
                                                         f"React {_major(v)}"),
    "react-dom":                   lambda v: None,           # tracked via 'react'
    "vue":                         _vue_entry,
    "@angular/core":               lambda v: _eol_entry("angular", _major(v),
                                                        f"Angular {_major(v)}"),
    "next":                        lambda v: _eol_entry("nextjs", _major(v),
                                                        f"Next.js {_major(v)}"),
    "nuxt":                        lambda v: _eol_entry("nuxt", _major(v),
                                                        f"Nuxt {_major(v)}"),
    "node":                        lambda v: _eol_entry("nodejs", _major(v),
                                                        f"Node.js {_major(v)}"),
    "express":                     lambda v: _eol_entry("express", _major(v),
                                                        f"Express {_major(v)}"),
    "ckeditor":                    lambda v: _eol_entry("ckeditor", _major(v),
                                                        f"CKEditor {_major(v)}"),
    "@ckeditor/ckeditor5-core":    lambda v: _eol_entry("ckeditor", "5", "CKEditor 5"),
}


def _is_maven_version_range(version):
    """True for Maven range syntax: '[2.16.0,)' or '(1.0,2.0]'.

    Unterminated ranges count too: a version that merely starts with '['
    or '(' (e.g. '[2.16.0') resolves nowhere and is skipped as well.
    """
    return bool(version) and version[0] in "[("


def _is_dynamic_version(version):
    """True for Gradle dynamic versions: '2.+', '1.0+eap', 'latest',
    'latest.release', ...

    Any version containing '+' counts as dynamic, because Gradle uses '+'
    in open-ended selectors ('2.+', '1.0+eap'). Trade-off: semver
    build-metadata versions like '1.2.3+build.5' are skipped too — rare in
    dependency manifests, and a skipped pin is safer than a tracker row
    doomed to mismatch every cycle lookup.
    """
    if not version:
        return False
    return "+" in version or version == "latest" or version.startswith("latest.")


def _strip_classifier_ext(version):
    """Normalize gradle GAV suffixes on a version string.

    '1.0:test-jar' (a 4th colon field = classifier) -> None: the classifier
    variant duplicates the base artifact and no registry resolves the
    joined string as written. '1.0@jar' (ext suffix) -> '1.0'. Returns None
    when nothing usable remains (e.g. '@jar' alone).
    """
    if ":" in version:
        return None
    if "@" in version:
        version = version.split("@", 1)[0]
    return version or None


def _map_java_dep_with_reason(group, artifact, version):
    """Map (group, artifact, version) to (entry, None) or (None, reason).

    Applies exactly the skip conditions of _map_java_dep, but returns a
    standardized reason string alongside the None so generate_config can
    record WHY a declaration produced no tracker row in
    _discovered_dependencies. Reasons mirror the checks in order:
    'classifier variant (duplicates the base artifact)', 'no version
    (parent/BOM-managed)', 'SNAPSHOT version', 'internal group',
    'unresolved property placeholder', 'maven version range',
    'gradle dynamic version', and 'known-untracked test dependency' when
    the matched handler opts out (junit & co).
    """
    if ":" in (version or ""):
        return None, "classifier variant (duplicates the base artifact)"
    version = _strip_classifier_ext(version)
    if not version:
        return None, "no version (parent/BOM-managed)"
    if version.endswith("-SNAPSHOT"):
        return None, "SNAPSHOT version"
    if group.startswith("internal."):
        return None, "internal group"
    if "$" in version:
        # Braced ${property} placeholders AND Groovy double-quoted $var
        # interpolation (implementation "g:a:$jacksonVersion") — the
        # literal string resolves nowhere on any registry.
        return None, "unresolved property placeholder"
    if _is_maven_version_range(version):
        return None, "maven version range"
    if _is_dynamic_version(version):
        return None, "gradle dynamic version"
    for pred, handler in _JAVA_MAPPINGS:
        if pred(group, artifact):
            entry = handler(group, artifact, version)
            if entry is None:
                return None, "known-untracked test dependency"
            return entry, None
    return None, "known-untracked test dependency"


def _map_java_dep(group, artifact, version):
    """Map (group, artifact, version) to a tracker entry, or None to skip.

    Thin wrapper over _map_java_dep_with_reason, kept with its original
    signature for existing callers and tests. Skips anything no public
    registry resolves as written: SNAPSHOT builds (in-flight project
    versions), internal coordinate prefixes, $-placeholder versions
    (braced ${property} or Groovy $var interpolation — the literal string
    resolves nowhere), classifier variants ('1.0:test-jar' - they
    duplicate the base artifact), Maven version ranges including
    unterminated ones ('[2.0,'), and Gradle dynamic versions ('2.+',
    'latest', '1.0+eap'). Ext suffixes ('1.0@jar') are truncated to the
    plain version before mapping.
    """
    return _map_java_dep_with_reason(group, artifact, version)[0]


def _map_npm_dep(name, version):
    handler = _NPM_MAPPINGS.get(name)
    return handler(_clean_version(version)) if handler else None


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
    """Parse pom.xml; return (deps, properties, repositories).

    deps:       list of (group, artifact, version, kind) — kind in
                {"parent", "dep", "managed-dep", "unversioned-dep",
                "test-scope-dep", "provided-scope-dep", "system-scope-dep"}:
                versioned deps inside a <dependencyManagement> block are
                "managed-dep" (BOM/version declarations, not direct usage);
                deps lacking a <version> (parent/BOM-managed) are
                "unversioned-dep" with version None; deps with a test,
                provided, or system <scope> carry that scope kind. All
                kinds are recorded for _discovered_dependencies; only
                "dep" and "managed-dep" map to tracker entries.
    properties: dict of property name -> resolved value
    repositories: URLs from root-level <repositories><repository> children.
                Snapshot and releases-disabled repositories are collected
                too — the runtime normalizes and picks — but repositories
                declared inside <profiles> (which activate conditionally)
                are deliberately ignored.
    """
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        print(f"  ! parse error in {path}: {exc}", file=sys.stderr)
        return [], {}, []

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

    # Walk all <dependencies> blocks, tagging the kind. root.iter() loses
    # parenting, so build a parent map to tell whether a block is enclosed
    # in a <dependencyManagement> declaration.
    parent_map = {child: parent for parent in root.iter() for child in parent}

    def _enclosed_in(elem, tag):
        p = parent_map.get(elem)
        while p is not None:
            if p.tag == tag:
                return True
            p = parent_map.get(p)
        return False

    for deps_node in root.iter(f"{ns}dependencies"):
        kind = ("managed-dep"
                if _enclosed_in(deps_node, f"{ns}dependencyManagement")
                else "dep")
        for dep in deps_node.findall(f"{ns}dependency"):
            g, a, v = t(dep, "groupId"), t(dep, "artifactId"), t(dep, "version")
            scope = t(dep, "scope") or "compile"
            if scope in ("test", "provided", "system"):
                # Non-runtime scope: recorded (with its scope kind) so the
                # complete picture shows it, but never mapped to a row.
                if g and a:
                    deps.append((g, a, resolve(v), f"{scope}-scope-dep"))
                continue
            if g and a and v:
                deps.append((g, a, resolve(v), kind))
            elif g and a:
                # Parent/BOM-managed: no version of its own to check.
                deps.append((g, a, None, "unversioned-dep"))

    # Root-level declared artifact repositories. Only direct <repositories>
    # children of the project root count: profile-conditional repositories
    # are skipped (see docstring).
    repos = []
    repos_node = root.find(f"{ns}repositories")
    if repos_node is not None:
        for repo in repos_node.findall(f"{ns}repository"):
            url = t(repo, "url")
            if url:
                repos.append(url)

    return deps, props, repos


# ---------------------------------------------------------------------------
# Gradle parser (regex — covers the common patterns, not every edge case)
# ---------------------------------------------------------------------------

def _strip_gradle_comments(text):
    """Remove // line comments and /* ... */ block comments from gradle
    sources, leaving quoted strings untouched (Groovy and Kotlin share the
    same comment and quoting grammar for this purpose).

    A '//' inside a string literal must survive — dependency coordinates and
    maven { url = uri("https://...") } blocks — and, conversely, an
    apostrophe inside a comment ("don't ship this") must not open a string
    that swallows following code. Backslash escapes inside strings are
    honoured. Line comments are removed up to (and keeping) the newline;
    block comments collapse to a single space, since every pattern below
    spans whitespace. Unterminated comments run to end of text.
    """
    out = []
    i = 0
    n = len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and text[i + 1:i + 2] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue
        if ch == "/" and text[i + 1:i + 2] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                break
            out.append(" ")
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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
# Repository URL declarations inside a `repositories { ... }` block, in the
# three forms Gradle accepts: `url = uri("...")` (Kotlin DSL and Groovy),
# `url = "..."` (direct assignment), and the Groovy shorthand `url "..."`.
_GRADLE_REPO_URL_RE = re.compile(
    r'\burl\s*(?:=\s*uri\s*\(\s*|=\s*|\s+)'
    r'([\'"])([^\'"]+)\1\s*\)?'
)
# The keyword opening the block whose `{` was just reached (dotted names
# allowed, e.g. project.repositories); used while scanning nesting.
_GRADLE_BLOCK_NAME_RE = re.compile(r"([A-Za-z_][\w.]*)\s*\{\Z")


def _gradle_plugin_coords(plugin_id):
    """Best-effort Maven coordinates for a Gradle plugin id.

    The full plugin id conventionally names the real plugin artifact's group
    (org.springframework.boot:spring-boot-gradle-plugin,
    io.gitlab.arturbosch.detekt:detekt-gradle-plugin, ...); Kotlin plugin ids
    and aliases all ship inside org.jetbrains.kotlin:kotlin-gradle-plugin.
    """
    if plugin_id == "org.jetbrains.kotlin" or plugin_id.startswith("org.jetbrains.kotlin."):
        return "org.jetbrains.kotlin", "kotlin-gradle-plugin"
    return plugin_id, plugin_id.rsplit(".", 1)[-1] + "-gradle-plugin"


def parse_gradle(path, catalog=None):
    """Parse build.gradle / build.gradle.kts.

    Groovy/Kotlin // line comments and /* ... */ block comments are stripped
    first (string literals are respected, so a // inside a dependency string
    or a maven { url = uri("https://...") } block survives), so
    commented-out dependencies never produce entries.

    Returns [(g, a, v, kind), ...] with kind "gradle" for dependencies,
    "gradle-plugin" for plugins-block entries, and "gradle-catalog" for
    libs.* references resolved against `catalog` (the (aliases, bundles)
    pair returned by parse_version_catalog).
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  ! read error on {path}: {exc}", file=sys.stderr)
        return []
    text = _strip_gradle_comments(text)
    deps = []
    for m in _GRADLE_PATTERN_QUOTED.finditer(text):
        deps.append((m.group(2), m.group(3), m.group(4), "gradle"))
    for m in _GRADLE_PATTERN_MAP.finditer(text):
        fields = dict(_GRADLE_MAP_FIELD.findall(m.group(0)))
        if len(fields) == 3:
            deps.append((fields["group"], fields["name"], fields["version"], "gradle"))
    for m in _GRADLE_PATTERN_PLUGIN_ID.finditer(text):
        g, a = _gradle_plugin_coords(m.group(2))
        deps.append((g, a, m.group(4), "gradle-plugin"))
    for m in _GRADLE_PATTERN_PLUGIN_KOTLIN.finditer(text):
        deps.append(("org.jetbrains.kotlin", "kotlin-gradle-plugin",
                     m.group(4), "gradle-plugin"))
    if catalog:
        aliases, bundles = catalog
        for g, a, v in _resolve_catalog_refs(text, aliases, bundles):
            deps.append((g, a, v, "gradle-catalog"))
    return deps


def _repositories_blocks(text):
    """Yield (body, inside_publishing) for every `repositories { ... }` block.

    One quote-aware pass over the text tracks which keyword opened each
    brace, so braces inside string literals never break the nesting count
    and nested blocks (maven { url = uri("...") }) stay contained within
    their parent's body. *body* is the text between the braces of every
    block opened by `repositories`; *inside_publishing* reports whether any
    enclosing block was opened by `publishing` — a publishing repository is
    a deployment target, not a dependency source. buildscript repositories
    are dependency repositories (classpath deps resolve from them) and are
    yielded normally. An unterminated block yields the remaining text.
    """
    stack = []      # (block keyword, index just past its `{`)
    blocks = []
    i = 0
    n = len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch == "{":
            m = _GRADLE_BLOCK_NAME_RE.search(text[max(0, i - 64):i + 1])
            stack.append((m.group(1) if m else "", i + 1))
            i += 1
            continue
        if ch == "}" and stack:
            name, start = stack.pop()
            if name == "repositories":
                inside_publishing = any(
                    keyword == "publishing" for keyword, _pos in stack)
                blocks.append((text[start:i], inside_publishing))
        i += 1
    return blocks


def _gradle_repo_urls(text):
    """Artifact-repository URLs declared in dependency `repositories` blocks.

    Matches `url = uri("...")`, `url = "..."`, and `url "..."` in every
    non-publishing `repositories { ... }` block. mavenCentral(),
    mavenLocal() and google() declare no URL and yield nothing. Pure;
    order-stable and deduplicated.
    """
    urls = []
    for body, inside_publishing in _repositories_blocks(text):
        if inside_publishing:
            continue
        for m in _GRADLE_REPO_URL_RE.finditer(body):
            url = m.group(2).strip()
            if url and url not in urls:
                urls.append(url)
    return urls


def parse_gradle_repositories(path):
    """Return the artifact-repository URLs declared in a gradle build file.

    Comments are stripped first (a commented-out repositories block is
    ignored); see :func:`_gradle_repo_urls` for the matched forms and the
    publishing exclusion.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  ! read error on {path}: {exc}", file=sys.stderr)
        return []
    return _gradle_repo_urls(_strip_gradle_comments(text))


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


def parse_version_catalog(path):
    """Parse a libs.versions.toml catalog; return (aliases, bundles).

    aliases: normalized alias -> (group, artifact, version). Aliases with no
    resolvable version (unknown version.ref, no module/group) are skipped.
    bundles: normalized bundle name -> list of normalized aliases. TOML
    support is a best-effort subset: no multi-line arrays, no nested inline
    tables beyond version = { ref = "..." }.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  ! read error on {path}: {exc}", file=sys.stderr)
        return {}, {}

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
    return aliases, norm_bundles


def _resolve_catalog_refs(text, aliases, bundles):
    """Yield (g, a, v) for every resolvable libs.* reference in gradle text.

    libs.versions.* and libs.plugins.* references are ignored; libs.bundles.N
    expands to the bundle's aliases. Unresolvable references are skipped.
    """
    for m in _CATALOG_REF_RE.finditer(text):
        seg = m.group(1).lower()
        if seg.startswith("versions.") or seg.startswith("plugins."):
            continue
        if seg.startswith("bundles."):
            for alias in bundles.get(_norm_alias(seg[len("bundles."):]), []):
                gav = aliases.get(alias)
                if gav:
                    yield gav
            continue
        gav = aliases.get(_norm_alias(seg))
        if gav:
            yield gav


# ---------------------------------------------------------------------------
# package.json parser
# ---------------------------------------------------------------------------

def parse_package_json(path):
    """Parse package.json; return (npm_deps, source_path).

    npm_deps: list of (name, version)
    Engine constraint engines.node, when present, is yielded as ("node", version).
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  ! parse error in {path}: {exc}", file=sys.stderr)
        return []

    deps = []
    engines = data.get("engines") or {}
    if "node" in engines:
        deps.append(("node", _clean_version(engines["node"])))
    for key in ("dependencies", "devDependencies"):
        for name, version in (data.get(key) or {}).items():
            deps.append((name, _clean_version(version)))
    return deps


# ---------------------------------------------------------------------------
# Transitive-resolution output parsers (pure; network-free and tool-free)
#
# Feeds --resolve-transitive: the real sources are parsed, never hand-rolled
# — mvn dependency:list output, gradle eolDumpDeps init-script output, and
# npm package-lock.json (which needs no tool at all).
# ---------------------------------------------------------------------------

# One `mvn dependency:list` line: group:artifact:type:version:scope
# (:classifier), optionally preceded by a "[INFO] "-style log prefix. Every
# field must be non-empty and free of colons/whitespace for the line to
# match; anything else (headers, blank lines, download progress) is ignored.
_MVN_GAV_LINE_RE = re.compile(
    r"^(?:\[[A-Z][A-Z ]*\]\s*)?"
    r"([^:\s]+):([^:\s]+):([^:\s]+):([^:\s]+):([^:\s]+)(?::([^:\s]+))?\s*$"
)


def parse_mvn_dependency_list(text):
    """Parse `mvn dependency:list -DoutputFile=...` text -> [(g, a, v)].

    Matches only strict 5-field (group:artifact:type:version:scope) or
    6-field (...:classifier) lines. Test-scoped entries are dropped (the
    tracker follows runtime classpaths, not test trees); 6-field lines have
    their classifier component stripped (a classifier jar duplicates the
    base artifact); duplicates are deduped keeping first occurrence.
    Header noise ("The following files have been resolved"), empty or
    garbage lines, and lines with empty fields are ignored. Pure,
    order-stable.
    """
    deps = []
    seen = set()
    for raw in text.splitlines():
        m = _MVN_GAV_LINE_RE.match(raw.strip())
        if not m:
            continue
        g, a, _type, v, scope, _classifier = m.groups()
        if scope == "test":
            continue
        if (g, a, v) not in seen:
            seen.add((g, a, v))
            deps.append((g, a, v))
    return deps


# One eolDumpDeps output line: "<configuration>:<group>:<artifact>:<version>".
# Every segment must be non-empty and colon-free; anything else (Gradle
# warnings, "<configuration>:UNRESOLVED" markers, blank lines) is ignored.
_GRADLE_DUMP_LINE_RE = re.compile(r"^[^:\s]+:[^:\s]+:[^:\s]+:[^:\s]+$")


def parse_gradle_dump(text):
    """Parse the eolDumpDeps init-script stdout -> [(g, a, v)].

    Lines have the shape "<config-name>:<group>:<artifact>:<version>"; the
    configuration-name prefix is stripped. Skips lines containing
    "UNRESOLVED" (a configuration whose resolution failed), lines that are
    not 4 colon-separated segments, and entries whose version is missing,
    "NONE" or "null" (case-insensitive; Gradle prints these for
    unresolvable module versions). Duplicates deduped; pure, order-stable.
    """
    deps = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "UNRESOLVED" in line:
            continue
        if not _GRADLE_DUMP_LINE_RE.match(line):
            continue
        _cfg, g, a, v = line.split(":")
        if v.lower() in ("none", "null"):
            continue
        if (g, a, v) not in seen:
            seen.add((g, a, v))
            deps.append((g, a, v))
    return deps


def parse_npm_lockfile(path):
    """Parse a package-lock.json; return [(name, version)].

    Supports lockfileVersion 2/3 (the "packages" mapping keyed by
    "node_modules/..." paths — the package name is the last path segment,
    @scope/pkg keys keep their scope) and lockfileVersion 1 (the legacy
    "dependencies" tree, recursed including nested installs). The "" root
    entry, entries without a usable version, and link:/file: resolved
    entries are skipped — a linked or local path is not a registry version
    to track (so is any ':'-bearing version: npm:/git aliases). Parsed with
    stdlib json; a malformed or unreadable lockfile prints one warning to
    stderr and returns [] (never raises). Order-stable, deduped on
    (name, version).
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"  ! parse error in {path}: {exc}", file=sys.stderr)
        return []
    if not isinstance(data, dict):
        print(f"  ! unexpected package-lock.json shape in {path}", file=sys.stderr)
        return []
    deps = []
    seen = set()

    def add(name, version):
        if (not name or not version or ":" in version
                or version.startswith(("link:", "file:"))):
            return
        if (name, version) not in seen:
            seen.add((name, version))
            deps.append((name, version))

    packages = data.get("packages")
    if isinstance(packages, dict):
        # lockfileVersion 2/3: "packages" keyed by "node_modules/..." paths.
        for key, entry in packages.items():
            if not key or not isinstance(entry, dict):
                continue  # "" is the root entry
            resolved = str(entry.get("resolved") or "")
            if resolved.startswith(("link:", "file:")):
                continue
            segments = [s for s in key.split("/") if s]
            if not segments:
                continue
            if len(segments) >= 2 and segments[-2].startswith("@"):
                name = f"{segments[-2]}/{segments[-1]}"
            else:
                name = segments[-1]
            add(name, entry.get("version"))
    elif isinstance(data.get("dependencies"), dict):
        # lockfileVersion 1: legacy "dependencies" tree, nested installs too.
        def walk(node):
            for name, info in node.items():
                if not isinstance(info, dict):
                    continue
                add(name, info.get("version"))
                nested = info.get("dependencies")
                if isinstance(nested, dict):
                    walk(nested)
        walk(data["dependencies"])
    return deps


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------

def scan_folder(folder):
    """Walk folder; return parsed-results dict keyed by language."""
    folder = Path(folder)
    if not folder.is_dir():
        raise SystemExit(f"Not a directory: {folder}")

    java_deps = []          # list of (group, artifact, version, source_file, kind)
    pom_properties = []     # list of (props_dict, source_file)
    node_deps = []          # list of (name, version, source_file)
    declared_repos = []     # declared artifact-repo URLs (order-stable, deduped)
    files_seen = []

    # Gradle version catalogs first, so build scripts can resolve their
    # libs.* references against them (multiple catalogs merge, last wins).
    catalog_aliases = {}
    catalog_bundles = {}
    for p in sorted(folder.rglob("libs.versions.toml")):
        files_seen.append(str(p))
        aliases, bundles = parse_version_catalog(p)
        catalog_aliases.update(aliases)
        catalog_bundles.update(bundles)
    catalog = ((catalog_aliases, catalog_bundles)
               if catalog_aliases or catalog_bundles else None)

    for p in sorted(folder.rglob("pom*.xml")):
        files_seen.append(str(p))
        deps, props, repos = parse_pom(p)
        for g, a, v, kind in deps:
            java_deps.append((g, a, v, str(p), kind))
        if props:
            pom_properties.append((props, str(p)))
        for url in repos:
            if url not in declared_repos:
                declared_repos.append(url)

    for pattern in ("*.gradle.kts", "build.gradle"):
        for p in sorted(folder.rglob(pattern)):
            files_seen.append(str(p))
            for g, a, v, kind in parse_gradle(p, catalog):
                java_deps.append((g, a, v, str(p), kind))
            for url in parse_gradle_repositories(p):
                if url not in declared_repos:
                    declared_repos.append(url)

    for p in sorted(folder.rglob("package.json")):
        if "node_modules" in p.parts:
            continue
        files_seen.append(str(p))
        for name, v in parse_package_json(p):
            node_deps.append((name, v, str(p)))

    return {
        "java":           java_deps,
        "pom_properties": pom_properties,
        "node":           node_deps,
        "repositories":   declared_repos,
        "files":          files_seen,
    }


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

def _entry_key(entry):
    """Stable de-dup key across entry shapes."""
    src = entry.get("source", "endoflife_date")
    return (
        src,
        entry.get("product"),
        entry.get("group"), entry.get("artifact"),
        entry.get("sdk"),   entry.get("major"),
        entry.get("version"),
    )


def _discovered_record(decl, fname, kind, outcome):
    """One _discovered_dependencies entry: a parsed declaration + outcome."""
    return {"decl": decl, "file": fname, "kind": kind, "outcome": outcome}


def _discovered_summary(records):
    """One-line _comment tally of the _discovered_dependencies outcomes."""
    def count(prefix):
        return sum(1 for r in records if r["outcome"].startswith(prefix))
    return (f"Declarations discovered: {len(records)} "
            f"(tracked {count('tracked: ')}, duplicates {count('duplicate-of: ')}, "
            f"skipped {count('skipped: ')}, unmapped {count('unmapped: ')}) "
            "- see _discovered_dependencies for the complete picture.")


def generate_config(scan, project_name):
    """Build an EOL config dict from scan results.

    products is the deduped runnable set (first declaration wins); every
    parsed declaration is additionally recorded in
    config["_discovered_dependencies"] with decl/file/kind/outcome, so the
    config carries the complete picture of the scanned manifests.
    """
    products = []
    seen_keys = set()
    kept_by_key = {}
    skipped_npm = []
    records = []

    def add(entry, comment=None):
        """Append entry unless its key was seen; return (added, dup_label).

        On a duplicate, returns the kept (first) entry's label so the
        caller can record 'duplicate-of: <label>'.
        """
        if entry is None:
            return False, None
        if comment:
            entry.setdefault("_comment", comment)
        key = _entry_key(entry)
        if key in seen_keys:
            kept = kept_by_key.get(key) or {}
            return False, kept.get("label")
        seen_keys.add(key)
        kept_by_key[key] = entry
        products.append(entry)
        return True, None

    # --- POM property-driven platform versions --------------------------------
    if scan["pom_properties"]:
        products.append({"_section": "=== Platforms (from POM properties) ==="})
        for props, src in scan["pom_properties"]:
            for prop_name, mapper in _POM_PROPERTY_MAPPINGS.items():
                if prop_name in props:
                    v = props[prop_name]
                    fname = os.path.basename(src)
                    decl = f"{prop_name}={v}"
                    if "$" in (v or ""):
                        # A property value that is itself an unresolved
                        # placeholder (e.g. <tomcat.version>${undefined.prop}
                        # </tomcat.version>) resolves nowhere on any registry;
                        # mapping it would fabricate a phantom tracker row
                        # (probed: "Apache Tomcat ${undefined.prop}"). Skip it
                        # like $-placeholder versions in the dependency path.
                        records.append(_discovered_record(
                            decl, fname, "property",
                            "skipped: unresolved property placeholder"))
                        continue
                    entry = mapper(v)
                    if entry is None:
                        records.append(_discovered_record(
                            decl, fname, "property", "skipped: unmapped property"))
                        continue
                    added, dup_label = add(
                        entry,
                        comment=f"From {fname} (<{prop_name}>{v}</{prop_name}>)")
                    if added:
                        records.append(_discovered_record(
                            decl, fname, "property", f"tracked: {entry['label']}"))
                    else:
                        records.append(_discovered_record(
                            decl, fname, "property", f"duplicate-of: {dup_label}"))

    # --- Java/Maven dependencies ---------------------------------------------
    if scan["java"]:
        added_section = False
        for g, a, v, src, kind in scan["java"]:
            decl = f"{g}:{a}:{v or ''}"
            fname = os.path.basename(src)
            if kind == "unversioned-dep" or not v:
                # No version to check (parent/BOM-managed); skipped for now.
                records.append(_discovered_record(
                    decl, fname, kind,
                    "skipped: no version (parent/BOM-managed)"))
                continue
            if kind in ("test-scope-dep", "provided-scope-dep", "system-scope-dep"):
                scope = kind[:-len("-scope-dep")]
                records.append(_discovered_record(
                    decl, fname, kind, f"skipped: {scope} scope"))
                continue
            entry, skip_reason = _map_java_dep_with_reason(g, a, v)
            if entry is None:
                records.append(_discovered_record(
                    decl, fname, kind, f"skipped: {skip_reason}"))
                continue
            if not added_section:
                products.append({"_section": "=== Java dependencies ==="})
                added_section = True
            comment = f"From {fname} ({g}:{a}:{v})"
            added, dup_label = add(entry, comment=comment)
            if added:
                records.append(_discovered_record(
                    decl, fname, kind, f"tracked: {entry['label']}"))
            else:
                records.append(_discovered_record(
                    decl, fname, kind, f"duplicate-of: {dup_label}"))

    # --- npm dependencies ----------------------------------------------------
    if scan["node"]:
        added_section = False
        for name, v, src in scan["node"]:
            decl = f"{name}@{v or ''}"
            fname = os.path.basename(src)
            entry = _map_npm_dep(name, v)
            if entry is None:
                # Track unmapped for the user's review
                if name not in {"react-dom"}:  # known-no-mapping
                    skipped_npm.append({"name": name, "version": v, "source": fname})
                outcome = ("skipped: vue version spec with no matching published cycle"
                           if name == "vue"
                           else "unmapped: see _skipped_npm_packages")
                records.append(_discovered_record(decl, fname, "npm", outcome))
                continue
            if not added_section:
                products.append({"_section": "=== npm dependencies ==="})
                added_section = True
            added, dup_label = add(entry, comment=f"From {fname} ({name}@{v})")
            if added:
                records.append(_discovered_record(
                    decl, fname, "npm", f"tracked: {entry['label']}"))
            else:
                records.append(_discovered_record(
                    decl, fname, "npm", f"duplicate-of: {dup_label}"))

    # --- Infer transitive platforms from detected ones -----------------------
    # Spring Boot's release train pairs each Boot minor with a Spring Security
    # minor: Boot 3.x.y -> Security 6.x.y, Boot 2.x.y -> Security 5.x.y.
    # Add the inferred entry only when not already present.
    inferred_section_added = False
    spring_boot = next(
        (p for p in products if p.get("product") == "spring-boot"),
        None,
    )
    if spring_boot:
        sb_v = spring_boot["version"]  # "3.5"
        sb_parts = sb_v.split(".")
        if len(sb_parts) == 2 and sb_parts[0] in ("2", "3") and not any(
            p.get("product") == "spring-security" for p in products
        ):
            ss_major = "6" if sb_parts[0] == "3" else "5"
            ss_v = f"{ss_major}.{sb_parts[1]}"
            if not inferred_section_added:
                products.append({"_section": "=== Inferred from Spring Boot release train ==="})
                inferred_section_added = True
            inferred_entry = _eol_entry("spring-security", ss_v, f"Spring Security {ss_v}")
            inferred_entry["_comment"] = (
                f"Auto-derived from Spring Boot {sb_v} (release train pairing). "
                f"Spring Security version is not explicitly pinned in the POMs."
            )
            add(inferred_entry)

    # --- Build config --------------------------------------------------------
    maven_repos = scan.get("repositories") or []
    config = {
        "_comment": [
            f"EOL config for the {project_name} project.",
            f"Auto-generated by generate_config.py on {date.today()}.",
            f"Files scanned: {len(scan['files'])}.",
            f"Tracker entries: {sum(1 for p in products if not p.get('_section'))}.",
            *([_discovered_summary(records)] if records else []),
            "",
            "REVIEW THIS FILE BEFORE DEPLOYING — auto-mapping is best-effort.",
            "Common things to check:",
            "  - Java distribution (amazon-corretto vs eclipse-temurin vs oracle-jdk)",
            "  - 'Latest patch not found' warnings indicate version pins not on Maven Central",
            *([
                "  - maven_repositories lists the artifact repos declared in the manifests;",
                "    at load the runtime offers them (first 8, config order) as fallback",
                "    lookups to maven_central entries without an explicit 'repository'",
            ] if maven_repos else []),
            "  - Skipped npm packages (see _skipped_npm_packages below) need manual entries;",
            "    every parsed declaration and its outcome is in _discovered_dependencies",
            "",
            "Run with:  python lambda_function.py " + f"eol_config.{project_name}.json",
        ],
        "alert_thresholds_days": [30, 60, 90],
        "notify_when": "always",
        "notifications": [
            {"type": "console"},
            {"type": "html_file", "path": f"eol_report_{project_name}.html"},
            {"type": "sns", "_comment": "topic_arn supplied via EventBridge event input (set by Terraform)"},
        ],
        "products": products,
    }

    if maven_repos:
        # Config-level, not per-entry: handler.py stamps this list onto
        # maven_central entries lacking an explicit 'repository' at load
        # time (single source of truth, capped there).
        config["maven_repositories"] = list(maven_repos)
    if skipped_npm:
        config["_skipped_npm_packages"] = skipped_npm
    if records:
        config["_discovered_dependencies"] = records

    return config


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:", 1)[1] if "Usage:" in __doc__ else "",
    )
    parser.add_argument("folder", help="Folder to scan (recursively) for dependency files")
    parser.add_argument("--name", help="Project name (default: folder basename)", default=None)
    parser.add_argument("--output", help="Output file (default: eol_config.<name>.json)", default=None)
    args = parser.parse_args()

    folder = args.folder
    project_name = args.name or os.path.basename(os.path.normpath(folder)).replace(" ", "-").lower()
    output = args.output or f"eol_config.{project_name}.json"

    print(f"Scanning {folder!r}...")
    scan = scan_folder(folder)

    print(f"  Files scanned        : {len(scan['files'])}")
    print(f"  Java/Maven dep decls : {len(scan['java'])}")
    print(f"  POM property files   : {len(scan['pom_properties'])}")
    print(f"  npm dep decls        : {len(scan['node'])}")
    print(f"  Repositories declared: {len(scan['repositories'])}")

    config = generate_config(scan, project_name)
    real_products = [p for p in config["products"] if not p.get("_section")]

    with open(output, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    print(f"\nWrote {output}")
    print(f"  Tracker entries     : {len(real_products)}")
    skipped = config.get("_skipped_npm_packages") or []
    if skipped:
        print(f"  Unmapped npm pkgs   : {len(skipped)} (listed in _skipped_npm_packages — review)")

    print(f"\nNext: review the file, then run")
    print(f"  python lambda_function.py {output}")


if __name__ == "__main__":
    main()
