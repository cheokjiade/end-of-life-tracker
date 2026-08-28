"""Network-free tests for the pypi_registry provider.

Standalone assertion script (no framework). All upstream documents are
synthetic and injected into the module cache; urllib is patched during
provider calls to fail loudly if any network is attempted.

Run from the repository root:  python tests/test_pypi_registry.py
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

from eoltracker.parsers import (
    pypi_registry as pypi,
    PROVIDERS,
    SOURCE_LABELS,
    source_url_for,
    check_product,
)
from eoltracker.report import format_report_text, format_report_html

TODAY = date(2026, 8, 28)
THRESHOLDS = [30, 60, 90]

REQUIRED_KEYS = {
    "label", "product", "version", "lts", "status", "message",
    "in_use_release_date", "latest_patch", "latest_patch_date",
    "latest_cycle", "latest_cycle_version", "latest_cycle_release_date",
    "on_latest_cycle", "eol_date", "support_date", "days_remaining",
    "support_days_remaining", "source",
}


def _files(*specs):
    """Build a release's file dicts from (upload_time, yanked, yanked_reason)."""
    return [{"upload_time": ut, "yanked": yk, "yanked_reason": yr,
             "filename": f"f{i}.whl"} for i, (ut, yk, yr) in enumerate(specs)]


def _doc(releases, info_version="2.33.0"):
    return {"info": {"name": "pkg", "version": info_version,
                     "yanked": False, "yanked_reason": None},
            "releases": releases}


def _run(entry, doc):
    """Call the provider with doc injected into the cache and network cut."""
    pypi._PYPI_CACHE[pypi._normalize_package(entry.get("package", ""))] = doc
    real_open = pypi.urllib.request.urlopen

    def _no_network(*_a, **_k):
        raise AssertionError("network attempted during network-free test")

    pypi.urllib.request.urlopen = _no_network
    try:
        return pypi._provider_pypi_registry(entry, TODAY)
    finally:
        pypi.urllib.request.urlopen = real_open


# --- 1) PEP 440-lite ordering and prerelease classification (pure) ---------
assert pypi._version_key("1.0") == pypi._version_key("1.0.0")
assert pypi._version_key("1.10.0") > pypi._version_key("1.9.9")
assert pypi._version_key("1.0rc1") < pypi._version_key("1.0")
assert pypi._version_key("1.0.dev1") < pypi._version_key("1.0a1")
assert pypi._version_key("2.0") > pypi._version_key("1.0.post9")
assert pypi._version_key("1!1.0") > pypi._version_key("2.0")
assert pypi._version_key("V2.0") == pypi._version_key("2.0.0")
assert pypi._version_key("1.0+local") is None
assert pypi._version_key("not-a-version") is None

assert pypi._is_prerelease("2.32.4") is False
assert pypi._is_prerelease("2.33.0rc1") is True
assert pypi._is_prerelease("1.0a1") is True
assert pypi._is_prerelease("1.0b2") is True
assert pypi._is_prerelease("1.0.dev1") is True
assert pypi._is_prerelease("1.0.post1") is False
assert pypi._is_prerelease("banana") is None

assert pypi._normalize_package("Requests.Too") == "requests-too"
assert pypi._normalize_package("  Dask ") == "dask"

# --- 2) Latest-stable selection (pure) --------------------------------------
DOC = _doc({
    "2.30.0": _files(("2023-05-22T10:00:00", False, None),
                     ("2023-05-22T09:00:00", False, None)),
    "2.31.0": _files(("2024-01-02T09:00:00", False, None)),
    "2.32.4": _files(("2024-05-29T14:30:00", False, None)),
    "2.33.0rc1": _files(("2025-03-01T00:00:00", False, None)),
    "2.33.0": _files(("2025-06-01T08:00:00", False, None)),
})
lv, ld = pypi._latest_stable(DOC["releases"])
assert lv == "2.33.0" and ld == date(2025, 6, 1), (lv, ld)

yanked_latest = _doc({
    "1.0": _files(("2020-01-01T00:00:00", False, None)),
    "1.1": _files(("2021-01-01T00:00:00", True, "oops"),
                  ("2021-01-02T00:00:00", True, None)),
    "1.2b1": _files(("2022-01-01T00:00:00", False, None)),
})
assert pypi._latest_stable(yanked_latest["releases"]) == ("1.0", date(2020, 1, 1))

assert pypi._latest_stable({"only": _files(("2020-01-01T00:00:00", True, None))}) == (None, None)

# Mixed yanked files: release is NOT yanked (pip-consistent, all-files rule).
assert pypi._release_yanked([{"yanked": True, "yanked_reason": "a"},
                             {"yanked": False, "yanked_reason": None}]) == (False, None)
assert pypi._release_yanked([{"yanked": True, "yanked_reason": "a"}]) == (True, "a")
assert pypi._release_yanked([{"yanked": True, "yanked_reason": ""}]) == (True, None)

# --- 3) Happy path: pinned release, full normalized shape -------------------
r = _run({"source": "pypi_registry", "package": "requests",
          "version": "2.32.4", "label": "Requests 2.32.4"}, DOC)
assert REQUIRED_KEYS <= set(r), sorted(REQUIRED_KEYS - set(r))
assert r["status"] == "ok"
assert r["label"] == "Requests 2.32.4"
assert r["in_use_release_date"] == "2024-05-29"
assert r["latest_patch"] == "2.33.0" and r["latest_patch_date"] == "2025-06-01"
assert r["on_latest_cycle"] is False
assert r["eol_date"] is None and r["days_remaining"] is None
assert r["source"] == "pypi_registry" and r["product"] == "requests"
assert "368d newer" in r["message"], r["message"]

# Default label derives from package + version.
r_dflt = _run({"source": "pypi_registry", "package": "requests", "version": "2.32.4"}, DOC)
assert r_dflt["label"] == "requests 2.32.4"

# On the latest stable.
r_on = _run({"source": "pypi_registry", "package": "requests", "version": "2.33.0"}, DOC)
assert r_on["status"] == "ok" and r_on["on_latest_cycle"] is True
assert "On latest stable PyPI release (2.33.0)" in r_on["message"]

# No version pinned: informational, no error.
r_nv = _run({"source": "pypi_registry", "package": "requests"}, DOC)
assert r_nv["status"] == "ok"
assert "latest stable is 2.33.0 (2025-06-01)" in r_nv["message"]

# Canonical-form fallback: unique match accepts 'v2.0' for release '2.0.0'.
r_canon = _run({"source": "pypi_registry", "package": "canon", "version": "v2.0"},
               _doc({"2.0.0": _files(("2024-02-02T00:00:00", False, None))}))
assert r_canon["status"] == "ok"
assert r_canon["in_use_release_date"] == "2024-02-02"

# Ambiguous canonical match is never guessed: loud error instead.
r_amb = _run({"source": "pypi_registry", "package": "amb", "version": "2.000"},
             _doc({"2.0.0": _files(("2024-02-02T00:00:00", False, None)),
                   "2.00": _files(("2024-02-03T00:00:00", False, None))}))
assert r_amb["status"] == "error" and "not present on PyPI" in r_amb["message"]

# Unparsable pinned version: exact-match only, flagged conservatively.
r_odd = _run({"source": "pypi_registry", "package": "odd", "version": "1.0.unknown"},
             _doc({"1.0.unknown": _files(("2023-01-01T00:00:00", False, None))}))
assert r_odd["status"] == "ok"
assert "unrecognized version format" in r_odd["message"]

# --- 4) Prerelease handling: informational, never "on latest" ---------------
r_pre = _run({"source": "pypi_registry", "package": "requests", "version": "2.33.0rc1"}, DOC)
assert r_pre["status"] == "ok"
assert "prerelease" in r_pre["message"]
assert r_pre["in_use_release_date"] == "2025-03-01"
assert r_pre["on_latest_cycle"] is False
assert r_pre["latest_patch"] == "2.33.0"  # prerelease never claimed stable

# --- 5) Yanked pinned release is an alert (no EOL date claimed) -------------
ydoc = _doc({
    "1.0": _files(("2020-01-01T00:00:00", True, "security issue")),
    "1.1": _files(("2021-01-01T00:00:00", False, None)),
}, info_version="1.1")
r_y = _run({"source": "pypi_registry", "package": "yankedpkg", "version": "1.0"}, ydoc)
assert r_y["status"] == "eol"
assert "yanked" in r_y["message"] and "security issue" in r_y["message"]
assert r_y["eol_date"] is None and r_y["days_remaining"] is None
assert r_y["in_use_release_date"] == "2020-01-01"

r_ynr = _run({"source": "pypi_registry", "package": "nr", "version": "2.0"},
             _doc({"2.0": _files(("2022-01-01T00:00:00", True, None))}))
assert r_ynr["status"] == "eol" and "no reason given" in r_ynr["message"]

# --- 6) Missing package / absent version / empty release: errors ------------
r_404 = _run({"source": "pypi_registry", "package": "ghost", "version": "1.0"}, None)
assert r_404["status"] == "error"
assert "not found on PyPI" in r_404["message"] and r_404["product"] == "ghost"

r_v = _run({"source": "pypi_registry", "package": "requests", "version": "9.9.9"}, DOC)
assert r_v["status"] == "error"
assert "not present on PyPI" in r_v["message"] and "2.33.0" in r_v["message"]

r_empty = _run({"source": "pypi_registry", "package": "emptyrel", "version": "1.0"},
               _doc({"1.0": []}))
assert r_empty["status"] == "error" and "no uploaded files" in r_empty["message"]

r_bad = _run({"source": "pypi_registry", "package": "badfiles", "version": "1.0"},
             _doc({"1.0": ["garbage"]}))
assert r_bad["status"] == "error" and "malformed" in r_bad["message"]

# Malformed document shape fails loudly with an error row, not a crash.
r_mal = _run({"source": "pypi_registry", "package": "mal"}, {"nope": True})
assert r_mal["status"] == "error" and "malformed" in r_mal["message"]

# Fetch layer: rejects malformed docs, caches 404s as None, caches good docs.
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def read(self):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, *_a):
        return False


def _swap_urlopen(fn):
    class _Swap:
        def __enter__(self):
            self._real = pypi.urllib.request.urlopen
            pypi.urllib.request.urlopen = fn
        def __exit__(self, *_a):
            pypi.urllib.request.urlopen = self._real
    return _Swap()


pypi._PYPI_CACHE.pop("fetchmal", None)
with _swap_urlopen(lambda req, timeout=None: _FakeResp(b'{"info": "not-an-object"}')):
    try:
        pypi._fetch_pypi_doc("fetchmal")
        raise AssertionError("malformed fetch document not rejected")
    except ValueError:
        pass

pypi._PYPI_CACHE.pop("fetch404", None)
def _raise_404(req, timeout=None):
    raise pypi.urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
with _swap_urlopen(_raise_404):
    assert pypi._fetch_pypi_doc("fetch404") is None
assert pypi._PYPI_CACHE["fetch404"] is None  # 404 is cached, not re-fetched

pypi._PYPI_CACHE.pop("fetchok", None)
_good = b'{"info": {"name": "x", "version": "1.0"}, "releases": {"1.0": []}}'
with _swap_urlopen(lambda req, timeout=None: _FakeResp(_good)):
    doc_ok = pypi._fetch_pypi_doc("fetchok")
assert doc_ok["info"]["version"] == "1.0"
assert pypi._PYPI_CACHE["fetchok"] is doc_ok  # good doc cached for the run

# Missing required entry field.
r_nofield = pypi._provider_pypi_registry({"source": "pypi_registry"}, TODAY)
assert r_nofield["status"] == "error" and "require 'package'" in r_nofield["message"]

# --- 7) Registration, labels, and upstream URL -------------------------------
assert "pypi_registry" in PROVIDERS
assert PROVIDERS["pypi_registry"] is pypi._provider_pypi_registry
assert SOURCE_LABELS["pypi_registry"] == "PyPI"
assert source_url_for({"source": "pypi_registry", "product": "requests"}) \
    == "https://pypi.org/project/requests/"
assert source_url_for({"source": "pypi_registry"}) is None

# --- 8) Dispatch + formatters consume the provider unchanged ----------------
pypi._PYPI_CACHE[pypi._normalize_package("requests")] = DOC
r_disp = check_product({"source": "pypi_registry", "package": "requests",
                        "version": "2.32.4"}, TODAY)
assert r_disp["status"] == "ok"
text, has_alerts = format_report_text([r_disp], THRESHOLDS, TODAY)
assert not has_alerts
assert "requests 2.32.4" in text and "[PyPI]" in text
assert "In use: 2.32.4 (released 2024-05-29)" in text
assert "Latest patch: 2.33.0 (released 2025-06-01)" in text

r_y2 = _run({"source": "pypi_registry", "package": "yankedpkg", "version": "1.0"}, ydoc)
text_y, alerts_y = format_report_text([r_y2], THRESHOLDS, TODAY)
assert alerts_y is True
assert "ALREADY END OF LIFE" in text_y and "security issue" in text_y

html_y, alerts_h = format_report_html([r_y2], THRESHOLDS, TODAY)
assert alerts_h is True
assert "PyPI" in html_y and "https://pypi.org/project/yankedpkg/" in html_y
assert "1 product(s) past end of life" in html_y

# Cache reuse: a second run hits the injected cache (urlopen stays patched out).
pypi._PYPI_CACHE[pypi._normalize_package("cachedpkg")] = DOC
r_c1 = pypi._provider_pypi_registry(
    {"source": "pypi_registry", "package": "cachedpkg", "version": "2.33.0"}, TODAY)
r_c2 = pypi._provider_pypi_registry(
    {"source": "pypi_registry", "package": "cachedpkg", "version": "2.32.4"}, TODAY)
assert r_c1["status"] == r_c2["status"] == "ok"
assert pypi._PYPI_CACHE[pypi._normalize_package("cachedpkg")] is DOC

print("OK tests/test_pypi_registry")
