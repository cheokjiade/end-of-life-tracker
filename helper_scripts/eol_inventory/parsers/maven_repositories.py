"""Declared artifact-repository collection for Maven and Gradle projects.

The runtime reads a top-level ``maven_repositories`` list and falls back to
those hosts when an artifact is not on Maven Central (see
``eoltracker/validation.py`` and ``eoltracker/handler.py``). This module
collects the URLs a project declares: root-level ``<repositories>`` in a POM
and dependency ``repositories { ... }`` blocks in ``build.gradle(.kts)`` and
``settings.gradle(.kts)``.

Every parser returns ``(urls, warnings)``: order-stable, deduplicated URLs and
scan warnings in the shared ``models.new_warning`` shape.
"""

import re
from pathlib import Path

from ..models import load_safe_xml, new_warning

_POM_NS = "{http://maven.apache.org/POM/4.0.0}"

# Repository URL declarations inside a `repositories { ... }` block, in the
# three forms Gradle accepts: `url = uri("...")` (Kotlin DSL and Groovy),
# `url = "..."` (direct assignment), and the Groovy shorthand `url "..."`.
_GRADLE_REPO_URL_RE = re.compile(
    r'\burl\s*(?:=\s*uri\s*\(\s*|=\s*|\s+)'
    r'([\'"])([^\'"]+)\1\s*\)?'
)
# The keyword opening the block whose `{` was just reached (dotted names
# allowed, e.g. project.repositories); used while scanning nesting.
_GRADLE_BLOCK_NAME_RE = re.compile(r"([A-Za-z_][\w.]*)\s*\{\Z")


def _strip_gradle_comments(text):
    """Remove // line comments and /* ... */ block comments from gradle
    sources, leaving quoted strings untouched (Groovy and Kotlin share the
    same comment and quoting grammar for this purpose).

    A '//' inside a string literal must survive — dependency coordinates and
    maven { url = uri("https://...") } blocks — and, conversely, an
    apostrophe inside a comment ("don't ship this") must not open a string
    that swallows following code. Backslash escapes inside strings are
    honoured. Line comments are removed up to (and keeping) the newline;
    block comments collapse to a single space, since every pattern below
    spans whitespace. Unterminated comments run to end of text.
    """
    out = []
    i = 0
    n = len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and text[i + 1:i + 2] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue
        if ch == "/" and text[i + 1:i + 2] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                break
            out.append(" ")
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def repositories_blocks(text):
    """Yield (body, excluded) for every `repositories { ... }` block.

    One quote-aware pass over the text tracks which keyword opened each
    brace, so braces inside string literals never break the nesting count
    and nested blocks (maven { url = uri("...") }) stay contained within
    their parent's body. Blocks are matched on the captured name: plain
    `repositories` and dotted spellings (`project.repositories`) count.
    *excluded* reports why the block is NOT a dependency-repository
    source: it is enclosed by `publishing` (a deployment target) or
    `pluginManagement` (plugin repositories, not dependency repos) —
    whether as an ancestor block or via the dotted spelling itself
    (`publishing.repositories { ... }`). buildscript repositories are
    dependency repositories (classpath deps resolve from them) and are
    yielded normally. An unterminated block yields nothing: a block is
    recorded only when its closing brace is seen, so a truncated file
    silently drops the incomplete tail rather than emit a wrong URL.
    """
    stack = []      # (block keyword, index just past its `{`)
    blocks = []
    i = 0
    n = len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch == "{":
            m = _GRADLE_BLOCK_NAME_RE.search(text[max(0, i - 64):i + 1])
            stack.append((m.group(1) if m else "", i + 1))
            i += 1
            continue
        if ch == "}" and stack:
            name, start = stack.pop()
            if name == "repositories" or name.endswith(".repositories"):
                # Dependency-repository exclusions consider both the
                # enclosing block keywords and the dotted spelling's own
                # qualifier prefix: publishing (deployment target) and
                # pluginManagement (plugin repos) never declare
                # dependency repositories.
                qualifiers = [
                    segment for keyword, _pos in stack
                    for segment in keyword.split(".")
                ]
                if name.endswith(".repositories"):
                    qualifiers += name[: -len(".repositories")].split(".")
                excluded = any(
                    segment in ("publishing", "pluginManagement")
                    for segment in qualifiers)
                blocks.append((text[start:i], excluded))
        i += 1
    return blocks


def gradle_repo_urls(text):
    """Artifact-repository URLs declared in dependency `repositories` blocks.

    Matches `url = uri("...")`, `url = "..."`, and `url "..."` in every
    non-excluded `repositories { ... }` block — publishing (deployment
    targets) and pluginManagement (plugin repos) blocks are not dependency
    sources, see :func:`repositories_blocks`. mavenCentral(),
    mavenLocal() and google() declare no URL and yield nothing. Pure;
    order-stable and deduplicated.
    """
    urls = []
    for body, excluded in repositories_blocks(text):
        if excluded:
            continue
        for m in _GRADLE_REPO_URL_RE.finditer(body):
            url = m.group(2).strip()
            if url and url not in urls:
                urls.append(url)
    return urls


def parse_gradle_repositories(path, rel_path):
    """Return ``(urls, warnings)`` for a gradle build or settings file.

    Comments are stripped first (a commented-out repositories block is
    ignored); see :func:`gradle_repo_urls` for the matched forms and the
    publishing/pluginManagement exclusions.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [new_warning(
            "unreadable_file", rel_path,
            f"could not read Gradle file: {exc}")]
    return gradle_repo_urls(_strip_gradle_comments(text)), []


def parse_pom_repositories(path, rel_path):
    """Return ``(urls, warnings)`` for a POM's declared repositories.

    Only direct ``<repositories>`` children of the project root count:
    profile-conditional repositories are skipped, because whether such a
    profile is active is unknowable from the manifest alone.
    """
    root, warning = load_safe_xml(path, rel_path, "POM")
    if root is None:
        return [], [warning]
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    repos = []
    repos_node = root.find(f"{ns}repositories")
    if repos_node is not None:
        for repo in repos_node.findall(f"{ns}repository"):
            url_node = repo.find(f"{ns}url")
            if url_node is None:
                url_node = repo.find("url")
            url = (url_node.text or "").strip() if url_node is not None else ""
            if url and url not in repos:
                repos.append(url)
    return repos, []


def parse_settings_gradle(path, rel_path, root=None, scan_state=None):
    """Discovery hook for ``settings.gradle(.kts)``: repositories only.

    Modern Gradle declares dependency repositories in the settings file
    under ``dependencyResolutionManagement { repositories { ... } }``
    (``pluginManagement`` repositories are plugin repos and stay
    excluded). Settings files declare no dependencies, so this parser
    returns no records; the URLs it finds are recorded in *scan_state*
    for :func:`eol_inventory.discovery.scan_folder` to aggregate.
    """
    urls, warnings = parse_gradle_repositories(path, rel_path)
    if scan_state is not None:
        state = scan_state.setdefault("gradle", {})
        collected = state.setdefault("repositories", [])
        for url in urls:
            if url not in collected:
                collected.append(url)
    return [], warnings
