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
import ipaddress
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
_SSH_PREFIX_RE = re.compile(r"^[+]{0,2}(?:git\+)?ssh://", re.IGNORECASE)
_SSH_HOST_RE = re.compile(r"[A-Za-z0-9.\-]+")
_HOST_LETTER_RE = re.compile(r"[A-Za-z]")
# ssh-scheme tokens and SCP-style references, for display-text scanning:
# case-insensitive schemes, bracketed IPv6 / lettered / dotted-quad hosts.
_DISPLAY_SSH_RE = re.compile(
    r"(?<![A-Za-z0-9._~%\-@])"
    r"(?:(?:git\+)?ssh://\S*"
    r"|[A-Za-z0-9._~%-]+@"
    r"(?P<scp_host>\[[0-9A-Fa-f:.]{2,45}\]"
    r"|[A-Za-z0-9.\-]*[A-Za-z][A-Za-z0-9.\-]*"
    r"|[0-9.]+"
    r"|)"
    r":\S*)",
    re.IGNORECASE)
# Colon-bearing userinfo before an @ with a path, fragment, or empty
# tail (user:pass@intranet/path, user:pass@#frag): passwords never
# survive, whatever the host shape. The character classes reach past
# ASCII so unicode passwords are caught too.
_USERINFO_AT_RE = re.compile(
    r"(?<![A-Za-z0-9._~%\-@])[^@\s:]+:[^@\s]+@")

_SCP_NO_COLLAPSE_HOSTS = ("npm", "workspace")
# A real scheme anchors the token (or, for marker-bearing tokens,
# precedes the redaction marker); a planted :// after the secret is
# attacker material, not a scheme.
_SCHEME_START_RE = re.compile(r"^[+]{0,2}(?:git\+)?[A-Za-z][A-Za-z0-9+.\-]*://")
_SCHEME_ANYWHERE_RE = re.compile(
    r"(?<![A-Za-z0-9+.\-#@/])(?:git\+)?[A-Za-z][A-Za-z0-9+.\-]*://")


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
        nested = _URL_SCHEME_RE.search(tail) if "://" in tail else None
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
    r"|[0-9.]+"
    r"|)"
    r":(?=\S)")


def scp_ref_collapses(ref):
    """True when *ref* (a scheme-less token) is an SCP-style reference
    that must collapse to a host-only placeholder. Used to gate
    re-joining a bare-line direct reference before redaction."""
    if not isinstance(ref, str) or not ref or "://" in ref \
            or ":" not in ref:
        return False
    scp = _SCP_REF_RE.search(ref)
    if not scp or _DIGEST_ANCHOR_RE.fullmatch(ref, scp.start()):
        return False
    if (scp.group("host") or "") in _SCP_NO_COLLAPSE_HOSTS:
        return False
    return _scp_collapse_host(scp.group("host") or "", ref[scp.end():])


def redact_dependency_ref(ref):
    """Redacted form of one dependency reference token (URL or path).

    Scheme URLs get userinfo/query/fragment redaction. ssh URLs and
    SCP-style Git references (``git@host:path``, ``user@host:path``,
    including mid-string, bracketed-IPv6, and empty-host shapes) collapse
    to an ``<ssh:host>`` placeholder: the user, path, and fragment never
    survive. A clean digest-pinned tail after the @ keeps the digest
    doctrine. When credential-shaped ``@`` material survives inside a
    URL-shaped token (for example whitespace split an authority), or a
    non-scheme token mixes @, colon, and whitespace or fragment material
    without a clean digest anchor, the whole token collapses to
    ``url:<redacted>`` so nothing raw is emitted. Plain versions,
    ranges, and paths pass through unchanged.
    """
    if not isinstance(ref, str) or not ref:
        return ref
    if _SSH_PREFIX_RE.match(ref):
        return ssh_placeholder(ref)
    if not _SCHEME_START_RE.match(ref):
        if not _NPM_ALIAS_RE.fullmatch(ref):
            if "@" in ref and _USERINFO_AT_RE.search(ref):
                after = ref[ref.index("@") + 1:]
                if "/" in after or "#" in after or ":" in after \
                        or not after:
                    # Colon-bearing userinfo with a path, fragment, or
                    # empty tail: the password never survives.
                    return URL_PLACEHOLDER
            if ":" in ref:
                scp = _SCP_REF_RE.search(ref)
                if scp and not _DIGEST_ANCHOR_RE.fullmatch(
                        ref, scp.start()) \
                        and _scp_collapse_host(scp.group("host") or "",
                                               ref[scp.end():]):
                    return ssh_placeholder(
                        "ssh://" + (scp.group("host") or ""))
    redacted = _redact_url_tokens(ref) if "://" in ref else ref
    if "://" in redacted and "@" in redacted.replace(f"{REDACTED}@", ""):
        return URL_PLACEHOLDER
    scheme_match = _SCHEME_ANYWHERE_RE.search(redacted)
    if scheme_match and not _SCHEME_START_RE.match(redacted):
        # Operator-prefixed URL specs (e.g. '== https://...'): URL
        # doctrine applies — unless mangled query/fragment material
        # follows the URL token, which is the planted-tail leak shape.
        ws = _WHITESPACE_RE.search(redacted, scheme_match.start())
        if ws and ("?" in redacted[ws.start():]
                   or "#" in redacted[ws.start():]):
            return URL_PLACEHOLDER
        return redact_urls(redacted)
    residue = redacted.replace(f"{REDACTED}@", "")
    if "@" not in residue and ":" in residue and "/" in residue \
            and "#" in residue \
            and not _SCHEME_ANYWHERE_RE.search(residue):
        # A scheme-less token mixing a colon, a path, and a fragment
        # without any @ is a bare host:path#fragment reference
        # (github.com:credential/private.git#token) or mangled junk:
        # no supported manifest grammar produces it, so fail closed.
        return URL_PLACEHOLDER
    if "@" in redacted and ":" in redacted:
        last_at = redacted.rfind("@")
        # The digest exemption covers only a single-@ token. Any other
        # multi-@ token is anomalous in every supported manifest grammar
        # and can carry credentials between the earlier @ and a
        # digest-shaped, empty, or junk tail, so it fails closed unless
        # the WHOLE token matches the benign scoped-npm-alias grammar
        # (npm:@scope/pkg@^1.2.3, pkg@workspace:*, pkg@npm:latest) — a
        # tail-only test would re-admit user:SECRET@img@1.2.3 payloads.
        single_anchor = (redacted.find("@") == last_at
                         and _DIGEST_TAIL_RE.fullmatch(redacted,
                                                       last_at + 1))
        alias = bool(_NPM_ALIAS_RE.fullmatch(redacted))
        if not single_anchor and not alias and (
                redacted.find("@") != last_at
                or _WHITESPACE_RE.search(redacted)
                or ("#" in residue and "@" in residue)) \
                and not _SCHEME_ANYWHERE_RE.search(redacted):
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


def _is_valid_ipv4(host):
    """True when *host* is a valid dotted-quad IPv4 literal."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


def _scp_collapse_host(host, tail):
    """True when an SCP match at *host* with *tail* after the colon must
    collapse: lettered, bracketed, or empty hosts are SCP shapes; digit
    junk (user@1.2.3:x) survives unless the tail carries a path
    separator, the host has four or more dot-separated segments, or the
    tail itself is colon-bearing (version tails never are)."""
    if not host or host.startswith("["):
        return True
    if _HOST_LETTER_RE.search(host):
        return True
    if _is_valid_ipv4(host):
        return True
    if "/" in tail or "\\" in tail or ":" in tail:
        return True
    return host.count(".") >= 3


def _registry_port_ok(text):
    """True when every colon-separated segment after the host (the text
    before the first slash, bracketed IPv6 handled) is a numeric port;
    a password fragment in port position fails closed."""
    if text.startswith("["):
        bracket = text.find("]")
        if bracket < 0:
            return False
        remainder = text[bracket + 1:]
        if remainder == "":
            return True
        if not remainder.startswith(":"):
            # Junk between the closing bracket and the colon is not a
            # registry grammar shape: fail closed.
            return False
        port_text = remainder[1:].split("/", 1)[0]
    else:
        port_text = "/".join(":".join(text.split(":")[1:]).split("/",
                                                             1)[:1])
    return port_text == "" or all(
        p.isascii() and p.isdigit() for p in port_text.split(":"))


def _ssh_token_placeholder(token):
    """Host-only placeholder for one ssh-scheme token (any scheme case)."""
    body = token[token.index("://") + 3:]
    authority = re.split(r"[/?#]", body, maxsplit=1)[0]
    if authority.startswith("["):
        host = authority[:authority.index("]") + 1] if "]" in authority \
            else ""
    else:
        host = authority.rsplit("@", 1)[-1].rsplit(":", 1)[0]
    if _SSH_HOST_RE.fullmatch(host):
        return f"<ssh:{host}>"
    return "<ssh>"


def redact_display_text(text):
    """Display text with URL and SSH/SCP/VCS reference material redacted.

    One bounded sanitizer for untrusted report fields: URL userinfo,
    query, and fragment redaction first, then case-insensitive
    ssh-scheme tokens and SCP-style references collapse to host-only
    placeholders wherever they appear, including embedded in prose.
    Dates, counts, names, paths, package aliases, digest anchors, and
    already-redacted placeholders pass through unchanged; the scan is
    linear and idempotent.
    """
    if not isinstance(text, str) or not text:
        return text
    redacted = redact_urls(text)
    def _collapse_token(match):
        token = match.group(0)
        marker_at = token.find(f"{REDACTED}@")
        scheme_at = token.find("://")
        if scheme_at >= 0 and (marker_at < 0 or scheme_at < marker_at):
            # A scheme preceding any redaction marker is a real URL:
            # userinfo/query/fragment redaction is the contract; path
            # material survives.
            return token
        if marker_at >= 0:
            # Marker-bearing without a preceding scheme: the URL
            # authority collapsed and any surviving tail is
            # host-reachable material — collapse.
            return URL_PLACEHOLDER
        m2 = _USERINFO_AT_RE.search(token) if "@" in token else None
        if m2 and not _DIGEST_ANCHOR_RE.fullmatch(token) \
                and not _NPM_ALIAS_RE.fullmatch(token):
            after = token[m2.end():]
            if "/" in after or "#" in after or ":" in after or not after:
                # Colon-bearing userinfo with a path, fragment, or empty
                # tail: the password never survives.
                return URL_PLACEHOLDER
        if "@" not in token and ":" in token and "/" in token \
                and "#" in token:
            # A scheme-less token mixing a colon, a path, and a fragment
            # without any @ is a bare host:path#fragment reference or
            # mangled junk; @-bearing tokens are the SSH scan's job.
            return URL_PLACEHOLDER
        return token

    redacted = re.sub(r"\S+", _collapse_token, redacted)
    out = []
    pos = 0
    for match in _DISPLAY_SSH_RE.finditer(redacted):
        token = match.group(0)
        if token.lower().startswith(("ssh://", "git+ssh://")):
            placeholder = _ssh_token_placeholder(token)
        else:
            host = match.group("scp_host") or ""
            anchor = _DIGEST_ANCHOR_RE.match(redacted, match.start())
            if anchor and not redacted[anchor.end():match.end()].strip(
                    "_.,;:!?)]}\"'-"):
                # The token is a digest anchor plus optional trailing
                # punctuation: a pinned reference, not an SCP shape.
                continue
            if host in _SCP_NO_COLLAPSE_HOSTS:
                continue
            host_start = match.start("scp_host") - match.start()
            tail = token[len(host) + host_start + 1:]
            if not _scp_collapse_host(host, tail):
                continue
            placeholder = ssh_placeholder("ssh://" + host)
        out.append(redacted[pos:match.start()])
        out.append(placeholder)
        pos = match.end()
    out.append(redacted[pos:])
    redacted = "".join(out)
    # Context pass: query/fragment tokens following URL or SSH material
    # are planted tails; collapse whole runs of them to a fixpoint so
    # the output is idempotent regardless of chain length.
    for _ in range(6):
        out = []
        pos = 0
        prev_url_like = False
        in_tail_run = False
        changed = False
        for match in re.finditer(r"\S+", redacted):
            token = match.group(0)
            tail_like = token[:1] in ("?", "#")
            if (prev_url_like or in_tail_run) and tail_like:
                out.append(redacted[pos:match.start()])
                out.append(URL_PLACEHOLDER)
                pos = match.end()
                in_tail_run = True
                changed = True
            else:
                in_tail_run = False
            prev_url_like = "://" in token or token.startswith(
                ("<ssh", "url:", "<redacted>"))
        out.append(redacted[pos:])
        redacted = "".join(out)
        if not changed:
            break
    return redacted


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
_WHITESPACE_RE = re.compile(r"\s")
# Version-spec tail of a scoped npm alias (npm:@scope/pkg@^1.2.3,
# pkg@workspace:*): the one benign multi-@ shape.
_NPM_ALIAS_RE = re.compile(
    r"^(?:npm:)?(?:@[A-Za-z0-9._~%-]+/)?[A-Za-z0-9._~%-]+"
    r"@(?:npm:|workspace:)?[\^~*<>=]?[vV]?[\w.+!~-]*$")


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
    leading = ref.lstrip().lstrip("+")
    scheme = _URL_SCHEME_RE.match(leading)
    if scheme:
        return _strip_scheme_image_reference(leading, scheme.end())
    slash = ref.find("/")
    head = ref if slash < 0 else ref[:slash]
    at = head.rfind("@")
    if at < 0:
        return _strip_path_credentials(ref)
    rest = head[at + 1:]
    if not rest or rest[:1] in ("#", "@", "?"):
        # An @ whose authority is empty or starts with a fragment,
        # another @, or query material carries only credentials and no
        # parseable registry host: fail closed.
        return URL_PLACEHOLDER
    if slash >= 0 and ":" in rest and not _registry_port_ok(rest):
        # Registry-port position: every colon-separated segment after
        # the host must be numeric; a password fragment in port
        # position fails closed.
        return URL_PLACEHOLDER
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
    if ":" in authority and not _registry_port_ok(authority):
        # Every segment after the host must be a numeric port; a
        # password fragment in port position fails closed.
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
