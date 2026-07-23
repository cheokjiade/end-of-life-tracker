"""Maven Central staleness provider.

Most Java libraries don't publish lifecycle dates (Apache Commons, jsoup,
Netty, Quartz, Logback, etc.). For these we report what we *can* know
from the registry: when the in-use version was released, what the latest
is, and when that was released.

Status is always 'ok' — no EOL is being claimed, this is informational.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..core import _error_result, logger

_MAVEN_CENTRAL_API = "https://search.maven.org/solrsearch/select"
# Two cache namespaces: one for "the latest gav of this artifact" and one
# for "this specific gav". Targeted queries avoid the rows-cutoff problem
# that bites artifacts with hundreds of releases (e.g. Netty).
_MAVEN_LATEST_CACHE = {}    # (group, artifact) -> {"v", "released"}|None
_MAVEN_VERSION_CACHE = {}   # (group, artifact, version) -> {"v", "released"}|None


def _fetch_maven_doc(query):
    """Run a Maven Central solr query and return the first doc, or None."""
    url = f"{_MAVEN_CENTRAL_API}?q={query}&core=gav&rows=1&wt=json"
    req = urllib.request.Request(url, headers={
        "Accept":     "application/json",
        "User-Agent": "EOL-Tracker/1.0",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    docs = data.get("response", {}).get("docs", [])
    if not docs:
        return None
    d = docs[0]
    ts = d.get("timestamp")
    return {
        "v":        d.get("v"),
        "released": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date() if ts else None,
    }


def _fetch_maven_latest(group, artifact):
    """Return the most recent gav for an artifact (any major), or None."""
    key = (group, artifact)
    if key in _MAVEN_LATEST_CACHE:
        return _MAVEN_LATEST_CACHE[key]
    q = f"g:{urllib.parse.quote(group)}+AND+a:{urllib.parse.quote(artifact)}"
    info = _fetch_maven_doc(q)
    _MAVEN_LATEST_CACHE[key] = info
    return info


def _fetch_maven_specific(group, artifact, version):
    """Return the gav doc for a specific version, or None if not on Central."""
    key = (group, artifact, version)
    if key in _MAVEN_VERSION_CACHE:
        return _MAVEN_VERSION_CACHE[key]
    q = (
        f"g:{urllib.parse.quote(group)}+AND+"
        f"a:{urllib.parse.quote(artifact)}+AND+"
        f"v:{urllib.parse.quote(version)}"
    )
    info = _fetch_maven_doc(q)
    _MAVEN_VERSION_CACHE[key] = info
    return info


def _provider_maven_central(entry, today):
    """Report release staleness for a Maven artifact. No EOL data implied."""
    group = entry.get("group")
    artifact = entry.get("artifact")
    version = str(entry.get("version", ""))
    label = entry.get("label", f"{group}:{artifact}:{version}")

    if not (group and artifact and version):
        result = _error_result(entry, "Maven Central entries require 'group', 'artifact', and 'version'")
        result["source"] = "maven_central"
        return result

    try:
        latest = _fetch_maven_latest(group, artifact)
        in_use = _fetch_maven_specific(group, artifact, version)
    except Exception as exc:
        logger.error("Maven Central fetch failed for %s:%s: %s", group, artifact, exc)
        result = _error_result(entry, f"Maven Central query failed: {exc}")
        result["source"] = "maven_central"
        return result

    if not latest:
        result = _error_result(entry, f"Artifact {group}:{artifact} not found on Maven Central")
        result["source"] = "maven_central"
        return result

    latest_v = latest["v"]
    latest_date = latest["released"]
    in_use_date = in_use["released"] if in_use else None
    on_latest = latest_v == version

    if on_latest:
        message = f"On latest Maven Central release ({latest_v})"
    elif in_use_date and latest_date:
        days_newer = (latest_date - in_use_date).days
        message = (
            f"In use: {version} ({in_use_date}); latest: {latest_v} "
            f"({latest_date}, {days_newer} days newer)"
        )
    elif in_use_date is None:
        message = (
            f"Version {version} not on Maven Central (private build?); "
            f"latest published is {latest_v} ({latest_date})"
        )
    else:
        message = f"In use: {version}; latest: {latest_v}"

    return {
        "label": label,
        "product": f"{group}:{artifact}",
        "version": version,
        "lts": False,
        "status": "ok",
        "message": message,
        "in_use_release_date": str(in_use_date) if in_use_date else None,
        "latest_patch": latest_v,
        "latest_patch_date": str(latest_date) if latest_date else None,
        "latest_cycle": None,
        "latest_cycle_version": None,
        "latest_cycle_release_date": None,
        "on_latest_cycle": on_latest,
        "eol_date": None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "maven_central",
    }


SOURCE = "maven_central"
LABEL = "Maven Central"
provider = _provider_maven_central


def url_for(r):
    product = r.get("product") or ""
    if ":" in product:
        group, artifact = product.split(":", 1)
        return f"https://central.sonatype.com/artifact/{group}/{artifact}"
    return None
