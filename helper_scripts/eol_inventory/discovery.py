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
    parse_gradle_records,
    parse_package_json_records,
    parse_pom_records,
)

# Manifest patterns in ecosystem precedence order: POMs, then Gradle
# files, then package.json. This preserves the historical scan order (and
# therefore the first-seen provenance) while staying deterministic.
_MANIFEST_PATTERNS = (
    ("maven", ("pom*.xml",), parse_pom_records),
    ("gradle", ("*.gradle.kts", "build.gradle"), parse_gradle_records),
    ("npm", ("package.json",), parse_package_json_records),
)


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
    by_ecosystem = {eco: [] for eco, _, _ in _MANIFEST_PATTERNS}

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
            for eco, group_patterns, parser in _MANIFEST_PATTERNS:
                if any(Path(rel).match(pat) for pat in group_patterns):
                    by_ecosystem[eco].append((rel, dirpath / filename, parser))
                    break

    # Ecosystem precedence order (maven, gradle, npm), each sorted by
    # relative path — deterministic and identical to the historical
    # scan order, so first-seen provenance is stable.
    candidates = []
    for eco, _, _ in _MANIFEST_PATTERNS:
        candidates.extend(sorted(by_ecosystem[eco], key=lambda item: item[0]))

    if len(candidates) > MAX_FILES:
        raise SystemExit(
            f"Refusing to scan {len(candidates)} manifest files "
            f"(limit {MAX_FILES}). Add excludes or scan a narrower folder.")

    for rel, abs_path, parser in candidates:
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
