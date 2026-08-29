"""Dockerfile FROM-instruction parser (normalized image records).

Pure stdlib; never runs Docker and never touches the network.

Scope: only `FROM` instructions are examined. Multi-stage builds,
`--platform` flags, stage aliases (`AS <name>`), tags, digests, and
simple `ARG NAME=default` substitution are supported. Continuation
lines (trailing backslash) are joined before parsing.

ARG scoping: only ARGs declared before the first FROM are global and
usable in FROM lines; stage-local ARGs (declared after a FROM) are
never visible to later FROM lines, so they never resolve references.

Records:   one per external, tagged image:
               kind="image", ecosystem="container",
               name=<registry-free repository path>,
               version=<tag>
           The provenance locator preserves the original reference
           text ("FROM python:${VERSION}-slim").

Warnings:  scratch images and stage-alias reuse are skipped silently;
           untagged/latest images, digest-only references, and
           unresolved variables each produce a warning and no record.
"""

import re
from pathlib import Path

from ..models import add_location, new_record, new_warning

# Registry hosts stripped during name normalization; the remaining
# repository path is the stable identity used for lifecycle mapping.
KNOWN_REGISTRY_HOSTS = frozenset({
    "docker.io", "index.docker.io", "registry-1.docker.io",
    "mcr.microsoft.com", "ghcr.io", "quay.io", "gcr.io",
    "public.ecr.aws", "registry.gitlab.com",
    "registry.k8s.io", "k8s.gcr.io",
})

_FROM_RE = re.compile(r"^FROM\s+(.*)$", re.IGNORECASE)
_ARG_RE = re.compile(r"^ARG\s+(\w+)(?:=(.*))?$", re.IGNORECASE)
_PLATFORM_FLAG_RE = re.compile(r"--platform=\S+", re.IGNORECASE)
_AS_RE = re.compile(r"\s+AS\s+(\S+)\s*$", re.IGNORECASE)
_VAR_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)")


def resolve_variables(text, values):
    """Substitute $VAR, ${VAR}, and ${VAR:-default} from values.

    Returns (text, missing_names): missing_names lists variables that
    had neither a value nor a default, in first-seen order. An empty
    value counts as unset, matching Docker's ARG semantics.
    """
    missing = []

    def _sub(match):
        name = match.group(1) or match.group(3)
        default = match.group(2)
        value = values.get(name)
        if value:
            return value
        if default is not None:
            return default
        missing.append(name)
        return match.group(0)

    return _VAR_RE.sub(_sub, text), missing


def split_image_reference(ref):
    """'registry:5000/img:tag@sha256:...' -> (repo, tag, digest).

    A colon before the first slash is a registry port, not a tag
    separator; the digest (if any) is split off first.
    """
    digest = None
    if "@" in ref:
        ref, _, digest = ref.partition("@")
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1:], digest
    return ref, None, digest


def image_identity(repo):
    """Return (identity, registry, repository) without losing provenance.

    Only Docker Hub official-library images receive the familiar short
    identity (``python``). Other registries remain part of the identity so a
    lookalike such as ``ghcr.io/library/python`` cannot inherit the official
    Python lifecycle mapping.
    """
    parts = repo.split("/")
    first = parts[0].lower()
    explicit_registry = (len(parts) > 1 and
                         ("." in first or ":" in first or first == "localhost"))
    if explicit_registry:
        registry = first
        repository = "/".join(parts[1:])
    else:
        registry = "docker.io"
        repository = repo if len(parts) > 1 else f"library/{repo}"
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io") \
            and repository.startswith("library/"):
        identity = repository[len("library/"):]
    else:
        identity = f"{registry}/{repository}"
    return identity, registry, repository


def normalize_image_name(repo):
    """Human-friendly repository name; full identity is stored separately."""
    parts = repo.split("/")
    if len(parts) > 1 and parts[0].lower() in KNOWN_REGISTRY_HOSTS:
        parts = parts[1:]
    if len(parts) > 1 and parts[0] == "library":
        parts = parts[1:]
    return "/".join(parts)


def emit_image_record(ref, rel_path, manifest, line, locator,
                      records, warnings, values=None):
    """Resolve, classify, and record one image reference.

    Digest-only and latest/untagged references yield warnings instead
    of records; references with unresolvable variables yield a warning
    and no record. Recognized tagged references become normalized
    container records.
    """
    resolved = ref
    if "$" in resolved:
        resolved, missing = resolve_variables(resolved, values or {})
        if missing or "$" in resolved:
            warnings.append(new_warning(
                "unresolved_variable", rel_path,
                f"line {line}: image {ref!r} references variables "
                f"with no resolvable value"))
            return
    repo, tag, digest = split_image_reference(resolved)
    identity, registry, repository = image_identity(repo)
    name = normalize_image_name(repo)
    record = new_record("container", name, version=tag, kind="image")
    record.update({
        "image_reference": resolved,
        "image_identity": identity,
        "registry": registry,
        "repository": repository,
        "tag": tag,
        "digest": digest,
    })
    add_location(record, rel_path, manifest, line=line, locator=locator)
    records.append(record)
    if digest:
        warnings.append(new_warning(
            "digest_reference", rel_path,
            f"line {line}: image {resolved!r} is pinned by digest; "
            f"no release cycle can be derived"))
        return
    if tag is None or tag.lower() == "latest":
        why = ("uses the latest tag" if tag is not None
               else "has no tag (implicitly latest)")
        warnings.append(new_warning(
            "latest_tag", rel_path,
            f"line {line}: image {resolved!r} {why}"))
        return


def parse_dockerfile_records(path, rel_path):
    """Parse a Dockerfile; return (records, warnings)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path, f"could not read Dockerfile: {exc}")]
    return _parse_dockerfile_text(text, rel_path)


def _join_continuations(text):
    """Join backslash-continued lines; keep the first physical line no."""
    logical = []
    pending = None
    start_line = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        chunk = raw.rstrip()
        if pending is None:
            start_line = lineno
            pending = chunk
        else:
            pending += " " + chunk
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip() + " "
            continue
        logical.append((start_line, pending))
        pending = None
    if pending is not None:
        logical.append((start_line, pending))
    return logical


def _parse_dockerfile_text(text, rel_path):
    records = []
    warnings = []
    args = {}
    aliases = set()
    seen_from = False

    for start_line, line in _join_continuations(text):
        code = line.strip()
        if not code or code.startswith("#"):
            continue
        upper = code.split(None, 1)[0].upper()

        if upper == "ARG":
            match = _ARG_RE.match(code)
            if match and not seen_from:
                value = match.group(2)
                if value is not None:
                    value = value.strip("\"'") or None
                args[match.group(1)] = value
            continue
        if upper != "FROM":
            continue
        seen_from = True

        rest = _FROM_RE.match(code).group(1)
        rest = _PLATFORM_FLAG_RE.sub("", rest)
        rest = re.sub(r"\s+#.*$", "", rest).strip()
        alias = None
        as_match = _AS_RE.search(rest)
        if as_match:
            alias = as_match.group(1)
            rest = rest[:as_match.start()].strip()
        if not rest:
            continue

        repo, _, _ = split_image_reference(rest)
        if repo.lower() in aliases or repo.lower() == "scratch":
            if alias:
                aliases.add(alias.lower())
            continue
        emit_image_record(
            rest, rel_path, "dockerfile", start_line, f"FROM {rest}",
            records, warnings, values=args)
        if alias:
            aliases.add(alias.lower())

    return records, warnings
