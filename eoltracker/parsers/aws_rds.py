"""AWS RDS / Aurora release-calendar scraper.

AWS publishes minor-version EOL dates in HTML tables on their docs site.
endoflife.date only tracks major versions, so for products like
"Aurora PostgreSQL 17.5" we scrape the AWS docs directly.

Defenses against silent breakage:
  1. Header-name -> column-index map (immune to column reordering/insertion)
  2. Required-headers schema check (loud failure if AWS renames a column)
  3. Row-count sanity floor (loud failure if the table is truncated)
  4. Runtime canary (a hardcoded version+EOL pair that must always parse)
  5. Structured logging of row count + parse stats for CloudWatch
"""

import calendar
import re
import urllib.request
from datetime import date, datetime

from ..core import _AWSCalendarParser, _error_result, logger

_AWS_DOCS_URLS = {
    "aurora-postgresql": (
        "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/"
        "aurorapostgresql-release-calendar.html"
    ),
    "rds-postgresql": (
        "https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/"
        "postgresql-release-calendar.html"
    ),
}

_AWS_HEADING_TEXT = {
    "aurora-postgresql": "Release calendar for Aurora PostgreSQL minor versions",
    "rds-postgresql": "Release calendar for Amazon RDS for PostgreSQL minor versions",
}

# Per-engine column names. The version column is the same across engines but
# the release-date and EOL columns differ ("Aurora ..." vs "RDS ...").
_AWS_COLUMNS = {
    "aurora-postgresql": {
        "version": "PostgreSQL minor engine version",
        "release": "Aurora release date",
        "eol":     "Aurora end of standard support date",
    },
    "rds-postgresql": {
        "version": "PostgreSQL minor engine version",
        "release": "RDS release date",
        "eol":     "RDS end of standard support date",
    },
}

# (clean_version, expected_eol). If parsing the canary version produces a
# different EOL date — or fails to find the row — every entry from this
# scraper is failed for the run.
_AWS_RDS_CANARIES = {
    "aurora-postgresql": ("17.7", date(2030, 2, 28)),
    # RDS-PostgreSQL has no LTS column; all current minor versions use
    # approximate Month-YYYY dates. 17.7 -> "March 2027" -> end-of-month.
    "rds-postgresql":    ("17.7", date(2027, 3, 31)),
}

_AWS_MIN_ROWS = 10
_AWS_RDS_CACHE = {}


def _parse_aws_date(text):
    """Parse an AWS calendar date string.

    Returns (date, was_approximate) or None on failure. Handles three observed
    formats:
      "30 June 2025"   -> exact day
      "May 1 2025"     -> exact day (AWS mixes the two)
      "December 2026"  -> approximate; returns last day of month
    """
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y"):
        try:
            return datetime.strptime(text, fmt).date(), False
        except ValueError:
            continue
    for fmt in ("%B %Y", "%b %Y"):
        try:
            d = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        last_day = calendar.monthrange(d.year, d.month)[1]
        return d.replace(day=last_day), True
    return None


def _clean_version(raw):
    """Strip '(LTS)' and footnote markers like '$1' from a version cell."""
    cleaned = re.sub(r"\s*\(LTS\)", "", raw)
    cleaned = re.sub(r"\$\d+", "", cleaned)
    return cleaned.strip()


def _scrape_aws_rds_calendar(engine):
    """Fetch + parse the AWS release calendar for *engine*.

    Returns {clean_version: {eol, eol_approximate, eol_raw, aurora_release, lts}}.
    Raises ValueError on structural drift or canary failure.
    """
    if engine in _AWS_RDS_CACHE:
        return _AWS_RDS_CACHE[engine]

    url = _AWS_DOCS_URLS.get(engine)
    if url is None:
        raise ValueError(f"No AWS docs URL configured for engine '{engine}'")

    target_heading = _AWS_HEADING_TEXT[engine]
    columns = _AWS_COLUMNS[engine]
    required_headers = set(columns.values())

    req = urllib.request.Request(url, headers={"Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")

    parser = _AWSCalendarParser(target_heading)
    parser.feed(html_text)

    if not parser.section_found:
        raise ValueError(f"Section heading not found: '{target_heading}'")
    if not parser.headers:
        raise ValueError("No table headers parsed under target section")

    missing = required_headers - set(parser.headers)
    if missing:
        raise ValueError(
            f"AWS docs schema changed: missing headers {sorted(missing)}; "
            f"got {parser.headers}"
        )

    col = {h: i for i, h in enumerate(parser.headers)}
    v_idx = col[columns["version"]]
    rel_idx = col[columns["release"]]
    eol_idx = col[columns["eol"]]

    versions = {}
    for row in parser.rows:
        if len(row) < len(parser.headers):
            continue  # major-version separator row
        raw_version = row[v_idx].strip()
        if not raw_version:
            continue
        clean = _clean_version(raw_version)
        eol_parsed = _parse_aws_date(row[eol_idx])
        rel_parsed = _parse_aws_date(row[rel_idx])
        versions[clean] = {
            "raw_version": raw_version,
            "lts": "(LTS)" in raw_version,
            "eol_raw": row[eol_idx],
            "eol": eol_parsed[0] if eol_parsed else None,
            "eol_approximate": eol_parsed[1] if eol_parsed else False,
            "release": rel_parsed[0] if rel_parsed else None,
        }

    if len(versions) < _AWS_MIN_ROWS:
        raise ValueError(
            f"Parsed only {len(versions)} versions; expected >= {_AWS_MIN_ROWS}. "
            f"Table may be truncated."
        )

    canary = _AWS_RDS_CANARIES.get(engine)
    if canary:
        cv, expected_eol = canary
        info = versions.get(cv)
        actual_eol = info.get("eol") if info else None
        if actual_eol != expected_eol:
            raise ValueError(
                f"Canary failed: '{cv}' expected EOL {expected_eol}, got {actual_eol}. "
                f"AWS docs structure may have changed."
            )

    logger.info(
        "AWS docs scraped (%s): %d versions, headers=%d",
        engine, len(versions), len(parser.headers)
    )

    _AWS_RDS_CACHE[engine] = versions
    return versions


def _provider_aws_rds_scrape(entry, today):
    """Look up AWS RDS/Aurora minor-version EOL by scraping AWS docs."""
    engine = entry.get("engine", "aurora-postgresql")
    version = str(entry.get("version", ""))
    label = entry.get("label", f"{engine} {version}")

    try:
        versions = _scrape_aws_rds_calendar(engine)
    except Exception as exc:
        logger.error("AWS scraper failed for %s %s: %s", engine, version, exc)
        result = _error_result(entry, f"AWS scraper failed: {exc}")
        result["source"] = "aws_rds_scrape"
        return result

    info = versions.get(version)
    if info is None:
        available = sorted(versions.keys(), reverse=True)[:8]
        result = _error_result(entry, f"Version '{version}' not in AWS calendar. Available: {available}")
        result["source"] = "aws_rds_scrape"
        return result

    def _vkey(v):
        try:
            return tuple(int(p) for p in v.split("."))
        except (ValueError, AttributeError):
            return (-1,)

    major = version.split(".")[0]
    same_major = sorted(
        (v for v in versions if v.split(".")[0] == major),
        key=_vkey, reverse=True,
    )
    latest_in_major = same_major[0] if same_major else version
    latest_in_major_release = versions.get(latest_in_major, {}).get("release")

    all_majors = sorted({v.split(".")[0] for v in versions}, key=lambda x: int(x) if x.isdigit() else -1, reverse=True)
    latest_major = all_majors[0] if all_majors else major
    latest_major_versions = sorted(
        (v for v in versions if v.split(".")[0] == latest_major),
        key=_vkey, reverse=True,
    )
    latest_cycle_version = latest_major_versions[0] if latest_major_versions else version
    latest_cycle_release = versions.get(latest_cycle_version, {}).get("release")

    eol = info["eol"]
    eol_approximate = info["eol_approximate"]

    result = {
        "label": label,
        "product": engine,
        "version": version,
        "lts": info["lts"],
        "latest_patch": latest_in_major,
        "latest_patch_date": str(latest_in_major_release) if latest_in_major_release else None,
        "latest_cycle": latest_major,
        "latest_cycle_version": latest_cycle_version,
        "latest_cycle_release_date": str(latest_cycle_release) if latest_cycle_release else None,
        "on_latest_cycle": major == latest_major,
        "eol_date": str(eol) if eol else None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "aws_rds_scrape",
    }

    if eol is None:
        result["status"] = "unknown"
        result["message"] = f"Could not parse EOL date (raw: '{info['eol_raw']}')"
    else:
        days = (eol - today).days
        result["days_remaining"] = days
        approx = " (approximate end-of-month)" if eol_approximate else ""
        if days < 0:
            result["status"] = "eol"
            result["message"] = f"AWS standard support ended {eol} ({abs(days)} days ago){approx}"
        elif days == 0:
            result["status"] = "eol"
            result["message"] = f"AWS standard support ends TODAY ({eol}){approx}"
        else:
            result["status"] = "approaching"
            result["message"] = f"AWS standard support ends {eol} ({days} days remaining){approx}"

    return result


SOURCE = "aws_rds_scrape"
LABEL = "AWS docs"
provider = _provider_aws_rds_scrape


def url_for(r):
    product = r.get("product") or ""
    return _AWS_DOCS_URLS.get(product)
