"""AWS SDK lifecycle scraper.

AWS publishes a per-major-version lifecycle phase for every SDK at the
version-support-matrix page. Phases:
  "Developer Preview"        -> ok        (not for prod, but no EOL)
  "General Availability"     -> ok
  "Maintenance Announcement" -> approaching  (EOL coming, ~6mo to maintenance)
  "Maintenance"              -> approaching  (limited fixes, ~12mo to EOL)
  "End-of-Support"           -> eol
No specific EOL dates are published in the matrix — only the GA date.
"""

import re
import urllib.request

from ..core import _HtmlTableExtractor, _error_result, logger, read_response_bytes

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
        html_text = read_response_bytes(resp).decode("utf-8", errors="replace")

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


SOURCE = "aws_sdk_lifecycle"
LABEL = "AWS SDK lifecycle"
provider = _provider_aws_sdk_lifecycle


def url_for(r):
    return _AWS_SDK_URL
