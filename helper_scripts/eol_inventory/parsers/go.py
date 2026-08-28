"""go.mod parser (normalized records).

Pure stdlib; never runs the go tooling and never touches the network.

Emitted records:
    - `module` directive            kind="module" (the project itself)
    - `go` / `toolchain` directives kind="runtime", name "go"
    - direct requires               direct=True,  kind="dependency"
    - indirect requires             direct=False, kind="dependency" —
      emitted too, so the indirect count is derivable from the records
    - module-to-module replace targets: the replacement module+version is
      what actually gets built, so it is recorded with provenance
      (locator "replace:<old>"), and the replaced module's own record
      gains a "replace=><target>" provenance location when present.

Replace directives always produce a warning. Local-path replacements
(./, ../, / or a Windows drive prefix) NEVER produce a public dependency
record — a local path resolves on no public registry — only a
go_local_replace warning.

Version normalization: the leading "v"/"V" of Go module versions is
stripped ("v1.2.3" -> "1.2.3"); toolchain values additionally lose their
"go" prefix ("go1.22.5" -> "1.22.5"). +incompatible and pseudo-version
suffixes are preserved verbatim.
"""

import re
from pathlib import Path

from ..models import add_location, new_record, new_warning

_BLOCK_RE = re.compile(r"^(\w+)\s*\(\s*$")
_TOOLCHAIN_RE = re.compile(r"^toolchain\s+(\S+)$")
_GO_RE = re.compile(r"^go\s+(\S+)$")
_MODULE_RE = re.compile(r"^module\s+(\S+)$")

_IGNORED_BLOCKS = ("exclude", "retract")


def _strip_v(version):
    """'v1.2.3' -> '1.2.3'; leaves non-versions and None alone."""
    if version and version[0] in "vV" and len(version) > 1:
        return version[1:]
    return version


def _is_local_path(token):
    """True for ./, ../, / and drive-letter rooted replacement targets."""
    if token.startswith("./") or token.startswith("../"):
        return True
    if token.startswith("/") or token.startswith("\\"):
        return True
    drive = re.match(r"^[A-Za-z]:", token)
    return drive is not None


def _parse_replace_side(side):
    """'<old> [vX]' / '<new> [vY]' -> (path_or_dir, version_or_None)."""
    tokens = side.split()
    if not tokens:
        return None, None
    target = tokens[0]
    version = tokens[1] if len(tokens) > 1 else None
    return target, _strip_v(version)


def parse_go_mod_records(path, rel_path):
    """Parse go.mod; return (records, warnings)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path, f"could not read go.mod: {exc}")]

    records = []
    warnings = []
    block = None

    def emit_dependency(name, version, direct, line, locator):
        record = new_record("go", name, version=version, direct=direct)
        add_location(record, rel_path, "go", line=line, locator=locator)
        records.append(record)
        return record

    def handle_require(code, line, indirect):
        tokens = code.split()
        if len(tokens) < 2:
            warnings.append(new_warning(
                "parse_error", rel_path,
                f"go.mod line {line}: malformed require: {code!r}"))
            return
        emit_dependency(tokens[0], _strip_v(tokens[1]), not indirect,
                        line, f"require:{tokens[0]}")

    def handle_replace(code, line):
        if "=>" not in code:
            warnings.append(new_warning(
                "parse_error", rel_path,
                f"go.mod line {line}: malformed replace: {code!r}"))
            return
        old, old_version = _parse_replace_side(code.split("=>", 1)[0])
        target, target_version = _parse_replace_side(code.split("=>", 1)[1])
        if not old or not target:
            warnings.append(new_warning(
                "parse_error", rel_path,
                f"go.mod line {line}: malformed replace: {code!r}"))
            return

        if _is_local_path(target):
            # Local replacements are never emitted as public dependencies.
            warnings.append(new_warning(
                "go_local_replace", rel_path,
                f"line {line}: replace {old} => {target} is a local path; "
                f"not emitted as a public dependency"))
            target_record = None
        else:
            warnings.append(new_warning(
                "go_replace", rel_path,
                f"line {line}: replace {old} => {target}"
                + (f" {target_version}" if target_version else "")))
            target_record = emit_dependency(
                target, target_version, direct=False, line=line,
                locator=f"replace:{old}")

        # Provenance on the replaced module's require record, when present.
        for record in records:
            if (record["kind"] == "dependency" and record["name"] == old
                    and record is not target_record):
                add_location(record, rel_path, "go", line=line,
                             locator=f"replace=>{target}")
                break

    for lineno, raw in enumerate(text.splitlines(), 1):
        parts = raw.split("//", 1)
        code = parts[0].strip()
        indirect = len(parts) > 1 and parts[1].strip() == "indirect"

        if not code:
            continue

        if code == ")":
            block = None
            continue

        if block == "require":
            handle_require(code, lineno, indirect)
            continue
        if block == "replace":
            handle_replace(code, lineno)
            continue
        if block in _IGNORED_BLOCKS:
            continue

        open_block = _BLOCK_RE.match(code)
        if open_block:
            name = open_block.group(1).lower()
            block = name if (name in ("require", "replace")
                             or name in _IGNORED_BLOCKS) else None
            continue

        m = _MODULE_RE.match(code)
        if m:
            record = new_record("go", m.group(1), kind="module")
            add_location(record, rel_path, "go", line=lineno, locator="module")
            records.append(record)
            continue

        m = _GO_RE.match(code)
        if m:
            record = new_record("go", "go", version=m.group(1),
                                kind="runtime")
            add_location(record, rel_path, "go", line=lineno, locator="go")
            records.append(record)
            continue

        m = _TOOLCHAIN_RE.match(code)
        if m:
            record = new_record("go", "go",
                                version=_strip_v(m.group(1)).removeprefix("go"),
                                kind="runtime")
            add_location(record, rel_path, "go", line=lineno,
                         locator="toolchain")
            records.append(record)
            continue

        tokens = code.split(None, 1)
        if tokens and tokens[0] == "require" and len(tokens) > 1:
            handle_require(tokens[1], lineno, indirect)
        elif tokens and tokens[0] == "replace" and len(tokens) > 1:
            handle_replace(tokens[1], lineno)
        # Anything else (exclude/retract single lines, stray text) is ignored.

    return records, warnings
