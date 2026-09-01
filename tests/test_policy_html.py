import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from eoltracker.parsers import check_product
from eoltracker.report import format_report_html

TODAY = date(2026, 7, 24)
TH = [30, 60, 90]

# policy_note renders with the info marker and is HTML-escaped (untrusted free text).
r = check_product({"source": "manual", "label": "T",
                   "policy_note": "a < b & <script>x</script>"}, TODAY)
out, _ = format_report_html([r], TH, TODAY)
assert "&#9432;" in out, "info marker missing"
assert "&lt;script&gt;" in out, "note not escaped"
assert "<script>" not in out, "unescaped note leaked into HTML"

# Provider messages, labels, versions, and release fields are untrusted too.
hostile = check_product({"source": "manual", "label": "<script>label</script>"}, TODAY)
hostile.update({
    "message": '<img src=x onerror="alert(1)">',
    "version": "<b>1</b>",
    "latest_patch": "<i>2</i>",
    "latest_patch_date": "<u>today</u>",
    "in_use_release_date": "<em>yesterday</em>",
    "latest_cycle": "<x>",
    "latest_cycle_version": "<y>",
})
hostile_html, _ = format_report_html([hostile], TH, TODAY)
for raw in ("<script>", "<img", "<b>", "<i>", "<u>", "<em>", "<x>", "<y>"):
    assert raw not in hostile_html, (raw, hostile_html)
assert "&lt;script&gt;label&lt;/script&gt;" in hostile_html
assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in hostile_html

# Absent note -> no info marker.
r2 = check_product({"source": "manual", "label": "X"}, TODAY)
out2, _ = format_report_html([r2], TH, TODAY)
assert "&#9432;" not in out2, out2

# support_message now appears in HTML (previously dropped by the HTML formatter).
r3 = check_product({"source": "manual", "label": "SM"}, TODAY)
r3["support_message"] = "Active support until 2027-01-01 (161 days remaining)"
out3, _ = format_report_html([r3], TH, TODAY)
assert "Active support until 2027-01-01" in out3, out3

# An error row does NOT render notes (consistent with the text formatter / spec).
r_err = check_product({"source": "nope", "policy_note": "should not show"}, TODAY)
out_err, _ = format_report_html([r_err], TH, TODAY)
assert "&#9432;" not in out_err, "policy_note leaked into an error row"

unknown = dict(r2, status="unknown", message="Lifecycle cannot be established")
unknown_html, unknown_alert = format_report_html([unknown], TH, TODAY)
assert unknown_alert is True
assert "UNKNOWN" in unknown_html
assert "require lifecycle review" in unknown_html
assert "All products are within support" not in unknown_html

print("OK test_policy_html")
