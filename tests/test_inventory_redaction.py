"""Secret-redaction tests for the inventory scanner.

Covers helper_scripts/eol_inventory/redact.py and its application at
every scanner output boundary: pip direct/editable/legacy URL
requirements (including file:// and git+ssh forms), GitLab remote
include URLs, GitLab/Docker image references whose resolved variables
embed registry credentials, npm hosted-git shorthands and git URLs, and
the redaction matrix itself (userinfo variants user:pass@ / user@ /
:pass@, query and fragment stripping, git+ssh, hosted-git shorthands,
and preservation of exact versions, ranges, package names, and npm
aliases). Standalone assertion script: no pytest, no network, no
subprocesses.

Run from the repository root:  python tests/test_inventory_redaction.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

_HELPER_DIR = Path(__file__).resolve().parents[1] / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory.parsers.docker as docker_parser
import eol_inventory.parsers.gitlab_ci as gitlab_parser
import eol_inventory.parsers.node as node_parser
import eol_inventory.parsers.python as python_parser
from eol_inventory import generate_config, scan_folder
from eol_inventory.redact import (
    hosted_git_placeholder,
    redact_dependency_ref,
    redact_image_reference,
    redact_urls,
    ssh_placeholder,
)
from eol_inventory.report_writer import (
    build_inventory_view,
    format_found_in,
    render_csv,
    render_html,
    render_markdown,
)

SECRET = "sup3rsecret"


def _has_warning(warnings, category, substring):
    return any(w["category"] == category and substring in w["message"]
               for w in warnings)


# ---------------------------------------------------------------------------
# Redaction helper matrix
# ---------------------------------------------------------------------------

def test_redact_urls_userinfo_query_fragment_matrix():
    assert redact_urls("https://user:pass@host.invalid/path") == \
        "https://<redacted>@host.invalid/path"
    assert redact_urls("https://user@host.invalid/path") == \
        "https://<redacted>@host.invalid/path"
    assert redact_urls("https://:pass@host.invalid/path") == \
        "https://<redacted>@host.invalid/path"
    assert redact_urls("https://user:p%40ss@host.invalid/path") == \
        "https://<redacted>@host.invalid/path"
    assert redact_urls("https://host.invalid/x?token=abc#frag") == \
        "https://host.invalid/x?<redacted>#<redacted>"
    assert redact_urls("git+https://user:pass@github.com/org/repo.git") == \
        "git+https://<redacted>@github.com/org/repo.git"
    assert redact_urls("file://user:pass@host.invalid/path") == \
        "file://<redacted>@host.invalid/path"
    line = 'pkg @ https://user:token@host.invalid/x ; python_version >= "3.8"'
    assert redact_urls(line) == \
        'pkg @ https://<redacted>@host.invalid/x ; python_version >= "3.8"'
    assert redact_urls(
        "https://host.invalid/path://nested:nested@evil.invalid/x") == \
        "https://host.invalid/path://<redacted>@evil.invalid/x"
    for text in ("https://example.com/ci.yml", "1.2.3", "^1.2.3", "~2.7.0",
                 ">=1.0,<2", "==1.2.*", "*", "latest", "requests",
                 "python_version >= \"3.8\"", "npm:@scope/real@^1.2.3",
                 "./libs/tool", "file:///opt/pkg", "registry:5000/img:1.0"):
        assert redact_urls(text) == text, text
    assert redact_urls(None) is None
    assert redact_urls("") == ""


def test_redact_dependency_ref_matrix():
    assert redact_dependency_ref(
        "git+ssh://git@github.com/org/repo.git") == "<ssh:github.com>"
    assert redact_dependency_ref(
        "ssh://git@host.invalid:2222/org/repo") == "<ssh:host.invalid>"
    assert redact_dependency_ref("ssh://host.invalid/repo") == \
        "<ssh:host.invalid>"
    assert redact_dependency_ref("https://us er:pw@host.invalid/x") == \
        "url:<redacted>"
    assert redact_dependency_ref("https://host.invalid/x@y") == \
        "url:<redacted>"
    assert redact_dependency_ref("file://user:pass@host.invalid/path") == \
        "file://<redacted>@host.invalid/path"
    assert redact_dependency_ref(
        "git+https://host.invalid/x#egg=egg") == \
        "git+https://host.invalid/x#<redacted>"
    for ref in ("./libs/tool", "../pkg", "1.2.3", "^1.2.3", ">=1.0,<2",
                "https://files.example.com/httpx-0.27.0.whl",
                "file:///opt/pkg"):
        assert redact_dependency_ref(ref) == ref, ref


def test_redact_image_reference_matrix():
    assert redact_image_reference(
        "user:pass@registry.invalid/img:1.0") == "registry.invalid/img:1.0"
    assert redact_image_reference(
        "user@registry.invalid/img") == "registry.invalid/img"
    assert redact_image_reference(
        "user:pass@registry.invalid:5000/img:1.0") == \
        "registry.invalid:5000/img:1.0"
    assert redact_image_reference("ubuntu@sha256:abc") == "ubuntu@sha256:abc"
    assert redact_image_reference(
        "golang:1.23@sha256:0123456789abcdef") == \
        "golang:1.23@sha256:0123456789abcdef"
    assert redact_image_reference(
        "registry.invalid/app@sha256:abc") == "registry.invalid/app@sha256:abc"
    assert redact_image_reference(
        "user:pass@registry.invalid/img@sha256:abc") == \
        "registry.invalid/img@sha256:abc"
    for ref in ("python:3.12", "myregistry:5000/img:1.2", "ubuntu",
                "ghcr.io/owner/image:2.0"):
        assert redact_image_reference(ref) == ref, ref


def test_hosted_git_and_ssh_placeholders():
    assert hosted_git_placeholder("github:org/private") == \
        ("<hosted-git:github>", "github")
    assert hosted_git_placeholder("gitlab:group/proj") == \
        ("<hosted-git:gitlab>", "gitlab")
    assert hosted_git_placeholder("bitbucket:team/repo") == \
        ("<hosted-git:bitbucket>", "bitbucket")
    assert hosted_git_placeholder("gist:00123abc") == \
        ("<hosted-git:gist>", "gist")
    assert hosted_git_placeholder("user/repo") == \
        ("<hosted-git:github>", "github")
    assert hosted_git_placeholder("user/repo#v2.0.0") == \
        ("<hosted-git:github>", "github")
    assert hosted_git_placeholder("user/repo#semver:^1.2.3") == \
        ("<hosted-git:github>", "github")
    for spec in ("^1.2.3", "~2.0", ">=1.0.0 <2.0.0", "1.2.3", "==1.2.*",
                 "@scope/pkg", "workspace:*", "npm:real@^1.2.3",
                 "file:../pkg", "1.2.3 - 2.3.4"):
        assert hosted_git_placeholder(spec) == (None, None), spec
    assert ssh_placeholder("git+ssh://git@github.com/org/repo.git") == \
        "<ssh:github.com>"
    assert ssh_placeholder("ssh://nobody@host.invalid/x") == \
        "<ssh:host.invalid>"
    assert ssh_placeholder("ssh://") == "<ssh>"


def test_redaction_is_idempotent():
    once = redact_urls("https://user:pass@host.invalid/x?token=abc#f")
    assert once == "https://<redacted>@host.invalid/x?<redacted>#<redacted>"
    assert redact_urls(once) == once
    ref = redact_dependency_ref("git+ssh://git@github.com/org/repo.git")
    assert ref == "<ssh:github.com>"
    assert redact_dependency_ref(ref) == ref
    img = redact_image_reference("user:pass@registry.invalid/img:1.0")
    assert img == "registry.invalid/img:1.0"
    assert redact_image_reference(img) == img


# ---------------------------------------------------------------------------
# Leak class (a): pip direct/editable/legacy URL requirements
# ---------------------------------------------------------------------------

def test_python_direct_url_requirements_redacted():
    parsed = python_parser._parse_requirement(
        "private-pkg @ https://user:" + SECRET + "@host.invalid/pkg.whl")
    assert parsed["problem"] == "url" and parsed["name"] == "private-pkg"
    record, warning = python_parser._emit_requirement(
        parsed, "runtime", "requirements", "requirements.txt", line=3)
    assert record["name"] == "private-pkg" and record["version"] is None
    assert SECRET not in warning["message"]
    assert "<redacted>@host.invalid" in warning["message"]

    legacy = python_parser._parse_requirement(
        "legacy@https://user:" + SECRET + "@host.invalid/x.tar.gz")
    assert legacy["problem"] == "url"
    assert SECRET not in legacy["ref"]
    assert legacy["ref"] == "https://<redacted>@host.invalid/x.tar.gz"

    git_ref = python_parser._parse_requirement(
        "g @ git+https://user:" + SECRET + "@github.com/org/repo")
    assert git_ref["problem"] == "url"
    assert git_ref["ref"] == "git+https://<redacted>@github.com/org/repo"

    local = python_parser._parse_requirement(
        "pkg @ file://user:" + SECRET + "@host.invalid/path")
    assert local["problem"] == "local"
    _, warning = python_parser._emit_requirement(
        local, "runtime", "requirements", "requirements.txt", line=4)
    assert SECRET not in warning["message"]
    assert "file://<redacted>@host.invalid/path" in warning["message"]

    ssh = python_parser._parse_requirement(
        "pkg @ git+ssh://git@github.com/org/repo.git")
    assert ssh["problem"] == "url" and ssh["ref"] == "<ssh:github.com>"


def test_python_requirements_file_and_editable_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        reqs = root / "requirements.txt"
        reqs.write_text(
            "private-pkg @ https://user:" + SECRET + "@host.invalid/pkg.whl\n"
            "-e git+https://user:" + SECRET + "@github.com/org/repo.git"
            "#egg=repo\n"
            "-e file://user:" + SECRET + "@host.invalid/pkg\n"
            "@https://user:" + SECRET + "@host.invalid/reqs.txt\n"
            "-r https://user:" + SECRET + "@host.invalid/other.txt\n",
            encoding="utf-8")
        records, warnings = python_parser.parse_requirements_records(
            reqs, "requirements.txt", root=root)
    serialized = json.dumps({"records": records, "warnings": warnings})
    assert SECRET not in serialized
    assert "<redacted>@host.invalid" in serialized
    assert "<redacted>@github.com" in serialized
    assert _has_warning(warnings, "url_dependency", "private-pkg")
    assert _has_warning(warnings, "url_dependency", "editable URL reference")
    assert _has_warning(warnings, "local_path_dependency",
                        "editable local path")
    assert _has_warning(warnings, "parse_error", "malformed requirement")
    assert _has_warning(warnings, "include_remote", "is remote")
    assert _has_warning(warnings, "include_remote", "<redacted>@host.invalid")


def test_python_unresolved_spec_and_malformed_raw_redacted():
    parsed = python_parser._parse_requirement(
        "pkg == https://user:" + SECRET + "@host.invalid/x")
    assert parsed["problem"] == "unresolved"
    assert SECRET not in (parsed["version_spec"] or "")
    record, warning = python_parser._emit_requirement(
        parsed, "runtime", "requirements", "requirements.txt", line=1)
    assert record["version_spec"] is not None
    assert SECRET not in (warning["message"] if warning else "")

    malformed = python_parser._parse_requirement(
        "@https://user:" + SECRET + "@host.invalid/x")
    assert malformed["name"] is None and malformed["problem"] == "malformed"
    _, warning = python_parser._emit_requirement(
        malformed, "runtime", "requirements", "requirements.txt", line=2)
    assert SECRET not in warning["message"]
    assert "https://<redacted>@host.invalid" in warning["message"]


def test_python_poetry_and_pipfile_url_values_redacted():
    records, warnings = [], []
    python_parser._emit_poetry_dependency(
        "git-pkg", "git+https://user:" + SECRET + "@github.com/org/repo",
        "runtime", "tool.poetry.dependencies.git-pkg", records, warnings,
        "pyproject.toml")
    assert records[0]["version_spec"] == \
        "git+https://<redacted>@github.com/org/repo"
    assert SECRET not in json.dumps({"records": records,
                                     "warnings": warnings})

    records, warnings = [], []
    python_parser._emit_poetry_dependency(
        "url-pkg", "https://user:" + SECRET + "@host.invalid/x.whl",
        "runtime", "tool.poetry.dependencies.url-pkg", records, warnings,
        "pyproject.toml")
    assert records[0]["version_spec"] == \
        "https://<redacted>@host.invalid/x.whl"
    assert SECRET not in warnings[0]["message"]

    with tempfile.TemporaryDirectory() as tmpdir:
        pipfile = Path(tmpdir) / "Pipfile"
        pipfile.write_text(
            '[[source]]\nurl = "https://pypi.org/simple"\n'
            '[packages]\nbroken = "https://user:' + SECRET +
            '@host.invalid/x.whl"\n',
            encoding="utf-8")
        records, warnings = python_parser.parse_pipfile_records(
            pipfile, "Pipfile", root=Path(tmpdir))
    by_name = {r["name"]: r for r in records}
    assert by_name["broken"]["version_spec"] == \
        "https://<redacted>@host.invalid/x.whl"
    assert SECRET not in json.dumps({"records": records,
                                     "warnings": warnings})


# ---------------------------------------------------------------------------
# Leak class (b): GitLab remote include URLs
# ---------------------------------------------------------------------------

def test_gitlab_remote_include_urls_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "include:\n"
            "  - remote: \"https://user:" + SECRET +
            "@example.invalid/ci.yml?private_token=abc123\"\n"
            "  - https://user:" + SECRET + "@example.invalid/scalar.yml\n"
            "image: node:22\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
    serialized = json.dumps({"records": records, "warnings": warnings})
    assert SECRET not in serialized
    assert "private_token" not in serialized
    remotes = [w for w in warnings if w["category"] == "ci_remote_include"]
    assert len(remotes) == 2
    for warning in remotes:
        assert "https://<redacted>@example.invalid" in warning["message"]
    nodes = [r for r in records if r["name"] == "node"]
    assert len(nodes) == 1 and nodes[0]["version"] == "22"


def test_gitlab_remote_include_project_kind_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        ci = root / "ci.yml"
        ci.write_text(
            "include:\n"
            "  - project: my-group/my-project\n"
            "  - component: gitlab.com/user:pw" + SECRET +
            "@example.invalid/proj@main\n",
            encoding="utf-8")
        _, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, "ci.yml", root=root)
    serialized = json.dumps(warnings)
    assert SECRET not in serialized
    remotes = [w for w in warnings if w["category"] == "ci_remote_include"]
    assert len(remotes) == 2
    assert "my-group/my-project" in remotes[0]["message"]


# ---------------------------------------------------------------------------
# Leak class (c): resolved image/service variable credentials
# ---------------------------------------------------------------------------

def test_gitlab_resolved_image_credentials_stripped():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "variables:\n"
            "  REG_USER: ci-user\n"
            "  REG_TOKEN: " + SECRET + "\n"
            "deploy-job:\n"
            "  image: $REG_USER:$REG_TOKEN@registry.invalid/team/app:3.2\n"
            "test-job:\n"
            "  services:\n"
            "    - name: postgres:$REG_TOKEN@registry.invalid/postgres:16\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
    serialized = json.dumps({"records": records, "warnings": warnings})
    assert SECRET not in serialized
    assert "ci-user" not in serialized
    assert _has_warning(warnings, "credential_redacted", "registry authority")
    app = [r for r in records if r["name"] == "registry.invalid/team/app"]
    assert len(app) == 1
    assert app[0]["version"] == "3.2"
    assert app[0]["image_reference"] == "registry.invalid/team/app:3.2"
    assert app[0]["image_identity"] == "registry.invalid/team/app"
    pg = [r for r in records if r["name"] == "registry.invalid/postgres"]
    assert len(pg) == 1 and pg[0]["version"] == "16"
    assert pg[0]["image_reference"] == "registry.invalid/postgres:16"


def test_gitlab_unresolved_variable_warning_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "deploy-job:\n"
            "  image: user:pw" + SECRET + "@registry.invalid/$MISSING/img:1\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
    assert records == []
    serialized = json.dumps(warnings)
    assert SECRET not in serialized
    unresolved = [w for w in warnings
                  if w["category"] == "unresolved_variable"]
    assert len(unresolved) == 1


def test_dockerfile_userinfo_image_ref_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        dockerfile = Path(tmpdir) / "Dockerfile"
        dockerfile.write_text(
            "FROM deploy:" + SECRET + "@registry.invalid/team/app:1.0\n"
            "FROM golang:1.23@sha256:0123456789abcdef AS pinned\n",
            encoding="utf-8")
        records, warnings = docker_parser.parse_dockerfile_records(
            dockerfile, "Dockerfile")
    serialized = json.dumps({"records": records, "warnings": warnings})
    assert SECRET not in serialized
    assert "deploy:" not in serialized
    assert _has_warning(warnings, "credential_redacted", "registry authority")
    record = records[0]
    assert record["image_reference"] == "registry.invalid/team/app:1.0"
    assert record["tag"] == "1.0"
    assert record["found_in"][0]["locator"] == \
        "FROM registry.invalid/team/app:1.0"
    # Digest references are untouched and produce no redaction warning.
    pinned = [r for r in records if r["name"] == "golang"]
    assert len(pinned) == 1
    assert pinned[0]["image_reference"] == \
        "golang:1.23@sha256:0123456789abcdef"
    assert pinned[0]["digest"] == "sha256:0123456789abcdef"
    assert not _has_warning(warnings, "credential_redacted", "golang")
    assert _has_warning(warnings, "digest_reference", "golang:1.23@sha256")


# ---------------------------------------------------------------------------
# Leak class (d): npm hosted-git shorthands and git URLs
# ---------------------------------------------------------------------------

def test_node_hosted_git_and_git_specs_never_leak():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        package = root / "package.json"
        package.write_text(json.dumps({
            "dependencies": {
                "gh-pkg": "github:org/private-repo",
                "gitlab-pkg": "gitlab:group/hidden-proj",
                "shorthand-pkg": "user/repo",
                "shorthand-pin-pkg": "user/repo#v2.0.0",
                "ssh-pkg": "git+ssh://git@github.com/org/repo.git",
                "git-url-pkg": "git+https://user:" + SECRET +
                               "@github.com/org/repo.git",
                "alias-pkg": "npm:@scope/real@^1.2.3",
                "range-pkg": "^1.2.3",
            },
        }), encoding="utf-8")
        records, warnings = node_parser.parse_package_json_records(
            package, "package.json", root=root)
    by_name = {r["name"]: r for r in records}
    assert by_name["gh-pkg"]["version_spec"] == "<hosted-git:github>"
    assert by_name["gitlab-pkg"]["version_spec"] == "<hosted-git:gitlab>"
    assert by_name["shorthand-pkg"]["version_spec"] == "<hosted-git:github>"
    assert by_name["shorthand-pin-pkg"]["version_spec"] == \
        "<hosted-git:github>"
    assert by_name["ssh-pkg"]["version_spec"] == "<ssh:github.com>"
    assert by_name["git-url-pkg"]["version_spec"] == "git+<redacted>"
    assert by_name["alias-pkg"]["version_spec"] == "npm:@scope/real@^1.2.3"
    assert by_name["range-pkg"]["version_spec"] == "^1.2.3"
    serialized = json.dumps({"records": records, "warnings": warnings})
    for secret in ("org/private-repo", "group/hidden-proj", "user/repo",
                   "git@github.com", SECRET):
        assert secret not in serialized, secret
    assert _has_warning(warnings, "url_dependency",
                        "hosted-git shorthand gh-pkg")
    assert _has_warning(warnings, "url_dependency",
                        "ssh reference ssh-pkg")
    assert _has_warning(warnings, "url_dependency", "git-url-pkg")
    assert _has_warning(warnings, "unresolved_version", "range-pkg")


def test_node_safe_spec_preserves_usable_specs():
    assert node_parser._safe_spec("^1.2.3") == "^1.2.3"
    assert node_parser._safe_spec("~2.0.0") == "~2.0.0"
    assert node_parser._safe_spec(">=1.2.3 <2") == ">=1.2.3 <2"
    assert node_parser._safe_spec("1.2.3") == "1.2.3"
    assert node_parser._safe_spec("workspace:*") == "workspace:*"
    assert node_parser._safe_spec("workspace:../shared") == \
        "workspace:../shared"
    assert node_parser._safe_spec("npm:@scope/real@^1.2.3") == \
        "npm:@scope/real@^1.2.3"
    assert node_parser._safe_spec("*") == "*"
    assert node_parser._safe_spec("latest") == "latest"


# ---------------------------------------------------------------------------
# Output boundaries: config, _inventory, reports
# ---------------------------------------------------------------------------

def test_scan_to_config_and_reports_carry_no_secrets():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "requirements.txt").write_text(
            "private-pkg @ https://user:" + SECRET +
            "@host.invalid/pkg.whl\n",
            encoding="utf-8")
        (root / "package.json").write_text(json.dumps({
            "dependencies": {"gh": "github:org/private-repo"}}),
            encoding="utf-8")
        (root / "Dockerfile").write_text(
            "FROM deploy:" + SECRET + "@registry.invalid/team/app:1.0\n",
            encoding="utf-8")
        (root / ".gitlab-ci.yml").write_text(
            "include:\n"
            "  - remote: \"https://u:" + SECRET +
            "@example.invalid/ci.yml\"\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "secrets")
    serialized = json.dumps(config)
    for secret in (SECRET, "org/private-repo", "deploy:"):
        assert secret not in serialized, secret
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    for secret in (SECRET, "org/private-repo", "deploy:"):
        assert secret not in rendered, secret


def test_report_view_redacts_legacy_raw_material():
    url = "https://user:" + SECRET + "@host.invalid/x"
    config = {
        "products": [],
        "_inventory": {
            "warnings": [{
                "category": "url_dependency", "path": "requirements.txt",
                "message": "pkg uses a direct URL reference (" + url +
                           "); version not resolved",
            }],
            "unmapped": [{
                "ecosystem": "python", "name": "pkg",
                "version_spec": url,
                "reason": "no exact version (" + url + ")",
                "found_in": [{"path": "requirements.txt",
                              "manifest": "requirements",
                              "locator": "pkg @ " + url}],
            }],
        },
    }
    view = build_inventory_view(config)
    assert SECRET not in json.dumps(view)
    md = render_markdown(view)
    csv_out = render_csv(view)
    html_out = render_html(view)
    for rendered in (md, csv_out, html_out):
        assert SECRET not in rendered
    assert "<redacted>@host.invalid" in csv_out
    assert format_found_in([{
        "path": "requirements.txt", "manifest": "requirements",
        "locator": "pkg @ " + url}]) == \
        "requirements.txt (pkg @ https://<redacted>@host.invalid/x)"


# ---------------------------------------------------------------------------

TESTS = [
    test_redact_urls_userinfo_query_fragment_matrix,
    test_redact_dependency_ref_matrix,
    test_redact_image_reference_matrix,
    test_hosted_git_and_ssh_placeholders,
    test_redaction_is_idempotent,
    test_python_direct_url_requirements_redacted,
    test_python_requirements_file_and_editable_redacted,
    test_python_unresolved_spec_and_malformed_raw_redacted,
    test_python_poetry_and_pipfile_url_values_redacted,
    test_gitlab_remote_include_urls_redacted,
    test_gitlab_remote_include_project_kind_redacted,
    test_gitlab_resolved_image_credentials_stripped,
    test_gitlab_unresolved_variable_warning_redacted,
    test_dockerfile_userinfo_image_ref_redacted,
    test_node_hosted_git_and_git_specs_never_leak,
    test_node_safe_spec_preserves_usable_specs,
    test_scan_to_config_and_reports_carry_no_secrets,
    test_report_view_redacts_legacy_raw_material,
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
    print("OK test_inventory_redaction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
