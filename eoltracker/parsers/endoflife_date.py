"""endoflife.date provider (default source).

Community endoflife.date API; major-cycle EOL. This is the default source
for entries that don't declare one.
"""

import json
import urllib.request
import urllib.error
from datetime import date

from ..core import parse_date_field, _error_result, logger

EOL_API_BASE = "https://endoflife.date/api"


def fetch_all_cycles(product):
    """Fetch all release cycles for a product from endoflife.date.

    Returns the list sorted newest-first (as the API provides), or None on error.
    """
    url = f"{EOL_API_BASE}/{product}.json"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.error("API error for %s: %s %s", product, exc.code, exc.reason)
        return None
    except Exception as exc:
        logger.error("Failed to fetch %s: %s", product, exc)
        return None


def _provider_endoflife_date(entry, today):
    """Look up EOL data from endoflife.date for a product entry.

    Fetches all cycles once per product to extract:
      - tracked cycle info (EOL, latest patch, patch release date)
      - latest available cycle (newest major/minor version)
    """
    product = entry["product"]
    version = entry["version"]
    label = entry.get("label", f"{product} {version}")

    cycles = fetch_all_cycles(product)
    if cycles is None:
        return _error_result(entry, "Failed to fetch data from API")

    info = None
    for c in cycles:
        if str(c.get("cycle")) == str(version):
            info = c
            break

    if info is None:
        available = [c.get("cycle") for c in cycles[:6]]
        return _error_result(entry, f"Cycle '{version}' not found. Available: {available}")

    eol = parse_date_field(info.get("eol"))
    support = parse_date_field(info.get("support"))
    latest_patch = info.get("latest", "unknown")
    latest_patch_date = info.get("latestReleaseDate")
    lts = info.get("lts", False)

    newest = cycles[0]
    latest_cycle = newest.get("cycle")
    latest_cycle_version = newest.get("latest", latest_cycle)
    latest_cycle_release_date = newest.get("releaseDate")
    on_latest_cycle = str(latest_cycle) == str(version)

    result = {
        "label": label,
        "product": product,
        "version": version,
        "lts": lts,
        "latest_patch": latest_patch,
        "latest_patch_date": latest_patch_date,
        "latest_cycle": latest_cycle,
        "latest_cycle_version": latest_cycle_version,
        "latest_cycle_release_date": latest_cycle_release_date,
        "on_latest_cycle": on_latest_cycle,
        "eol_date": str(eol) if isinstance(eol, date) else None,
        "support_date": str(support) if isinstance(support, date) else None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "endoflife_date",
    }

    if eol is True:
        result["status"] = "eol"
        result["message"] = "Already end of life (no specific date)"
    elif eol is False:
        result["status"] = "ok"
        result["message"] = "No EOL date announced - still supported"
    elif isinstance(eol, date):
        days = (eol - today).days
        result["days_remaining"] = days
        if days < 0:
            result["status"] = "eol"
            result["message"] = f"EOL since {eol} ({abs(days)} days ago)"
        elif days == 0:
            result["status"] = "eol"
            result["message"] = f"Reaches end of life TODAY ({eol})"
        else:
            result["status"] = "approaching"
            result["message"] = f"EOL on {eol} ({days} days remaining)"
    else:
        result["status"] = "unknown"
        result["message"] = f"Could not determine EOL status (raw value: {info.get('eol')})"

    if isinstance(support, date):
        support_days = (support - today).days
        result["support_days_remaining"] = support_days
        if support_days < 0:
            result["support_message"] = f"Active support ended {support} ({abs(support_days)} days ago)"
        else:
            result["support_message"] = f"Active support until {support} ({support_days} days remaining)"

    return result


SOURCE = "endoflife_date"
LABEL = "endoflife.date"
provider = _provider_endoflife_date


def url_for(r):
    product = r.get("product") or ""
    return f"https://endoflife.date/{product}" if product else None
