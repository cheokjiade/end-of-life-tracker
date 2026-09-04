"""Network-free tests for the NuGet V3 registry provider.

Standalone assertion script (no test framework): builds synthetic V3
service-index / registration documents, injects them through a fake
``_http_get_json`` (or a fake ``urllib.request.urlopen`` for gzip), and
exercises the provider end to end. Never touches the network.
"""

import gzip
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.parsers import check_product, PROVIDERS, SOURCE_LABELS, source_url_for
from eoltracker.parsers import nuget_registry as nuget
from eoltracker.report import format_report_text

TODAY = date(2026, 8, 28)

SERVICE_INDEX_URL = "https://api.nuget.org/v3/index.json"
REG_BASE = "https://api.nuget.org/v3/registrations-test"
CATALOG = "https://api.nuget.example/catalog"


# ---------------------------------------------------------------------------
# Synthetic NuGet V3 documents
# ---------------------------------------------------------------------------

def service_index(types=("RegistrationsBaseUrl/3.6.0",), resources=None):
    if resources is None:
        resources = [{"@id": f"{REG_BASE}/", "@type": t} for t in types]
    return {"version": "3.0.0", "resources": resources}


def leaf(version, published, listed=True, deprecation=None, pkg="Newtonsoft.Json"):
    """A registration leaf's catalogEntry (what _collect_leaves yields)."""
    ce = {
        "@id": f"{CATALOG}/{pkg.lower()}/{version}.json",
        "id": pkg,
        "version": version,
        "published": published,
        "listed": listed,
    }
    if deprecation is not None:
        ce["deprecation"] = deprecation
    return ce


def page(*entries, page_id="p1"):
    """A registration page carrying inline items."""
    return {
        "@id": f"{REG_BASE}/x/{page_id}.json",
        "count": len(entries),
        "items": [{"@id": f"{REG_BASE}/x/{page_id}/{e['version']}.json",
                   "catalogEntry": e} for e in entries],
    }


def reg_index(*pages):
    return {"@id": f"{REG_BASE}/x/index.json", "@type": "Package",
            "count": len(pages), "items": list(pages)}


def docs_for(entries, pkg="Newtonsoft.Json"):
    """Service index + single-page registration for *pkg*."""
    return {
        SERVICE_INDEX_URL: service_index(),
        f"{REG_BASE}/{pkg.lower()}/index.json": reg_index(page(*entries)),
    }


def newtonsoft_docs(**extra):
    entries = [
        leaf("9.0.1", "2016-06-27T10:59:44Z"),
        leaf("12.0.1", "2021-03-19T00:13:12Z"),
        leaf("13.0.1", "2021-03-19T00:13:12Z"),
        leaf("13.0.3", "2023-11-08T19:29:25Z"),
        leaf("13.1.0-beta", "2026-01-15T08:00:00Z"),
    ]
    docs = docs_for(entries)
    docs.update(extra)
    return docs


# ---------------------------------------------------------------------------
# Fetch injection helpers
# ---------------------------------------------------------------------------

class FakeHttp:
    """Deterministic stand-in for nuget_registry._http_get_json."""

    def __init__(self, docs):
        self.docs = dict(docs)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.docs:
            raise AssertionError(f"unexpected fetch: {url}")
        doc = self.docs[url]
        if isinstance(doc, Exception):
            raise doc
        return doc


class FakeResponse:
    """Context-manager stand-in for the object urllib.request.urlopen returns
    (a compliant stream: each read consumes up to *size* bytes)."""

    def __init__(self, body, headers=None):
        self._body = body
        self._offset = 0
        self.headers = headers or {}

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._body[self._offset:]
        else:
            chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_REAL_GET = nuget._http_get_json
_REAL_URLOPEN = urllib.request.urlopen

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ---------------------------------------------------------------------------
# Registration and URL generation
# ---------------------------------------------------------------------------

@test
def t_registration_wiring():
    assert nuget.SOURCE == "nuget_registry"
    assert PROVIDERS["nuget_registry"] is nuget.provider
    assert SOURCE_LABELS["nuget_registry"] == "NuGet"
    assert source_url_for({"source": "nuget_registry",
                           "product": "Newtonsoft.Json"}) == \
        "https://www.nuget.org/packages/Newtonsoft.Json"
    assert nuget.url_for({"product": "Newtonsoft.Json"}) == \
        "https://www.nuget.org/packages/Newtonsoft.Json"
    assert nuget.url_for({"product": ""}) is None
    assert nuget.url_for({}) is None
    # check_product dispatch reaches the provider (cache injected, no fetch)
    nuget._NUGET_CACHE[("pkg", "dispatch.pkg")] = [
        leaf("1.0.0", "2025-01-01T00:00:00Z", pkg="Dispatch.Pkg")]
    r = check_product({"source": "nuget_registry", "package": "Dispatch.Pkg",
                       "version": "1.0.0"}, TODAY)
    assert r["status"] == "ok" and r["source"] == "nuget_registry", r


# ---------------------------------------------------------------------------
# Happy path: full normalized result shape
# ---------------------------------------------------------------------------

@test
def t_full_result_shape_on_latest_stale():
    fake = FakeHttp(newtonsoft_docs())
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "Newtonsoft.Json",
         "version": "13.0.3", "label": "Newtonsoft.Json 13.0.3"}, TODAY)
    required = {"label", "product", "version", "lts", "in_use_release_date",
                "latest_patch", "latest_patch_date", "latest_cycle",
                "latest_cycle_version", "latest_cycle_release_date",
                "on_latest_cycle", "eol_date", "support_date",
                "days_remaining", "support_days_remaining", "source",
                "status", "message"}
    assert not (required - set(r)), f"missing keys: {required - set(r)}"
    assert r["status"] == "ok", r
    assert r["label"] == "Newtonsoft.Json 13.0.3"
    assert r["product"] == "Newtonsoft.Json"
    assert r["version"] == "13.0.3"
    assert r["in_use_release_date"] == "2023-11-08"
    assert r["latest_patch"] == "13.0.3"
    assert r["latest_patch_date"] == "2023-11-08"
    assert r["on_latest_cycle"] is True
    assert r["eol_date"] is None and r["days_remaining"] is None
    assert r["source"] == "nuget_registry"
    # 2023-11-08 is ~33 months before TODAY: stale, informational only
    assert "likely unmaintained" in r["message"], r["message"]
    assert "2023-11-08" in r["message"]


@test
def t_fresh_on_latest_message():
    leaves = [leaf("1.0.0", "2026-08-20T10:00:00Z", pkg="Fresh.Lib")]
    r = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "Fresh.Lib", "version": "1.0.0"},
        leaves, date(2026, 8, 28))
    assert r["status"] == "ok"
    assert r["message"] == "On latest stable NuGet release (1.0.0 (2026-08-20))", r["message"]


@test
def t_behind_message():
    fake = FakeHttp(newtonsoft_docs())
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "Newtonsoft.Json",
         "version": "12.0.1"}, TODAY)
    assert r["status"] == "ok", r
    assert r["in_use_release_date"] == "2021-03-19"
    assert r["latest_patch"] == "13.0.3"
    assert r["on_latest_cycle"] is False
    assert "In use 12.0.1 published 2021-03-19" in r["message"], r["message"]
    assert "latest stable 13.0.3 (2023-11-08)" in r["message"], r["message"]
    assert "1 major(s) behind" in r["message"], r["message"]


@test
def t_no_version_pinned():
    fake = FakeHttp(newtonsoft_docs())
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "Newtonsoft.Json"}, TODAY)
    assert r["status"] == "ok", r
    assert "Pinned version not provided" in r["message"], r["message"]
    assert r["latest_patch"] == "13.0.3"


# ---------------------------------------------------------------------------
# Error paths: 404, non-404 HTTP, missing fields, absent versions
# ---------------------------------------------------------------------------

@test
def t_404_package_missing():
    reg_url = f"{REG_BASE}/nosuch.pkg/index.json"
    fake = FakeHttp({SERVICE_INDEX_URL: service_index(),
                     reg_url: urllib.error.HTTPError(reg_url, 404, "Not Found",
                                                     None, None)})
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "NoSuch.Pkg", "version": "1.0.0"},
        TODAY)
    assert r["status"] == "error", r
    assert "not found" in r["message"].lower(), r["message"]
    assert r["source"] == "nuget_registry"
    assert r["product"] == "NoSuch.Pkg"
    assert r["label"] == "NoSuch.Pkg 1.0.0"
    assert nuget._NUGET_CACHE[("pkg", "nosuch.pkg")] is None  # 404 is cached


@test
def t_http_500_is_error():
    reg_url = f"{REG_BASE}/brokenhttp.pkg/index.json"
    fake = FakeHttp({SERVICE_INDEX_URL: service_index(),
                     reg_url: urllib.error.HTTPError(reg_url, 500, "Server Error",
                                                     None, None)})
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "BrokenHttp.Pkg",
         "version": "1.0.0"}, TODAY)
    assert r["status"] == "error", r
    assert "query failed" in r["message"], r["message"]


@test
def t_missing_package_field():
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "version": "1.0.0"}, TODAY)
    assert r["status"] == "error", r
    assert "require 'package'" in r["message"], r["message"]


@test
def t_version_not_found_is_error():
    fake = FakeHttp(newtonsoft_docs())
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "Newtonsoft.Json",
         "version": "99.0.0"}, TODAY)
    assert r["status"] == "error", r
    assert "99.0.0" in r["message"], r["message"]
    assert "13.0.3" in r["message"], r["message"]
    assert r["product"] == "Newtonsoft.Json"


# ---------------------------------------------------------------------------
# Defensive structural errors (loud, not silent)
# ---------------------------------------------------------------------------

@test
def t_malformed_service_index_is_error():
    fake = FakeHttp({SERVICE_INDEX_URL: {
        "resources": [{"@id": "https://s", "@type": "SearchQueryService"}]}})
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "X.Pkg", "version": "1.0.0"}, TODAY)
    assert r["status"] == "error", r
    assert "RegistrationsBaseUrl" in r["message"], r["message"]


@test
def t_pick_registration_base_preferences():
    idx = service_index(resources=[
        {"@id": "https://x/old", "@type": "RegistrationsBaseUrl"},
        {"@id": "https://x/v34", "@type": "RegistrationsBaseUrl/3.4.0"},
        {"@id": "https://x/v36", "@type": "RegistrationsBaseUrl/3.6.0"},
        {"@id": "https://x/search", "@type": "SearchQueryService"},
    ])
    assert nuget._pick_registration_base(idx) == "https://x/v36"
    idx2 = service_index(resources=[
        {"@id": "https://x/old/", "@type": "RegistrationsBaseUrl"},
        {"@id": "https://x/search", "@type": "SearchQueryService"},
    ])
    assert nuget._pick_registration_base(idx2) == "https://x/old/"
    for bad in ({}, {"resources": []}, {"resources": "nope"}, "nope",
                {"resources": [{"@type": "SearchQueryService", "@id": "https://s"}]},
                {"resources": [{"@type": "RegistrationsBaseUrl/3.6.0"}]}):
        try:
            nuget._pick_registration_base(bad)
        except ValueError:
            pass
        else:
            assert False, f"expected ValueError for {bad!r}"


@test
def t_malformed_registration_is_error():
    reg_url = f"{REG_BASE}/broken.pkg/index.json"
    cases = [
        ("registration index is not a JSON object", "nope", {}),
        ("registration index has no items list", {}, {}),
        ("registration index has no items list", {"items": 3}, {}),
        ("neither inline items nor an @id", {"items": [{"items": None}]}, {}),
        ("did not return an object",
         {"items": [{"@id": f"{REG_BASE}/page.json", "items": None}]},
         {f"{REG_BASE}/page.json": "not-a-dict"}),
        ("registration page has no items list",
         {"items": [{"@id": f"{REG_BASE}/page.json", "items": None}]},
         {f"{REG_BASE}/page.json": {"items": 5}}),
        ("contains no catalog entries",
         {"items": [{"items": [{"leaf": "nope"}]}]}, {}),
        ("contains no catalog entries", {"items": []}, {}),
    ]
    for fragment, reg_doc, extra in cases:
        fake = FakeHttp({SERVICE_INDEX_URL: service_index(), reg_url: reg_doc,
                         **extra})
        nuget._http_get_json = fake
        r = nuget._provider_nuget_registry(
            {"source": "nuget_registry", "package": "Broken.Pkg",
             "version": "1.0.0"}, TODAY)
        assert r["status"] == "error", (fragment, r)
        assert "NuGet registry query failed" in r["message"], (fragment, r)
        assert fragment in r["message"], (fragment, r)


@test
def t_malformed_json_is_error():
    def fake_urlopen(req, timeout=None):
        return FakeResponse(b"{definitely not json")
    urllib.request.urlopen = fake_urlopen
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "X.Pkg", "version": "1.0.0"}, TODAY)
    assert r["status"] == "error", r
    assert "query failed" in r["message"], r["message"]


# ---------------------------------------------------------------------------
# Pagination: paged leaves are followed through the injected fetch
# ---------------------------------------------------------------------------

@test
def t_pagination_and_inline_leaves():
    page2_id = f"{REG_BASE}/paged.lib/page2.json"
    docs = {
        SERVICE_INDEX_URL: service_index(),
        f"{REG_BASE}/paged.lib/index.json": reg_index(
            page(leaf("1.0.0", "2020-01-01T12:00:00Z", pkg="Paged.Lib"),
                 leaf("1.1.0", "2021-02-03T12:00:00Z", pkg="Paged.Lib"),
                 page_id="page1"),
            {"@id": page2_id, "count": 2, "items": None},
        ),
        page2_id: page(leaf("2.0.0", "2024-06-01T00:00:00Z", pkg="Paged.Lib"),
                       leaf("2.1.0-beta", "2026-08-01T00:00:00Z", pkg="Paged.Lib"),
                       page_id="page2"),
    }
    fake = FakeHttp(docs)
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "paged.lib", "version": "2.0.0"},
        TODAY)
    assert r["status"] == "ok", r
    assert page2_id in fake.calls, "paged leaf page was not fetched"
    assert r["latest_patch"] == "2.0.0"
    assert r["in_use_release_date"] == "2024-06-01"
    assert r["version"] == "2.0.0"


@test
def t_collect_leaves_mixed_pages():
    calls = []

    def fetch_page(url):
        calls.append(url)
        return page(leaf("3.0.0", "2025-01-01T00:00:00Z", pkg="M"), page_id="p2")

    reg = reg_index(
        page(leaf("1.0.0", "2020-01-01T00:00:00Z", pkg="M"), page_id="p1"),
        {"@id": f"{REG_BASE}/page2", "count": 1, "items": None},
    )
    leaves = nuget._collect_leaves(reg, fetch_page, REG_BASE)
    assert calls == [f"{REG_BASE}/page2"]
    assert [l["version"] for l in leaves] == ["1.0.0", "3.0.0"]


@test
def t_pagination_guards():
    calls = []
    for page_url, fragment in (
            ("http://api.nuget.org/v3/registrations-test/page", "HTTPS"),
            ("https://example.invalid/page", "origin"),
            ("https://api.nuget.org/v3/other/page", "base"),
            ("https://api.nuget.org/v3/registrations-test/%2e%2e/private", "parent")):
        try:
            nuget._collect_leaves(
                {"items": [{"@id": page_url, "items": None}]},
                lambda url: calls.append(url) or {"items": []}, REG_BASE)
        except ValueError as exc:
            assert fragment in str(exc), (page_url, exc)
        else:
            assert False, f"unsafe page URL accepted: {page_url}"
    assert calls == []

    try:
        nuget._collect_leaves(
            {"items": [{"items": []}] * (nuget._NUGET_MAX_PAGES + 1)},
            lambda _url: {}, REG_BASE)
    except ValueError as exc:
        assert "page limit" in str(exc)
    else:
        assert False, "oversize registration page list was accepted"

    real_limit = nuget._NUGET_MAX_LEAVES
    try:
        nuget._NUGET_MAX_LEAVES = 1
        inline = reg_index(page(
            leaf("1.0.0", "2020-01-01T00:00:00Z", pkg="M"),
            leaf("2.0.0", "2021-01-01T00:00:00Z", pkg="M")))
        try:
            nuget._collect_leaves(inline, lambda _url: {}, REG_BASE)
        except ValueError as exc:
            assert "leaf limit" in str(exc)
        else:
            assert False, "oversize registration leaf list was accepted"
    finally:
        nuget._NUGET_MAX_LEAVES = real_limit


# ---------------------------------------------------------------------------
# gzip handling (fake urlopen, no network)
# ---------------------------------------------------------------------------

@test
def t_gzip_handling():
    doc = service_index()
    payload = gzip.compress(json.dumps(doc).encode("utf-8"))

    urllib.request.urlopen = lambda req, timeout=None: FakeResponse(
        payload, {"Content-Encoding": "gzip"})
    assert nuget._http_get_json(SERVICE_INDEX_URL) == doc

    urllib.request.urlopen = lambda req, timeout=None: FakeResponse(
        json.dumps(doc).encode("utf-8"), {})  # plain JSON unaffected
    assert nuget._http_get_json(SERVICE_INDEX_URL) == doc

    urllib.request.urlopen = lambda req, timeout=None: FakeResponse(
        b"not really gzip", {"Content-Encoding": "gzip"})
    try:
        nuget._http_get_json(SERVICE_INDEX_URL)
    except OSError:
        pass
    else:
        assert False, "corrupt gzip body should raise"

    original_limit = nuget._NUGET_BODY_BYTES
    try:
        nuget._NUGET_BODY_BYTES = 64
        urllib.request.urlopen = lambda req, timeout=None: FakeResponse(
            b"x" * 65, {})
        try:
            nuget._http_get_json(SERVICE_INDEX_URL)
        except ValueError as exc:
            assert "byte limit" in str(exc)
        else:
            assert False, "oversize compressed response should raise"

        expanded = gzip.compress(
            json.dumps({"value": "x" * 500}).encode("utf-8"))
        assert len(expanded) < 64
        urllib.request.urlopen = lambda req, timeout=None: FakeResponse(
            expanded, {"Content-Encoding": "gzip"})
        try:
            nuget._http_get_json(SERVICE_INDEX_URL)
        except ValueError as exc:
            assert "decompressed response" in str(exc)
        else:
            assert False, "gzip expansion should be bounded"
    finally:
        nuget._NUGET_BODY_BYTES = original_limit


@test
def t_gzip_end_to_end():
    docs = newtonsoft_docs()

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        return FakeResponse(gzip.compress(json.dumps(docs[url]).encode("utf-8")),
                            {"Content-Encoding": "gzip"})

    urllib.request.urlopen = fake_urlopen
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "Newtonsoft.Json",
         "version": "13.0.3"}, TODAY)
    assert r["status"] == "ok", r
    assert r["latest_patch"] == "13.0.3"


# ---------------------------------------------------------------------------
# Per-lookup cumulative budgets (requests / wire bytes / decoded bytes /
# retained entries)
# ---------------------------------------------------------------------------

def paged_docs(pkg, n_pages):
    """Service index + registration for *pkg* with *n_pages* paged pages."""
    reg_url = f"{REG_BASE}/{pkg.lower()}/index.json"
    pages = [{"@id": f"{REG_BASE}/{pkg.lower()}/page{i}.json", "count": 1,
              "items": None} for i in range(n_pages)]
    docs = {SERVICE_INDEX_URL: service_index(), reg_url: reg_index(*pages)}
    for i in range(n_pages):
        docs[f"{REG_BASE}/{pkg.lower()}/page{i}.json"] = page(
            leaf(f"1.{i}.0", "2020-01-01T00:00:00Z", pkg=pkg),
            page_id=f"page{i}")
    return docs


@test
def t_budget_requests_exhausted():
    docs = paged_docs("Budget.Pkg", 5)
    seen = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        return FakeResponse(json.dumps(docs[url]).encode("utf-8"), {})

    urllib.request.urlopen = fake_urlopen
    original = nuget._NUGET_MAX_REQUESTS
    try:
        nuget._NUGET_MAX_REQUESTS = 3  # index + registration + first page
        r = nuget._provider_nuget_registry(
            {"source": "nuget_registry", "package": "Budget.Pkg",
             "version": "1.0.0"}, TODAY)
    finally:
        nuget._NUGET_MAX_REQUESTS = original
    assert r["status"] == "error", r
    assert "budget exceeded" in r["message"], r["message"]
    assert "requests" in r["message"], r["message"]
    assert len(seen) == 3, seen  # fetching stopped at the cap
    assert ("pkg", "budget.pkg") not in nuget._NUGET_CACHE
    assert getattr(nuget._FETCH_BUDGET, "budget", None) is None


@test
def t_budget_bytes_exhausted():
    docs = newtonsoft_docs()

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        return FakeResponse(json.dumps(docs[url]).encode("utf-8"), {})

    urllib.request.urlopen = fake_urlopen
    original = nuget._NUGET_MAX_TOTAL_BYTES
    try:
        nuget._NUGET_MAX_TOTAL_BYTES = 10
        r = nuget._provider_nuget_registry(
            {"source": "nuget_registry", "package": "Newtonsoft.Json",
             "version": "13.0.3"}, TODAY)
    finally:
        nuget._NUGET_MAX_TOTAL_BYTES = original
    assert r["status"] == "error", r
    assert "budget exceeded" in r["message"], r["message"]
    assert "bytes" in r["message"], r["message"]
    assert ("pkg", "newtonsoft.json") not in nuget._NUGET_CACHE


@test
def t_budget_decoded_exhausted():
    # Cumulative DECODED bytes are bounded: many individually legal gzip
    # responses cannot aggregate unbounded decompression and parsing
    # work in one lookup. The cap here allows the first response and
    # rejects the second; no later fetch may occur, the thread-local
    # budget must be cleared, and no partial package cache entry may be
    # installed.
    docs = newtonsoft_docs()
    seen = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        return FakeResponse(
            gzip.compress(json.dumps(docs[url]).encode("utf-8")),
            {"Content-Encoding": "gzip"})

    urllib.request.urlopen = fake_urlopen
    original = nuget._NUGET_MAX_TOTAL_DECODED_BYTES
    first_decoded = len(json.dumps(docs[SERVICE_INDEX_URL]).encode("utf-8"))
    try:
        nuget._NUGET_MAX_TOTAL_DECODED_BYTES = first_decoded + 1
        r = nuget._provider_nuget_registry(
            {"source": "nuget_registry", "package": "Newtonsoft.Json",
             "version": "13.0.3"}, TODAY)
    finally:
        nuget._NUGET_MAX_TOTAL_DECODED_BYTES = original
    assert r["status"] == "error", r
    assert "budget exceeded" in r["message"], r["message"]
    assert "decoded" in r["message"], r["message"]
    assert len(seen) == 2, seen  # fetching stopped at the cap
    assert ("pkg", "newtonsoft.json") not in nuget._NUGET_CACHE
    assert getattr(nuget._FETCH_BUDGET, "budget", None) is None


@test
def t_budget_decoded_counts_plain_responses():
    # Non-gzip bodies count toward the decoded budget too (identity
    # encoding: wire bytes and decoded bytes are the same).
    docs = newtonsoft_docs()
    seen = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        return FakeResponse(json.dumps(docs[url]).encode("utf-8"), {})

    urllib.request.urlopen = fake_urlopen
    original = nuget._NUGET_MAX_TOTAL_DECODED_BYTES
    first_decoded = len(json.dumps(docs[SERVICE_INDEX_URL]).encode("utf-8"))
    try:
        nuget._NUGET_MAX_TOTAL_DECODED_BYTES = first_decoded + 1
        r = nuget._provider_nuget_registry(
            {"source": "nuget_registry", "package": "Newtonsoft.Json",
             "version": "13.0.3"}, TODAY)
    finally:
        nuget._NUGET_MAX_TOTAL_DECODED_BYTES = original
    assert r["status"] == "error", r
    assert "decoded" in r["message"], r["message"]
    assert len(seen) == 2, seen


@test
def t_budget_retained_entries_exhausted():
    # _NUGET_MAX_LEAVES is the retained-catalogEntry budget for one lookup;
    # exhausting it surfaces as a loud error result from the provider.
    fake = FakeHttp(newtonsoft_docs())  # five catalog entries
    nuget._http_get_json = fake
    original = nuget._NUGET_MAX_LEAVES
    try:
        nuget._NUGET_MAX_LEAVES = 4
        r = nuget._provider_nuget_registry(
            {"source": "nuget_registry", "package": "Newtonsoft.Json",
             "version": "13.0.3"}, TODAY)
    finally:
        nuget._NUGET_MAX_LEAVES = original
    assert r["status"] == "error", r
    assert "leaf limit" in r["message"], r["message"]


@test
def t_budget_not_installed_for_direct_fetches():
    # _http_get_json called outside a provider lookup carries no budget.
    doc = service_index()
    urllib.request.urlopen = lambda req, timeout=None: FakeResponse(
        json.dumps(doc).encode("utf-8"), {})
    assert nuget._http_get_json(SERVICE_INDEX_URL) == doc
    assert getattr(nuget._FETCH_BUDGET, "budget", None) is None


# ---------------------------------------------------------------------------
# Case-insensitive matching, case preservation, version normalization
# ---------------------------------------------------------------------------

@test
def t_case_insensitive_package_lookup():
    fake = FakeHttp(newtonsoft_docs())
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "NEWTONSOFT.JSON",
         "version": "13.0.3"}, TODAY)
    assert r["status"] == "ok", r
    assert f"{REG_BASE}/newtonsoft.json/index.json" in fake.calls
    assert r["product"] == "NEWTONSOFT.JSON"
    assert r["label"] == "NEWTONSOFT.JSON 13.0.3"


@test
def t_case_insensitive_version_and_preservation():
    fake = FakeHttp(newtonsoft_docs())
    nuget._http_get_json = fake
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "Newtonsoft.Json",
         "version": "13.1.0-BETA"}, TODAY)
    assert r["status"] == "ok", r
    assert r["version"] == "13.1.0-BETA"  # input casing preserved
    assert "Using prerelease 13.1.0-BETA published 2026-01-15" in r["message"], r["message"]
    assert "latest stable is 13.0.3" in r["message"], r["message"]


@test
def t_version_normalization_units():
    assert nuget._normalize_version("1.2") == "1.2.0"
    assert nuget._normalize_version("1.2.0.0") == "1.2.0"
    assert nuget._normalize_version("1.2.3.4") == "1.2.3.4"
    assert nuget._normalize_version("1.0.0-BETA1") == "1.0.0-beta1"
    assert nuget._normalize_version("1.0.0-Beta1+Meta") == "1.0.0-beta1"
    assert nuget._normalize_version(" 13.0.3 ") == "13.0.3"
    assert nuget._normalize_version(None) == ""
    assert nuget._normalize_version("") == ""
    leaves = [leaf("1.2.0", "2021-01-01T00:00:00Z"),
              leaf("1.3.0+build.9", "2022-01-01T00:00:00Z")]
    assert nuget._find_leaf(leaves, "1.2")["version"] == "1.2.0"
    assert nuget._find_leaf(leaves, "1.2.0.0")["version"] == "1.2.0"
    assert nuget._find_leaf(leaves, "1.3.0")["version"] == "1.3.0+build.9"
    assert nuget._find_leaf(leaves, "1.2.3") is None


# ---------------------------------------------------------------------------
# Conservative SemVer / prerelease handling
# ---------------------------------------------------------------------------

@test
def t_semver_ordering():
    order = ["1.9.9", "2.0.0-alpha", "2.0.0-alpha.1", "2.0.0-alpha.beta",
             "2.0.0-beta", "2.0.0-beta.2", "2.0.0-beta.11", "2.0.0-rc.1",
             "2.0.0", "2.0.0.1", "2.1.0"]
    keys = [nuget._semver_key(v) for v in order]
    assert all(k is not None for k in keys), keys
    assert keys == sorted(keys), list(zip(order, keys))
    for bad in ("", "abc", "1.x", "1.2.3.4.5", "v1.2.3", None):
        assert nuget._semver_key(bad) is None, bad
    assert nuget._is_prerelease("1.0.0-beta1")
    assert nuget._is_prerelease("1.0.0-BETA1+x")
    assert not nuget._is_prerelease("1.0.0")
    assert not nuget._is_prerelease("1.0.0+build")
    assert not nuget._is_prerelease("")


@test
def t_latest_stable_selection():
    leaves = [
        leaf("1.0.0", "2020-01-01T00:00:00Z"),
        leaf("1.9.0", "2023-06-01T12:34:56Z"),
        leaf("2.0.0", "2024-01-01T00:00:00Z", listed=False),
        leaf("2.1.0-beta", "2026-01-01T00:00:00Z"),
    ]
    ver, d = nuget._latest_stable(leaves)
    assert ver == "1.9.0", ver  # unlisted 2.0.0 and prerelease 2.1.0-beta never win
    assert str(d) == "2023-06-01"


@test
def t_no_stable_release():
    leaves = [leaf("0.1.0-alpha", "2020-01-01T00:00:00Z")]
    r = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "P.Lib",
         "version": "0.1.0-alpha"}, leaves, TODAY)
    assert r["status"] == "ok", r
    assert "no stable listed release" in r["message"], r["message"]
    assert r["latest_patch"] is None


# ---------------------------------------------------------------------------
# Deprecated / unlisted pins alert without claiming EOL dates
# ---------------------------------------------------------------------------

@test
def t_unlisted_pinned_alerts():
    leaves = [leaf("1.0.0", "2020-01-01T00:00:00Z", listed=False, pkg="U.Lib"),
              leaf("1.1.0", "2021-01-01T00:00:00Z", pkg="U.Lib")]
    r = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "U.Lib", "version": "1.0.0"},
        leaves, TODAY)
    assert r["status"] == "eol", r
    assert "unlisted" in r["message"], r["message"]
    assert r["eol_date"] is None and r["days_remaining"] is None


@test
def t_deprecated_pinned_alerts():
    dep = {"reasons": ["Legacy"], "message": "Use Other.Lib instead",
           "alternatePackage": {"id": "Other.Lib", "range": "(, )"}}
    leaves = [leaf("1.0.0", "2020-01-01T00:00:00Z", deprecation=dep, pkg="D.Lib"),
              leaf("1.1.0", "2021-01-01T00:00:00Z", pkg="D.Lib")]
    r = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "D.Lib", "version": "1.0.0"},
        leaves, TODAY)
    assert r["status"] == "eol", r
    assert "deprecated" in r["message"], r["message"]
    assert "Legacy" in r["message"], r["message"]
    assert "Other.Lib" in r["message"], r["message"]
    assert r["eol_date"] is None
    # deprecation on another version must not alert a clean pin
    r2 = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "D.Lib", "version": "1.1.0"},
        leaves, TODAY)
    assert r2["status"] == "ok", r2
    assert "deprecated" not in r2["message"], r2["message"]


@test
def t_deprecated_and_unlisted_combined():
    leaves = [leaf("1.0.0", "2020-01-01T00:00:00Z", listed=False,
                   deprecation={"reasons": ["Obsolete"]}, pkg="B.Lib")]
    r = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "B.Lib", "version": "1.0.0"},
        leaves, TODAY)
    assert r["status"] == "eol", r
    assert "deprecated" in r["message"] and "unlisted" in r["message"], r["message"]
    assert r["eol_date"] is None and r["days_remaining"] is None


# ---------------------------------------------------------------------------
# Published dates: parsing and the 1900-01-01 "unknown" marker
# ---------------------------------------------------------------------------

@test
def t_published_date_parsing_and_unknown_marker():
    assert nuget._parse_published("2024-02-29T10:00:00Z") == date(2024, 2, 29)
    assert nuget._parse_published("1900-01-01T00:00:00Z") is None
    for bad in (None, 5, "", "not-a-date", "2024-13-01T00:00:00Z"):
        assert nuget._parse_published(bad) is None, bad
    leaves = [leaf("1.0.0", "1900-01-01T00:00:00Z", pkg="Old.Lib")]
    r = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "Old.Lib", "version": "1.0.0"},
        leaves, TODAY)
    assert r["status"] == "ok", r
    assert r["in_use_release_date"] is None
    assert r["latest_patch"] == "1.0.0" and r["latest_patch_date"] is None
    assert "published" not in r["message"], r["message"]


# ---------------------------------------------------------------------------
# Caching and formatter integration
# ---------------------------------------------------------------------------

@test
def t_fetch_is_cached():
    fake = FakeHttp(newtonsoft_docs())
    nuget._http_get_json = fake
    entry = {"source": "nuget_registry", "package": "Newtonsoft.Json",
             "version": "13.0.3"}
    nuget._provider_nuget_registry(entry, TODAY)
    n = len(fake.calls)
    assert n >= 2  # service index + registration index
    nuget._provider_nuget_registry(entry, TODAY)
    assert len(fake.calls) == n, "second lookup hit the network"
    nuget._NUGET_CACHE[("pkg", "newtonsoft.json")] = [
        leaf("9.9.9", "2099-01-01T00:00:00Z")]
    r = nuget._provider_nuget_registry(
        {"source": "nuget_registry", "package": "Newtonsoft.Json",
         "version": "9.9.9"}, TODAY)
    assert r["latest_patch"] == "9.9.9"  # injected cache wins
    assert len(fake.calls) == n


@test
def t_report_rendering():
    leaves = [leaf("1.0.0", "2020-01-01T00:00:00Z", pkg="Old.Lib")]
    ok = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "Old.Lib", "version": "1.0.0",
         "label": "Old.Lib 1.0.0"}, leaves, date(2026, 8, 28))
    dep_leaves = [leaf("2.0.0", "2019-01-01T00:00:00Z",
                       deprecation={"reasons": ["Legacy"]}, pkg="Old.Lib")]
    flagged = nuget._nuget_result_from_leaves(
        {"source": "nuget_registry", "package": "Old.Lib", "version": "2.0.0",
         "label": "Old.Lib 2.0.0"}, dep_leaves, date(2026, 8, 28))
    text, has_alerts = format_report_text([ok, flagged], [30, 60, 90], TODAY)
    assert has_alerts
    assert "Old.Lib 2.0.0  [NuGet]" in text, text
    assert "deprecated" in text, text
    assert "Old.Lib 1.0.0" in text, text


# ---------------------------------------------------------------------------

def main():
    for fn in TESTS:
        nuget._NUGET_CACHE.clear()
        nuget._http_get_json = _REAL_GET
        urllib.request.urlopen = _REAL_URLOPEN
        try:
            fn()
        finally:
            nuget._NUGET_CACHE.clear()
            nuget._http_get_json = _REAL_GET
            urllib.request.urlopen = _REAL_URLOPEN
        print(f"ok {fn.__name__}")
    print("OK test_nuget_registry")


if __name__ == "__main__":
    main()
