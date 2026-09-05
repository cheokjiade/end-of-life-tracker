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
    v = re.sub(r"^[vV]", "", v.strip())
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
# Returns None when no lifecycle mapping exists; exact versions then fall
# back to the npm registry provider in config_writer.
# ---------------------------------------------------------------------------


def _vue_entry(version):
    """vue -> endoflife.date entry, or None when the spec must be skipped.

    endoflife.date's vue cycles are major.minor ('3.5', '3.4', '3.3',
    '2.7', ... '2.0') plus the bare-major cycle '1'; there are no cycles
    '3', '2' or '1.0' (verified live against /api/vue.json). A bare-major
    spec ('^3', '3', '2') must therefore not be guessed into a cycle -
    return None so the package stays unmapped - while a numeric 1.x.y pin
    ('1.0', '1.2.3') maps to the bare-major cycle '1' (label 'Vue 1'),
    since no 1.x minor cycles exist. Both the major and minor segments
    must be numeric before any mapping: a range-style spec ('3.x', '3.X',
    '2.x', '1.x', '1.x.y') has no matching cycle at all, so skipping it is
    safer than a doomed row. (Unlike the root generator, a v-prefixed spec
    such as 'v3.5.3' does map here: this package's _clean_version strips a
    leading 'v', so the major segment is numeric by the time it arrives.)
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
    # react-dom follows React's release lifecycle. Mapping both to the same
    # key lets config de-duplication retain one tracker row while merging the
    # provenance of every discovered package.
    "react-dom":                   lambda v: _eol_entry("react", _major(v),
                                                        f"React {_major(v)}"),
    "vue":                         _vue_entry,
    "@angular/core":               lambda v: _eol_entry("angular", _major(v),
                                                        f"Angular {_major(v)}"),
    # endoflife.date nextjs cycles are major-only ('16', '15', ...): a
    # major.minor cycle string makes every lookup fail with
    # "Cycle '14.2' not found".
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


def _map_java_dep(group, artifact, version):
    # Skip artifacts that won't resolve on any public registry: SNAPSHOT
    # builds (in-flight project versions), internal coordinate prefixes,
    # and ${unresolved.property} placeholders that slipped through.
    if (
        version.lower().endswith("-snapshot")
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
# Container image repository -> tracker entry mappings
#
# Image names are normalized registry-free repository paths ("python",
# "dotnet/aspnet"; see parsers/docker.py normalize_image_name). Only
# tags that yield a valid endoflife.date cycle string map to a product;
# everything else stays in the inventory. Slugs verified against
# endoflife.date on 2026-08-29.
# ---------------------------------------------------------------------------

def _tag_numeric_parts(tag):
    """Leading numeric dot components of a tag: '3.12.4-slim' -> ['3','12','4']."""
    base = re.split(r"[-+]", tag, maxsplit=1)[0]
    parts = []
    for piece in base.split("."):
        if piece.isdigit():
            parts.append(piece)
        else:
            break
    return parts


def _cycle_major(tag):
    """'20.15.1-alpine' -> '20'; None when the tag has no leading number."""
    parts = _tag_numeric_parts(tag)
    return parts[0] if parts else None


def _cycle_major_minor(tag):
    """'3.12.4-slim' -> '3.12', '24.04.1' -> '24.04'; None under 2 parts."""
    parts = _tag_numeric_parts(tag)
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else None


def _image_entry(product, cycle, label):
    if cycle is None:
        return None
    return _eol_entry(product, cycle, label)


_IMAGE_MAPPINGS = {
    "python":         lambda tag: _image_entry("python", _cycle_major_minor(tag), f"Python {_cycle_major_minor(tag)}"),
    "node":           lambda tag: _image_entry("nodejs", _cycle_major(tag), f"Node.js {_cycle_major(tag)}"),
    "golang":         lambda tag: _image_entry("golang", _cycle_major_minor(tag), f"Go {_cycle_major_minor(tag)}"),
    "mcr.microsoft.com/dotnet/runtime": lambda tag: _image_entry("dotnet", _cycle_major(tag), f".NET {_cycle_major(tag)}"),
    "mcr.microsoft.com/dotnet/aspnet":  lambda tag: _image_entry("dotnet", _cycle_major(tag), f".NET {_cycle_major(tag)}"),
    "mcr.microsoft.com/dotnet/sdk":     lambda tag: _image_entry("dotnet", _cycle_major(tag), f".NET {_cycle_major(tag)}"),
    "ubuntu":         lambda tag: _image_entry("ubuntu", _cycle_major_minor(tag), f"Ubuntu {_cycle_major_minor(tag)}"),
    "debian":         lambda tag: _image_entry("debian", _cycle_major(tag), f"Debian {_cycle_major(tag)}"),
    "alpine":         lambda tag: _image_entry("alpine", _cycle_major_minor(tag), f"Alpine {_cycle_major_minor(tag)}"),
    "postgres":       lambda tag: _image_entry("postgresql", _cycle_major(tag), f"PostgreSQL {_cycle_major(tag)}"),
    "mysql":          lambda tag: _image_entry("mysql", _cycle_major_minor(tag), f"MySQL {_cycle_major_minor(tag)}"),
    "redis":          lambda tag: _image_entry("redis", _cycle_major_minor(tag), f"Redis {_cycle_major_minor(tag)}"),
    "nginx":          lambda tag: _image_entry("nginx", _cycle_major_minor(tag), f"nginx {_cycle_major_minor(tag)}"),
}


def _map_image_dep(name, tag):
    """Map a normalized image name + tag to a tracker entry, or None.

    None means no lifecycle mapping: either the repository is unknown
    or the tag provides no valid release cycle. The record stays in
    the inventory either way.
    """
    if not tag:
        return None
    handler = _IMAGE_MAPPINGS.get((name or "").lower())
    return handler(tag) if handler else None


def _image_skip_reason(name, tag):
    """Why a container record has no tracker entry (ASCII, stable)."""
    if (name or "").lower() not in _IMAGE_MAPPINGS:
        return "no endoflife.date mapping for this image"
    return "image tag provides no endoflife.date cycle"


# ---------------------------------------------------------------------------
# Registry-provider entry builders
#
# Entry shapes follow the provider contracts in eoltracker/parsers/ (see
# docs/plans/2026-08-28-project-dependency-inventory.md): release recency
# and unsafe-release signals, never EOL dates.
# ---------------------------------------------------------------------------

def _pypi_entry(package, version):
    return {"source": "pypi_registry", "package": package,
            "version": version, "label": f"{package} {version}"}


def _npm_registry_entry(package, version):
    return {"source": "npm_registry", "package": package,
            "version": version, "label": f"{package} {version}"}


def _nuget_entry(package, version):
    return {"source": "nuget_registry", "package": package,
            "version": version, "label": f"{package} {version}"}


def _go_proxy_entry(module, version):
    """Go module proxy entry; proxy versions carry the leading 'v'."""
    v = str(version)
    if not v.startswith("v"):
        v = f"v{v}"
    return {"source": "go_proxy", "module": module,
            "version": v, "label": f"{module} {v}"}


def _dotnet_runtime_cycle(version):
    """endoflife.date dotnet cycle for a runtime/SDK version, or None.

    .NET 5 and newer use major-only cycles ("9.0.100" -> "9"); the
    netcoreapp 3.x cycles are major.minor ("3.1.400" -> "3.1"). .NET
    Framework and netstandard targets have no endoflife.date cycle and
    yield None, so they stay in the inventory instead of becoming a
    broken tracker row. Cycles verified against the live API on
    2026-08-29.
    """
    if not version:
        return None
    parts = str(version).split(".")
    major_minor = ".".join(parts[:2])
    if major_minor in ("3.1", "3.0"):
        return major_minor
    if parts[0].isdigit() and int(parts[0]) >= 5:
        return parts[0]
    return None
