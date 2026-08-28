"""Build and verify the allowlisted AWS Lambda deployment artifact.

This replaces the former denylist packaging (``data "archive_file"`` archiving
the repository root minus a hand-maintained exclusion list), which could pull
untracked secrets or unrelated future files into the deployed ZIP (audit
finding S-01, ``docs/audits/2026-08-27-security-risk-audit.md``).

The artifact is an allowlist, strictly:

- ``lambda_function.py``
- every Git-tracked ``*.py`` under ``eoltracker/``

Everything else is structurally excluded because it is never collected.
Known compiled/junk artifacts (``__pycache__/``, ``*.pyc``, ``*.pyo``) are
skipped so builds stay predictable in dirty development trees; any other
non-Python file or untracked Python file found under ``eoltracker/`` is refused
outright (fail closed) rather than skipped, so unknown junk can never enter the
artifact unnoticed. Packaging must run from the root of a Git worktree.

Outputs (under ``terraform/build/``):

- ``lambda.zip``      deterministic artifact: sorted entries, fixed timestamps
- ``manifest.json``   SHA-256 of every input, entry, and the whole ZIP

Terraform refuses to apply unless the ZIP matches this manifest and the
manifest matches the currently checked-out runtime sources, so a stale,
partial, or foreign artifact cannot be deployed. Run::

    python build_lambda_package.py build    # before terraform plan/apply
    python build_lambda_package.py verify   # re-checks it, network-free

Verification is fully offline (hashlib + zipfile only).

Stdlib only. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

SCHEMA_VERSION = 1

# The runtime surface, relative to the repository root. Everything not
# reachable from these roots never enters the artifact.
TOP_LEVEL_RUNTIME_FILES = ["lambda_function.py"]
RUNTIME_PACKAGE_DIR = "eoltracker"

# Compiled/junk artifacts that may legitimately exist in a dirty working tree.
# They are never runtime code and are skipped rather than treated as an error.
IGNORED_SUFFIXES = {".pyc", ".pyo"}
IGNORED_PARTS = {"__pycache__"}

BUILD_SUBDIR = Path("terraform") / "build"
ARTIFACT_NAME = "lambda.zip"
MANIFEST_NAME = "manifest.json"
ARTIFACT_RELPATH = str(BUILD_SUBDIR / ARTIFACT_NAME).replace("\\", "/")

# Fixed metadata keeps byte-for-byte reproducibility across rebuilds.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_EXTERNAL_ATTR = 0o100644 << 16


class PackagingError(Exception):
    """A fail-closed condition detected while collecting runtime sources."""


def _is_reparse_point(path: Path) -> bool:
    """True for Windows junctions and other filesystem reparse points."""
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError as exc:
        raise PackagingError(f"cannot inspect runtime path {path}: {exc}") from exc
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and attrs & marker)


def _reject_link(path: Path, description: str) -> None:
    """Reject symlinks, reparse-point aliases, and hard-link aliases."""
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise PackagingError(f"cannot inspect runtime path {path}: {exc}") from exc
    if path.is_symlink() or _is_reparse_point(path):
        raise PackagingError(f"refusing linked/reparse-point {description}: {path}")
    # POSIX directories normally have multiple links (``.`` and each child
    # directory's ``..``). Only regular files can be smuggled into the package
    # through a hard-link alias; directory aliases are covered by the symlink
    # and reparse-point checks above.
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
        raise PackagingError(f"refusing hard-linked {description}: {path}")


def _assert_exact_path_case(repo_root: Path, relative_path: Path) -> None:
    """Reject case-only aliases for an expected repository-relative path.

    ``Path.exists`` follows the filesystem's case-folding rules on Windows and
    macOS. Walk directory entries instead so the ZIP member casing, Git index,
    and Python import path cannot silently disagree.
    """
    parent = repo_root
    for part in relative_path.parts:
        try:
            names = [entry.name for entry in os.scandir(parent)]
        except OSError as exc:
            raise PackagingError(f"cannot inspect runtime path casing under {parent}: {exc}") from exc
        if part in names:
            parent = parent / part
            continue
        aliases = sorted(name for name in names if name.casefold() == part.casefold())
        if aliases:
            raise PackagingError(
                f"runtime path casing differs from the allowlist: expected "
                f"{relative_path.as_posix()!r}, found component {aliases[0]!r}"
            )
        return


def _git_tracked_runtime_paths(repo_root: Path) -> set[str]:
    """Return runtime paths recorded by Git, failing closed outside a repo."""
    try:
        root_result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise PackagingError(f"cannot verify Git-tracked runtime sources: {exc}") from exc
    if root_result.returncode != 0:
        raise PackagingError(
            "cannot verify Git-tracked runtime sources: repository root is not a Git worktree"
        )

    git_root = Path(root_result.stdout.strip()).resolve()
    try:
        same_root = os.path.samefile(repo_root, git_root)
    except OSError:
        same_root = os.path.normcase(str(repo_root)) == os.path.normcase(str(git_root))
    if not same_root:
        raise PackagingError(
            f"packaging root must be the Git worktree root (got {repo_root}, Git root is {git_root})"
        )

    tracked_result = subprocess.run(
        [
            "git", "-C", str(repo_root), "ls-files", "-z", "--",
            *TOP_LEVEL_RUNTIME_FILES, RUNTIME_PACKAGE_DIR,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tracked_result.returncode != 0:
        raise PackagingError("cannot enumerate Git-tracked runtime sources")
    return {
        os.fsdecode(raw).replace("\\", "/")
        for raw in tracked_result.stdout.split(b"\0")
        if raw
    }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect_runtime_sources(repo_root: Path) -> List[str]:
    """Return the sorted POSIX-style allowlisted runtime file paths.

    Raises :class:`PackagingError` when something non-runtime and unexpected
    sits under the package directory, or when a required runtime root is
    missing.
    """
    repo_root = repo_root.resolve()
    tracked_paths = _git_tracked_runtime_paths(repo_root)

    collected: List[str] = []
    for name in TOP_LEVEL_RUNTIME_FILES:
        _assert_exact_path_case(repo_root, Path(name))
        path = repo_root / name
        _reject_link(path, "runtime file")
        if not path.is_file():
            raise PackagingError(f"required runtime file is missing: {name}")
        collected.append(name)

    pkg_dir = repo_root / RUNTIME_PACKAGE_DIR
    _assert_exact_path_case(repo_root, Path(RUNTIME_PACKAGE_DIR))
    _reject_link(pkg_dir, "runtime package")
    if not pkg_dir.is_dir():
        raise PackagingError(f"required runtime package is missing: {RUNTIME_PACKAGE_DIR}/")

    try:
        package_paths = sorted(pkg_dir.rglob("*"))
    except OSError as exc:
        raise PackagingError(
            f"cannot traverse runtime package {RUNTIME_PACKAGE_DIR}/: {exc}"
        ) from exc

    for path in package_paths:
        rel_parts = path.relative_to(repo_root).parts
        _reject_link(path, "path under runtime package")
        if any(part.casefold() in IGNORED_PARTS for part in rel_parts):
            continue
        package_parts = path.relative_to(pkg_dir).parts
        if any(part.startswith(".") for part in package_parts):
            rel = path.relative_to(repo_root).as_posix()
            raise PackagingError(
                f"refusing hidden path under {RUNTIME_PACKAGE_DIR}/: {rel}"
            )
        if path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        if not path.is_file():
            continue

        rel = path.relative_to(repo_root).as_posix()
        if path.suffix != ".py":
            raise PackagingError(
                f"refusing unexpected non-runtime file under {RUNTIME_PACKAGE_DIR}/: {rel}"
                " (runtime ships only lambda_function.py and eoltracker/**.py;"
                " remove it or teach build_lambda_package.py about deliberate additions)"
            )
        if rel not in tracked_paths:
            raise PackagingError(f"refusing untracked runtime source: {rel}")
        collected.append(rel)

    if len(collected) <= len(TOP_LEVEL_RUNTIME_FILES):
        raise PackagingError(f"no runtime modules found under {RUNTIME_PACKAGE_DIR}/")

    collected_set = set(collected)
    tracked_allowlist = {
        rel for rel in tracked_paths
        if rel in TOP_LEVEL_RUNTIME_FILES
        or (
            rel.startswith(f"{RUNTIME_PACKAGE_DIR}/")
            and rel.endswith(".py")
            and not any(part.casefold() in IGNORED_PARTS for part in Path(rel).parts)
        )
    }
    if collected_set != tracked_allowlist:
        missing = sorted(tracked_allowlist - collected_set)
        extra = sorted(collected_set - tracked_allowlist)
        raise PackagingError(
            "runtime source set differs from Git-tracked allowlist"
            f" (missing={missing}, untracked={extra})"
        )

    return sorted(collected)


def _write_deterministic_zip(zip_path: Path, repo_root: Path, sources: List[str]) -> None:
    tmp_path = zip_path.with_name(zip_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for rel in sources:  # already sorted -> stable member order
                info = zipfile.ZipInfo(rel, date_time=FIXED_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = FIXED_EXTERNAL_ATTR
                info.create_system = 0
                zf.writestr(info, (repo_root / rel).read_bytes())
        tmp_path.replace(zip_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def build(repo_root: Path | None = None, build_dir: Path | None = None) -> Dict[str, object]:
    """Build the artifact and manifest. Returns the manifest dict."""
    repo_root = (repo_root or Path(__file__).resolve().parent).resolve()
    build_dir = (build_dir or repo_root / BUILD_SUBDIR).resolve()
    zip_path = build_dir / ARTIFACT_NAME
    manifest_path = build_dir / MANIFEST_NAME

    sources = collect_runtime_sources(repo_root)

    input_hashes: Dict[str, str] = {}
    entry_hashes: Dict[str, str] = {}
    for rel in sources:
        data = (repo_root / rel).read_bytes()
        input_hashes[rel] = sha256_bytes(data)
        entry_hashes[rel] = input_hashes[rel]

    build_dir.mkdir(parents=True, exist_ok=True)
    _write_deterministic_zip(zip_path, repo_root, sources)

    artifact_bytes = zip_path.read_bytes()
    manifest = {
        "schema": SCHEMA_VERSION,
        "tool": "build_lambda_package.py",
        "inputs": input_hashes,
        "artifact": {
            "path": ARTIFACT_RELPATH,
            "entries": sources,
            "entry_sha256": entry_hashes,
            "size_bytes": len(artifact_bytes),
            "sha256": sha256_bytes(artifact_bytes),
        },
    }

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"built {zip_path} ({len(sources)} entries)")
    print(f"wrote {manifest_path}")
    return manifest


def verify(
    repo_root: Path | None = None,
    build_dir: Path | None = None,
    artifact_path: Path | None = None,
) -> List[str]:
    """Check the built artifact against the manifest and live sources.

    Returns a list of human-readable failure strings; an empty list means the
    artifact is exactly the current runtime allowlist, byte for byte.
    Network-free and deterministic.
    """
    repo_root = (repo_root or Path(__file__).resolve().parent).resolve()
    build_dir = (build_dir or repo_root / BUILD_SUBDIR).resolve()
    manifest_path = build_dir / MANIFEST_NAME
    zip_path = artifact_path or build_dir / ARTIFACT_NAME

    failures: List[str] = []

    def fail(msg: str) -> None:
        failures.append(msg)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"manifest not found: {manifest_path} (run 'build' first)")
        return failures
    except (OSError, ValueError) as exc:
        fail(f"unreadable/unparseable manifest {manifest_path}: {exc}")
        return failures

    if manifest.get("schema") != SCHEMA_VERSION:
        fail(f"unsupported manifest schema: {manifest.get('schema')!r}")

    raw_inputs = manifest.get("inputs")
    if not isinstance(raw_inputs, dict):
        fail("manifest inputs must be an object")
        declared_inputs: Dict[str, str] = {}
    else:
        declared_inputs = raw_inputs
    raw_artifact = manifest.get("artifact")
    if not isinstance(raw_artifact, dict):
        fail("manifest artifact must be an object")
        artifact_meta: Dict[str, object] = {}
    else:
        artifact_meta = raw_artifact

    expected_sources = collect_runtime_sources(repo_root)

    # 1+2. Manifest reflects the live tree: same file set, same contents.
    declared_names = sorted(declared_inputs)
    if declared_names != expected_sources:
        only_live = sorted(set(expected_sources) - set(declared_names))
        only_manifest = sorted(set(declared_names) - set(expected_sources))
        detail = []
        if only_live:
            detail.append(f"added since build: {only_live}")
        if only_manifest:
            detail.append(f"removed since build: {only_manifest}")
        fail("artifact is stale: runtime file set changed (" + "; ".join(detail) + ")"
             " -- rebuild with 'build'")
    for rel, digest in sorted(declared_inputs.items()):
        src = repo_root / rel
        try:
            actual = sha256_bytes(src.read_bytes())
        except OSError as exc:
            fail(f"cannot read declared input {rel}: {exc}")
            continue
        if actual != digest:
            fail(f"input changed since build: {rel} -- rebuild with 'build'")

    # 3. Whole-artifact integrity against the recorded hash.
    try:
        actual_zip_sha = sha256_bytes(zip_path.read_bytes())
    except OSError as exc:
        fail(f"cannot read artifact {zip_path}: {exc}")
        return failures

    recorded_zip_sha = artifact_meta.get("sha256")
    if actual_zip_sha != recorded_zip_sha:
        fail("artifact hash mismatch: ZIP does not match the manifest"
             f" (recorded={recorded_zip_sha}, actual={actual_zip_sha})")

    # 4..7. Exact member set and byte-level entry integrity.
    recorded_entries: List[str] = list(artifact_meta.get("entries") or [])
    recorded_entry_hashes: Dict[str, str] = dict(artifact_meta.get("entry_sha256") or {})

    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        fail(f"unreadable artifact ZIP {zip_path}: {exc}")
        return failures

    with zf:
        names = zf.namelist()
        duplicated = sorted(n for n in set(names) if names.count(n) > 1)
        if duplicated:
            fail(f"duplicate entries inside artifact: {duplicated}")

        extra = sorted(set(names) - set(recorded_entries))
        missing = sorted(set(recorded_entries) - set(names))
        if extra or missing or duplicated:
            fail("artifact entry set differs from the manifest"
                 f" (unexpected={extra}, absent={missing})")

        for rel in sorted(set(names)):
            try:
                actual_entry_sha = sha256_bytes(zf.read(rel))
            except KeyError:
                continue  # reported above as absent
            if rel not in recorded_entry_hashes:
                fail(f"undeclared entry inside artifact: {rel}")
                continue
            if actual_entry_sha != recorded_entry_hashes.get(rel):
                fail(f"entry bytes differ from manifest: {rel}")
            if actual_entry_sha != declared_inputs.get(rel):
                fail(f"entry does not match current source snapshot: {rel}"
                     " -- rebuild with 'build'")

    return failures


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode", choices=("build", "verify"), nargs="?", default="build",
        help="'build' writes terraform/build/{lambda.zip,manifest.json}; "
             "'verify' re-checks an existing artifact (default: build)",
    )
    parser.add_argument("--repo-root", type=Path, default=None,
                        help="repository root (defaults to the script location)")
    parser.add_argument("--build-dir", type=Path, default=None,
                        help="output directory (defaults to <repo>/terraform/build)")
    parser.add_argument("--artifact", type=Path, default=None,
                        help="[verify] artifact location other than <build-dir>/lambda.zip")
    args = parser.parse_args(argv)

    try:
        if args.mode == "build":
            build(repo_root=args.repo_root, build_dir=args.build_dir)
            return 0
        failures = verify(repo_root=args.repo_root, build_dir=args.build_dir,
                          artifact_path=args.artifact)
    except PackagingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if failures:
        for message in failures:
            print(f"FAIL: {message}", file=sys.stderr)
        print(f"verification failed ({len(failures)} problem(s))", file=sys.stderr)
        return 1
    print("OK: artifact matches the current runtime allowlist and its manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
