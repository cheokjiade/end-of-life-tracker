import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from eoltracker.parsers import check_product
from eoltracker.report import format_report_text

TODAY = date(2026, 7, 24)
TH = [30, 60, 90]

# policy_note on an UNTRACKED item shows up as an ASCII "Policy:" sub-line.
r = check_product({"source": "manual", "label": "PuTTY",
                   "policy_note": "Only newest release supported."}, TODAY)
text, _ = format_report_text([r], TH, TODAY)
assert "Policy: Only newest release supported." in text, text

# Absent note -> no "Policy:" line anywhere.
r2 = check_product({"source": "manual", "label": "X"}, TODAY)
text2, _ = format_report_text([r2], TH, TODAY)
assert "Policy:" not in text2, text2

# support_message still renders (approaching item); manual-inject the field.
r3 = check_product({"source": "manual", "label": "SM", "eol_date": "2026-09-01"}, TODAY)
r3["support_message"] = "Active support until 2026-08-01 (8 days remaining)"
text3, _ = format_report_text([r3], TH, TODAY)
assert "Active support until 2026-08-01" in text3, text3

# Unknown/error tracker health is explicit and alerts instead of appearing OK.
unknown = dict(r2, status="unknown", message="Lifecycle cannot be established")
unknown_text, unknown_alert = format_report_text([unknown], TH, TODAY)
assert unknown_alert is True
assert "!! TRACKER HEALTH - UNKNOWN STATUS (1)" in unknown_text
assert "No Immediate Concerns" not in unknown_text

error_text, error_alert = format_report_text([
    dict(r2, status="error", message="source unavailable")], TH, TODAY)
assert error_alert is True and "!! TRACKER HEALTH - CHECK ERRORS (1)" in error_text

undated = dict(r2, status="approaching", days_remaining=None,
               message="maintenance phase")
undated_text, undated_alert = format_report_text([undated], TH, TODAY)
assert undated_alert is True and "APPROACHING END OF LIFE" in undated_text

print("OK test_policy_text")
