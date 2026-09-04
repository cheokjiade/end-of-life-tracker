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
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

_HELPER_DIR = Path(__file__).resolve().parents[1] / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory.parsers.docker as docker_parser
import eol_inventory.parsers.gitlab_ci as gitlab_parser
import eol_inventory.parsers.go as go_parser
import eol_inventory.parsers.node as node_parser
import eol_inventory.parsers.python as python_parser
from eol_inventory import generate_config, scan_folder
from eol_inventory.redact import (
    _COMPOSED_CREDENTIAL_RE,
    hosted_git_placeholder,
    redact_dependency_ref,
    redact_display_reference,
    redact_display_text,
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
                 "npm:user@1.2.3",
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
    # Short hex tails are not registry digests (exact per-algorithm
    # lengths only): no exemption from credential stripping.
    assert redact_image_reference("ubuntu@sha256:abc") == "sha256:abc"
    assert redact_image_reference(
        "golang:1.23@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef") == \
        "golang:1.23@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert redact_image_reference(
        "registry.invalid/app@sha256:abc") == "registry.invalid/sha256:abc"
    assert redact_image_reference(
        "user:pass@registry.invalid/img@sha256:abc") == \
        "registry.invalid/sha256:abc"
    for ref in ("python:3.12", "myregistry:5000/img:1.2", "ubuntu",
                "ghcr.io/owner/image:2.0"):
        assert redact_image_reference(ref) == ref, ref


def test_scp_style_git_refs_collapse():
    # Sol round-two 1A: SCP-style Git/SSH references collapse to the ssh
    # host placeholder; user, path, and fragment never survive.
    assert redact_dependency_ref(
        "git@github.com:private/repo.git#token-" + SECRET) == \
        "<ssh:github.com>"
    assert redact_dependency_ref("git@github.com:org/repo.git") == \
        "<ssh:github.com>"
    assert redact_dependency_ref("user@host.invalid:path/to/x") == \
        "<ssh:host.invalid>"
    assert redact_dependency_ref("u@h:medium@host2:x/y") == "<ssh:h>"
    # Search-based scan: mid-string, bracketed-IPv6, empty-host, and
    # whitespace-mangled shapes fail closed; digest tails are preserved.
    assert redact_dependency_ref(
        "see git@github.com:org/repo#token-" + SECRET) == \
        "<ssh:github.com>"
    assert redact_dependency_ref("widget @ user@[2001:db8::1]:p#x") == \
        "<ssh>"
    assert redact_dependency_ref("user@:path") == "<ssh>"
    assert redact_dependency_ref("git@github.com :p#token-" + SECRET) == \
        "url:<redacted>"
    # Whitespace inside the host/path tail fails closed even without a
    # fragment; whitespace without a colon is not credential material.
    assert redact_dependency_ref("user:pass@host :path") == "url:<redacted>"
    assert redact_dependency_ref("user@host\t:p") == "url:<redacted>"
    assert redact_dependency_ref("user@host path") == "user@host path"
    # Any Unicode whitespace in the tail fails closed the same way.
    for ws in ("\u00a0", "\u2028", "\u3000", "\x0b", "\x0c"):
        assert redact_dependency_ref(
            "user:pass@host" + ws + ":path") == "url:<redacted>", repr(ws)
    digest = "sha256:" + "a" * 64
    assert redact_dependency_ref("img@" + digest) == "img@" + digest
    # A digest tail followed by path material is not punctuation-only:
    # fail closed rather than exempting arbitrary tails after the anchor.
    assert redact_dependency_ref("img@" + digest + "/x") == "<ssh:sha256>"
    assert redact_dependency_ref(
        "img@" + digest + "?token=" + SECRET) == "<ssh:sha256>"
    # The digest exemption covers only single-@ tokens: a credential
    # segment between an earlier @ and the anchor fails closed.
    # A multi-@ token fails closed in every shape unless the WHOLE token
    # matches the scoped-npm-alias grammar: whitespace, fragment, digest
    # tails (exact, punctuation-bounded, case-variant, wrong length),
    # version tails, and bare trailing anchors.
    for shape in ("host/x\u000by:" + SECRET + "@img@" + digest,
                  "host/path#u:" + SECRET + "@img@" + digest,
                  "user:" + SECRET + "@img@" + digest,
                  "residspec==user:" + SECRET + "@img@" + digest,
                  "user:" + SECRET + "@img@" + digest + ",",
                  "user:" + SECRET + "@img@sha256:" + "a" * 63,
                  "user:" + SECRET + "@img@sha256:" + "a" * 65 + "x",
                  "user:" + SECRET + "@img@",
                  "user:" + SECRET + "@img@1.2.3",
                  "user:" + SECRET + "@img@1.2.3#frag",
                  "host/path#u:" + SECRET + "@1.2.3",
                  "user:" + SECRET + "@img@1.2.3, x"):
        assert redact_dependency_ref(shape) == "url:<redacted>", shape
    # The alias grammar exempts the benign scoped family, including
    # dist-tag tails, in full.
    for alias in ("npm:@scope/pkg@^1.2.3", "npm:@scope/pkg@latest",
                  "npm:@scope/pkg@next", "@scope/pkg@1.2.3",
                  "pkg@workspace:*", "pkg@npm:latest", "npm:user@1.2.3"):
        assert redact_dependency_ref(alias) == alias, alias
    assert redact_dependency_ref("<ssh:github.com>") == "<ssh:github.com>"
    # Benign specs and aliases are byte-identical: no over-broad match.
    for benign in ("1.2.3", "^1.2.3", ">=1.0,<2.0", "npm:user@1.2.3",
                   "npm:@scope/pkg@^1.2.3", "workspace:*",
                   "user@127.0.0.1", "user@1.2.3:x", "../local/path",
                   "pkg==1.0.0"):
        assert redact_dependency_ref(benign) == benign, benign
    assert redact_dependency_ref("<ssh:github.com>") == "<ssh:github.com>"
    # Tagged digest anchors keep the digest doctrine through the
    # colon-tail rule; colon-password tails do not.
    tagged = "name:tag@sha256:" + "a" * 64
    assert redact_dependency_ref(tagged) == tagged
    assert redact_display_text(tagged) == tagged
    assert redact_dependency_ref(
        "registry/name:tag@sha1:" + "b" * 40) == \
        "registry/name:tag@sha1:" + "b" * 40
    assert redact_dependency_ref(tagged + ";") == tagged + ";"
    assert redact_display_text(tagged + ";") == tagged + ";"
    assert redact_image_reference("user:pass@[::1]x:s3cr3t/img") == \
        "url:<redacted>"
    # Consolidation-review pins: slash-bearing junk-host paths, mangled
    # +schemes, colon-bearing userinfo with dotless or empty hosts, and
    # URL/ssh-adjacent fragment tails all fail closed.
    for ref in ("git@10.0.0.1:pr//ivate.git",
                "git@10.0.0.1:u@2:pr.git",
                "Git+SSH://git@host.invalid/private/repo.git"
                "?credential=x",
                "user:pw9z9z9z9@intranet/path/to/repo",
                "user:pass@/path/to/repo",
                "user:pass@#frag-" + SECRET):
        out = redact_dependency_ref(ref)
        assert out.startswith("<ssh") or out == "url:<redacted>", \
            (ref, out)
        assert SECRET not in out and "private" not in out and \
            "credential" not in out and "pw9z9z9z9" not in out, \
            (ref, out)
        assert redact_dependency_ref(out) == out, (ref, out)
    out = redact_dependency_ref("git@1.2.3.4.5:z9z9z9" + SECRET)
    assert out == "<ssh:1.2.3.4.5>", out
    assert redact_dependency_ref(out) == out
    # Display: planted query/fragment tails after URL and ssh tokens
    # collapse; benign fragment prose survives.
    out = redact_display_text(
        "install via == https://host.invalid/x ?q=z9z9z9" + SECRET)
    assert "z9z9z9" not in out and "url:<redacted>" in out, out
    out = redact_display_text(
        "clone >= ssh://git@h.invalid/p/r.git #frag-z9z9z9" + SECRET)
    assert "z9z9z9" not in out and "url:<redacted>" in out, out
    assert redact_display_text("see #lts-policy for detail") == \
        "see #lts-policy for detail"
    assert redact_display_text(
        "user:pw9z9z9z9@intranet/path/to/repo") == "url:<redacted>"


def test_python_scp_direct_reference_redacted():
    parsed = python_parser._parse_requirement(
        "widget @ git@github.com:private/repo.git#token-" + SECRET)
    assert parsed["problem"] == "url"
    assert parsed["ref"] == "<ssh:github.com>"
    record, warning = python_parser._emit_requirement(
        parsed, "runtime", "requirements", "requirements.txt", line=5)
    assert record["name"] == "widget" and record["version"] is None
    assert SECRET not in warning["message"]
    assert "<ssh:github.com>" in warning["message"]
    # An editable SCP-style target is redacted the same way.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        reqs = root / "requirements.txt"
        lines = ["widget @ git@github.com:private/repo.git#token-" + SECRET,
                 "-e git@github.com:private/edit.git#egg=edit",
                 "git@192.0.2.1:private/secret-repo.git",
                 "github.com:credential/private.git#token-" + SECRET,
                 "vspec == https://user:" + SECRET
                 + "@host.invalid/x\u000by?q=" + SECRET]
        for ws in ("\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d",
                   "\x1e", "\x85"):
            lines.append("split @ user:" + SECRET + "@host" + ws + ":path")
            lines.append("half @ user:p" + ws + "ss:" + SECRET
                         + "@host.invalid/x")
        reqs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        records, warnings = python_parser.parse_requirements_records(
            reqs, "requirements.txt", root=root)
        scan = scan_folder(root)
        config = generate_config(scan, "scp-refs")
    serialized = json.dumps({"records": records, "warnings": warnings,
                             "config": config})
    assert SECRET not in serialized
    assert "private/repo" not in serialized and "private/edit" not in \
        serialized
    assert "secret-repo" not in serialized
    assert "credential/private" not in serialized
    assert "<ssh:github.com>" in serialized
    assert "<ssh:192.0.2.1>" in serialized
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered
    assert "secret-repo" not in rendered and "credential/private" not in \
        rendered


def test_ssh_scheme_case_and_ipv4_scp_collapse():
    # Final-audit Batch 1: URI schemes are case-insensitive and valid
    # IPv4 hosts are real SCP hosts; every variant collapses to a
    # host-only placeholder with no path, query, or fragment.
    for ref in (
            "SSH://git@host.invalid/private/repo.git#fragment",
            "Git+SSH://git@host.invalid/private/repo.git?credential=x",
            "sSh://user@host.invalid:2222/private/repo",
            "GIT+ssh://u:p@host.invalid/private.git",
            "git@192.0.2.1:private/secret-repo.git",
            "git@10.0.0.1:credential/private.git",
            "user@192.0.2.1:path#frag",
            "ssh://[2001:db8::1]:private/repo",
    ):
        out = redact_dependency_ref(ref)
        assert out.startswith("<ssh"), (ref, out)
        assert SECRET not in out and "private" not in out and \
            "credential" not in out and "fragment" not in out, (ref, out)
        assert redact_dependency_ref(out) == out, (ref, out)
    # Host-grammar boundary: benign numeric/alias shapes survive.
    assert redact_dependency_ref("user@1.2.3:x") == "user@1.2.3:x"
    assert redact_dependency_ref("user@127.0.0.1") == "user@127.0.0.1"
    assert redact_dependency_ref("npm:@scope/pkg@^1.2.3") == \
        "npm:@scope/pkg@^1.2.3"
    assert redact_dependency_ref("pkg@npm:latest") == "pkg@npm:latest"
    assert redact_dependency_ref(
        "img@sha256:" + "a" * 64) == "img@sha256:" + "a" * 64
    # Display sanitizer: the same material embedded in prose collapses
    # while surrounding text survives; digest anchors with trailing
    # punctuation are pinned references, not SCP shapes.
    prose = "see SSH://git@host.invalid/private/repo.git#fragment " \
            "and git@192.0.2.1:private/secret-repo.git for details"
    out = redact_display_text(prose)
    assert "private" not in out and SECRET not in out, out
    assert "<ssh:host.invalid>" in out and "<ssh:192.0.2.1>" in out, out
    assert "see " in out and " for details" in out, out
    assert redact_display_text(out) == out
    assert redact_display_text(
        "see img@sha256:" + "a" * 64 + " for reproducibility") == \
        "see img@sha256:" + "a" * 64 + " for reproducibility"
    assert redact_display_text(
        "pinned img@sha512:" + "b" * 128 + ".") == \
        "pinned img@sha512:" + "b" * 128 + "."
    assert redact_display_text("use git@:private/repo.git now") == \
        "use <ssh> now"
    # The no-@ colon+path+fragment gate is per-token: one benign URL in
    # the same field does not immunize a sibling payload, and benign
    # free-text with colons, slashes, or hash marks survives.
    mixed = "see https://ok.example/x and " \
            "github.com:cred/private.git#tok-" + SECRET
    out = redact_display_text(mixed)
    assert SECRET not in out and "cred/private" not in out, out
    assert "https://ok.example/x" in out and "url:<redacted>" in out, out
    assert redact_display_text(out) == out
    assert redact_display_text("x git@1.2.3:x/y y git@1.2.3:x/y") == \
        "x <ssh:1.2.3> y <ssh:1.2.3>"
    assert redact_display_text(
        "Track Java LTS: adopt 17/21; see #lts-policy") == \
        "Track Java LTS: adopt 17/21; see #lts-policy"
    assert redact_display_text("release 10:30 at /docs#intro") == \
        "release 10:30 at /docs#intro"
    # The per-token gate is unconditional: an @-bearing sibling never
    # immunizes a hostile @-less token.
    for field in ("ping admin@example.com now github.com:cred/private.git"
                  "#tok-" + SECRET,
                  "user@github.com:x/y github.com:cred/private.git#tok-"
                  + SECRET,
                  "pkg@workspace:* github.com:cred/private.git#tok-"
                  + SECRET,
                  "user@1.2.3:x github.com:cred/private.git#tok-" + SECRET):
        out = redact_display_text(field)
        assert SECRET not in out and "cred/private" not in out, (field, out)
    # Marker-bearing authorities are not user content: a token whose @
    # was introduced by redact_urls still collapses on its path tail,
    # including a leading structural @ from malformed lines.
    for field in ("user:pass@github.com:cred/private.git#tok-" + SECRET,
                  "user:pass@localhost:cred/private.git#tok-" + SECRET,
                  "user:pass@user2:x@github.com:cred/private.git#tok-"
                  + SECRET,
                  "@user:pass@github.com:cred/private.git#tok-" + SECRET):
        assert redact_display_text(field) == "url:<redacted>", field
    # Benign marker-bearing URLs keep their scheme and survive.
    for field in ("https://<redacted>@host.invalid/pkg.whl",
                  "file://<redacted>@host.invalid/path",
                  "git+https://<redacted>@github.com/org/repo"):
        assert redact_display_text(field) == field, field
    # Four-segment digit hosts with noncanonical octets are junk hosts.
    for ref in ("git@1.2.3.999:z9z9z9" + SECRET,
                "git@0177.0.0.1:z9z9z9" + SECRET):
        out = redact_dependency_ref(ref)
        assert out.startswith("<ssh") or out == "url:<redacted>", \
            (ref, out)
        assert SECRET not in out, (ref, out)
    assert redact_dependency_ref("user@1.2.3:x") == "user@1.2.3:x"
    # Junk-host bare lines (five-octet digits, unbracketed IPv6,
    # backslash separators) fail closed to host-only placeholders.
    for ref in ("git@1.2.3.4.5:private/secret-repo.git",
                "git@1.2.3.4.5:z9z9z9" + SECRET,
                "git@2001:db8::1:z9z9z9" + SECRET,
                "git@1.2.3.4.5:z9z9z9" + SECRET + "\\x"):
        out = redact_dependency_ref(ref)
        assert out.startswith("<ssh") or out == "url:<redacted>", (ref, out)
        assert SECRET not in out and "private" not in out and \
            "secret-repo" not in out and "z9z9z9" not in out, (ref, out)
        assert redact_dependency_ref(out) == out, (ref, out)
    # Long hostile input stays bounded and linear.
    hostile = ("x " + "SSH://git@host.invalid/p" + SECRET + " ") * 2000
    out = redact_display_text(hostile)
    assert SECRET not in out and len(out) < len(hostile)


def test_display_multi_anchor_scp_collapses():
    # Review finding 1 (High): a token carrying two @ anchors before an
    # SCP colon-path slipped past the display sanitizer because the SSH
    # scan's boundary lookbehind refuses to start a match after an @,
    # while redact_dependency_ref already fails closed on the same input.
    sentinel = "widget@git@host.invalid:private/SENTINEL-repo.git"
    out = redact_display_text(sentinel)
    assert "SENTINEL" not in out and "private" not in out, out
    assert redact_display_text(out) == out, out
    assert redact_dependency_ref(sentinel) == "url:<redacted>"
    prose = "see " + sentinel + " for details"
    out = redact_display_text(prose)
    assert "SENTINEL" not in out and "private" not in out, out
    assert out.startswith("see ") and out.endswith(" for details"), out
    assert redact_display_text(out) == out, out
    # Punctuation-wrapped, planted-scheme, credential-first, and
    # digit-host variants of the multi-anchor shape fail closed too.
    for shape in ("(" + sentinel + ")",
                  sentinel + ",",
                  sentinel + "://",
                  sentinel + "://u:p@h.invalid/",
                  SECRET + "@git@host.invalid:private/x",
                  SECRET + "@host@1.2.3:x",
                  "u@v@w:p",
                  "user@img@sha256:" + "a" * 64,
                  "widget@git@[2001:db8::1]:private/x",
                  "widget@git@:private/x"):
        out = redact_display_text(shape)
        assert SECRET not in out and "private" not in out \
            and "SENTINEL" not in out and "u@v@w" not in out, (shape, out)
        assert redact_display_text(out) == out, (shape, out)
    # Stability: single-anchor digests, npm aliases (scoped and the
    # yarn-lock ``pkg@npm:other@1.0.0`` form), and plain e-mail prose
    # are byte-identical; an e-mail followed by a colon path is an SCP
    # reference and collapses.
    digest = "nginx:1.25@sha256:" + "a" * 64
    for benign in (digest, "see " + digest + " pinned",
                   "@scope/pkg@1.2.3", "npm:@scope/pkg@^1.2.3",
                   "pkg@npm:other@1.0.0", "pkg@npm:@scope/other@^1.0.0",
                   "aliases @scope/pkg@1.2.3, pkg@npm:other@1.0.0",
                   "mail user@example.com now",
                   "npm:user@1.2.3", "pkg@workspace:*"):
        assert redact_display_text(benign) == benign, benign
    assert redact_display_text("mail user@example.com:private/x now") == \
        "mail <ssh:example.com> now"


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
    # Every helper is idempotent on the new audit forms as well.
    for text in (
            "user:pass@" + SECRET + "@evil.invalid/x",
            "user:pass@user2:" + SECRET + "@evil.invalid/x.yml",
            "user:pass@@sup3rsec.invalid/x.yml",
            " https://user:pass@registry.invalid/img:1.0",
            "\thttps://user:pass@registry.invalid/img:1.0",
            "FROM ${IMG:- https://user:pass@registry.invalid/img:1.0}",
            "npm:user@1.2.3",
            "https://user:pass@user2:" + SECRET + "@evil.invalid/x"):
        assert redact_urls(redact_urls(text)) == redact_urls(text), text
        assert redact_image_reference(
            redact_image_reference(text)) == redact_image_reference(text), \
            text
        assert redact_dependency_ref(
            redact_dependency_ref(text)) == redact_dependency_ref(text), text
        assert redact_display_text(
            redact_display_text(text)) == redact_display_text(text), text
    # The display sanitizer is a fixed point on the multi-anchor SCP
    # shape and its prose embedding as well.
    for text in ("widget@git@host.invalid:private/SENTINEL-repo.git",
                 "see widget@git@host.invalid:private/SENTINEL-repo.git "
                 "for details"):
        assert redact_display_text(
            redact_display_text(text)) == redact_display_text(text), text


def test_redact_urls_deep_nested_anchor_chain_bounded():
    # F2: the nested-anchor descent once recursed per level, so a
    # hostile anchor chain crashed redaction (and the report CLI) with
    # RecursionError; the walk is now iterative under a small cap that
    # fails closed to the URL placeholder.
    for chain in ("a://" * 1200, "a://" * 20000):
        out = redact_urls(chain)
        assert out.startswith("a://"), out
        assert out.endswith("url:<redacted>"), out
        assert len(out) < 200, len(out)
        assert redact_urls(out) == out, out
    # A credential hidden past the cap collapses together with the tail.
    deep = "a://" * 1200 + "user:" + SECRET + "@evil.invalid/x"
    out = redact_urls(deep)
    assert SECRET not in out and "user:" not in out, out
    assert "url:<redacted>" in out, out
    assert redact_urls(out) == out, out
    # Shallow chains keep their byte-identical pre-cap behavior.
    assert redact_urls("a://" * 3) == "a://" * 3
    assert redact_urls(
        "https://host.invalid/path://nested:nested@evil.invalid/x") == \
        "https://host.invalid/path://<redacted>@evil.invalid/x"


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
            "FROM golang:1.23@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef AS pinned\n",
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
        "golang:1.23@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert pinned[0]["digest"] == "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
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


def test_report_view_redacts_non_string_version_spec():
    # F1: `_redacted_text` passed non-string specs through unredacted, so
    # a hostile dict or list spec rendered its credential raw in every
    # report; the string form is redacted instead (None stays empty).
    url = "https://user:" + SECRET + "@registry.example/pkg?token=abc#f"
    config = {
        "products": [],
        "_inventory": {
            "warnings": [],
            "unmapped": [
                {"ecosystem": "python", "name": "pkg-dict",
                 "version_spec": {"spec": url},
                 "reason": "hostile spec shape", "found_in": []},
                {"ecosystem": "python", "name": "pkg-list",
                 "version_spec": [url],
                 "reason": "hostile spec shape", "found_in": []},
            ],
        },
    }
    view = build_inventory_view(config)
    md = render_markdown(view)
    csv_out = render_csv(view)
    html_out = render_html(view)
    for rendered in (md, csv_out, html_out):
        assert SECRET not in rendered
        assert "registry.example" in rendered
    assert "<redacted>@registry.example" in csv_out
    assert "&lt;redacted&gt;" in md


def test_report_view_and_scan_preserve_npm_alias_versions():
    # R5: "npm:user@1.2.3" is an alias, not a credential -- byte-identical
    # through the scanner, the config, and every report view.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "package.json").write_text(json.dumps({
            "dependencies": {"alias-user": "npm:user@1.2.3"}}),
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "npm-alias")
    serialized = json.dumps(config)
    assert "npm:user@1.2.3" in serialized
    assert "<redacted>" not in serialized
    view = build_inventory_view(config)
    assert "npm:user@1.2.3" in json.dumps(view)
    assert "<redacted>" not in json.dumps(view)
    md = render_markdown(view)
    csv_out = render_csv(view)
    html_out = render_html(view)
    assert "npm:user@1.2.3" in csv_out
    assert "npm:user@1.2.3" in html_out
    assert "npm:user&#64;1.2.3" in md
    for rendered in (md, csv_out, html_out):
        assert "<redacted>" not in rendered


# ---------------------------------------------------------------------------
# Leak class (e): audit F1/F2 -- scheme-prefixed and slash-less image refs
# ---------------------------------------------------------------------------

def test_redact_image_reference_scheme_prefix_fail_closed():
    stripped = redact_image_reference(
        "https://user:" + SECRET + "@registry.invalid/team/app:1.0")
    assert stripped == "registry.invalid/team/app:1.0"
    assert redact_image_reference(stripped) == stripped
    assert redact_image_reference(
        "HTTPS://user:" + SECRET + "@registry.invalid/team/app:1.0") == \
        "registry.invalid/team/app:1.0"
    # Credential-shaped material that survives the strip fails closed.
    assert redact_image_reference(
        "https://host.invalid/path://nested:nested@evil.invalid/x") == \
        "url:<redacted>"
    assert redact_image_reference(
        "https://user:pass@host.invalid/img:1.0?token=abc#f") == \
        "url:<redacted>"
    assert redact_image_reference("url:<redacted>") == "url:<redacted>"
    for ref in ("python:3.12", "ghcr.io/owner/image:2.0",
                "registry.invalid/team/app:1.0"):
        assert redact_image_reference(ref) == ref, ref


def test_redact_image_reference_padded_scheme_fail_closed():
    # Leading whitespace must not defeat scheme detection (R1a): every
    # padded form fails closed exactly like its unpadded counterpart.
    for ref in (" https://user:" + SECRET +
                "@registry.invalid/team/app:1.0",
                "\thttps://user:" + SECRET +
                "@registry.invalid/team/app:1.0",
                " \t\r\nhttps://user:pass@registry.invalid/team/app:1.0",
                " user:pass@registry.invalid/team/app:1.0"):
        stripped = redact_image_reference(ref)
        assert stripped == "registry.invalid/team/app:1.0", ref
        assert "user:pass" not in stripped and SECRET not in stripped
        assert redact_image_reference(stripped) == stripped


def test_redact_image_reference_digest_shape_guard():
    stripped = redact_image_reference("user:" + SECRET + "@img:1.0")
    assert stripped == "img:1.0"
    assert redact_image_reference(stripped) == stripped
    assert redact_image_reference("user:pass@localhost:5000") == \
        "localhost:5000"
    # A real digest anchor still passes through byte-identical.
    for ref in ("img@sha256:" + "a" * 64,
                "golang:1.23@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "name:tag@sha256:" + "b" * 64):
        assert redact_image_reference(ref) == ref, ref
    # Short hex tails are not digests: no exemption from stripping.
    assert redact_image_reference("ubuntu@sha256:abc") == "sha256:abc"
    # A second @ before the digest anchor is not a digest reference.
    assert redact_image_reference("user:pass@img@sha256:abc") == "sha256:abc"
    # The head-authority strip feeds its remainder through the same
    # path-segment scan, so credential segments after a slash are
    # removed in one pass and the output is a fixed point.
    once = redact_image_reference("user:pass@reg/a:b@c/img:1")
    assert once == "reg/c/img:1", once
    assert redact_image_reference(once) == once
    multi = redact_image_reference("u1:p1@a/u2:p2@b/img")
    assert multi == "a/b/img", multi
    assert redact_image_reference(multi) == multi


def test_redact_image_reference_path_scan_is_linear():
    # The right-to-left path scan must stay linear: a hostile FROM line
    # with thousands of @ segments is bounded work, not quadratic CPU.
    hostile = "start/" + "a@/" * 20000
    start = time.perf_counter()
    out = redact_image_reference(hostile)
    elapsed = time.perf_counter() - start
    assert "@" not in out and "a/" not in out
    assert redact_image_reference(out) == out
    assert elapsed < 2.0, elapsed


def test_redact_urls_multi_at_authority_chain():
    assert redact_urls(
        "../outside user:pass@" + SECRET + "@evil.invalid/x.yml") == \
        "../outside <redacted>@evil.invalid/x.yml"
    assert redact_urls(
        "user:pass@extra@" + SECRET + "@host.invalid") == \
        "<redacted>@host.invalid"
    once = redact_urls("user:pass@" + SECRET + "@evil.invalid/x")
    assert once == "<redacted>@evil.invalid/x"
    assert redact_urls(once) == once
    # Colon-carrying chains consume every userinfo segment, including
    # the first password.
    assert redact_urls(
        "user:pass@user2:" + SECRET + "@evil.invalid/x.yml") == \
        "<redacted>@evil.invalid/x.yml"
    assert redact_urls(
        "see a:b@c.d and e:f@g.h end") == \
        "see <redacted>@c.d and <redacted>@g.h end"
    # Empty chain segments are consumed too.
    assert redact_urls("user:pass@@sup3rsec.invalid/x.yml") == \
        "<redacted>@sup3rsec.invalid/x.yml"
    assert redact_urls("user:pass@@@" + SECRET + ".invalid/x") == \
        "<redacted>@sup3rsecret.invalid/x"
    for text in ("npm:@scope/real@^1.2.3", "npm:user@1.2.3",
                 "img@sha256:" + "a" * 64,
                 "name:tag@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                 "github.com/org/repo", "golang.org/x/net",
                 "example.com/mod"):
        assert redact_urls(text) == text, text


def test_dockerfile_scheme_image_ref_repro_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Dockerfile").write_text(
            "FROM https://user:" + SECRET +
            "@registry.invalid/team/app:1.0\n"
            "FROM registry.invalid/team/app:2.0\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "scheme-image")
    serialized = json.dumps(config)
    assert SECRET not in serialized
    assert "https:" not in serialized
    redactions = [w for w in config["_inventory"]["warnings"]
                  if w["category"] == "credential_redacted"]
    assert len(redactions) == 1
    assert "registry.invalid/team/app:1.0" in redactions[0]["message"]
    items = {item["image_reference"]: item
             for item in config["_inventory"]["unmapped"]
             if item.get("image_reference")}
    # Redacted repro and clean fixture produce identical record shapes.
    assert items["registry.invalid/team/app:1.0"]["tag"] == "1.0"
    assert items["registry.invalid/team/app:2.0"]["tag"] == "2.0"
    for item in items.values():
        assert item["name"] == "registry.invalid/team/app"
        assert item["registry"] == "registry.invalid"
        assert item["repository"] == "team/app"
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered
    assert "https:" not in rendered


def test_dockerfile_slashless_credential_repro_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Dockerfile").write_text(
            "FROM user:" + SECRET + "@img:1.0\n"
            "FROM img:2.0\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "slashless-image")
    serialized = json.dumps(config)
    assert SECRET not in serialized
    assert _has_warning(config["_inventory"]["warnings"],
                        "credential_redacted", "redacted to 'img:1.0'")
    by_tag = {item["tag"]: item
              for item in config["_inventory"]["unmapped"]
              if item.get("tag") in ("1.0", "2.0")}
    # Redacted repro and clean fixture produce identical record shapes.
    assert by_tag["1.0"]["name"] == "img"
    assert by_tag["1.0"]["image_reference"] == "img:1.0"
    assert by_tag["2.0"]["image_reference"] == "img:2.0"
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered


def test_dockerfile_padded_arg_credentials_redacted():
    # R1 end-to-end: padded ARG values and padded inline defaults must
    # not smuggle scheme-prefixed credentials into records, config, or
    # reports, and the credential_redacted warning must fire.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Dockerfile").write_text(
            "ARG IMG= https://user:" + SECRET +
            "@registry.invalid/team/app:1.0\n"
            "FROM $IMG\n"
            "FROM registry.invalid/team/app:2.0\n"
            "FROM ${BASE:- https://user:" + SECRET +
            "@registry.invalid/team/other:3.0}\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "padded-arg")
    serialized = json.dumps(config)
    assert SECRET not in serialized
    assert "user:pass" not in serialized
    # The unresolved-template locator is fail-closed redacted too.
    assert "https://<redacted>@registry.invalid" in serialized
    redactions = config["_inventory"]["warnings"]
    assert _has_warning(redactions, "credential_redacted",
                        "redacted to 'registry.invalid/team/app:1.0'")
    assert _has_warning(redactions, "credential_redacted",
                        "redacted to 'registry.invalid/team/other:3.0'")
    items = {item["image_reference"]: item
             for item in config["_inventory"]["unmapped"]
             if item.get("image_reference")}
    # Both repro forms parse as the clean reference; no raw secret row.
    assert items["registry.invalid/team/app:1.0"]["tag"] == "1.0"
    assert items["registry.invalid/team/app:1.0"]["name"] == \
        "registry.invalid/team/app"
    assert items["registry.invalid/team/app:1.0"]["found_in"][0][
        "locator"] == "FROM $IMG"
    assert items["registry.invalid/team/app:2.0"]["tag"] == "2.0"
    other = items["registry.invalid/team/other:3.0"]
    assert other["tag"] == "3.0"
    assert other["found_in"][0]["locator"] == \
        "FROM ${BASE:- https://<redacted>@registry.invalid/team/other:3.0}"
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered
    assert "user:pass" not in rendered


def test_dockerfile_unresolved_template_warning_redacted():
    # Round-3 audit: an unresolved sibling variable keeps the raw template
    # in the unresolved_variable warning; the credential inside the
    # template must not reach config JSON, _inventory, or reports.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Dockerfile").write_text(
            "ARG B\n"
            "FROM ${A:-https://user:" + SECRET +
            "@evil.invalid/x:1}${B}\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "unresolved-template")
    serialized = json.dumps(config)
    assert SECRET not in serialized
    assert "user:pass" not in serialized
    assert "<redacted>@evil.invalid" in serialized
    assert not config["_inventory"]["unmapped"]
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered
    assert "user:pass" not in rendered


def test_dockerfile_template_credential_backstop_shapes():
    # F3, round-6: scheme-less template defaults with an @-bearing path
    # segment lose the credential segment at both docker.py call sites
    # (unresolved-variable warning and FROM locator). The composed
    # display backstop remains defense in depth for anything the strip
    # cannot reach, collapsing it to url:<redacted>.
    shapes = (
        ("${A:-evil/xops8:pw8x@e8/y}${B}", "${A:-evil/e8/y}${B}"),
        ("${A:-evil/xuser:pw9@10.0.0.1/y}${B}",
         "${A:-evil/10.0.0.1/y}${B}"),
    )
    for raw, stripped in shapes:
        once = redact_display_reference(raw)
        assert once == stripped, (raw, once)
        assert redact_display_reference(once) == once, once
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Dockerfile").write_text(
            "FROM " + shapes[0][0] + "\nFROM " + shapes[1][0] + "\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "template-backstop")
    serialized = json.dumps(config)
    assert "pw8x" not in serialized and "pw9" not in serialized
    unresolved = [w for w in config["_inventory"]["warnings"]
                  if w["category"] == "unresolved_variable"]
    assert len(unresolved) == 2
    assert unresolved[0]["message"] == \
        "line 1: image '" + shapes[0][1] + "' references variables " \
        "with no resolvable value"
    assert unresolved[1]["message"] == \
        "line 2: image '" + shapes[1][1] + "' references variables " \
        "with no resolvable value"
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert "pw8x" not in rendered and "pw9" not in rendered
    # The backstop is scoped to composed display text: redact_urls keeps
    # its global contract, the strip handles @-bearing path segments,
    # and the backstop regex still catches host shapes the strip or the
    # narrow global redactors would miss.
    assert redact_urls("npm:user@1.2.3") == "npm:user@1.2.3"
    for fragment in ("ops8:pw8x@[::1]", "user:pw9@0177.0.0.1",
                     "user:pw9@2130706433", "xops8:pw9x@sha256:a.bcd.com"):
        assert _COMPOSED_CREDENTIAL_RE.search("evil/x" + fragment + "/y"), \
            fragment
    assert not _COMPOSED_CREDENTIAL_RE.search(
        "name:tag@sha256:" + "a" * 64)
    # The digest exemption boundary applies to every algorithm: a hex
    # tail followed by a dot is a hostname, not a digest. The path scan
    # strips the credential segment outright; the backstop regex pins
    # the same boundary for anything the strip cannot reach.
    composed = "user:pass@reg/x:y@sha256:" + "a" * 64 + ".evil.com/img"
    out = redact_display_reference(composed)
    assert out == "reg/sha256:" + "a" * 64 + ".evil.com/img", out
    assert redact_display_reference(out) == out, out
    assert redact_display_reference(
        "user:pass@reg/x:y@sha1:" + "b" * 40 + ".evil.com/img") == \
        "reg/sha1:" + "b" * 40 + ".evil.com/img"
    assert _COMPOSED_CREDENTIAL_RE.search(
        "u:p@sha256:" + "a" * 64 + ".evil.com")
    assert not _COMPOSED_CREDENTIAL_RE.search(
        "u:p@sha256:" + "a" * 64)
    assert not _COMPOSED_CREDENTIAL_RE.search(
        "u:p@sha256:" + "a" * 64 + "/x")
    assert _COMPOSED_CREDENTIAL_RE.search(
        "u:p@sha1:" + "b" * 40 + "+z/x")


def test_dockerfile_mixed_scheme_digest_credentials_redacted():
    # Round-nine: the record boundary applies the composed backstop too,
    # so digest-shaped hostname credentials and adjacent-scheme
    # authorities never reach records, config JSON, or reports raw.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Dockerfile").write_text(
            "FROM a/u1:" + SECRET +
            "@sha1:" + "b" * 40 + ".evil.com/c://d://u2:" + SECRET +
            "@evil/img\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "mixed-scheme-digest")
    serialized = json.dumps(config)
    assert serialized.count(SECRET) == 0
    assert _has_warning(config["_inventory"]["warnings"],
                        "credential_redacted", "redacted to")
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered
    for ref in ("${A:-registry.invalid/team/app:1.0}${B}", "${IMG}",
                "python:3.12", ">=1.0,<2", "registry:5000/img:1.0",
                "name:tag@sha256:" + "a" * 64,
                "${BASE:- https://<redacted>@registry.invalid/team/x:1}"):
        assert redact_display_reference(ref) == ref, ref


# ---------------------------------------------------------------------------
# Leak class (f): audit F3 -- GitLab local include targets
# ---------------------------------------------------------------------------

def test_redact_image_reference_mid_path_credentials_stripped():
    # Round-6 finding A: an @ after the first slash is credential
    # material unless it is a clean digest anchor on the repository
    # segment; scheme'd URL authorities stay with redact_urls.
    assert redact_image_reference("evil/xops8:pw8x@e8/y") == "evil/e8/y"
    assert redact_image_reference("evil/xuser:pw9@10.0.0.1/y") == \
        "evil/10.0.0.1/y"
    assert redact_image_reference("evil/xa@b@c/y") == "evil/c/y"
    assert redact_image_reference("@e8/y") == "e8/y"
    digest = "sha256:" + "a" * 64
    assert redact_image_reference("registry.invalid/team/app@" + digest) == \
        "registry.invalid/team/app@" + digest
    assert redact_image_reference("registry.invalid/img:1.0@" + digest) == \
        "registry.invalid/img:1.0@" + digest
    assert redact_image_reference("evil/app@user:pw@" + digest) == \
        "evil/" + digest
    assert redact_image_reference("registry:5000/img:1") == \
        "registry:5000/img:1"
    assert redact_image_reference("user:pass@img:1.0") == "img:1.0"
    # An @ whose authority is empty or starts with fragment/query
    # material carries only credentials: fail closed.
    assert redact_image_reference("user:pass@/img") == "url:<redacted>"
    assert redact_image_reference("user:pass@#frag-" + SECRET) == \
        "url:<redacted>"
    assert redact_image_reference("/img") == "/img"
    # Bracketed-IPv6 authorities obey the same port-position rule.
    assert redact_image_reference("user:pass@[::1]:s3cr3t/img") == \
        "url:<redacted>"
    assert redact_image_reference(
        "user:pass@[2001:db8::1]:s3cr3t:5000/img") == "url:<redacted>"
    assert redact_image_reference(
        "user:pass@[::1]:5000/img:1.0") == "[::1]:5000/img:1.0"
    # An @ inside a scheme'd URL authority is URL userinfo, not path
    # material: the scheme'd handling keeps the <redacted> marker form.
    padded = "${BASE:- https://user:pw@registry.invalid/team/x:1}"
    assert redact_display_reference(padded) == \
        "${BASE:- https://<redacted>@registry.invalid/team/x:1}"


def test_dockerfile_resolvable_template_credentials_stripped():
    # Round-6 finding A end-to-end: a fully resolvable template default
    # carrying a scheme-less credential in the image path must never
    # reach records, config JSON, or reports.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Dockerfile").write_text(
            "ARG A=evil/xuser:" + SECRET + "@10.0.0.1/y\n"
            "FROM ${A}\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "resolvable-template")
    serialized = json.dumps(config)
    assert SECRET not in serialized
    assert _has_warning(config["_inventory"]["warnings"],
                        "credential_redacted", "redacted to")
    items = [item for item in config["_inventory"]["unmapped"]
             if item.get("image_reference")]
    assert items and items[0]["image_reference"] == "evil/10.0.0.1/y"
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered


def test_dockerfile_deferred_authority_credentials_redacted():
    # Round-eight: an @ inside a scheme'd URL authority is deferred to
    # redact_urls, which also runs at the record boundary, so doubly
    # scheme'd FROM lines never carry raw credentials into records,
    # config JSON, or reports.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Dockerfile").write_text(
            "FROM a/b://c/d://user:" + SECRET + "@evil.com/img\n",
            encoding="utf-8")
        scan = scan_folder(root)
        config = generate_config(scan, "deferred-authority")
    serialized = json.dumps(config)
    assert SECRET not in serialized
    assert "<redacted>@evil.com" in serialized
    assert _has_warning(config["_inventory"]["warnings"],
                        "credential_redacted", "redacted to")
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered


def test_redact_urls_at_less_colon_text_is_fast():
    # Round-6 finding D: the scheme-less credential pass is skipped for
    # text without any @ (the pattern requires one), so colon-rich
    # @-less input no longer hits quadratic backtracking.
    text = "a:b:c:d:" * 20000
    start = time.perf_counter()
    out = redact_urls(text)
    elapsed = time.perf_counter() - start
    assert out == text
    assert elapsed < 15.0, elapsed


# ---------------------------------------------------------------------------
# Leak class (f): audit F3 -- GitLab local include targets
# ---------------------------------------------------------------------------

def test_gitlab_local_include_targets_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "included.yml").write_text("image: alpine:3.20\n",
                                           encoding="utf-8")
        ci = root / ".gitlab-ci.yml"
        ci.write_text(
            "include:\n"
            "  - local: \"../outside user:pass@" + SECRET +
            "@evil.invalid/x.yml\"\n"
            "  - local: \"missing user:pass@" + SECRET +
            "@evil.invalid/x.yml\"\n"
            "  - local: \"included.yml\"\n",
            encoding="utf-8")
        records, warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml", root=root)
        _, skipped_warnings = gitlab_parser.parse_gitlab_ci_records(
            ci, ".gitlab-ci.yml")
        scan = scan_folder(root)
        config = generate_config(scan, "local-includes")
    serialized = json.dumps({"records": records, "warnings": warnings,
                             "skipped": skipped_warnings,
                             "config": config})
    assert SECRET not in serialized
    assert "user:pass" not in serialized
    assert _has_warning(warnings, "ci_include_escape",
                        "../outside <redacted>@evil.invalid/x.yml")
    assert _has_warning(warnings, "ci_include_missing",
                        "missing <redacted>@evil.invalid/x.yml")
    assert _has_warning(skipped_warnings, "ci_include_skipped",
                        "<redacted>@evil.invalid/x.yml")
    # A legitimate in-root local include is still followed and parsed.
    assert any(r["name"] == "alpine" and r["version"] == "3.20"
               for r in records)
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered


# ---------------------------------------------------------------------------
# Leak class (g): audit F4 -- go.mod module paths
# ---------------------------------------------------------------------------

def test_go_module_paths_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "go.mod").write_text(
            "module example.com/app\n"
            "\n"
            "require example.com/old v1.0.0\n"
            "\n"
            "require user:pass@" + SECRET + "@evil.invalid/weird "
            "notaversion\n"
            "\n"
            "replace example.com/old => user:pass@" + SECRET +
            "@evil.invalid/mod v1.2.3\n"
            "\n"
            "replace example.com/local => ../user:pass@" + SECRET +
            "@evil.invalid/path\n",
            encoding="utf-8")
        records, warnings = go_parser.parse_go_mod_records(
            root / "go.mod", "go.mod")
        scan = scan_folder(root)
        config = generate_config(scan, "go-paths")
    serialized = json.dumps({"records": records, "warnings": warnings,
                             "config": config})
    assert SECRET not in serialized
    assert "user:pass" not in serialized
    assert _has_warning(warnings, "go_replace",
                        "replace example.com/old => "
                        "<redacted>@evil.invalid/mod v1.2.3")
    assert _has_warning(warnings, "go_local_replace",
                        "replace example.com/local => "
                        "../<redacted>@evil.invalid/path is a local path")
    assert _has_warning(warnings, "unresolved_version",
                        "require <redacted>@evil.invalid/weird has "
                        "non-canonical module version 'notaversion'")
    by_name = {r["name"]: r for r in records if r["kind"] == "dependency"}
    assert by_name["<redacted>@evil.invalid/mod"]["version"] == "1.2.3"
    assert by_name["<redacted>@evil.invalid/weird"]["version_spec"] == \
        "notaversion"
    assert "example.com/old" not in by_name
    entries = {p["module"]: p for p in config["products"]
               if p.get("module")}
    assert entries["<redacted>@evil.invalid/mod"]["version"] == "v1.2.3"
    assert entries["<redacted>@evil.invalid/mod"]["label"] == \
        "<redacted>@evil.invalid/mod v1.2.3"
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered

    # Legitimate module paths pass through byte-identical.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "go.mod").write_text(
            "module example.com/app\n"
            "\n"
            "require example.com/old v1.0.0\n"
            "\n"
            "replace example.com/old => github.com/fork/repo v1.2.3\n",
            encoding="utf-8")
        records, warnings = go_parser.parse_go_mod_records(
            root / "go.mod", "go.mod")
    by_name = {r["name"]: r for r in records if r["kind"] == "dependency"}
    assert by_name["github.com/fork/repo"]["version"] == "1.2.3"
    assert _has_warning(warnings, "go_replace",
                        "replace example.com/old => "
                        "github.com/fork/repo v1.2.3")


def test_go_module_directive_credentials_redacted():
    # R4: even though config_writer drops kind="module" rows, the
    # record-layer invariant holds: a credential-shaped module directive
    # is redacted; legitimate module paths pass through byte-identical.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "go.mod").write_text(
            "module user:pass@" + SECRET + "@evil.invalid/weird\n"
            "module example.com/app\n",
            encoding="utf-8")
        records, warnings = go_parser.parse_go_mod_records(
            root / "go.mod", "go.mod")
    modules = [r["name"] for r in records if r["kind"] == "module"]
    assert modules == ["<redacted>@evil.invalid/weird", "example.com/app"]
    assert SECRET not in json.dumps({"records": records,
                                     "warnings": warnings})
    assert "user:pass" not in json.dumps({"records": records,
                                          "warnings": warnings})


# ---------------------------------------------------------------------------
# Leak class (h): audit F5 -- python runtime version specs
# ---------------------------------------------------------------------------

def test_python_runtime_constraints_redacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "pyproject.toml").write_text(
            "[project]\n"
            "name = \"sample\"\n"
            "requires-python = \">=3.8, <https://user:pass@" + SECRET +
            "@evil.invalid/x\"\n"
            "\n"
            "[tool.poetry.dependencies]\n"
            "python = \"^3.8,<https://user:pass@" + SECRET +
            "@evil.invalid/y\"\n",
            encoding="utf-8")
        records, warnings = python_parser.parse_pyproject_records(
            root / "pyproject.toml", "pyproject.toml")
        scan = scan_folder(root)
        config = generate_config(scan, "runtime-specs")
    serialized = json.dumps({"records": records, "warnings": warnings,
                             "config": config})
    assert SECRET not in serialized
    assert "user:pass" not in serialized
    specs = sorted(r["version_spec"] for r in records
                   if r["name"] == "python" and r["kind"] == "runtime")
    assert specs == [">=3.8, <https://<redacted>@evil.invalid/x",
                     "^3.8,<https://<redacted>@evil.invalid/y"]
    unmapped = [item for item in config["_inventory"]["unmapped"]
                if item["name"] == "python"]
    assert len(unmapped) == 2
    for item in unmapped:
        assert SECRET not in item["reason"]
        assert "<redacted>@evil.invalid" in item["version_spec"]
    assert warnings == [] or all(
        SECRET not in w["message"] for w in warnings)
    view = build_inventory_view(config)
    rendered = "\n".join((
        render_markdown(view), render_csv(view), render_html(view)))
    assert SECRET not in rendered

    # Legitimate constraints pass through byte-identical.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "pyproject.toml").write_text(
            "[project]\n"
            "name = \"sample\"\n"
            "requires-python = \">=3.8,<3.13\"\n"
            "\n"
            "[tool.poetry.dependencies]\n"
            "python = \"^3.8\"\n",
            encoding="utf-8")
        records, _ = python_parser.parse_pyproject_records(
            root / "pyproject.toml", "pyproject.toml")
    specs = sorted(r["version_spec"] for r in records
                   if r["name"] == "python" and r["kind"] == "runtime")
    assert specs == [">=3.8,<3.13", "^3.8"]


# ---------------------------------------------------------------------------

TESTS = [
    test_redact_urls_userinfo_query_fragment_matrix,
    test_redact_dependency_ref_matrix,
    test_redact_image_reference_matrix,
    test_hosted_git_and_ssh_placeholders,
    test_redaction_is_idempotent,
    test_redact_urls_deep_nested_anchor_chain_bounded,
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
    test_report_view_redacts_non_string_version_spec,
    test_redact_image_reference_scheme_prefix_fail_closed,
    test_redact_image_reference_padded_scheme_fail_closed,
    test_redact_image_reference_digest_shape_guard,
    test_redact_urls_multi_at_authority_chain,
    test_dockerfile_scheme_image_ref_repro_redacted,
    test_dockerfile_slashless_credential_repro_redacted,
    test_dockerfile_padded_arg_credentials_redacted,
    test_dockerfile_unresolved_template_warning_redacted,
    test_redact_image_reference_mid_path_credentials_stripped,
    test_dockerfile_resolvable_template_credentials_stripped,
    test_dockerfile_deferred_authority_credentials_redacted,
    test_dockerfile_mixed_scheme_digest_credentials_redacted,
    test_redact_urls_at_less_colon_text_is_fast,
    test_redact_image_reference_path_scan_is_linear,
    test_scp_style_git_refs_collapse,
    test_python_scp_direct_reference_redacted,
    test_ssh_scheme_case_and_ipv4_scp_collapse,
    test_display_multi_anchor_scp_collapses,
    test_dockerfile_template_credential_backstop_shapes,
    test_gitlab_local_include_targets_redacted,
    test_go_module_paths_redacted,
    test_go_module_directive_credentials_redacted,
    test_python_runtime_constraints_redacted,
    test_report_view_and_scan_preserve_npm_alias_versions,
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
