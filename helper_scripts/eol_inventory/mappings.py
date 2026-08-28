"""Version helpers and provider mapping tables.

Moved verbatim from the original root generate_config.py.
"""

import re


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
