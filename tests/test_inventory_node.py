"""Tests for the Node.js inventory parser.

Covers helper_scripts/eol_inventory/parsers/node.py: normalized records,
provenance locations, warnings, exact-spec passthrough, sibling lock
resolution (package-lock.json v3 and legacy v1, npm-shrinkwrap.json
precedence), lock provenance locations, unresolved specifications that
are preserved with structured warnings instead of guessed, and
determinism. Standalone assertion script: no pytest, no network, no
subprocesses.

Run from the repository root:  python tests/test_inventory_node.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "inventory_node"

_HELPER_DIR = ROOT / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory.parsers.node as node_parser


def _parse(*parts):
    path = FIX.joinpath(*parts)
    return node_parser.parse_package_json_records(path, "/".join(parts))


def _records_named(records, name):
    return [r for r in records if r["name"] == name]


def _one(records, name):
    hits = _records_named(records, name)
    assert len(hits) == 1, f"expected exactly one {name!r} record, got {hits}"
    return hits[0]


def _has_warning(warnings, category, substring):
    return any(w["category"] == category and substring in w["message"]
               for w in warnings)


def _locators(record):
    return [loc.get("locator") for loc in record["found_in"]]


# ---------------------------------------------------------------------------
# Lock resolution (locked fixture: package-lock.json v3)
# ---------------------------------------------------------------------------

def test_exact_spec_passthrough_never_touches_lock():
    records, warnings = _parse("locked", "package.json")

    left_pad = _one(records, "left-pad")
    assert left_pad["version"] == "1.3.0"
    assert left_pad["version_spec"] is None
    assert left_pad["scope"] == "runtime" and left_pad["direct"] is True
    assert left_pad["kind"] == "dependency"
    assert left_pad["found_in"] == [{
        "path": "locked/package.json", "manifest": "npm",
        "locator": "dependencies.left-pad"}]
    assert not _has_warning(warnings, "unresolved_version", "left-pad")


def test_lock_resolution_v3_provenance_and_scoped_packages():
    records, warnings = _parse("locked", "package.json")
    assert not _has_warning(warnings, "parse_error", "")

    react = _one(records, "react")
    assert react["version"] == "18.2.0"
    assert react["version_spec"] is None
    assert react["ecosystem"] == "node" and react["scope"] == "runtime"
    assert react["found_in"][0] == {
        "path": "locked/package.json", "manifest": "npm",
        "locator": "dependencies.react"}
    assert react["found_in"][1] == {
        "path": "locked/package-lock.json", "manifest": "npm",
        "locator": "lock:react"}

    scoped = _one(records, "@scope/util")
    assert scoped["version"] == "2.1.0"   # lock evidence, not ^-cleaned 2.0.0
    assert _locators(scoped) == [
        "dependencies.@scope/util", "lock:@scope/util"]

    assert not _has_warning(warnings, "unresolved_version", "react")
    assert not _has_warning(warnings, "unresolved_version", "@scope/util")


def test_devdependencies_resolve_through_lock():
    records, warnings = _parse("locked", "package.json")

    ts = _one(records, "typescript")
    assert ts["version"] == "5.4.5" and ts["scope"] == "dev"
    assert _locators(ts) == ["devDependencies.typescript", "lock:typescript"]
    assert not _has_warning(warnings, "unresolved_version", "typescript")


def test_lock_miss_preserves_spec_with_warning():
    records, warnings = _parse("locked", "package.json")

    ghost = _one(records, "ghost-pkg")
    assert ghost["version"] is None
    assert ghost["version_spec"] == "^9.9.9"
    assert ghost["found_in"] == [{
        "path": "locked/package.json", "manifest": "npm",
        "locator": "dependencies.ghost-pkg"}]
    assert _has_warning(
        warnings, "unresolved_version",
        "no lock evidence for ghost-pkg (^9.9.9); range preserved, not guessed")


def test_shrinkwrap_precedence_over_package_lock():
    records, warnings = _parse("shrinkwrap", "package.json")

    react = _one(records, "react")
    assert react["version"] == "18.3.1"   # shrinkwrap wins over lock 17.0.2
    assert react["found_in"][1] == {
        "path": "shrinkwrap/npm-shrinkwrap.json", "manifest": "npm",
        "locator": "lock:react"}
    assert not _has_warning(warnings, "unresolved_version", "react")
    assert not _has_warning(warnings, "parse_error", "")


def test_lock_v1_legacy_dependencies_tree():
    records, warnings = _parse("lockv1", "package.json")
    assert warnings == []

    lodash = _one(records, "lodash")
    assert lodash["version"] == "4.17.21" and lodash["version_spec"] is None
    assert lodash["found_in"][1] == {
        "path": "lockv1/package-lock.json", "manifest": "npm",
        "locator": "lock:lodash"}


# ---------------------------------------------------------------------------
# Unresolved specifications (unlocked fixture: no lock file)
# ---------------------------------------------------------------------------

def test_unresolved_specs_preserved_with_typed_warnings():
    records, warnings = _parse("unlocked", "package.json")

    node = _one(records, "node")
    assert node["kind"] == "runtime" and node["version"] == "18"
    assert node["found_in"] == [{
        "path": "unlocked/package.json", "manifest": "npm",
        "locator": "engines.node"}]
    assert not _has_warning(warnings, "unresolved_version", "engines.node")
    assert not _has_warning(warnings, "parse_error", "")

    for name, spec in (("react", "^18.2.0"), ("anything", "*"),
                       ("latest-pkg", "latest")):
        record = _one(records, name)
        assert record["version"] is None and record["version_spec"] == spec
        assert record["found_in"][0]["locator"].startswith(
            ("dependencies.", "devDependencies."))
        assert _has_warning(
            warnings, "unresolved_version",
            f"no lock evidence for {name} ({spec}); range preserved, not guessed")

    ws = _one(records, "ws-dep")
    assert ws["version"] is None and ws["version_spec"] == "workspace:*"
    assert _has_warning(warnings, "workspace_dependency", "ws-dep")
    assert _has_warning(warnings, "workspace_dependency", "workspace:*")

    git = _one(records, "git-dep")
    assert git["version"] is None
    assert git["version_spec"] == "git+https://github.com/x/y.git"
    assert _has_warning(warnings, "url_dependency", "git-dep")

    local = _one(records, "local-dep")
    assert local["version"] is None and local["version_spec"] == "file:../local"
    assert _has_warning(warnings, "local_path_dependency", "local-dep")


# ---------------------------------------------------------------------------
# Malformed inputs and determinism
# ---------------------------------------------------------------------------

def test_malformed_lock_warns_and_preserves_range():
    records, warnings = _parse("badlock", "package.json")

    react = _one(records, "react")
    assert react["version"] is None
    assert react["version_spec"] == "^18.2.0"
    assert react["found_in"] == [{
        "path": "badlock/package.json", "manifest": "npm",
        "locator": "dependencies.react"}]
    assert _has_warning(warnings, "parse_error", "package-lock.json")
    assert _has_warning(
        warnings, "unresolved_version",
        "no lock evidence for react (^18.2.0); range preserved, not guessed")


def test_invalid_manifest_parse_error():
    records, warnings = _parse("missing.json")
    assert records == []
    assert len(warnings) == 1
    assert warnings[0]["category"] == "parse_error"
    assert warnings[0]["path"] == "missing.json"


def test_parsing_is_deterministic():
    first = _parse("locked", "package.json")
    second = _parse("locked", "package.json")
    assert first == second


def test_optional_and_peer_dependencies_are_direct_inventory():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "package.json"
        path.write_text(json.dumps({
            "optionalDependencies": {"fsevents": "2.3.3"},
            "peerDependencies": {"react": "18.2.0"},
        }), encoding="utf-8")
        records, warnings = node_parser.parse_package_json_records(
            path, "package.json")
    assert warnings == []
    assert [(r["name"], r["scope"], r["direct"]) for r in records] == [
        ("fsevents", "optional", True), ("react", "peer", True)]


# ---------------------------------------------------------------------------

TESTS = [
    test_exact_spec_passthrough_never_touches_lock,
    test_lock_resolution_v3_provenance_and_scoped_packages,
    test_devdependencies_resolve_through_lock,
    test_lock_miss_preserves_spec_with_warning,
    test_shrinkwrap_precedence_over_package_lock,
    test_lock_v1_legacy_dependencies_tree,
    test_unresolved_specs_preserved_with_typed_warnings,
    test_malformed_lock_warns_and_preserves_range,
    test_invalid_manifest_parse_error,
    test_parsing_is_deterministic,
    test_optional_and_peer_dependencies_are_direct_inventory,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    if failed:
        print(f"{failed} test(s) failed")
        return 1
    print("OK test_inventory_node")
    return 0


if __name__ == "__main__":
    sys.exit(main())
