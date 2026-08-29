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
    - Pipfile.lock        the resolved graph (records carry direct=False,
                          mirroring how the go parser emits indirect
                          requires)
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

from ..models import MAX_FILE_BYTES, add_location, new_record, new_warning

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
_CONTINUATION_RE = re.compile(r"\\\r?\n")


def _is_version(spec):
    """True when spec is a plain exact version (no operators/wildcards)."""
    return bool(_VERSION_CHARSET_RE.match(spec)) and "*" not in spec


def _is_local_path(token):
    """True for ., .., ./, ../, /, \\, ~, drive-letter and file: paths."""
    if token in (".", ".."):
        return True
    return bool(_LOCAL_PATH_RE.match(token))


def _parse_requirement(spec_line):
    """One PEP 508-ish requirement -> parsed dict (pure, no I/O).

    Keys: name, extras (inner text or None), version, version_spec,
    problem (None | "unpinned" | "unresolved" | "url" | "local" |
    "malformed"), ref (URL/path text or None), raw.
    """
    line = spec_line.strip()
    raw = line

    m = _DIRECT_REF_RE.match(line)
    if m:
        ref = m.group(4).strip()
        problem = "local" if _is_local_path(ref) else "url"
        return {"name": m.group(1), "extras": m.group(3), "version": None,
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
        ref = rest[1:].strip()
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
            f"malformed requirement ({parsed['raw']})")

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

def parse_requirements_records(path, rel_path):
    """Parse a requirements file (following includes); (records, warnings)."""
    root_abs = _root_of(path, rel_path)
    return _parse_requirements_file(path, rel_path, root_abs, set())


def _root_of(path, rel_path):
    """Scan root derived from the absolute path and the rel-path depth."""
    root = Path(path).resolve()
    for _ in range(len(rel_path.split("/")) - 1):
        root = root.parent
    return root


def _parse_requirements_file(path, rel_path, root_abs, seen):
    abs_path = Path(path).resolve()
    if abs_path in seen:
        return [], [new_warning(
            "include_cycle", rel_path,
            f"requirements include cycle at {rel_path}")]
    seen.add(abs_path)

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

    text = _CONTINUATION_RE.sub(" ", text)
    records = []
    warnings = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("-"):
            _handle_option_line(line, abs_path, rel_path, root_abs, seen,
                                records, warnings)
            continue
        parsed = _parse_requirement(line)
        record, warning = _emit_requirement(
            parsed, "runtime", "requirements", rel_path, line=lineno)
        if record:
            records.append(record)
        if warning:
            warnings.append(warning)
    return records, warnings


def _strip_comment(line):
    """Cut a "#" comment -- only at line start or preceded by whitespace
    (so URL fragments survive)."""
    for idx, ch in enumerate(line):
        if ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
            return line[:idx]
    return line


def _handle_option_line(line, abs_path, rel_path, root_abs, seen,
                        records, warnings):
    tokens = line.split()
    option = tokens[0]
    if option in ("-r", "--requirement"):
        if len(tokens) < 2:
            warnings.append(new_warning(
                "parse_error", rel_path,
                f"missing target for {option} include"))
            return
        target = tokens[1].strip().strip('"').strip("'")
        inc_abs, inc_rel = _resolve_include(target, abs_path, root_abs)
        if inc_abs is None:
            warnings.append(new_warning(
                "include_escape", rel_path,
                f"include target {target} lies outside the scan root; "
                f"not followed"))
            return
        sub_records, sub_warnings = _parse_requirements_file(
            inc_abs, inc_rel, root_abs, seen)
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
                f"editable local path ({target}); not a registry package"))
        elif "://" in target or target.startswith("git+"):
            warnings.append(new_warning(
                "url_dependency", rel_path,
                f"editable URL reference ({target}); version not resolved"))
        else:
            warnings.append(new_warning(
                "parse_error", rel_path,
                f"unsupported editable target ({target})"))
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
    """

    _BARE = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

    def __init__(self, text):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.line = 1
        self.tables = {}
        self.current = self.tables

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
            raise _UnsupportedToml(
                f"line {self.line}: array of tables ([[...]]) is not "
                f"supported")
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

    tables, warning = _parse_toml_subset(text, rel_path)
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
                                version_spec=requires_python.strip(),
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
                                        version_spec=value.strip(),
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
            record["version_spec"] = value
            warnings.append(new_warning(
                "unresolved_version", rel_path,
                f"{name} has no exact version ({value}); not guessed"))
        return
    if isinstance(value, dict):
        version = value.get("version")
        if isinstance(version, str) and version.strip():
            if _is_version(version.strip()):
                record["version"] = version.strip()
            else:
                record["version_spec"] = version.strip()
                warnings.append(new_warning(
                    "unresolved_version", rel_path,
                    f"{name} has no exact version ({version.strip()}); "
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
# Pipfile.lock
# ---------------------------------------------------------------------------

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
            if isinstance(version, str) and version.startswith("==") \
                    and len(version) > 2:
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
                f"({line})")]
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
    m = re.match(r"^[Pp]ython-([0-9][\w.]*)$", content)
    if not m:
        return [], [new_warning(
            "unresolved_version", rel_path,
            f"runtime.txt does not contain a recognized Python version "
            f"({content})")]
    record = new_record("python", "python", version=m.group(1),
                        kind="runtime")
    add_location(record, rel_path, "python", locator="runtime.txt")
    return [record], []
