"""
EOL Checker Lambda — checks software end-of-life status from multiple data sources.

Each product entry declares which data source (provider) to use. Built-in
providers:

    endoflife_date   (default)  — community endoflife.date API; major-cycle EOL
    aws_rds_scrape              — scrapes AWS docs release-calendar pages for
                                  RDS / Aurora PostgreSQL *minor*-version EOL,
                                  which endoflife.date does not track

Adding a new provider is a matter of writing a function with signature
(entry, today) -> result_dict and registering it in PROVIDERS.

Configuration is loaded from an S3 JSON file so products can be updated
without redeploying the Lambda. Alerts are sent via SNS, SES, console, or
HTML file (multiple channels can be enabled at once).

Environment variables:
    CONFIG_BUCKET   — S3 bucket containing the config file
    CONFIG_KEY      — S3 key for the config file (default: eol_config.json)
    SNS_TOPIC_ARN   — SNS topic ARN for plain-text email notifications
    SES_FROM_EMAIL  — SES sender for HTML email notifications (optional)
    SES_TO_EMAILS   — Comma-separated SES recipients (optional)
"""

import calendar
import html.parser
import json
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import date, datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EOL_API_BASE = "https://endoflife.date/api"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config_from_s3():
    """Load product configuration from S3."""
    import boto3

    bucket = os.environ["CONFIG_BUCKET"]
    key = os.environ.get("CONFIG_KEY", "eol_config.json")
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def load_config_from_file(path):
    """Load product configuration from a local file (for testing)."""
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

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


def parse_date_field(value):
    """Parse an EOL/support field which can be a date string, bool, or None.

    Returns:
        date   — if a valid date string was provided
        True   — already EOL / support ended (no specific date)
        False  — no EOL planned / still supported
        None   — field missing or unparseable
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Providers
#
# Each provider takes (entry, today) and returns a normalized result dict.
# Providers are looked up by entry["source"]; missing source defaults to
# "endoflife_date" so existing configs keep working unchanged.
# ---------------------------------------------------------------------------

def _error_result(entry, message):
    """Build an error-shaped result for a config entry."""
    return {
        "label": entry.get("label", f'{entry.get("product", "?")} {entry.get("version", "?")}'),
        "product": entry.get("product"),
        "version": entry.get("version"),
        "status": "error",
        "message": message,
        "days_remaining": None,
        "source": entry.get("source", "endoflife_date"),
    }


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
        result["message"] = "No EOL date announced — still supported"
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


# ---------------------------------------------------------------------------
# AWS RDS / Aurora release-calendar scraper
#
# AWS publishes minor-version EOL dates in HTML tables on their docs site.
# endoflife.date only tracks major versions, so for products like
# "Aurora PostgreSQL 17.5" we scrape the AWS docs directly.
#
# Defenses against silent breakage:
#   1. Header-name -> column-index map (immune to column reordering/insertion)
#   2. Required-headers schema check (loud failure if AWS renames a column)
#   3. Row-count sanity floor (loud failure if the table is truncated)
#   4. Runtime canary (a hardcoded version+EOL pair that must always parse)
#   5. Structured logging of row count + parse stats for CloudWatch
# ---------------------------------------------------------------------------

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


class _AWSCalendarParser(html.parser.HTMLParser):
    """Locates a section by H2 text and extracts its first <table>."""

    def __init__(self, target_heading):
        super().__init__()
        self.target_heading = target_heading
        self.section_found = False
        self.headers = []
        self.rows = []
        self._in_h2 = False
        self._h2_buf = []
        self._in_target = False
        self._in_table = False
        self._table_done = False
        self._row = None
        self._cell_kind = None
        self._cell_buf = []

    def handle_starttag(self, tag, attrs):
        if self._table_done:
            return
        if tag == "h2":
            self._in_h2 = True
            self._h2_buf = []
            return
        if not self._in_target:
            return
        if tag == "table" and not self._in_table:
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell_kind = tag
            self._cell_buf = []

    def handle_endtag(self, tag):
        if tag == "h2" and self._in_h2:
            heading = " ".join("".join(self._h2_buf).split())
            self._in_h2 = False
            if self.target_heading in heading:
                self._in_target = True
                self.section_found = True
            elif self._in_target:
                if self._in_table:
                    self._table_done = True
                self._in_target = False
            return
        if not self._in_target:
            return
        if tag in ("th", "td") and self._cell_kind is not None:
            cell = " ".join("".join(self._cell_buf).split())
            self._row.append((self._cell_kind, cell))
            self._cell_kind = None
            self._cell_buf = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                if not self.headers and all(k == "th" for k, _ in self._row):
                    self.headers = [t for _, t in self._row]
                else:
                    self.rows.append([t for _, t in self._row])
            self._row = None
        elif tag == "table" and self._in_table:
            self._in_table = False
            self._table_done = True

    def handle_data(self, data):
        if self._in_h2:
            self._h2_buf.append(data)
        elif self._cell_kind is not None:
            self._cell_buf.append(data)


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


PROVIDERS = {
    "endoflife_date": _provider_endoflife_date,
    "aws_rds_scrape": _provider_aws_rds_scrape,
}


def check_product(entry, today):
    """Dispatch a config entry to its data-source provider.

    The provider is selected via entry["source"]; defaults to "endoflife_date"
    when not specified. Unknown sources produce an error-shaped result.
    """
    source = entry.get("source", "endoflife_date")
    provider = PROVIDERS.get(source)
    if provider is None:
        return _error_result(entry, f"Unknown source '{source}'. Known: {sorted(PROVIDERS)}")
    return provider(entry, today)


# ---------------------------------------------------------------------------
# Source labels (shared by both formatters)
# ---------------------------------------------------------------------------

_SOURCE_LABELS = {
    "endoflife_date": "endoflife.date",
    "aws_rds_scrape": "AWS docs",
}


def _source_label(r):
    """Render a result's data-source key as a human-readable label."""
    key = r.get("source", "endoflife_date")
    return _SOURCE_LABELS.get(key, key)


# ---------------------------------------------------------------------------
# Categorisation (shared by both formatters)
# ---------------------------------------------------------------------------

def _categorise(results, thresholds):
    """Split results into eol / approaching / ok / error buckets."""
    max_threshold = max(thresholds) if thresholds else 90
    eol, approaching, ok, errors = [], [], [], []

    for r in results:
        status = r["status"]
        if status == "error":
            errors.append(r)
        elif status == "eol":
            eol.append(r)
        elif status == "approaching" and r["days_remaining"] is not None and r["days_remaining"] <= max_threshold:
            approaching.append(r)
        else:
            ok.append(r)

    approaching.sort(key=lambda x: x["days_remaining"])
    return eol, approaching, ok, errors, max_threshold


# ---------------------------------------------------------------------------
# Plain-text report
# ---------------------------------------------------------------------------

def _append_version_info(lines, r):
    """Append latest-patch and latest-cycle lines to the report."""
    if r.get("latest_patch"):
        patch_line = f"    Latest patch: {r['latest_patch']}"
        if r.get("latest_patch_date"):
            patch_line += f" (released {r['latest_patch_date']})"
        lines.append(patch_line)

    if r.get("latest_cycle"):
        if r.get("on_latest_cycle"):
            cycle_line = f"    Latest cycle: {r['latest_cycle']} (you are on the latest)"
        else:
            cycle_line = f"    Latest cycle: {r['latest_cycle']} -> {r.get('latest_cycle_version', '?')}"
            if r.get("latest_cycle_release_date"):
                cycle_line += f" (released {r['latest_cycle_release_date']})"
        lines.append(cycle_line)


def format_report_text(results, thresholds, today):
    """Format results into a readable plain-text report.

    Returns (report_text, has_alerts).
    """
    eol_items, approaching_items, ok_items, error_items, max_t = _categorise(results, thresholds)

    lines = [
        f"End-of-Life Status Report  -  {today}",
        "=" * 52,
    ]

    has_alerts = bool(eol_items or approaching_items)

    if eol_items:
        lines += ["", "!! ALREADY END OF LIFE", "-" * 42]
        for r in eol_items:
            lines.append(f"  * {r['label']}  [{_source_label(r)}]")
            lines.append(f"    {r['message']}")
            _append_version_info(lines, r)

    if approaching_items:
        lines += ["", f">> APPROACHING END OF LIFE (within {max_t} days)", "-" * 42]
        for r in approaching_items:
            lines.append(f"  * {r['label']}  [{_source_label(r)}]")
            lines.append(f"    {r['message']}")
            if r.get("support_message"):
                lines.append(f"    {r['support_message']}")
            _append_version_info(lines, r)

    if ok_items:
        lines += ["", "-- No Immediate Concerns", "-" * 42]
        for r in ok_items:
            lines.append(f"  * {r['label']}  -  {r['message']}  [{_source_label(r)}]")
            _append_version_info(lines, r)

    if error_items:
        lines += ["", "?? Errors", "-" * 42]
        for r in error_items:
            lines.append(f"  * {r['label']}  -  {r['message']}  [{_source_label(r)}]")

    sources_used = sorted({_source_label(r) for r in results})
    lines += [
        "",
        "=" * 52,
        f"Sources: {', '.join(sources_used)}  |  Products checked: {len(results)}",
    ]

    return "\n".join(lines), has_alerts


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_STATUS_COLOURS = {
    "eol":         {"bg": "#fce4e4", "badge_bg": "#d32f2f", "badge_text": "#fff"},
    "approaching": {"bg": "#fff8e1", "badge_bg": "#f57c00", "badge_text": "#fff"},
    "ok":          {"bg": "#e8f5e9", "badge_bg": "#388e3c", "badge_text": "#fff"},
    "error":       {"bg": "#f5f5f5", "badge_bg": "#757575", "badge_text": "#fff"},
}


def _badge(status_key, label):
    c = _STATUS_COLOURS.get(status_key, _STATUS_COLOURS["error"])
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:3px;'
        f'font-size:12px;font-weight:bold;color:{c["badge_text"]};'
        f'background-color:{c["badge_bg"]}">{label}</span>'
    )


def _cycle_cell(r):
    if not r.get("latest_cycle"):
        return "-"
    if r.get("on_latest_cycle"):
        return f'{r["latest_cycle"]} (latest)'
    text = f'{r["latest_cycle"]} &rarr; {r.get("latest_cycle_version", "?")}'
    if r.get("latest_cycle_release_date"):
        text += f'<br><span style="color:#888;font-size:12px">released {r["latest_cycle_release_date"]}</span>'
    return text


def _status_label(r, bucket):
    """Render a coloured badge. *bucket* is the category the item was placed in
    (eol / approaching / ok / error), which may differ from r['status'] when a
    product is 'approaching' but beyond the configured threshold."""
    if bucket == "eol":
        return _badge("eol", "END OF LIFE")
    if bucket == "approaching":
        return _badge("approaching", f'{r["days_remaining"]}d remaining')
    if bucket == "ok":
        if r.get("days_remaining") is not None:
            return _badge("ok", f'{r["days_remaining"]}d remaining')
        return _badge("ok", "OK")
    return _badge("error", "ERROR")


def _html_table_rows(items, status_key):
    """Generate <tr> elements for a list of result items."""
    bg = _STATUS_COLOURS.get(status_key, _STATUS_COLOURS["error"])["bg"]
    rows = []
    for r in items:
        patch = r.get("latest_patch", "-")
        if r.get("latest_patch_date"):
            patch += f'<br><span style="color:#888;font-size:12px">released {r["latest_patch_date"]}</span>'

        eol_display = r.get("eol_date") or "-"

        rows.append(
            f'<tr style="background-color:{bg}">'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0;font-weight:bold">{r["label"]}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{_status_label(r, status_key)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{r["message"]}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{eol_display}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{patch}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{_cycle_cell(r)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0;color:#555;font-size:12px">{_source_label(r)}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def format_report_html(results, thresholds, today):
    """Format results into an inline-styled HTML report.

    Returns (html_string, has_alerts).
    """
    eol_items, approaching_items, ok_items, error_items, max_t = _categorise(results, thresholds)
    has_alerts = bool(eol_items or approaching_items)

    # Summary banner
    if eol_items and approaching_items:
        banner_text = f"{len(eol_items)} EOL + {len(approaching_items)} approaching"
        banner_bg = "#d32f2f"
    elif eol_items:
        banner_text = f"{len(eol_items)} product(s) past end of life"
        banner_bg = "#d32f2f"
    elif approaching_items:
        banner_text = f"{len(approaching_items)} product(s) approaching end of life"
        banner_bg = "#f57c00"
    else:
        banner_text = "All products are within support"
        banner_bg = "#388e3c"

    # Build table rows in status order
    all_rows = ""
    if eol_items:
        all_rows += _html_table_rows(eol_items, "eol")
    if approaching_items:
        all_rows += _html_table_rows(approaching_items, "approaching")
    if ok_items:
        all_rows += _html_table_rows(ok_items, "ok")
    if error_items:
        all_rows += _html_table_rows(error_items, "error")

    sources_used_html = ", ".join(sorted({_source_label(r) for r in results}))

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#f4f4f4;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f4">
<tr><td align="center" style="padding:24px 12px">

  <!-- Container -->
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:900px;background-color:#ffffff;border-radius:8px;overflow:hidden">

    <!-- Header -->
    <tr>
      <td style="background-color:#1a237e;color:#ffffff;padding:20px 24px">
        <h1 style="margin:0;font-size:22px;font-weight:bold">End-of-Life Status Report</h1>
        <p style="margin:6px 0 0;font-size:14px;color:#c5cae9">{today} &nbsp;|&nbsp; {len(results)} products checked</p>
      </td>
    </tr>

    <!-- Banner -->
    <tr>
      <td style="background-color:{banner_bg};color:#ffffff;padding:12px 24px;font-size:15px;font-weight:bold">
        {banner_text}
      </td>
    </tr>

    <!-- Table -->
    <tr>
      <td style="padding:0">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
          <tr style="background-color:#263238">
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Product</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Status</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Details</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">EOL Date</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Latest Patch</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Latest Cycle</th>
            <th style="padding:10px 12px;text-align:left;color:#ffffff;font-size:13px">Source</th>
          </tr>
          {all_rows}
        </table>
      </td>
    </tr>

    <!-- Footer -->
    <tr>
      <td style="padding:16px 24px;font-size:12px;color:#888888;border-top:1px solid #e0e0e0">
        Sources: {sources_used_html}
        &nbsp;|&nbsp; Alert threshold: {max_t} days
      </td>
    </tr>

  </table>

</td></tr>
</table>
</body>
</html>"""

    return html, has_alerts


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _notify_console(report_text, **_kwargs):
    """Print the plain-text report to stdout."""
    print(report_text)


def _notify_html_file(report_html, notif_config, **_kwargs):
    """Write the HTML report to a local file.

    The current date/hour/minute is injected before the extension so each run
    produces a uniquely named file (e.g. eol_report_2026-05-03_1430.html).
    """
    path = notif_config.get("path", "eol_report.html")
    base, ext = os.path.splitext(path)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = f"{base}_{timestamp}{ext}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_html)
    logger.info("HTML report written to %s", path)


def _notify_sns(report_text, subject, notif_config, **_kwargs):
    """Publish the plain-text report to an SNS topic."""
    import boto3

    topic_arn = notif_config.get("topic_arn") or os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        logger.error("SNS notification skipped: no topic_arn in config or SNS_TOPIC_ARN env var")
        return
    sns = boto3.client("sns")
    sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=report_text)
    logger.info("SNS notification sent to %s", topic_arn)


def _notify_ses(report_html, subject, notif_config, **_kwargs):
    """Send the HTML report as an email via SES."""
    import boto3

    from_email = notif_config.get("from_email") or os.environ.get("SES_FROM_EMAIL")
    to_emails = notif_config.get("to_emails") or []
    env_to = os.environ.get("SES_TO_EMAILS")
    if env_to and not to_emails:
        to_emails = [e.strip() for e in env_to.split(",")]

    if not from_email or not to_emails:
        logger.error("SES notification skipped: from_email or to_emails not configured")
        return

    ses = boto3.client("ses")
    ses.send_email(
        Source=from_email,
        Destination={"ToAddresses": to_emails},
        Message={
            "Subject": {"Data": subject[:100], "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": report_html, "Charset": "UTF-8"},
            },
        },
    )
    logger.info("SES email sent from %s to %s", from_email, to_emails)


_NOTIFIERS = {
    "console":   _notify_console,
    "html_file": _notify_html_file,
    "sns":       _notify_sns,
    "ses":       _notify_ses,
}


def send_notifications(config, report_text, report_html, subject):
    """Dispatch the report to every notification channel listed in config."""
    notifications = config.get("notifications", [{"type": "sns"}])

    for notif in notifications:
        ntype = notif.get("type")
        handler = _NOTIFIERS.get(ntype)
        if handler is None:
            logger.warning("Unknown notification type: %s — skipping", ntype)
            continue
        try:
            handler(
                report_text=report_text,
                report_html=report_html,
                subject=subject,
                notif_config=notif,
            )
        except Exception as exc:
            logger.error("Notification '%s' failed: %s", ntype, exc)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """Entry point for AWS Lambda."""
    today = date.today()

    config = load_config_from_s3()
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])
    notify_when = config.get("notify_when", "always")  # "always" | "alerts_only"

    logger.info("Checking %d products for EOL status", len(products))

    results = [check_product(entry, today) for entry in products]

    for r in results:
        logger.info("%s: %s", r["label"], r["message"])

    report_text, has_alerts = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    should_notify = notify_when == "always" or has_alerts

    if should_notify:
        prefix = "EOL ALERT" if has_alerts else "EOL Report"
        subject = f"[{prefix}] Software End-of-Life Status - {today}"
        send_notifications(config, report_text, report_html, subject)
    else:
        logger.info("No alerts and notify_when=alerts_only — skipping notification")

    return {
        "statusCode": 200,
        "checked": len(results),
        "has_alerts": has_alerts,
        "notified": should_notify,
    }


# ---------------------------------------------------------------------------
# Local testing  (python lambda_function.py [config.json])
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "eol_config.json"
    config = load_config_from_file(config_path)

    today = date.today()
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])

    results = [check_product(entry, today) for entry in products]
    report_text, has_alerts = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    prefix = "EOL ALERT" if has_alerts else "EOL Report"
    subject = f"[{prefix}] Software End-of-Life Status - {today}"

    send_notifications(config, report_text, report_html, subject)
