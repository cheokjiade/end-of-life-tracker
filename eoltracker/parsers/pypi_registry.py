"""PyPI registry staleness provider.

Python libraries on PyPI don't publish EOL dates. Like npm_registry, we
report what the registry knows: when the in-use release was uploaded, the
latest *stable* release and its upload date, and per-release yanked
status/reason. Release age is informational - no EOL is claimed. A yanked
pinned release is an alert (PyPI withdraws it from default installs), and
an explicitly requested version absent from the registry is an error.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from ..core import _error_result, logger

_PYPI_JSON_API = "https://pypi.org/pypi"
_PYPI_CACHE = {}          # normalized package name -> doc | None (404)

# PEP 440-lite: enough grammar to order PyPI versions and spot pre-releases.
# Versions outside this grammar (local versions, exotic tags) yield None from
# _version_key and are handled conservatively - never claimed as stable.
_PEP440_RE = re.compile(
    r"^\s*v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)"
    r"(?P<pre>[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-.]?(?P<pre_n>\d+)?)?"
    r"(?P<post>(?:-(?P<post_n1>\d+))|(?:[-_.]?(?:post|rev|r)[-_.]?(?P<post_n2>\d+)?))?"
    r"(?P<dev>[-_.]?dev[-_.]?(?P<dev_n>\d+)?)?"
    r"(?:\+(?P<local>[a-zA-Z0-9]+(?:[-_.][a-zA-Z0-9]+)*))?\s*$",
    re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2,
             "pre": 2, "preview": 2}


def _normalize_package(name):
    """PEP 503 normalization - PyPI treats case and -/_/. as equivalent."""
    return re.sub(r"[-_.]+", "-", str(name).strip()).lower()


def _version_key(version):
    """PEP 440-lite total-order sort key, or None when not orderable."""
    s = str(version).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    m = _PEP440_RE.match(s)
    if not m or m.group("local"):
        return None
    release = tuple(int(p) for p in m.group("release").split("."))
    release = release + (0,) * max(0, 8 - len(release))
    if m.group("pre_l"):
        pre = (1, _PRE_RANK[m.group("pre_l").lower()], int(m.group("pre_n") or 0))
    elif m.group("dev") is not None and m.group("post") is None:
        pre = (0, 0, 0)           # 1.0.dev1 sorts before 1.0a1
    else:
        pre = (2, 0, 0)           # a final release sorts after any pre-release
    post = ((1, int(m.group("post_n1") or m.group("post_n2") or 0))
            if m.group("post") is not None else (0, 0))
    dev = ((0, int(m.group("dev_n") or 0))
           if m.group("dev") is not None else (1, 0))
    return (int(m.group("epoch") or 0), release, pre, post, dev)


def _is_prerelease(version):
    """True/False per PEP 440-lite; None when the version can't be classified."""
    key = _version_key(version)
    if key is None:
        return None
    return key[2][0] == 1 or key[4][0] == 0


def _validate_pypi_doc(doc):
    """Structural drift check: fail loudly rather than parse a foreign shape."""
    if not isinstance(doc, dict):
        raise ValueError("PyPI document is not a JSON object")
    info = doc.get("info")
    if not isinstance(info, dict):
        raise ValueError("PyPI document has no 'info' object")
    if not isinstance(info.get("version"), str):
        raise ValueError("PyPI 'info.version' is not a string")
    releases = doc.get("releases")
    if not isinstance(releases, dict) or not releases:
        raise ValueError("PyPI document has no 'releases' mapping")


def _fetch_pypi_doc(package):
    """Fetch the PyPI JSON document for *package*, or None on 404. Cached."""
    norm = _normalize_package(package)
    if norm in _PYPI_CACHE:
        return _PYPI_CACHE[norm]
    url = f"{_PYPI_JSON_API}/{urllib.parse.quote(norm, safe='')}/json"
    req = urllib.request.Request(url, headers={
        "Accept":     "application/json",
        "User-Agent": "EOL-Tracker/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _PYPI_CACHE[norm] = None
            return None
        raise
    _validate_pypi_doc(doc)
    _PYPI_CACHE[norm] = doc
    return doc


def _parse_upload_date(ts):
    """Tolerant 'YYYY-MM-DDTHH:MM:SS' -> date; None when absent/unparseable."""
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    try:
        return datetime.strptime(ts[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _release_date(files):
    """Earliest upload date across a release's files (when the release shipped)."""
    dates = [d for f in files if isinstance(f, dict)
             for d in [_parse_upload_date(f.get("upload_time"))] if d]
    return min(dates) if dates else None


def _release_yanked(files):
    """(yanked, reason) for a release. pip semantics: yanked only when *every*
    file is yanked; the reason is the first non-empty one given."""
    real = [f for f in files if isinstance(f, dict)]
    if not real or not all(bool(f.get("yanked")) for f in real):
        return False, None
    reason = next((str(f.get("yanked_reason")).strip() for f in real
                   if f.get("yanked_reason") and str(f.get("yanked_reason")).strip()),
                  None)
    return True, reason


def _latest_stable(releases):
    """Pure: pick the latest stable, non-yanked release from a 'releases'
    mapping. Returns (version, date) or (None, None) when nothing qualifies.
    Conservative: pre-releases, unparsable or local versions, fully yanked
    releases, and file-less entries never qualify as stable."""
    best_key, best_v, best_date = None, None, None
    for v in sorted(releases):
        key = _version_key(v)
        if key is None or _is_prerelease(v):
            continue
        files = releases[v]
        if not isinstance(files, list) or not files:
            continue
        yanked, _ = _release_yanked(files)
        if yanked:
            continue
        if best_key is None or key > best_key:
            best_key, best_v, best_date = key, v, _release_date(files)
    return best_v, best_date


def _find_release_files(releases, version):
    """Release-key lookup with a conservative canonical-form fallback (e.g.
    'V2.0' vs '2.0.0'); only a unique canonical match is accepted."""
    if version in releases:
        return version
    key = _version_key(version)
    if key is None:
        return None
    matches = [v for v in releases if _version_key(v) == key]
    return matches[0] if len(matches) == 1 else None


def _error(entry, package, message):
    """Error-shaped result that keeps the source link resolvable."""
    result = _error_result(entry, message)
    result["source"] = "pypi_registry"
    result["product"] = package
    return result


def _pypi_result_from_doc(entry, doc, today):
    """Pure: build a normalized result dict from a fetched PyPI JSON doc."""
    package = entry.get("package", "")
    version = str(entry["version"]) if entry.get("version") is not None else ""
    label = entry.get("label") or (f"{package} {version}".strip())

    if doc is None:
        return _error(entry, package, f"Package '{package}' not found on PyPI")

    try:
        _validate_pypi_doc(doc)
    except ValueError as exc:
        return _error(entry, package, f"PyPI document malformed: {exc}")

    releases = doc["releases"]
    latest_v, latest_date = _latest_stable(releases)
    latest_bits = f"latest stable is {latest_v}" + (f" ({latest_date})" if latest_date else "")

    if not version:
        result = _base_result(entry, package, version, label, latest_v, latest_date)
        result["status"] = "ok"
        result["message"] = ("In-use version not provided; " + latest_bits
                             if latest_v else
                             "In-use version not provided; PyPI lists no stable release")
        return result

    key_version = _find_release_files(releases, version)
    if key_version is None:
        return _error(entry, package,
                      f"Version '{version}' not present on PyPI"
                      + (f"; {latest_bits}" if latest_v else ""))

    files = releases[key_version]
    if not isinstance(files, list) or not files:
        return _error(entry, package,
                      f"Version '{version}' has no uploaded files on PyPI")
    if not all(isinstance(f, dict) for f in files):
        return _error(entry, package,
                      f"PyPI release data for '{version}' is malformed")

    yanked, reason = _release_yanked(files)
    in_use_date = _release_date(files)
    result = _base_result(entry, package, version, label, latest_v, latest_date)
    result["in_use_release_date"] = str(in_use_date) if in_use_date else None

    if yanked:
        result["status"] = "eol"
        result["message"] = (f"PyPI marks {version} yanked: {reason}" if reason
                             else f"PyPI marks {version} yanked (no reason given)")
        return result

    result["status"] = "ok"
    result["on_latest_cycle"] = latest_v is not None and key_version == latest_v
    is_pre = _is_prerelease(version)
    note = ("; note: prerelease version" if is_pre
            else "; note: unrecognized version format" if is_pre is None else "")

    if latest_v is None:
        result["message"] = (
            f"In use {version}" + (f" ({in_use_date})" if in_use_date else "")
            + "; PyPI lists no stable release" + note)
    elif result["on_latest_cycle"]:
        result["message"] = f"On latest stable PyPI release ({latest_v})" + note
    else:
        bits = []
        if in_use_date and latest_date and latest_date > in_use_date:
            bits.append(f"{(latest_date - in_use_date).days}d newer")
        tail = f" ({', '.join(bits)})" if bits else ""
        result["message"] = (
            f"In use {version}" + (f" ({in_use_date})" if in_use_date else "")
            + f"; {latest_bits}" + tail + note)
    return result


def _base_result(entry, package, version, label, latest_v, latest_date):
    """The full normalized key set shared by every non-error outcome."""
    return {
        "label": label,
        "product": package,
        "version": version,
        "lts": False,
        "in_use_release_date": None,
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
        "source": "pypi_registry",
    }


def _provider_pypi_registry(entry, today):
    """Report PyPI-registry staleness (and yanked status) for a package."""
    package = entry.get("package")
    if not package:
        result = _error_result(entry, "pypi_registry entries require 'package'")
        result["source"] = "pypi_registry"
        return result
    try:
        doc = _fetch_pypi_doc(package)
    except Exception as exc:
        logger.error("PyPI registry fetch failed for %s: %s", package, exc)
        result = _error_result(entry, f"PyPI registry query failed: {exc}")
        result["source"] = "pypi_registry"
        result["product"] = package
        return result
    return _pypi_result_from_doc(entry, doc, today)


SOURCE = "pypi_registry"
LABEL = "PyPI"
provider = _provider_pypi_registry


def url_for(r):
    product = r.get("product") or ""
    return f"https://pypi.org/project/{product}/" if product else None
