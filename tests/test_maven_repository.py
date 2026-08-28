"""Network-free canonical Maven repository tests (issue #12)."""

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
    key = ("org.example", "widget", "1.0.0")
    assert key not in maven._MAVEN_VERSION_CACHE
    recovered = maven._fetch_maven_specific(*key)
finally:
    maven.urllib.request.urlopen = real_urlopen
assert recovered["released"].isoformat() == "2025-02-25"
assert attempts == 2
print("OK transient failures are not cached")

clear_caches()
print("OK test_maven_repository")
