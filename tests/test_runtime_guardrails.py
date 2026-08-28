"""Network-free runtime guardrail tests (issue #9 remediation, audit R-05).

Standalone assertion script (repo convention: no framework, no network).
Covers the three containment layers added on top of eoltracker.validation:

  1. config loads (local file) reject invalid top-level/runtime shapes
     before any provider runs (enforce_valid_config);
  2. per-product structural failures convert to normalized error rows at
     the check_product dispatch boundary, without calling the provider;
  3. unexpected provider exceptions are isolated per entry as error results
     while valid entries continue and section dividers are preserved.
"""

import json
import io
import logging
import os
import subprocess
import sys
import tempfile
import types
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.validation import (
    ConfigValidationError,
    enforce_valid_config,
    product_entry_errors,
    validate_config,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 8, 27)
TMPDIR = tempfile.mkdtemp()


def errors(results):
    return [r for r in results if r["severity"] == "error"]


def by_path(results, path):
    return [r for r in results if r["path"] == path]


from eoltracker.core import _error_result
from eoltracker import handler as handler_mod
from eoltracker.handler import load_config_from_file
from eoltracker.parsers import PROVIDERS, SOURCE_LABELS, check_product
from eoltracker.report import format_report_html, format_report_text


def tmpfile(name, data):
    path = os.path.join(TMPDIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data))
    return path


def loads_ok(data):
    """load_config_from_file on a temp file; asserts nothing was rejected."""
    return load_config_from_file(tmpfile("ok.json", data))


def load_rejected(data):
    """load_config_from_file raises ConfigValidationError with path context."""
    try:
        loads_ok(data)
    except ConfigValidationError as exc:
        return exc
    raise AssertionError(f"expected ConfigValidationError for {data}")


# --- loaders enforce fatal top-level/runtime shapes ---------------------------
sample = loads_ok(json.load(open(os.path.join(ROOT, "eol_config.sample.json"))))
assert isinstance(sample.get("products"), list), "sample template must load"

for bad_root in ([1, 2], "nope", 7, None):
    exc = load_rejected(bad_root)
    assert any(f["path"] == "config" for f in exc.findings), (bad_root, exc)
    assert exc.findings, "ConfigValidationError must carry .findings"

for bad_products in ({}, {"products": "nope"}, {"products": []},
                     {"products": {}}):
    exc = load_rejected(bad_products)
    assert any(f["path"] == "products" for f in exc.findings), (bad_products, exc)

section_only = load_rejected({"products": [
    {"_section": "Databases"}, {"_section": "Libraries"},
]})
assert any(f["path"] == "products" for f in section_only.findings), section_only

one = [{"label": "A", "source": "manual"}]
for bad_thresholds in (
        [], [0], [-5], ["soon"], [True], [float("inf")], [float("nan")],
        {}, "soon"):
    exc = load_rejected({"alert_thresholds_days": bad_thresholds, "products": one})
    assert any(f["path"] == "alert_thresholds_days" for f in exc.findings), \
        (bad_thresholds, exc)

exc = load_rejected({"notify_when": "sometimes", "products": one})
assert any(f["path"] == "notify_when" for f in exc.findings), exc

for bad_channels in (
    "console",                             # not a list
    [{"type": "slack"}],                   # unsupported type
    [{"type": "html_file", "path": 42}],
    [{"type": "sns", "topic_arn": 7}],
    [{"type": "ses", "to_emails": "team@example.com"}],
):
    exc = load_rejected({"products": one, "notifications": bad_channels})
    assert any(f["path"].startswith("notifications")
               for f in exc.findings), (bad_channels, exc)

# exception message carries the offending field paths (diagnostics requirement)
exc = load_rejected({"notify_when": "?", "alert_thresholds_days": "soon",
                     "products": one})
assert "notify_when" in str(exc) and "alert_thresholds_days" in str(exc), exc
assert exc.findings and all(f["severity"] == "error" for f in exc.findings)

# --- non-fatal findings are logged-and-carried, never rejected ----------------
loads_ok({"products": [
    {"label": "A", "source": "manual"},
    {"label": "A", "source": "manual"},           # duplicate-label warning
    {"source": "wheel"},                          # unknown source -> error row
    {"source": "maven_central", "version": "9"},  # missing required group
    {"source": "npm_registry", "package": "x"},   # version optional for npm
    "not-an-object",                              # non-object entry
    {"_section": "-divider-", "label": "d"},      # dividers stay supported
    {"label": "H", "source": ["npm_registry"]},   # unhashable source, no raise
    {"label": "C", "source": {"k": 1}},           # dict source, no raise
]})
loads_ok({"products": one, "alert_threshold_dayz": [30]})  # typo warning only

res = enforce_valid_config({"products": [{"source": "maven_central",
                                          "artifact": "x", "version": "9"}]})
assert res and res[0]["path"] == "products[0].group", res
assert product_entry_errors({"label": "A", "source": "manual"}) == []
assert [f["path"] for f in product_entry_errors(42, index=5)] == ["products[5]"]

# unhashable source values become findings, never TypeError (registry lookup)
r = validate_config({"products": [{"source": ["npm_registry"]}]})
assert by_path(errors(r), "products[0].source") and "npm_registry" in \
    r[0]["message"], r
assert validate_config({"products": [{"source": {"k": 1}}]})[0]["severity"] == "error"

# --- check_product preserves section dividers ----------------------------------
assert check_product({"_section": "Spring Boot"}, TODAY) is None
r = check_product({"_section": ""}, TODAY)          # falsy marker = product
assert r is not None and r["status"] == "error", r  # gated: missing required
r = check_product({"_section": False, "source": "manual"}, TODAY)
assert r["status"] == "untracked"

# --- non-dict entries normalize without touching providers ---------------------
for garbage in (42, "oops", 3.14, None, [1]):
    r = check_product(garbage, TODAY)
    assert r["status"] == "error" and r["days_remaining"] is None, (garbage, r)
    assert "unusable product entry" in r["label"], (garbage, r)
    assert type(garbage).__name__ in r["label"], (garbage, r)
    assert r["source"] == "unknown" and r["message"], (garbage, r)

# --- _error_result keeps the uniform shape on hostile input --------------------
keys = {"label", "product", "version", "status", "message",
        "days_remaining", "source"}
assert keys <= set(_error_result({"label": "L", "product": "p",
                                  "version": 1, "source": "manual"}, "m"))
for base in ({}, {"source": "jackson_lifecycle"}, 42, None, "x"):
    assert keys <= set(_error_result(base, "msg")), base
assert _error_result({}, "msg")["label"] == "? ?", _error_result({}, "msg")
assert _error_result(None, "msg")["source"] == "unknown"


def _sentinel(entry, today):
    raise AssertionError("provider must not run for structurally invalid entries")


saved_providers = dict(PROVIDERS)
saved_labels = dict(SOURCE_LABELS)
try:
    # structural gate short-circuits before the provider (no network)
    PROVIDERS["aws_rds_scrape"] = _sentinel
    r = check_product({"source": "aws_rds_scrape", "engine": "mysql",
                       "version": "8"}, TODAY, index=3)
    assert r["status"] == "error" and "products[3].engine" in r["message"], r
    assert "aurora-postgresql" in r["message"], r

    PROVIDERS["maven_central"] = _sentinel
    r = check_product({"source": "maven_central", "artifact": "x",
                       "version": "9"}, TODAY, index=4)
    assert r["status"] == "error" and "products[4].group" in r["message"], r
    # without an index the path prefix degrades gracefully to "entry."
    r = check_product({"source": "maven_central", "artifact": "x"}, TODAY)
    assert "entry.group" in r["message"], r

    # policy_note rides along on gated error rows
    r = check_product({"source": "manual", "label": "N",
                       "policy_note": "psa"}, TODAY, index=0)
    assert r["policy_note"] == "psa", r

    # --- unexpected provider exceptions become normalized error rows ----------
    def _boom(entry, today):
        raise RuntimeError("http://user:sekrit-token@internal.example")

    # Register the boom provider like a module would (SOURCE + provider); the
    # structural gate must then admit it (no field rules -> warning only).
    PROVIDERS["synthetic"] = _boom
    SOURCE_LABELS["synthetic"] = "Synthetic boom"
    r = check_product({"label": "Boom", "source": "synthetic"}, TODAY)
    assert r["status"] == "error" and "RuntimeError" in r["message"], r
    assert r["label"] == "Boom" and r["source"] == "synthetic", r
    assert "sekrit-token" not in r["message"], r   # non-secret diagnostics only
    assert "see run logs" not in r["message"], r
    captured = io.StringIO()
    log_handler = logging.StreamHandler(captured)
    handler_mod.logger.addHandler(log_handler)
    try:
        check_product({"label": "Boom", "source": "synthetic"}, TODAY)
    finally:
        handler_mod.logger.removeHandler(log_handler)
    assert "sekrit-token" not in captured.getvalue(), captured.getvalue()
    r = check_product({"label": "Boom2", "source": "synthetic",
                       "policy_note": "n"}, TODAY)
    assert r["status"] == "error" and r.get("policy_note") == "n", r

    # --- broken provider contracts (non-dict returns) normalize too -----------
    PROVIDERS["synthetic"] = lambda entry, today: "not-a-dict"
    r = check_product({"label": "Bad", "source": "synthetic",
                       "policy_note": "psa"}, TODAY)
    assert r["status"] == "error" and isinstance(r, dict), r
    assert "invalid result" in r["message"] and "str" in r["message"], r
    assert "not-a-dict" not in r["message"], r
    r2 = check_product({"label": "Bad3", "source": "synthetic"}, TODAY)
    assert isinstance(r2, dict) and r2["status"] == "error", r2
    PROVIDERS["synthetic"] = lambda entry, today: None
    r3 = check_product({"label": "BadNone", "source": "synthetic"}, TODAY)
    assert r3["status"] == "error" and "NoneType" in r3["message"], r3

    # unhashable source values reach the gate and normalize (no TypeError)
    for hostile in (["npm_registry"], {"k": 1}):
        r = check_product({"label": "H", "source": hostile}, TODAY)
        assert r["status"] == "error", (hostile, r)
        assert "source" in r["message"], (hostile, r)

    # --- optional-version npm entry still reaches its provider ----------------
    seen = {}

    def _npm_stub(entry, today):
        seen["called"] = True
        return {"label": "x", "status": "ok", "message": "latest",
                "days_remaining": None, "source": "npm_registry"}

    PROVIDERS["npm_registry"] = _npm_stub
    r = check_product({"source": "npm_registry", "package": "x",
                       "policy_note": "lat"}, TODAY)
    assert seen.get("called"), r
    assert r["status"] == "ok" and r.get("policy_note") == "lat", r

    # unknown sources still enumerate the known set
    r = check_product({"source": "wheel"}, TODAY)
    assert r["status"] == "error" and "endoflife_date" in r["message"], r

    # --- mixed run: malformed rows isolated, valid rows continue --------------
    PROVIDERS["synthetic"] = _boom  # restore the raising stub for this run
    products = [
        {"label": "Good", "source": "manual"},
        {"_section": "Databases"},
        {"label": "B", "source": "synthetic"},
        {"source": "maven_central", "version": "9"},  # gated, never fetched
        42,                                           # non-object entry
        {"source": "manual", "label": "C"},
    ]
    results = [r for i, e in enumerate(products)
               if (r := check_product(e, TODAY, index=i)) is not None]
    assert [r["status"] for r in results] == [
        "untracked", "error", "error", "error", "untracked"], results
    assert results[0]["source"] == "manual" and results[4]["source"] == "manual"
    assert results[1]["label"] == "B" and "RuntimeError" in results[1]["message"]
    assert "products[3].group" in results[2]["message"], results[2]
    assert "unusable product entry (int)" in results[3]["label"], results[3]

    # both formatters consume the mixed result set unchanged
    text, has_alerts = format_report_text(results, [30, 60, 90], TODAY)
    html, _ = format_report_html(results, [30, 60, 90], TODAY)
    assert "Good" in text and "Good" in html, text
    assert "unusable product entry (int)" in text, text
finally:
    PROVIDERS.clear()
    PROVIDERS.update(saved_providers)
    SOURCE_LABELS.clear()
    SOURCE_LABELS.update(saved_labels)

# --- CLI: fatal config exits 1 with the same diagnostics as --validate --------
bad = tmpfile("bad.json", {"products": one, "notify_when": "sometimes"})
proc = subprocess.run([sys.executable, "lambda_function.py", bad],
                      capture_output=True, text=True, cwd=ROOT)
assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
assert "INVALID" in proc.stdout and "notify_when" in proc.stdout, proc.stdout
assert "Traceback" not in proc.stderr, proc.stderr

# Invalid JSON is also converted to the structured config diagnostic rather
# than being mistaken for an arbitrary runtime ValueError.
raw_bad = os.path.join(TMPDIR, "invalid-json.json")
with open(raw_bad, "wb") as f:
    f.write(b"{not-json")
proc = subprocess.run([sys.executable, "lambda_function.py", raw_bad],
                      capture_output=True, text=True, cwd=ROOT)
assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
assert "INVALID" in proc.stdout and "invalid JSON" in proc.stdout, proc.stdout
assert "Traceback" not in proc.stderr, proc.stderr

# S3 bytes use the same decode/enforcement path as local files.
class _Body:
    def __init__(self, raw):
        self.raw = raw
    def read(self):
        return self.raw

class _S3:
    def __init__(self, raw):
        self.raw = raw
    def get_object(self, **_kwargs):
        return {"Body": _Body(self.raw)}

old_boto3 = sys.modules.get("boto3")
old_bucket = os.environ.get("CONFIG_BUCKET")
fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda _name: _S3(json.dumps({
    "products": [{"_section": "Only divider"}],
}).encode("ascii"))
sys.modules["boto3"] = fake_boto3
os.environ["CONFIG_BUCKET"] = "test-bucket"
try:
    try:
        handler_mod.load_config_from_s3("projects/test/eol_config.json")
        raise AssertionError("section-only S3 config accepted")
    except ConfigValidationError as exc:
        assert any(f["path"] == "products" for f in exc.findings), exc.findings
finally:
    if old_boto3 is None:
        del sys.modules["boto3"]
    else:
        sys.modules["boto3"] = old_boto3
    if old_bucket is None:
        del os.environ["CONFIG_BUCKET"]
    else:
        os.environ["CONFIG_BUCKET"] = old_bucket

print("OK test_runtime_guardrails")
