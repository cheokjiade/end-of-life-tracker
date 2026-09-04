"""Output-boundary control tests: CSV formula smuggling and text-report forgery.

Covers the two verified failure modes from the security audit:

- The inventory CSV renderer neutralized formula triggers only at index 0
  of a cell, so a value with an embedded newline could smuggle a live
  formula onto a second logical row in a CSV parser that does not honour
  quoting; control characters could corrupt rows.
- The plain-text tracker report embedded labels/messages/notes/versions
  raw, so injected newlines could forge the report's own section
  structure and ANSI/control bytes could color or corrupt terminals.

Also pins the unchanged, already-escaped HTML boundary (both trackers)
and the Markdown renderer's control-character handling. Standalone
assertion script: no pytest, no network.

Run from the repository root:  python tests/test_output_controls.py
"""
import csv
import html
import io
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_HELPER_DIR = ROOT / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

from eol_inventory.report_writer import (
    build_inventory_view,
    render_csv,
    render_markdown,
)
from eoltracker.report import format_report_html, format_report_text

TODAY = date(2026, 7, 24)
TH = [30, 60, 90]


def _config():
    """Synthetic single-product config (maven -> java ecosystem)."""
    return {
        "alert_thresholds_days": TH,
        "notifications": [{"type": "console"}],
        "products": [
            {"source": "maven_central", "group": "io.netty",
             "artifact": "netty-codec-http", "version": "4.1.111.Final",
             "label": "Netty Codec HTTP"},
        ],
    }


def _csv_rows(config):
    return list(csv.reader(io.StringIO(
        render_csv(build_inventory_view(config)))))


def _result(**overrides):
    """Minimal provider-shaped result for the tracker formatters."""
    result = {
        "label": "Product", "product": "product", "version": "1.0.0",
        "status": "eol", "message": "supported until 2026-01-01",
        "eol_date": "2026-01-01", "days_remaining": -30,
        "source": "manual",
    }
    result.update(overrides)
    return result


# ---------------------------------------------------------------------------
# Inventory CSV: formula smuggling via embedded newlines / control chars
# ---------------------------------------------------------------------------

def test_csv_newline_prefixed_formula_neutralized():
    config = _config()
    config["products"][0]["label"] = "benign\n=cmd('calc')"
    name = _csv_rows(config)[1][3]
    assert name == "benign\n'=cmd('calc')", name
    # The second logical row no longer begins with a live formula trigger.
    assert not name.split("\n")[1].startswith("="), name


def test_csv_neutralizes_triggers_after_cr_and_mixed_newlines():
    config = _config()
    config["products"][0]["label"] = "a\r\n=1\r@2\n-3"
    name = _csv_rows(config)[1][3]
    assert name == "a\n'=1\n'@2\n'-3", name
    for line in name.split("\n")[1:]:
        assert not line.startswith(("=", "+", "-", "@", "\t")), name


def test_csv_leading_trigger_chars_still_neutralized():
    config = _config()
    config["products"][0]["label"] = "@SUM(A1)"
    config["products"][0]["version"] = "\t1.0"
    rows = _csv_rows(config)
    assert rows[1][3] == "'@SUM(A1)", rows[1][3]
    assert rows[1][4] == "'\t1.0", rows[1][4]


def test_csv_strips_control_characters():
    config = _config()
    config["products"][0]["label"] = "net\x00ty\x07\x1b[31mred"
    config["products"][0]["version"] = "1.\x7f0"
    rows = _csv_rows(config)
    assert rows[1][3] == "nettyred", rows[1][3]
    assert rows[1][4] == "1.0", rows[1][4]


def test_csv_benign_values_byte_identical():
    config = _config()
    config["products"][0]["version"] = "1.2.3-beta"
    config["products"][0]["note"] = ">=3.8,<3.12 works; pinned in pom.xml:12"
    rows = _csv_rows(config)
    assert rows[1] == [
        "product", "java", "maven_central", "Netty Codec HTTP",
        "1.2.3-beta", "tracked", "", "not recorded",
        "note=>=3.8,<3.12 works; pinned in pom.xml:12"], rows[1]


def test_csv_benign_multiline_cell_preserved():
    config = _config()
    config["products"][0]["note"] = "line one\nline two"
    details = _csv_rows(config)[1][8]
    assert details == "note=line one\nline two", details


# ---------------------------------------------------------------------------
# Inventory Markdown: control bytes stripped, table rows stay intact
# ---------------------------------------------------------------------------

def test_markdown_strips_ansi_and_controls_rows_intact():
    config = _config()
    config["products"][0]["label"] = "net\x1b[31mty\x1b[0m\n| row |"
    config["products"][0]["version"] = "1.\x002.3\x7f"
    md = render_markdown(build_inventory_view(config))
    assert "\x1b" not in md and "\x00" not in md and "\x7f" not in md
    assert ("| netty \\| row \\| | 1.2.3 | maven\\_central | not recorded "
            "|  |  |") in md, md


def test_markdown_scan_date_cannot_forge_bullets():
    config = _config()
    config["_inventory"] = {"scan_date": "2026-01-01\n- Forged bullet"}
    md = render_markdown(build_inventory_view(config))
    assert not any(ln == "- Forged bullet" for ln in md.splitlines()), md
    assert "- Scan date: 2026-01-01 - Forged bullet" in md, md


def test_csv_redacts_hostile_non_string_version_spec():
    # F1 at the output boundary: a hostile non-string version_spec is
    # redacted through its string form before the CSV/Markdown cells are
    # built; a valid string spec renders byte-identically.
    config = {
        "products": [],
        "_inventory": {"warnings": [], "unmapped": [{
            "ecosystem": "java", "name": "pkg",
            "version_spec": {"a": "https://u:p@host.invalid/x"},
            "reason": "hostile spec shape", "found_in": []}]},
    }
    rows = list(csv.reader(io.StringIO(
        render_csv(build_inventory_view(config)))))
    assert rows[1][4] == "{'a': 'https://<redacted>@host.invalid/x'}", \
        rows[1][4]
    md = render_markdown(build_inventory_view(config))
    assert "p@host.invalid" not in md, md


# ---------------------------------------------------------------------------
# Tracker plain-text report: no forged structure, no terminal escapes
# ---------------------------------------------------------------------------

def test_text_label_newline_cannot_forge_sections():
    # status "ok" so the real "-- No Immediate Concerns" header exists and
    # a forged duplicate would push the count to 2.
    text, alerts = format_report_text([
        _result(status="ok", days_remaining=10,
                label="benign\n=== END OF LIFE ===",
                message="ok\n-- No Immediate Concerns\n"
                        "!! ALREADY END OF LIFE")], TH, TODAY)
    lines = text.splitlines()
    assert alerts is False
    assert ("  * benign === END OF LIFE ===  -  ok -- No Immediate "
            "Concerns !! ALREADY END OF LIFE  [manual]") in lines, text
    assert not any(ln.startswith("=== END OF LIFE") for ln in lines), text
    assert not any(ln.startswith("!! ALREADY END OF LIFE")
                   for ln in lines), text
    assert sum(1 for ln in lines
               if ln == "-- No Immediate Concerns") == 1, text


def test_text_strips_ansi_escapes_from_fields():
    text, _ = format_report_text([
        _result(label="\x1b[31mEvil\x1b[0m",
                message="see \x1b]0;title\x07docs",
                support_message="\x1b[1msupport",
                policy_note="\x1b[2Jpolicy",
                version="\x1b[31m9.9.9\x1b[0m",
                in_use_release_date="2020-01-01")], TH, TODAY)
    assert "\x1b" not in text, text
    for fragment in ("[31m", "[0m", "[1m", "[2J"):
        assert fragment not in text, text
    assert "Evil" in text, text
    assert "In use: 9.9.9 (released 2020-01-01)" in text, text
    assert "see docs" in text, text
    assert "Policy: policy" in text, text
    assert "support" in text, text


def test_text_collapses_cr_and_mixed_newlines():
    text, _ = format_report_text([
        _result(label="alpha\r\nbeta\rgamma\ndelta",
                message="m\r\nm2")], TH, TODAY)
    assert "\r" not in text, text
    assert "alpha beta gamma delta" in text, text
    assert "    m m2" in text, text


def test_text_drops_control_characters():
    text, _ = format_report_text([
        _result(label="pro\x00duct\x7f\x9b[31m")], TH, TODAY)
    assert "\x00" not in text and "\x7f" not in text and "\x9b" not in text, \
        text
    # The 8-bit CSI byte is dropped with the other C1 controls; the rest is
    # inert printable text, not a re-interpretable escape.
    assert "product[31m" in text, text


def test_text_benign_values_byte_identical():
    text, alerts = format_report_text([
        _result(label="Spring Security 6.3", product="spring-security",
                version="6.3.1", status="ok", days_remaining=300,
                message=">=3.8,<3.12 supported; EOL 2027-06-30",
                support_message="Active support until 2027-06-01",
                policy_note="Only newest release supported.",
                latest_patch="6.3.1", latest_patch_date="2026-01-15")],
        TH, TODAY)
    lines = text.splitlines()
    assert alerts is False
    assert ("  * Spring Security 6.3  -  >=3.8,<3.12 supported; EOL "
            "2027-06-30  [manual]") in lines, text
    assert "    Active support until 2027-06-01" in lines, text
    assert "    Policy: Only newest release supported." in lines, text
    assert "    Latest patch: 6.3.1 (released 2026-01-15)" in lines, text
    assert "'" not in text, text


# ---------------------------------------------------------------------------
# HTML boundaries stay escaped and unchanged for the same inputs
# ---------------------------------------------------------------------------

def test_tracker_html_unchanged_for_hostile_fields():
    rendered, _ = format_report_html([
        _result(label="benign\n=== END OF LIFE ===",
                message="<b>x</b>\x1b[31m")], TH, TODAY)
    assert html.escape("benign\n=== END OF LIFE ===") in rendered
    assert "&lt;b&gt;x&lt;/b&gt;" in rendered
    assert "<b>x</b>" not in rendered


TESTS = [
    test_csv_newline_prefixed_formula_neutralized,
    test_csv_neutralizes_triggers_after_cr_and_mixed_newlines,
    test_csv_leading_trigger_chars_still_neutralized,
    test_csv_strips_control_characters,
    test_csv_benign_values_byte_identical,
    test_csv_benign_multiline_cell_preserved,
    test_markdown_strips_ansi_and_controls_rows_intact,
    test_markdown_scan_date_cannot_forge_bullets,
    test_csv_redacts_hostile_non_string_version_spec,
    test_text_label_newline_cannot_forge_sections,
    test_text_strips_ansi_escapes_from_fields,
    test_text_collapses_cr_and_mixed_newlines,
    test_text_drops_control_characters,
    test_text_benign_values_byte_identical,
    test_tracker_html_unchanged_for_hostile_fields,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    if failed:
        print(f"{failed} test(s) failed")
        return 1
    print("OK test_output_controls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
