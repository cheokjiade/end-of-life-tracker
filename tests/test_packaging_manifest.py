"""Allowlisted Lambda packaging: deterministic build + manifest verification.

Standalone assertion script (repo convention: no framework, no network).
Exercises build_lambda_package.py against throwaway synthetic repositories so
the deployed-artifact guarantees hold regardless of what lives in the working
tree: exact allowlist membership, byte-for-byte reproducibility, refusal of
unexpected files, and detection of stale/tampered artifacts.
"""

import os, sys, json, zipfile, tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_lambda_package as pkg


def make_repo(root: Path) -> None:
    """A minimal runtime surface shaped like the real one."""
    (root / "eoltracker" / "parsers").mkdir(parents=True)
    (root / "lambda_function.py").write_text(
        'def lambda_handler(event, context):\n    return {}\n', encoding="utf-8")
    (root / "eoltracker" / "__init__.py").write_text("\n", encoding="utf-8")
    (root / "eoltracker" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "eoltracker" / "parsers" / "__init__.py").write_text(
        "REGISTRY = {}\n", encoding="utf-8")


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    repo = td / "repo"
    repo.mkdir()
    # Unrelated root noise that must never ship.
    (repo / "README.md").write_text("secret-ish docs\n", encoding="utf-8")
    (repo / "credentials.txt").write_text("not-a-real-secret\n", encoding="utf-8")
    make_repo(repo)

    out1 = td / "b1"
    m = pkg.build(repo_root=repo, build_dir=out1)

    expected_entries = [
        "eoltracker/__init__.py",
        "eoltracker/core.py",
        "eoltracker/parsers/__init__.py",
        "lambda_function.py",
    ]
    assert m["schema"] == 1, m["schema"]
    assert m["artifact"]["entries"] == expected_entries, m["artifact"]["entries"]
    assert set(m["inputs"]) == set(expected_entries), sorted(m["inputs"])

    with zipfile.ZipFile(out1 / "lambda.zip") as zf:
        assert zf.namelist() == expected_entries, zf.namelist()
        assert b"secret-ish docs" not in b"".join(zf.read(n) for n in zf.namelist())
        assert "README.md" not in zf.namelist()

    # Determinism: an independent rebuild is byte-identical (fixed timestamps,
    # sorted members) including the manifest itself.
    out2 = td / "b2"
    m2 = pkg.build(repo_root=repo, build_dir=out2)
    assert (out1 / "lambda.zip").read_bytes() == (out2 / "lambda.zip").read_bytes()
    del m["artifact"]["size_bytes"]
    assert json.loads((out2 / "manifest.json").read_text(encoding="utf-8"))["artifact"]["sha256"] \
        == m["artifact"]["sha256"]

    # Clean verification passes.
    assert pkg.verify(repo_root=repo, build_dir=out1) == [], "clean verify failed"

    # Source drift after build -> verification must fail loudly.
    (repo / "eoltracker" / "core.py").write_text("VALUE = 2\n", encoding="utf-8")
    failures = pkg.verify(repo_root=repo, build_dir=out1)
    assert any("changed since build" in f for f in failures), failures

print("OK allowlist build/determinism/drift")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    repo = td / "repo"
    repo.mkdir()
    make_repo(repo)
    out = td / "build"
    pkg.build(repo_root=repo, build_dir=out)

    # Tampering: an injected extra member is rejected.
    zipped = out / "lambda.zip"
    with zipfile.ZipFile(zipped, "a") as zf:
        zf.writestr("EOL_NOTES.md", "smuggled payload\n")
    failures = pkg.verify(repo_root=repo, build_dir=out)
    assert any("EOL_NOTES.md" in f for f in failures), failures
    assert any("entry set differs" in f for f in failures), failures

    # Mutated member bytes (names intact) break integrity checks.
    (td / "mutant").mkdir()
    with zipfile.ZipFile(zipped) as src, \
            zipfile.ZipFile(td / "mutant" / "lambda.zip", "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename.endswith("core.py"):
                data += b"# tampered\n"
            dst.writestr(info.filename, data)
    failures = pkg.verify(repo_root=repo, build_dir=out,
                          artifact_path=td / "mutant" / "lambda.zip")
    assert any("hash mismatch" in f for f in failures), failures
    assert any("differ from manifest" in f for f in failures), failures

    # Duplicate member names are rejected even with valid bytes.
    (td / "dupe").mkdir()
    with zipfile.ZipFile(zipped) as src, \
            zipfile.ZipFile(td / "dupe" / "lambda.zip", "w") as dst:
        for name in ["eoltracker/core.py"] + src.namelist():
            dst.writestr(name, src.read(name))
    failures = pkg.verify(repo_root=repo, build_dir=out,
                          artifact_path=td / "dupe" / "lambda.zip")
    assert any("duplicate" in f for f in failures), failures

    # A corrupt, non-ZIP artifact is a clean verification failure, not a
    # traceback that could obscure the deployment gate.
    corrupt = td / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")
    failures = pkg.verify(repo_root=repo, build_dir=out, artifact_path=corrupt)
    assert any("unreadable artifact ZIP" in f for f in failures), failures

    # CLI exit codes: clean pass 0, tampered run 1.
    good = td / "good"
    good.mkdir()
    repo2 = good / "r2"
    repo2.mkdir()
    make_repo(repo2)
    pkg.build(repo_root=repo2, build_dir=good / "bd")
    assert pkg.main(["verify", "--repo-root", str(repo2), "--build-dir", str(good / "bd")]) == 0
    with zipfile.ZipFile(good / "bd" / "lambda.zip", "a") as zf:
        zf.writestr("extra.bin", b"\x00")
    assert pkg.main(["verify", "--repo-root", str(repo2), "--build-dir", str(good / "bd")]) == 1

print("OK tamper/duplicate/CLI detection")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    repo = td / "repo"
    repo.mkdir()

    # Missing required runtime roots fail closed.
    try:
        pkg.collect_runtime_sources(repo)
        raise AssertionError("missing lambda_function.py accepted")
    except pkg.PackagingError:
        pass

    make_repo(repo)
    # Stray non-Python junk under eoltracker/ is refused outright...
    (repo / "eoltracker" / "notes.dat").write_text("junk\n", encoding="utf-8")
    try:
        pkg.collect_runtime_sources(repo)
        raise AssertionError("stray non-py file accepted")
    except pkg.PackagingError:
        pass
    # ...while known compiled artifacts are skipped silently.
    (repo / "eoltracker" / "notes.dat").unlink()
    pycache = repo / "eoltracker" / "__pycache__"
    pycache.mkdir()
    (pycache / "core.cpython-312.pyc").write_bytes(b"\x00stale bytecode\x00")
    sources = pkg.collect_runtime_sources(repo)
    assert not any("__pycache__" in s or s.endswith(".pyc") for s in sources), sources
    build_dir = td / "build"
    pkg.build(repo_root=repo, build_dir=build_dir)
    assert pkg.verify(repo_root=repo, build_dir=build_dir) == [], "pycache build unverified"
    with zipfile.ZipFile(build_dir / "lambda.zip") as zf:
        assert all(n.endswith(".py") for n in zf.namelist()), zf.namelist()

print("OK fail-closed collection rules")

print("OK test_packaging_manifest")
