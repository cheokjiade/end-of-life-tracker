"""Network-free tests for the go_proxy provider.

Standalone assertion script (repo convention: no framework). Synthetic proxy
documents are injected into the module's fetch cache, and ``_fetch_proxy`` is
replaced with a sentinel that fails the run on any unexpected network access.
"""

import io
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from eoltracker.parsers import PROVIDERS, SOURCE_LABELS, check_product, source_url_for
from eoltracker.parsers import go_proxy as gp

TODAY = date(2026, 8, 28)
MOD = "golang.org/x/net"
V = "v0.44.0"
LATEST_NET = '{"Version": "v0.44.0", "Time": "2025-08-19T18:00:00Z"}'
INFO_V044 = '{"Version": "v0.44.0", "Time": "2025-08-19T18:00:00Z"}'


def _no_network(url):
    raise AssertionError(f"unexpected network access in test: {url}")


def use_proxy(listed=None, latest=None, infos=None, mods=None, module=MOD):
    """Reset the fetch cache and inject synthetic proxy documents.

    listed/latest/mods are raw endpoint bodies (or None for a 404);
    infos maps version -> raw .info body (or None for a 404).
    Returns the escaped module path the cache is keyed under.
    """
    gp._GO_CACHE.clear()
    gp._fetch_proxy = _no_network
    esc = gp._escape_module_path(module)
    gp._GO_CACHE[("list", esc)] = listed
    gp._GO_CACHE[("latest", esc)] = latest
    for ver, body in (infos or {}).items():
        gp._GO_CACHE[("info", esc, ver)] = body
    for ver, body in (mods or {}).items():
        gp._GO_CACHE[("mod", esc, ver)] = body
    return esc


# ---------------------------------------------------------------------------
# 1) Protocol path escaping (uppercase -> !lowercase)
# ---------------------------------------------------------------------------

assert gp._escape_module_path("github.com/Azure/azure-sdk-for-go") == \
    "github.com/!azure/azure-sdk-for-go"
assert gp._escape_module_path("Example.COM/Path") == "!example.!c!o!m/!path"
assert gp._escape_module_path("golang.org/x/net") == "golang.org/x/net"
assert gp._escape_module_path("go.uber.org/Zap") == "go.uber.org/!zap"

assert gp._module_url("!my!module.example/path", "@v/list") == \
    "https://proxy.golang.org/!my!module.example/path/@v/list"
assert gp._info_url("m", "v1.0.0+incompatible") == \
    "https://proxy.golang.org/m/@v/v1.0.0+incompatible.info"
guarded = gp._info_url("m", "../evil")
assert "%2F" in guarded and "/evil" not in guarded, guarded

# ---------------------------------------------------------------------------
# 2) Pure document parsers
# ---------------------------------------------------------------------------

assert gp._parse_version_list("v1.0.0\n\n  v1.1.0  \nv2.0.0+incompatible\n") == \
    ["v1.0.0", "v1.1.0", "v2.0.0+incompatible"]
assert gp._parse_version_list(None) == []
assert gp._parse_version_list("") == []

doc = gp._parse_info_doc('{"Version": "v1.2.3", "Time": "2024-05-13T16:20:06Z"}')
assert doc == {"version": "v1.2.3", "time": date(2024, 5, 13)}, doc
doc = gp._parse_info_doc('{"Version": "v1.2.3", "Time": "2024-05-13T16:20:06+02:00"}')
assert doc == {"version": "v1.2.3", "time": date(2024, 5, 13)}, doc
assert gp._parse_info_doc('{"Version": "v1.2.3"}') == {"version": "v1.2.3", "time": None}
assert gp._parse_info_doc('{"Time": "2024-05-13T00:00:00Z"}') == {"version": None, "time": date(2024, 5, 13)}
assert gp._parse_info_doc('"just a string"') is None      # JSON, not an object
assert gp._parse_info_doc("not json at all") is None      # malformed
assert gp._parse_info_doc(None) is None

assert gp._parse_rfc3339_date("2024-05-13") == date(2024, 5, 13)
assert gp._parse_rfc3339_date("2024-13-40T00:00:00Z") is None   # invalid date
assert gp._parse_rfc3339_date("bogus") is None
assert gp._parse_rfc3339_date(None) is None

# ---------------------------------------------------------------------------
# 3) Semantic version comparison, stability, pseudo-versions
# ---------------------------------------------------------------------------

assert gp._compare_semver("v1.2.3", "v1.2.4") == -1
assert gp._compare_semver("v1.10.0", "v1.9.0") == 1
assert gp._compare_semver("v2.0.0", "v1.99.99") == 1
assert gp._compare_semver("v1.2.3", "v1.2.3") == 0
assert gp._compare_semver("v1.2.3+meta", "v1.2.3") == 0    # build metadata ignored
assert gp._compare_semver("v1.2.3-rc.1", "v1.2.3") == -1   # prerelease < release
assert gp._compare_semver("v1.2.3-rc.1", "v1.2.3-rc.2") == -1
assert gp._compare_semver("v1.2.3-1", "v1.2.3-alpha") == -1  # numeric < alphanumeric
assert gp._compare_semver("v1.2.3-alpha", "v1.2.3-alpha.1") == -1
assert gp._compare_semver("v1.2", "v1.2.3") is None
assert gp._compare_semver("1.2.3", "v1.2.2") is None

assert gp._is_pseudo_version("v0.0.0-20250101000000-abcdefabcdef")
assert gp._is_pseudo_version("v1.2.3-0.20250101000000-abcdefabcdef")
assert gp._is_pseudo_version("v1.2.4-0.20250101000000-abcdefabcdef")
assert not gp._is_pseudo_version("v1.2.3")
assert not gp._is_pseudo_version("v1.2.3-rc.1")

assert gp._is_stable("v1.2.3")
assert gp._is_stable("v1.2.3+incompatible")
assert not gp._is_stable("v1.2.3-rc.1")
assert not gp._is_stable("v0.0.0-20250101000000-abcdefabcdef")
assert not gp._is_stable("garbage")

assert gp._latest_stable(["v1.9.0", "v1.10.0", "v1.11.0-rc.1"]) == "v1.10.0"
assert gp._latest_stable(["v1.1.0-rc.1", "v0.0.0-20250101000000-abcdefabcdef"]) is None
assert gp._latest_stable([]) is None

# ---------------------------------------------------------------------------
# 4) Retraction parsing and coverage (pure go.mod transforms)
# ---------------------------------------------------------------------------

GOMOD = """module example.com/m

go 1.21

retract (
    v1.0.0
    [v1.1.0, v1.2.0] // security regression, use v1.3.0
    v1.3.0-rc.1
)

retract v1.9.0 // superseded
"""
retracts = gp._parse_retractions(GOMOD)
assert retracts == [
    ("v1.0.0", None, ""),
    ("v1.1.0", "v1.2.0", "security regression, use v1.3.0"),
    ("v1.3.0-rc.1", None, ""),
    ("v1.9.0", None, "superseded"),
], retracts

assert gp._parse_retractions('module example.com/m\n\ngo 1.21\n') == []
assert gp._parse_retractions("retract \"v1.5.0\"") == [("v1.5.0", None, "")]
assert gp._parse_retractions("") == []
assert gp._parse_retractions(None) == []
# A word that merely starts with 'retract' is not a directive.
assert gp._parse_retractions("retracted v1.0.0\n") == []
# Versions that are not valid semver are skipped, never guessed.
assert gp._parse_retractions("retract v1.2\n") == []

assert gp._is_retracted("v1.0.0", retracts)
assert gp._is_retracted("v1.1.0", retracts)          # range low edge
assert gp._is_retracted("v1.1.5", retracts)          # range interior
assert gp._is_retracted("v1.2.0", retracts)          # range high edge
assert gp._is_retracted("v1.9.0", retracts)
assert not gp._is_retracted("v1.0.1", retracts)
assert not gp._is_retracted("v1.2.1", retracts)
assert not gp._is_retracted("v2.0.0", retracts)
assert gp._is_retracted("v1.2.3+incompatible", [("v1.2.3", None, "")])
assert not gp._is_retracted("garbage", [("v1.0.0", None, "")])
assert not gp._is_retracted("v1.2.3", [])

# ---------------------------------------------------------------------------
# 5) Pure result composition
# ---------------------------------------------------------------------------

r = gp._go_result_from_data(
    {"source": "go_proxy", "module": MOD, "version": "v0.43.0"},
    {"module": MOD, "version": "v0.43.0",
     "listed": ["v0.43.0", "v0.44.0"],
     "pinned_doc": {"version": "v0.43.0", "time": date(2025, 6, 2)},
     "latest_doc": {"version": "v0.44.0", "time": date(2025, 8, 19)},
     "latest_date": date(2025, 8, 19),
     "mod_text": "",
     "retraction_note": None},
    TODAY)
assert r["status"] == "ok", r
assert r["message"] == \
    "In use v0.43.0 (2025-06-02); latest stable v0.44.0 (2025-08-19, 78 days newer)", r["message"]
assert r["eol_date"] is None and r["days_remaining"] is None
assert r["latest_patch"] == "v0.44.0" and r["latest_patch_date"] == "2025-08-19"
assert r["in_use_release_date"] == "2025-06-02"
assert r["on_latest_cycle"] is False
assert r["retracted"] is False
assert r["source"] == "go_proxy"

# Full normalized key set, matching the registry-provider contract.
for key in ("label", "product", "version", "lts", "status", "message",
            "in_use_release_date", "latest_patch", "latest_patch_date",
            "latest_cycle", "latest_cycle_version", "latest_cycle_release_date",
            "on_latest_cycle", "eol_date", "support_date", "days_remaining",
            "support_days_remaining", "source"):
    assert key in r, key

# ---------------------------------------------------------------------------
# 6) Provider against injected caches (no network: _fetch_proxy is a sentinel)
# ---------------------------------------------------------------------------

# On the latest stable release.
esc = use_proxy(listed="v0.43.0\nv0.44.0\n", latest=LATEST_NET,
                infos={V: INFO_V044, "v0.43.0": '{"Version": "v0.43.0", "Time": "2025-06-02T09:30:00Z"}'},
                mods={V: "module golang.org/x/net\n\ngo 1.23\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": V}, TODAY)
assert r["status"] == "ok", r
assert r["message"] == "On latest stable release (v0.44.0) published 2025-08-19", r["message"]
assert r["on_latest_cycle"] is True
assert r["retracted"] is False and r["retraction_note"] is None
assert r["eol_date"] is None and r["days_remaining"] is None

# Behind latest: pinned timestamp, latest stable, days newer.
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v0.43.0"}, TODAY)
assert r["status"] == "ok" and not r["on_latest_cycle"]
assert r["message"] == \
    "In use v0.43.0 (2025-06-02); latest stable v0.44.0 (2025-08-19, 78 days newer)", r["message"]

# @latest is authoritative when the proxy has not listed the newest version yet;
# the chosen latest comes with its timestamp from @latest itself (no extra fetch).
esc = use_proxy(listed="v1.0.0\n", latest='{"Version": "v1.1.0", "Time": "2026-01-15T10:00:00Z"}',
                infos={"v1.0.0": '{"Version": "v1.0.0", "Time": "2025-01-01T00:00:00Z"}'},
                mods={"v1.1.0": "module golang.org/x/net\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.0.0"}, TODAY)
assert r["latest_patch"] == "v1.1.0" and r["latest_patch_date"] == "2026-01-15", r

# The reverse: @latest behind the listed versions -> latest .info is consulted.
esc = use_proxy(listed="v1.0.0\nv1.2.0\n",
                latest='{"Version": "v1.1.0", "Time": "2026-01-15T10:00:00Z"}',
                infos={"v1.0.0": '{"Version": "v1.0.0", "Time": "2025-01-01T00:00:00Z"}',
                       "v1.2.0": '{"Version": "v1.2.0", "Time": "2026-02-20T00:00:00Z"}'},
                mods={"v1.2.0": "module golang.org/x/net\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.0.0"}, TODAY)
assert r["latest_patch"] == "v1.2.0" and r["latest_patch_date"] == "2026-02-20", r

# A prerelease @latest never outranks a listed stable release.
esc = use_proxy(listed="v1.0.0\nv1.1.0-rc.1\n",
                latest='{"Version": "v1.1.0-rc.1", "Time": "2026-03-01T00:00:00Z"}',
                infos={"v1.0.0": '{"Version": "v1.0.0", "Time": "2025-01-01T00:00:00Z"}'},
                mods={"v1.0.0": "module golang.org/x/net\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.0.0"}, TODAY)
assert r["latest_patch"] == "v1.0.0" and r["on_latest_cycle"] is True, r

# Retraction alert: the proxy-served go.mod of the latest stable retracts the pin.
UPSTREAM = "github.com/Azure/azure-sdk-for-go"
esc = use_proxy(listed="v0.6.0\nv0.7.0\nv1.0.0\n",
                latest='{"Version": "v1.0.0", "Time": "2025-09-01T00:00:00Z"}',
                infos={"v0.7.0": '{"Version": "v0.7.0", "Time": "2024-03-01T00:00:00Z"}',
                       "v1.0.0": '{"Version": "v1.0.0", "Time": "2025-09-01T00:00:00Z"}'},
                mods={"v1.0.0": "module github.com/Azure/azure-sdk-for-go\n\n"
                                "go 1.21\n\n"
                                "retract [v0.5.0, v0.7.0] // superseded by the v1.0.0 track\n"},
                module=UPSTREAM)
assert esc == "github.com/!azure/azure-sdk-for-go"
r = gp._provider_go_proxy({"source": "go_proxy", "module": UPSTREAM, "version": "v0.7.0"}, TODAY)
assert r["status"] == "eol", r
assert r["retracted"] is True
assert r["retraction_reason"] == "superseded by the v1.0.0 track"
assert r["message"] == ("v0.7.0 is retracted by upstream: superseded by the v1.0.0 track; "
                        "latest stable is v1.0.0 (2025-09-01)"), r["message"]
assert r["eol_date"] is None and r["days_remaining"] is None   # no EOL claimed
assert r["product"] == UPSTREAM                                # original case kept
assert source_url_for(r) == "https://pkg.go.dev/github.com/Azure/azure-sdk-for-go@v0.7.0"

# Retraction explicitly not reported for a pseudo-version pin (conservative).
PSEUDO = "v0.0.0-20250101000000-abcdefabcdef"
esc = use_proxy(listed="v0.43.0\nv0.44.0\n", latest=LATEST_NET,
                infos={PSEUDO: '{"Version": "%s", "Time": "2025-01-01T00:00:00Z"}' % PSEUDO})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": PSEUDO}, TODAY)
assert r["status"] == "ok", r
assert r["retracted"] is None
assert r["retraction_note"] == "retraction not determined: pseudo-version pin"
assert r["message"] == ("Pseudo-version v0.0.0-20250101000000-abcdefabcdef (published 2025-01-01); "
                        "latest stable is v0.44.0 (2025-08-19) "
                        "(retraction not determined: pseudo-version pin)"), r["message"]

# Prerelease pin: factual, never claimed to be behind the stable release.
esc = use_proxy(listed="v1.0.0\nv1.3.0-rc.1\n",
                latest='{"Version": "v1.0.0", "Time": "2024-11-01T00:00:00Z"}',
                infos={"v1.3.0-rc.1": '{"Version": "v1.3.0-rc.1", "Time": "2026-01-05T00:00:00Z"}',
                       "v1.0.0": '{"Version": "v1.0.0", "Time": "2024-11-01T00:00:00Z"}'},
                mods={"v1.0.0": "module golang.org/x/net\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.3.0-rc.1"}, TODAY)
assert r["status"] == "ok" and r["on_latest_cycle"] is False, r
assert r["message"] == ("Prerelease v1.3.0-rc.1 (published 2026-01-05); "
                        "latest stable is v1.0.0 (2024-11-01)"), r["message"]
assert r["retracted"] is False

# No stable tagged release anywhere: latest stays None, retraction undetermined.
esc = use_proxy(listed="v1.1.0-rc.1\n",
                latest='{"Version": "v0.0.0-20250601000000-abcdefabcdef", "Time": "2025-06-01T00:00:00Z"}',
                infos={"v1.0.0": '{"Version": "v1.0.0", "Time": "2024-01-01T00:00:00Z"}'})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.0.0"}, TODAY)
assert r["status"] == "ok" and r["latest_patch"] is None, r
assert r["retracted"] is None
assert r["retraction_note"] == "retraction not determined: no stable release to consult"
assert r["message"] == ("No stable tagged release on Go module proxy; in use v1.0.0 (2024-01-01) "
                        "(retraction not determined: no stable release to consult)"), r["message"]

# Pinned version absent from the proxy: informational note, private-build hint.
esc = use_proxy(listed="v0.43.0\nv0.44.0\n", latest=LATEST_NET,
                infos={"v0.99.0": None, V: INFO_V044},
                mods={V: "module golang.org/x/net\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v0.99.0"}, TODAY)
assert r["status"] == "unknown", r
assert r["in_use_release_date"] is None
assert r["message"] == ("Version v0.99.0 not found on Go module proxy (private build?); "
                        "latest stable is v0.44.0 (2025-08-19)"), r["message"]

# No pinned version in the entry: report the latest stable only.
esc = use_proxy(listed="v0.43.0\nv0.44.0\n", latest=LATEST_NET)
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD}, TODAY)
assert r["status"] == "ok" and r["version"] == ""
assert r["message"] == "No pinned version provided; latest stable is v0.44.0 (2025-08-19)", r["message"]
assert r["retracted"] is None and r["retraction_note"] is None

# Version normalization: bare and capital-V forms gain the canonical 'v' prefix.
esc = use_proxy(listed="v0.43.0\nv0.44.0\n", latest=LATEST_NET, infos={V: INFO_V044},
                mods={V: "module golang.org/x/net\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "0.44.0"}, TODAY)
assert r["version"] == "v0.44.0" and r["status"] == "ok", r
esc = use_proxy(listed="v1.2.3\n", latest='{"Version": "v1.2.3", "Time": "2026-01-01T00:00:00Z"}',
                infos={"v1.2.3": '{"Version": "v1.2.3", "Time": "2026-01-01T00:00:00Z"}'},
                mods={"v1.2.3": "module m.example\n"}, module="m.example")
r = gp._provider_go_proxy({"source": "go_proxy", "module": "m.example", "version": "V1.2.3"}, TODAY)
assert r["version"] == "v1.2.3" and r["on_latest_cycle"] is True, r

# A malformed @latest document is tolerated when @v/list still resolves.
esc = use_proxy(listed="v1.0.0\nv1.2.0\n", latest="not json",
                infos={"v1.0.0": '{"Version": "v1.0.0", "Time": "2025-01-01T00:00:00Z"}',
                       "v1.2.0": '{"Version": "v1.2.0", "Time": "2026-02-20T00:00:00Z"}'},
                mods={"v1.2.0": "module golang.org/x/net\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.0.0"}, TODAY)
assert r["status"] == "ok" and r["latest_patch"] == "v1.2.0", r

# ---------------------------------------------------------------------------
# 7) Defensive errors (loud, uniform shapes)
# ---------------------------------------------------------------------------

# Module unknown to the proxy: both endpoints 404.
esc = use_proxy(listed=None, latest=None)
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": V}, TODAY)
assert r["status"] == "error", r
assert r["message"] == "Module 'golang.org/x/net' not found on Go module proxy", r["message"]
assert r["source"] == "go_proxy" and r["product"] == MOD

# Missing module field.
r = gp._provider_go_proxy({"source": "go_proxy", "version": V}, TODAY)
assert r["status"] == "error" and "require 'module'" in r["message"], r

# Refused module paths: whitespace and '@' would corrupt proxy endpoint URLs.
r = gp._provider_go_proxy({"source": "go_proxy", "module": "example.com/two words"}, TODAY)
assert r["status"] == "error" and "whitespace" in r["message"], r
r = gp._provider_go_proxy({"source": "go_proxy", "module": "example.com/foo@v1"}, TODAY)
assert r["status"] == "error" and "'@'" in r["message"], r

# Malformed .info document for the pin.
esc = use_proxy(listed="v1.0.0\n", latest='{"Version": "v1.0.0", "Time": "2026-01-01T00:00:00Z"}',
                infos={"v1.0.0": "<html>error page</html>"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.0.0"}, TODAY)
assert r["status"] == "error" and "Malformed .info document" in r["message"], r

# The proxy lists a version whose .info is missing: treated as upstream drift.
esc = use_proxy(listed="v1.0.0\nv1.2.0\n",
                latest='{"Version": "v1.1.0", "Time": "2026-01-15T10:00:00Z"}',
                infos={"v1.0.0": '{"Version": "v1.0.0", "Time": "2025-01-01T00:00:00Z"}',
                       "v1.2.0": None})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.0.0"}, TODAY)
assert r["status"] == "error" and "source may have changed" in r["message"], r

# go.mod for the latest stable missing: retraction cannot be established.
esc = use_proxy(listed="v1.0.0\n", latest='{"Version": "v1.0.0", "Time": "2026-01-01T00:00:00Z"}',
                infos={"v1.0.0": '{"Version": "v1.0.0", "Time": "2026-01-01T00:00:00Z"}'},
                mods={"v1.0.0": None})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": "v1.0.0"}, TODAY)
assert r["status"] == "error" and "retraction status cannot be established" in r["message"], r

# Fetch-layer behaviour: 410 is a negative-cache miss, 500 raises.
def _http_error(code):
    def _raise(url):
        raise urllib.error.HTTPError(url, code, "oops", {}, io.BytesIO(b""))
    return _raise

gp._GO_CACHE.clear()
gp._fetch_proxy = _http_error(410)
assert gp._fetch_cached(("list", "gone"), "https://proxy.golang.org/gone/@v/list") is None
assert gp._fetch_cached(("list", "gone"), "anything") is None    # negative-cached

gp._GO_CACHE.clear()
gp._fetch_proxy = _http_error(500)
try:
    gp._fetch_cached(("list", "boom"), "https://proxy.golang.org/boom/@v/list")
    raise AssertionError("HTTP 500 should propagate")
except urllib.error.HTTPError:
    pass

# A transport-level failure becomes a loud error row.
gp._GO_CACHE.clear()
def _transport_error(url):
    raise urllib.error.URLError("connection refused")
gp._fetch_proxy = _transport_error
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": V}, TODAY)
assert r["status"] == "error" and "Go module proxy query failed" in r["message"], r

# HTTP 500 likewise, and never silently as not-found.
gp._GO_CACHE.clear()
gp._fetch_proxy = _http_error(500)
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": V}, TODAY)
assert r["status"] == "error" and "Go module proxy query failed" in r["message"], r

# ---------------------------------------------------------------------------
# 8) Registration, labels, and upstream URLs
# ---------------------------------------------------------------------------

assert "go_proxy" in PROVIDERS
assert PROVIDERS["go_proxy"] is gp.provider
assert SOURCE_LABELS["go_proxy"] == gp.LABEL == "Go proxy"
assert gp.SOURCE == "go_proxy"
assert source_url_for({"source": "go_proxy", "product": MOD, "version": V}) == \
    "https://pkg.go.dev/golang.org/x/net@v0.44.0"
assert source_url_for({"source": "go_proxy", "product": MOD}) == \
    "https://pkg.go.dev/golang.org/x/net"
assert source_url_for({"source": "go_proxy"}) is None

# Dispatch through the shared registry.
esc = use_proxy(listed="v0.43.0\nv0.44.0\n", latest=LATEST_NET, infos={V: INFO_V044},
                mods={V: "module golang.org/x/net\n"})
r = check_product({"source": "go_proxy", "module": MOD, "version": V,
                   "policy_note": "module age is not a lifecycle"}, TODAY)
assert r["source"] == "go_proxy" and r["status"] == "ok", r
assert r["policy_note"] == "module age is not a lifecycle"

# Label passthrough and default.
esc = use_proxy(listed="v0.44.0\n", latest=LATEST_NET, infos={V: INFO_V044},
                mods={V: "module golang.org/x/net\n"})
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": V,
                           "label": "x/net pin"}, TODAY)
assert r["label"] == "x/net pin", r
r = gp._provider_go_proxy({"source": "go_proxy", "module": MOD, "version": V}, TODAY)
assert r["label"] == "golang.org/x/net v0.44.0", r

print("OK test_go_proxy")
