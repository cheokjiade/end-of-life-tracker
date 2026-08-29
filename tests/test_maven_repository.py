"""Network-free canonical Maven repository tests (issue #12)."""

import logging
import os
import random
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


# Deliberate-limitation pins (adversarial review of d78074a): these qualifier
# spellings are classified STABLE by design — bare "milestone"/"m"/"b" with
# no trailing number, and unlisted spellings like "pre1"/"dev". No real-world
# Maven Central casualty was identified for any of them; if one surfaces,
# extend _PRERELEASE_SEGMENT_RE (see the comment there) and this pin.
for stable_by_design in ("1.0-milestone-3", "1.0-pre1", "1.0-dev", "1.0.0-b"):
    assert not maven._is_prerelease_version(stable_by_design), stable_by_design
print("OK qualifier exclusions pinned: milestone-N/pre1/dev/b stay stable by design")


# Garbage inputs never crash and stay deterministic: the helpers stringify
# whatever they receive (str(None), str(123), repr(bytes)) and fall back to
# the plain tag when no <versions> list exists.
for garbage in (None, 123, b"bytes", b"1.0.0-alpha", "", object()):
    first = maven._is_prerelease_version(garbage)
    assert first == maven._is_prerelease_version(garbage), garbage
assert maven._is_prerelease_version(None) is False
assert maven._is_prerelease_version(123) is False
assert maven._is_prerelease_version(b"bytes") is False
assert maven._pick_latest(None, None) is None
assert maven._pick_latest(5, None) == 5          # no versions list -> tag as-is
assert maven._pick_latest(None, [1, 2]) == 2     # numeric-aware max still applies
assert maven._pick_latest(None, [None]) is None
assert maven._pick_latest(b"1.0", [b"1.0"]) == b"1.0"
assert maven._pick_latest(3, [b"a", 2]) == 2
for args in ((None, [1, 2]), (None, [None]), (b"1.0", [b"1.0"]), (3, [b"a", 2])):
    assert maven._pick_latest(*args) == maven._pick_latest(*args), args
print("OK garbage inputs (None/int/bytes) are crash-free and deterministic")


# Order-independence: the chosen latest never depends on the <versions>
# listing order (metadata files are not guaranteed to be sorted).
rng = random.Random(1234)
for tag, versions, expected in (
    ("5.0.0.Alpha2", NETTY_VERSIONS, "4.2.17.Final"),   # stale pre-release tag
    ("2.22.0", ["1.0.0", "2.22.0", "3.0.0"], "2.22.0"),  # stable listed tag
    (None, ["1.0.9", "1.0.10", "1.0.2"], "1.0.10"),      # no tag, numeric max
):
    assert maven._pick_latest(tag, list(versions)) == expected, (tag, versions)
    assert maven._pick_latest(tag, list(reversed(versions))) == expected
    for _ in range(5):
        shuffled = list(versions)
        rng.shuffle(shuffled)
        assert maven._pick_latest(tag, shuffled) == expected, (tag, shuffled)
print("OK latest selection is independent of the versions-list order")


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


# --- Fallback trigger also fires when the VERSION is missing on Central ------
#
# Live-repro shape (org.opensaml:opensaml:2.6.6): Maven Central HAS the
# artifact (metadata's latest is 2.6.4) but the pinned 2.6.6 exists only on
# the declared repository. The chain must fire on the unverified version,
# rescue the version from the declared repository, and mark it ok with the
# version-provenance prefix — Central's authoritative latest is kept (the
# artifact was never missing there), so the row compares staleness against
# 2.6.4, not the declared repository's own latest.

clear_caches()
calls.clear()


def opensaml_urlopen(request, timeout):
    url = request.full_url
    calls.append((url, request.get_method()))
    if url.startswith(maven._MAVEN_REPOSITORY):
        if url.endswith("maven-metadata.xml"):
            return FakeResponse(b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata><versioning>
  <latest>2.6.4</latest><release>2.6.4</release>
  <versions><version>2.6.4</version></versions>
</versioning></metadata>
""")
        # In-use POM HEAD on Central: 2.6.6 is not published there.
        if "/2.6.6/" in url:
            raise http_404(request)
        assert "/2.6.4/opensaml-2.6.4.pom" in url, url
        return FakeResponse(headers={
            "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})
    assert "build.shibboleth.net" in url, url
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(b"""<metadata><versioning>
<release>3.0.0</release></versioning></metadata>""")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = opensaml_urlopen
    result = maven._provider_maven_central({
        "label": "OpenSAML 2.6.6",
        "group": "org.opensaml",
        "artifact": "opensaml",
        "version": "2.6.6",
        "repositories": [SHIBBOLETH],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "ok", result
assert result["repository"] == (
    "https://build.shibboleth.net/nexus/content/repositories/releases"), result
assert result["source_label"] == "build.shibboleth.net", result
assert result["message"].startswith(
    "Version 2.6.6 not on Maven Central; found on build.shibboleth.net: "), result
assert "In use: 2.6.6" in result["message"], result
assert "latest: 2.6.4" in result["message"], result
assert result["latest_patch"] == "2.6.4", result
central_calls = [c for c in calls if c[0].startswith(maven._MAVEN_REPOSITORY)]
assert len(central_calls) == 3, calls  # metadata GET + 2.6.4 HEAD + 2.6.6 HEAD
print("OK central metadata present but version 404s -> chain rescues (ok row)")


# Version found NOWHERE (artifact present on Central, version missing on
# Central and on every declared repository) -> unknown row naming the full
# attempted chain, with the latest the artifact metadata still reports.

clear_caches()
calls.clear()


def version_missing_everywhere_urlopen(request, timeout):
    calls.append(request.full_url)
    url = request.full_url
    if url.startswith(maven._MAVEN_REPOSITORY) and \
            url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>2.6.4</release>"
                            b"</versioning></metadata>")
    raise http_404(request)


try:
    maven.urllib.request.urlopen = version_missing_everywhere_urlopen
    result = maven._provider_maven_central({
        "label": "OpenSAML 2.6.6",
        "group": "org.opensaml",
        "artifact": "opensaml",
        "version": "2.6.6",
        "repositories": [FALLBACK],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "unknown", result
assert result["message"] == (
    "Version 2.6.6 could not be verified on Maven Central "
    "or 1 declared repositories (private build, typo, or repository gap); "
    "latest published is 2.6.4 (date unknown)"), result
print("OK version missing on Central and every declared repo -> unknown chain wording")


# Entries WITHOUT a 'repositories' list keep the byte-identical Central-only
# could-not-verify message (the chain never runs; no source-label override).

clear_caches()
calls.clear()


def central_meta_404_poms_urlopen(request, timeout):
    calls.append(request.full_url)
    url = request.full_url
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>2.0.0</release>"
                            b"</versioning></metadata>")
    if "/1.0.0/" in url:
        raise http_404(request)
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = central_meta_404_poms_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "unknown", result
assert result["message"] == (
    "Version 1.0.0 could not be verified on Maven Central "
    "(private build, typo, or repository gap); "
    "latest published is 2.0.0 (2025-02-25)"), result
assert "source_label" not in result, result
print("OK no-fallbacks could-not-verify message stays byte-identical")


# --- Chain cap: at most 8 declared repositories probed per call ---------------
#
# A hand-written 300-URL list would otherwise drive up to 600 sequential
# fetches inside ONE provider call, beyond the runner's per-check budget.

clear_caches()
calls.clear()
captured = []
log_handler = _CaptureHandler()
logging.getLogger().addHandler(log_handler)
TEN_REPOS = [f"https://c{i}.example/repo" for i in range(1, 11)]


def all_404_cap_urlopen(request, timeout):
    calls.append(request.full_url)
    raise http_404(request)


try:
    maven.urllib.request.urlopen = all_404_cap_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": TEN_REPOS,
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen
    logging.getLogger().removeHandler(log_handler)

assert result["status"] == "error", result
assert result["message"] == (
    "Artifact org.example:widget not found on Maven Central "
    "or 8 declared repositories"), result
assert any("probing the first 8 only" in m for m in captured), captured
assert not any(
    "c9.example" in u or "c10.example" in u for u in calls), calls
assert any("c8.example" in u for u in calls), calls
print("OK 10-URL chain is capped at the first 8 with a count warning")


# --- Adversarial-review pins: chain triggers and provenance -------------------

# G1: artifact AND in-use version both verified on Central -> the chain never
# runs at all (zero fallback probes), even though the declared repository
# would answer too. Protects the trigger against first-artifact regressions.

clear_caches()
calls.clear()


def central_ok_fallback_unused_urlopen(request, timeout):
    calls.append(request.full_url)
    url = request.full_url
    assert url.startswith(maven._MAVEN_REPOSITORY), url
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>2.0.0</release>"
                            b"</versioning></metadata>")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = central_ok_fallback_unused_urlopen
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
assert len(calls) == 3, calls  # Central: metadata GET + 2.0.0 latest HEAD + 1.0.0 in-use HEAD
assert "Not on Maven Central" not in result["message"], result
assert "source_label" not in result, result
print("OK in-use verified on Central -> zero fallback probes")

# G4 (F3): the 8-repository cap warning fires only when the chain actually
# runs — an entry fully verified on Central logs nothing despite 10 URLs.

clear_caches()
calls.clear()
captured = []
log_handler = _CaptureHandler()
logging.getLogger().addHandler(log_handler)
try:
    maven.urllib.request.urlopen = central_ok_fallback_unused_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": TEN_REPOS,
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen
    logging.getLogger().removeHandler(log_handler)

assert result["status"] == "ok", result
assert captured == [], captured
assert all(c.startswith(maven._MAVEN_REPOSITORY) for c in calls), calls
print("OK no cap warning when the chain never runs (in-use verified on Central)")

# G2 (F1): a declared repository equal to the primary — the ordinary
# <id>central</id> pom declaration, here with a trailing slash to prove the
# normalized comparison — is skipped silently in the chain. Without this,
# its cache hit short-circuits the chain into a self-contradictory
# "Not on Maven Central; found on repo1.maven.org" row and the rescuing
# repository behind it is never probed.

clear_caches()
calls.clear()


def central_candidate_chain_urlopen(request, timeout):
    calls.append((request.full_url, request.get_method()))
    url = request.full_url
    if url.startswith(maven._MAVEN_REPOSITORY):
        if url.endswith("maven-metadata.xml"):
            return FakeResponse(b"<metadata><versioning>"
                                b"<release>9.0.0</release>"
                                b"</versioning></metadata>")
        if "/9.0.0/" in url:
            return FakeResponse(headers={
                "Last-Modified": "Mon, 01 Jun 2026 10:00:00 GMT"})
        # In-use 1.0.0 not published on Central.
        raise http_404(request)
    assert "build.shibboleth.net" in url, url
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>5.2.3</release>"
                            b"</versioning></metadata>")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = central_candidate_chain_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": ["https://repo1.maven.org/maven2/", SHIBBOLETH],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "ok", result
assert result["message"].startswith(
    "Version 1.0.0 not on Maven Central; found on build.shibboleth.net: "), result
assert "repo1.maven.org" not in result["message"], result
assert result["repository"] == (
    "https://build.shibboleth.net/nexus/content/repositories/releases"), result
assert result["source_label"] == "build.shibboleth.net", result
assert any("build.shibboleth.net" in c[0] for c in calls), calls
central_calls = [c for c in calls if c[0].startswith(maven._MAVEN_REPOSITORY)]
assert len(central_calls) == 3, calls  # metadata + 9.0.0 latest HEAD + 1.0.0 HEAD 404
print("OK Central-as-candidate is skipped silently; the chain reaches Shibboleth")

# G3 (F2): under the version-missing trigger a candidate that HAS the
# artifact but NOT the version no longer wins — the chain keeps probing
# until one confirms the version; the row keeps Central's authoritative
# latest (fresh metadata) while the version verification and provenance
# come from the candidate that actually has it.

clear_caches()
calls.clear()
REPO_A = "https://a.example/one"
REPO_B = "https://b.example/two"


def ab_chain_urlopen(request, timeout):
    calls.append((request.full_url, request.get_method()))
    url = request.full_url
    if url.startswith(maven._MAVEN_REPOSITORY):
        if url.endswith("maven-metadata.xml"):
            return FakeResponse(b"<metadata><versioning>"
                                b"<release>9.0.0</release>"
                                b"</versioning></metadata>")
        if "/9.0.0/" in url:
            return FakeResponse(headers={
                "Last-Modified": "Mon, 01 Jun 2026 10:00:00 GMT"})
        raise http_404(request)   # in-use 1.0.0 not published on Central
    if url.startswith(REPO_A):
        if url.endswith("maven-metadata.xml"):
            return FakeResponse(b"<metadata><versioning>"
                                b"<release>2.5.0</release>"
                                b"</versioning></metadata>")
        raise http_404(request)   # artifact listed, but no 1.0.0 POM
    assert url.startswith(REPO_B), url
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>3.0.0</release>"
                            b"</versioning></metadata>")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = ab_chain_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": [REPO_A, REPO_B],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "ok", result
assert result["latest_patch"] == "9.0.0", result          # Central's latest kept
assert result["latest_patch_date"] == "2026-06-01", result
assert result["message"].startswith(
    "Version 1.0.0 not on Maven Central; found on b.example: "), result
assert "In use: 1.0.0" in result["message"], result
assert "latest: 9.0.0" in result["message"], result
assert "2.5.0" not in result["message"], result
assert result["repository"] == REPO_B, result
assert result["source_label"] == "b.example", result
assert any("a.example" in c[0] for c in calls), calls      # A probed...
assert any("b.example" in c[0] for c in calls), calls      # ...and B too
print("OK version-missing chain requires a version hit; Central latest kept")

# ...and when NO candidate has the version either, the existing chain
# wording is byte-identical and "latest published" still names Central's
# (a candidate that lists the artifact never hijacks the row).

clear_caches()
calls.clear()


def ab_no_version_urlopen(request, timeout):
    calls.append(request.full_url)
    url = request.full_url
    if url.endswith("maven-metadata.xml"):
        if url.startswith(maven._MAVEN_REPOSITORY):
            return FakeResponse(b"<metadata><versioning>"
                                b"<release>9.0.0</release>"
                                b"</versioning></metadata>")
        if url.startswith(REPO_A):
            return FakeResponse(b"<metadata><versioning>"
                                b"<release>2.5.0</release>"
                                b"</versioning></metadata>")
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>3.0.0</release>"
                            b"</versioning></metadata>")
    if "/9.0.0/" in url:
        return FakeResponse(headers={
            "Last-Modified": "Mon, 01 Jun 2026 10:00:00 GMT"})
    raise http_404(request)   # 1.0.0 (and every latest POM) missing


try:
    maven.urllib.request.urlopen = ab_no_version_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": [REPO_A, REPO_B],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "unknown", result
assert result["message"] == (
    "Version 1.0.0 could not be verified on Maven Central "
    "or 2 declared repositories (private build, typo, or repository gap); "
    "latest published is 9.0.0 (2026-06-01)"), result
assert result["repository"] == maven._MAVEN_REPOSITORY, result
assert "source_label" not in result, result
assert any("a.example" in u for u in calls), calls
assert any("b.example" in u for u in calls), calls
print("OK version missing on Central and every candidate -> unchanged wording")

# G6: an EMPTY 'repositories' list behaves exactly like no list — the
# message is byte-identical to the no-list run of the same entry, with no
# chain, no warning, and no source-label override.

clear_caches()
calls.clear()
try:
    maven.urllib.request.urlopen = central_meta_404_poms_urlopen
    empty_list = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": [],
    }, date(2026, 8, 28))
    clear_caches()
    no_list = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert empty_list["status"] == "unknown", empty_list
assert empty_list["message"] == no_list["message"], (empty_list, no_list)
assert empty_list["message"] == (
    "Version 1.0.0 could not be verified on Maven Central "
    "(private build, typo, or repository gap); "
    "latest published is 2.0.0 (2025-02-25)"), empty_list
assert "source_label" not in empty_list, empty_list
print("OK empty 'repositories' list behaves exactly like no list")


# --- Adversarial-review pins: chain results stay truthful ---------------------

# Trap (i): the winning fallback HAS the artifact but the in-use version
# 404s on THAT repository -> unknown, never ok.

clear_caches()
calls.clear()


def fallback_lacks_version_urlopen(request, timeout):
    calls.append(request.full_url)
    url = request.full_url
    if url.startswith(maven._MAVEN_REPOSITORY):
        raise http_404(request)
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>2.5.0</release>"
                            b"</versioning></metadata>")
    if "/1.0.0/" in url:
        raise http_404(request)
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = fallback_lacks_version_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": [FALLBACK],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert result["status"] == "unknown", result
assert result["message"].startswith(
    "Not on Maven Central; found on repo.example: "), result
assert "latest published is 2.5.0 (2025-02-25)" in result["message"], result
assert "version 1.0.0 could not be verified there" in result["message"], result
print("OK artifact rescued but version 404s on the fallback -> unknown, not ok")


# Trap (iii): a garbage-XML repository mid-chain logs a warning and the
# chain continues to the next candidate.

clear_caches()
calls.clear()
captured = []
log_handler = _CaptureHandler()
logging.getLogger().addHandler(log_handler)
BAD_XML = "https://bad-xml.example/repo"


def garbage_xml_chain_urlopen(request, timeout):
    calls.append(request.full_url)
    url = request.full_url
    if url.startswith(maven._MAVEN_REPOSITORY):
        raise http_404(request)
    if url.startswith(BAD_XML) and url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<not-closed>")
    assert url.startswith(FALLBACK), url
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>2.5.0</release>"
                            b"</versioning></metadata>")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = garbage_xml_chain_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": [BAD_XML, FALLBACK],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen
    logging.getLogger().removeHandler(log_handler)

assert result["status"] == "ok", result
assert result["repository"] == "https://repo.example/custom", result
assert any("declared repository lookup failed" in m for m in captured), captured
print("OK garbage-XML repo mid-chain warns and the chain continues")


# A blank/whitespace URL in the chain is skipped with a logged warning and
# the chain continues.

clear_caches()
calls.clear()
captured = []
log_handler = _CaptureHandler()
logging.getLogger().addHandler(log_handler)
try:
    maven.urllib.request.urlopen = central_404_fallback_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": ["   ", FALLBACK],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen
    logging.getLogger().removeHandler(log_handler)

assert result["status"] == "ok", result
assert result["repository"] == "https://repo.example/custom", result
assert any("skipping blank declared repository" in m for m in captured), captured
print("OK blank URL in the chain logs a skip and the chain continues")


# A credential-bearing URL in the chain is skipped and the URL itself is
# never logged (the ValueError reason names the field, not the value).

clear_caches()
calls.clear()
captured = []
log_handler = _CaptureHandler()
logging.getLogger().addHandler(log_handler)
try:
    maven.urllib.request.urlopen = central_404_fallback_urlopen
    result = maven._provider_maven_central({
        "label": "Widget",
        "group": "org.example",
        "artifact": "widget",
        "version": "1.0.0",
        "repositories": ["https://user:secret@example.com/repo", FALLBACK],
    }, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen
    logging.getLogger().removeHandler(log_handler)

assert result["status"] == "ok", result
assert result["repository"] == "https://repo.example/custom", result
assert any("skipping declared repository" in m and "credentials" in m
           for m in captured), captured
assert not any("secret" in m for m in captured), captured
assert not any("example.com" in m for m in captured), captured
assert not any("example.com" in u for u in calls), calls
print("OK credential-bearing URL skipped and never logged")


# Cache partitioning under the fallback chain: a chain-resolved row is
# served entirely from the per-repository caches on a repeat call, and a
# different declared repository probes its own partition.

clear_caches()
calls.clear()
REPO_A = "https://chain-a.example/maven2"
REPO_B = "https://chain-b.example/maven2"
CACHE_ENTRY = {
    "label": "Widget",
    "group": "org.example",
    "artifact": "widget",
    "version": "1.0.0",
}


def chain_cache_urlopen(request, timeout):
    calls.append(request.full_url)
    url = request.full_url
    if url.startswith(maven._MAVEN_REPOSITORY):
        raise http_404(request)
    assert "chain-a.example" in url, url
    if url.endswith("maven-metadata.xml"):
        return FakeResponse(b"<metadata><versioning>"
                            b"<release>2.5.0</release>"
                            b"</versioning></metadata>")
    return FakeResponse(headers={
        "Last-Modified": "Tue, 25 Feb 2025 16:43:14 GMT"})


try:
    maven.urllib.request.urlopen = chain_cache_urlopen
    first = maven._provider_maven_central(
        {**CACHE_ENTRY, "repositories": [REPO_A]}, date(2026, 8, 28))
    after_first = len(calls)
    second = maven._provider_maven_central(
        {**CACHE_ENTRY, "repositories": [REPO_A]}, date(2026, 8, 28))
    after_second = len(calls)
    third = maven._provider_maven_central(
        {**CACHE_ENTRY, "repositories": [REPO_B]}, date(2026, 8, 28))
finally:
    maven.urllib.request.urlopen = real_urlopen

assert first["status"] == "ok", first
assert first["repository"] == "https://chain-a.example/maven2", first
assert second == first, second
assert after_second == after_first, (after_second, after_first)
assert third["status"] == "error", third
assert third["message"] == (
    "Artifact org.example:widget not found on Maven Central "
    "or 1 declared repositories"), third
assert any("chain-b.example" in u for u in calls[after_second:]), calls
print("OK chain results are cached per repository; other repos probe their own partition")


clear_caches()
print("OK test_maven_repository")
