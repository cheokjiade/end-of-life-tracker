"""Network-free canonical Maven repository tests (issue #12)."""

import logging
import os
import sys
import urllib.error
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.parsers import maven_central as maven


METADATA = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>org.example</groupId>
  <artifactId>widget</artifactId>
  <versioning>
    <latest>2.1.0</latest>
    <release>2.0.0</release>
    <versions><version>1.0.0</version><version>2.0.0</version></versions>
  </versioning>
</metadata>
"""


class FakeResponse:
    def __init__(self, body=b"", headers=None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def http_404(request):
    return urllib.error.HTTPError(
        request.full_url, 404, "Not Found", hdrs=None, fp=None)


def clear_caches():
    maven._MAVEN_LATEST_CACHE.clear()
    maven._MAVEN_VERSION_CACHE.clear()


# Pure metadata parsing: release wins, then latest, then the last version.
assert maven._parse_metadata_release(METADATA) == "2.0.0"
assert maven._parse_metadata_release(b"""
<metadata><versioning><latest>3.0</latest></versioning></metadata>
""") == "3.0"
assert maven._parse_metadata_release(b"""
<metadata><versioning><versions><version>1</version><version>2</version>
</versions></versioning></metadata>
""") == "2"
assert maven._parse_metadata_release(b"<metadata/>") is None
try:
    maven._parse_metadata_release(b"<not-closed>")
    raise AssertionError("malformed metadata XML was accepted")
except ValueError as exc:
    assert "invalid Maven metadata XML" in str(exc)
print("OK Maven metadata parsing")


# --- Stale pre-release tag handling (live netty metadata shape) --------------

# Pre-release qualifier segments (case-insensitive) mark a version unstable;
# Final/GA/RELEASE/sp<N>/plain numerics are stable.
for pre in ("5.0.0.Alpha2", "1.0.0-b1", "2.0.0-RC1", "9.9.M3",
            "1.0.0-SNAPSHOT", "3.0.0-beta7", "1.0.milestone2",
            "2.0.0-preview1", "21.0-ea", "1.0.0-cr1"):
    assert maven._is_prerelease_version(pre), pre
for stable in ("4.2.17.Final", "1.0.0", "2.22.0", "1.0.0.GA",
               "1.0.0.RELEASE", "3.2.2.sp1", "1.0.Final", "10.0.2"):
    assert not maven._is_prerelease_version(stable), stable
print("OK pre-release qualifier detection")


# Numeric-aware ordering: numeric segments compare numerically, stable beats
# pre-release at an equal numeric core, and text chunks break ties.
keys = [maven._version_order_key(v) for v in
        ("1.0.0-alpha", "1.0.0", "1.0.9", "1.0.10", "4.2.16.Final",
         "4.2.17.Final", "5.0.0.Alpha1", "5.0.0.Alpha2", "6.0.0")]
for earlier, later in zip(keys, keys[1:]):
    assert earlier < later, (earlier, later)
print("OK version ordering key")


# A release/latest tag is trusted only when it is stable AND listed; a stale
# pre-release tag falls back to the newest stable listed version.
NETTY_VERSIONS = [
    "4.1.118.Final", "4.1.119.Final", "4.2.15.Final", "4.2.16.Final",
    "4.2.17.Final", "5.0.0.Alpha1", "5.0.0.Alpha2",
]
assert maven._pick_latest("5.0.0.Alpha2", NETTY_VERSIONS) == "4.2.17.Final"
# A stable tag present in <versions> is trusted verbatim.
assert maven._pick_latest("2.22.0", ["1.0.0", "2.22.0", "3.0.0"]) == "2.22.0"
# A stable tag absent from <versions> is not authoritative either.
assert maven._pick_latest("3.0.0", ["2.0.0", "2.10.0"]) == "2.10.0"
# Every listed version pre-release -> deterministic highest overall.
assert maven._pick_latest(
    "1.0.0-beta2", ["1.0.0-alpha", "1.0.0-beta2", "2.0.0-RC1"]) == "2.0.0-RC1"
# No <versions> list (tiny/private metadata) -> trust the tag as today.
assert maven._pick_latest("5.0.0.Alpha2", None) == "5.0.0.Alpha2"
assert maven._pick_latest("5.0.0.Alpha2", []) == "5.0.0.Alpha2"
assert maven._pick_latest(None, []) is None
# ...and the metadata parser wires the pieces together.
assert maven._parse_metadata_release(b"""
<metadata><versioning>
  <latest>5.0.0.Alpha2</latest><release>5.0.0.Alpha2</release>
  <versions>
    <version>4.2.16.Final</version><version>4.2.17.Final</version>
    <version>5.0.0.Alpha1</version><version>5.0.0.Alpha2</version>
  </versions>
</versioning></metadata>
""") == "4.2.17.Final"
print("OK stale pre-release tag is not trusted as latest")


# Pure repository normalization: blank -> None, one trailing slash stripped,
# non-http(s) or relative values rejected.
assert maven._normalize_repository(None) is None
assert maven._normalize_repository("   ") is None
assert maven._normalize_repository(
    "  https://build.shibboleth.net/nexus/content/repositories/releases/  "
) == "https://build.shibboleth.net/nexus/content/repositories/releases"
assert maven._normalize_repository("https://example.com/repo//") == \
    "https://example.com/repo/"
assert maven._normalize_repository("http://localhost:8081/maven2") == \
    "http://localhost:8081/maven2"
for bad in ("not-a-url", "ftp://example.com/maven2", "/relative/path",
            "example.com/maven2"):
    try:
        maven._normalize_repository(bad)
        raise AssertionError(f"invalid repository accepted: {bad!r}")
    except ValueError:
        pass
print("OK repository normalization")


# Credentials, query/fragment, and malformed ports are rejected; scheme and
# host are canonicalized to lower case while the path keeps its case.
for bad, why in (
    ("https://user:secret@example.com/repo", "credentials"),
    ("https://user@example.com/repo", "credentials"),
    ("https://example.com/repo?x=1", "query or fragment"),
    ("https://example.com/repo#frag", "query or fragment"),
    ("https://h:badport/repo", "port"),
):
    try:
        maven._normalize_repository(bad)
        raise AssertionError(f"invalid repository accepted: {bad!r}")
    except ValueError as exc:
        assert why in str(exc), (bad, str(exc))
assert maven._normalize_repository("HTTPS://EXAMPLE.com/repo/") == \
    "https://example.com/repo"
print("OK repository normalization guards")


# Latest lookup: canonical metadata GET + POM HEAD, exact date, cache reuse.
clear_caches()
calls = []


def latest_urlopen(request, timeout):
    calls.append((request.full_url, request.get_method(), timeout))
    if request.full_url.endswith("maven-metadata.xml"):
        return FakeResponse(METADATA)
    assert request.full_url.endswith("/2.0.0/widget-2.0.0.pom")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT",
    })


real_urlopen = maven.urllib.request.urlopen
try:
    maven.urllib.request.urlopen = latest_urlopen
    first = maven._fetch_maven_latest("org.example", "widget")
    second = maven._fetch_maven_latest("org.example", "widget")
finally:
    maven.urllib.request.urlopen = real_urlopen

assert first == second == {
    "v": "2.0.0", "released": date(2025, 2, 25)}
assert len(calls) == 2, calls
assert calls[0][1] == "GET" and calls[1][1] == "HEAD"
assert all(call[2] == 10 for call in calls)
print("OK latest metadata/POM lookup and cache reuse")

# Metadata is bounded before XML parsing.
try:
    maven.urllib.request.urlopen = lambda *_args, **_kwargs: FakeResponse(
        b"x" * (maven._MAX_METADATA_BYTES + 1))
    try:
        maven._fetch_metadata_release("org.example", "oversized")
        raise AssertionError("oversized Maven metadata was accepted")
    except ValueError as exc:
        assert "exceeds 1 MiB" in str(exc)
finally:
    maven.urllib.request.urlopen = real_urlopen
print("OK Maven metadata response bound")


# Specific-version lookup confirms by POM HEAD; invalid date is tolerated.
clear_caches()
calls.clear()


def specific_urlopen(request, timeout):
    calls.append((request.full_url, request.get_method(), timeout))
    return FakeResponse(headers={"Last-Modified": "not-a-date"})


try:
    maven.urllib.request.urlopen = specific_urlopen
    info = maven._fetch_maven_specific("org.example", "widget", "1.0.0")
    again = maven._fetch_maven_specific("org.example", "widget", "1.0.0")
finally:
    maven.urllib.request.urlopen = real_urlopen
assert info == again == {"v": "1.0.0", "released": None}
assert len(calls) == 1 and calls[0][1] == "HEAD"
print("OK specific POM existence/date and cache reuse")

# A confirmed POM without a date remains healthy and is described truthfully.
real_latest = maven._fetch_maven_latest
real_specific = maven._fetch_maven_specific
try:
    maven._fetch_maven_latest = lambda *_args: {
        "v": "2.0.0", "released": date(2025, 2, 25)}
    maven._fetch_maven_specific = lambda *_args: {
        "v": "1.0.0", "released": None}
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
    }, date(2026, 8, 28))
finally:
    maven._fetch_maven_latest = real_latest
    maven._fetch_maven_specific = real_specific
assert result["status"] == "ok", result
assert "release date unknown" in result["message"], result
assert "not on Maven Central" not in result["message"], result
# Default-repository rows gain no per-row source-label override.
assert "source_label" not in result, result
print("OK confirmed undated POM message")

try:
    maven._fetch_maven_latest = lambda *_args: {
        "v": "2.0.0", "released": None}
    maven._fetch_maven_specific = lambda *_args: {
        "v": "1.0.0", "released": None}
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
    }, date(2026, 8, 28))
finally:
    maven._fetch_maven_latest = real_latest
    maven._fetch_maven_specific = real_specific
assert "latest: 2.0.0 (date unknown)" in result["message"], result
assert "None" not in result["message"], result
print("OK undated latest message")


# Repository path components are quoted; coordinates cannot alter the host.
base = maven._artifact_base_url("org.example space", "widget/name")
assert base == (
    "https://repo1.maven.org/maven2/org/example%20space/widget%2Fname")


# A clean 404 is stable not-found and may be cached.
clear_caches()
calls.clear()


def missing_urlopen(request, timeout):
    calls.append(request.full_url)
    raise http_404(request)


try:
    maven.urllib.request.urlopen = missing_urlopen
    assert maven._fetch_maven_specific("org.example", "missing", "1") is None
    assert maven._fetch_maven_specific("org.example", "missing", "1") is None
finally:
    maven.urllib.request.urlopen = real_urlopen
assert len(calls) == 1
print("OK HTTP 404 maps to cached not-found")


# Transient failures are never cached: a later attempt can recover.
clear_caches()
attempts = 0


def transient_urlopen(request, timeout):
    global attempts
    attempts += 1
    if attempts == 1:
        raise TimeoutError("simulated timeout")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT",
    })


try:
    maven.urllib.request.urlopen = transient_urlopen
    try:
        maven._fetch_maven_specific("org.example", "widget", "1.0.0")
        raise AssertionError("transient timeout was swallowed")
    except TimeoutError:
        pass
    key = (maven._MAVEN_REPOSITORY, "org.example", "widget", "1.0.0")
    assert key not in maven._MAVEN_VERSION_CACHE
    recovered = maven._fetch_maven_specific("org.example", "widget", "1.0.0")
finally:
    maven.urllib.request.urlopen = real_urlopen
assert recovered["released"].isoformat() == "2025-02-25"
assert attempts == 2
print("OK transient failures are not cached")


# Custom repository override: requests hit the configured host with the
# trailing slash stripped; the message names that host and the result
# carries the normalized effective base URL.
clear_caches()
calls.clear()
SHIBBOLETH = "https://build.shibboleth.net/nexus/content/repositories/releases/"


def shibboleth_urlopen(request, timeout):
    calls.append((request.full_url, request.get_method()))
    if request.full_url.endswith("maven-metadata.xml"):
        return FakeResponse(b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata><versioning><release>5.2.3</release></versioning></metadata>
""")
    assert request.full_url.endswith(
        "/org/opensaml/opensaml-core-api/5.2.3/"
        "opensaml-core-api-5.2.3.pom"), request.full_url
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT",
    })


try:
    maven.urllib.request.urlopen = shibboleth_urlopen
    result = maven._provider_maven_central({
        "label": "OpenSAML Core API",
        "group": "org.opensaml",
        "artifact": "opensaml-core-api",
        "version": "5.2.3",
        "repository": SHIBBOLETH,
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert calls, "provider made no network calls"
assert all("build.shibboleth.net" in url and "/releases/" in url
           for url, _method in calls), calls
assert calls[0] == (
    "https://build.shibboleth.net/nexus/content/repositories/releases"
    "/org/opensaml/opensaml-core-api/maven-metadata.xml",
    "GET"), calls[0]
assert calls[0][1] == "GET" and calls[1][1] == "HEAD", calls
assert result["status"] == "ok", result
assert "build.shibboleth.net" in result["message"], result
assert "Maven Central" not in result["message"], result
assert result["repository"] == (
    "https://build.shibboleth.net/nexus/content/repositories/releases")
# Provenance: a custom-repo row labels itself by host, not "Maven Central".
assert result["source_label"] == "build.shibboleth.net", result
print("OK custom repository override")


# Error rows from a custom repository keep the truthful host label;
# default-repository error rows gain no override.
clear_caches()
calls.clear()


def missing_repo_urlopen(request, timeout):
    calls.append(request.full_url)
    raise http_404(request)


try:
    maven.urllib.request.urlopen = missing_repo_urlopen
    shib_missing = maven._provider_maven_central({
        "label": "OpenSAML Core API",
        "group": "org.opensaml",
        "artifact": "opensaml-core-api",
        "version": "5.1.2",
        "repository": SHIBBOLETH,
    }, date(2026, 8, 28))
    clear_caches()
    default_missing = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert shib_missing["status"] == "error", shib_missing
assert "build.shibboleth.net" in shib_missing["message"], shib_missing
assert shib_missing["source_label"] == "build.shibboleth.net", shib_missing
assert default_missing["status"] == "error", default_missing
assert "source_label" not in default_missing, default_missing

# The fetch-failure branch (not just not-found) keeps the host label too.
calls.clear()


def failing_repo_urlopen(request, timeout):
    calls.append(request.full_url)
    raise TimeoutError("simulated timeout")


try:
    maven.urllib.request.urlopen = failing_repo_urlopen
    shib_failed = maven._provider_maven_central({
        "label": "OpenSAML Core API",
        "group": "org.opensaml",
        "artifact": "opensaml-core-api",
        "version": "5.1.2",
        "repository": SHIBBOLETH,
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert shib_failed["status"] == "error", shib_failed
assert "query failed (TimeoutError)" in shib_failed["message"], shib_failed
assert shib_failed["source_label"] == "build.shibboleth.net", shib_failed
print("OK error rows honour the repository label")


# Invalid repository values fail closed as error rows with zero network calls,
# propagating the specific normalization reason (which always names the field).
calls.clear()


def must_not_be_called(request, timeout):
    calls.append(request.full_url)
    return FakeResponse(METADATA)


for bad, exact in (
    ("not-a-url", None),
    ("https://user:secret@example.com/repo",
     "repository must not contain credentials"),
    ("https://example.com/repo?x=1", None),
    ("https://h:badport/repo", None),
):
    try:
        maven.urllib.request.urlopen = must_not_be_called
        result = maven._provider_maven_central({
            "label": "Broken repo",
            "group": "org.example",
            "artifact": "widget",
            "version": "1.0.0",
            "repository": bad,
        }, date(2026, 8, 28))
    finally:
        maven.urllib.request.urlopen = real_urlopen

    assert result["status"] == "error", (bad, result)
    assert result["message"].startswith("repository"), (bad, result)
    if exact is not None:
        assert result["message"] == exact, (bad, result)
assert calls == [], calls
print("OK invalid repository is a no-network error")


# Cache keys include the repository: the same gav in two repositories is
# fetched twice, once per host.
clear_caches()
calls.clear()


def two_repo_urlopen(request, timeout):
    calls.append(request.full_url)
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT",
    })


try:
    maven.urllib.request.urlopen = two_repo_urlopen
    maven._fetch_maven_specific(
        "org.example", "widget", "1.0.0",
        repository="https://repo-a.example/maven2")
    maven._fetch_maven_specific(
        "org.example", "widget", "1.0.0",
        repository="https://repo-b.example/maven2")
finally:
    maven.urllib.request.urlopen = real_urlopen

assert len(calls) == 2, calls
assert any("repo-a.example" in url for url in calls), calls
assert any("repo-b.example" in url for url in calls), calls
print("OK cache does not leak across repositories")


# url_for honours the repository: custom repos get their own artifact
# directory URL; default rows keep the central.sonatype.com artifact page.
shib_row = {
    "label": "OpenSAML Core API",
    "product": "org.opensaml:opensaml-core-api",
    "repository": "https://build.shibboleth.net/nexus/content/repositories/releases",
}
assert maven.url_for(shib_row) == (
    "https://build.shibboleth.net/nexus/content/repositories/releases"
    "/org/opensaml/opensaml-core-api/"), maven.url_for(shib_row)
assert maven.url_for({"product": "io.netty:netty-codec-http"}) == \
    "https://central.sonatype.com/artifact/io.netty/netty-codec-http"
assert maven.url_for({"product": "io.netty:netty-codec-http",
                      "repository": maven._MAVEN_REPOSITORY}) == \
    "https://central.sonatype.com/artifact/io.netty/netty-codec-http"
print("OK url_for honours the repository")


# End-to-end on the stubbed fetch path: netty-shaped metadata (stale
# pre-release release/latest = 5.0.0.Alpha2) yields latest 4.2.17.Final, and
# an in-use 4.1.x gets a sane positive "days newer" — never a negative one.
clear_caches()
calls.clear()
NETTY_METADATA = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>io.netty</groupId>
  <artifactId>netty-codec-http</artifactId>
  <versioning>
    <latest>5.0.0.Alpha2</latest>
    <release>5.0.0.Alpha2</release>
    <versions>
      <version>4.1.118.Final</version>
      <version>4.1.119.Final</version>
      <version>4.2.15.Final</version>
      <version>4.2.16.Final</version>
      <version>4.2.17.Final</version>
      <version>5.0.0.Alpha1</version>
      <version>5.0.0.Alpha2</version>
    </versions>
    <lastUpdated>20250729164314</lastUpdated>
  </versioning>
</metadata>
"""


def netty_urlopen(request, timeout):
    url = request.full_url
    calls.append((url, request.get_method()))
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(NETTY_METADATA)
    if "/4.2.17.Final/" in url:
        return FakeResponse(headers={
            "Last-Modified": "Wed, 16 Jul 2025 10:00:00 GMT"})
    assert url.endswith(
        "/4.1.119.Final/netty-codec-http-4.1.119.Final.pom"), url
    return FakeResponse(headers={
        "Last-Modified": "Wed, 26 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = netty_urlopen
    latest = maven._fetch_maven_latest("io.netty", "netty-codec-http")
    result = maven._provider_maven_central({
        "label": "Netty Codec HTTP 4.1.119",
        "group": "io.netty",
        "artifact": "netty-codec-http",
        "version": "4.1.119.Final",
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert latest == {"v": "4.2.17.Final", "released": date(2025, 7, 16)}, latest
assert result["status"] == "ok", result
assert result["latest_patch"] == "4.2.17.Final", result
days = (date(2025, 7, 16) - date(2025, 2, 26)).days
assert f"{days} days newer" in result["message"], result
assert "-3648" not in result["message"], result
# The selected latest — not the stale tag — is the version whose POM is probed.
assert any("/4.2.17.Final/netty-codec-http-4.2.17.Final.pom" in url
           for url, _method in calls), calls
assert any("/4.1.119.Final/netty-codec-http-4.1.119.Final.pom" in url
           for url, _method in calls), calls
print("OK netty-shaped metadata yields a sane latest")


# --- Declared-repository fallback chain --------------------------------------
#
# When an artifact is not found on the primary repository, the entry's
# 'repositories' list is tried in order (handler.py stamps the config-level
# maven_repositories list onto entries; hand-written configs set the same
# entry-level key directly). First non-None hit wins.

clear_caches()
calls.clear()
FALLBACK = "https://repo.example/custom/"


def central_404_fallback_urlopen(request, timeout):
    calls.append((request.full_url, request.get_method()))
    if request.full_url.startswith(maven._MAVEN_REPOSITORY):
        raise http_404(request)
    if request.full_url.endswith("maven-metadata.xml"):
        return FakeResponse(b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata><versioning><release>2.5.0</release></versioning></metadata>
""")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT",
    })


try:
    maven.urllib.request.urlopen = central_404_fallback_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": [FALLBACK],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "ok", result
assert result["repository"] == "https://repo.example/custom", result
assert result["source_label"] == "repo.example", result
assert result["message"].startswith(
    "Not on Maven Central; found on repo.example: "), result
assert "In use: 1.0.0" in result["message"], result
assert "latest: 2.5.0" in result["message"], result
# Maven Central was tried first (metadata GET + in-use POM HEAD, both 404),
# then the chain resolved everything on the declared repository.
central_calls = [c for c in calls if c[0].startswith(maven._MAVEN_REPOSITORY)]
assert len(central_calls) == 2, calls
assert all("repo.example" in c[0] for c in calls[2:]), calls
print("OK central 404 falls back to a declared repository (in-use found, ok row)")


# The whole chain missing -> the error row names the declared repositories;
# no custom repo produced the result, so no host label override.
clear_caches()
calls.clear()


def all_404_urlopen(request, timeout):
    calls.append(request.full_url)
    raise http_404(request)


try:
    maven.urllib.request.urlopen = all_404_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": [FALLBACK],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "error", result
assert result["message"] == (
    "Artifact org.example:widget not found on Maven Central "
    "or 1 declared repositories"), result
assert "source_label" not in result, result
print("OK central+fallback 404s produce the declared-repositories error row")


# An unusable URL inside the list is skipped with a logged warning and the
# chain continues to the next candidate.
clear_caches()
calls.clear()
captured = []


class _CaptureHandler(logging.Handler):
    def emit(self, record):
        captured.append(record.getMessage())


log_handler = _CaptureHandler()
logging.getLogger().addHandler(log_handler)
try:
    maven.urllib.request.urlopen = central_404_fallback_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": ["not-a-url", FALLBACK],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen
    logging.getLogger().removeHandler(log_handler)

assert result["status"] == "ok", result
assert result["repository"] == "https://repo.example/custom", result
assert any("skipping declared repository" in m for m in captured), captured
print("OK invalid URL in the chain logs a warning and the chain continues")


# First declared repository hit wins; later candidates are never touched.
clear_caches()
calls.clear()


def first_fallback_urlopen(request, timeout):
    calls.append(request.full_url)
    if request.full_url.startswith(maven._MAVEN_REPOSITORY):
        raise http_404(request)
    assert "first.example" in request.full_url, request.full_url
    if request.full_url.endswith("maven-metadata.xml"):
        return FakeResponse(b"""<metadata><versioning>
<release>3.1.0</release></versioning></metadata>""")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT",
    })


try:
    maven.urllib.request.urlopen = first_fallback_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": ["https://first.example/one",
                         "https://second.example/two"],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "ok", result
assert result["repository"] == "https://first.example/one", result
assert result["source_label"] == "first.example", result
assert not any("second.example" in u for u in calls), calls
print("OK first declared repository hit wins; later candidates untouched")


# An explicit 'repository' keeps today's single-repo behavior: the chain is
# not consulted even when it fails.
clear_caches()
calls.clear()


def shib_only_urlopen(request, timeout):
    calls.append(request.full_url)
    assert "build.shibboleth.net" in request.full_url, request.full_url
    raise http_404(request)


try:
    maven.urllib.request.urlopen = shib_only_urlopen
    result = maven._provider_maven_central({
        "label": "OpenSAML Core API",
        "group": "org.opensaml",
        "artifact": "opensaml-core-api",
        "version": "5.1.2",
        "repository": SHIBBOLETH,
        "repositories": ["https://unused.example/repo"],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "error", result
assert "not found on build.shibboleth.net" in result["message"], result
assert "declared repositories" not in result["message"], result
assert result["source_label"] == "build.shibboleth.net", result
assert all("build.shibboleth.net" in u for u in calls), calls
print("OK explicit repository keeps single-repo behavior (no chain)")


# A malformed 'repositories' value (not a list) fails closed with zero
# network calls, like an invalid explicit 'repository'.
calls.clear()
try:
    maven.urllib.request.urlopen = must_not_be_called
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": "https://repo.example/one",
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "error", result
assert "'repositories' must be a list of repository URLs" in result["message"], result
assert calls == [], calls
print("OK malformed 'repositories' value is a no-network error row")


clear_caches()
print("OK test_maven_repository")
