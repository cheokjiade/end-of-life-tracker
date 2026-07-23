"""Manual provider — a component with no automated EOL source.

With an 'eol_date' it behaves like a hand-entered endoflife.date row (real
countdown). Without one it is reported as 'untracked' so it stays visible in
the report instead of being silently dropped.
"""

from datetime import date

from ..core import parse_date_field


def _provider_manual(entry, today):
    """A component with no automated EOL source.

    With an 'eol_date' it behaves like a hand-entered endoflife.date row
    (real countdown). Without one it is reported as 'untracked' so it stays
    visible in the report instead of being silently dropped.
    """
    label = entry.get("label", "Manual entry")
    note = entry.get("note")
    eol = parse_date_field(entry.get("eol_date"))

    result = {
        "label": label,
        "product": None,
        "version": entry.get("version"),
        "lts": False,
        "latest_patch": entry.get("latest"),
        "latest_patch_date": None,
        "latest_cycle": None,
        "latest_cycle_version": None,
        "latest_cycle_release_date": None,
        "on_latest_cycle": False,
        "eol_date": str(eol) if isinstance(eol, date) else None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "reference_url": entry.get("reference_url"),
        "source": "manual",
    }

    if isinstance(eol, date):
        days = (eol - today).days
        result["days_remaining"] = days
        prefix = f"{note} - " if note else ""
        if days < 0:
            result["status"] = "eol"
            result["message"] = f"{prefix}EOL since {eol} ({abs(days)} days ago)"
        elif days == 0:
            result["status"] = "eol"
            result["message"] = f"{prefix}Reaches end of life TODAY ({eol})"
        else:
            result["status"] = "approaching"
            result["message"] = f"{prefix}EOL on {eol} ({days} days remaining)"
    else:
        result["status"] = "untracked"
        result["message"] = note or "No automated EOL source available (manual review)"

    return result


SOURCE = "manual"
LABEL = "manual"
provider = _provider_manual


def url_for(r):
    return r.get("reference_url")
