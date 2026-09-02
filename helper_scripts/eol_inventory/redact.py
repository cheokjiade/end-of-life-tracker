"""Central secret redaction for scanner-derived text.

Manifests and CI files scanned by the inventory can carry embedded
credentials: URL userinfo (``user:password@``, ``user@``, ``:pass@``),
query-string tokens, URL fragments, and private VCS references
(hosted-git shorthands, ssh URLs). Nothing raw from a scanned file may
reach structured warnings, normalized records, ``_inventory`` metadata,
generated configs, inventory reports, or stdout. These pure helpers give
every emission point one place to sanitize text; they never touch the
network and never raise on malformed input.

Redaction is deliberately narrow: exact versions, non-secret range
specifiers (``^``, ``~``, comparison operators), package names, and
usable aliases contain no credential material and pass through
unchanged. Only URL authority userinfo, query/fragment payloads, and
credential-shaped VCS references are replaced.
"""

import bisect
import re

REDACTED = "<redacted>"
URL_PLACEHOLDER = f"url:{REDACTED}"

_URL_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
# No ">" here: redacted output embeds <redacted>, and stripping a
# trailing ">" would eat the placeholder's closing bracket, making
# repeated redaction drift.
_URL_TRAILING = ")]};,\"'"

# Scheme-less credential authority (``user:pass@host``), e.g. GitLab
# component includes or bare registry references. Requires a password
# colon and a dotted host tail so ranges, aliases, package names, and
# ``name:tag@sha256:`` digest references never match; "<>" is excluded
# from the password so the regex never matches its own output
# (idempotency). Each ``(segment@)`` iteration consumes one userinfo
# segment; segments may contain colons and be empty, so every segment
# of a mangled multi-@ chain (``user:pass@user2:pass2@@host``) is
# consumed. The host must contain at least one letter, so all-numeric
# dotted tails (versions such as "npm:user@1.2.3", IP literals) are
# never mistaken for credential hosts; scheme-prefixed URLs redact
# their whole authority regardless of host shape.
_CREDENTIAL_AUTHORITY_RE = re.compile(
    r"(?<![A-Za-z0-9.\-])[A-Za-z0-9._~%-]+:(?:[^@\s/<>]*@)+"
    r"(?P<host>(?=[A-Za-z0-9.\-]*[A-Za-z])(?:[A-Za-z0-9\-]+\.)+"
    r"[A-Za-z0-9\-]+|localhost)")

_HOSTED_GIT_HOSTS = ("github", "gitlab", "bitbucket", "gist", "sourcehut")
_BARE_GIT_SHORTHAND_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._~-]+(?:#.*)?")
_SSH_PREFIX_RE = re.compile(r"^(?:git\+)?ssh://")
_SSH_HOST_RE = re.compile(r"[A-Za-z0-9.\-]+")


# ---------------------------------------------------------------------------
# URL text redaction
# ---------------------------------------------------------------------------

def redact_urls(text):
    """Redact userinfo, query, and fragment material in every URL in text.

    ``https://user:pass@host/path?x=1#y`` becomes
    ``https://<redacted>@host/path?<redacted>#<redacted>``. Nested scheme
    anchors inside one URL token (``https://host/path://u:p@evil/x``) are
    redacted too, as are credential-shaped ``user:pass@host`` authorities
    with no scheme (GitLab ``component:`` includes, bare registry
    references). Text without URLs -- versions, ranges, package names,
    aliases, plain paths -- is returned unchanged.
    """
    if not text:
        return text
    if "://" in text:
        text = _redact_url_tokens(text)
    if "@" not in text:
        return text
    return _CREDENTIAL_AUTHORITY_RE.sub(
        lambda m: f"{REDACTED}@{m.group('host')}", text)


def _redact_url_tokens(text):
    """Scheme-anchored URL token scan only (no scheme-less pass)."""
    out = []
    pos = 0
    while True:
        match = _URL_SCHEME_RE.search(text, pos)
        if not match:
            out.append(text[pos:])
            break
        start = match.start()
        out.append(text[pos:start])
        end = _url_token_end(text, match.end())
        out.append(_redact_one_url(text[start:end]))
        pos = end
    return "".join(out)


def _url_token_end(text, from_pos):
    """End of the whitespace-bounded URL token starting at from_pos,
    minus trailing punctuation that likely belongs to surrounding text."""
    n = len(text)
    end = from_pos
    while end < n and not text[end].isspace():
        end += 1
    while end > from_pos and text[end - 1] in _URL_TRAILING:
        end -= 1
    return end


# Nested scheme anchors inside one URL token (``https://h/p://u:p@e/x``)
# are walked iteratively, not recursively, under a small depth cap: past
# the cap the remaining tail collapses to ``url:<redacted>`` instead of
# recursing, so a hostile anchor chain (``"a://" * 1200``, once a
# RecursionError in the report CLI) is bounded work and fails closed.
_MAX_NESTED_URL_DEPTH = 8


def _redact_one_url(url):
    """One whitespace-bounded URL token with authority, query, and
    fragment material redacted (nested scheme anchors walked iteratively
    up to _MAX_NESTED_URL_DEPTH; a deeper tail collapses to the URL
    placeholder)."""
    match = _URL_SCHEME_RE.search(url)
    if not match:
        return url
    scheme_end = match.end()
    body = url[scheme_end:]
    body, _, fragment = body.partition("#")
    body, _, query = body.partition("?")
    out = [url[:scheme_end]]
    rest = body
    depth = 0
    while True:
        slash = rest.find("/")
        authority = rest if slash < 0 else rest[:slash]
        tail = "" if slash < 0 else rest[slash:]
        at = authority.rfind("@")
        if at > 0:
            authority = f"{REDACTED}@{authority[at + 1:]}"
        out.append(authority)
        nested = _URL_SCHEME_RE.search(tail)
        if not nested:
            out.append(tail)
            break
        depth += 1
        if depth > _MAX_NESTED_URL_DEPTH:
            out.append(URL_PLACEHOLDER)
            break
        out.append(tail[:nested.end()])
        rest = tail[nested.end():]
    result = "".join(out)
    if query:
        result += f"?{REDACTED}"
    if fragment:
        result += f"#{REDACTED}"
    return result


# ---------------------------------------------------------------------------
# Dependency reference redaction (pip-style URL/path refs)
# ---------------------------------------------------------------------------

# SCP-style Git/SSH reference scan: user@host:path with a lettered
# host, a bracketed IPv6 literal, or an empty host before the colon,
# and a non-empty path. The search form with a boundary lookbehind also
# catches mid-string shapes that reach multi-token call sites; digest
# doctrine is applied to the tail after the match before collapsing.
_SCP_REF_RE = re.compile(
    r"(?<![A-Za-z0-9._~%\-@])"
    r"[A-Za-z0-9._~%-]+@"
    r"(?P<host>\[[0-9A-Fa-f:.]{2,45}\]"
    r"|[A-Za-z0-9.\-]*[A-Za-z][A-Za-z0-9.\-]*"
    r"|)"
    r":(?=\S)")


def redact_dependency_ref(ref):
    """Redacted form of one dependency reference token (URL or path).

    Scheme URLs get userinfo/query/fragment redaction. ssh URLs and
    SCP-style Git references (``git@host:path``, ``user@host:path``,
    including mid-string, bracketed-IPv6, and empty-host shapes) collapse
    to an ``<ssh:host>`` placeholder: the user, path, and fragment never
    survive. A clean digest-pinned tail after the @ keeps the digest
    doctrine. When credential-shaped ``@`` material survives inside a
    URL-shaped token (for example whitespace split an authority), or a
    non-scheme token mixes @, colon, and path/fragment material without
    a clean digest anchor, the whole token collapses to
    ``url:<redacted>`` so nothing raw is emitted. Plain versions,
    ranges, and paths pass through unchanged.
    """
    if not isinstance(ref, str) or not ref:
        return ref
    if ref.startswith(("ssh://", "git+ssh://")):
        return ssh_placeholder(ref)
    if "://" not in ref:
        scp = _SCP_REF_RE.search(ref)
        if scp and not _DIGEST_ANCHOR_RE.fullmatch(ref, scp.start()):
            return ssh_placeholder("ssh://" + (scp.group("host") or ""))
    redacted = _redact_url_tokens(ref)
    if "://" in redacted and "@" in redacted.replace(f"{REDACTED}@", ""):
        return URL_PLACEHOLDER
    residue = redacted.replace(f"{REDACTED}@", "")
    if "@" in residue and ":" in residue and "#" in residue:
        last_at = residue.rfind("@")
        if not _DIGEST_TAIL_RE.fullmatch(residue, last_at + 1):
            return URL_PLACEHOLDER
    return redact_urls(redacted)


# ---------------------------------------------------------------------------
# VCS placeholder classification (hosted-git shorthands, ssh URLs)
# ---------------------------------------------------------------------------

def ssh_placeholder(url):
    """``<ssh:host>`` for an ssh/git+ssh URL; the host is non-secret
    structural information, the rest of the URL is never emitted."""
    rest = _SSH_PREFIX_RE.sub("", url)
    authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    authority = authority.rsplit("@", 1)[-1].rsplit(":", 1)[0]
    if _SSH_HOST_RE.fullmatch(authority):
        return f"<ssh:{authority}>"
    return "<ssh>"


def hosted_git_placeholder(spec):
    """(placeholder, host) when spec is an npm hosted-git shorthand
    (``github:org/repo``, ``gitlab:group/proj``, bare ``user/repo``,
    with optional ``#commit``); ``(None, None)`` otherwise. The raw
    shorthand is never emitted."""
    for host in _HOSTED_GIT_HOSTS:
        if spec.startswith(f"{host}:"):
            return f"<hosted-git:{host}>", host
    if _BARE_GIT_SHORTHAND_RE.fullmatch(spec):
        return "<hosted-git:github>", "github"
    return None, None


# ---------------------------------------------------------------------------
# Container image reference redaction
# ---------------------------------------------------------------------------

# Digest tail of a digest-pinned reference ("@<algorithm>:<hex>"). Only
# the algorithms registries actually serve are accepted, with their
# exact hex lengths, so a tag colon ("user:pass@img:1.0") is never
# mistaken for a digest anchor and short fake tails ("sha256:abc") are
# never exempted from credential stripping.
_DIGEST_SHAPE = (r"sha256:[0-9a-fA-F]{64}|sha1:[0-9a-fA-F]{40}"
                 r"|sha384:[0-9a-fA-F]{96}|sha512:[0-9a-fA-F]{128}")
# No ^/$ anchors: fullmatch() supplies them, including the pos-anchored
# call from _strip_path_credentials.
_DIGEST_TAIL_RE = re.compile(f"(?:{_DIGEST_SHAPE})")
_AT_RE = re.compile("@")
_SLASH_RE = re.compile("/")

# user@<algorithm>:<exact-length-hex> occupying the whole tail: a
# digest-pinned reference, not an SCP credential shape.
_DIGEST_ANCHOR_RE = re.compile(
    rf"[A-Za-z0-9._~%-]+@(?:(?:{_DIGEST_SHAPE}))")


def _strip_path_credentials(ref):
    """Strip every @-bearing path segment from an image reference.

    @ positions are scanned right to left over the original text: a
    clean digest anchor (exact per-algorithm hex tail, no second @ in
    its segment) is kept, an @ inside a scheme'd URL authority (no
    slash between the scheme and the @) is left to redact_urls, and
    every other @-bearing segment is credential material and is
    removed. All removals are collected first and applied in one join,
    so the pass is linear in the input size and its output is a fixed
    point.
    """
    ats = [m.start() for m in _AT_RE.finditer(ref)]
    if not ats:
        return ref
    slashes = [m.start() for m in _SLASH_RE.finditer(ref)]
    schemes = [(m.start(), m.end()) for m in _URL_SCHEME_RE.finditer(ref)]
    remove = []
    si = len(schemes) - 1
    cut = len(ref)
    for p in reversed(ats):
        if p >= cut:
            continue
        while si >= 0 and schemes[si][1] > p:
            si -= 1
        j = bisect.bisect_left(slashes, p) - 1
        nearest_slash = slashes[j] if j >= 0 else -1
        if si >= 0 and nearest_slash < schemes[si][1]:
            break
        seg_start = nearest_slash + 1
        if _DIGEST_TAIL_RE.fullmatch(ref, p + 1) \
                and ref.find("@", seg_start, p) < 0:
            continue
        remove.append((seg_start, p + 1))
        cut = seg_start
    if not remove:
        return ref
    out = []
    pos = len(ref)
    for start, end in remove:
        out.append(ref[end:pos])
        pos = start
    out.append(ref[:pos])
    return "".join(reversed(out))


def redact_image_reference(ref):
    """Image reference with any registry-authority userinfo removed.

    ``user:pass@registry.example.com/img:1.0`` becomes
    ``registry.example.com/img:1.0`` (stripped, not placeholder-marked,
    so downstream reference parsing stays valid). Digest references
    (``img@sha256:...``) and clean references pass through unchanged.
    Credential-bearing ``@`` segments after the first slash are stripped
    the same way; only a clean digest anchor on a repository segment
    passes through. Scheme-prefixed text (``https://user:pass@host/img:
    1.0``) is not a valid image reference and fails closed: scheme and
    authority userinfo are stripped down to a parseable
    registry/repository form, or the whole ref collapses to
    ``url:<redacted>`` when anything credential-shaped survives.
    """
    if not isinstance(ref, str) or "@" not in ref:
        return ref
    # Scheme detection must survive leading whitespace (" ARG=..."-style
    # padding), so match against the leading non-whitespace region and
    # fail closed as before.
    leading = ref.lstrip()
    scheme = _URL_SCHEME_RE.match(leading)
    if scheme:
        return _strip_scheme_image_reference(leading, scheme.end())
    slash = ref.find("/")
    head = ref if slash < 0 else ref[:slash]
    at = head.rfind("@")
    if at < 0:
        return _strip_path_credentials(ref)
    rest = head[at + 1:]
    if not rest:
        # An @ with nothing after it in the authority position carries
        # only credentials ("user:pass@/img"): strip the userinfo and
        # keep whatever follows instead of returning the raw text.
        return ref[at + 1:]
    if slash < 0 and _DIGEST_TAIL_RE.fullmatch(rest) \
            and "@" not in head[:at]:
        return ref
    return _strip_path_credentials(
        rest + ("" if slash < 0 else ref[slash:]))


def _strip_scheme_image_reference(ref, scheme_end):
    """Fail-closed redaction of a scheme-prefixed image reference.

    Strips the scheme and any authority userinfo down to a parseable
    registry/repository form; collapses to ``url:<redacted>`` when the
    remainder is not one (empty authority, surviving @ anchors, query,
    fragment, or whitespace payloads).
    """
    body = ref[scheme_end:]
    slash = body.find("/")
    authority = body if slash < 0 else body[:slash]
    tail = "" if slash < 0 else body[slash:]
    at = authority.rfind("@")
    if at >= 0:
        authority = authority[at + 1:]
    if not authority:
        return URL_PLACEHOLDER
    stripped = authority + tail
    if "@" in stripped or "?" in stripped or "#" in stripped \
            or any(ch.isspace() for ch in stripped):
        return URL_PLACEHOLDER
    return stripped


# ---------------------------------------------------------------------------
# Composed display text backstop (docker.py warning/locator sites only)
# ---------------------------------------------------------------------------

# Fail-closed backstop for COMPOSED display text only. redact_urls is
# deliberately narrow -- its credential host must be a dotted hostname
# or localhost, so versions, ranges, and alias specs (npm:user@1.2.3,
# name:tag@sha256:...) pass through unchanged everywhere -- but inside
# an unresolved ${VAR:-...} template that narrowness can leave a
# surviving ``user:pass@`` fragment with a dotless, IP-literal, or
# bracketed-IPv6 host. Composed display text therefore collapses any
# such fragment to ``url:<redacted>``: the host may be a bracketed IPv6
# literal, a dotted group of up to four digits (octal spellings), a bare
# decimal integer, or any letter-bearing token; the digest exemption
# requires a FULL digest-shaped tail (``<algo>:<hex>`` consumed to a
# non-hex boundary), so a ``sha256:``-prefixed hostname cannot exploit
# it; "<>" is excluded so the pattern never matches the markers' output
# or its own (idempotent), and colon-then-@-less tails such as
# npm:user@1.2.3 never match.
_COMPOSED_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9.\-])[A-Za-z0-9._~%-]+:(?:[^@\s/<>]*@)+"
    rf"(?!({_DIGEST_SHAPE})(?![0-9a-fA-F.+:-]))"
    r"(?P<host>\[[0-9A-Fa-f:.]{2,45}\]"
    r"|\d{1,4}(?:\.\d{1,4}){3}"
    r"|\d{7,10}"
    r"|(?=[^\s/<>@]*[A-Za-z])[^\s/<>@]*)")


def redact_display_reference(ref):
    """Composed docker display text: an unresolved-variable warning
    message or a FROM locator built from a raw (possibly templated)
    image reference.

    Applies the standard ``redact_urls(redact_image_reference(ref))``
    composition, then a fail-closed backstop that collapses any
    surviving credential-shaped ``user:pass@`` fragment (dotless or
    IP-literal host) to ``url:<redacted>``.

    Scoped to composed display text ONLY: the global helpers keep their
    narrow contracts (redact_urls never collapses these fragments), so
    alias specs, digest references, versions, and ranges pass through
    byte-identically wherever they legitimately appear.
    """
    redacted = redact_urls(redact_image_reference(ref))
    if "@" not in redacted:
        return redacted
    return _COMPOSED_CREDENTIAL_RE.sub(URL_PLACEHOLDER, redacted)
