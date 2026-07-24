"""Generate an EOL tracker config from a project's dependency files.

Scans a folder for Maven, Gradle, and Node manifests; emits an
eol_config.<project>.json file suitable for use with lambda_function.py.

Supported formats:
    pom.xml             — Maven (multi-module supported via rglob)
    *.gradle.kts        — Gradle Kotlin DSL
    build.gradle        — Gradle Groovy DSL (same regex patterns)
    package.json        — Node (skips node_modules)

Mapping strategy:
    Java deps   -> known group:artifact patterns map to specific tracker
                   providers (endoflife.date Spring Boot/Framework/Tomcat/
                   Log4j, jackson_lifecycle, aws_sdk_lifecycle); everything
                   else falls back to maven_central staleness.
    POM props   -> known names (tomcat.version, netty.version, logback.version,
                   quartz.version, kotlin.version, java.version) produce the
                   matching tracker entry — catches transitively-managed
                   platforms not declared as explicit <dependency>s.
    Node deps   -> known package names map to endoflife.date entries
                   (react, vue, angular, next, nuxt, typescript, node,
                   express); unmapped packages are listed in
                   _skipped_npm_packages for manual review.

Usage:
    python generate_config.py <folder> [--name PROJECT] [--output FILE]

Examples:
    python generate_config.py "project-b" --name b
    python generate_config.py ssg-frontend --name frontend
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
            "source":  "jackson_lifecycle",
            "version": _major_minor(v),
            "label":   f"Jackson {_major_minor(v)}",
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
        lambda g, a: g.startswith("org.jetbrains.kotlin"),
        lambda g, a, v: _eol_entry("kotlin", _major_minor(v),
                                   f"Kotlin {_major_minor(v)}"),
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

_NPM_MAPPINGS = {
    "react":                       lambda v: _eol_entry("react", _major(v),
                                                        f"React {_major(v)}"),
    "react-dom":                   lambda v: None,           # tracked via 'react'
    "vue":                         lambda v: _eol_entry("vue", _major(v),
                                                        f"Vue {_major(v)}"),
    "@angular/core":               lambda v: _eol_entry("angular", _major(v),
                                                        f"Angular {_major(v)}"),
    "next":                        lambda v: _eol_entry("nextjs", _major_minor(v),
                                                        f"Next.js {_major_minor(v)}"),
    "nuxt":                        lambda v: _eol_entry("nuxt", _major(v),
                                                        f"Nuxt {_major(v)}"),
    "typescript":                  lambda v: _eol_entry("typescript", _major_minor(v),
                                                        f"TypeScript {_major_minor(v)}"),
    "node":                        lambda v: _eol_entry("nodejs", _major(v),
                                                        f"Node.js {_major(v)}"),
    "express":                     lambda v: _eol_entry("express", _major(v),
                                                        f"Express {_major(v)}"),
    "ckeditor":                    lambda v: _eol_entry("ckeditor", _major(v),
                                                        f"CKEditor {_major(v)}"),
    "@ckeditor/ckeditor5-core":    lambda v: _eol_entry("ckeditor", "5", "CKEditor 5"),
}


def _map_java_dep(group, artifact, version):
    # Skip artifacts that won't resolve on any public registry: SNAPSHOT
    # builds (in-flight project versions), internal coordinate prefixes,
    # and ${unresolved.property} placeholders that slipped through.
    if (
        version.endswith("-SNAPSHOT")
        or group.startswith("internal.")
        or "${" in version
    ):
        return None
    for pred, handler in _JAVA_MAPPINGS:
        if pred(group, artifact):
            return handler(group, artifact, version)
    return None


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
    files_seen = []

    for p in sorted(folder.rglob("pom*.xml")):
        files_seen.append(str(p))
        deps, props = parse_pom(p)
        for g, a, v, kind in deps:
            java_deps.append((g, a, v, str(p), kind))
        if props:
            pom_properties.append((props, str(p)))

    for pattern in ("*.gradle.kts", "build.gradle"):
        for p in sorted(folder.rglob(pattern)):
            files_seen.append(str(p))
            for g, a, v, kind in parse_gradle(p):
                java_deps.append((g, a, v, str(p), kind))

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


def generate_config(scan, project_name):
    """Build an EOL config dict from scan results."""
    products = []
    seen_keys = set()
    skipped_npm = []

    def add(entry, comment=None):
        if entry is None:
            return False
        if comment:
            entry.setdefault("_comment", comment)
        key = _entry_key(entry)
        if key in seen_keys:
            return False
        seen_keys.add(key)
        products.append(entry)
        return True

    # --- POM property-driven platform versions --------------------------------
    if scan["pom_properties"]:
        products.append({"_section": "=== Platforms (from POM properties) ==="})
        for props, src in scan["pom_properties"]:
            for prop_name, mapper in _POM_PROPERTY_MAPPINGS.items():
                if prop_name in props:
                    v = props[prop_name]
                    entry = mapper(v)
                    add(entry, comment=f"From {os.path.basename(src)} (<{prop_name}>{v}</{prop_name}>)")

    # --- Java/Maven dependencies ---------------------------------------------
    if scan["java"]:
        added_section = False
        for g, a, v, src, kind in scan["java"]:
            entry = _map_java_dep(g, a, v)
            if entry is None:
                continue
            if not added_section:
                products.append({"_section": "=== Java dependencies ==="})
                added_section = True
            comment = f"From {os.path.basename(src)} ({g}:{a}:{v})"
            add(entry, comment=comment)

    # --- npm dependencies ----------------------------------------------------
    if scan["node"]:
        added_section = False
        for name, v, src in scan["node"]:
            entry = _map_npm_dep(name, v)
            if entry is None:
                # Track unmapped for the user's review
                if name not in {"react-dom"}:  # known-no-mapping
                    skipped_npm.append({"name": name, "version": v, "source": os.path.basename(src)})
                continue
            if not added_section:
                products.append({"_section": "=== npm dependencies ==="})
                added_section = True
            add(entry, comment=f"From {os.path.basename(src)} ({name}@{v})")

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
    config = {
        "_comment": [
            f"EOL config for the {project_name} project.",
            f"Auto-generated by generate_config.py on {date.today()}.",
            f"Files scanned: {len(scan['files'])}.",
            f"Tracker entries: {sum(1 for p in products if not p.get('_section'))}.",
            "",
            "REVIEW THIS FILE BEFORE DEPLOYING — auto-mapping is best-effort.",
            "Common things to check:",
            "  - Java distribution (amazon-corretto vs eclipse-temurin vs oracle-jdk)",
            "  - 'Latest patch not found' warnings indicate version pins not on Maven Central",
            "  - Skipped npm packages (see _skipped_npm_packages below) need manual entries",
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

    if skipped_npm:
        config["_skipped_npm_packages"] = skipped_npm

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
