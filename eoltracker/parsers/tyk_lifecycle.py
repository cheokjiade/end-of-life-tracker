"""Tyk lifecycle scraper.

Tyk isn't on endoflife.date, but publishes an LTS support table in its docs.
We parse the source markdown from the public tyk-docs GitHub repo (more stable
than the rendered page). Table columns:
  Version | Full Support Window | Maintenance Support Window | Completely Unsupported From
Effective EOL = last day of the month BEFORE "Completely Unsupported From"
(5.8 "unsupported from July 2027" -> supported through 2027-06-30). Dashboard /
MDCB / Pump track the Gateway LTS line, so their entries use the Gateway
major.minor (e.g. 5.8).
"""

import calendar
import re
import urllib.request
from datetime import date, datetime

from ..core import _error_result, logger, read_response_bytes

_TYK_LTS_URL = (
    "https://raw.githubusercontent.com/TykTechnologies/tyk-docs/main/"
    "developer-support/release-types/long-term-support.mdx"
)
_TYK_CACHE = {}
_TYK_REQUIRED_COLS = {"Version", "Completely Unsupported From"}
_TYK_MIN_ROWS = 2
# Value canary: the current LTS whose EOL must always parse to this exact date.
# Catches column-meaning drift / off-by-one date math that a row-count check can't
# ("Completely Unsupported From" July 2028 -> EOL 2028-06-30). Update when this LTS
# eventually drops off the table (the scraper will fail loudly, as intended).
_TYK_CANARY = ("5.13", date(2028, 6, 30))


def _parse_tyk_unsupported(text):
    """'July 2027' (Completely Unsupported From) -> last day of the PRIOR month
    (2027-06-30), the effective EOL. Returns date or None."""
    text = (text or "").strip()
    parsed = None
    for fmt in ("%B %Y", "%b %Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    try:
        py, pm = (parsed.year - 1, 12) if parsed.month == 1 else (parsed.year, parsed.month - 1)
        return date(py, pm, calendar.monthrange(py, pm)[1])
    except ValueError:
        return None  # implausible cell (e.g. 'January 0001' year underflow) -> not a date


def _parse_tyk_table(md_text):
    """Parse the LTS markdown table -> {major.minor: {eol, unsupported_raw}}."""
    versions = {}
    cols = None
    for line in md_text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if cols:
                break  # table block ended
            continue
        # split on unescaped pipes only, then unescape — a literal '\|' inside a
        # cell must not create a phantom column and shift the date read (re-review finding)
        cells = [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", s.strip("|"))]
        if cols is None:
            if _TYK_REQUIRED_COLS.issubset(cells):
                cols = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue  # separator row
        if len(cells) < len(cols):
            continue
        row = dict(zip(cols, cells))
        m = re.match(r"(\d+\.\d+)", row.get("Version", ""))
        if not m:
            continue
        versions[m.group(1)] = {
            "eol": _parse_tyk_unsupported(row.get("Completely Unsupported From", "")),
            "unsupported_raw": row.get("Completely Unsupported From", ""),
        }
    if cols is None:
        raise ValueError(
            f"Tyk LTS table header not found (need columns {sorted(_TYK_REQUIRED_COLS)}); "
            f"docs structure may have changed."
        )
    return versions


def _validate_tyk(versions):
    """Raise ValueError if the parsed Tyk table fails the row-floor or value canary.

    The canary defends against the failure a row count can't see: the headers stay
    but the column's meaning (or the date math) drifts, so cells still parse as
    dates but produce silently-wrong EOLs.
    """
    dated = [v for v, info in versions.items() if info["eol"]]
    if len(dated) < _TYK_MIN_ROWS:
        raise ValueError(
            f"Tyk LTS table parsed only {len(dated)} dated versions "
            f"(expected >= {_TYK_MIN_ROWS}); docs structure may have changed. Parsed: {versions}"
        )
    canary_v, canary_eol = _TYK_CANARY
    got = versions.get(canary_v, {}).get("eol")
    if got != canary_eol:
        raise ValueError(
            f"Tyk canary failed: '{canary_v}' expected EOL {canary_eol}, got {got}. "
            f"Column meaning, date math, or the LTS table may have drifted."
        )


def _scrape_tyk_lifecycle():
    """Fetch + parse the Tyk LTS table. Raises ValueError on structural drift."""
    if "data" in _TYK_CACHE:
        return _TYK_CACHE["data"]
    req = urllib.request.Request(_TYK_LTS_URL, headers={"User-Agent": "EOL-Tracker/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        md = read_response_bytes(resp).decode("utf-8", "replace")
    versions = _parse_tyk_table(md)
    _validate_tyk(versions)
    logger.info("Tyk LTS scraped: %s", {v: str(i["eol"]) for v, i in versions.items()})
    _TYK_CACHE["data"] = versions
    return versions


def _provider_tyk_lifecycle(entry, today):
    """Look up a Tyk LTS version's EOL from the Tyk docs support table."""
    raw_version = str(entry.get("version", ""))
    m = re.match(r"(\d+\.\d+)", raw_version)
    version = m.group(1) if m else raw_version
    label = entry.get("label", f"Tyk {raw_version}")

    try:
        versions = _scrape_tyk_lifecycle()
    except Exception as exc:
        logger.error("Tyk scraper failed for %s: %s", raw_version, exc)
        result = _error_result(entry, f"Tyk scraper failed: {exc}")
        result["source"] = "tyk_lifecycle"
        return result

    info = versions.get(version)
    if info is None:
        available = sorted(versions.keys(), reverse=True)
        result = _error_result(entry, f"Tyk LTS version '{version}' not in support table. Available: {available}")
        result["source"] = "tyk_lifecycle"
        return result

    eol = info["eol"]
    result = {
        "label": label,
        "product": "tyk",
        "version": raw_version,
        "lts": True,
        "latest_patch": None,
        "latest_patch_date": None,
        "latest_cycle": None,
        "latest_cycle_version": None,
        "latest_cycle_release_date": None,
        "on_latest_cycle": False,
        "eol_date": str(eol) if eol else None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "tyk_lifecycle",
    }
    if eol is None:
        result["status"] = "unknown"
        result["message"] = f"Could not parse Tyk EOL date (raw: '{info['unsupported_raw']}')"
    else:
        days = (eol - today).days
        result["days_remaining"] = days
        if days < 0:
            result["status"] = "eol"
            result["message"] = f"Tyk {version} LTS support ended {eol} ({abs(days)} days ago)"
        elif days == 0:
            result["status"] = "eol"
            result["message"] = f"Tyk {version} LTS support ends TODAY ({eol})"
        else:
            result["status"] = "approaching"
            result["message"] = f"Tyk {version} LTS support ends {eol} ({days} days remaining)"
    return result


SOURCE = "tyk_lifecycle"
LABEL = "Tyk docs"
provider = _provider_tyk_lifecycle


def url_for(r):
    return "https://tyk.io/docs/developer-support/release-types/long-term-support"
