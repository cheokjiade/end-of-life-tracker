"""Normalized dependency records, provenance locations, and warnings.

Every manifest parser emits records in the normalized discovery model
before config mapping (see docs/plans/2026-08-28-project-dependency-inventory.md):

    {
      "ecosystem": "java",
      "name": "io.netty:netty-codec-http",
      "version": "4.1.111.Final",
      "scope": "runtime",
      "direct": true,
      "kind": "dependency",
      "group": "io.netty",
      "artifact": "netty-codec-http",
      "found_in": [
        {"path": "services/api/pom.xml", "manifest": "maven",
         "line": 42, "locator": "dependency:io.netty:netty-codec-http"}
      ]
    }

Rules enforced here:
    - paths are relative to the scan root and use "/" separators;
    - duplicate records merge all distinct provenance locations;
    - sorting is deterministic across platforms;
    - unresolved ranges and dynamic expressions become warnings, never
      silently rewritten exact versions.
"""

import fnmatch

SCHEMA_VERSION = 1
GENERATOR_VERSION = "1.0.0"

# Directories never descended into during discovery (source-control
# metadata, dependency caches, virtualenvs, compiled/generated output).
DEFAULT_EXCLUDED_DIRS = (
    ".git", "node_modules", ".venv", "venv", "vendor",
    "target", "bin", "obj", "dist", "build",
)

# Safety guards against accidental huge scans.
MAX_FILE_BYTES = 2_000_000
MAX_FILES = 5_000


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def new_record(ecosystem, name, version=None, scope="runtime", direct=True,
               kind="dependency", group=None, artifact=None, version_spec=None):
    """Build one normalized dependency record with empty provenance."""
    return {
        "ecosystem": ecosystem,
        "name": name,
        "version": version,
        "version_spec": version_spec,
        "scope": scope,
        "direct": bool(direct),
        "kind": kind,
        "group": group,
        "artifact": artifact,
        "found_in": [],
    }


def format_location(path, manifest, line=None, locator=None):
    """One provenance location dict; optional keys are omitted when unknown."""
    loc = {"path": path, "manifest": manifest}
    if line is not None:
        loc["line"] = int(line)
    if locator:
        loc["locator"] = locator
    return loc


def add_location(record, path, manifest, line=None, locator=None):
    record["found_in"].append(
        format_location(path, manifest, line=line, locator=locator))


def sort_locations(locations):
    """Deterministic cross-platform ordering of provenance locations."""
    return sorted(
        locations,
        key=lambda loc: (loc["path"], loc["manifest"],
                         loc.get("line", 0), loc.get("locator", "")),
    )


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

def new_warning(category, path, message):
    return {"category": category, "path": path, "message": message}


def sort_warnings(warnings):
    """Deterministic ordering; duplicate warnings collapse to one."""
    unique = {(
        w["category"], w["path"], w["message"],
    ): w for w in warnings}
    return [unique[k] for k in sorted(unique)]


# ---------------------------------------------------------------------------
# Exclusion matching (gitignore-lite, documented conservatively)
# ---------------------------------------------------------------------------

def load_ignore_patterns(root, warnings):
    """Read .eolignore from the scan root, if present.

    Returns a list of patterns. One pattern per line; blank lines and
    #-comments are skipped. An unreadable .eolignore yields a warning,
    never a crash.
    """
    ignore_path = root / ".eolignore"
    if not ignore_path.is_file():
        return []
    try:
        text = ignore_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(new_warning(
            "unreadable_ignore", ".eolignore", f"could not read .eolignore: {exc}"))
        return []
    patterns = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.rstrip("/"))
    return patterns


def is_excluded(rel_posix, patterns):
    """True when rel_posix matches any user exclusion pattern.

    A pattern containing "/" matches the full relative path; a bare
    pattern matches any single path segment (so `dist` excludes every
    dist directory, and `*.log` every log file).
    """
    if not rel_posix:
        return False
    segments = rel_posix.split("/")
    for pattern in patterns:
        if "/" in pattern:
            if _fnmatch(rel_posix, pattern):
                return True
        elif any(_fnmatch(seg, pattern) for seg in segments):
            return True
    return False


def _fnmatch(name, pattern):
    return fnmatch.fnmatch(name, pattern)
