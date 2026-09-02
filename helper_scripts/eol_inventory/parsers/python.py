"""Python manifest parsers (normalized records).

Pure stdlib; never runs pip/poetry/python and never touches the network.
Python 3.10 compatible, so pyproject.toml is read with a conservative
hand-written TOML-subset scanner (no tomllib) -- anything it does not
understand stops parsing with a "toml_unsupported" warning instead of
producing guessed records.

Covered inputs:
    - requirements*.txt   exact pins, environment markers, extras,
                          editable/direct-URL/local-path declarations,
                          options, and recursive includes (followed only
                          while they stay inside the scan root)
    - pyproject.toml      PEP 621 [project] tables and common Poetry
                          dependency tables
    - Pipfile             direct declarations, enriched by a sibling lock
    - Pipfile.lock        the resolved graph (records carry direct=False,
                          mirroring how the go parser emits indirect requires)
    - .python-version / runtime.txt   Python runtime evidence

Version policy: exact pins ("==2.32.4", "===2.32.4") become record
versions. Everything else -- ranges, wildcards, compatible releases,
unpinned names, URL and local-path references, Poetry caret/tilde specs --
stays in "version_spec" with a structured warning. Versions are never
guessed.
"""

import json
import re
from pathlib import Path

from ..models import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PARSE_DEPTH,
    add_location,
    guarded_local_file,
    new_record,
    new_warning,
    scan_root_for,
)
from ..redact import redact_dependency_ref, redact_display_text, redact_urls

# ---------------------------------------------------------------------------
# Requirement-line parsing (shared by requirements files and pyproject)
# ---------------------------------------------------------------------------

_DIRECT_REF_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[([^\]]*)\])?\s*@\s*(.+)$")
_NAME_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[([^\]]*)\])?\s*(.*)$")
_VERSION_CHARSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._!+~-]*$")
_LOCAL_PATH_RE = re.compile(
    r"^(?:\./|\.\./|\.\\|\.\.\\|/|\\|~(?:[/\\]|\Z)|[A-Za-z]:[/\\]|file:)")
_INCLUDE_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _is_version(spec):
    """True when spec is a plain exact version (no operators/wildcards)."""
    return bool(_VERSION_CHARSET_RE.fullmatch(spec)) and "*" not in spec


def _is_local_path(token):
    """True for ., .., ./, ../, /, \\, ~, drive-letter and file: paths."""
    if token in (".", ".."):
        return True
    return bool(_LOCAL_PATH_RE.match(token))


def _parse_requirement(spec_line):
    """One PEP 508-ish requirement -> parsed dict (pure, no I/O).

    Keys: name, extras (inner text or None), version, version_spec,
    problem (None | "unpinned" | "unresolved" | "url" | "local" |
    "malformed"), ref (URL/path text with any embedded credentials
    redacted, or None), raw.
    """
    line = spec_line.strip()
    raw = line

    m = _DIRECT_REF_RE.match(line)
    if m:
        name = m.group(1)
        ref = m.group(4).strip()
        if "//" not in ref and "@" not in ref:
            # A bare-line direct reference consumed the user portion as
            # the name (git@host:path): re-join so the SCP shape reaches
            # the redaction boundary.
            ref = f"{name}@{ref}"
        ref = redact_dependency_ref(ref)
        problem = "local" if _is_local_path(ref) else "url"
        return {"name": name, "extras": m.group(3), "version": None,
                "version_spec": None, "problem": problem, "ref": ref,
                "raw": raw}

    if _is_local_path(line) and "@" not in line:
        return {"name": None, "extras": None, "version": None,
                "version_spec": None, "problem": "local", "ref": line,
                "raw": raw}

    spec, marker = line, None
    if ";" in line:
        spec, marker = line.split(";", 1)
        marker = marker.strip()
        spec = spec.strip()

    m = _NAME_RE.match(spec)
    if not m:
        return {"name": None, "extras": None, "version": None,
                "version_spec": None, "problem": "malformed", "ref": None,
                "raw": raw}
    name, extras, rest = m.group(1), m.group(3), m.group(4).strip()

    if rest.startswith("@"):
        ref = redact_dependency_ref(rest[1:].strip())
        problem = "local" if _is_local_path(ref) else "url"
        return {"name": name, "extras": extras, "version": None,
                "version_spec": None, "problem": problem, "ref": ref,
                "raw": raw}

    version = None
    version_spec = None
    if not rest:
        problem = "unpinned"
    elif rest.startswith("==="):
        candidate = rest[3:].strip()
        if _is_version(candidate):
            version = candidate
            problem = None
        else:
            problem = "unresolved"
    elif rest.startswith("=="):
        candidate = rest[2:].strip()
        if candidate.endswith(".*") or not _is_version(candidate):
            problem = "unresolved"
        else:
            version = candidate
            problem = None
    else:
        problem = "unresolved"

    if problem in ("unpinned", "unresolved"):
        if rest and marker:
            version_spec = f"{rest} ; {marker}"
        elif marker:
            version_spec = marker
        else:
            version_spec = rest or None
        if version_spec is not None:
            version_spec = redact_dependency_ref(version_spec)

    return {"name": name, "extras": extras, "version": version,
            "version_spec": version_spec, "problem": problem, "ref": None,
            "raw": raw}


def _emit_requirement(parsed, scope, manifest, rel_path, locator_prefix="",
                      line=None):
    """(record, warning) for one parsed requirement; record always emitted
    when the name is known."""
    name = parsed["name"]
    if name is None:
        if parsed["problem"] == "local":
            return None, new_warning(
                "local_path_dependency", rel_path,
                f"local path reference ({parsed['ref']}); "
                f"not a registry package")
        return None, new_warning(
            "parse_error", rel_path,
            f"malformed requirement ({redact_display_text(parsed['raw'])})")

    locator = f"{locator_prefix}{name}"
    if parsed["extras"]:
        locator += f"[{parsed['extras'].strip()}]"
    record = new_record("python", name, version=parsed["version"],
                        version_spec=parsed["version_spec"], scope=scope)
    add_location(record, rel_path, manifest, line=line, locator=locator)

    problem = parsed["problem"]
    if problem == "unpinned":
        return record, new_warning(
            "unresolved_version", rel_path,
            f"{name} has no version constraint (unpinned)")
    if problem == "unresolved":
        return record, new_warning(
            "unresolved_version", rel_path,
            f"{name} has no exact version ({parsed['version_spec']}); "
            f"not guessed")
    if problem == "url":
        return record, new_warning(
            "url_dependency", rel_path,
            f"{name} uses a direct URL reference ({parsed['ref']}); "
            f"version not resolved")
    if problem == "local":
        return record, new_warning(
            "local_path_dependency", rel_path,
            f"{name} references a local path ({parsed['ref']}); "
            f"version not resolved")
    return record, None


# ---------------------------------------------------------------------------
# requirements*.txt
# ---------------------------------------------------------------------------

def parse_requirements_records(path, rel_path, root=None, include_state=None):
    """Parse a requirements file (following includes); (records, warnings)."""
    root_abs = Path(root).resolve() if root is not None else _root_of(path, rel_path)
    if include_state is None:
        include_state = {"visited": set(), "manifests": set()}
    visited = include_state.setdefault("visited", set())
    manifests = include_state.setdefault("manifests", set())
    return _parse_requirements_file(
        path, rel_path, root_abs, active=set(), visited=visited,
        manifests=manifests, depth=0)


def _root_of(path, rel_path):
    """Scan root derived from the absolute path and the rel-path depth."""
    return scan_root_for(path, rel_path)


def _sibling_rel(rel_path, filename):
    directory = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    return f"{directory}/{filename}" if directory else filename


def _parse_requirements_file(
        path, rel_path, root_abs, active, visited, manifests, depth):
    if depth > MAX_PARSE_DEPTH:
        return [], [new_warning(
            "include_depth", rel_path,
            f"requirements include chain exceeds {MAX_PARSE_DEPTH} levels")]
    abs_path = Path(path).resolve()
    if abs_path in active:
        return [], [new_warning(
            "include_cycle", rel_path,
            f"requirements include cycle at {rel_path}")]
    if abs_path in visited:
        return [], []
    if len(visited) >= MAX_FILES:
        return [], [new_warning(
            "include_limit", rel_path,
            f"requirements include limit of {MAX_FILES} files reached; "
            "remaining includes skipped")]
    visited.add(abs_path)
    next_active = active | {abs_path}

    try:
        size = abs_path.stat().st_size
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path,
            f"could not stat requirements file: {exc}")]
    if size > MAX_FILE_BYTES:
        return [], [new_warning(
            "oversize_input", rel_path,
            f"file exceeds {MAX_FILE_BYTES} byte limit ({size} bytes); "
            f"skipped")]
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path,
            f"could not read requirements file: {exc}")]
    manifests.add(rel_path)

    records = []
    warnings = []
    for lineno, raw_line in _logical_requirement_lines(text):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("-"):
            _handle_option_line(
                line, abs_path, rel_path, root_abs, next_active, visited,
                manifests, depth, records, warnings)
            continue
        # pip-tools appends integrity hashes to otherwise exact requirements.
        # Hashes describe artifacts; they are not part of the version spec.
        requirement = re.split(
            r"\s+--(?:hash|no-binary|only-binary|config-settings)(?:=|\s)",
            line, maxsplit=1)[0]
        parsed = _parse_requirement(requirement.strip())
        record, warning = _emit_requirement(
            parsed, "runtime", "requirements", rel_path, line=lineno)
        if record:
            records.append(record)
        if warning:
            warnings.append(warning)
    return records, warnings


def _logical_requirement_lines(text):
    """Yield ``(physical_start_line, logical_line)`` with continuations joined.

    Lines split on CR/LF only: exotic whitespace separators must survive
    into the logical line so the redaction boundary sees them.
    """
    parts = []
    start = None
    pieces = re.split(r"(\r\n|\r|\n)", text)
    lineno = 0
    for i in range(0, len(pieces), 2):
        content = pieces[i]
        ending = pieces[i + 1] if i + 1 < len(pieces) else ""
        if content == "" and not ending and i + 1 >= len(pieces):
            break
        lineno += 1
        if start is None:
            start = lineno
        if ending and content.endswith("\\"):
            parts.append(content[:-1])
            continue
        parts.append(content)
        yield start, " ".join(parts)
        parts = []
        start = None
    if parts:
        # A trailing backslash followed by the file's final newline is still
        # a continuation; join it to the empty logical remainder.
        yield start, " ".join(parts)


def _strip_comment(line):
    """Cut a "#" comment -- only at line start or preceded by whitespace
    (so URL fragments survive)."""
    for idx, ch in enumerate(line):
        if ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
            return line[:idx]
    return line


def _handle_option_line(
        line, abs_path, rel_path, root_abs, active, visited, manifests,
        depth, records, warnings):
    tokens = line.split()
    option = tokens[0]
    attached = None
    if option.startswith("--requirement="):
        attached = option.partition("=")[2]
        option = "--requirement"
    elif option.startswith("-r") and option != "-r":
        attached = option[2:]
        option = "-r"
    if option in ("-r", "--requirement"):
        if attached:
            target = attached
        elif len(tokens) >= 2:
            target = tokens[1]
        else:
            warnings.append(new_warning(
                "parse_error", rel_path,
                f"missing target for {option} include"))
            return
        target = target.strip().strip('"').strip("'")
        if _INCLUDE_URL_RE.match(target):
            warnings.append(new_warning(
                "include_remote", rel_path,
                f"include target {redact_urls(target)} is remote; "
                f"not followed"))
            return
        inc_abs, inc_rel = _resolve_include(target, abs_path, root_abs)
        if inc_abs is None:
            warnings.append(new_warning(
                "include_escape", rel_path,
                f"include target {redact_urls(target)} lies outside the "
                f"scan root; not followed"))
            return
        sub_records, sub_warnings = _parse_requirements_file(
            inc_abs, inc_rel, root_abs, active, visited, manifests, depth + 1)
        records.extend(sub_records)
        warnings.extend(sub_warnings)
    elif option in ("-e", "--editable"):
        target = " ".join(tokens[1:]).strip()
        if not target:
            warnings.append(new_warning(
                "parse_error", rel_path, "missing target for -e/--editable"))
        elif _is_local_path(target):
            warnings.append(new_warning(
                "local_path_dependency", rel_path,
                f"editable local path ({redact_dependency_ref(target)}); "
                f"not a registry package"))
        elif "://" in target or target.startswith("git+"):
            warnings.append(new_warning(
                "url_dependency", rel_path,
                f"editable URL reference ({redact_dependency_ref(target)}); "
                f"version not resolved"))
        else:
            warnings.append(new_warning(
                "parse_error", rel_path,
                f"unsupported editable target ({redact_dependency_ref(target)})"))
    elif option in ("-c", "--constraint"):
        warnings.append(new_warning(
            "unsupported_option", rel_path,
            "constraint files are not followed"))
    # Any other option (--index-url, --extra-index-url, ...) is environment
    # configuration, not a dependency declaration: silently ignored.


def _resolve_include(target, including_abs, root_abs):
    """(abs_path, rel_posix) for an include target, or (None, None) when
    it resolves outside the scan root."""
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path(including_abs).parent / candidate
    try:
        resolved = candidate.resolve()
        rel = resolved.relative_to(root_abs).as_posix()
    except (ValueError, OSError):
        return None, None
    return resolved, rel


# ---------------------------------------------------------------------------
# pyproject.toml (conservative TOML subset)
# ---------------------------------------------------------------------------

class _UnsupportedToml(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _MiniToml:
    """Conservative TOML-subset scanner.

    Supports table headers [a.b.c] (bare or quoted components), single-
    component keys, basic/literal strings, booleans, numbers, multi-line
    arrays, and single-line inline tables. Everything else raises
    _UnsupportedToml with a line number; the caller keeps the tables
    parsed so far and warns.

    With skip_array_tables=True, array-of-tables headers ([[a.b]]) are
    recognized instead of rejected: their header path is consumed and
    their key/values are parked in a scratch table that is never
    returned, so later [table] sections still parse. Used for Pipfile,
    where [[source]] index blocks carry no dependency data.
    """

    _BARE = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

    def __init__(self, text, skip_array_tables=False):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.line = 1
        self.tables = {}
        self.current = self.tables
        self.skip_array_tables = skip_array_tables
        self._scratch = {}

    def parse(self):
        while True:
            self._skip_ws_comments_newlines()
            if self.i >= self.n:
                return self.tables
            if self._peek() == "[":
                self._parse_table_header()
            else:
                self._parse_key_value()

    def _peek(self):
        return self.s[self.i] if self.i < self.n else ""

    def _skip_spaces(self):
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def _skip_ws_comments_newlines(self):
        while self.i < self.n:
            ch = self.s[self.i]
            if ch in " \t\r":
                self.i += 1
            elif ch == "\n":
                self.line += 1
                self.i += 1
            elif ch == "#":
                while self.i < self.n and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def _parse_table_header(self):
        self.i += 1
        if self._peek() == "[":
            if not self.skip_array_tables:
                raise _UnsupportedToml(
                    f"line {self.line}: array of tables ([[...]]) is not "
                    f"supported")
            self.i += 1
            self._parse_header_path()
            if self._peek() != "]":
                raise _UnsupportedToml(
                    f"line {self.line}: malformed table header")
            self.i += 1
            if self._peek() != "]":
                raise _UnsupportedToml(
                    f"line {self.line}: malformed table header")
            self.i += 1
            self.current = self._scratch
            return
        path = self._parse_header_path()
        if self._peek() != "]":
            raise _UnsupportedToml(
                f"line {self.line}: malformed table header")
        self.i += 1
        table = self.tables
        for component in path:
            nested = table.get(component)
            if not isinstance(nested, dict):
                if nested is not None:
                    raise _UnsupportedToml(
                        f"line {self.line}: conflicting table definition "
                        f"({'.'.join(path)})")
                nested = {}
                table[component] = nested
            table = nested
        self.current = table

    def _parse_header_path(self):
        components = []
        while True:
            self._skip_spaces()
            components.append(self._parse_key_component())
            self._skip_spaces()
            if self._peek() == ".":
                self.i += 1
                continue
            return components

    def _parse_key_component(self):
        ch = self._peek()
        if ch in "\"'":
            return self._parse_string()
        start = self.i
        while self.i < self.n and self.s[self.i] in self._BARE:
            self.i += 1
        if self.i == start:
            raise _UnsupportedToml(f"line {self.line}: missing key")
        return self.s[start:self.i]

    def _parse_key_value(self):
        key = self._parse_key_component()
        self._skip_spaces()
        if self._peek() == ".":
            raise _UnsupportedToml(
                f"line {self.line}: dotted keys are not supported")
        if self._peek() != "=":
            raise _UnsupportedToml(f"line {self.line}: expected '=' after key")
        self.i += 1
        self._skip_spaces()
        if self._peek() in ("\n", "\r", "#", ""):
            raise _UnsupportedToml(f"line {self.line}: missing value")
        value = self._parse_value()
        self._skip_spaces()
        if self._peek() == "#":
            while self.i < self.n and self.s[self.i] != "\n":
                self.i += 1
        if self.i < self.n and self.s[self.i] not in "\r\n":
            raise _UnsupportedToml(
                f"line {self.line}: unexpected content after value")
        self.current[key] = value

    def _parse_value(self):
        ch = self._peek()
        if ch == '"':
            if self.s[self.i:self.i + 3] == '"""':
                raise _UnsupportedToml(
                    f"line {self.line}: multi-line basic strings are not "
                    f"supported")
            return self._parse_string()
        if ch == "'":
            if self.s[self.i:self.i + 3] == "'''":
                raise _UnsupportedToml(
                    f"line {self.line}: multi-line literal strings are not "
                    f"supported")
            return self._parse_string()
        if ch == "[":
            return self._parse_array()
        if ch == "{":
            return self._parse_inline_table()
        start = self.i
        while self.i < self.n and self.s[self.i] not in ",]}#\n\r":
            self.i += 1
        raw = self.s[start:self.i].strip()
        if not raw:
            raise _UnsupportedToml(f"line {self.line}: missing value")
        if raw == "true":
            return True
        if raw == "false":
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw

    def _parse_string(self):
        quote = self.s[self.i]
        self.i += 1
        out = []
        while True:
            if self.i >= self.n:
                raise _UnsupportedToml(
                    f"line {self.line}: unterminated string")
            ch = self.s[self.i]
            if ch in "\n\r":
                raise _UnsupportedToml(
                    f"line {self.line}: raw newline in string")
            if ch == quote:
                self.i += 1
                return "".join(out)
            if ch == "\\" and quote == '"':
                self.i += 1
                out.append(self._parse_escape())
                continue
            out.append(ch)
            self.i += 1

    def _parse_escape(self):
        if self.i >= self.n:
            raise _UnsupportedToml(f"line {self.line}: unterminated escape")
        esc = self.s[self.i]
        simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
                  "b": "\b", "f": "\f"}
        if esc in simple:
            self.i += 1
            return simple[esc]
        if esc in ("u", "U"):
            width = 4 if esc == "u" else 8
            digits = self.s[self.i + 1:self.i + 1 + width]
            if len(digits) != width or any(
                    c not in "0123456789abcdefABCDEF" for c in digits):
                raise _UnsupportedToml(
                    f"line {self.line}: invalid unicode escape")
            self.i += 1 + width
            return chr(int(digits, 16))
        raise _UnsupportedToml(f"line {self.line}: invalid escape \\{esc}")

    def _parse_array(self):
        self.i += 1
        items = []
        while True:
            self._skip_ws_comments_newlines()
            if self.i >= self.n:
                raise _UnsupportedToml(
                    f"line {self.line}: unterminated array")
            if self._peek() == "]":
                self.i += 1
                return items
            items.append(self._parse_value())
            self._skip_ws_comments_newlines()
            if self._peek() == ",":
                self.i += 1
                continue
            if self._peek() == "]":
                self.i += 1
                return items
            raise _UnsupportedToml(
                f"line {self.line}: expected ',' or ']' in array")

    def _parse_inline_table(self):
        self.i += 1
        table = {}
        while True:
            self._skip_spaces()
            if self._peek() in ("\n", "\r", ""):
                raise _UnsupportedToml(
                    f"line {self.line}: multi-line inline tables are not "
                    f"supported")
            if self._peek() == "}":
                self.i += 1
                return table
            key = self._parse_key_component()
            self._skip_spaces()
            if self._peek() == ".":
                raise _UnsupportedToml(
                    f"line {self.line}: dotted keys are not supported")
            if self._peek() != "=":
                raise _UnsupportedToml(
                    f"line {self.line}: expected '=' in inline table")
            self.i += 1
            self._skip_spaces()
            if self._peek() in ("\n", "\r"):
                raise _UnsupportedToml(
                    f"line {self.line}: multi-line inline tables are not "
                    f"supported")
            table[key] = self._parse_value()
            self._skip_spaces()
            if self._peek() == ",":
                self.i += 1
                continue
            if self._peek() == "}":
                self.i += 1
                return table
            raise _UnsupportedToml(
                f"line {self.line}: expected ',' or '}}' in inline table")


def _parse_toml_subset(text, rel_path):
    """(tables, warning) -- on unsupported syntax parsing stops, keeping
    the tables parsed so far."""
    parser = _MiniToml(text)
    try:
        return parser.parse(), None
    except _UnsupportedToml as exc:
        return parser.tables, new_warning(
            "toml_unsupported", rel_path, exc.message)


def parse_pyproject_records(path, rel_path):
    """Parse pyproject.toml (PEP 621 + common Poetry tables); returns
    (records, warnings)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path,
            f"could not read pyproject.toml: {exc}")]

    try:
        tables, warning = _parse_toml_subset(text, rel_path)
    except RecursionError:
        return [], [new_warning(
            "parse_error", rel_path,
            "pyproject.toml nesting exceeds the safe parser limit")]
    warnings = [warning] if warning else []
    records = []

    project = tables.get("project")
    if project is not None and not isinstance(project, dict):
        warnings.append(new_warning(
            "toml_unsupported", rel_path,
            "[project] is not a table; skipped"))
        project = None
    if project is not None:
        _extract_pep621(project, records, warnings, rel_path)

    tool = tables.get("tool")
    if tool is not None and not isinstance(tool, dict):
        warnings.append(new_warning(
            "toml_unsupported", rel_path,
            "[tool] is not a table; skipped"))
        tool = None
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if poetry is not None and not isinstance(poetry, dict):
        warnings.append(new_warning(
            "toml_unsupported", rel_path,
            "[tool.poetry] is not a table; skipped"))
        poetry = None
    if poetry is not None:
        _extract_poetry(poetry, records, warnings, rel_path)

    return records, warnings


def _extract_pep621(project, records, warnings, rel_path):
    """Records from the PEP 621 [project] table."""
    requires_python = project.get("requires-python")
    if requires_python is not None:
        if isinstance(requires_python, str) and requires_python.strip():
            record = new_record("python", "python", version=None,
                                version_spec=redact_urls(
                                    requires_python.strip()),
                                kind="runtime")
            add_location(record, rel_path, "pyproject",
                         locator="requires-python")
            records.append(record)
        else:
            warnings.append(new_warning(
                "toml_unsupported", rel_path,
                "requires-python is not a string; skipped"))

    dependencies = project.get("dependencies")
    if dependencies is not None:
        if isinstance(dependencies, list):
            for entry in dependencies:
                if isinstance(entry, str):
                    parsed = _parse_requirement(entry)
                    record, warning = _emit_requirement(
                        parsed, "runtime", "pyproject", rel_path,
                        locator_prefix="project.dependencies.")
                    if record:
                        records.append(record)
                    if warning:
                        warnings.append(warning)
                else:
                    warnings.append(new_warning(
                        "toml_unsupported", rel_path,
                        "project.dependencies contains a non-string entry"))
        else:
            warnings.append(new_warning(
                "toml_unsupported", rel_path,
                "project.dependencies is not an array; skipped"))

    optional = project.get("optional-dependencies")
    if optional is not None:
        if isinstance(optional, dict):
            for group, entries in optional.items():
                if not isinstance(entries, list):
                    warnings.append(new_warning(
                        "toml_unsupported", rel_path,
                        f"optional-dependencies.{group} is not an array; "
                        f"skipped"))
                    continue
                for entry in entries:
                    if isinstance(entry, str):
                        parsed = _parse_requirement(entry)
                        record, warning = _emit_requirement(
                            parsed, "optional", "pyproject", rel_path,
                            locator_prefix=(
                                f"project.optional-dependencies.{group}."))
                        if record:
                            records.append(record)
                        if warning:
                            warnings.append(warning)
                    else:
                        warnings.append(new_warning(
                            "toml_unsupported", rel_path,
                            f"optional-dependencies.{group} contains a "
                            f"non-string entry"))
        else:
            warnings.append(new_warning(
                "toml_unsupported", rel_path,
                "project.optional-dependencies is not a table; skipped"))


def _extract_poetry(poetry, records, warnings, rel_path):
    """Records from common Poetry dependency tables."""
    sources = [
        ("runtime", "tool.poetry.dependencies", poetry.get("dependencies")),
        ("dev", "tool.poetry.dev-dependencies",
         poetry.get("dev-dependencies")),
    ]
    groups = poetry.get("group")
    if groups is not None:
        if isinstance(groups, dict):
            for group_name, group_table in groups.items():
                if not isinstance(group_table, dict):
                    warnings.append(new_warning(
                        "toml_unsupported", rel_path,
                        f"tool.poetry.group.{group_name} is not a table; "
                        f"skipped"))
                    continue
                scope = "dev" if group_name == "dev" else "optional"
                deps = group_table.get("dependencies")
                if deps is not None:
                    sources.append((
                        scope, f"tool.poetry.group.{group_name}.dependencies",
                        deps))
        else:
            warnings.append(new_warning(
                "toml_unsupported", rel_path,
                "tool.poetry.group is not a table; skipped"))

    for scope, prefix, dependencies in sources:
        if dependencies is None:
            continue
        if not isinstance(dependencies, dict):
            warnings.append(new_warning(
                "toml_unsupported", rel_path,
                f"{prefix} is not a table; skipped"))
            continue
        for name, value in dependencies.items():
            locator = f"{prefix}.{name}"
            if name == "python" and prefix == "tool.poetry.dependencies":
                if isinstance(value, str) and value.strip():
                    record = new_record("python", "python", version=None,
                                        version_spec=redact_urls(
                                            value.strip()),
                                        kind="runtime")
                    add_location(record, rel_path, "pyproject",
                                 locator=locator)
                    records.append(record)
                else:
                    warnings.append(new_warning(
                        "toml_unsupported", rel_path,
                        f"{locator} is not a string constraint; skipped"))
                continue
            _emit_poetry_dependency(name, value, scope, locator, records,
                                    warnings, rel_path)


def _emit_poetry_dependency(name, value, scope, locator, records, warnings,
                            rel_path):
    """One Poetry dependency value (string, inline table, or table)."""
    record = new_record("python", name, scope=scope)
    add_location(record, rel_path, "pyproject", locator=locator)
    records.append(record)

    if isinstance(value, str):
        if _is_version(value):
            record["version"] = value
        else:
            spec = redact_dependency_ref(value)
            record["version_spec"] = spec
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"{name} has no exact version ({spec}); not guessed"))
        return
    if isinstance(value, dict):
        version = value.get("version")
        if isinstance(version, str) and version.strip():
            spec = version.strip()
            if _is_version(spec):
                record["version"] = spec
            else:
                spec = redact_dependency_ref(spec)
                record["version_spec"] = spec
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"{name} has no exact version ({spec}); "
                    f"not guessed"))
        elif "path" in value or "file" in value:
            warnings.append(new_warning(
                "local_path_dependency", rel_path,
                f"{name} references a local path; version not resolved"))
        elif "git" in value or "url" in value:
            warnings.append(new_warning(
                "url_dependency", rel_path,
                f"{name} uses a direct URL reference; version not resolved"))
        else:
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"{name} has no version declared"))
        return
    warnings.append(new_warning(
        "toml_unsupported", rel_path,
        f"{locator} has an unsupported value type; skipped"))


# ---------------------------------------------------------------------------
# Pipfile and Pipfile.lock
# ---------------------------------------------------------------------------

def parse_pipfile_records(path, rel_path, root=None, consumed_locks=None):
    """Parse direct Pipfile declarations, enriched by a sibling lock file.

    [[source]] index blocks are recognized and skipped; they carry
    package-index configuration, not dependencies. A sibling
    Pipfile.lock successfully read is reported through `consumed_locks`
    (the scan-root-relative path) so discovery lists it as a consumed
    manifest even when it resolves nothing.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        tables = _MiniToml(text, skip_array_tables=True).parse()
    except (OSError, _UnsupportedToml) as exc:
        message = exc.message if isinstance(exc, _UnsupportedToml) else str(exc)
        return [], [new_warning("parse_error", rel_path,
                                f"Pipfile parse error: {message}")]

    lock = {}
    warnings = []
    lock_path = Path(path).with_name("Pipfile.lock")
    root_abs = Path(root).resolve() if root is not None else scan_root_for(
        path, rel_path)
    guarded_lock, lock_warning = guarded_local_file(
        lock_path, root_abs, _sibling_rel(rel_path, "Pipfile.lock"))
    if lock_warning:
        warnings.append(lock_warning)
    if guarded_lock is not None:
        try:
            lock = json.loads(guarded_lock.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            lock = {}
            warnings.append(new_warning(
                "parse_error", _sibling_rel(rel_path, "Pipfile.lock"),
                f"Pipfile.lock parse error: {exc}"))
        else:
            if consumed_locks is not None:
                consumed_locks.add(_sibling_rel(rel_path, "Pipfile.lock"))

    records = []
    for section, lock_section, scope in (
            ("packages", "default", "runtime"),
            ("dev-packages", "develop", "dev")):
        packages = tables.get(section)
        if not isinstance(packages, dict):
            continue
        locked = lock.get(lock_section) if isinstance(lock, dict) else {}
        for name in sorted(packages):
            value = packages[name]
            spec = value if isinstance(value, str) else (
                value.get("version") if isinstance(value, dict) else None)
            version = None
            if isinstance(spec, str):
                candidate = spec[2:] if spec.startswith("==") else spec
                if _is_version(candidate):
                    version = candidate
            resolved_from_lock = False
            lock_info = locked.get(name) if isinstance(locked, dict) else None
            lock_version = lock_info.get("version") if isinstance(lock_info, dict) else None
            if version is None and isinstance(lock_version, str) \
                    and lock_version.startswith("=="):
                candidate = lock_version[2:]
                if _is_version(candidate):
                    version = candidate
                    resolved_from_lock = True
            record = new_record("python", name, version=version,
                                version_spec=None if version else (
                                    redact_urls(spec)
                                    if isinstance(spec, str) else spec),
                                scope=scope, direct=True)
            add_location(record, rel_path, "pipfile",
                         locator=f"{section}.{name}")
            if resolved_from_lock:
                add_location(
                    record, _sibling_rel(rel_path, "Pipfile.lock"),
                    "pipfile-lock", locator=f"{lock_section}.{name}")
            if version is None:
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"{name} has no exact direct Pipfile version; not guessed"))
            records.append(record)
    return records, warnings

def parse_pipfile_lock_records(path, rel_path):
    """Parse Pipfile.lock; returns (records, warnings).

    The lock holds the resolved dependency graph, not just direct
    declarations, so every record carries direct=False (mirroring how
    the go parser emits indirect requires).
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return [], [new_warning(
            "parse_error", rel_path, f"Pipfile.lock parse error: {exc}")]

    if not isinstance(data, dict):
        return [], [new_warning(
            "parse_error", rel_path,
            "Pipfile.lock top-level value is not an object")]

    records = []
    warnings = []
    for section, scope in (("default", "runtime"), ("develop", "dev")):
        packages = data.get(section)
        if not isinstance(packages, dict):
            continue
        for name in sorted(packages):
            info = packages.get(name)
            if not isinstance(info, dict):
                warnings.append(new_warning(
                    "parse_error", rel_path,
                    f"{section}.{name} is not a table; skipped"))
                continue
            version = info.get("version")
            if (isinstance(version, str) and version.startswith("==")
                    and _is_version(version[2:])):
                record = new_record("python", name, version=version[2:],
                                    scope=scope, direct=False)
            else:
                record = new_record("python", name, version=None,
                                    scope=scope, direct=False)
                if "git" in info or "url" in info:
                    warnings.append(new_warning(
                        "url_dependency", rel_path,
                        f"{name} is a VCS/URL dependency in Pipfile.lock; "
                        f"version not resolved"))
                elif "path" in info or "file" in info:
                    warnings.append(new_warning(
                        "local_path_dependency", rel_path,
                        f"{name} is a local-path dependency in Pipfile.lock;"
                        f" version not resolved"))
                else:
                    warnings.append(new_warning(
                        "unresolved_version", rel_path,
                        f"{name} has no pinned version in Pipfile.lock"))
            add_location(record, rel_path, "pipfile-lock",
                         locator=f"{section}.{name}")
            records.append(record)
    return records, warnings


# ---------------------------------------------------------------------------
# Runtime evidence: .python-version and runtime.txt
# ---------------------------------------------------------------------------

def parse_python_version_records(path, rel_path):
    """Parse .python-version; returns (records, warnings)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path,
            f"could not read .python-version: {exc}")]

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\d+(?:\.\d+)*)", line)
        if not m:
            return [], [new_warning(
                "unresolved_version", rel_path,
                f".python-version does not contain a recognized version "
                f"({redact_urls(line)})")]
        record = new_record("python", "python", version=m.group(1),
                            kind="runtime")
        add_location(record, rel_path, "python", locator="python-version")
        return [record], []
    return [], []


def parse_runtime_txt_records(path, rel_path):
    """Parse a Heroku-style runtime.txt; returns (records, warnings)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path,
            f"could not read runtime.txt: {exc}")]

    content = text.strip()
    m = re.fullmatch(r"[Pp]ython-([0-9]+(?:\.[0-9]+)*)", content)
    if not m:
        return [], [new_warning(
            "unresolved_version", rel_path,
            f"runtime.txt does not contain a recognized Python version "
            f"({redact_urls(content)})")]
    record = new_record("python", "python", version=m.group(1),
                        kind="runtime")
    add_location(record, rel_path, "python", locator="runtime.txt")
    return [record], []
