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
from pathlib import Path

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
from .parsers.dotnet import (
    parse_csproj_records,
    parse_directory_packages_props,
    parse_global_json_records,
)
from .parsers.python import (
    parse_pipfile_lock_records,
    parse_pyproject_records,
    parse_python_version_records,
    parse_requirements_records,
    parse_runtime_txt_records,
)


def _match_globs(rel_path, patterns):
    return any(Path(rel_path).match(pat) for pat in patterns)


def _is_gitlab_ci_file(rel_path):
    """GitLab CI inputs: .gitlab-ci.yml/.yaml plus local YAML under .gitlab/."""
    name = rel_path.rsplit("/", 1)[-1]
    if name in (".gitlab-ci.yml", ".gitlab-ci.yaml"):
        return True
    return rel_path.startswith(".gitlab/") \
        and name.endswith((".yml", ".yaml"))


def _parse_python_manifest(path, rel_path):
    """Dispatch one Python manifest file to its parser by basename."""
    name = rel_path.rsplit("/", 1)[-1].lower()
    if name == "pyproject.toml":
        return parse_pyproject_records(path, rel_path)
    if name == "pipfile.lock":
        return parse_pipfile_lock_records(path, rel_path)
    if name == ".python-version":
        return parse_python_version_records(path, rel_path)
    if name == "runtime.txt":
        return parse_runtime_txt_records(path, rel_path)
    return parse_requirements_records(path, rel_path)


def _parse_dotnet_manifest(path, rel_path):
    """Dispatch one .NET project file to its parser by basename."""
    name = rel_path.rsplit("/", 1)[-1].lower()
    if name == "global.json":
        return parse_global_json_records(path, rel_path)
    if name == "directory.packages.props":
        return parse_directory_packages_props(path, rel_path)
    return parse_csproj_records(path, rel_path)


# Manifest table in ecosystem precedence order. This preserves the
# historical scan order (and therefore first-seen provenance) while
# staying deterministic. Lock files (package-lock.json,
# npm-shrinkwrap.json, packages.lock.json) are sibling-resolved by their
# parsers and are deliberately NOT discovered; Directory.Packages.props
# has its own parser and is. The `wants_root` flag marks parsers that
# receive the scan root (the GitLab CI parser needs it to keep local
# includes inside the scan root).
_MANIFEST_PATTERNS = (
    ("maven", ("pom*.xml",), parse_pom_records, False),
    ("gradle", ("*.gradle.kts", "build.gradle"), parse_gradle_records, False),
    ("npm", ("package.json",), parse_package_json_records, False),
    ("python", ("requirements*.txt", "pyproject.toml", "Pipfile.lock",
                ".python-version", "runtime.txt"),
     _parse_python_manifest, False),
    ("go", ("go.mod",), parse_go_mod_records, False),
    ("dotnet", ("*.csproj", "*.fsproj", "*.vbproj",
                "Directory.Packages.props", "global.json"),
     _parse_dotnet_manifest, False),
    ("docker", ("Dockerfile", "Dockerfile.*", "*.Dockerfile"),
     parse_dockerfile_records, False),
    ("gitlab", None, parse_gitlab_ci_records, True),
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


def scan_folder(folder, exclude=None):
    """Walk folder; return a scan result of normalized records.

    Returns a dict:
        root        absolute scan root path
        root_name   basename of the scan root
        files       sorted manifest paths relative to the root, "/"-separated
        records     normalized dependency records (deterministic order)
        warnings    structured warnings (deterministic order)

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
    warnings = []
    by_ecosystem = {eco: [] for eco, _, _, _ in _MANIFEST_PATTERNS}

    for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                followlinks=False):
        dirpath = Path(dirpath)
        rel_dir = dirpath.relative_to(root).as_posix() if dirpath != root else ""
        # Prune excluded and symlinked directories in place (deterministic).
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in DEFAULT_EXCLUDED_DIRS
            and not is_excluded(f"{rel_dir}/{d}" if rel_dir else d, patterns)
        )
        for filename in sorted(filenames):
            rel = f"{rel_dir}/{filename}" if rel_dir else filename
            if is_excluded(rel, patterns):
                continue
            for eco, patterns_group, _, _ in _MANIFEST_PATTERNS:
                if _matches(rel, patterns_group, eco):
                    by_ecosystem[eco].append(rel)
                    break

    # Ecosystem precedence order (see _MANIFEST_PATTERNS), each sorted by
    # relative path — deterministic and identical to the historical scan
    # order, so first-seen provenance is stable.
    candidates = []
    for eco, _, parser, wants_root in _MANIFEST_PATTERNS:
        for rel in sorted(by_ecosystem[eco]):
            candidates.append((rel, root / rel, parser, wants_root))

    if len(candidates) > MAX_FILES:
        raise SystemExit(
            f"Refusing to scan {len(candidates)} manifest files "
            f"(limit {MAX_FILES}). Add excludes or scan a narrower folder.")

    for rel, abs_path, parser, wants_root in candidates:
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
        # A symlinked file pointing outside the scan root is never emitted.
        if abs_path.is_symlink() and not _is_within(root, abs_path):
            warnings.append(new_warning(
                "escaped_symlink", rel,
                "symlink target lies outside the scan root; skipped"))
            continue

        if wants_root:
            file_records, file_warnings = parser(abs_path, rel, root)
        else:
            file_records, file_warnings = parser(abs_path, rel)
        records.extend(file_records)
        warnings.extend(file_warnings)
        files.append(rel)

    files.sort()
    return {
        "root": str(root.resolve()),
        "root_name": root.resolve().name or root.name,
        "files": files,
        "records": records,
        "warnings": warnings,
    }
