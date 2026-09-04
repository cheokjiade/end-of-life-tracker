"""Tests for the container image inventory parsers.

Covers helper_scripts/eol_inventory/parsers/docker.py and
helper_scripts/eol_inventory/parsers/gitlab_ci.py plus the container
image mappings: FROM/ARG handling, stage aliases, tags, digests,
latest/untagged warnings, GitLab image/services/variables forms, local
include following, remote-include and escape warnings, cycle
extraction, and determinism. Standalone assertion script: no pytest,
no network, no subprocesses.

Run from the repository root:  python tests/test_inventory_containers.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "inventory_containers"

_HELPER_DIR = ROOT / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory.mappings as mappings
import eol_inventory.parsers.docker as docker_parser
import eol_inventory.parsers.gitlab_ci as gitlab_parser
from eol_inventory import scan_folder


def _parse_dockerfile(*parts):
    path = FIX.joinpath(*parts)
    return docker_parser.parse_dockerfile_records(path, "/".join(parts))


def _parse_ci(*parts, **kwargs):
    # parts[0] is the fixture scan root; rel paths stay root-relative,
    # matching how discovery passes them in production.
    path = FIX.joinpath(*parts)
    kwargs.setdefault("root", FIX.joinpath(parts[0]))
    return gitlab_parser.parse_gitlab_ci_records(
        path, "/".join(parts[1:]), **kwargs)


def _records_named(records, name):
    return [r for r in records if r["name"] == name]


def _one(records, name):
    hits = _records_named(records, name)
    assert len(hits) == 1, f"expected exactly one {name!r} record, got {hits}"
    return hits[0]


def _has_warning(warnings, category, substring):
    return any(w["category"] == category and substring in w["message"]
               for w in warnings)


def _loc(record):
    return record["found_in"][0]


# ---------------------------------------------------------------------------
# Shared image-reference helpers
# ---------------------------------------------------------------------------

def test_split_image_reference():
    assert docker_parser.split_image_reference("python:3.12") == \
        ("python", "3.12", None)
    assert docker_parser.split_image_reference("ubuntu") == \
        ("ubuntu", None, None)
    assert docker_parser.split_image_reference("python:3.12@sha256:abc") == \
        ("python", "3.12", "sha256:abc")
    # A colon before the first slash is a registry port, not a tag.
    assert docker_parser.split_image_reference("registry:5000/img") == \
        ("registry:5000/img", None, None)
    assert docker_parser.split_image_reference("registry:5000/img:1.0") == \
        ("registry:5000/img", "1.0", None)


def test_normalize_image_name():
    assert docker_parser.normalize_image_name("python") == "python"
    assert docker_parser.normalize_image_name("library/nginx") == "nginx"
    assert docker_parser.normalize_image_name("docker.io/library/redis") == \
        "redis"
    assert docker_parser.normalize_image_name(
        "mcr.microsoft.com/dotnet/aspnet") == "dotnet/aspnet"
    assert docker_parser.normalize_image_name(
        "registry.gitlab.com/group/app/ci-builder") == \
        "group/app/ci-builder"
    # Unknown registry hosts stay in the name.
    assert docker_parser.normalize_image_name("registry.example.com/foo") == \
        "registry.example.com/foo"


def test_resolve_variables():
    resolve = docker_parser.resolve_variables
    assert resolve("python:${V}-slim", {"V": "3.12"}) == \
        ("python:3.12-slim", [])
    assert resolve("img:$V2", {"V2": "1.0"}) == ("img:1.0", [])
    assert resolve("${V:-fallback}", {}) == ("fallback", [])
    assert resolve("$MISSING", {}) == ("$MISSING", ["MISSING"])
    assert resolve("${V}", {"V": None}) == ("${V}", ["V"])
    assert resolve("plain", {}) == ("plain", [])


# ---------------------------------------------------------------------------
# Dockerfile fixture: multi-stage, ARG defaults, aliases
# ---------------------------------------------------------------------------

def test_dockerfile_multistage_arg_aliases():
    records, warnings = _parse_dockerfile("docker", "Dockerfile")
    assert warnings == []

    slim = _records_named(records, "python")[0]
    assert len(slim["found_in"]) == 1
    assert slim["version"] == "3.12-slim"
    assert slim["ecosystem"] == "container"
    assert slim["kind"] == "image"
    assert slim["direct"] is True and slim["scope"] == "runtime"
    assert _loc(slim)["path"] == "docker/Dockerfile"
    assert _loc(slim)["manifest"] == "dockerfile"
    assert _loc(slim)["line"] == 4
    assert _loc(slim)["locator"] == "FROM python:${PYTHON_VERSION}-slim"

    alpine = _records_named(records, "python")
    assert [r["version"] for r in alpine] == ["3.12-slim", "3.12.4-alpine"]
    assert alpine[1]["found_in"][0]["line"] == 11
    assert alpine[1]["found_in"][0]["locator"] == \
        "FROM python:3.12.4-alpine"

    node = _one(records, "node")
    assert node["version"] == "20"
    assert node["found_in"][0]["line"] == 15

    # The stage aliases 'builder' and 'tester' never become records.
    assert not _records_named(records, "builder")
    assert not _records_named(records, "tester")
    assert len(records) == 3


def test_dockerfile_edge_cases():
    records, warnings = _parse_dockerfile("docker", "Dockerfile.edge")

    golang = _records_named(records, "golang")
    assert [r["version"] for r in golang] == ["1.23", "1.23.0-alpine"]
    assert golang[0]["digest"] == (
        "sha256:0123456789abcdef0123456789abcdef"
        "0123456789abcdef0123456789abcdef")
    assert golang[1]["found_in"][0]["line"] == 7

    # Continuation-joined FROM keeps the first physical line number.
    alpine = _one(records, "alpine")
    assert alpine["version"] == "3.20"
    assert alpine["found_in"][0]["line"] == 9
    assert alpine["found_in"][0]["locator"] == "FROM alpine:3.20"

    # scratch and the stage-alias reuse are silent.
    assert not _records_named(records, "scratch")
    assert not _records_named(records, "build")
    assert len(records) == 5

    assert _has_warning(warnings, "latest_tag", "python' has no tag")
    assert _has_warning(warnings, "latest_tag", "python:latest")
    assert _has_warning(warnings, "digest_reference", "golang:1.23@sha256")
    assert _has_warning(warnings, "unresolved_variable", "BASE_IMAGE")


def test_dockerfile_parsing_is_deterministic():
    assert _parse_dockerfile("docker", "Dockerfile") == \
        _parse_dockerfile("docker", "Dockerfile")
    assert _parse_dockerfile("docker", "Dockerfile.edge") == \
        _parse_dockerfile("docker", "Dockerfile.edge")


def test_stage_alias_can_match_image_name():
    records, warnings = docker_parser._parse_dockerfile_text(
        "FROM alpine:3.20 AS alpine\nFROM alpine\n", "Dockerfile")
    assert warnings == []
    assert len(records) == 1
    assert records[0]["name"] == "alpine"

    records, warnings = docker_parser._parse_dockerfile_text(
        "FROM python:3.12 AS python\nFROM python:3.13 AS runtime\n",
        "Dockerfile")
    assert warnings == []
    assert [record["version"] for record in records] == ["3.12", "3.13"]

    records, warnings = docker_parser._parse_dockerfile_text(
        "ARG PYTHON_VERSION=3.12 # pinned\n"
        "# comment ending in \\\nFROM node:20\n"
        "FROM python:${PYTHON_VERSION}\n", "Dockerfile")
    assert warnings == []
    assert [(r["name"], r["version"]) for r in records] == [
        ("node", "20"), ("python", "3.12")]


def test_gitlab_top_level_image_and_services():
    with tempfile.TemporaryDirectory() as tmpdir:
        ci = Path(tmpdir) / "pipeline.yml"
        ci.write_text(
            "image: python:3.12\n"
            "services:\n"
            "  - mysql:8.0\n"
            "job:\n"
            "  script: [echo]\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, "pipeline.yml")
    assert warnings == []
    top = _one(records, "python")
    assert top["version"] == "3.12"
    assert _loc(top)["locator"] == "image"
    service = _one(records, "mysql")
    assert service["version"] == "8.0"
    assert _loc(service)["locator"] == "services"


# ---------------------------------------------------------------------------
# GitLab CI fixture: image/services/variables forms and includes
# ---------------------------------------------------------------------------

def test_gitlab_image_forms_and_variable_resolution():
    records, warnings = _parse_ci("gitlab", ".gitlab-ci.yml")

    default_image = _records_named(records, "python")[0]
    assert default_image["version"] == "3.12-slim"
    assert _loc(default_image)["line"] == 8
    assert _loc(default_image)["locator"] == "default:image"

    builder = _one(records, "group/app/ci-builder")
    assert builder["version"] == "1.2.3"
    assert _loc(builder)["line"] == 18
    assert _loc(builder)["locator"] == "build-job:image"

    job_image = _records_named(records, "node")[0]
    assert job_image["version"] == "22-bookworm"  # resolved from job vars
    assert _loc(job_image)["line"] == 28
    assert _loc(job_image)["locator"] == "test-job:image"

    assert not _has_warning(warnings, "unresolved_variable", "RUNTIME_IMAGE")
    assert not _has_warning(warnings, "unresolved_variable", "JOB_IMAGE")
    # Variable values are never emitted into the inventory.
    assert not any("SECRET_TOKEN" in w["message"] for w in warnings)
    assert not _records_named(records, "SECRET_TOKEN")
    assert not _records_named(records, "example-token-value")


def test_gitlab_variables_are_order_independent_and_inherited_by_includes():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        included = root / ".gitlab" / "included.yml"
        included.parent.mkdir()
        included.write_text(
            "included-job:\n"
            "  image: $LATE_IMAGE\n"
            "  services:\n"
            "    - $SHARED_SERVICE\n"
            "    - $DEFAULT_SERVICE\n"
            "leak-check:\n"
            "  image: $SERVICE_SECRET\n"
            "cache-leak-check:\n"
            "  image: $CACHE_SECRET\n",
            encoding="utf-8")
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "image: $LATE_IMAGE\n"
            "include:\n"
            "  - local: .gitlab/included.yml\n"
            "job:\n"
            "  image: $JOB_IMAGE\n"
            "  variables:\n"
            "    JOB_IMAGE: node:22\n"
            "cache-leak-root:\n"
            "  image: $CACHE_SECRET\n"
            "variables:\n"
            "  LATE_IMAGE: python:3.12\n"
            "  SHARED_SERVICE: redis:7.2\n"
            "default:\n"
            "  services:\n"
            "    - name: postgres:16\n"
            "      variables:\n"
            "        SERVICE_SECRET: secret:tag\n"
            "  cache:\n"
            "    variables:\n"
            "      CACHE_SECRET: cache-secret:tag\n"
            "  variables:\n"
            "    DEFAULT_SERVICE: alpine:3.20\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)

    python_records = _records_named(records, "python")
    assert len(python_records) == 2
    assert {record["found_in"][0]["path"] for record in python_records} == {
        ".gitlab-ci.yml", ".gitlab/included.yml"}
    assert _one(records, "node")["version"] == "22"
    assert _one(records, "redis")["version"] == "7.2"
    assert _one(records, "alpine")["version"] == "3.20"
    assert not _records_named(records, "secret")
    assert not _records_named(records, "cache-secret")
    unresolved = [warning for warning in warnings
                  if warning["category"] == "unresolved_variable"]
    assert len(unresolved) == 3
    assert {name for name in ("SERVICE_SECRET", "CACHE_SECRET")
            if any(name in warning["message"]
                   for warning in unresolved)} == {
        "SERVICE_SECRET", "CACHE_SECRET"}


def test_gitlab_services_forms():
    records, _ = _parse_ci("gitlab", ".gitlab-ci.yml")

    postgres = _one(records, "postgres")
    assert postgres["version"] == "16-alpine"
    assert _loc(postgres)["line"] == 11
    assert _loc(postgres)["locator"] == "default:services"

    redis = _one(records, "redis")
    assert redis["version"] == "7.2"
    assert _loc(redis)["line"] == 20
    assert _loc(redis)["locator"] == "build-job:services"


def test_gitlab_local_include_followed():
    records, _ = _parse_ci("gitlab", ".gitlab-ci.yml")

    aspnet = _one(records, "dotnet/aspnet")
    assert aspnet["version"] == "8.0"
    assert _loc(aspnet)["path"] == ".gitlab/ci/deploy.yml"
    assert _loc(aspnet)["manifest"] == "gitlab_ci"
    assert _loc(aspnet)["locator"] == "deploy-image-job:image"


def test_gitlab_unresolved_and_remote_include_warnings():
    records, warnings = _parse_ci("gitlab", ".gitlab-ci.yml")

    # Predefined CI variables cannot be resolved: warning, no record.
    assert not _records_named(records, "$CI_REGISTRY_IMAGE/web")
    unresolved = [w for w in warnings
                  if w["category"] == "unresolved_variable"]
    assert len(unresolved) == 1
    assert unresolved[0]["path"] == ".gitlab/ci/deploy.yml"

    remotes = [w for w in warnings if w["category"] == "ci_remote_include"]
    assert len(remotes) == 1
    assert remotes[0]["path"] == ".gitlab-ci.yml"
    assert "project" in remotes[0]["message"]


def test_gitlab_scalar_remote_include_is_warn_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "example.com").mkdir()
        (root / "example.com" / "remote.yml").write_text(
            "image: python:3.12\n", encoding="utf-8")
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "include: https://example.com/remote.yml\nimage: node:22\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
    assert not _records_named(records, "python")
    assert _one(records, "node")["version"] == "22"
    assert _has_warning(warnings, "ci_remote_include", "https://")


def test_gitlab_include_variables_merge_before_root_precedence():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "first.yml").write_text(
            "first-job:\n  image: $IMG\n", encoding="utf-8")
        (root / "second.yml").write_text(
            "variables:\n  IMG: node:20\nsecond-job:\n  image: $IMG\n",
            encoding="utf-8")
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "include:\n  - local: first.yml\n  - local: second.yml\n"
            "variables:\n  IMG: node:22\nroot-job:\n  image: $IMG\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
    nodes = _records_named(records, "node")
    assert len(nodes) == 3
    assert {r["version"] for r in nodes} == {"22"}
    assert not _has_warning(warnings, "unresolved_variable", "IMG")


def test_gitlab_record_shape():
    records, _ = _parse_ci("gitlab", ".gitlab-ci.yml")
    for record in records:
        assert record["ecosystem"] == "container"
        assert record["kind"] == "image"
        assert record["direct"] is True
        assert record["scope"] == "runtime"
        for loc in record["found_in"]:
            assert loc["manifest"] == "gitlab_ci"


def test_gitlab_parsing_is_deterministic():
    assert _parse_ci("gitlab", ".gitlab-ci.yml") == \
        _parse_ci("gitlab", ".gitlab-ci.yml")


def test_gitlab_anchors_and_tabs():
    with tempfile.TemporaryDirectory() as tmpdir:
        anchored = Path(tmpdir) / "anchored.yml"
        anchored.write_text(
            ".base: &base\n"
            "  image: python:3.12\n"
            "\n"
            "job:\n"
            "  <<: *base\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            anchored, "anchored.yml")
        assert _one(records, "python")["version"] == "3.12"
        assert _has_warning(warnings, "ci_yaml_unsupported", "anchors")

        tabbed = Path(tmpdir) / "tabbed.yml"
        tabbed.write_text("job:\n\timage: python:3.12\n", encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            tabbed, "tabbed.yml")
        assert records == []
        assert _has_warning(warnings, "parse_error", "tab indentation")

        scalar = Path(tmpdir) / "scalar.yml"
        scalar.write_text(
            "variables:\n  IMAGE: python:3.12\n"
            "job:\n  image: *default_image\n  script:\n    - |\n"
            "      image: node:20\n      echo done\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            scalar, "scalar.yml")
        assert records == []
        assert _has_warning(warnings, "ci_yaml_unsupported", "aliases")


def test_gitlab_include_guards():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "scan"
        (root / ".gitlab" / "ci").mkdir(parents=True)
        outside = Path(tmpdir) / "outside.yml"
        outside.write_text("image: python:3.12\n", encoding="utf-8")

        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "include:\n"
            "  - local: \"../outside.yml\"\n"
            "  - local: \"/.gitlab/ci/missing.yml\"\n"
            "  - remote: \"https://example.com/ci.yml\"\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
        assert records == []
        assert _has_warning(warnings, "ci_include_escape", "outside.yml")
        assert _has_warning(warnings, "ci_include_missing", "missing.yml")
        assert _has_warning(warnings, "ci_remote_include",
                            "https://example.com")

        # Without a scan root, local includes are skipped with a warning.
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml")
        assert records == []
        assert _has_warning(warnings, "ci_include_skipped", "scan root")


def test_gitlab_zero_indent_lists_and_oversize_include():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        included = root / "included.yml"
        included.write_text("image: python:3.12\n", encoding="utf-8")
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "services:\n- postgres:16\ninclude:\n- local: included.yml\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
        assert sorted((r["name"], r["version"]) for r in records) == [
            ("postgres", "16"), ("python", "3.12")]
        assert warnings == []

        included.write_text("#" * 2_000_001, encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
        assert [(r["name"], r["version"]) for r in records] == [
            ("postgres", "16")]
        assert _has_warning(warnings, "oversize_input", "byte limit")


def test_gitlab_scalar_list_includes_are_followed_locally_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "included.yml").write_text(
            "image: python:3.12\n", encoding="utf-8")
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "include:\n"
            "  - included.yml\n"
            "  - https://example.com/remote.yml\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)

    assert [(r["name"], r["version"]) for r in records] == [
        ("python", "3.12")]
    assert _loc(records[0])["path"] == "included.yml"
    assert _has_warning(
        warnings, "ci_remote_include", "https://example.com/remote.yml")


def test_gitlab_include_glob_and_circular_and_depth():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".gitlab" / "ci").mkdir(parents=True)

        (root / ".gitlab" / "ci" / "a.yml").write_text(
            "image: python:3.12\n", encoding="utf-8")
        (root / ".gitlab" / "ci" / "b.yml").write_text(
            "image: nginx:1.27\n", encoding="utf-8")
        glob_ci = root / "glob.yml"
        glob_ci.write_text(
            "include:\n"
            "  - local: \"/.gitlab/ci/*.yml\"\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            glob_ci, "glob.yml", root=root)
        assert sorted(r["name"] for r in records) == ["nginx", "python"]
        assert warnings == []

        (root / "cyc_a.yml").write_text(
            "include:\n  - local: \"cyc_b.yml\"\n", encoding="utf-8")
        (root / "cyc_b.yml").write_text(
            "include:\n  - local: \"cyc_a.yml\"\n", encoding="utf-8")
        _, warnings = gitlab_parser.parse_gitlab_ci_records(
            root / "cyc_a.yml", "cyc_a.yml", root=root)
        assert _has_warning(warnings, "ci_include_depth", "circular")

        for i in range(7):
            target = f"chain_{i + 1}.yml" if i < 6 else "chain_end.yml"
            (root / f"chain_{i}.yml").write_text(
                f"include:\n  - local: \"{target}\"\n", encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            root / "chain_0.yml", "chain_0.yml", root=root)
        assert records == []
        assert _has_warning(warnings, "ci_include_depth", "exceeds")


def test_gitlab_globbed_include_cannot_follow_escaping_symlink():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        root = base / "scan"
        include_dir = root / ".gitlab" / "ci"
        include_dir.mkdir(parents=True)
        outside = base / "outside.yml"
        outside.write_text("image: python:3.12\n", encoding="utf-8")
        link = include_dir / "escape.yml"
        try:
            link.symlink_to(outside)
        except OSError:
            return  # Symlink creation may require Windows developer mode.
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            'include:\n  - local: "/.gitlab/ci/*.yml"\n', encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
        assert records == []
        assert _has_warning(warnings, "ci_include_escape", "outside.yml")


# ---------------------------------------------------------------------------
# Lifecycle mappings
# ---------------------------------------------------------------------------

def test_image_cycle_helpers():
    assert mappings._tag_numeric_parts("3.12.4-slim") == ["3", "12", "4"]
    assert mappings._tag_numeric_parts("bookworm") == []
    assert mappings._cycle_major("20.15.1-alpine") == "20"
    assert mappings._cycle_major("12-slim") == "12"
    assert mappings._cycle_major_minor("3.12.4-slim") == "3.12"
    assert mappings._cycle_major_minor("24.04.1") == "24.04"
    assert mappings._cycle_major_minor("16-alpine") is None
    assert mappings._cycle_major_minor("stable-alpine") is None


def test_map_image_dep_recognized_images():
    cases = [
        ("python", "3.12-slim", "python", "3.12"),
        ("python", "3.12.4-alpine", "python", "3.12"),
        ("node", "20", "nodejs", "20"),
        ("node", "22-bookworm", "nodejs", "22"),
        ("golang", "1.23.0-alpine", "golang", "1.23"),
        ("mcr.microsoft.com/dotnet/runtime", "8.0", "dotnet", "8"),
        ("mcr.microsoft.com/dotnet/aspnet", "8.0.404", "dotnet", "8"),
        ("mcr.microsoft.com/dotnet/sdk", "9.0", "dotnet", "9"),
        ("ubuntu", "24.04", "ubuntu", "24.04"),
        ("ubuntu", "24.04.1", "ubuntu", "24.04"),
        ("debian", "12-slim", "debian", "12"),
        ("alpine", "3.20", "alpine", "3.20"),
        ("postgres", "16-alpine", "postgresql", "16"),
        ("mysql", "8.0.36", "mysql", "8.0"),
        ("redis", "7.2.5", "redis", "7.2"),
        ("nginx", "1.27-alpine", "nginx", "1.27"),
    ]
    for name, tag, product, cycle in cases:
        entry = mappings._map_image_dep(name, tag)
        assert entry == {"product": product, "version": cycle,
                         "label": entry["label"]}, (name, tag, entry)
        assert entry["label"].endswith(cycle), (name, tag, entry)


def test_map_image_dep_unmapped():
    assert mappings._map_image_dep("python", "bookworm") is None
    assert mappings._map_image_dep("python", "3") is None
    assert mappings._map_image_dep("unknown/img", "1.0") is None
    assert mappings._map_image_dep("nginx", "stable-alpine") is None
    assert mappings._map_image_dep("python", None) is None
    assert mappings._map_image_dep(None, "1.0") is None
    assert mappings._map_image_dep("ghcr.io/library/python", "3.12") is None


# ---------------------------------------------------------------------------
# Consumed-manifest listing for followed GitLab CI local includes
# ---------------------------------------------------------------------------

def test_followed_gitlab_include_is_listed_as_consumed_manifest():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".gitlab-ci.yml").write_text(
            "include:\n  - /templates/base.yml\n")
        (root / "templates").mkdir()
        (root / "templates" / "base.yml").write_text("image: python:3.12\n")
        # Present but never included: no parser reads it, so it must
        # not appear in the manifest list.
        (root / "templates" / "unused.yml").write_text("image: redis:7\n")
        scan = scan_folder(root)
    assert scan["files"] == [".gitlab-ci.yml", "templates/base.yml"]
    assert [r["name"] for r in scan["records"]
            if r["ecosystem"] == "container"] == ["python"]


def test_gitlab_include_without_images_is_still_listed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".gitlab-ci.yml").write_text(
            "include: /templates/empty.yml\n")
        (root / "templates").mkdir()
        (root / "templates" / "empty.yml").write_text(
            "stages:\n  - build\n")
        scan = scan_folder(root)
    # The include was followed and read even though it contributes zero
    # records and zero warnings: consumed means listed.
    assert scan["files"] == [".gitlab-ci.yml", "templates/empty.yml"]
    assert scan["records"] == []
    assert scan["warnings"] == []


def test_gitlab_missing_include_is_not_listed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".gitlab-ci.yml").write_text(
            "include: /templates/absent.yml\n")
        scan = scan_folder(root)
    assert scan["files"] == [".gitlab-ci.yml"]
    assert _has_warning(scan["warnings"], "ci_include_missing", "absent.yml")


# ---------------------------------------------------------------------------

TESTS = [
    test_split_image_reference,
    test_normalize_image_name,
    test_resolve_variables,
    test_dockerfile_multistage_arg_aliases,
    test_dockerfile_edge_cases,
    test_dockerfile_parsing_is_deterministic,
    test_stage_alias_can_match_image_name,
    test_gitlab_top_level_image_and_services,
    test_gitlab_image_forms_and_variable_resolution,
    test_gitlab_variables_are_order_independent_and_inherited_by_includes,
    test_gitlab_services_forms,
    test_gitlab_local_include_followed,
    test_gitlab_unresolved_and_remote_include_warnings,
    test_gitlab_scalar_remote_include_is_warn_only,
    test_gitlab_include_variables_merge_before_root_precedence,
    test_gitlab_record_shape,
    test_gitlab_parsing_is_deterministic,
    test_gitlab_anchors_and_tabs,
    test_gitlab_include_guards,
    test_gitlab_zero_indent_lists_and_oversize_include,
    test_gitlab_scalar_list_includes_are_followed_locally_only,
    test_gitlab_include_glob_and_circular_and_depth,
    test_gitlab_globbed_include_cannot_follow_escaping_symlink,
    test_image_cycle_helpers,
    test_map_image_dep_recognized_images,
    test_map_image_dep_unmapped,
    test_followed_gitlab_include_is_listed_as_consumed_manifest,
    test_gitlab_include_without_images_is_still_listed,
    test_gitlab_missing_include_is_not_listed,
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
    print("OK test_inventory_containers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
