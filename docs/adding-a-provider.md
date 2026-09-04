# Adding a data-source provider (parser)

This tracker maps each product to a **provider** — a function that fetches EOL/lifecycle
data from one source and returns a normalized result. Providers follow a uniform contract
and are wired together through a small registry, so adding a source is a localized change.

See `AGENTS.md` for the architecture overview. This is the step-by-step how-to.

## The contract

```python
def _provider_<name>(entry, today) -> dict
```

- `entry` — one object from the config's `products` array (its `source` field routed it
  here). It carries whatever fields your provider needs (`product`, `version`, `package`,
  `group`/`artifact`, etc.) plus an optional `label`.
- `today` — a `datetime.date` (injected so tests are deterministic).
- **Returns** a result dict. `_validate_provider_result` in
  `eoltracker/parsers/__init__.py` is a hard gate: a return value that breaks any rule
  below is discarded and the entry becomes an error row, so a partial dict is never
  rendered. Exactly these nine keys are required (`_RESULT_KEYS`) — set the ones your
  source can't supply to `None`:

```python
{
    "label": label,               # display name (default it if entry has none)
    "product": <handle>,          # what url_for needs (slug / package / "g:a")
    "version": version,
    "status": "eol" | "approaching" | "ok" | "unknown" | "error" | "untracked",
    "message": "<human sentence>",
    "eol_date": "YYYY-MM-DD" or None,
    "days_remaining": int or None,
    "latest_patch": <str> or None,
    "source": "<name>",
}
```

What the gate enforces:

- the return value is a `dict`; all nine keys above are present (extra keys are allowed);
- `label`, `message`, and `source` are **non-empty strings**;
- `source` equals the source the entry was dispatched to;
- `status` is one of exactly six values (`_RESULT_STATUSES`): `eol`, `approaching`, `ok`,
  `error`, `unknown`, `untracked`;
- `days_remaining` is an `int` or `None` (a `bool` is rejected).

Nothing else is type-checked: `product`, `version`, `eol_date`, and `latest_patch` are
passed through as-is, so keep them strings or `None` for the formatters. One optional
key is rendered when present: every existing provider also sets `latest_patch_date`
(a `"YYYY-MM-DD"` string or `None`), which `eoltracker/report.py` reads with `.get`
and shows beside the latest patch.

On any failure, return `_error_result(entry, "<why>")` and set `result["source"] =
"<name>"` (and `result["product"]` if you want the source link to resolve).

## Skeleton (copy, rename, fill in)

Create a new file `eoltracker/parsers/<name>.py`. Import shared helpers from `..core`; the
module is auto-discovered at import time via the `SOURCE` / `provider` attributes at the
bottom (no registry edits anywhere else).

```python
"""<Name> provider — <one line on the source and what it reports>."""

import urllib.request

from ..core import _error_result, logger, read_response_bytes

_FOO_URL = "https://example.com/eol-data"
_FOO_CACHE = {}          # cache the fetch — a run checks many products against one source
_FOO_MIN_ROWS = 3        # tune to the smallest legitimate upstream data set


def _fetch_foo():
    """Fetch + return the source data, or raise. Cached per process."""
    if "data" in _FOO_CACHE:
        return _FOO_CACHE["data"]
    req = urllib.request.Request(_FOO_URL, headers={"User-Agent": "EOL-Tracker/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = read_response_bytes(resp).decode("utf-8", "replace")
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
    result = {  # all nine required keys — see "The contract" above
        "label": label, "product": "foo", "version": version, "source": "foo",
        "eol_date": str(eol) if eol else None, "days_remaining": None,
        "latest_patch": None, "latest_patch_date": None,   # optional, rendered if set
        "status": "unknown", "message": "pending",         # overwritten below
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
            result["status"] = "approaching"
            # _categorise demotes a *dated far-future* approaching to 'ok';
            # an undated approaching (days_remaining None) always stays an alert
            result["message"] = f"EOL {eol} ({days} days remaining)"
    return result
```

## Register (module attributes — auto-discovered)

At the **bottom of the same file**, declare the registration contract. `eoltracker/parsers/__init__.py`
scans the package at import time and wires these into `PROVIDERS`, `SOURCE_LABELS`, and the
`source_url_for` dispatch — no other file changes, no registry to edit.

```python
SOURCE = "foo"                 # entry["source"] value that routes here
LABEL  = "Foo docs"            # human label shown in reports (defaults to SOURCE)
provider = _provider_foo       # the (entry, today) -> dict function


def url_for(r):                # optional — the clickable upstream link for a result
    return _FOO_URL
```

Report rendering treats the return value of `url_for` and every result field
as untrusted. A source link is rendered only when it **validates as an HTTPS
URL** (exactly the `https` scheme, a host present, no whitespace or control
characters); anything else degrades to plain escaped label text, so fixed
HTTPS endpoints in module constants are preferred. Every dynamic value
(labels, versions, dates, messages, notes) is HTML-escaped at the report
boundary — providers must never embed markup in result strings, plain text
only.

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

`untracked` is the worked example (added for the `manual` provider). A new status needs
(all in `eoltracker/report.py`): `_categorise` (new bucket in the returned dict), **both**
`format_report_text` and `format_report_html` (a rendering block / banner decision), and for
HTML also `_STATUS_COLOURS["<status>"]` and a branch in `_status_label`.

## Test it (network-free)

No test framework — write a scratch `python` script that imports the module and injects
data so no network is hit:

```python
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))   # run from the repository root

from eoltracker.parsers import foo, PROVIDERS, SOURCE_LABELS, source_url_for

# 1) parse helper against synthetic raw text
assert foo._parse_foo(SAMPLE)["5.8"]["eol"] == date(2027, 6, 30)
# 2) provider against injected cache (no fetch)
foo._FOO_CACHE["data"] = {"5.8": {"eol": date(2027, 6, 30)}}
r = foo._provider_foo({"source": "foo", "version": "5.8"}, date(2026, 7, 24))
assert r["status"] == "approaching" and r["eol_date"] == "2027-06-30"
# 3) registration wired (auto-discovered — no registry edits)
assert "foo" in PROVIDERS and SOURCE_LABELS["foo"] and source_url_for({"source": "foo"})
```

Then one live smoke run: `python lambda_function.py <a config using source: foo>`.

## Document the provider

Add it to `eol_config_generation_prompt.md` so the canonical
`manage-eol-config` skill knows to use it: a row in the providers table, an
entry-shape example, and a line in the mapping decision order.

## Repairing a broken provider

Scraper providers fail loudly by design: when an upstream page drifts, the report
grows `error` rows ("source may have changed", a canary assertion, a row-count
floor) instead of silently wrong dates. When that happens:

1. **Reproduce.** Run the failing provider directly (network on) and read the
   actual error — canary failure, row-count floor, missing header, HTTP error:

   ```python
   import sys
   from datetime import date
   from pathlib import Path
   sys.path.insert(0, str(Path.cwd()))   # run from the repository root
   from eoltracker.parsers import tyk_lifecycle as mod   # the broken module
   print(mod.provider({"source": mod.SOURCE, "version": "5.8"}, date.today()))
   ```

   (A provider's module filename may differ from its `source` string — e.g.
   source `aws_rds_scrape` lives in `eoltracker/parsers/aws_rds.py`; list
   `eoltracker/parsers/` to find the right module.)

2. **Fetch the raw source** the provider parses (the URL constant at the top of
   the module) and save it to a scratch file. Compare its structure against what
   the pure `_parse_*` helper expects: headers, column order, section headings,
   markdown vs rendered HTML.
3. **Fix the pure parse helper** against the saved raw text. Keep it pure — no
   network — so the fix is testable offline.
4. **Keep the defensive checks.** Never delete a canary or lower a row-count
   floor just to silence the error. Update the canary only when the upstream
   fact legitimately changed (e.g. a version's EOL date was revised upstream),
   and say so in the commit message.
5. **Re-run the module's network-free test script** (synthetic raw text +
   injected cache, as in "Test it (network-free)" above), adding a regression
   case built from the new page shape.
6. **One live smoke run:** `python lambda_function.py <config using the source>`
   — confirm the `error` rows are gone.

If the upstream source is gone for good (page deleted, product discontinued),
migrate the affected config entries to another provider or `manual`, and update
`eol_config_generation_prompt.md` so config generation stops recommending the
dead source.

## Worked example: `tyk_lifecycle`

The Tyk provider (in `eoltracker/parsers/tyk_lifecycle.py`) is a complete reference for a
bespoke-docs scraper: it fetches the Tyk LTS table from the tyk-docs GitHub markdown, parses
`Version | … | Completely Unsupported From` (deriving EOL = last day of the month *before*
"Completely Unsupported From"), validates ≥2 dated rows, caches, and maps
Dashboard/MDCB/Pump onto the Gateway LTS `major.minor`. See `_parse_tyk_table`,
`_scrape_tyk_lifecycle`, and `_provider_tyk_lifecycle`.
