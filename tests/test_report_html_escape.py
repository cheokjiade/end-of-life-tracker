import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import html as _html
from datetime import date

from eoltracker.parsers import check_product
from eoltracker.report import format_report_html

TODAY = date(2026, 8, 27)
TH = [30, 60, 90]


def poisioned(**over):
    """A result dict whose every dynamic field carries an injection payload."""
    r = {
        "label": "PRODLBL <script>a()</script>",
        "product": "prod",
        "version": "1.<b>2</b>",
        "status": "ok",
        "message": "MSG <img src=x onerror=a()>",
        "days_remaining": 400,
        "eol_date": "2030-01-<i>eol</i>",
        "latest_patch": "1.2.<s>p</s>",
        "latest_patch_date": "2026-01-<u>pd</u>",
        "latest_cycle": "3.<q>c</q>",
        "latest_cycle_version": "4.<v>cv</v>",
        "on_latest_cycle": False,
        "latest_cycle_release_date": "2026-02-<w>cd</w>",
        "in_use_release_date": "2025-03-<z>rd</z>",
        "support_message": "SUP <script>s()</script>",
        "policy_note": "POL <em>n</em>",
        "source": "manual",
    }
    r.update(over)
    return r


def esc(s):
    return _html.escape(str(s), quote=True)


# ---------------------------------------------------------------------------
# 1. Every dynamic field of every bucket is escaped; renderer markup intact.
# ---------------------------------------------------------------------------
results = [
    poisioned(status="eol", days_remaining=-10),
    poisioned(status="approaching", days_remaining=20),
    poisioned(),                                        # ok bucket
    poisioned(status="error"),                          # error bucket
    poisioned(status="untracked", days_remaining=None), # untracked bucket
]
out, alerts = format_report_html(results, TH, TODAY)

assert alerts is True

RAW_PAYLOADS = [
    "<script>", "<img ", "<b>2", "<i>eol", "<s>p</s>", "<u>pd",
    "<q>c</q>", "<v>cv", "<w>cd", "<z>rd", "<em>n",
]
for frag in RAW_PAYLOADS:
    assert frag not in out, f"unescaped payload leaked into HTML: {frag}"

# Field-by-field: the escaped form of each value is rendered somewhere.
for value in [
    results[0]["label"], results[0]["message"], results[0]["version"],
    results[0]["eol_date"], results[0]["latest_patch"],
    results[0]["latest_patch_date"], results[0]["latest_cycle"],
    results[0]["latest_cycle_version"], results[0]["latest_cycle_release_date"],
    results[0]["in_use_release_date"], results[0]["support_message"],
    results[0]["policy_note"],
]:
    assert esc(value) in out, f"escaped form of {value!r} missing from HTML"

# Renderer-owned entities/markup survive: arrow between cycles, info marker,
# muted sub-line spans, all five bucket backgrounds (layout preserved).
assert "&rarr;" in out
assert "&#9432;" in out
for bg in ("#fce4e4", "#fff8e1", "#e8f5e9", "#ede7f6", "#eceff1"):
    assert out.count(f'style="background-color:{bg}"') >= 1, f"{bg} row missing"

# One info marker per non-error row carrying a policy_note (4 here).
assert out.count("&#9432;") == 4, out.count("&#9432;")

# Error rows keep the text-report behaviour: message only, no notes.
err_row = out.split('background-color:#ede7f6"', 1)[1].split("</tr>", 1)[0]
assert "Policy:" not in err_row and "&#9432;" not in err_row

# Status badges remain intact numerics even with poisoned neighbours.
assert ">20d remaining</span>" in out
assert ">END OF LIFE</span>" in out
assert ">OK</span>" not in out  # a numeric days-remaining badge is rendered instead
assert "400d remaining</span>" in out
assert ">UNTRACKED</span>" in out
assert ">CHECK FAILED</span>" in out

# ---------------------------------------------------------------------------
# 2. Layout integrity: structural tag balance and column skeleton unchanged.
# ---------------------------------------------------------------------------
assert out.startswith("<!DOCTYPE html>")
assert out.count("<th ") == 7 and out.count("</th>") == 7
assert out.count("<td ") == out.count("</td>")
assert out.count("<tr") == out.count("</tr>")
for column in ("Product", "Status", "Details", "EOL Date",
               "Latest Patch", "Latest Cycle", "Source"):
    assert column in out

# Clean row (no sub-lines) vs poisoned row (same fields => same sub-lines):
# poisoning must not add rows or cells.
clean_out, _ = format_report_html([poisioned()], TH, TODAY)
dirty_out, _ = format_report_html([poisioned(message="plain message")], TH, TODAY)
assert clean_out.count("<td ") == dirty_out.count("<td ")
assert clean_out.count("<br>") == dirty_out.count("<br>")

# Missing optional fields degrade to placeholders, not holes.
bare_out, _ = format_report_html([
    poisioned(latest_patch=None, latest_patch_date=None,
              latest_cycle=None, latest_cycle_release_date=None,
              eol_date=None)
], TH, TODAY)
assert bare_out.count(">-</td>") == 3, bare_out.count(">-</td>")

# Cycle fallback when latest_cycle_version is missing -> renderer "?".
q_out, _ = format_report_html([poisioned(latest_cycle_version=None,
                                         latest_cycle_release_date=None)], TH, TODAY)
assert "&rarr; ?</td>" in q_out

# on_latest_cycle branch.
lat_out, _ = format_report_html([poisioned(on_latest_cycle=True)], TH, TODAY)
assert "(latest)" in lat_out and "&rarr;" not in lat_out

# ---------------------------------------------------------------------------
# 3. Summary banner, header counts, footer threshold are escaped uniformly.
# ---------------------------------------------------------------------------
hdr_out, alerts_empty = format_report_html(results=[], thresholds=TH,
                                           today="2026-<b>08</b>-27")
assert alerts_empty is False
assert "All products are within support" in hdr_out          # static banner
assert "2026-&lt;b&gt;08&lt;/b&gt;-27" in hdr_out
assert "&lt;b&gt;" in hdr_out and "<b>" not in hdr_out
flattened = hdr_out.replace("\n", "").replace(" ", "")
assert "0productschecked</p>" in flattened

eol_banner, _ = format_report_html([poisioned(status="eol", days_remaining=-1)], TH, TODAY)
assert "1 product(s) past end of life" in eol_banner

thr_out, _ = format_report_html(results=[poisioned()], thresholds=TH, today=TODAY)
assert "Alert threshold: 90 days" in thr_out

# Footer lists every distinct source label, escaped (unknown keys fall back
# to the raw registry key, which is config-controlled).
mixed = [poisioned(source="manual"),
         poisioned(label="Other", source="rogue<u>key</u>", status="untracked")]
foot_out, _ = format_report_html(mixed, TH, TODAY)
assert "rogue&lt;u&gt;key&lt;/u&gt;" in foot_out
assert "<u>" not in foot_out

# ---------------------------------------------------------------------------
# 4. Source links: validated HTTPS only, otherwise plain escaped text.
# ---------------------------------------------------------------------------

# Valid HTTPS link renders as the standard anchor, query included and escaped
# (the anchor text is the data-source label, not the product label).
ok_src, _ = format_report_html(
    [check_product({"source": "manual", "label": "Linked",
                    "reference_url": "https://example.com/docs?a=1&b=2"}, TODAY)],
    TH, TODAY)
exp_ok = esc("https://example.com/docs?a=1&b=2")
assert f'<a href="{exp_ok}" target="_blank" rel="noopener" style="color:#1565c0;text-decoration:none">manual</a>' in ok_src

# Uppercase-scheme HTTPS still validates (urlsplit normalises schemes).
up_src, _ = format_report_html(
    [check_product({"source": "manual", "label": "U",
                    "reference_url": "HTTPS://example.com/x"}, TODAY)], TH, TODAY)
assert '<a href="HTTPS://example.com/x"' in up_src

# Quotes/angles in a URL are attribute-escaped, never executable content.
tricky = 'https://good.com/?a=1"><script>evil()</script>'
tricky_src, _ = format_report_html(
    [check_product({"source": "manual", "label": "T",
                    "reference_url": tricky}, TODAY)], TH, TODAY)
assert "<script>" not in tricky_src
assert f'<a href="{esc(tricky)}"' in tricky_src

# Malicious / invalid URLs never become links: they fall back to the
# escaped label with no <a> anywhere in the document.
BAD_URLS = [
    "http://insecure.example.com/",                     # HTTP is not allowed
    "javascript:alert(document.domain)",
    "JAVASCRIPT:alert(1)",                              # odd case still != https
    "data:text/html,<script>alert(1)</script>",
    "vbscript:x",
    "//protocol-relative.example.com/x",
    "ftp://files.example.com/pub",
    "https:///no-host.example",                          # no host component
    "https://exa mple.com/a b",                          # embedded space
    " https://example.com/leading",                     # leading whitespace
    "https://example.com/trailing ",                    # trailing whitespace
    "https://good.com/\t?q=1",                           # embedded tab
    "https://good.com/\nq",                              # embedded newline
    "https://evil.com\\@example.com/",                  # browser treats \\ as /
    "\x01https://control.char.example",                  # control char
    "",                                                  # empty
    "   ",                                               # whitespace-only
    "https://[::1",                                      # malformed IPv6
    None,                                                # non-string types
    17,
    {"href": "https://evil.example"},
]
for bad in BAD_URLS:
    r = check_product({"source": "manual", "label": "Nolink",
                       "reference_url": bad}, TODAY)
    one_out, _ = format_report_html([r], TH, TODAY)
    assert "<a " not in one_out, f"invalid URL became a link: {bad!r}"
    assert ">manual</td>" in one_out, f"label-only fallback missing for {bad!r}"

# No reference_url at all -> plain label, no link.
plain_r = check_product({"source": "manual", "label": "Plain Manual"}, TODAY)
plain_out, _ = format_report_html([plain_r], TH, TODAY)
assert "<a " not in plain_out
assert ">Plain Manual</td>" in plain_out

print("OK test_report_html_escape")
