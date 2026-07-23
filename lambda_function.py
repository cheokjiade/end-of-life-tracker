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
import urllib.parse
import urllib.request
import urllib.error
from datetime import date, datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EOL_API_BASE = "https://endoflife.date/api"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config_from_s3(key=None):
    """Load product configuration from S3.

    *key* overrides the CONFIG_KEY env var when supplied. EventBridge rules
    pass it via the invocation event so a single Lambda can fan out across
    many per-project config files.
    """
    import boto3

    bucket = os.environ["CONFIG_BUCKET"]
    key = key or os.environ.get("CONFIG_KEY", "eol_config.a.json")
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


# ---------------------------------------------------------------------------
# Maven Central staleness provider
#
# Most Java libraries don't publish lifecycle dates (Apache Commons, jsoup,
# Netty, Quartz, Logback, etc.). For these we report what we *can* know
# from the registry: when the in-use version was released, what the latest
# is, and when that was released.
#
# Status is always 'ok' — no EOL is being claimed, this is informational.
# ---------------------------------------------------------------------------

_MAVEN_CENTRAL_API = "https://search.maven.org/solrsearch/select"
# Two cache namespaces: one for "the latest gav of this artifact" and one
# for "this specific gav". Targeted queries avoid the rows-cutoff problem
# that bites artifacts with hundreds of releases (e.g. Netty).
_MAVEN_LATEST_CACHE = {}    # (group, artifact) -> {"v", "released"}|None
_MAVEN_VERSION_CACHE = {}   # (group, artifact, version) -> {"v", "released"}|None


def _fetch_maven_doc(query):
    """Run a Maven Central solr query and return the first doc, or None."""
    url = f"{_MAVEN_CENTRAL_API}?q={query}&core=gav&rows=1&wt=json"
    req = urllib.request.Request(url, headers={
        "Accept":     "application/json",
        "User-Agent": "EOL-Tracker/1.0",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    docs = data.get("response", {}).get("docs", [])
    if not docs:
        return None
    d = docs[0]
    ts = d.get("timestamp")
    return {
        "v":        d.get("v"),
        "released": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date() if ts else None,
    }


def _fetch_maven_latest(group, artifact):
    """Return the most recent gav for an artifact (any major), or None."""
    key = (group, artifact)
    if key in _MAVEN_LATEST_CACHE:
        return _MAVEN_LATEST_CACHE[key]
    q = f"g:{urllib.parse.quote(group)}+AND+a:{urllib.parse.quote(artifact)}"
    info = _fetch_maven_doc(q)
    _MAVEN_LATEST_CACHE[key] = info
    return info


def _fetch_maven_specific(group, artifact, version):
    """Return the gav doc for a specific version, or None if not on Central."""
    key = (group, artifact, version)
    if key in _MAVEN_VERSION_CACHE:
        return _MAVEN_VERSION_CACHE[key]
    q = (
        f"g:{urllib.parse.quote(group)}+AND+"
        f"a:{urllib.parse.quote(artifact)}+AND+"
        f"v:{urllib.parse.quote(version)}"
    )
    info = _fetch_maven_doc(q)
    _MAVEN_VERSION_CACHE[key] = info
    return info


def _provider_maven_central(entry, today):
    """Report release staleness for a Maven artifact. No EOL data implied."""
    group = entry.get("group")
    artifact = entry.get("artifact")
    version = str(entry.get("version", ""))
    label = entry.get("label", f"{group}:{artifact}:{version}")

    if not (group and artifact and version):
        result = _error_result(entry, "Maven Central entries require 'group', 'artifact', and 'version'")
        result["source"] = "maven_central"
        return result

    try:
        latest = _fetch_maven_latest(group, artifact)
        in_use = _fetch_maven_specific(group, artifact, version)
    except Exception as exc:
        logger.error("Maven Central fetch failed for %s:%s: %s", group, artifact, exc)
        result = _error_result(entry, f"Maven Central query failed: {exc}")
        result["source"] = "maven_central"
        return result

    if not latest:
        result = _error_result(entry, f"Artifact {group}:{artifact} not found on Maven Central")
        result["source"] = "maven_central"
        return result

    latest_v = latest["v"]
    latest_date = latest["released"]
    in_use_date = in_use["released"] if in_use else None
    on_latest = latest_v == version

    if on_latest:
        message = f"On latest Maven Central release ({latest_v})"
    elif in_use_date and latest_date:
        days_newer = (latest_date - in_use_date).days
        message = (
            f"In use: {version} ({in_use_date}); latest: {latest_v} "
            f"({latest_date}, {days_newer} days newer)"
        )
    elif in_use_date is None:
        message = (
            f"Version {version} not on Maven Central (private build?); "
            f"latest published is {latest_v} ({latest_date})"
        )
    else:
        message = f"In use: {version}; latest: {latest_v}"

    return {
        "label": label,
        "product": f"{group}:{artifact}",
        "version": version,
        "lts": False,
        "status": "ok",
        "message": message,
        "in_use_release_date": str(in_use_date) if in_use_date else None,
        "latest_patch": latest_v,
        "latest_patch_date": str(latest_date) if latest_date else None,
        "latest_cycle": None,
        "latest_cycle_version": None,
        "latest_cycle_release_date": None,
        "on_latest_cycle": on_latest,
        "eol_date": None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "maven_central",
    }


# ---------------------------------------------------------------------------
# npm registry staleness provider
#
# npm libraries rarely publish EOL dates. Like maven_central, we report what
# the registry knows: when the in-use version shipped, the latest version and
# its date, and how far behind. The one hard signal npm gives is per-version
# deprecation, which we surface as an alert.
# ---------------------------------------------------------------------------

_NPM_REGISTRY_API = "https://registry.npmjs.org"
_NPM_STALE_MONTHS = 24
_NPM_CACHE = {}


def _months_between(d1, d2):
    """Whole months from d1 to d2 (d2 >= d1 assumed for positive result)."""
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)


def _fetch_npm_doc(package):
    """Fetch npm registry metadata for *package*, or None on 404. Cached."""
    if package in _NPM_CACHE:
        return _NPM_CACHE[package]
    enc = urllib.parse.quote(package, safe="@")  # '@mui/material' -> '@mui%2Fmaterial'
    url = f"{_NPM_REGISTRY_API}/{enc}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "EOL-Tracker/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _NPM_CACHE[package] = None
            return None
        raise
    _NPM_CACHE[package] = doc
    return doc


def _npm_result_from_doc(entry, doc, today):
    """Pure: build a normalized result dict from a fetched npm registry doc."""
    package = entry.get("package", "")
    version = str(entry["version"]) if entry.get("version") is not None else ""
    label = entry.get("label") or (f"{package} {version}".strip())

    if doc is None:
        result = _error_result(entry, f"Package '{package}' not found on npm registry")
        result["source"] = "npm_registry"
        result["product"] = package
        return result

    dist_tags = doc.get("dist-tags") or {}
    latest = dist_tags.get("latest")
    times = doc.get("time") or {}
    versions = doc.get("versions") or {}

    def _pdate(v):
        ts = times.get(v)
        if not ts:
            return None
        try:
            return datetime.strptime(ts[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    latest_date = _pdate(latest) if latest else None
    in_use_date = _pdate(version) if version else None
    on_latest = bool(version) and version == latest

    result = {
        "label": label,
        "product": package,
        "version": version,
        "lts": False,
        "in_use_release_date": str(in_use_date) if in_use_date else None,
        "latest_patch": latest,
        "latest_patch_date": str(latest_date) if latest_date else None,
        "latest_cycle": None,
        "latest_cycle_version": None,
        "latest_cycle_release_date": None,
        "on_latest_cycle": on_latest,
        "eol_date": None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "npm_registry",
    }

    dep = versions.get(version, {}).get("deprecated") if version else None
    if isinstance(dep, str) and dep.strip():
        result["status"] = "eol"
        result["message"] = f"npm marks {version} deprecated: {dep.strip()}"
        return result

    result["status"] = "ok"
    if not latest:
        result["message"] = "No versions published on npm registry"
    elif not version:
        result["message"] = f"In-use version not provided; latest is {latest}" + (f" ({latest_date})" if latest_date else "")
    elif version not in versions:
        result["message"] = f"Version {version} not on npm registry (private build?); latest is {latest}" + (f" ({latest_date})" if latest_date else "")
    elif on_latest:
        if latest_date and _months_between(latest_date, today) >= _NPM_STALE_MONTHS:
            yrs = _months_between(latest_date, today) / 12.0
            result["message"] = f"On latest ({latest}) but it's from {latest_date} (~{yrs:.1f}y) - likely unmaintained"
        else:
            result["message"] = f"On latest npm release ({latest})"
    else:
        def _maj(v):
            m = re.match(r"\s*(\d+)", v or "")
            return int(m.group(1)) if m else None
        mu, ml = _maj(version), _maj(latest)
        majors = (ml - mu) if (mu is not None and ml is not None and ml > mu) else 0
        bits = []
        if majors:
            bits.append(f"{majors} major(s) behind")
        if in_use_date and latest_date:
            bits.append(f"{(latest_date - in_use_date).days}d newer")
        tail = f" ({', '.join(bits)})" if bits else ""
        result["message"] = (
            f"In use {version}" + (f" ({in_use_date})" if in_use_date else "")
            + f"; latest {latest}" + (f" ({latest_date})" if latest_date else "") + tail
        )
    return result


def _provider_npm_registry(entry, today):
    """Report npm-registry staleness (and deprecation) for a package."""
    package = entry.get("package")
    if not package:
        result = _error_result(entry, "npm_registry entries require 'package'")
        result["source"] = "npm_registry"
        return result
    try:
        doc = _fetch_npm_doc(package)
    except Exception as exc:
        logger.error("npm registry fetch failed for %s: %s", package, exc)
        result = _error_result(entry, f"npm registry query failed: {exc}")
        result["source"] = "npm_registry"
        result["product"] = package
        return result
    return _npm_result_from_doc(entry, doc, today)


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


# ---------------------------------------------------------------------------
# Generic HTML table extractor
#
# Used by scrapers whose target page has only one table of interest. Finds
# the first <table> whose <th> row contains every entry in required_headers.
# (The existing _AWSCalendarParser is heading-anchored and is used by the
# AWS RDS scraper, where the calendar page has multiple tables.)
# ---------------------------------------------------------------------------

class _HtmlTableExtractor(html.parser.HTMLParser):
    """Extract the first <table> whose <th> row contains all required_headers."""

    def __init__(self, required_headers):
        super().__init__()
        self._required = set(required_headers)
        self.headers = []
        self.rows = []
        self.found = False
        self._depth = 0
        self._cur_headers = []
        self._cur_rows = []
        self._row = None
        self._cell_kind = None
        self._cell_buf = []

    def handle_starttag(self, tag, attrs):
        if self.found:
            return
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._cur_headers = []
                self._cur_rows = []
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell_kind = tag
            self._cell_buf = []

    def handle_endtag(self, tag):
        if self.found:
            return
        if tag == "table":
            if self._depth == 1:
                if self._cur_headers and self._required.issubset(set(self._cur_headers)):
                    self.headers = self._cur_headers
                    self.rows = self._cur_rows
                    self.found = True
            self._depth = max(0, self._depth - 1)
            return
        if self._depth != 1:
            return
        if tag in ("th", "td") and self._cell_kind is not None:
            cell = " ".join("".join(self._cell_buf).split())
            self._row.append((self._cell_kind, cell))
            self._cell_kind = None
            self._cell_buf = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                if not self._cur_headers and all(k == "th" for k, _ in self._row):
                    self._cur_headers = [t for _, t in self._row]
                else:
                    self._cur_rows.append([t for _, t in self._row])
            self._row = None

    def handle_data(self, data):
        if self._cell_kind is not None:
            self._cell_buf.append(data)


# ---------------------------------------------------------------------------
# AWS SDK lifecycle scraper
#
# AWS publishes a per-major-version lifecycle phase for every SDK at the
# version-support-matrix page. Phases:
#   "Developer Preview"        -> ok        (not for prod, but no EOL)
#   "General Availability"     -> ok
#   "Maintenance Announcement" -> approaching  (EOL coming, ~6mo to maintenance)
#   "Maintenance"              -> approaching  (limited fixes, ~12mo to EOL)
#   "End-of-Support"           -> eol
# No specific EOL dates are published in the matrix — only the GA date.
# ---------------------------------------------------------------------------

_AWS_SDK_URL = "https://docs.aws.amazon.com/sdkref/latest/guide/version-support-matrix.html"
_AWS_SDK_REQUIRED_HEADERS = {"SDK", "Major version", "Current Phase", "General Availability Date"}
_AWS_SDK_MIN_ROWS = 12
# Canary: Java v1 is unambiguously past EOL; trips loudly if AWS removes
# it or renames the phase column.
_AWS_SDK_CANARY = {
    "sdk_substring":   "SDK for Java",
    "major":           "1.x",
    "phase_substring": "End-of-Support",
}
_AWS_SDK_CACHE = None


def _scrape_aws_sdk_lifecycle():
    """Fetch + parse the AWS SDKs and Tools version-support-matrix.

    Returns a list of {sdk, major, phase, ga_date_raw} dicts.
    """
    global _AWS_SDK_CACHE
    if _AWS_SDK_CACHE is not None:
        return _AWS_SDK_CACHE

    req = urllib.request.Request(_AWS_SDK_URL, headers={"Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")

    parser = _HtmlTableExtractor(_AWS_SDK_REQUIRED_HEADERS)
    parser.feed(html_text)

    if not parser.found:
        raise ValueError(
            f"AWS SDK matrix table not found. Required headers: "
            f"{sorted(_AWS_SDK_REQUIRED_HEADERS)}"
        )

    col = {h: i for i, h in enumerate(parser.headers)}
    sdk_idx, major_idx = col["SDK"], col["Major version"]
    phase_idx, ga_idx = col["Current Phase"], col["General Availability Date"]

    entries = []
    for row in parser.rows:
        if len(row) < len(parser.headers) or not row[sdk_idx].strip():
            continue
        entries.append({
            "sdk":         row[sdk_idx],
            "major":       row[major_idx],
            "phase":       row[phase_idx],
            "ga_date_raw": row[ga_idx],
        })

    if len(entries) < _AWS_SDK_MIN_ROWS:
        raise ValueError(
            f"Parsed only {len(entries)} SDK entries; expected >= {_AWS_SDK_MIN_ROWS}. "
            f"Table may be truncated or malformed."
        )

    canary = _AWS_SDK_CANARY
    found_canary = next(
        (e for e in entries
         if canary["sdk_substring"] in e["sdk"] and e["major"] == canary["major"]),
        None,
    )
    if not found_canary or canary["phase_substring"] not in found_canary["phase"]:
        raise ValueError(
            f"AWS SDK canary failed: expected '{canary['sdk_substring']}' "
            f"{canary['major']} to be '{canary['phase_substring']}', got {found_canary}. "
            f"AWS docs structure may have changed."
        )

    logger.info("AWS SDK lifecycle matrix scraped: %d entries", len(entries))
    _AWS_SDK_CACHE = entries
    return entries


def _provider_aws_sdk_lifecycle(entry, today):
    """Look up an AWS SDK's lifecycle phase from the AWS docs matrix."""
    sdk = entry.get("sdk", "")
    major = str(entry.get("major", ""))
    label = entry.get("label", f"{sdk} {major}")

    if not (sdk and major):
        result = _error_result(entry, "AWS SDK entries require 'sdk' and 'major'")
        result["source"] = "aws_sdk_lifecycle"
        return result

    try:
        entries = _scrape_aws_sdk_lifecycle()
    except Exception as exc:
        logger.error("AWS SDK lifecycle scraper failed: %s", exc)
        result = _error_result(entry, f"AWS SDK lifecycle scraper failed: {exc}")
        result["source"] = "aws_sdk_lifecycle"
        return result

    found = next(
        (e for e in entries if sdk in e["sdk"] and e["major"] == major),
        None,
    )
    if not found:
        available = sorted({f"{e['sdk']} {e['major']}" for e in entries})[:8]
        result = _error_result(
            entry,
            f"SDK '{sdk}' major '{major}' not in AWS matrix. Available: {available}"
        )
        result["source"] = "aws_sdk_lifecycle"
        return result

    phase = found["phase"]
    if "End-of-Support" in phase:
        status = "eol"
    elif "Maintenance" in phase:  # both "Maintenance" and "Maintenance Announcement"
        status = "approaching"
    else:
        status = "ok"

    same_sdk = [e for e in entries if e["sdk"] == found["sdk"]]
    def _major_key(m):
        nums = re.findall(r"\d+", m)
        return int(nums[0]) if nums else -1
    same_sdk_sorted = sorted(same_sdk, key=lambda e: _major_key(e["major"]), reverse=True)
    latest_major_entry = same_sdk_sorted[0] if same_sdk_sorted else found
    on_latest_cycle = latest_major_entry["major"] == major

    return {
        "label": label,
        "product": found["sdk"],
        "version": major,
        "lts": False,
        "status": status,
        "message": f"{phase} (SDK went GA on {found['ga_date_raw']})",
        "latest_patch": None,
        "latest_patch_date": None,
        "latest_cycle": latest_major_entry["major"],
        "latest_cycle_version": latest_major_entry["major"],
        "latest_cycle_release_date": None,
        "on_latest_cycle": on_latest_cycle,
        "eol_date": None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "aws_sdk_lifecycle",
    }


# ---------------------------------------------------------------------------
# Jackson lifecycle scraper
#
# FasterXML's Jackson Releases wiki page lists branches as either "open"
# (currently maintained) or "closed" (no further patches). No specific EOL
# dates are published — this provider returns ok/eol only.
# ---------------------------------------------------------------------------

_JACKSON_WIKI_URL = "https://github.com/FasterXML/jackson/wiki/Jackson-Releases"
# Canary: 2.18 is an LTS that should consistently appear; if it's absent
# from both buckets, the wiki has been restructured.
_JACKSON_CANARY = "2.18"
_JACKSON_CACHE = None


class _JacksonWikiParser(html.parser.HTMLParser):
    """Extract Jackson branch statuses from the FasterXML Releases wiki page.

    The page uses <h3> headings like "Open branches" / "Closed branches" /
    "Legacy" within an <h2>"Public releases" section. Each list item under
    a section starts with a link tag <a href="Jackson-Release-X.Y">X.Y</a>
    whose text content is the branch number — that's what we collect.

    Sections handled:
      "Open branches" / "Currently Maintained" -> open
      "Closed branches" / "Recently Closed"    -> closed
      "Legacy"                                 -> closed (old majors)
      anything else                            -> ignored
    """

    def __init__(self):
        super().__init__()
        self._section = None       # "open" | "closed" | None
        self._heading_tag = None
        self._heading_buf = []
        self._in_anchor = False
        self._anchor_buf = []
        self.open_branches = set()
        self.closed_branches = set()

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4"):
            self._heading_tag = tag
            self._heading_buf = []
        elif tag == "a" and self._section is not None:
            self._in_anchor = True
            self._anchor_buf = []

    def handle_endtag(self, tag):
        if self._heading_tag is not None and tag == self._heading_tag:
            heading = " ".join("".join(self._heading_buf).split()).lower()
            self._heading_tag = None
            if "open" in heading or "maintain" in heading:
                self._section = "open"
            elif "closed" in heading or "legacy" in heading:
                self._section = "closed"
            elif tag in ("h1", "h2"):
                self._section = None
            # h3/h4 with non-matching name leaves the section unchanged
        elif tag == "a" and self._in_anchor:
            self._in_anchor = False
            text = "".join(self._anchor_buf).strip()
            m = re.fullmatch(r"(\d+\.\d+)", text)
            if m and self._section:
                if self._section == "open":
                    self.open_branches.add(m.group(1))
                elif self._section == "closed":
                    self.closed_branches.add(m.group(1))

    def handle_data(self, data):
        if self._heading_tag is not None:
            self._heading_buf.append(data)
        elif self._in_anchor:
            self._anchor_buf.append(data)


def _scrape_jackson_lifecycle():
    """Fetch + parse the Jackson Releases wiki page."""
    global _JACKSON_CACHE
    if _JACKSON_CACHE is not None:
        return _JACKSON_CACHE

    req = urllib.request.Request(_JACKSON_WIKI_URL, headers={
        "Accept": "text/html",
        "User-Agent": "EOL-Tracker/1.0",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")

    parser = _JacksonWikiParser()
    parser.feed(html_text)

    if not parser.open_branches:
        raise ValueError(
            f"No 'open' Jackson branches detected. closed={parser.closed_branches}. "
            f"Wiki structure may have changed."
        )
    if (_JACKSON_CANARY not in parser.open_branches
            and _JACKSON_CANARY not in parser.closed_branches):
        raise ValueError(
            f"Jackson canary failed: branch {_JACKSON_CANARY} not found. "
            f"open={parser.open_branches}, closed={parser.closed_branches}"
        )

    logger.info(
        "Jackson lifecycle parsed: open=%s closed=%s",
        sorted(parser.open_branches), sorted(parser.closed_branches)
    )

    _JACKSON_CACHE = {"open": parser.open_branches, "closed": parser.closed_branches}
    return _JACKSON_CACHE


def _provider_jackson_lifecycle(entry, today):
    """Look up Jackson branch status from the FasterXML wiki."""
    version = str(entry.get("version", ""))
    label = entry.get("label", f"Jackson {version}.x")

    try:
        data = _scrape_jackson_lifecycle()
    except Exception as exc:
        logger.error("Jackson scraper failed: %s", exc)
        result = _error_result(entry, f"Jackson scraper failed: {exc}")
        result["source"] = "jackson_lifecycle"
        return result

    open_branches = data["open"]
    closed_branches = data["closed"]

    if version in open_branches:
        status = "ok"
        message = f"Branch {version} is currently maintained per FasterXML wiki"
    elif version in closed_branches:
        status = "eol"
        message = (
            f"Branch {version} has been closed by FasterXML "
            f"(no further patches; no specific EOL date published)"
        )
    else:
        result = _error_result(
            entry,
            f"Branch '{version}' not in Jackson wiki. "
            f"Open: {sorted(open_branches, reverse=True)}; "
            f"closed: {sorted(closed_branches, reverse=True)}"
        )
        result["source"] = "jackson_lifecycle"
        return result

    def _vkey(v):
        try:
            return tuple(int(p) for p in v.split("."))
        except (ValueError, AttributeError):
            return (-1,)
    latest_open = max(open_branches, key=_vkey) if open_branches else None
    on_latest = (version == latest_open) if latest_open else False

    return {
        "label": label,
        "product": "jackson",
        "version": version,
        "lts": False,
        "status": status,
        "message": message,
        "latest_patch": None,
        "latest_patch_date": None,
        "latest_cycle": latest_open,
        "latest_cycle_version": latest_open,
        "latest_cycle_release_date": None,
        "on_latest_cycle": on_latest,
        "eol_date": None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "jackson_lifecycle",
    }


# ---------------------------------------------------------------------------
# Tyk lifecycle scraper
#
# Tyk isn't on endoflife.date, but publishes an LTS support table in its docs.
# We parse the source markdown from the public tyk-docs GitHub repo (more stable
# than the rendered page). Table columns:
#   Version | Full Support Window | Maintenance Support Window | Completely Unsupported From
# Effective EOL = last day of the month BEFORE "Completely Unsupported From"
# (5.8 "unsupported from July 2027" -> supported through 2027-06-30). Dashboard /
# MDCB / Pump track the Gateway LTS line, so their entries use the Gateway
# major.minor (e.g. 5.8).
# ---------------------------------------------------------------------------

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
        cells = [c.strip() for c in s.strip("|").split("|")]
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
        md = resp.read().decode("utf-8", "replace")
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


PROVIDERS = {
    "endoflife_date":     _provider_endoflife_date,
    "aws_rds_scrape":     _provider_aws_rds_scrape,
    "aws_sdk_lifecycle":  _provider_aws_sdk_lifecycle,
    "jackson_lifecycle":  _provider_jackson_lifecycle,
    "maven_central":      _provider_maven_central,
    "npm_registry":       _provider_npm_registry,
    "manual":             _provider_manual,
    "tyk_lifecycle":      _provider_tyk_lifecycle,
}


def check_product(entry, today):
    """Dispatch a config entry to its data-source provider.

    Returns None for non-product entries (those carrying a '_section' marker
    used as visual dividers in the config). Otherwise the provider is
    selected via entry["source"]; defaults to "endoflife_date" when not
    specified. Unknown sources produce an error-shaped result.
    """
    if entry.get("_section"):
        return None
    source = entry.get("source", "endoflife_date")
    provider = PROVIDERS.get(source)
    if provider is None:
        return _error_result(entry, f"Unknown source '{source}'. Known: {sorted(PROVIDERS)}")
    return provider(entry, today)


# ---------------------------------------------------------------------------
# Source labels (shared by both formatters)
# ---------------------------------------------------------------------------

_SOURCE_LABELS = {
    "endoflife_date":    "endoflife.date",
    "aws_rds_scrape":    "AWS docs",
    "aws_sdk_lifecycle": "AWS SDK lifecycle",
    "jackson_lifecycle": "FasterXML wiki",
    "maven_central":     "Maven Central",
    "npm_registry":      "npm",
    "manual":            "manual",
    "tyk_lifecycle":     "Tyk docs",
}


def _source_label(r):
    """Render a result's data-source key as a human-readable label."""
    key = r.get("source", "endoflife_date")
    return _SOURCE_LABELS.get(key, key)


def _source_url_for(r):
    """Compute the upstream-docs URL backing this result, or None.

    Centralised so adding a provider is one branch here rather than touching
    every result-building site. The 'product' field is the per-provider
    handle (endoflife.date slug, AWS engine name, "group:artifact" for
    Maven Central, etc.) — see each provider for what it sets.
    """
    src = r.get("source")
    product = r.get("product") or ""

    if src == "endoflife_date":
        return f"https://endoflife.date/{product}" if product else None
    if src == "aws_rds_scrape":
        return _AWS_DOCS_URLS.get(product)
    if src == "aws_sdk_lifecycle":
        return _AWS_SDK_URL
    if src == "jackson_lifecycle":
        return _JACKSON_WIKI_URL
    if src == "maven_central" and ":" in product:
        group, artifact = product.split(":", 1)
        return f"https://central.sonatype.com/artifact/{group}/{artifact}"
    if src == "npm_registry" and product:
        return f"https://www.npmjs.com/package/{product}"
    if src == "manual":
        return r.get("reference_url")
    if src == "tyk_lifecycle":
        return "https://tyk.io/docs/developer-support/release-types/long-term-support"
    return None


def _source_html(r):
    """Render the source as a clickable link in HTML reports (label-only fallback)."""
    label = _source_label(r)
    url = _source_url_for(r)
    if url and url.startswith(("https://", "http://")):
        safe = html.escape(url, quote=True)
        return (
            f'<a href="{safe}" target="_blank" rel="noopener" '
            f'style="color:#1565c0;text-decoration:none">{label}</a>'
        )
    return label


# ---------------------------------------------------------------------------
# Categorisation (shared by both formatters)
# ---------------------------------------------------------------------------

def _categorise(results, thresholds):
    """Split results into eol / approaching / ok / error / untracked buckets."""
    max_threshold = max(thresholds) if thresholds else 90
    eol, approaching, ok, errors, untracked = [], [], [], [], []

    for r in results:
        status = r["status"]
        if status == "error":
            errors.append(r)
        elif status == "untracked":
            untracked.append(r)
        elif status == "eol":
            eol.append(r)
        elif status == "approaching" and r["days_remaining"] is not None and r["days_remaining"] <= max_threshold:
            approaching.append(r)
        else:
            ok.append(r)

    approaching.sort(key=lambda x: x["days_remaining"])
    return eol, approaching, ok, errors, untracked, max_threshold


# ---------------------------------------------------------------------------
# Plain-text report
# ---------------------------------------------------------------------------

def _append_version_info(lines, r):
    """Append in-use, latest-patch, and latest-cycle lines to the report."""
    if r.get("in_use_release_date"):
        lines.append(f"    In use: {r['version']} (released {r['in_use_release_date']})")

    if r.get("latest_patch"):
        patch_line = f"    Latest patch: {r['latest_patch']}"
        if r.get("latest_patch_date"):
            patch_line += f" (released {r['latest_patch_date']})"
        lines.append(patch_line)

    if r.get("latest_cycle"):
        cycle = r["latest_cycle"]
        cycle_v = r.get("latest_cycle_version")
        if r.get("on_latest_cycle"):
            cycle_line = f"    Latest cycle: {cycle} (you are on the latest)"
        elif cycle_v and cycle_v != cycle:
            cycle_line = f"    Latest cycle: {cycle} -> {cycle_v}"
            if r.get("latest_cycle_release_date"):
                cycle_line += f" (released {r['latest_cycle_release_date']})"
        else:
            cycle_line = f"    Latest cycle: {cycle}"
        lines.append(cycle_line)


def format_report_text(results, thresholds, today):
    """Format results into a readable plain-text report.

    Returns (report_text, has_alerts).
    """
    eol_items, approaching_items, ok_items, error_items, untracked_items, max_t = _categorise(results, thresholds)

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

    if untracked_items:
        lines += ["", "?? UNTRACKED (no EOL source)", "-" * 42]
        for r in untracked_items:
            lines.append(f"  * {r['label']}  -  {r['message']}  [{_source_label(r)}]")
            _append_version_info(lines, r)

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
    "untracked":   {"bg": "#eceff1", "badge_bg": "#607d8b", "badge_text": "#fff"},
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
    if bucket == "untracked":
        return _badge("untracked", "UNTRACKED")
    return _badge("error", "ERROR")


def _html_table_rows(items, status_key):
    """Generate <tr> elements for a list of result items."""
    bg = _STATUS_COLOURS.get(status_key, _STATUS_COLOURS["error"])["bg"]
    rows = []
    for r in items:
        patch = r.get("latest_patch") or "-"
        if r.get("latest_patch_date"):
            patch += f'<br><span style="color:#888;font-size:12px">released {r["latest_patch_date"]}</span>'

        eol_display = r.get("eol_date") or "-"

        product_cell = r["label"]
        if r.get("in_use_release_date"):
            product_cell += (
                f'<br><span style="color:#888;font-weight:normal;font-size:12px">'
                f'in-use {r["version"]} released {r["in_use_release_date"]}</span>'
            )

        rows.append(
            f'<tr style="background-color:{bg}">'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0;font-weight:bold">{product_cell}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{_status_label(r, status_key)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{r["message"]}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{eol_display}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{patch}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{_cycle_cell(r)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0;color:#555;font-size:12px">{_source_html(r)}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def format_report_html(results, thresholds, today):
    """Format results into an inline-styled HTML report.

    Returns (html_string, has_alerts).
    """
    eol_items, approaching_items, ok_items, error_items, untracked_items, max_t = _categorise(results, thresholds)
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
    if untracked_items:
        all_rows += _html_table_rows(untracked_items, "untracked")

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


def _project_from_base(base):
    """Derive the project segment from an html_file base name.

    'eol_report_a'  -> 'a'
    'eol_report'      -> 'default'
    anything else     -> the base itself (best effort)
    """
    if base == "eol_report":
        return "default"
    if base.startswith("eol_report_"):
        return base[len("eol_report_"):] or "default"
    return base or "default"


def _notify_html_file(report_html, notif_config, **_kwargs):
    """Write the HTML report under reports/<project>/<year>/<month>/<day>/.

    The project is derived from the configured path's base name; the dated
    folders and the filename's timestamp both come from the current local time,
    so the folder path and filename always agree. Each run produces a uniquely
    named file, e.g.
    reports/a/2026/05/03/eol_report_a_2026-05-03_1430.html.
    """
    path = notif_config.get("path", "eol_report.html")
    base, ext = os.path.splitext(os.path.basename(path))
    now = datetime.now()
    out_dir = os.path.join(
        "reports", _project_from_base(base),
        now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"),
    )
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{base}_{now:%Y-%m-%d_%H%M}{ext}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_html)
    logger.info("HTML report written to %s", path)


def _notify_sns(report_text, subject, notif_config, runtime_overrides=None, **_kwargs):
    """Publish the plain-text report to an SNS topic.

    Topic ARN resolution: notif.topic_arn > event override > SNS_TOPIC_ARN env var.
    """
    import boto3

    overrides = runtime_overrides or {}
    topic_arn = (
        notif_config.get("topic_arn")
        or overrides.get("sns_topic_arn")
        or os.environ.get("SNS_TOPIC_ARN")
    )
    if not topic_arn:
        logger.error("SNS notification skipped: no topic_arn in config, event, or SNS_TOPIC_ARN env var")
        return
    sns = boto3.client("sns")
    sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=report_text)
    logger.info("SNS notification sent to %s", topic_arn)


def _notify_ses(report_html, subject, notif_config, runtime_overrides=None, **_kwargs):
    """Send the HTML report as an email via SES.

    Address resolution: notif fields > event overrides > SES_FROM_EMAIL/SES_TO_EMAILS env vars.
    """
    import boto3

    overrides = runtime_overrides or {}
    from_email = (
        notif_config.get("from_email")
        or overrides.get("ses_from_email")
        or os.environ.get("SES_FROM_EMAIL")
    )
    to_emails = notif_config.get("to_emails") or []
    if not to_emails:
        raw_to = overrides.get("ses_to_emails") or os.environ.get("SES_TO_EMAILS")
        if raw_to:
            to_emails = [e.strip() for e in raw_to.split(",")]

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


def send_notifications(config, report_text, report_html, subject, runtime_overrides=None):
    """Dispatch the report to every notification channel listed in config.

    *runtime_overrides* carries per-invocation routing values (e.g. SNS topic ARN
    supplied by EventBridge so each project routes to its own topic).
    """
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
                runtime_overrides=runtime_overrides,
            )
        except Exception as exc:
            logger.error("Notification '%s' failed: %s", ntype, exc)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """Entry point for AWS Lambda.

    The invocation event may carry per-project overrides:
      - config_key      — S3 key of the project's config file
      - project         — display name (used in the subject line and logs)
      - sns_topic_arn   — destination SNS topic for this project
      - ses_from_email  — sender for SES notifications
      - ses_to_emails   — comma-separated recipients for SES notifications

    All overrides fall back to the existing env vars when absent.
    """
    today = date.today()
    event = event or {}

    config_key = event.get("config_key")
    project = event.get("project")
    runtime_overrides = {
        k: v for k, v in {
            "sns_topic_arn":  event.get("sns_topic_arn"),
            "ses_from_email": event.get("ses_from_email"),
            "ses_to_emails":  event.get("ses_to_emails"),
        }.items() if v
    }

    if project:
        logger.info("Running EOL check for project '%s' (config=%s)", project, config_key or "<env default>")

    config = load_config_from_s3(config_key)
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])
    notify_when = config.get("notify_when", "always")  # "always" | "alerts_only"

    logger.info("Checking %d products for EOL status", len(products))

    results = [r for r in (check_product(entry, today) for entry in products) if r is not None]

    for r in results:
        logger.info("%s: %s", r["label"], r["message"])

    report_text, has_alerts = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    should_notify = notify_when == "always" or has_alerts

    if should_notify:
        prefix = "EOL ALERT" if has_alerts else "EOL Report"
        proj_tag = f" [{project}]" if project else ""
        subject = f"[{prefix}]{proj_tag} Software End-of-Life Status - {today}"
        send_notifications(config, report_text, report_html, subject,
                           runtime_overrides=runtime_overrides)
    else:
        logger.info("No alerts and notify_when=alerts_only — skipping notification")

    return {
        "statusCode": 200,
        "project": project,
        "checked": len(results),
        "has_alerts": has_alerts,
        "notified": should_notify,
    }


# ---------------------------------------------------------------------------
# Local testing  (python lambda_function.py [config.json])
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "eol_config.a.json"
    config = load_config_from_file(config_path)

    today = date.today()
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])

    results = [r for r in (check_product(entry, today) for entry in products) if r is not None]
    report_text, has_alerts = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    prefix = "EOL ALERT" if has_alerts else "EOL Report"
    subject = f"[{prefix}] Software End-of-Life Status - {today}"

    send_notifications(config, report_text, report_html, subject)
