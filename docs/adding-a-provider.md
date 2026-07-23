# Adding a data-source provider (parser)

This tracker maps each product to a **provider** — a function that fetches EOL/lifecycle
data from one source and returns a normalized result. Providers follow a uniform contract
and are wired together through a small registry, so adding a source is a localized change.

See `CLAUDE.md` for the architecture overview. This is the step-by-step how-to.

## The contract

```python
def _provider_<name>(entry, today) -> dict
```

- `entry` — one object from the config's `products` array (its `source` field routed it
  here). It carries whatever fields your provider needs (`product`, `version`, `package`,
  `group`/`artifact`, etc.) plus an optional `label`.
- `today` — a `datetime.date` (injected so tests are deterministic).
- **Returns** a result dict. Build the common keys the formatters read; set the ones your
  source can't supply to `None`. Minimum viable set:

```python
{
    "label": label,               # display name (default it if entry has none)
    "product": <handle>,          # what _source_url_for needs (slug / package / "g:a")
    "version": version,
    "status": "eol" | "approaching" | "ok" | "unknown" | "error" | "untracked",
    "message": "<human sentence>",
    "eol_date": "YYYY-MM-DD" or None,
    "days_remaining": int or None,
    "latest_patch": <str> or None,
    "source": "<name>",
    # …plus the other keys the formatters read; copy an existing provider's dict.
}
```

On any failure, return `_error_result(entry, "<why>")` and set `result["source"] =
"<name>"` (and `result["product"]` if you want the source link to resolve).

## Skeleton (copy, rename, fill in)

Drop this under a `# ---` banner in `lambda_function.py`, next to the other providers.

```python
# ---------------------------------------------------------------------------
# <Name> provider
#
# <one line on the source and what it reports>
# ---------------------------------------------------------------------------

_FOO_URL = "https://example.com/eol-data"
_FOO_CACHE = {}          # cache the fetch — a run checks many products against one source


def _fetch_foo():
    """Fetch + return the source data, or raise. Cached per process."""
    if "data" in _FOO_CACHE:
        return _FOO_CACHE["data"]
    req = urllib.request.Request(_FOO_URL, headers={"User-Agent": "EOL-Tracker/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "replace")
    data = _parse_foo(raw)                       # keep parsing pure + testable
    if len(data) < _FOO_MIN_ROWS:                # loud failure on structural drift
        raise ValueError(f"Foo parsed only {len(data)} rows; source may have changed.")
    _FOO_CACHE["data"] = data
    return data


def _parse_foo(raw):
    """Pure: raw text -> {version: {eol, ...}}. No network — unit-test this directly."""
    ...
    return {}


def _provider_foo(entry, today):
    version = str(entry.get("version", ""))
    label = entry.get("label", f"Foo {version}")
    try:
        data = _fetch_foo()
    except Exception as exc:
        logger.error("Foo provider failed for %s: %s", version, exc)
        result = _error_result(entry, f"Foo source failed: {exc}")
        result["source"] = "foo"
        return result

    info = data.get(version)
    if info is None:
        result = _error_result(entry, f"Version '{version}' not found. Available: {sorted(data)}")
        result["source"] = "foo"
        return result

    eol = info["eol"]                            # a datetime.date, or None
    result = {  # copy the full key set from _provider_maven_central / _provider_endoflife_date
        "label": label, "product": "foo", "version": version, "source": "foo",
        "eol_date": str(eol) if eol else None, "days_remaining": None,
        "latest_patch": None, "latest_patch_date": None,
        # …the rest of the standard keys, set to None where N/A…
    }
    if eol is None:
        result["status"] = "unknown"
        result["message"] = "Could not determine EOL"
    else:
        days = (eol - today).days
        result["days_remaining"] = days
        if days <= 0:
            result["status"] = "eol"
            result["message"] = f"EOL {eol} ({abs(days)} days ago)" if days < 0 else f"EOL today ({eol})"
        else:
            result["status"] = "approaching"      # _categorise demotes to 'ok' if beyond threshold
            result["message"] = f"EOL {eol} ({days} days remaining)"
    return result
```

## Register in three places

```python
PROVIDERS = {
    ...
    "foo": _provider_foo,                 # 1. dispatch
}

_SOURCE_LABELS = {
    ...
    "foo": "Foo docs",                    # 2. human label in reports
}

def _source_url_for(r):
    ...
    if src == "foo":
        return _FOO_URL                   # 3. the clickable upstream link
    return None
```

## Defensive parsing (required for scrapers)

Web sources drift silently. Match the bar set by `_scrape_aws_rds_calendar` and
`_scrape_jackson_lifecycle`:

- **Required-header / structure check** — confirm the columns/section you expect exist;
  raise if not.
- **Row-count floor** — raise if you parsed fewer rows than a known minimum (a truncated
  or restructured page).
- **Canary** — assert a hardcoded known fact still parses (e.g. a specific version → a
  specific date/phase). If the canary breaks, fail the whole source rather than emit
  wrong data. A loud `error` row in the report is the goal; a silently-wrong EOL date is
  the failure mode to prevent.

Prefer the **most stable parse target**: e.g. `tyk_lifecycle` parses the docs page's
**markdown source in GitHub**, not the rendered HTML, because markdown is far less likely
to shift than a 1.6 MB rendered page.

## If you add a new status value

`untracked` is the worked example (added for the `manual` provider). A new status needs:
`_categorise` (new bucket + return tuple), **both** `format_report_text` and
`format_report_html` (unpack line + a rendering block), and for HTML also
`_STATUS_COLOURS["<status>"]` and a branch in `_status_label`.

## Test it (network-free)

No test framework — write a scratch `python` script that imports the module and injects
data so no network is hit:

```python
import importlib.util, os
from datetime import date
spec = importlib.util.spec_from_file_location("lf", r"E:\Git\endoflife\lambda_function.py")
lf = importlib.util.module_from_spec(spec); spec.loader.exec_module(lf)   # __main__ guard prevents the CLI

# 1) parse helper against synthetic raw text
assert lf._parse_foo(SAMPLE)["5.8"]["eol"] == date(2027, 6, 30)
# 2) provider against injected cache (no fetch)
lf._FOO_CACHE["data"] = {"5.8": {"eol": date(2027, 6, 30)}}
r = lf._provider_foo({"source": "foo", "version": "5.8"}, date(2026, 7, 24))
assert r["status"] == "approaching" and r["eol_date"] == "2027-06-30"
# 3) registration wired
assert "foo" in lf.PROVIDERS and lf._SOURCE_LABELS["foo"] and lf._source_url_for({"source": "foo"})
```

Then one live smoke run: `python lambda_function.py <a config using source: foo>`.

## Document the provider

Add it to `eol_config_generation_prompt.md` so config generation (and the
`eol-config-extractor` agent) knows to use it: a row in the providers table, an entry-shape
example, and a line in the mapping decision order.

## Worked example: `tyk_lifecycle`

The Tyk provider (in `lambda_function.py`) is a complete reference for a bespoke-docs
scraper: it fetches the Tyk LTS table from the tyk-docs GitHub markdown, parses
`Version | … | Completely Unsupported From` (deriving EOL = last day of the month *before*
"Completely Unsupported From"), validates ≥2 dated rows, caches, and maps
Dashboard/MDCB/Pump onto the Gateway LTS `major.minor`. See `_parse_tyk_table`,
`_scrape_tyk_lifecycle`, and `_provider_tyk_lifecycle`.
