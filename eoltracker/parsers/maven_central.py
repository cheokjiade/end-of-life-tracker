"""Maven Central staleness provider.

Most Java libraries don't publish lifecycle dates (Apache Commons, jsoup,
Netty, Quartz, Logback, etc.). For these we report what we *can* know
from the registry: when the in-use version was released, what the latest
is, and when that was released.

The reported latest is the metadata's release/latest tag only when that tag
is a stable version which actually appears in the <versions> list. Some
projects leave stale pre-release tags behind (io.netty:netty-codec-http
advertised 5.0.0.Alpha2 while its newest listed version was 4.2.17.Final),
which would otherwise produce absurd "older than the release" rows. When the
tag is untrustworthy, the highest stable version in <versions> wins
(numeric-aware ordering); if every listed version is a pre-release, the
highest overall is used; with no <versions> list the tag is trusted as-is.

Status is 'ok' only when the in-use version could be positively located on
Central (or it is the resolved 'latest'); an in-use version Central has no
record of is data-quality 'unknown', not healthy.

Entries may set an optional 'repository' key: the absolute http(s) base URL
of any Maven 2 repository layout exposing maven-metadata.xml and POM
Last-Modified headers (e.g. the Shibboleth repository for OpenSAML
artifacts, which are not published to Maven Central). The default is Maven
Central.
"""

import re
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
# including for artifacts with hundreds of releases (e.g. Netty). Both keys
# lead with the repository so overrides never collide with Central entries.
_MAVEN_LATEST_CACHE = {}    # (repository, group, artifact) -> {"v", "released"}|None
_MAVEN_VERSION_CACHE = {}   # (repository, group, artifact, version) -> {"v", "released"}|None


def _normalize_repository(value):
    """Canonical base URL for an optional custom Maven repository override.

    Returns None when *value* is absent or blank; otherwise strips
    surrounding whitespace, lowercases the scheme and host (the path stays
    case-sensitive), and strips exactly one trailing '/'. Raises ValueError
    for anything that is not an absolute http(s) URL, that carries
    credentials, or that includes a query string, fragment, or malformed
    port. Pure: no network.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("repository must be an absolute http(s) URL")
    text = value.strip()
    if not text:
        return None
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("repository must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ValueError("repository must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("repository must not include a query or fragment")
    try:
        _ = parts.port
    except ValueError:
        raise ValueError("repository has an invalid port") from None
    # Canonical scheme/host case keeps cache keys stable across variants and
    # makes the Maven Central detection below case-insensitive.
    parts = parts._replace(
        scheme=parts.scheme.lower(), netloc=parts.netloc.lower())
    text = urllib.parse.urlunsplit(parts)
    if text.endswith("/"):
        text = text[:-1]
    return text


def _artifact_base_url(group, artifact, repository=_MAVEN_REPOSITORY):
    """Canonical, path-quoted repository URL for one Maven artifact."""
    group_path = "/".join(
        urllib.parse.quote(part, safe="") for part in group.split("."))
    artifact_path = urllib.parse.quote(artifact, safe="")
    return f"{repository}/{group_path}/{artifact_path}"


# A pre-release qualifier segment (case-insensitive): alpha/beta/rc/cr/
# snapshot/preview/ea as a prefix or exact match, m/milestone (and the b
# shorthand) only when followed by a number — e.g. 5.0.0.Alpha2, 1.0.0-b1,
# 2.0.0-RC1, 9.9.M3. Final, GA, RELEASE, sp<N> and plain numerics are stable.
_PRERELEASE_SEGMENT_RE = re.compile(
    r"^(?:alpha|beta|rc|cr|snapshot|preview|ea\d*|b\d+|m\d+|milestone\d+)")


def _is_prerelease_version(version):
    """True when any dot/dash-separated segment is a pre-release qualifier."""
    if not version:
        return False
    return any(
        _PRERELEASE_SEGMENT_RE.match(segment.lower())
        for segment in re.split(r"[.\-]", str(version)) if segment)


def _version_order_key(version):
    """Numeric-aware ordering key for Maven version strings.

    Versions are split on '.' and '-': leading numeric segments compare
    numerically (1.0.10 > 1.0.9), then a stable version beats a pre-release
    at the same numeric core, then letter/digit chunks of the remaining
    segments break ties deterministically (Alpha2 > Alpha1, Alpha10 > Alpha2).
    Pure; never raises on odd input.
    """
    segments = [s for s in re.split(r"[.\-]", str(version).strip()) if s]
    core = []
    while segments and segments[0].isdigit():
        core.append((0, int(segments.pop(0)), ""))
    tail = []
    for segment in segments:
        for chunk in re.findall(r"\d+|\D+", segment.lower()):
            if chunk.isdigit():
                tail.append((0, int(chunk), ""))
            else:
                tail.append((1, 0, chunk))
    return (core, 0 if _is_prerelease_version(version) else 1, tail)


def _pick_latest(tag, versions):
    """Choose the reported latest version from parsed maven-metadata.xml.

    *tag* is the metadata's release/latest value (or None) and *versions*
    the listed <versions> strings. A tag is trusted only when it is a stable
    version present in *versions*; otherwise the highest stable listed
    version wins, falling back to the highest listed version overall when
    none is stable. With no <versions> list (tiny/private metadata) the tag
    is trusted as-is. Pure; returns a version string or None.
    """
    if not versions:
        return tag
    if tag and tag in versions and not _is_prerelease_version(tag):
        return tag
    pool = [v for v in versions if not _is_prerelease_version(v)]
    return max(pool or versions, key=_version_order_key)


def _parse_versioning(raw):
    """Parse maven-metadata.xml into (release-or-latest tag, versions list)."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("invalid Maven metadata XML") from exc
    versioning = root.find("versioning")
    if versioning is None:
        return None, []
    tag = None
    for name in ("release", "latest"):
        value = versioning.findtext(name)
        if value and value.strip():
            tag = value.strip()
            break
    versions = versioning.find("versions")
    values = []
    if versions is not None:
        values = [
            node.text.strip() for node in versions.findall("version")
            if node.text and node.text.strip()
        ]
    return tag, values


def _parse_metadata_release(raw):
    """Extract the current release/latest version from maven-metadata.xml."""
    return _pick_latest(*_parse_versioning(raw))


def _fetch_metadata_release(group, artifact, repository=_MAVEN_REPOSITORY):
    """Fetch canonical repository metadata; return None on HTTP 404."""
    url = f"{_artifact_base_url(group, artifact, repository)}/maven-metadata.xml"
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


def _pom_info(group, artifact, version, repository=_MAVEN_REPOSITORY):
    """Confirm a version POM and read its repository modification date."""
    encoded_version = urllib.parse.quote(version, safe="")
    pom_name = urllib.parse.quote(f"{artifact}-{version}.pom", safe="")
    url = f"{_artifact_base_url(group, artifact, repository)}/{encoded_version}/{pom_name}"
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


def _fetch_maven_latest(group, artifact, repository=_MAVEN_REPOSITORY):
    """Return the most recent gav for an artifact (any major), or None."""
    key = (repository, group, artifact)
    if key in _MAVEN_LATEST_CACHE:
        return _MAVEN_LATEST_CACHE[key]
    version = _fetch_metadata_release(group, artifact, repository)
    info = None
    if version:
        info = _pom_info(group, artifact, version, repository)
        if info is None:
            # Metadata is authoritative for the latest version even if a CDN
            # edge has not made the POM visible yet.
            info = {"v": version, "released": None}
    _MAVEN_LATEST_CACHE[key] = info
    return info


def _fetch_maven_specific(group, artifact, version, repository=_MAVEN_REPOSITORY):
    """Return the gav doc for a specific version, or None if absent from
    the repository."""
    key = (repository, group, artifact, version)
    if key in _MAVEN_VERSION_CACHE:
        return _MAVEN_VERSION_CACHE[key]
    info = _pom_info(group, artifact, version, repository)
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

    repository = _MAVEN_REPOSITORY
    if "repository" in entry:
        try:
            repository = _normalize_repository(entry.get("repository"))
        except ValueError as exc:
            # Propagate the specific reason (credentials, query/fragment,
            # malformed port, ...) instead of a generic complaint; the check
            # is pure, so this stays a no-network error row.
            result = _error_result(entry, str(exc))
            result["source"] = "maven_central"
            return result
        if repository is None:
            result = _error_result(
                entry,
                "'repository' must be an absolute http(s) URL when provided")
            result["source"] = "maven_central"
            return result

    where = ("Maven Central" if repository == _MAVEN_REPOSITORY
             else urllib.parse.urlsplit(repository).netloc)

    try:
        latest = _fetch_maven_latest(group, artifact, repository)
        in_use = _fetch_maven_specific(group, artifact, version, repository)
    except Exception as exc:
        summary = type(exc).__name__
        logger.error("%s fetch failed (%s)", where, summary)
        result = _error_result(entry, f"{where} query failed ({summary})")
        result["source"] = "maven_central"
        if repository != _MAVEN_REPOSITORY:
            result["source_label"] = where
        return result

    if not latest:
        result = _error_result(entry, f"Artifact {group}:{artifact} not found on {where}")
        result["source"] = "maven_central"
        if repository != _MAVEN_REPOSITORY:
            result["source_label"] = where
        return result

    latest_v = latest["v"]
    latest_date = latest["released"]
    latest_date_text = str(latest_date) if latest_date else "date unknown"
    in_use_date = in_use["released"] if in_use else None
    on_latest = latest_v == version

    # 'latest' is resolved independently of the in-use gav query, so the
    # in-use version may be absent from the repository (private build, typo,
    # or an indexing gap). That is unverifiable data quality -> unknown, not OK.
    status = "ok" if (in_use is not None or on_latest) else "unknown"

    if on_latest:
        message = f"On latest {where} release ({latest_v})"
    elif in_use_date and latest_date:
        days_newer = (latest_date - in_use_date).days
        message = (
            f"In use: {version} ({in_use_date}); latest: {latest_v} "
            f"({latest_date}, {days_newer} days newer)"
        )
    elif in_use is None:
        message = (
            f"Version {version} could not be verified on {where} "
            f"(private build, typo, or repository gap); "
            f"latest published is {latest_v} ({latest_date_text})"
        )
    elif in_use_date is None:
        message = (
            f"In use: {version} (release date unknown); "
            f"latest: {latest_v} ({latest_date_text})"
        )
    else:
        message = f"In use: {version}; latest: {latest_v}"

    result = {
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
        "repository": repository,
    }
    if repository != _MAVEN_REPOSITORY:
        # Provenance: a custom-repository row must not render under the
        # "Maven Central" source label; default rows gain no new key.
        result["source_label"] = where
    return result


SOURCE = "maven_central"
LABEL = "Maven Central"
provider = _provider_maven_central


def url_for(r):
    product = r.get("product") or ""
    if ":" not in product:
        return None
    group, artifact = product.split(":", 1)
    repository = r.get("repository")
    if repository and repository != _MAVEN_REPOSITORY:
        return f"{_artifact_base_url(group, artifact, repository)}/"
    return f"https://central.sonatype.com/artifact/{group}/{artifact}"
