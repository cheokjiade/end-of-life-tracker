"""Network-free tests for provider cache reuse (issue #11).

The HTML-only runner checks every config inside one process specifically so
module-level provider caches are reused across configs. These tests pin the
endoflife.date process-lifetime memo: a seeded product is served from cache
with urlopen wired to fail if ever touched, and failed lookups are never
cached (a transient error must not poison later retries within the same run).
"""

import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.parsers import endoflife_date as eold


# ---------------------------------------------------------------------------
# 1. A successful fetch is memoized; repeat lookups hit no network
# ---------------------------------------------------------------------------

_KEY = "cache-check-product"
eold._CYCLES_CACHE[_KEY] = [{"cycle": "3.13", "eol": "2029-10-01"}]

_real_urlopen = eold.urllib.request.urlopen


def _forbid_network(*args, **kwargs):
    raise AssertionError("network access attempted despite warm cache")


try:
    eold.urllib.request.urlopen = _forbid_network
    first = eold.fetch_all_cycles(_KEY)
    second = eold.fetch_all_cycles(_KEY)
finally:
    eold.urllib.request.urlopen = _real_urlopen
    del eold._CYCLES_CACHE[_KEY]

assert first == second == [{"cycle": "3.13", "eol": "2029-10-01"}]
assert _KEY not in eold._CYCLES_CACHE, "test must clean up after itself"

print("OK endoflife_date fetch reuses the process-lifetime cache")


# ---------------------------------------------------------------------------
# 2. Failed lookups are not cached (HTTPError path)
# ---------------------------------------------------------------------------

def _raise_http_error(*args, **kwargs):
    raise urllib.error.HTTPError("https://endoflife.date/api/x.json", 500,
                                 "Internal Server Error", hdrs=None, fp=None)


_uncached = "transient-failure-product"
try:
    eold.urllib.request.urlopen = _raise_http_error
    result = eold.fetch_all_cycles(_uncached)
finally:
    eold.urllib.request.urlopen = _real_urlopen

assert result is None
assert _uncached not in eold._CYCLES_CACHE, "failures must not be cached"

print("OK failed endoflife_date lookups are not cached")

print("OK test_provider_cache")
