"""Deterministic folder walking and manifest discovery.

Discovery walks the scan root with os.walk (top-down, never following
directory symlinks), prunes default and user-supplied exclusions, and
parses manifests into normalized records. File order is fully sorted so
results are identical across platforms and runs.

Never follows manifest includes or symlinks outside the scan root; paths
outside the scan root are never emitted. File-size and total-file guards
prevent accidental huge scans.
"""

import os
import stat
from pathlib import Path, PurePosixPath

from .models import (
    DEFAULT_EXCLUDED_DIRS,
    MAX_FILES,
    MAX_FILE_BYTES,
    is_excluded,
    load_ignore_patterns,
    new_warning,
)
from .parsers import (
    parse_dockerfile_records,
    parse_gitlab_ci_records,
    parse_go_mod_records,
    parse_gradle_records,
    parse_package_json_records,
    parse_pom_records,
)
from .parsers.maven_repositories import (
    parse_gradle_repositories,
    parse_pom_repositories,
    parse_settings_gradle,
)
from .parsers.node import parse_nvmrc_records
from .parsers.dotnet import parse_csproj_records, parse_global_json_records
from .parsers.python import (
    parse_pipfile_records,
    parse_pipfile_lock_records,
    parse_pyproject_records,
    parse_python_version_records,
    parse_requirements_records,
    parse_runtime_txt_records,
)


def _match_globs(rel_path, patterns):
    return any(PurePosixPath(rel_path).match(pat) for pat in patterns)


def _is_gitlab_ci_file(rel_path):
    """GitLab CI inputs: .gitlab-ci.yml/.yaml plus local YAML under .gitlab/."""
    name = rel_path.rsplit("/", 1)[-1]
    if name in (".gitlab-ci.yml", ".gitlab-ci.yaml"):
        return True
    return rel_path.startswith(".gitlab/") \
        and name.endswith((".yml", ".yaml"))


def _parse_python_manifest(path, rel_path, root, scan_state=None):
    """Dispatch one Python manifest file to its parser by basename."""
    name = rel_path.rsplit("/", 1)[-1].lower()
    if name == "pyproject.toml":
        return parse_pyproject_records(path, rel_path)
    if name == "pipfile.lock":
        return parse_pipfile_lock_records(path, rel_path)
    if name == "pipfile":
        state = None if scan_state is None else scan_state.setdefault(
            "pipfile", {"locks": set()})
        return parse_pipfile_records(
            path, rel_path, root=root,
            consumed_locks=None if state is None else state["locks"])
    if name == ".python-version":
        return parse_python_version_records(path, rel_path)
    if name == "runtime.txt":
        return parse_runtime_txt_records(path, rel_path)
    state = None if scan_state is None else scan_state.setdefault(
        "requirements", {"visited": set(), "manifests": set()})
    return parse_requirements_records(
        path, rel_path, root=root, include_state=state)


def _parse_node_manifest(path, rel_path, root, scan_state=None):
    if rel_path.rsplit("/", 1)[-1].lower() == ".nvmrc":
        return parse_nvmrc_records(path, rel_path)
    state = None if scan_state is None else scan_state.setdefault(
        "npm", {"locks": set()})
    return parse_package_json_records(
        path, rel_path, root=root,
        consumed_locks=None if state is None else state["locks"])


def _parse_dotnet_manifest(path, rel_path, root, scan_state=None):
    """Dispatch one .NET project file to its parser by basename."""
    name = rel_path.rsplit("/", 1)[-1].lower()
    if name == "global.json":
        return parse_global_json_records(path, rel_path)
    state = None if scan_state is None else scan_state.setdefault(
        "dotnet", {"sidecars": set()})
    return parse_csproj_records(
        path, rel_path, root=root,
        consumed=None if state is None else state["sidecars"])


def _parse_gitlab_manifest(path, rel_path, root, scan_state=None):
    state = None if scan_state is None else scan_state.setdefault(
        "gitlab", {"files": 0, "visited": set(), "manifests": set()})
    return parse_gitlab_ci_records(
        path, rel_path, root=root, include_state=state)


# Manifest table in ecosystem precedence order. This preserves the
# historical scan order (and therefore first-seen provenance) while
# staying deterministic. Lock files (package-lock.json,
# npm-shrinkwrap.json, packages.lock.json) and Directory.Packages.props
# are sibling/nearest-sidecar evidence resolved by their parsers and are
# deliberately NOT discovered as candidates; sidecars the parsers
# actually read are merged into the file list afterwards (see the
# consumed-sidecar merge at the end of scan_folder). The `wants_root`
# flag marks parsers that receive the scan root (the GitLab CI parser
# needs it to keep local includes inside the scan root); `wants_state`
# carries scanner-wide include de-duplication, budgets, and consumed-
# sidecar tracking.
_MANIFEST_PATTERNS = (
    ("maven", ("pom*.xml",), parse_pom_records, False, False),
    # settings.gradle(.kts) declares dependency repositories (and, rarely,
    # buildscript classpath dependencies); its row precedes the gradle row
    # so the settings spellings dispatch here (the first matching row
    # wins) and parse_settings_gradle runs the ordinary Gradle record scan
    # in addition to the repository scan. The ecosystem key is the dispatch
    # bucket and must be unique per row, hence "gradle_settings" rather
    # than a second "gradle" row: two rows sharing a key would parse every
    # gradle file twice.
    ("gradle_settings", ("settings.gradle", "settings.gradle.kts"),
     parse_settings_gradle, True, True),
    ("gradle", ("*.gradle.kts", "*.gradle"), parse_gradle_records,
     False, False),
    ("npm", ("package.json", ".nvmrc"), _parse_node_manifest, True, True),
    ("python", ("requirements*.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
                ".python-version", "runtime.txt"),
     _parse_python_manifest, True, True),
    ("go", ("go.mod",), parse_go_mod_records, False, False),
    ("dotnet", ("*.csproj", "*.fsproj", "*.vbproj", "global.json"),
     _parse_dotnet_manifest, True, True),
    ("docker", ("Dockerfile", "Dockerfile.*", "*.Dockerfile"),
     parse_dockerfile_records, False, False),
    ("gitlab", None, _parse_gitlab_manifest, True, True),
)


def _matches(rel_path, patterns, ecosystem):
    if patterns is not None:
        return _match_globs(rel_path, patterns)
    return _is_gitlab_ci_file(rel_path)


def _is_within(root, candidate):
    """True when candidate resolves inside root."""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_directory_link(candidate):
    """True for directory symlinks and Windows junctions/reparse links."""
    if candidate.is_symlink() or (
            hasattr(candidate, "is_junction") and candidate.is_junction()):
        return True
    try:
        attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def scan_folder(folder, exclude=None):
    """Walk folder; return a scan result of normalized records.

    Returns a dict:
        root        absolute scan root path
        root_name   basename of the scan root
        files       sorted manifest paths relative to the root, "/"-separated
        records     normalized dependency records (deterministic order)
        warnings    structured warnings (deterministic order)
        maven_repositories
                    declared artifact-repository URLs (discovery order,
                    deduplicated)

    exclude: optional extra exclusion patterns (same syntax as .eolignore).
    """
    root = Path(folder)
    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")

    warnings = []
    ignore_patterns = load_ignore_patterns(root, warnings)
    patterns = list(ignore_patterns) + list(exclude or ())

    records = []
    files = []
    by_ecosystem = {eco: [] for eco, _, _, _, _ in _MANIFEST_PATTERNS}

    for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                followlinks=False):
        dirpath = Path(dirpath)
        rel_dir = dirpath.relative_to(root).as_posix() if dirpath != root else ""
        # Prune excluded and linked directories in place (deterministic).
        kept_dirs = []
        for dirname in sorted(dirnames):
            rel = f"{rel_dir}/{dirname}" if rel_dir else dirname
            if (dirname in DEFAULT_EXCLUDED_DIRS
                    or is_excluded(rel, patterns)):
                continue
            candidate = dirpath / dirname
            if _is_directory_link(candidate) or not _is_within(root, candidate):
                warnings.append(new_warning(
                    "escaped_symlink", rel,
                    "directory link is not followed by the scanner"))
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            rel = f"{rel_dir}/{filename}" if rel_dir else filename
            if is_excluded(rel, patterns):
                continue
            for eco, patterns_group, _, _, _ in _MANIFEST_PATTERNS:
                if _matches(rel, patterns_group, eco):
                    by_ecosystem[eco].append(rel)
                    break

    # Ecosystem precedence order (see _MANIFEST_PATTERNS), each sorted by
    # relative path — deterministic and identical to the historical scan
    # order, so first-seen provenance is stable.
    candidates = []
    for eco, _, parser, wants_root, wants_state in _MANIFEST_PATTERNS:
        for rel in sorted(by_ecosystem[eco]):
            candidates.append(
                (eco, rel, root / rel, parser, wants_root, wants_state))

    if len(candidates) > MAX_FILES:
        raise SystemExit(
            f"Refusing to scan {len(candidates)} manifest files "
            f"(limit {MAX_FILES}). Add excludes or scan a narrower folder.")

    scan_state = {}
    maven_repositories = []
    for eco, rel, abs_path, parser, wants_root, wants_state in candidates:
        try:
            size = abs_path.stat().st_size
        except OSError as exc:
            warnings.append(new_warning(
                "unreadable_file", rel, f"could not stat file: {exc}"))
            continue
        if size > MAX_FILE_BYTES:
            warnings.append(new_warning(
                "oversize_input", rel,
                f"file exceeds {MAX_FILE_BYTES} byte limit ({size} bytes); skipped"))
            continue
        # A path resolving outside the scan root is never emitted.
        if not _is_within(root, abs_path):
            warnings.append(new_warning(
                "escaped_symlink", rel,
                "symlink target lies outside the scan root; skipped"))
            continue

        try:
            if wants_state:
                file_records, file_warnings = parser(
                    abs_path, rel, root, scan_state)
            elif wants_root:
                file_records, file_warnings = parser(abs_path, rel, root)
            else:
                file_records, file_warnings = parser(abs_path, rel)
        except RecursionError:
            file_records = []
            file_warnings = [new_warning(
                "parse_error", rel,
                "manifest nesting/include depth exceeded the safe parser limit")]
        except Exception as exc:
            # A single malformed manifest must not erase valid evidence from
            # every other file in the project.
            file_records = []
            file_warnings = [new_warning(
                "parse_error", rel,
                f"manifest parser failed safely: {type(exc).__name__}: {exc}")]
        records.extend(file_records)
        warnings.extend(file_warnings)
        files.append(rel)

        # Declared artifact repositories: the runtime's fallback hosts for
        # artifacts that are not on Maven Central. Collected from the same
        # (path, rel_path) pair the record parser just read; settings files
        # report theirs through scan_state instead (see parse_settings_gradle).
        if eco in ("maven", "gradle"):
            repo_urls, repo_warnings = (
                parse_pom_repositories(abs_path, rel) if eco == "maven"
                else parse_gradle_repositories(abs_path, rel))
            warnings.extend(repo_warnings)
            for url in repo_urls:
                if url not in maven_repositories:
                    maven_repositories.append(url)

    # Sidecar manifests the scan consumed while resolving or enriching
    # declarations (requirements includes, sibling Pipfile/npm lock
    # files, .NET central/lock sidecars, followed GitLab CI local
    # includes) join the manifest list even when they yield no records
    # or warnings of their own. Files merely present but never read
    # stay unlisted; failed reads surface as warnings instead.
    consumed = set()
    for state in (scan_state.get("requirements", {}),
                  scan_state.get("pipfile", {}), scan_state.get("npm", {}),
                  scan_state.get("dotnet", {}), scan_state.get("gitlab", {})):
        for field in ("manifests", "locks", "sidecars"):
            consumed.update(state.get(field, ()))
    files = sorted(set(files) | consumed)
    for url in scan_state.get("gradle", {}).get("repositories", ()):
        if url not in maven_repositories:
            maven_repositories.append(url)
    return {
        "root": str(root.resolve()),
        "root_name": root.resolve().name or root.name,
        "files": files,
        "records": records,
        "warnings": warnings,
        "maven_repositories": maven_repositories,
    }
