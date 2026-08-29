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

Entries may instead set a 'repositories' key: a list of such base URLs
tried in order when the artifact is not found on the primary repository,
or when the primary lists the artifact but the pinned version could not
be verified there (e.g. Central has the metadata while the in-use release
lives only on the project's own repository). A candidate is probed only
when it can actually rescue the row: an artifact-missing trigger stops at
the first candidate that lists the artifact (first artifact hit wins),
while a version-missing trigger keeps probing until a candidate also
confirms the in-use version — a candidate that lists the artifact but not
the version does not win. A candidate whose normalized URL equals the
primary (the ordinary <id>central</id> declaration) is skipped silently:
the primary has already been probed. An unusable URL is skipped with a
logged warning; the chain is capped at the first 8 URLs (mirroring the
handler's stamping cap — the cap warning fires only when the chain
actually runs). A row rescued on a missing artifact carries the
provenance prefix "Not on Maven Central; found on <host>:" and a
source_label of the host, with latest and in-use data both from the
rescuing repository. A row rescued on a missing version carries the
prefix "Version <v> not on Maven Central; found on <host>:" — the
artifact was never missing from Central — and keeps Central's
authoritative latest (latest_patch, latest_patch_date, and the on_latest
comparison all reference Central) while the in-use release date and the
row's repository/source_label provenance come from the rescuing host. If
the whole chain yields nothing the row is a not-found error naming the
declared repositories (or, when the artifact itself resolved on Central,
an unknown row naming them); when instead the artifact resolved on a
fallback but the version did not, the unknown row names that winning
host, not the repository list. generate_config.py emits a config-level
"maven_repositories" list that handler.py stamps onto entries lacking an
explicit 'repository' (capped at 8 at load time), so hand-written configs
can use the same entry-level 'repositories' key directly. An explicit
'repository' keeps the single-repository behavior with no chain.
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
# Cap on how many declared 'repositories' URLs one provider call probes,
# mirroring handler.py's _MAX_STAMPED_REPOSITORIES: the chain runs up to
# two sequential fetches per repository INSIDE one check, where the
# runner's per-check time budget cannot intercede.
_MAX_CHAIN_REPOSITORIES = 8
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
# Deliberate exclusions (pinned by tests; extend only with evidence of a
# real-world Central casualty, none identified by review):
#   - bare "milestone"/"m"/"b" with no trailing number
#     ("1.0-milestone-3", "1.0.0-b"),
#   - unlisted qualifier spellings ("1.0-pre1", "1.0-dev").
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
    explicit_repository = "repository" in entry
    if explicit_repository:
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

    # Fallback chain: declared repositories to try when the artifact is not
    # found on the primary one, or when the primary lists the artifact but
    # the in-use version could not be verified there (the version may live
    # only on a declared repository). An explicit 'repository' keeps the
    # single-repository behavior (no chain).
    fallbacks = entry.get("repositories")
    if fallbacks is not None and not isinstance(fallbacks, list):
        result = _error_result(
            entry, "'repositories' must be a list of repository URLs")
        result["source"] = "maven_central"
        return result
    if explicit_repository:
        fallbacks = None

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

    fell_back = False
    # Whether the primary lists the artifact at all: an artifact-missing
    # rescue takes latest AND in-use from the winning candidate, while a
    # version-only rescue keeps Central's authoritative latest and takes
    # only the in-use verification from the winner.
    central_had_artifact = bool(latest)
    if (not latest or in_use is None) and fallbacks:
        if len(fallbacks) > _MAX_CHAIN_REPOSITORIES:
            # Same cap as the handler's stamping: the chain probes up to two
            # sequential URLs per repository inside ONE provider call, beyond
            # the runner's per-check budget. Only counts are logged, never
            # URLs. Inside the chain branch: an entry fully verified on the
            # primary probes nothing and logs nothing.
            logger.warning(
                "%s: %d declared repositories listed; probing the first %d only",
                label, len(fallbacks), _MAX_CHAIN_REPOSITORIES)
            fallbacks = fallbacks[:_MAX_CHAIN_REPOSITORIES]
        for candidate in fallbacks:
            try:
                candidate_repo = _normalize_repository(candidate)
            except ValueError as exc:
                logger.warning("%s: skipping declared repository (%s)",
                               label, exc)
                continue
            if not candidate_repo:
                logger.warning("%s: skipping blank declared repository", label)
                continue
            if candidate_repo == _MAVEN_REPOSITORY:
                # Already probed as the primary above; the ordinary
                # <id>central</id> declaration must not short-circuit the
                # chain. Skipped silently — nothing is wrong.
                continue
            try:
                candidate_latest = _fetch_maven_latest(
                    group, artifact, candidate_repo)
                if not candidate_latest:
                    continue
                candidate_in_use = _fetch_maven_specific(
                    group, artifact, version, candidate_repo)
            except Exception as exc:
                logger.warning(
                    "%s: declared repository lookup failed (%s)",
                    label, type(exc).__name__)
                continue
            if central_had_artifact and candidate_in_use is None:
                # Version-missing trigger: a candidate that lists the
                # artifact but not the in-use version does not rescue the
                # row — keep probing the chain for one that has the version.
                continue
            repository = candidate_repo
            if not central_had_artifact:
                latest = candidate_latest
            in_use = candidate_in_use
            fell_back = True
            break
        if fell_back:
            where = urllib.parse.urlsplit(repository).netloc

    if not latest:
        if fallbacks:
            result = _error_result(
                entry,
                f"Artifact {group}:{artifact} not found on Maven Central "
                f"or {len(fallbacks)} declared repositories")
        else:
            result = _error_result(
                entry, f"Artifact {group}:{artifact} not found on {where}")
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
        if fell_back:
            message = f"On latest release ({latest_v})"
        else:
            message = f"On latest {where} release ({latest_v})"
    elif in_use_date and latest_date:
        days_newer = (latest_date - in_use_date).days
        message = (
            f"In use: {version} ({in_use_date}); latest: {latest_v} "
            f"({latest_date}, {days_newer} days newer)"
        )
    elif in_use is None:
        if fell_back:
            message = (
                f"latest published is {latest_v} ({latest_date_text}); "
                f"version {version} could not be verified there "
                f"(private build, typo, or repository gap)"
            )
        elif fallbacks:
            # The chain ran (the version was unverified on Maven Central)
            # and no declared repository resolved it either — name the full
            # attempted chain, not Central alone.
            message = (
                f"Version {version} could not be verified on {where} "
                f"or {len(fallbacks)} declared repositories "
                f"(private build, typo, or repository gap); "
                f"latest published is {latest_v} ({latest_date_text})"
            )
        else:
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

    if fell_back:
        # Provenance first: this row was rescued from a declared repository.
        # When only the version was rescued the artifact lives on Central,
        # so "Not on Maven Central" would be false — say what was missing.
        if central_had_artifact:
            message = (f"Version {version} not on Maven Central; "
                       f"found on {where}: {message}")
        else:
            message = f"Not on Maven Central; found on {where}: {message}"

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
