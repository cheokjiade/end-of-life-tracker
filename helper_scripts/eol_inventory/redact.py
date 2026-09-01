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


def _redact_one_url(url):
    """One whitespace-bounded URL token with authority, query, and
    fragment material redacted (recursing into nested scheme anchors)."""
    match = _URL_SCHEME_RE.search(url)
    if not match:
        return url
    scheme_end = match.end()
    body = url[scheme_end:]
    body, _, fragment = body.partition("#")
    body, _, query = body.partition("?")
    slash = body.find("/")
    authority = body if slash < 0 else body[:slash]
    tail = "" if slash < 0 else body[slash:]
    at = authority.rfind("@")
    if at > 0:
        authority = f"{REDACTED}@{authority[at + 1:]}"
    if "://" in tail:
        tail = redact_urls(tail)
    out = url[:scheme_end] + authority + tail
    if query:
        out += f"?{REDACTED}"
    if fragment:
        out += f"#{REDACTED}"
    return out


# ---------------------------------------------------------------------------
# Dependency reference redaction (pip-style URL/path refs)
# ---------------------------------------------------------------------------

def redact_dependency_ref(ref):
    """Redacted form of one dependency reference token (URL or path).

    Scheme URLs get userinfo/query/fragment redaction. ssh URLs collapse
    to an ``<ssh:host>`` placeholder. When credential-shaped ``@``
    material survives inside a URL-shaped token (for example whitespace
    split an authority), the whole token collapses to ``url:<redacted>``
    so nothing raw is emitted. Plain versions, ranges, and paths pass
    through unchanged.
    """
    if not isinstance(ref, str) or not ref:
        return ref
    if ref.startswith(("ssh://", "git+ssh://")):
        return ssh_placeholder(ref)
    redacted = _redact_url_tokens(ref)
    if "://" in redacted and "@" in redacted.replace(f"{REDACTED}@", ""):
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

# Digest tail of a slash-less reference ("@<algorithm>:<hex>"). The
# algorithm is restricted to the algorithms registries actually serve,
# so a tag colon ("user:pass@img:1.0") is never mistaken for a digest
# anchor and its userinfo is stripped instead of passed through.
_DIGEST_TAIL_RE = re.compile(r"^(?:sha256|sha384|sha512|sha1):[0-9a-fA-F]+$")


def redact_image_reference(ref):
    """Image reference with any registry-authority userinfo removed.

    ``user:pass@registry.example.com/img:1.0`` becomes
    ``registry.example.com/img:1.0`` (stripped, not placeholder-marked,
    so downstream reference parsing stays valid). Digest references
    (``img@sha256:...``) and clean references pass through unchanged.
    Scheme-prefixed text (``https://user:pass@host/img:1.0``) is not a
    valid image reference and fails closed: scheme and authority
    userinfo are stripped down to a parseable registry/repository form,
    or the whole ref collapses to ``url:<redacted>`` when anything
    credential-shaped survives.
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
        return ref
    rest = head[at + 1:]
    if not rest:
        return ref
    if slash < 0 and _DIGEST_TAIL_RE.fullmatch(rest) \
            and "@" not in head[:at]:
        return ref
    return rest + ("" if slash < 0 else ref[slash:])


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
