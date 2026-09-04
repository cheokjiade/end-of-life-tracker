"""Go module proxy staleness provider.

Go modules don't publish EOL dates. Like npm_registry and maven_central, we
report what the registry knows: when the pinned version was published (the
proxy's ``.info`` document), the latest stable semantic version, and whether
the pinned version is retracted.

Queries the official Go module proxy protocol (proxy.golang.org):

- ``@v/list``            one version per line
- ``@v/<version>.info``  ``{"Version": ..., "Time": "RFC3339"}``
- ``@latest``            same JSON shape, for the latest known version
- ``@v/<version>.mod``   the go.mod served for a version

Module paths are escaped per the proxy protocol: every uppercase ASCII
letter becomes ``!`` + lowercase (``github.com/Azure/foo`` ->
``github.com/!azure/foo``).

Retraction is reported **only** when the authoritative proxy data establishes
it: a ``retract`` directive in the go.mod the proxy serves for the latest
stable release covering the pinned version (the same mechanism the go command
uses). When it cannot be determined — pseudo-version pin, no stable release
to consult, unparsable pin — ``retracted`` is None, ``retraction_note``
records why, and retraction is explicitly not reported.

Prerelease and pseudo-version pins are handled conservatively: they are
reported factually with their timestamp, never claimed to be "behind" a
stable release, and pseudo-versions are never reported as retracted. No EOL
date is ever claimed: module age is not a lifecycle.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from ..core import _error_result, logger, read_response_bytes

_GO_PROXY_BASE = "https://proxy.golang.org"
# Cache namespaces (a run checks many modules against one proxy):
#   ("list", escaped_module)                  -> @v/list body | None
#   ("latest", escaped_module)                -> @latest body | None
#   ("info", escaped_module, version)         -> .info body | None
#   ("mod", escaped_module, version)          -> .mod body | None
_GO_CACHE = {}

_SEMVER_RE = re.compile(
    r"^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?(?:\+([0-9A-Za-z.\-]+))?$"
)
# Canonical Go pseudo-version suffixes: -yyyymmddhhmmss-hash or
# -0.yyyymmddhhmmss-hash (14-digit timestamp, 12-char hash).
_PSEUDO_RE = re.compile(r"-(?:0\.\d{14}|\d{14})-[0-9a-f]{12}$")
_RETRACT_RANGE_RE = re.compile(r"\[\s*([^\s,\]]+)\s*,\s*([^\s,\]]+)\s*\]")
_RETRACT_VERSION_RE = re.compile(r"\bv\d[0-9A-Za-z.\-+]*")


# ---------------------------------------------------------------------------
# Fetch layer (cached; the pure transformations below are testable without it)
# ---------------------------------------------------------------------------

def _fetch_proxy(url):
    """GET *url* from the module proxy and return the decoded body."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json, text/plain; charset=utf-8",
        "User-Agent": "EOL-Tracker/1.0",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return read_response_bytes(resp).decode("utf-8", "replace")


def _fetch_cached(key, url):
    """Cached proxy GET. 404/410 -> None (negative-cached); other errors raise."""
    if key in _GO_CACHE:
        return _GO_CACHE[key]
    try:
        body = _fetch_proxy(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            _GO_CACHE[key] = None
            return None
        raise
    _GO_CACHE[key] = body
    return body


# ---------------------------------------------------------------------------
# Pure transformations — no network, unit-test these directly
# ---------------------------------------------------------------------------

def _escape_module_path(module):
    """Pure: escape a module path per the proxy protocol.

    Every uppercase ASCII letter becomes '!' + its lowercase form; everything
    else passes through unchanged.
    """
    out = []
    for ch in module:
        if "A" <= ch <= "Z":
            out.append("!")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _module_url(escaped, tail):
    """Pure: proxy endpoint URL for an escaped module path + tail ('@v/list')."""
    return f"{_GO_PROXY_BASE}/{urllib.parse.quote(escaped, safe='/._~-!')}/{tail}"


def _info_url(escaped, version):
    """Pure: URL of the @v/<version>.info endpoint."""
    q = urllib.parse.quote(version, safe=".+_~-")
    return _module_url(escaped, f"@v/{q}.info")


def _mod_url(escaped, version):
    """Pure: URL of the @v/<version>.mod endpoint."""
    q = urllib.parse.quote(version, safe=".+_~-")
    return _module_url(escaped, f"@v/{q}.mod")


def _parse_version_list(raw):
    """Pure: @v/list body -> list of version strings (blank lines dropped)."""
    if raw is None:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _parse_rfc3339_date(value):
    """Pure: leading date of an RFC3339 timestamp -> datetime.date or None."""
    if not isinstance(value, str):
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?=$|[\sT])", value.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_info_doc(raw):
    """Pure: a .info or @latest body -> {"version", "time"} or None if malformed.

    ``time`` is a datetime.date or None; a document that is present but not a
    JSON object, or invalid JSON, yields None (malformed = error upstream).
    """
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    ver = doc.get("Version")
    version = ver.strip() if isinstance(ver, str) and ver.strip() else None
    return {"version": version, "time": _parse_rfc3339_date(doc.get("Time"))}


def _parse_semver(version):
    """Pure: parse 'vMAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]'.

    Returns ((major, minor, patch), prerelease_ids) where numeric prerelease
    identifiers are ints (semver precedence) — or None when not valid semver.
    Build metadata is ignored, per semver.
    """
    m = _SEMVER_RE.match(version or "")
    if not m:
        return None
    core = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    pre = m.group(4)
    if not pre:
        return (core, ())
    ids = tuple(int(p) if p.isdigit() else p for p in pre.split("."))
    return (core, ids)


def _compare_semver(a, b):
    """Pure: semver precedence compare -> -1 / 0 / 1, or None if either invalid."""
    pa, pb = _parse_semver(a), _parse_semver(b)
    if pa is None or pb is None:
        return None
    if pa[0] != pb[0]:
        return -1 if pa[0] < pb[0] else 1
    pre_a, pre_b = pa[1], pb[1]
    if pre_a == pre_b:
        return 0
    if not pre_a:
        return 1     # a release outranks any of its prereleases
    if not pre_b:
        return -1
    for x, y in zip(pre_a, pre_b):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        if isinstance(x, int):
            return -1  # numeric identifiers have lower precedence
        if isinstance(y, int):
            return 1
        return -1 if x < y else 1
    return -1 if len(pre_a) < len(pre_b) else 1


def _is_pseudo_version(version):
    """Pure: True for the canonical Go pseudo-version timestamp-hash suffixes."""
    return bool(_PSEUDO_RE.search(version or ""))


def _is_stable(version):
    """Pure: a tagged release — valid semver, no prerelease, not a pseudo-version."""
    parsed = _parse_semver(version)
    return parsed is not None and not parsed[1] and not _is_pseudo_version(version)


def _latest_stable(versions):
    """Pure: highest stable semantic version in *versions*, or None."""
    best = None
    for v in versions:
        if not _is_stable(v):
            continue
        if best is None or _compare_semver(v, best) == 1:
            best = v
    return best


def _parse_retractions(mod_text):
    """Pure: go.mod text -> [(low, high, reason), ...] from retract directives.

    ``high`` is None for a single retracted version; ``reason`` is the line's
    trailing // comment ("" when absent). Unparsable tokens are skipped —
    never guess a retraction.
    """
    retracts = []
    in_block = False
    for line in (mod_text or "").splitlines():
        code, _, comment = line.partition("//")
        code = code.strip()
        reason = comment.strip()
        if in_block:
            if code.startswith(")"):
                in_block = False
                code = code[1:].strip()
            if code:
                retracts.extend(_parse_retract_tokens(code, reason))
            continue
        if not code.startswith("retract"):
            continue
        rest = code[len("retract"):]
        if rest and rest[0] not in " \t(":
            continue  # e.g. a directive-looking word such as 'retracted'
        rest = rest.strip()
        if rest.startswith("("):
            rest = rest[1:].strip()
            if rest.endswith(")") and rest.count(")") == 1:
                rest = rest[:-1].strip()  # one-line 'retract ( v1.0.0 )'
            else:
                in_block = True
        if rest:
            retracts.extend(_parse_retract_tokens(rest, reason))
    return retracts


def _parse_retract_tokens(text, reason):
    """Pure: one retract directive's content -> [(low, high, reason), ...]."""
    out = []
    for m in _RETRACT_RANGE_RE.finditer(text):
        low, high = m.group(1).strip('"'), m.group(2).strip('"')
        if _parse_semver(low) and _parse_semver(high):
            out.append((low, high, reason))
    remainder = _RETRACT_RANGE_RE.sub(" ", text)
    for m in _RETRACT_VERSION_RE.finditer(remainder):
        v = m.group(0).rstrip(".").strip('"')
        if _parse_semver(v):
            out.append((v, None, reason))
    return out


def _is_retracted(version, retracts):
    """Pure: True when a retract directive covers *version* (semver precedence).

    go.mod retract forms are a single version or a closed [low, high] range;
    a single version covers exactly itself.
    """
    if not _parse_semver(version):
        return False
    for low, high, _reason in retracts:
        eq_low = _compare_semver(version, low)
        if eq_low is None:
            continue
        if high is None:
            if eq_low == 0:
                return True
        else:
            le_high = _compare_semver(version, high)
            if eq_low >= 0 and le_high is not None and le_high <= 0:
                return True
    return False


def _module_problem(module):
    """Pure: an error message for a module path we refuse to query, else None."""
    if not module:
        return "go_proxy entries require 'module'"
    if any(ch.isspace() for ch in module):
        return f"Module path '{module}' contains whitespace; refusing to query the proxy"
    if "@" in module:
        return f"Module path '{module}' contains '@'; refusing to query the proxy"
    return None


# ---------------------------------------------------------------------------
# Result composition (pure — the provider only fetches and injects documents)
# ---------------------------------------------------------------------------

def _go_error(entry, message, module, version):
    """Error-shaped result with go_proxy's keys filled in."""
    result = _error_result(entry, message)
    result["source"] = "go_proxy"
    result["product"] = module
    result["version"] = version
    result["label"] = entry.get("label") or f"{module or '?'} {version}".strip()
    return result


def _go_result_from_data(entry, data, today):
    """Pure: normalized result from fetched proxy documents, then a visible
    note when retraction was attempted but could not be determined."""
    result = _compose_go_result(entry, data, today)
    if result["retraction_note"]:
        result["message"] += f" ({result['retraction_note']})"
    return result


def _compose_go_result(entry, data, today):
    """Pure: build the normalized result from fetched proxy documents.

    data keys:
      module          validated module path (str)
      version         normalized pinned version ('' when absent)
      listed          versions from @v/list ([] when the endpoint 404'd)
      pinned_doc      parsed .info of the pin, or None when not on the proxy
      latest_doc      parsed @latest, or None
      latest_date     publish date of the chosen latest stable release
      mod_text        go.mod served for the latest stable release (or None)
      retraction_note why retraction is undetermined, when it was attempted
    """
    module = data["module"]
    version = data["version"]
    label = entry.get("label") or f"{module} {version}".strip()

    candidates = list(data.get("listed") or [])
    latest_ver = (data.get("latest_doc") or {}).get("version")
    if latest_ver:
        candidates.append(latest_ver)
    latest_v = _latest_stable(candidates)
    latest_date = data.get("latest_date")

    retracted = None
    retraction_reason = None
    mod_text = data.get("mod_text")
    if version and mod_text is not None and _parse_semver(version) \
            and not _is_pseudo_version(version):
        for low, high, reason in _parse_retractions(mod_text):
            if _is_retracted(version, [(low, high, reason)]):
                retracted = True
                retraction_reason = reason or None
                break
        else:
            retracted = False

    pinned_doc = data.get("pinned_doc")
    pinned_date = (pinned_doc or {}).get("time")
    in_use = str(pinned_date) if pinned_date else None

    result = {
        "label": label,
        "product": module,
        "version": version,
        "lts": False,
        "status": "ok",
        "message": "",
        "in_use_release_date": in_use,
        "latest_patch": latest_v,
        "latest_patch_date": str(latest_date) if latest_date else None,
        "latest_cycle": None,
        "latest_cycle_version": None,
        "latest_cycle_release_date": None,
        "on_latest_cycle": False,
        "eol_date": None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "retracted": retracted,
        "retraction_reason": retraction_reason,
        "retraction_note": data.get("retraction_note"),
        "source": "go_proxy",
    }

    latest_bits = None
    if latest_v:
        latest_bits = f"latest stable is {latest_v}"
        if latest_date:
            latest_bits += f" ({latest_date})"

    if retracted:
        result["status"] = "eol"
        message = f"{version} is retracted by upstream"
        if retraction_reason:
            message += f": {retraction_reason}"
        if latest_bits:
            message += f"; {latest_bits}"
        result["message"] = message
        return result

    if not version:
        result["message"] = (
            f"No pinned version provided; {latest_bits}" if latest_bits
            else "No pinned version provided and no stable release on Go module proxy"
        )
        return result

    on_latest = latest_v is not None and _compare_semver(version, latest_v) == 0
    result["on_latest_cycle"] = on_latest

    if pinned_doc is None:
        result["status"] = "unknown"
        message = f"Version {version} not found on Go module proxy (private build?)"
        if latest_bits:
            message += f"; {latest_bits}"
        result["message"] = message
        return result

    if _is_pseudo_version(version):
        message = f"Pseudo-version {version}"
        if in_use:
            message += f" (published {in_use})"
        message += f"; {latest_bits}" if latest_bits \
            else " (module has no stable tagged release)"
        result["message"] = message
        return result

    parsed = _parse_semver(version)
    if parsed is not None and parsed[1]:
        message = f"Prerelease {version}"
        if in_use:
            message += f" (published {in_use})"
        if latest_bits:
            message += f"; {latest_bits}"
        result["message"] = message
        return result

    if latest_v is None:
        message = f"No stable tagged release on Go module proxy; in use {version}"
        if in_use:
            message += f" ({in_use})"
        result["message"] = message
        return result

    if on_latest:
        message = f"On latest stable release ({latest_v})"
        if latest_date:
            message += f" published {latest_date}"
        result["message"] = message
        return result

    message = f"In use {version}"
    if in_use:
        message += f" ({in_use})"
    message += f"; latest stable {latest_v}"
    if latest_date:
        message += f" ({latest_date}"
        if pinned_date and latest_date > pinned_date:
            message += f", {(latest_date - pinned_date).days} days newer"
        message += ")"
    result["message"] = message
    return result


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

def _provider_go_proxy(entry, today):
    """Report proxy-known facts for a Go module pin. No EOL date is claimed."""
    module = str(entry.get("module") or "").strip()
    raw_version = entry.get("version")
    version = str(raw_version).strip() if raw_version is not None else ""
    # The proxy needs the canonical lowercase 'v' prefix; accept 1.2.3 / V1.2.3.
    if version:
        version = "v" + version[1:] if version[0] in "vV" else "v" + version

    problem = _module_problem(module)
    if problem:
        return _go_error(entry, problem, module or None, version)

    def query_failed(exc):
        logger.error("Go module proxy fetch failed for %s: %s", module, exc)
        return _go_error(entry, f"Go module proxy query failed: {exc}", module, version)

    escaped = _escape_module_path(module)
    try:
        list_raw = _fetch_cached(("list", escaped), _module_url(escaped, "@v/list"))
        latest_raw = _fetch_cached(("latest", escaped), _module_url(escaped, "@latest"))
    except Exception as exc:
        return query_failed(exc)

    if list_raw is None and latest_raw is None:
        return _go_error(
            entry, f"Module '{module}' not found on Go module proxy", module, version)

    listed = _parse_version_list(list_raw)
    latest_doc = _parse_info_doc(latest_raw) if latest_raw is not None else None
    if latest_raw is not None and latest_doc is None:
        logger.warning("Go module proxy returned a malformed @latest document for %s", module)

    candidates = list(listed)
    if latest_doc and latest_doc.get("version"):
        candidates.append(latest_doc["version"])
    latest_v = _latest_stable(candidates)

    latest_date = None
    if latest_v:
        if latest_doc and latest_doc.get("version") == latest_v:
            latest_date = latest_doc.get("time")
        else:
            try:
                lraw = _fetch_cached(("info", escaped, latest_v), _info_url(escaped, latest_v))
            except Exception as exc:
                return query_failed(exc)
            ldoc = _parse_info_doc(lraw) if lraw is not None else None
            if ldoc is None:
                return _go_error(
                    entry,
                    f"Go module proxy lists {latest_v} but its .info document "
                    "is missing or malformed; source may have changed",
                    module, version)
            latest_date = ldoc.get("time")

    pinned_doc = None
    if version:
        try:
            praw = _fetch_cached(("info", escaped, version), _info_url(escaped, version))
        except Exception as exc:
            return query_failed(exc)
        if praw is not None:
            pinned_doc = _parse_info_doc(praw)
            if pinned_doc is None:
                return _go_error(
                    entry,
                    f"Malformed .info document for {version} on Go module proxy",
                    module, version)

    mod_text = None
    retraction_note = None
    if version:
        if not _parse_semver(version):
            retraction_note = "retraction not determined: unparsable pinned version"
        elif _is_pseudo_version(version):
            retraction_note = "retraction not determined: pseudo-version pin"
        elif latest_v is None:
            retraction_note = "retraction not determined: no stable release to consult"
        else:
            try:
                mod_text = _fetch_cached(("mod", escaped, latest_v), _mod_url(escaped, latest_v))
            except Exception as exc:
                return query_failed(exc)
            if mod_text is None:
                return _go_error(
                    entry,
                    f"go.mod for {latest_v} is unavailable on Go module proxy; "
                    "retraction status cannot be established",
                    module, version)

    return _go_result_from_data(entry, {
        "module": module,
        "version": version,
        "listed": listed,
        "pinned_doc": pinned_doc,
        "latest_doc": latest_doc,
        "latest_date": latest_date,
        "mod_text": mod_text,
        "retraction_note": retraction_note,
    }, today)


SOURCE = "go_proxy"
LABEL = "Go proxy"
provider = _provider_go_proxy


def url_for(r):
    """Upstream link: the module (at the pinned version) on pkg.go.dev."""
    product = r.get("product") or ""
    if not product:
        return None
    version = r.get("version") or ""
    product = urllib.parse.quote(str(product), safe="/")
    version = urllib.parse.quote(str(version), safe="")
    return f"https://pkg.go.dev/{product}" + (f"@{version}" if version else "")
