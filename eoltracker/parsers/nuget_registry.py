"""NuGet V3 registry provider.

NuGet packages carry no EOL dates, so like npm_registry we report what the
registry knows: when the pinned version was published, the latest listed
stable version and its date, and how far behind the pin is. The hard signals
NuGet does expose — per-version deprecation and unlisted (hidden) releases —
are surfaced as alerts without ever claiming an EOL date.

Walks the official V3 protocol: the service index is fetched first to locate
the registration resource, then the package's registration index is read
(following paged leaves), matching the pinned version case-insensitively and
with NuGet-style version normalization (1.2 == 1.2.0 == 1.2.0.0, build
metadata ignored). Registration content may arrive gzipped, which is handled
transparently (decompressed size stays bounded by core.decompress_gzip_bytes).
A published date of 1900-01-01 is NuGet's "unknown" marker.

Per-lookup (provider invocation) cumulative budgets, all failing loudly as
error results when exhausted:
  - _NUGET_MAX_REQUESTS     — total HTTP requests issued by one lookup
                              (service index + registration + paged leaves)
  - _NUGET_MAX_TOTAL_BYTES  — total wire bytes downloaded across those
                              requests (aligned with MAX_HTTP_BODY_BYTES,
                              the per-response cap)
  - _NUGET_MAX_LEAVES       — catalogEntry dicts retained while walking one
                              registration (already enforced in
                              _collect_leaves)
Each response is additionally bounded per-response by _NUGET_BODY_BYTES.
"""

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from ..core import (
    MAX_HTTP_BODY_BYTES,
    _error_result,
    decompress_gzip_bytes,
    logger,
    read_response_bytes,
)

_NUGET_SERVICE_INDEX = "https://api.nuget.org/v3/index.json"
_NUGET_STALE_MONTHS = 24
_NUGET_UNKNOWN_DATE = date(1900, 1, 1)
_NUGET_CACHE = {}
_NUGET_BODY_BYTES = MAX_HTTP_BODY_BYTES
_NUGET_MAX_PAGES = 256
_NUGET_MAX_LEAVES = 100_000
_NUGET_MAX_REQUESTS = 256
_NUGET_MAX_TOTAL_BYTES = MAX_HTTP_BODY_BYTES

# Per-invocation fetch budget, consulted by _http_get_json. Thread-local so a
# budget belongs to exactly one provider call even if checks ever run
# concurrently in threads.
_FETCH_BUDGET = threading.local()


class _NugetBudget:
    """Cumulative per-lookup fetch budgets; raises ValueError on exhaustion
    so the lookup stops fetching and surfaces a loud error result."""

    __slots__ = ("max_requests", "max_bytes", "requests", "bytes")

    def __init__(self):
        self.max_requests = _NUGET_MAX_REQUESTS
        self.max_bytes = _NUGET_MAX_TOTAL_BYTES
        self.requests = 0
        self.bytes = 0

    def begin_request(self):
        self.requests += 1
        if self.requests > self.max_requests:
            raise ValueError(
                f"NuGet budget exceeded: more than {self.max_requests} "
                f"requests in one lookup")

    def add_bytes(self, count):
        self.bytes += count
        if self.bytes > self.max_bytes:
            raise ValueError(
                f"NuGet budget exceeded: downloaded more than "
                f"{self.max_bytes} bytes in one lookup "
                f"across {self.requests} requests")

# Registration resource types, best first (3.6.0+ supports SemVer 2.0.0).
_REG_TYPE_RANK = {
    "registrationsbaseurl/3.6.0": 50,
    "registrationsbaseurl/3.4.0": 40,
    "registrationsbaseurl/3.0.0-rc": 30,
    "registrationsbaseurl/3.0.0-beta": 20,
    "registrationsbaseurl": 10,
    "registrationbaseurl/3.6.0": 50,
    "registrationbaseurl/3.4.0": 40,
    "registrationbaseurl/3.0.0-rc": 30,
    "registrationbaseurl/3.0.0-beta": 20,
    "registrationbaseurl": 10,
}


# ---------------------------------------------------------------------------
# Fetch layer (cached per process; injectable/mocked in tests)
# ---------------------------------------------------------------------------

def _http_get_json(url):
    """GET *url* and parse the JSON body, or raise. Handles gzip responses.

    Counts against the per-invocation fetch budget (see _NugetBudget) when
    one is installed, so one lookup cannot issue unbounded requests or
    download unbounded bytes across many individually bounded responses.
    """
    budget = getattr(_FETCH_BUDGET, "budget", None)
    if budget is not None:
        budget.begin_request()
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "EOL-Tracker/1.0",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = read_response_bytes(resp, max_bytes=_NUGET_BODY_BYTES)
        encoding = str(resp.headers.get("Content-Encoding") or "").lower()
    if budget is not None:
        budget.add_bytes(len(raw))
    if encoding == "gzip":
        raw = decompress_gzip_bytes(raw, max_bytes=_NUGET_BODY_BYTES)
    return json.loads(raw.decode("utf-8", "replace"))


def _fetch_service_index():
    """Fetch the NuGet V3 service index. Cached per process."""
    if "index" in _NUGET_CACHE:
        return _NUGET_CACHE["index"]
    doc = _http_get_json(_NUGET_SERVICE_INDEX)
    _NUGET_CACHE["index"] = doc
    return doc


def _fetch_package(package):
    """Fetch + flatten the registration leaves for *package*.

    Returns the list of catalogEntry dicts, or None when NuGet has no such
    package (HTTP 404). Cached per process. Raises on network errors other
    than 404 and on structural drift in the service index or registration.
    """
    key = str(package).strip().lower()
    ck = ("pkg", key)
    if ck in _NUGET_CACHE:
        return _NUGET_CACHE[ck]
    index = _fetch_service_index()
    base = _validate_registration_base(_pick_registration_base(index))
    url = f"{base.rstrip('/')}/{urllib.parse.quote(key, safe='')}/index.json"
    try:
        reg = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _NUGET_CACHE[ck] = None
            return None
        raise
    leaves = _collect_leaves(reg, _http_get_json, base)
    if not leaves:
        raise ValueError(
            "registration index contains no catalog entries; source may have changed")
    _NUGET_CACHE[ck] = leaves
    return leaves


# ---------------------------------------------------------------------------
# Pure helpers (no network — unit-test these directly)
# ---------------------------------------------------------------------------

def _pick_registration_base(index_doc):
    """Pure: pick the best registration base URL from a service-index doc.

    Prefers the highest SemVer-capable RegistrationsBaseUrl resource. Raises
    ValueError on structural drift so the run fails loudly instead of hitting
    a wrong endpoint.
    """
    if not isinstance(index_doc, dict):
        raise ValueError("service index is not a JSON object")
    resources = index_doc.get("resources")
    if not isinstance(resources, list):
        raise ValueError("service index has no resources list")
    best_rank, best_url = -1, None
    for res in resources:
        if not isinstance(res, dict):
            continue
        url = res.get("@id")
        if not isinstance(url, str) or not url:
            continue
        for rtype in str(res.get("@type") or "").split():
            rank = _REG_TYPE_RANK.get(rtype.lower())
            if rank is None and rtype.lower().startswith("registration"):
                rank = 5
            if rank is not None and rank > best_rank:
                best_rank, best_url = rank, url
                break
    if best_url is None:
        raise ValueError("service index exposes no RegistrationsBaseUrl resource")
    return best_url


def _url_origin(parsed):
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError(f"invalid registration URL port: {exc}") from exc
    return (parsed.hostname.lower() if parsed.hostname else None, port)


def _registration_path(url):
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    if any(part == ".." for part in path.split("/")):
        raise ValueError("registration URL contains a parent path segment")
    return path.rstrip("/")


def _validate_registration_base(base):
    """Return a safe registration base anchored to the fixed NuGet origin."""
    candidate = urllib.parse.urlsplit(base)
    service = urllib.parse.urlsplit(_NUGET_SERVICE_INDEX)
    if candidate.scheme.lower() != "https" or candidate.username \
            or candidate.password or candidate.query or candidate.fragment:
        raise ValueError("registration base must be a plain HTTPS URL")
    if _url_origin(candidate) != _url_origin(service):
        raise ValueError("registration base must use the NuGet service origin")
    if not _registration_path(base):
        raise ValueError("registration base has no path")
    return base


def _validate_page_url(page_url, base):
    """Reject paged-registration URLs outside the selected HTTPS base."""
    if not isinstance(page_url, str) or not page_url:
        raise ValueError("registration page has an invalid @id")
    page = urllib.parse.urlsplit(page_url)
    root = urllib.parse.urlsplit(base)
    if page.scheme.lower() != "https" or page.username or page.password \
            or page.fragment:
        raise ValueError("registration page @id must be an HTTPS URL")
    if _url_origin(page) != _url_origin(root):
        raise ValueError("registration page @id escapes the registration origin")
    page_path = _registration_path(page_url)
    root_path = _registration_path(base)
    if page_path != root_path and not page_path.startswith(root_path + "/"):
        raise ValueError("registration page @id escapes the registration base")
    return page_url


def _collect_leaves(reg_doc, fetch_page, expected_base):
    """Pure-ish: walk a registration index and return its catalogEntry dicts.

    Pages carrying inline items are read directly; pages whose ``items`` is
    null (paged registration) are fetched through *fetch_page* so pagination
    is injectable in tests. Leaves lacking a catalogEntry are skipped; raises
    ValueError on structural drift.
    """
    if not isinstance(reg_doc, dict):
        raise ValueError("registration index is not a JSON object")
    pages = reg_doc.get("items")
    if not isinstance(pages, list):
        raise ValueError("registration index has no items list")
    if len(pages) > _NUGET_MAX_PAGES:
        raise ValueError(
            f"registration index exceeds {_NUGET_MAX_PAGES} page limit")
    leaves = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("registration page is not a JSON object")
        items = page.get("items")
        if items is None:
            page_url = page.get("@id")
            if not page_url:
                raise ValueError("registration page has neither inline items nor an @id")
            page_url = _validate_page_url(page_url, expected_base)
            fetched = fetch_page(page_url)
            if not isinstance(fetched, dict):
                raise ValueError(f"registration page {page_url} did not return an object")
            items = fetched.get("items")
        if not isinstance(items, list):
            raise ValueError("registration page has no items list")
        for leaf in items:
            if isinstance(leaf, dict) and isinstance(leaf.get("catalogEntry"), dict):
                if len(leaves) >= _NUGET_MAX_LEAVES:
                    raise ValueError(
                        f"registration index exceeds {_NUGET_MAX_LEAVES} leaf limit")
                leaves.append(leaf["catalogEntry"])
    return leaves


def _normalize_version(version):
    """Pure: normalize a NuGet version the way NuGet itself compares them.

    Lower-cases, strips build metadata, zero-pads the numeric core to three
    parts and drops a trailing ".0" revision (1.2 == 1.2.0 == 1.2.0.0), then
    re-attaches the lower-cased prerelease tag. Unparseable cores fall back
    to the lower-cased literal so exotic versions still compare exactly.
    """
    v = str(version or "").strip().lower()
    if not v:
        return ""
    v = v.split("+", 1)[0]
    core, dash, pre = v.partition("-")
    parts = core.split(".")
    if len(parts) <= 4 and all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        while len(nums) < 3:
            nums.append(0)
        while len(nums) > 3 and nums[-1] == 0:
            nums.pop()
        core = ".".join(str(n) for n in nums)
    return f"{core}-{pre}" if dash and pre else core


def _is_prerelease(version):
    """Pure: True when *version* carries a prerelease tag (conservative
    SemVer: a ``-`` suffix after stripping build metadata)."""
    v = str(version or "").strip().split("+", 1)[0]
    _, dash, pre = v.partition("-")
    return bool(dash and pre)


def _semver_key(version):
    """Pure: SemVer 2.0 sort key, or None when the version is unorderable.

    Orders per SemVer: numeric core, then stable > prerelease, with prerelease
    identifiers ordered numeric-before-alphanumeric and a longer identifier
    list sorting after its prefix (1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-beta).
    """
    v = str(version or "").strip().lower()
    if not v:
        return None
    v = v.split("+", 1)[0]
    core, dash, pre = v.partition("-")
    parts = core.split(".")
    if not (1 <= len(parts) <= 4) or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    while len(nums) < 4:
        nums.append(0)
    if not (dash and pre):
        return (tuple(nums), 1, ())
    ids = []
    for ident in pre.split("."):
        if ident.isdigit():
            ids.append((0, int(ident), ""))
        elif ident:
            ids.append((1, 0, ident))
        else:
            return None
    return (tuple(nums), 0, tuple(ids))


def _parse_published(ts):
    """Pure: ISO-8601 timestamp -> date, or None.

    Returns None for absent/unparseable values and for 1900-01-01, NuGet's
    marker for "publish date unknown".
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        d = datetime.strptime(ts[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return None if d == _NUGET_UNKNOWN_DATE else d


def _latest_stable(leaves):
    """Pure: (version, published_date|None) of the highest listed stable
    release. Prereleases and unlisted versions never win."""
    best_key, best_ver, best_date = None, None, None
    for ce in leaves:
        if not isinstance(ce, dict) or ce.get("listed") is False:
            continue
        v = ce.get("version")
        key = _semver_key(v)
        if key is None or _is_prerelease(v):
            continue
        if best_key is None or key > best_key:
            best_key, best_ver, best_date = key, str(v), _parse_published(ce.get("published"))
    return best_ver, best_date


def _find_leaf(leaves, version):
    """Pure: the catalogEntry whose version matches *version* case- and
    normalization-insensitively, or None."""
    target = _normalize_version(version)
    if not target:
        return None
    for ce in leaves:
        if _normalize_version(ce.get("version")) == target:
            return ce
    return None


def _deprecation_detail(dep):
    """Pure: human-readable summary of a NuGet deprecation object."""
    bits = []
    reasons = dep.get("reasons")
    if isinstance(reasons, list) and reasons:
        bits.append("/".join(str(r) for r in reasons))
    msg = dep.get("message")
    if isinstance(msg, str) and msg.strip():
        bits.append(msg.strip())
    alt = dep.get("alternatePackage")
    if isinstance(alt, dict) and alt.get("id"):
        bits.append(f"use {alt['id']}")
    return ", ".join(bits)


def _major_of(version):
    """Pure: the numeric major component of *version*, or None."""
    m = re.match(r"\s*(\d+)", str(version or ""))
    return int(m.group(1)) if m else None


def _months_between(d1, d2):
    """Pure: whole months from d1 to d2 (d2 >= d1 assumed for positive result)."""
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)


def _entry_version(entry):
    """Pure: the entry's pinned version as a (possibly empty) string."""
    v = entry.get("version")
    return "" if v is None else str(v).strip()


def _nuget_error(entry, package, message):
    """Uniform error result with NuGet-specific label/product/source set."""
    result = _error_result(entry, message)
    result["source"] = SOURCE
    result["product"] = package
    label = entry.get("label")
    if not label:
        version = _entry_version(entry)
        label = f"{package} {version}".strip()
    result["label"] = label
    return result


def _nuget_result_from_leaves(entry, leaves, today):
    """Pure: build the normalized result from registration leaves (or None).

    Deprecated or unlisted pinned versions alert (status "eol" bucket) — but
    no EOL date is ever claimed, because NuGet exposes no lifecycle dates.
    Release age is informational only.
    """
    package = entry.get("package") or entry.get("product") or ""
    version = _entry_version(entry)
    label = entry.get("label") or f"{package} {version}".strip()

    if leaves is None:
        return _nuget_error(entry, package, f"Package '{package}' not found on NuGet")

    latest, latest_date = _latest_stable(leaves)
    latest_note = f"latest stable is {latest}"
    if latest_date:
        latest_note += f" ({latest_date})"

    pinned = _find_leaf(leaves, version) if version else None
    if version and pinned is None:
        return _nuget_error(
            entry, package,
            f"Version '{version}' not found on NuGet for '{package}'; {latest_note}")

    pinned_date = _parse_published(pinned.get("published")) if pinned else None
    on_latest = bool(
        version and latest
        and _normalize_version(version) == _normalize_version(latest))

    result = {
        "label": label,
        "product": package,
        "version": version,
        "lts": False,
        "in_use_release_date": str(pinned_date) if pinned_date else None,
        "latest_patch": latest,
        "latest_patch_date": str(latest_date) if latest_date else None,
        "latest_cycle": None,
        "latest_cycle_version": None,
        "latest_cycle_release_date": None,
        "on_latest_cycle": on_latest,
        "eol_date": None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": SOURCE,
    }

    alerts = []
    dep = pinned.get("deprecation") if pinned else None
    if isinstance(dep, dict) and dep:
        detail = _deprecation_detail(dep)
        alerts.append("deprecated" + (f" ({detail})" if detail else ""))
    if pinned is not None and pinned.get("listed") is False:
        alerts.append("unlisted (hidden from nuget.org search)")
    if alerts:
        result["status"] = "eol"
        result["message"] = f"NuGet flags {version}: " + "; ".join(alerts)
        return result

    result["status"] = "ok"
    prerelease = bool(version) and _is_prerelease(version)
    if not latest:
        if prerelease:
            result["message"] = f"Using prerelease {version}; no stable listed release on NuGet"
        else:
            result["message"] = "No stable listed release found on NuGet"
        return result
    if not version:
        result["message"] = f"Pinned version not provided; {latest_note}"
        return result
    pd = f" published {pinned_date}" if pinned_date else ""
    ld = f" ({latest_date})" if latest_date else ""
    if prerelease:
        result["message"] = f"Using prerelease {version}{pd}; latest stable is {latest}{ld}"
        return result
    if on_latest:
        months = _months_between(latest_date, today) if latest_date else 0
        if latest_date and months >= _NUGET_STALE_MONTHS:
            yrs = months / 12.0
            result["message"] = (
                f"On latest stable ({latest}) but it's from {latest_date} "
                f"(~{yrs:.1f}y) - likely unmaintained")
        else:
            result["message"] = f"On latest stable NuGet release ({latest}{ld})"
        return result
    mu, ml = _major_of(version), _major_of(latest)
    majors = (ml - mu) if (mu is not None and ml is not None and ml > mu) else 0
    behind = f"; {majors} major(s) behind" if majors else ""
    result["message"] = f"In use {version}{pd}; latest stable {latest}{ld}{behind}"
    return result


def _provider_nuget_registry(entry, today):
    """Report NuGet-registry recency, deprecation, and listing state."""
    package = entry.get("package") or entry.get("product")
    if not package:
        result = _error_result(entry, "nuget_registry entries require 'package'")
        result["source"] = SOURCE
        return result
    budget = _NugetBudget()
    _FETCH_BUDGET.budget = budget
    try:
        leaves = _fetch_package(package)
    except Exception as exc:
        logger.error("NuGet registry fetch failed for %s: %s", package, exc)
        return _nuget_error(entry, package, f"NuGet registry query failed: {exc}")
    finally:
        _FETCH_BUDGET.budget = None
    return _nuget_result_from_leaves(entry, leaves, today)


SOURCE = "nuget_registry"
LABEL = "NuGet"
provider = _provider_nuget_registry


def url_for(r):
    product = r.get("product") or ""
    return f"https://www.nuget.org/packages/{urllib.parse.quote(product, safe='')}" if product else None
