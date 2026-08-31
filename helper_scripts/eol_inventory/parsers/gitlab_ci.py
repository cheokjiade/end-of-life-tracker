"""GitLab CI image parser (normalized records).

Conservative line-based scan of .gitlab-ci.yml-style YAML: it
recognizes common CI structures (top-level, `default:`, and job-level
`image:` and `services:` in scalar or `name:` object form; top-level,
`default:`, and job-level `variables:` used to resolve image
references; and local `include:` entries) rather than claiming full
YAML support. CI configuration is never executed.

Remote includes, anchors/aliases/merge keys, inline JSON-style
mappings, unresolved variables, and local includes outside the scan
root produce warnings instead of guesses. Arbitrary variable values
are never emitted into the inventory; they are only used to resolve
image references.

Image records share the container model with the Dockerfile parser:
kind="image", ecosystem="container", registry-normalized name, and the
tag as version. Provenance locators name the owning CI context
("image", "default:image", "<job>:image", "<owner>:services").
"""

import glob as _glob
import re
from pathlib import Path

from ..models import MAX_FILES, guarded_local_file, new_warning
from .docker import emit_image_record

_MAX_INCLUDE_DEPTH = 5

_INCLUDE_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_ANCHOR_RE = re.compile(r"(?:^|[\s{[,&])&[\w.-]+")
_ALIAS_RE = re.compile(r"(?:^|[\s{[,&])\*[\w.-]+")
_MERGE_RE = re.compile(r"(?:^|\s)<<:")

# Per-service mapping keys; anything else in a services list item is
# treated as a scalar image reference.
_SERVICE_KEYS = frozenset({
    "name", "alias", "command", "entrypoint", "variables",
    "ports", "link", "network", "healthcheck",
})

# include: kinds that reference files we cannot or should not read.
_REMOTE_INCLUDE_KINDS = frozenset({
    "project", "remote", "template", "component",
})
_IGNORED_INCLUDE_KINDS = frozenset({"file", "ref", "inputs"})


def _collect_top_level_variables(text):
    """Collect simple top-level/default variables before include traversal."""
    variables = {}
    current_top = None
    collecting = False
    collect_indent = None
    variable_indent = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = re.sub(r"\s+#.*$", "", raw.strip()).strip()
        if not content or ":" not in content:
            continue
        key, _, value = content.partition(":")
        key = key.strip()
        value = value.strip()
        if indent == 0:
            current_top = key
            collecting = key == "variables" and not value
            collect_indent = 0 if collecting else None
            variable_indent = None
            continue
        if current_top == "default" and key == "variables":
            collecting = not value
            collect_indent = indent if collecting else None
            variable_indent = None
            continue
        if not collecting or indent <= collect_indent:
            collecting = False
            continue
        if variable_indent is None:
            variable_indent = indent
        if indent != variable_indent:
            continue
        if value and value not in ("|", "|-", "|+", ">", ">-", ">+"):
            variables[key] = value.strip("\"'")
    return variables


def parse_gitlab_ci_records(path, rel_path, root=None, include_state=None):
    """Parse one GitLab CI YAML file; return (records, warnings).

    root is the scan root; local include targets are resolved against
    it and followed only when they stay inside it. Without a root,
    includes are skipped with a warning.
    """
    if include_state is None:
        include_state = {"files": 0, "visited": set()}
    visited = include_state.setdefault("visited", set())
    resolved = Path(path).resolve()
    if resolved in visited:
        return [], []
    if include_state["files"] >= MAX_FILES:
        return [], [new_warning(
            "ci_include_limit", rel_path,
            f"GitLab CI file limit of {MAX_FILES} files reached; skipped")]
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path,
            f"could not read GitLab CI file: {exc}")]
    visited.add(resolved)
    include_state["files"] += 1
    return _parse_ci_text(
        text, rel_path, root, active=frozenset({resolved}),
        include_state=include_state)


def _parse_ci_text(text, rel_path, root, depth=0, active=frozenset(),
                   include_state=None, inherited_vars=None):
    if include_state is None:
        include_state = {"files": 0, "visited": set()}
    visited = include_state.setdefault("visited", set())
    records = []
    warnings = []
    top_vars = dict(inherited_vars or {})
    top_vars.update(_collect_top_level_variables(text))
    job_vars = {}
    pending_emissions = []   # (raw value, line, locator, job variable dict)
    current_top = None
    collecting = None          # None | "variables" | "services" | "include"
    collect_indent = 0
    vars_target = None         # dict receiving captured variables
    services_locator = None
    pending_image = None       # (indent, locator, name_consumed)
    pending_item_name = False  # services "-" awaiting a "name:" line
    tab_warned = False
    anchors_warned = False
    block_scalar_indent = None

    def warn(category, message):
        warnings.append(new_warning(category, rel_path, message))

    def image_value(raw, line, locator):
        value = raw.strip().strip("\"'")
        if not value:
            return
        if value.startswith(("*", "&")):
            warn("ci_yaml_unsupported",
                 f"line {line}: YAML image aliases/anchors are not resolved")
            return
        if value[0] in "{[":
            warn("ci_yaml_unsupported",
                 f"line {line}: inline mapping or sequence image values "
                 f"are not parsed")
            return
        pending_emissions.append((value, line, locator, job_vars))

    def follow_resolved(resolved, line):
        try:
            resolved = Path(resolved).resolve()
        except OSError as exc:
            warn("ci_include_missing",
                 f"line {line}: local include could not be resolved: {exc}")
            return
        try:
            inc_rel = resolved.relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            warn("ci_include_escape",
                 f"line {line}: local include {resolved.name!r} escapes "
                 f"the scan root; not followed")
            return
        if resolved in active:
            warn("ci_include_depth",
                 f"line {line}: circular local include {inc_rel!r}")
            return
        if resolved in visited:
            return
        if depth + 1 > _MAX_INCLUDE_DEPTH:
            warn("ci_include_depth",
                 f"line {line}: include chain exceeds "
                 f"{_MAX_INCLUDE_DEPTH} levels at {inc_rel!r}")
            return
        if include_state["files"] >= MAX_FILES:
            warn("ci_include_limit",
                 f"line {line}: local include limit of {MAX_FILES} files "
                 "reached; remaining includes skipped")
            return
        guarded, guard_warning = guarded_local_file(resolved, root, inc_rel)
        if guard_warning:
            warnings.append(guard_warning)
            return
        if guarded is None:
            warn("ci_include_missing",
                 f"line {line}: local include {inc_rel!r} does not exist")
            return
        try:
            inc_text = guarded.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warn("ci_include_missing",
                 f"line {line}: local include {inc_rel!r} could not be "
                 f"read: {exc}")
            return
        include_state["files"] += 1
        visited.add(resolved)
        inc_records, inc_warnings = _parse_ci_text(
            inc_text, inc_rel, root, depth + 1, active | {resolved},
            include_state, inherited_vars=dict(top_vars))
        records.extend(inc_records)
        warnings.extend(inc_warnings)

    def follow_local_include(target_raw, line):
        target = target_raw.strip().strip("\"'")
        if not target:
            return
        if _INCLUDE_URL_RE.match(target):
            warn("ci_remote_include",
                 f"line {line}: include URL {target!r} is remote; "
                 f"not followed")
            return
        if root is None:
            warn("ci_include_skipped",
                 f"line {line}: local include {target!r} not followed "
                 f"(scan root unavailable)")
            return
        base = Path(root)
        rel = target.lstrip("/")
        try:
            resolved = (base / rel).resolve()
            resolved.relative_to(base.resolve())
        except (OSError, ValueError):
            warn("ci_include_escape",
                 f"line {line}: local include {target!r} escapes the "
                 f"scan root; not followed")
            return
        if any(ch in target for ch in "*?["):
            matches = sorted(
                p for p in _glob.glob(str(resolved)) if Path(p).is_file())
            if not matches:
                warn("ci_include_missing",
                     f"line {line}: local include pattern {target!r} "
                     f"matches no files")
                return
            for match in matches:
                follow_resolved(match, line)
            return
        if not resolved.is_file():
            warn("ci_include_missing",
                 f"line {line}: local include {target!r} does not exist")
            return
        follow_resolved(resolved, line)

    def handle_include(kind, target_raw, line):
        kind = kind.strip().strip("\"'").lower()
        target = target_raw.strip().strip("\"'")
        if kind in _REMOTE_INCLUDE_KINDS:
            warn("ci_remote_include",
                 f"line {line}: {kind} include {target!r} is remote; "
                 f"not followed")
            return
        if kind == "artifact":
            warn("ci_yaml_unsupported",
                 f"line {line}: artifact include is not followed")
            return
        if kind in _IGNORED_INCLUDE_KINDS:
            return
        follow_local_include(target_raw, line)

    def handle_include_value(value, line):
        value = value.strip()
        if not value:
            return
        if value[0] in "{[":
            warn("ci_yaml_unsupported",
                 f"line {line}: inline mapping or sequence include values "
                 f"are not parsed")
            return
        if ":" in value:
            kind, _, target = value.partition(":")
            handle_include(kind, target, line)
        else:
            follow_local_include(value, line)

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("---") or raw.startswith("..."):
            current_top = None
            collecting = None
            pending_image = None
            pending_item_name = False
            job_vars = {}
            continue
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            if not tab_warned:
                warn("parse_error",
                     f"line {lineno}: tab indentation is not valid YAML; "
                     f"line skipped")
                tab_warned = True
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block_scalar_indent is not None:
            if indent > block_scalar_indent:
                continue
            block_scalar_indent = None
        content = re.sub(r"\s+#.*$", "", raw.strip()).strip()
        if not content:
            continue
        if (_ANCHOR_RE.search(content) or _ALIAS_RE.search(content)
                or _MERGE_RE.search(content)):
            if not anchors_warned:
                warn("ci_yaml_unsupported",
                     f"line {lineno}: YAML anchors, aliases, or merge keys "
                     f"are not resolved; results may be incomplete")
                anchors_warned = True
            # Anchored/aliased lines are still handled literally below.

        is_item = content == "-" or content.startswith("- ")
        body = "" if content == "-" else content[2:].strip() \
            if is_item else content
        if is_item and body in ("|", "|-", "|+", ">", ">-", ">+"):
            block_scalar_indent = indent
            continue

        if is_item:
            if collecting == "services" and indent >= collect_indent:
                if not body:
                    pending_item_name = True
                    continue
                key = body.partition(":")[0].strip()
                if key in _SERVICE_KEYS:
                    if key == "name":
                        image_value(body.partition(":")[2], lineno,
                                    services_locator)
                        pending_item_name = False
                else:
                    image_value(body, lineno, services_locator)
                    pending_item_name = False
                continue
            if collecting == "include" and indent >= collect_indent:
                if _INCLUDE_URL_RE.match(body):
                    follow_local_include(body, lineno)
                elif ":" in body:
                    kind, _, target = body.partition(":")
                    handle_include(kind, target, lineno)
                else:
                    follow_local_include(body, lineno)
                continue
            # List items elsewhere (script, stages, rules, ...) ignored.
            continue

        if ":" not in content:
            continue
        key, _, val = content.partition(":")
        key = key.strip()
        val = val.strip()
        if val in ("|", "|-", "|+", ">", ">-", ">+"):
            block_scalar_indent = indent
            if key in ("image", "services", "include"):
                warn("ci_yaml_unsupported",
                     f"line {lineno}: block scalar {key} value is not parsed")
            continue

        if indent == 0:
            current_top = key
            job_vars = {}
            collecting = None
            pending_image = None
            pending_item_name = False
            if key == "image":
                if val:
                    image_value(val, lineno, "image")
                else:
                    pending_image = (indent, "image", False)
            elif key == "services":
                collecting = "services"
                collect_indent = indent
                services_locator = "services"
                pending_item_name = False
            elif key == "variables":
                if val:
                    warn("ci_yaml_unsupported",
                         f"line {lineno}: inline variables mapping is "
                         f"not parsed")
                else:
                    collecting = "variables"
                    collect_indent = indent
                    vars_target = top_vars
            elif key == "include":
                if val:
                    handle_include_value(val, lineno)
                collecting = "include"
                collect_indent = indent
            continue

        if collecting and indent <= collect_indent:
            collecting = None

        if pending_image is not None:
            if indent <= pending_image[0]:
                pending_image = None
            else:
                if key == "name" and not pending_image[2]:
                    image_value(val, lineno, pending_image[1])
                    pending_image = (pending_image[0], pending_image[1], True)
                continue

        if pending_item_name and collecting == "services" \
                and indent > collect_indent and key == "name":
            image_value(val, lineno, services_locator)
            pending_item_name = False
            continue

        if collecting == "variables" and indent > collect_indent:
            # Captured for resolution only; never emitted into the
            # inventory.
            if val:
                vars_target[key] = val.strip("\"'")
            continue
        if collecting == "include" and indent > collect_indent:
            handle_include(key, val, lineno)
            continue
        if collecting == "services" and indent > collect_indent:
            # Other keys of a block-form service item (entrypoint, ...).
            continue

        if key == "image":
            if current_top is None:
                locator = "image"
            elif current_top == "default":
                locator = "default:image"
            else:
                locator = f"{current_top}:image"
            if val:
                image_value(val, lineno, locator)
            else:
                pending_image = (indent, locator, False)
            continue
        if key == "services":
            collecting = "services"
            collect_indent = indent
            if current_top is None:
                services_locator = "services"
            elif current_top == "default":
                services_locator = "default:services"
            else:
                services_locator = f"{current_top}:services"
            pending_item_name = False
            continue
        if key == "variables":
            if val:
                warn("ci_yaml_unsupported",
                     f"line {lineno}: inline variables mapping is not parsed")
            else:
                collecting = "variables"
                collect_indent = indent
                vars_target = (top_vars if current_top in (None, "default")
                               else job_vars)
            continue
        if key == "include":
            if val:
                handle_include_value(val, lineno)
            collecting = "include"
            collect_indent = indent
            continue
        # script:, stage:, rules:, artifacts:, and other keys ignored.

    for value, line, locator, image_job_vars in pending_emissions:
        values = dict(top_vars)
        values.update(image_job_vars)
        emit_image_record(value, rel_path, "gitlab_ci", line, locator,
                          records, warnings, values=values)
    return records, warnings
