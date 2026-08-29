"""Tests for the Python inventory parsers.

Covers helper_scripts/eol_inventory/parsers/python.py: requirements*
parsing (exact pins, markers, extras, options, recursive includes with
scan-root escape and cycle protection), pyproject.toml via the
conservative TOML subset (PEP 621 + Poetry), Pipfile.lock resolution,
runtime evidence files, warnings instead of guessed versions, and
determinism. Standalone assertion script: no pytest, no network, no
subprocesses.

Run from the repository root:  python tests/test_inventory_python.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "inventory_python"

_HELPER_DIR = ROOT / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory.parsers.python as python_parser


def _parse_req(*parts):
    path = FIX.joinpath(*parts)
    return python_parser.parse_requirements_records(path, "/".join(parts))


def _parse_pyproject(*parts):
    path = FIX.joinpath(*parts)
    return python_parser.parse_pyproject_records(path, "/".join(parts))


def _records_named(records, name):
    return [r for r in records if r["name"] == name]


def _one(records, name):
    hits = _records_named(records, name)
    assert len(hits) == 1, f"expected exactly one {name!r} record, got {hits}"
    return hits[0]


def _has_warning(warnings, category, substring):
    return any(w["category"] == category and substring in w["message"]
               for w in warnings)


def _categories(warnings):
    return [w["category"] for w in warnings]


def _locators(record):
    return [loc.get("locator") for loc in record["found_in"]]


# ---------------------------------------------------------------------------
# _parse_requirement units
# ---------------------------------------------------------------------------

def test_parse_requirement_exact_pins_and_markers():
    f = python_parser._parse_requirement
    exact = f("requests==2.32.4")
    assert exact["name"] == "requests" and exact["version"] == "2.32.4"
    assert exact["version_spec"] is None and exact["problem"] is None

    arbitrary = f("pkg===1.0.0-beta.1")
    assert arbitrary["version"] == "1.0.0-beta.1"

    marked = f("rich==13.7.1 ; python_version >= \"3.8\"")
    assert marked["version"] == "13.7.1" and marked["version_spec"] is None

    unpinned_marked = f("pkg ; python_version < '3'")
    assert unpinned_marked["problem"] == "unpinned"
    assert unpinned_marked["version_spec"] == "python_version < '3'"


def test_parse_requirement_unresolved_specs():
    f = python_parser._parse_requirement
    rng = f("urllib3>=2.2,<3")
    assert rng["version"] is None and rng["version_spec"] == ">=2.2,<3"
    assert rng["problem"] == "unresolved"

    wildcard = f("wildcard==1.2.*")
    assert wildcard["problem"] == "unresolved"
    assert wildcard["version_spec"] == "==1.2.*"

    compatible = f("pydantic~=2.7")
    assert compatible["problem"] == "unresolved"

    unpinned = f("unpinned-package")
    assert unpinned["problem"] == "unpinned"
    assert unpinned["version"] is None and unpinned["version_spec"] is None


def test_parse_requirement_url_local_and_malformed():
    f = python_parser._parse_requirement
    url = f("httpx @ https://files.example.com/httpx-0.27.0.whl")
    assert url["problem"] == "url" and url["name"] == "httpx"

    legacy = f("legacy-ref@https://example.com/legacy.tar.gz")
    assert legacy["problem"] == "url" and legacy["ref"].startswith("https://")

    extras_url = f("pkg[extra] @ https://example.com/pkg.whl")
    assert extras_url["problem"] == "url" and extras_url["extras"] == "extra"

    local = f("./libs/localtool")
    assert local["problem"] == "local" and local["name"] is None

    named_local = f("pkg @ file:///opt/pkg")
    assert named_local["problem"] == "local"

    malformed = f("==1.0")
    assert malformed["problem"] == "malformed" and malformed["name"] is None


# ---------------------------------------------------------------------------
# requirements fixture (includes, options, provenance, warnings)
# ---------------------------------------------------------------------------

def test_requirements_pins_provenance_and_markers():
    records, warnings = _parse_req("requirements", "app", "requirements.txt")

    requests = [r for r in _records_named(records, "requests")
                if r["version"] == "2.32.4"][0]
    assert requests["version"] == "2.32.4"
    assert requests["ecosystem"] == "python" and requests["scope"] == "runtime"
    assert requests["direct"] is True
    assert requests["found_in"] == [{
        "path": "requirements/app/requirements.txt", "manifest": "requirements",
        "line": 4, "locator": "requests"}]

    extras = [r for r in _records_named(records, "requests")
              if r["version"] == "2.31.0"][0]
    assert extras["found_in"][0]["line"] == 10
    assert extras["found_in"][0]["locator"] == "requests[socks]"

    rich = _one(records, "rich")
    assert rich["version"] == "13.7.1" and rich["version_spec"] is None
    assert rich["found_in"][0]["line"] == 9

    assert _one(records, "urllib3")["version_spec"] == ">=2.2,<3"


def test_requirements_included_files_carry_their_own_provenance():
    records, warnings = _parse_req("requirements", "app", "requirements.txt")

    pytest_rec = _one(records, "pytest")
    assert pytest_rec["version"] == "8.2.0"
    assert pytest_rec["found_in"] == [{
        "path": "requirements/app/requirements-dev.txt",
        "manifest": "requirements", "line": 2, "locator": "pytest"}]

    base = _one(records, "base-dep")
    assert base["found_in"] == [{
        "path": "requirements/app/base/base.txt", "manifest": "requirements",
        "line": 1, "locator": "base-dep"}]
    assert not _has_warning(warnings, "include_cycle", "")
    assert not _has_warning(warnings, "include_escape", "")


def test_requirements_warnings_instead_of_guessed_versions():
    records, warnings = _parse_req("requirements", "app", "requirements.txt")

    assert _has_warning(warnings, "unresolved_version", "urllib3")
    assert _has_warning(warnings, "unresolved_version", "pydantic")
    assert _has_warning(warnings, "unresolved_version", "wildcard")
    assert _has_warning(warnings, "unresolved_version", "sphinx")
    assert _has_warning(warnings, "unresolved_version",
                        "unpinned-package has no version constraint")
    assert _has_warning(warnings, "url_dependency", "coloredlogs")
    assert _has_warning(warnings, "url_dependency", "legacy-ref")
    assert _has_warning(warnings, "local_path_dependency", "./libs/localtool")
    assert _has_warning(warnings, "local_path_dependency", "editable local")
    assert _has_warning(warnings, "unsupported_option", "constraint files")
    assert len(records) == 13
    assert len(warnings) == 12


def test_requirements_include_escape_and_cycle():
    records, warnings = _parse_req("requirements", "app", "escape.txt")
    assert records == []
    assert _has_warning(warnings, "include_escape", "../../../outside.txt")

    records, warnings = _parse_req("requirements", "cycle", "a.txt")
    assert _one(records, "top-dep")["version"] == "1.0.0"
    assert _one(records, "b-dep")["version"] == "2.0.0"
    assert _has_warning(warnings, "include_cycle", "cycle/a.txt")


def test_requirements_options_are_not_dependencies():
    records, warnings = _parse_req("requirements", "app", "requirements.txt")
    names = {r["name"] for r in records}
    assert "--index-url" not in names
    assert "https://pypi.org/simple" not in names
    assert not _records_named(records, "constraints.txt")


def test_requirements_root_include_hashes_and_depth_guard():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "child.txt").write_text(
            "requests==2.32.4 --hash=sha256:abc --hash=sha256:def\n",
            encoding="utf-8")
        (root / "requirements.txt").write_text(
            "--requirement=child.txt\n", encoding="utf-8")
        records, warnings = python_parser.parse_requirements_records(
            root / "requirements.txt", "requirements.txt", root=root)
        assert _one(records, "requests")["version"] == "2.32.4"
        assert warnings == []

        (root / "requirements.txt").write_text(
            "requests==2.32.4 \\\n"
            "    --hash=sha256:abc\n"
            "flask==3.0.0\n"
            "urllib3==2.2.2\n", encoding="utf-8")
        records, warnings = python_parser.parse_requirements_records(
            root / "requirements.txt", "requirements.txt", root=root)
        assert _one(records, "requests")["found_in"][0]["line"] == 1
        assert _one(records, "flask")["found_in"][0]["line"] == 3
        assert _one(records, "urllib3")["found_in"][0]["line"] == 4
        assert warnings == []

        (root / "shared.txt").write_text(
            "shared==1.0.0\n", encoding="utf-8")
        (root / "left.txt").write_text(
            "-r shared.txt\n", encoding="utf-8")
        (root / "right.txt").write_text(
            "-r shared.txt\n", encoding="utf-8")
        (root / "requirements.txt").write_text(
            "-r left.txt\n-r right.txt\n", encoding="utf-8")
        records, warnings = python_parser.parse_requirements_records(
            root / "requirements.txt", "requirements.txt", root=root)
        assert len(_records_named(records, "shared")) == 1
        assert warnings == []

        for index in range(67):
            target = f"deep-{index + 1}.txt"
            (root / f"deep-{index}.txt").write_text(
                f"-r {target}\n", encoding="utf-8")
        (root / "deep-67.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        records, warnings = python_parser.parse_requirements_records(
            root / "deep-0.txt", "deep-0.txt", root=root)
        assert records == []
        assert _has_warning(warnings, "include_depth", "exceeds")

        (root / "requirements.txt").write_text(
            "-rchild.txt\n", encoding="utf-8")
        records, warnings = python_parser.parse_requirements_records(
            root / "requirements.txt", "requirements.txt", root=root)
        assert _one(records, "requests")["version"] == "2.32.4"
        assert warnings == []


# ---------------------------------------------------------------------------
# pyproject.toml: PEP 621
# ---------------------------------------------------------------------------

def test_pyproject_pep621_runtime_and_dependencies():
    records, warnings = _parse_pyproject("pyproject", "pep621",
                                         "pyproject.toml")

    runtime = _one(records, "python")
    assert runtime["kind"] == "runtime"
    assert runtime["version"] is None
    assert runtime["version_spec"] == ">=3.10"
    assert runtime["found_in"] == [{
        "path": "pyproject/pep621/pyproject.toml", "manifest": "pyproject",
        "locator": "requires-python"}]

    fastapi = _one(records, "fastapi")
    assert fastapi["version"] == "0.111.0" and fastapi["scope"] == "runtime"
    assert fastapi["found_in"] == [{
        "path": "pyproject/pep621/pyproject.toml", "manifest": "pyproject",
        "locator": "project.dependencies.fastapi"}]

    uvicorn = _one(records, "uvicorn")
    assert uvicorn["version"] == "0.30.1"
    assert uvicorn["found_in"][0]["locator"] == \
        "project.dependencies.uvicorn[standard]"


def test_pyproject_pep621_optional_and_warnings():
    records, warnings = _parse_pyproject("pyproject", "pep621",
                                         "pyproject.toml")

    pytest_rec = _one(records, "pytest")
    assert pytest_rec["scope"] == "optional" and pytest_rec["version"] == "8.2.0"
    assert pytest_rec["found_in"][0]["locator"] == \
        "project.optional-dependencies.dev.pytest"

    mkdocs = _one(records, "mkdocs")
    assert mkdocs["scope"] == "optional" and mkdocs["version"] == "1.6.0"

    assert _has_warning(warnings, "unresolved_version", "sqlalchemy")
    assert _has_warning(warnings, "unresolved_version", "hypothesis")
    assert _has_warning(warnings, "url_dependency", "httpx")
    assert len(records) == 8
    assert len(warnings) == 3


# ---------------------------------------------------------------------------
# pyproject.toml: Poetry
# ---------------------------------------------------------------------------

def test_pyproject_poetry_python_constraint_and_versions():
    records, warnings = _parse_pyproject("pyproject", "poetry",
                                         "pyproject.toml")

    runtime = _one(records, "python")
    assert runtime["kind"] == "runtime"
    assert runtime["version_spec"] == ">=3.8,<4.0"
    assert runtime["found_in"][0]["locator"] == \
        "tool.poetry.dependencies.python"
    assert not _has_warning(warnings, "unresolved_version", "python")

    requests = _one(records, "requests")
    assert requests["version"] == "2.31.0"
    assert requests["found_in"][0]["locator"] == \
        "tool.poetry.dependencies.requests"

    assert _one(records, "httpx")["version_spec"] == "^0.27.0"
    assert _one(records, "pendulum")["version_spec"] == "*"
    assert _one(records, "toolz")["version_spec"] == "^1.2"


def test_pyproject_poetry_scopes_and_reference_warnings():
    records, warnings = _parse_pyproject("pyproject", "poetry",
                                         "pyproject.toml")

    pytest_rec = _one(records, "pytest")
    assert pytest_rec["scope"] == "dev" and pytest_rec["version"] == "8.2.0"
    assert pytest_rec["found_in"][0]["locator"] == \
        "tool.poetry.dev-dependencies.pytest"

    mkdocs = _one(records, "mkdocs")
    assert mkdocs["scope"] == "optional" and mkdocs["version"] == "1.6.0"
    assert mkdocs["found_in"][0]["locator"] == \
        "tool.poetry.group.docs.dependencies.mkdocs"

    local = _one(records, "local-pkg")
    assert local["version"] is None
    assert _has_warning(warnings, "local_path_dependency", "local-pkg")

    git = _one(records, "git-pkg")
    assert git["version"] is None
    assert _has_warning(warnings, "url_dependency", "git-pkg")

    assert len(records) == 9
    assert len(warnings) == 5


def test_pyproject_toml_subset_tables():
    path = FIX / "pyproject" / "pep621" / "pyproject.toml"
    tables, warning = python_parser._parse_toml_subset(
        path.read_text(encoding="utf-8"), "pep621/pyproject.toml")
    assert warning is None
    assert tables["project"]["name"] == "sample-api"
    assert tables["build-system"]["build-backend"] == "setuptools.build_meta"
    sample = tables["tool"]["sample"]
    assert sample["flag"] is True and sample["count"] == 3
    assert sample["ratio"] == 0.5 and sample["name"] == "literal-name"


def test_pyproject_toml_subset_unsupported_stops_parsing():
    weird = _parse_pyproject("pyproject", "weird", "pyproject.toml")
    records, warnings = weird
    assert _one(records, "leftpad")["version"] == "0.1.0"
    assert len(warnings) == 1
    assert warnings[0]["category"] == "toml_unsupported"
    assert "line 7" in warnings[0]["message"]

    records, warnings = _parse_pyproject(
        "pyproject", "unsupported-early", "pyproject.toml")
    assert records == []
    assert len(warnings) == 1
    assert warnings[0]["category"] == "toml_unsupported"

    tables, warning = python_parser._parse_toml_subset(
        "a.b = 1\n", "x.toml")
    assert tables == {} and warning["category"] == "toml_unsupported"

    tables, warning = python_parser._parse_toml_subset(
        'a = "unterminated\n', "x.toml")
    assert tables == {} and warning["category"] == "toml_unsupported"


# ---------------------------------------------------------------------------
# Pipfile.lock
# ---------------------------------------------------------------------------

def test_pipfile_lock_versions_and_provenance():
    path = FIX / "pipfile" / "Pipfile.lock"
    records, warnings = python_parser.parse_pipfile_lock_records(
        path, "pipfile/Pipfile.lock")

    certifi = _one(records, "certifi")
    assert certifi["version"] == "2024.6.2"
    assert certifi["direct"] is False and certifi["scope"] == "runtime"
    assert certifi["found_in"] == [{
        "path": "pipfile/Pipfile.lock", "manifest": "pipfile-lock",
        "locator": "default.certifi"}]

    requests = _one(records, "requests")
    assert requests["version"] == "2.32.3"

    pytest_rec = _one(records, "pytest")
    assert pytest_rec["version"] == "8.2.0" and pytest_rec["scope"] == "dev"
    assert pytest_rec["found_in"][0]["locator"] == "develop.pytest"

    assert not _has_warning(warnings, "unresolved_version", "certifi")


def test_pipfile_lock_unpinned_entries_warn():
    path = FIX / "pipfile" / "Pipfile.lock"
    records, warnings = python_parser.parse_pipfile_lock_records(
        path, "pipfile/Pipfile.lock")

    git = _one(records, "git-dep")
    assert git["version"] is None and git["direct"] is False
    path_dep = _one(records, "path-dep")
    assert path_dep["version"] is None
    assert _has_warning(warnings, "url_dependency", "git-dep")
    assert _has_warning(warnings, "local_path_dependency", "path-dep")
    assert len(records) == 5
    assert len(warnings) == 2


def test_pipfile_lock_malformed_json():
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "Pipfile.lock"
        bad.write_text("{not json", encoding="utf-8")
        records, warnings = python_parser.parse_pipfile_lock_records(
            bad, "Pipfile.lock")
    assert records == []
    assert len(warnings) == 1
    assert warnings[0]["category"] == "parse_error"


def test_pipfile_direct_dependencies_resolve_from_lock():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pipfile = root / "Pipfile"
        pipfile.write_text(
            '[packages]\nrequests = "*"\nflask = "==3.0.3"\n'
            '[dev-packages]\npytest = "*"\n', encoding="utf-8")
        (root / "Pipfile.lock").write_text(json.dumps({
            "default": {"requests": {"version": "==2.32.3"},
                        "flask": {"version": "==3.0.3"}},
            "develop": {"pytest": {"version": "==8.2.0"}},
        }), encoding="utf-8")
        records, warnings = python_parser.parse_pipfile_records(
            pipfile, "Pipfile")
    assert warnings == []
    assert [(r["name"], r["version"], r["scope"], r["direct"])
            for r in records] == [
        ("flask", "3.0.3", "runtime", True),
        ("requests", "2.32.3", "runtime", True),
        ("pytest", "8.2.0", "dev", True),
    ]
    assert records[0]["found_in"][0]["locator"] == "packages.flask"


def test_pipfile_malformed_sibling_lock_warns():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pipfile = root / "Pipfile"
        pipfile.write_text('[packages]\nrequests = "*"\n', encoding="utf-8")
        (root / "Pipfile.lock").write_text("{broken", encoding="utf-8")
        records, warnings = python_parser.parse_pipfile_records(
            pipfile, "Pipfile", root=root)
    assert _one(records, "requests")["version"] is None
    assert _has_warning(warnings, "parse_error", "Pipfile.lock")


# ---------------------------------------------------------------------------
# Runtime evidence
# ---------------------------------------------------------------------------

def test_python_version_file():
    path = FIX / "runtime" / ".python-version"
    records, warnings = python_parser.parse_python_version_records(
        path, "runtime/.python-version")
    assert warnings == []
    runtime = _one(records, "python")
    assert runtime["version"] == "3.12.1" and runtime["kind"] == "runtime"
    assert runtime["found_in"] == [{
        "path": "runtime/.python-version", "manifest": "python",
        "locator": "python-version"}]

    bad = FIX / "runtime" / "bad.python-version"
    records, warnings = python_parser.parse_python_version_records(
        bad, "runtime/bad.python-version")
    assert records == []
    assert _has_warning(warnings, "unresolved_version", "system")

    empty = FIX / "runtime" / "empty.python-version"
    records, warnings = python_parser.parse_python_version_records(
        empty, "runtime/empty.python-version")
    assert records == [] and warnings == []


def test_runtime_txt_file():
    path = FIX / "runtime" / "runtime.txt"
    records, warnings = python_parser.parse_runtime_txt_records(
        path, "runtime/runtime.txt")
    assert warnings == []
    runtime = _one(records, "python")
    assert runtime["version"] == "3.11.4" and runtime["kind"] == "runtime"
    assert runtime["found_in"] == [{
        "path": "runtime/runtime.txt", "manifest": "python",
        "locator": "runtime.txt"}]

    with tempfile.TemporaryDirectory() as td:
        garbage = Path(td) / "runtime.txt"
        garbage.write_text("ruby-3.2.0", encoding="utf-8")
        records, warnings = python_parser.parse_runtime_txt_records(
            garbage, "runtime.txt")
    assert records == []
    assert _has_warning(warnings, "unresolved_version", "ruby-3.2.0")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_parsing_is_deterministic():
    assert _parse_req("requirements", "app", "requirements.txt") == \
        _parse_req("requirements", "app", "requirements.txt")
    assert _parse_pyproject("pyproject", "pep621", "pyproject.toml") == \
        _parse_pyproject("pyproject", "pep621", "pyproject.toml")
    assert _parse_pyproject("pyproject", "poetry", "pyproject.toml") == \
        _parse_pyproject("pyproject", "poetry", "pyproject.toml")
    path = FIX / "pipfile" / "Pipfile.lock"
    assert python_parser.parse_pipfile_lock_records(
        path, "pipfile/Pipfile.lock") == \
        python_parser.parse_pipfile_lock_records(path, "pipfile/Pipfile.lock")


# ---------------------------------------------------------------------------

TESTS = [
    test_parse_requirement_exact_pins_and_markers,
    test_parse_requirement_unresolved_specs,
    test_parse_requirement_url_local_and_malformed,
    test_requirements_pins_provenance_and_markers,
    test_requirements_included_files_carry_their_own_provenance,
    test_requirements_warnings_instead_of_guessed_versions,
    test_requirements_include_escape_and_cycle,
    test_requirements_options_are_not_dependencies,
    test_requirements_root_include_hashes_and_depth_guard,
    test_pyproject_pep621_runtime_and_dependencies,
    test_pyproject_pep621_optional_and_warnings,
    test_pyproject_poetry_python_constraint_and_versions,
    test_pyproject_poetry_scopes_and_reference_warnings,
    test_pyproject_toml_subset_tables,
    test_pyproject_toml_subset_unsupported_stops_parsing,
    test_pipfile_lock_versions_and_provenance,
    test_pipfile_lock_unpinned_entries_warn,
    test_pipfile_lock_malformed_json,
    test_pipfile_direct_dependencies_resolve_from_lock,
    test_pipfile_malformed_sibling_lock_warns,
    test_python_version_file,
    test_runtime_txt_file,
    test_parsing_is_deterministic,
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
    print("OK test_inventory_python")
    return 0


if __name__ == "__main__":
    sys.exit(main())
