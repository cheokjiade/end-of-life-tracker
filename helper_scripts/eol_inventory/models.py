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

import codecs
import fnmatch
import os
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

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
MAX_PARSE_DEPTH = 64


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


def load_safe_xml(path, rel_path, what):
    """Return ``(root, warning)`` for bounded XML without DTD/entities.

    Maven POM and MSBuild project formats do not require document type
    declarations. Rejecting them before ElementTree parses the document avoids
    entity-expansion attacks while keeping malformed inputs visible as scan
    warnings.
    """
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return None, new_warning(
            "unreadable_file", rel_path, f"could not read {what}: {exc}")
    if len(raw) > MAX_FILE_BYTES:
        return None, new_warning(
            "oversize_input", rel_path,
            f"file exceeds {MAX_FILE_BYTES} byte limit; skipped")
    forbidden, encoding_error = _xml_declaration_status(raw)
    if encoding_error:
        return None, new_warning(
            "parse_error", rel_path,
            f"{what} has an invalid {encoding_error} encoding")
    if forbidden:
        return None, new_warning(
            "parse_error", rel_path,
            f"{what} contains a forbidden DTD/entity declaration")
    try:
        return ET.fromstring(raw), None
    except ET.ParseError as exc:
        return None, new_warning(
            "parse_error", rel_path, f"{what} parse error: {exc}")


def _xml_declaration_status(raw):
    """Return ``(forbidden, encoding_error)`` across XML byte encodings."""
    byte_pattern = re.compile(
        br"<!\s*(?:DOCTYPE|ENTITY)\b", flags=re.IGNORECASE)
    if byte_pattern.search(raw):
        return True, None

    encoding = None
    text = None
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif b"\x00" in raw:
        # BOM-less XML may begin with a declaration, a document type, the root
        # element, or whitespace. Try the bounded set of encodings ElementTree
        # accepts and require the decoded document to begin like XML. Checking
        # UTF-32 first prevents its byte pattern being mistaken for UTF-16.
        for candidate in (
                "utf-32-le", "utf-32-be", "utf-16-le", "utf-16-be"):
            try:
                decoded = raw.decode(candidate)
            except UnicodeDecodeError:
                continue
            if decoded.lstrip("\ufeff \t\r\n").startswith("<"):
                encoding = candidate
                text = decoded
                break
        if encoding is None:
            return False, "XML"
    else:
        return False, None
    if text is None:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            return False, encoding
    forbidden = re.search(
        r"<!\s*(?:DOCTYPE|ENTITY)\b", text, flags=re.IGNORECASE)
    return bool(forbidden), None


# ---------------------------------------------------------------------------
# Exclusion matching (gitignore-lite, documented conservatively)
# ---------------------------------------------------------------------------

# Only link redirection reparse tags are rejected outright. Other reparse
# tags (OneDrive cloud placeholders, ProjFS/GVFS placeholders, WOF
# compaction) annotate readable plain files that resolve to themselves;
# those are left to the realpath containment check below.
_LINK_REPARSE_TAGS = frozenset((
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
))


def _is_link_or_reparse(candidate):
    """True for symlinks and Windows junctions/link reparse points.

    Non-link reparse tags are not rejected here: they fall through to
    the realpath containment check, which still rejects genuine
    redirections while accepting readable placeholders that resolve to
    themselves.
    """
    if candidate.is_symlink():
        return True
    try:
        st = candidate.lstat()
    except OSError:
        return False
    if getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS:
        return True
    return _resolves_away_from_parent(candidate)


def _resolves_away_from_parent(candidate):
    """True when realpath(candidate) is not realpath(parent) + basename.

    Catches links that is_symlink()/lstat() cannot report (junctions on
    some Python versions). Anchored on the resolved parent so a link in
    the scan root path itself does not cause false rejections.
    """
    try:
        pre_resolve = os.path.abspath(candidate)
        expected = os.path.join(
            os.path.realpath(os.path.dirname(pre_resolve)),
            os.path.basename(pre_resolve))
        return os.path.normcase(
            os.path.realpath(pre_resolve)) != os.path.normcase(expected)
    except (OSError, ValueError):
        return False


def load_ignore_patterns(root, warnings):
    """Read .eolignore from the scan root, if present.

    Returns a list of patterns. One pattern per line; blank lines and
    #-comments are skipped. The entry must be a plain file inside the
    scan root: a symlink/junction/reparse point named .eolignore is
    rejected, the read is bounded by MAX_FILE_BYTES, and every
    rejection or read failure yields a warning, never a crash.
    """
    ignore_path = root / ".eolignore"
    if _is_link_or_reparse(ignore_path):
        warnings.append(new_warning(
            "escaped_symlink", ".eolignore",
            ".eolignore is a symlink/junction/reparse point; not followed"))
        return []
    if not ignore_path.is_file():
        return []
    try:
        resolved = ignore_path.resolve()
        resolved.relative_to(Path(root).resolve())
    except (OSError, ValueError):
        warnings.append(new_warning(
            "escaped_symlink", ".eolignore",
            ".eolignore resolves outside the scan root; skipped"))
        return []
    try:
        with open(resolved, "rb") as stream:
            payload = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        warnings.append(new_warning(
            "unreadable_ignore", ".eolignore", f"could not read .eolignore: {exc}"))
        return []
    if len(payload) > MAX_FILE_BYTES:
        warnings.append(new_warning(
            "oversize_input", ".eolignore",
            f"file exceeds {MAX_FILE_BYTES} byte limit; skipped"))
        return []
    text = payload.decode("utf-8", errors="replace")
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
    # fnmatch() applies os.path.normcase and is therefore case-insensitive on
    # Windows but case-sensitive on Linux. Config scans must be deterministic.
    return fnmatch.fnmatchcase(name, pattern)


def scan_root_for(path, rel_path):
    """Derive the lexical scan root from an absolute path + relative path.

    Use the lexical path rather than resolving the manifest symlink: resolving
    first can move the apparent root and weaken include containment checks.
    """
    root = Path(path).absolute().parent
    for _ in range(max(0, len(str(rel_path).split("/")) - 1)):
        root = root.parent
    return root.resolve()


def guarded_local_file(path, root, rel_path):
    """Return (resolved_path, warning) for a bounded in-root sidecar file."""
    candidate = Path(path)
    if not candidate.exists():
        return None, None
    try:
        resolved = candidate.resolve()
        resolved.relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return None, new_warning(
            "escaped_symlink", rel_path,
            "sidecar/include target lies outside the scan root; skipped")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return None, new_warning(
            "unreadable_file", rel_path, f"could not stat file: {exc}")
    if size > MAX_FILE_BYTES:
        return None, new_warning(
            "oversize_input", rel_path,
            f"file exceeds {MAX_FILE_BYTES} byte limit ({size} bytes); skipped")
    return resolved, None
