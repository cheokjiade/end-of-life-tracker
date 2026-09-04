"""Regression tests for the HTML status badge rendering (eoltracker/report.py).

An undated `approaching` row (production-reachable via the AWS SDK
lifecycle provider's Maintenance phase, which publishes no EOL date)
must render a sane badge instead of a "Noned remaining" literal. The
plain-text report is pinned too: it never rendered days-remaining and
must stay that way. Standalone assertion script: no pytest, no network.

Run from the repository root:  python tests/test_report_badge.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from eoltracker.report import _status_label, format_report_html, format_report_text

TODAY = date(2026, 7, 24)
TH = [30, 60, 90]

# Undated approaching row, mirroring the aws_sdk_lifecycle Maintenance
# phase result shape (status approaching, no EOL date, no days remaining).
undated = {
    "label": "AWS SDK for Java 1.x", "product": "aws-sdk-java",
    "version": "1.x", "status": "approaching",
    "message": "Maintenance phase (SDK went GA on 2021-01-14)",
    "days_remaining": None, "eol_date": None, "support_date": None,
    "latest_patch": None, "latest_patch_date": None,
    "source": "aws_sdk_lifecycle",
}

# Dated approaching rows keep the days-remaining badge.
dated = dict(undated, label="Python 3.9", product="python", version="3.9",
             message="Support for 3.9 ends 2025-10-31", days_remaining=42)

html, alerts = format_report_html([undated, dated], TH, TODAY)
assert alerts is True
assert "Noned remaining" not in html, "undated badge leaked a None literal"
assert "None" not in html, html
assert "AT RISK (no date)" in html, "undated badge text missing"
assert "42d remaining" in html, "dated badge lost its days-remaining text"

badge = _status_label(undated, "approaching")
assert ">AT RISK (no date)<" in badge, badge
assert "None" not in badge, badge
badge = _status_label(dated, "approaching")
assert ">42d remaining<" in badge, badge

# The plain-text report is unaffected: no days-remaining rendering, no None.
text, text_alerts = format_report_text([undated], TH, TODAY)
assert text_alerts is True
assert "None" not in text, text
assert "Maintenance phase (SDK went GA on 2021-01-14)" in text, text
assert "AT RISK (no date)" not in text, "HTML badge text leaked into text report"

print("OK test_report_badge")
