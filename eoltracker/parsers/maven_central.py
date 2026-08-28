"""Maven Central staleness provider.

Most Java libraries don't publish lifecycle dates (Apache Commons, jsoup,
Netty, Quartz, Logback, etc.). For these we report what we *can* know
from the registry: when the in-use version was released, what the latest
is, and when that was released.

Status is 'ok' only when the in-use version could be positively located on
Central (or it is the resolved 'latest'); an in-use version Central has no
record of is data-quality 'unknown', not healthy.
"""

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from ..core import _error_result, logger

_MAVEN_REPOSITORY = "https://repo1.maven.org/maven2"
_HTTP_TIMEOUT_SECONDS = 10
_MAX_METADATA_BYTES = 1024 * 1024
# Two cache namespaces: one for "the latest gav of this artifact" and one
# for "this specific gav". Canonical metadata has no search-result row cutoff,
# including for artifacts with hundreds of releases (e.g. Netty).
_MAVEN_LATEST_CACHE = {}    # (group, artifact) -> {"v", "released"}|None
_MAVEN_VERSION_CACHE = {}   # (group, artifact, version) -> {"v", "released"}|None


def _artifact_base_url(group, artifact):
    """Canonical, path-quoted repository URL for one Maven artifact."""
    group_path = "/".join(
        urllib.parse.quote(part, safe="") for part in group.split("."))
    artifact_path = urllib.parse.quote(artifact, safe="")
    return f"{_MAVEN_REPOSITORY}/{group_path}/{artifact_path}"


def _parse_metadata_release(raw):
    """Extract the current release/latest version from maven-metadata.xml."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("invalid Maven metadata XML") from exc
    versioning = root.find("versioning")
    if versioning is None:
        return None
    for tag in ("release", "latest"):
        value = versioning.findtext(tag)
        if value and value.strip():
            return value.strip()
    versions = versioning.find("versions")
    if versions is None:
        return None
    values = [
        node.text.strip() for node in versions.findall("version")
        if node.text and node.text.strip()
    ]
    return values[-1] if values else None


def _fetch_metadata_release(group, artifact):
    """Fetch canonical repository metadata; return None on HTTP 404."""
    url = f"{_artifact_base_url(group, artifact)}/maven-metadata.xml"
    req = urllib.request.Request(url, headers={
        "Accept": "application/xml",
        "User-Agent": "EOL-Tracker/1.0",
    })
    try:
        with urllib.request.urlopen(
                req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read(_MAX_METADATA_BYTES + 1)
            if len(raw) > _MAX_METADATA_BYTES:
                raise ValueError("Maven metadata response exceeds 1 MiB")
            return _parse_metadata_release(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _pom_info(group, artifact, version):
    """Confirm a version POM and read its repository modification date."""
    encoded_version = urllib.parse.quote(version, safe="")
    pom_name = urllib.parse.quote(f"{artifact}-{version}.pom", safe="")
    url = f"{_artifact_base_url(group, artifact)}/{encoded_version}/{pom_name}"
    req = urllib.request.Request(url, method="HEAD", headers={
        "Accept": "application/xml",
        "User-Agent": "EOL-Tracker/1.0",
    })
    try:
        with urllib.request.urlopen(
                req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            modified = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    released = None
    if modified:
        try:
            released = parsedate_to_datetime(modified).date()
        except (TypeError, ValueError, OverflowError):
            pass
    return {"v": version, "released": released}


def _fetch_maven_latest(group, artifact):
    """Return the most recent gav for an artifact (any major), or None."""
    key = (group, artifact)
    if key in _MAVEN_LATEST_CACHE:
        return _MAVEN_LATEST_CACHE[key]
    version = _fetch_metadata_release(group, artifact)
    info = None
    if version:
        info = _pom_info(group, artifact, version)
        if info is None:
            # Metadata is authoritative for the latest version even if a CDN
            # edge has not made the POM visible yet.
            info = {"v": version, "released": None}
    _MAVEN_LATEST_CACHE[key] = info
    return info


def _fetch_maven_specific(group, artifact, version):
    """Return the gav doc for a specific version, or None if not on Central."""
    key = (group, artifact, version)
    if key in _MAVEN_VERSION_CACHE:
        return _MAVEN_VERSION_CACHE[key]
    info = _pom_info(group, artifact, version)
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
        summary = type(exc).__name__
        logger.error("Maven Central fetch failed (%s)", summary)
        result = _error_result(
            entry, f"Maven Central repository query failed ({summary})")
        result["source"] = "maven_central"
        return result

    if not latest:
        result = _error_result(entry, f"Artifact {group}:{artifact} not found on Maven Central")
        result["source"] = "maven_central"
        return result

    latest_v = latest["v"]
    latest_date = latest["released"]
    latest_date_text = str(latest_date) if latest_date else "date unknown"
    in_use_date = in_use["released"] if in_use else None
    on_latest = latest_v == version

    # 'latest' is resolved independently of the in-use gav query, so the
    # in-use version may be absent from Central (private build, typo, or an
    # indexing gap). That is unverifiable data quality -> unknown, not OK.
    status = "ok" if (in_use is not None or on_latest) else "unknown"

    if on_latest:
        message = f"On latest Maven Central release ({latest_v})"
    elif in_use_date and latest_date:
        days_newer = (latest_date - in_use_date).days
        message = (
            f"In use: {version} ({in_use_date}); latest: {latest_v} "
            f"({latest_date}, {days_newer} days newer)"
        )
    elif in_use is None:
        message = (
            f"Version {version} not on Maven Central (private build?); "
            f"latest published is {latest_v} ({latest_date_text})"
        )
    elif in_use_date is None:
        message = (
            f"In use: {version} (release date unknown); "
            f"latest: {latest_v} ({latest_date_text})"
        )
    else:
        message = f"In use: {version}; latest: {latest_v}"

    return {
        "label": label,
        "product": f"{group}:{artifact}",
        "version": version,
        "lts": False,
        "status": status,
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
