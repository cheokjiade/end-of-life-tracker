"""npm registry staleness provider.

npm libraries rarely publish EOL dates. Like maven_central, we report what
the registry knows: when the in-use version shipped, the latest version and
its date, and how far behind. The one hard signal npm gives is per-version
deprecation, which we surface as an alert.
"""

import json
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

from ..core import _error_result, logger

_NPM_REGISTRY_API = "https://registry.npmjs.org"
_NPM_STALE_MONTHS = 24
_NPM_CACHE = {}


def _months_between(d1, d2):
    """Whole months from d1 to d2 (d2 >= d1 assumed for positive result)."""
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)


def _fetch_npm_doc(package):
    """Fetch npm registry metadata for *package*, or None on 404. Cached."""
    if package in _NPM_CACHE:
        return _NPM_CACHE[package]
    enc = urllib.parse.quote(package, safe="@")  # '@mui/material' -> '@mui%2Fmaterial'
    url = f"{_NPM_REGISTRY_API}/{enc}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "EOL-Tracker/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _NPM_CACHE[package] = None
            return None
        raise
    _NPM_CACHE[package] = doc
    return doc


def _npm_result_from_doc(entry, doc, today):
    """Pure: build a normalized result dict from a fetched npm registry doc."""
    package = entry.get("package", "")
    version = str(entry["version"]) if entry.get("version") is not None else ""
    label = entry.get("label") or (f"{package} {version}".strip())

    if doc is None:
        result = _error_result(entry, f"Package '{package}' not found on npm registry")
        result["source"] = "npm_registry"
        result["product"] = package
        return result

    dist_tags = doc.get("dist-tags") or {}
    latest = dist_tags.get("latest")
    times = doc.get("time") or {}
    versions = doc.get("versions") or {}

    def _pdate(v):
        ts = times.get(v)
        if not ts:
            return None
        try:
            return datetime.strptime(ts[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    latest_date = _pdate(latest) if latest else None
    in_use_date = _pdate(version) if version else None
    on_latest = bool(version) and version == latest

    result = {
        "label": label,
        "product": package,
        "version": version,
        "lts": False,
        "in_use_release_date": str(in_use_date) if in_use_date else None,
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
        "source": "npm_registry",
    }

    dep = versions.get(version, {}).get("deprecated") if version else None
    if isinstance(dep, str) and dep.strip():
        result["status"] = "eol"
        result["message"] = f"npm marks {version} deprecated: {dep.strip()}"
        return result

    result["status"] = "ok"
    if not latest:
        result["message"] = "No versions published on npm registry"
    elif not version:
        result["message"] = f"In-use version not provided; latest is {latest}" + (f" ({latest_date})" if latest_date else "")
    elif version not in versions:
        result["message"] = f"Version {version} not on npm registry (private build?); latest is {latest}" + (f" ({latest_date})" if latest_date else "")
    elif on_latest:
        if latest_date and _months_between(latest_date, today) >= _NPM_STALE_MONTHS:
            yrs = _months_between(latest_date, today) / 12.0
            result["message"] = f"On latest ({latest}) but it's from {latest_date} (~{yrs:.1f}y) - likely unmaintained"
        else:
            result["message"] = f"On latest npm release ({latest})"
    else:
        def _maj(v):
            m = re.match(r"\s*(\d+)", v or "")
            return int(m.group(1)) if m else None
        mu, ml = _maj(version), _maj(latest)
        majors = (ml - mu) if (mu is not None and ml is not None and ml > mu) else 0
        bits = []
        if majors:
            bits.append(f"{majors} major(s) behind")
        if in_use_date and latest_date:
            bits.append(f"{(latest_date - in_use_date).days}d newer")
        tail = f" ({', '.join(bits)})" if bits else ""
        result["message"] = (
            f"In use {version}" + (f" ({in_use_date})" if in_use_date else "")
            + f"; latest {latest}" + (f" ({latest_date})" if latest_date else "") + tail
        )
    return result


def _provider_npm_registry(entry, today):
    """Report npm-registry staleness (and deprecation) for a package."""
    package = entry.get("package")
    if not package:
        result = _error_result(entry, "npm_registry entries require 'package'")
        result["source"] = "npm_registry"
        return result
    try:
        doc = _fetch_npm_doc(package)
    except Exception as exc:
        logger.error("npm registry fetch failed for %s: %s", package, exc)
        result = _error_result(entry, f"npm registry query failed: {exc}")
        result["source"] = "npm_registry"
        result["product"] = package
        return result
    return _npm_result_from_doc(entry, doc, today)


SOURCE = "npm_registry"
LABEL = "npm"
provider = _provider_npm_registry


def url_for(r):
    product = r.get("product") or ""
    return f"https://www.npmjs.com/package/{product}" if product else None
