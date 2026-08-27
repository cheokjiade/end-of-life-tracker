"""Network-free tests for the HTML-only local report runner (issue #11).

Covers: --all discovery and sample-template exclusion, explicit selection
(including the sample), safe default output when a config declares no usable
html_file path, config immutability (in memory and on disk), error/unknown
result counting, and proof that console / SNS / SES channels are never invoked
(through the real dispatcher, with sentinel notifiers). Products use the
'manual' provider plus an unknown source so no network access occurs.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker import notify as notify_mod
from eoltracker.html_runner import (
    DEFAULT_HTML_PATH,
    discover_configs,
    expand_config_arg,
    build_html_only_config,
    fallback_output_for,
    run_config,
)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


def sentinel(channel):
    """A channel handler that fails loudly if ever dispatched."""
    def boom(*args, **kwargs):
        raise AssertionError(f"channel notifier invoked for '{channel}'")
    return boom


# ---------------------------------------------------------------------------
# 1. Discovery (--all mode): finds configs sorted, excludes the sample template
# ---------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    write_json(os.path.join(tmp, "eol_config.beta.json"), {"products": []})
    write_json(os.path.join(tmp, "eol_config.alpha.json"), {"products": []})
    write_json(os.path.join(tmp, "eol_config.sample.json"), {"products": []})
    write_json(os.path.join(tmp, "unrelated.json"), {"products": []})

    found = [os.path.basename(p) for p in discover_configs(tmp)]
    assert found == ["eol_config.alpha.json", "eol_config.beta.json"], found

# An empty/non-matching directory yields [] rather than raising.
with tempfile.TemporaryDirectory() as empty:
    assert discover_configs(empty) == []

print("OK discovery excludes the sample and sorts deterministically")


# ---------------------------------------------------------------------------
# 2. Explicit selection resolves named files (sample included) and shorthands
# ---------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    sample = write_json(os.path.join(tmp, "eol_config.sample.json"), {"products": []})
    demo = write_json(os.path.join(tmp, "eol_config.demo.json"), {"products": []})

    # Existing paths pass through unchanged -> explicit selection works,
    # including explicitly picking eol_config.sample.json.
    assert expand_config_arg(sample) == os.path.normpath(sample)
    # Unresolvable things do not silently become paths.
    assert expand_config_arg(os.path.join(tmp, "nope.json")) is None

    prev_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        # Shorthand: "demo" -> eol_config.demo.json (same UX as run.sh/run.ps1).
        resolved = expand_config_arg("demo")
        assert os.path.abspath(resolved) == os.path.abspath(demo), resolved
    finally:
        os.chdir(prev_cwd)

print("OK explicit arg resolution accepts named files (sample included)")


# ---------------------------------------------------------------------------
# 3. Safe default output + HTML-only suppression view; source config untouched
# ---------------------------------------------------------------------------

original = {
    "_comment": ["curation preserved"],
    "alert_thresholds_days": [30, 60, 90],
    "notify_when": "always",
    "notifications": [
        {"type": "console"},
        {"type": "html_file"},  # declared but no path -> default kicks in
        {"type": "sns", "topic_arn": "arn:aws:sns:eu-west-1:123:eol"},
        {"type": "ses", "from_email": "noreply@example.com",
         "to_emails": ["team@example.com"]},
    ],
    "products": [{"source": "manual", "label": "Widget"}],
}
snapshot = copy.deepcopy(original)

view = build_html_only_config(original)
assert view["notifications"] == [
    {"type": "html_file", "path": DEFAULT_HTML_PATH}
], view["notifications"]
assert original == snapshot, "input config must not be mutated"

# A config with no notifications block at all also gets the safe default.
bare_view = build_html_only_config({"products": []})
assert bare_view["notifications"] == [
    {"type": "html_file", "path": DEFAULT_HTML_PATH}
]

# A configured html_file path is honoured verbatim.
named = build_html_only_config({
    "notifications": [{"type": "html_file", "path": "eol_report_beta.html"}],
})
assert named["notifications"] == [
    {"type": "html_file", "path": "eol_report_beta.html"}
]

# The per-config fallback derivation keeps --all runs collision-free.
assert fallback_output_for("eol_config.beta.json") == "eol_report_beta.html"
assert os.path.basename(fallback_output_for(os.path.join(tmp, "team.json"))) == \
    "eol_report_team.html"
assert fallback_output_for("eol_config.json") == DEFAULT_HTML_PATH
assert fallback_output_for("weird name.json").startswith("eol_report_weird_name")

print("OK safe default output and HTML-only suppression view")


# ---------------------------------------------------------------------------
# 4. run_config: counts, immutability on disk, channel suppression via record
# ---------------------------------------------------------------------------

calls = []

def recording_notifier(config, report_text, report_html, subject):
    calls.append({
        "config": config,
        "report_text": report_text,
        "report_html": report_html,
        "subject": subject,
    })
    return {"html_file": "reports/sentinel/report.html"}


today_cfg = {
    "alert_thresholds_days": [30, 60, 90],
    "notifications": [
        {"type": "console"},
        {"type": "sns", "topic_arn": "arn:aws:sns:eu-west-1:123:eol"},
        {"type": "html_file", "path": "eol_report_it.html"},
    ],
    "products": [
        {"source": "manual", "label": "Legacy Tool", "eol_date": "2020-01-01",
         "note": "date from inventory sheet"},
        {"source": "manual", "label": "Custom Gateway"},
        {"source": "definitely_unknown_source", "label": "Broken Row"},
    ],
}

TODAY = date(2026, 8, 27)

with tempfile.TemporaryDirectory() as tmp:
    cfg_path = write_json(os.path.join(tmp, "eol_config.units.json"), today_cfg)
    with open(cfg_path, "rb") as f:
        before_bytes = f.read()

    s = run_config(cfg_path, today=TODAY, notifier=recording_notifier)

    assert len(calls) == 1, calls
    c = calls[0]
    assert c["report_text"] == "", "runner must not request plain-text delivery"
    assert c["subject"].startswith("[EOL ALERT]"), c["subject"]
    got = c["config"]["notifications"]
    assert got == [{"type": "html_file", "path": "eol_report_it.html"}], got
    assert "Legacy Tool" in c["report_html"]

    assert s["config"] == cfg_path
    assert s["output"] == "reports/sentinel/report.html"
    assert s["checked"] == 3, s
    assert s["errors"] == 1, s      # unknown source -> error row
    assert s["unknown"] == 0, s
    assert s["has_alerts"] is True  # Legacy Tool is past its EOL date

    with open(cfg_path, "rb") as f:
        assert f.read() == before_bytes, "config file on disk must be unchanged"

# A config without a products array fails loudly instead of lying quietly.
with tempfile.TemporaryDirectory() as tmp:
    bad = os.path.join(tmp, "broken.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write('{"alert_thresholds_days": [30]}')
    try:
        run_config(bad, today=TODAY, notifier=recording_notifier)
        raise AssertionError("expected ValueError for missing products array")
    except ValueError:
        pass

# No notifications block at all -> derived per-config fallback path.
calls.clear()
with tempfile.TemporaryDirectory() as tmp:
    fresh = write_json(os.path.join(tmp, "eol_config.fresh.json"), {
        "products": [{"source": "manual", "label": "Fresh Thing"}],
    })
    s = run_config(fresh, today=TODAY, notifier=recording_notifier)
    assert calls[-1]["config"]["notifications"] == [
        {"type": "html_file", "path": "eol_report_fresh.html"}
    ], calls[-1]["config"]["notifications"]
    assert s["checked"] == 1 and s["errors"] == 0 and s["unknown"] == 0

print("OK run_config summary, immutability, and channel selection")


# ---------------------------------------------------------------------------
# 5. Real dispatcher end-to-end: console/SNS/SES unreachable, real HTML written
# ---------------------------------------------------------------------------

saved_notifiers = dict(notify_mod._NOTIFIERS)
prev_cwd = os.getcwd()
tmp = tempfile.mkdtemp()
try:
    notify_mod._NOTIFIERS["console"] = sentinel("console")
    notify_mod._NOTIFIERS["sns"] = sentinel("sns")
    notify_mod._NOTIFIERS["ses"] = sentinel("ses")

    os.chdir(tmp)
    cfg = write_json(os.path.join(tmp, "eol_config.live.json"), {
        "notifications": [
            {"type": "console"},
            {"type": "sns", "topic_arn": "arn:aws:sns:eu-west-1:123:eol"},
            {"type": "ses", "from_email": "noreply@example.com",
             "to_emails": ["team@example.com"]},
        ],
        "products": [
            {"source": "manual", "label": "Widget 2.0", "eol_date": "2027-06-30"},
        ],
    })
    s2 = run_config(cfg, today=TODAY)
    out = s2["output"]
    assert out, f"expected a written path, got {out}"
    assert "eol_report_live" in out.replace(os.sep, "/"), out
    assert os.path.isfile(out), out
    with open(out, encoding="utf-8") as f:
        body = f.read()
    assert "Widget 2.0" in body and "<html" in body.lower(), body[:200]
finally:
    os.chdir(prev_cwd)
    notify_mod._NOTIFIERS.clear()
    notify_mod._NOTIFIERS.update(saved_notifiers)
    shutil.rmtree(tmp, ignore_errors=True)

print("OK real dispatcher writes HTML only; console/SNS/SES unreachable")

print("OK test_html_runner")
