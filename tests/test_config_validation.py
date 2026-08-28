"""Network-free structural validation checks (issue #5 remediation).

Standalone assertion script (repo convention: no framework, no network).
Covers eoltracker.validation rules, its CLI contract behind
``python lambda_function.py --validate``, and keeps the tracked sample
template permanently valid.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.validation import main as validate_main
from eoltracker.validation import VALID_ENGINES, validate_config, validate_config_file
from eoltracker.parsers.aws_rds import DEFAULT_ENGINE, _AWS_DOCS_URLS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def errors(results):
    return [r for r in results if r["severity"] == "error"]


def warnings(results):
    return [r for r in results if r["severity"] == "warning"]


def by_path(results, path):
    return [r for r in results if r["path"] == path]


# --- the tracked sample stays valid (keeps the template exemplary) --------
sample_results = validate_config_file(os.path.join(ROOT, "eol_config.sample.json"))
assert not sample_results, f"tracked sample must be clean, got: {sample_results}"

# --- structural shape ------------------------------------------------------
assert [r["path"] for r in validate_config([1, 2])] == ["config"]
res = validate_config({})
assert by_path(errors(res), "products"), res
res = validate_config({"products": "nope"})
assert by_path(errors(res), "products"), res
res = validate_config({"products": []})
assert by_path(errors(res), "products"), res

# dividers are skipped exactly like check_product does
res = validate_config({"products": [{"_section": "Spring Boot", "label": "d"}]})
assert not res, res

# --- default provider (endoflife_date) ------------------------------------
res = validate_config({"products": [{"product": "python"}]})
assert by_path(errors(res), "products[0].version"), res
res = validate_config({"products": [{"version": "3.13"}]})
assert by_path(errors(res), "products[0].product"), res
# blank / wrong-typed scalars are rejected like missing ones
for bad in ("", "   ", None, {"v": 1}, True):
    res = validate_config({"products": [{"product": bad}]})
    assert by_path(errors(res), "products[0].product"), (bad, res)

# Identifiers and versions must be strings. JSON numbers such as 3.10 are
# already lossy (decoded as 3.1) before providers can compare them.
for bad_version in (3, 3.10):
    res = validate_config({"products": [{
        "product": "python", "version": bad_version,
    }]})
    assert by_path(errors(res), "products[0].version"), (bad_version, res)

# --- unknown sources -------------------------------------------------------
res = validate_config({"products": [{"source": "wheel", "version": "1"}]})
errs = by_path(errors(res), "products[0].source")
assert errs and "endoflife_date" in errs[0]["message"], res

# --- every registered non-default provider's required fields ---------------
for source, missing, present in (
    ("aws_rds_scrape", "version", {"engine": "aurora-postgresql"}),
    ("aws_sdk_lifecycle", "sdk", {}),
    ("jackson_lifecycle", "version", {}),
    ("maven_central", "group", {}),
    ("npm_registry", "package", {}),
    ("tyk_lifecycle", "version", {}),
):
    entry = dict(present)
    entry["source"] = source
    res = validate_config({"products": [entry]})
    assert by_path(errors(res), f"products[0].{missing}"), (source, res)
    # an otherwise-complete entry passes
    complete = {
        "aws_rds_scrape": {"engine": "aurora-postgresql"},
        "aws_sdk_lifecycle": {"sdk": "boto3", "major": "2"},
        "jackson_lifecycle": {},
        "maven_central": {"group": "com.example", "artifact": "core"},
        "npm_registry": {"package": "example-pkg"},
        "tyk_lifecycle": {},
    }[source]
    entry_ok = dict(complete, source=source, version="1.0")
    res = validate_config({"products": [entry_ok]})
    assert not errors(res), (source, res)

# manual entries are watch-only by design, but still need a report label.
assert by_path(errors(validate_config({
    "products": [{"source": "manual"}]})), "products[0].label")
assert not errors(validate_config({
    "products": [{"source": "manual", "label": "Vendor tool"}]}))

# npm can watch the registry's latest release without an in-use version.
assert not errors(validate_config({"products": [{
    "source": "npm_registry", "package": "example-pkg",
}]}))

# Optional display fields are safe only as non-blank strings.
for field in ("label", "policy_note", "reference_url"):
    for bad in ("", "   ", 42, True, []):
        res = validate_config({"products": [{
            "product": "python", "version": "3.13", field: bad,
        }]})
        assert by_path(errors(res), f"products[0].{field}"), (field, bad, res)

# --- aws_rds_scrape engine guard -------------------------------------------
assert set(VALID_ENGINES) == set(_AWS_DOCS_URLS)
assert DEFAULT_ENGINE in VALID_ENGINES
res = validate_config({
    "products": [
        {"source": "aws_rds_scrape", "engine": "mysql", "version": "17.5"}]
})
assert by_path(errors(res), "products[0].engine"), res

# --- thresholds -------------------------------------------------------------
# 'one' keeps the otherwise-empty products list from raising its own error.
one = [{"label": "A", "source": "manual"}]
for bad in ([], [0], [-30], ["soon"], [True], [float("inf")], [float("nan")]):
    res = validate_config({"alert_thresholds_days": bad, "products": one})
    assert by_path(errors(res), "alert_thresholds_days"), (bad, res)
assert not errors(validate_config({
    "alert_thresholds_days": [30, 90], "products": one,
}))
assert not errors(validate_config({
    "alert_thresholds_days": [10 ** 400], "products": one,
}))

# --- notify_when ------------------------------------------------------------
res = validate_config({"notify_when": "sometimes", "products": one})
assert by_path(errors(res), "notify_when"), res
for good in ("always", "alerts_only"):
    assert not errors(validate_config({"notify_when": good, "products": one}))

# --- notification channels --------------------------------------------------
res = validate_config({"products": one, "notifications": [
    "console",              # not an object
    {"type": "slack"},      # unsupported type
    {"type": "html_file", "path": 42},
    {"type": "sns", "topic_arn": 7},
    {"type": "ses", "to_emails": "team@example.com"},  # string, not list
]})
expected_paths = {
    "notifications[0]", "notifications[1].type",
    "notifications[2].path", "notifications[3].topic_arn",
    "notifications[4].to_emails",
}
got_paths = {r["path"] for r in errors(res)}
assert got_paths == expected_paths, res

valid_channels = validate_config({"products": one, "notifications": [
    {"type": "console"},
    {"type": "html_file", "path": "eol_report.html"},
    {"type": "sns", "topic_arn": "arn:aws:sns:eu-west-1:123:eol-alerts"},
    {"type": "ses", "from_email": "noreply@example.com",
     "to_emails": ["team@example.com"]},
]})
assert not errors(valid_channels)

empty_notifications = validate_config({"products": one, "notifications": []})
assert by_path(warnings(empty_notifications), "notifications"), empty_notifications

# env-var / event-payload fallbacks surface as warnings, never errors
fallback = validate_config({"products": one, "notifications": [
    {"type": "sns"}, {"type": "ses"}]})
warn_paths = [w["path"] for w in warnings(fallback)]
assert "notifications[0].topic_arn" in warn_paths, fallback
assert "notifications[1]" in warn_paths, fallback
assert not errors(fallback)

# --- duplicates + unknown keys ----------------------------------------------
dup = validate_config({"products": [
    {"label": "A", "source": "manual"},
    {"label": "A", "source": "manual"},
]})
assert by_path(warnings(dup), "products[1].label"), dup
res = validate_config({"alert_threshold_dayz": [30], "products": []})
unknown_key_warns = [w for w in warnings(res) if w["path"] == "alert_threshold_dayz"]
assert unknown_key_warns and "typo" in unknown_key_warns[0]["message"], res

# sorting: findings come back path-ordered regardless of discovery order
res = validate_config({"notify_when": "?", "products": [{"source": "x"}]})
paths = [r["path"] for r in res]
assert paths == sorted(paths), paths

# --- file loading edge cases -------------------------------------------------
tmpdir = tempfile.mkdtemp()

def tmpfile(name, data):
    p = os.path.join(tmpdir, name)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(p, mode, encoding=None if isinstance(data, bytes) else "utf-8") as f:
        f.write(data)
    return p

good = tmpfile("good.json", json.dumps(
    {"products": [{"source": "manual", "label": "Vendor tool"}]}).encode("ascii"))
assert not validate_config_file(good)

bad_json = tmpfile("bad.json", b"{oops")
findings = validate_config_file(bad_json)
assert len(findings) == 1 and findings[0]["severity"] == "error"
assert "invalid JSON" in findings[0]["message"]

not_utf8 = tmpfile("wide.json", '{"products": []}'.encode("utf-16"))
findings = validate_config_file(not_utf8)
errs = errors(findings)
assert [e["path"] for e in errs] == ["config"], findings
assert "UTF-8" in errs[0]["message"] or "ASCII" in errs[0]["message"], findings

# Valid UTF-8 can still be unsafe on Windows' locale-dependent plain open().
# Keep deployable configs ASCII-only, as documented in AGENTS.md.
utf8_non_ascii = tmpfile(
    "non-ascii.json",
    json.dumps({"products": [{"source": "manual", "label": "caf\u00e9"}]},
               ensure_ascii=False).encode("utf-8"),
)
findings = validate_config_file(utf8_non_ascii)
errs = errors(findings)
assert [e["path"] for e in errs] == ["config"], findings
assert "ASCII-only" in errs[0]["message"], findings

missing = os.path.join(tmpdir, "nope.json")
findings = validate_config_file(missing)
assert len(findings) == 1 and findings[0]["severity"] == "error"

# --- CLI exit codes -----------------------------------------------------------
assert validate_main([good]) == 0
assert validate_main([bad_json]) == 1
assert validate_main([]) == 2
assert validate_main([good, good]) == 2

proc = subprocess.run(
    [sys.executable, "lambda_function.py", "--validate", "eol_config.sample.json"],
    capture_output=True, text=True, cwd=ROOT)
assert proc.returncode == 0, proc.stdout + proc.stderr
assert "VALID" in proc.stdout

proc = subprocess.run(
    [sys.executable, "lambda_function.py", "--validate=eol_config.sample.json"],
    capture_output=True, text=True, cwd=ROOT)
assert proc.returncode == 0, proc.stdout + proc.stderr

proc = subprocess.run(
    [sys.executable, "lambda_function.py", "--validate="],
    capture_output=True, text=True, cwd=ROOT)
assert proc.returncode == 2, proc.stdout + proc.stderr

print("OK test_config_validation")
