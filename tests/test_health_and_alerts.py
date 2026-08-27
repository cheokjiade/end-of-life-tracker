"""Regression tests: separate lifecycle alerts from tracker-health failures.

Covers issue #6 (audit R-01, R-02, R-08):
  - undated 'approaching' states alert regardless of thresholds (R-01);
  - 'error'/'unknown' notify under alerts_only and never render healthy,
    with distinct subjects/banners; deliberate 'untracked' stays separate
    (R-02);
  - missing npm/Maven in-use versions become 'unknown' instead of 'ok' (R-08).

Network-free: providers are exercised through pure helpers or seeded caches;
the Lambda handler is driven with mocked config loading and notifications.
"""

import os
import sys
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker import handler as handler_mod
from eoltracker import runner as runner_mod
from eoltracker.parsers import check_product
from eoltracker.parsers.maven_central import (
    _MAVEN_LATEST_CACHE,
    _MAVEN_VERSION_CACHE,
    _provider_maven_central,
)
from eoltracker.parsers.npm_registry import _npm_result_from_doc
from eoltracker.report import analyse_results, format_report_html, format_report_text

TODAY = date(2026, 8, 27)
TH = [30, 60, 90]


def R(status, days=None, label="L", source="test-src", message="m"):
    """A uniform-shaped synthetic result."""
    return {
        "label": label, "product": "p", "version": "1", "lts": False,
        "status": status, "message": message,
        "latest_patch": None, "latest_patch_date": None,
        "latest_cycle": None, "latest_cycle_version": None,
        "latest_cycle_release_date": None, "on_latest_cycle": False,
        "eol_date": None, "support_date": None, "days_remaining": days,
        "support_days_remaining": None, "source": source,
    }


# ---------------------------------------------------------------------------
# 1. Flag matrix from analyse_results
# ---------------------------------------------------------------------------
assert analyse_results([R("eol")], TH)["has_lifecycle_alerts"] is True
assert analyse_results([R("eol")], TH)["has_health_failures"] is False

assert analyse_results([R("approaching", 20)], TH)["has_lifecycle_alerts"] is True
assert analyse_results([R("approaching", 20)], TH)["has_health_failures"] is False

# Undated approaching counts as a lifecycle alert (R-01).
a_undated = analyse_results([R("approaching", None, "SDK in Maintenance")], TH)
assert a_undated["has_lifecycle_alerts"] is True
assert len(a_undated["approaching"]) == 1
assert analyse_results([R("approaching", None)], TH)["has_health_failures"] is False

assert analyse_results([R("ok")], TH)["has_lifecycle_alerts"] is False
assert analyse_results([R("ok")], TH)["has_health_failures"] is False

assert analyse_results([R("error", label="Bad")], TH)["has_health_failures"] is True
assert analyse_results([R("unknown", label="Unk")], TH)["has_health_failures"] is True
assert analyse_results([R("unknown", label="Unk")], TH)["has_lifecycle_alerts"] is False

# Deliberate untracked: neither an alert nor a health failure.
a_untracked = analyse_results([R("untracked", label="PuTTY")], TH)
assert a_untracked["has_lifecycle_alerts"] is False
assert a_untracked["has_health_failures"] is False
assert len(a_untracked["untracked"]) == 1

# Empty thresholds -> default 90 preserved.
assert analyse_results([], [])["max_threshold"] == 90


# ---------------------------------------------------------------------------
# 2. Dated far-future approaching is STILL informational (behaviour kept)
# ---------------------------------------------------------------------------
a_far = analyse_results([R("approaching", 400, "Future Thing")], TH)
assert a_far["has_lifecycle_alerts"] is False
assert len(a_far["ok"]) == 1
text_far, alerts_far = format_report_text([R("approaching", 400, "Future Thing")], TH, TODAY)
assert alerts_far is False
assert "No Immediate Concerns" in text_far


# ---------------------------------------------------------------------------
# 3. Mixed dated + undated approaching must sort without crashing (old code
#    would either drop the undated item or raise TypeError on None compare)
# ---------------------------------------------------------------------------
a_mix = analyse_results(
    [R("approaching", None, "Undated"), R("approaching", 45, "Soon"), R("approaching", 10, "Sooner")],
    TH,
)
order = [r["days_remaining"] for r in a_mix["approaching"]]
assert order == [10, 45, None], order
text_mix, _ = format_report_text(a_mix["approaching"], TH, TODAY)
assert "at risk, no date" in text_mix


# ---------------------------------------------------------------------------
# 4. R-01 regression: undated approaching renders as an ALERT (was silently OK)
# ---------------------------------------------------------------------------
undated = R("approaching", None, "AWS SDK Java v1", message="Maintenance phase")
text_r01, alerts_text_r01 = format_report_text([undated], TH, TODAY)
assert alerts_text_r01 is True, text_r01
assert ">> APPROACHING END OF LIFE" in text_r01
assert "[at risk, no date]" in text_r01
assert "-- No Immediate Concerns" not in text_r01.split("APPROACHING")[0]

html_r01, alerts_html_r01 = format_report_html([undated], TH, TODAY)
assert alerts_html_r01 is True
assert "AT RISK (no date)" in html_r01
assert "All products are within support" not in html_r01
assert "approaching end of life" in html_r01  # orange lifecycle banner


# ---------------------------------------------------------------------------
# 5. R-02 regression: error and unknown never render healthy
# ---------------------------------------------------------------------------
err = R("error", label="Broken Source", message="fetch failed")
unk = R("unknown", label="Mystery Lib", message="could not verify")

text_r02, alerts_text_r02 = format_report_text([err, unk], TH, TODAY)
assert alerts_text_r02 is True
assert "!! TRACKER HEALTH - CHECK ERRORS (1)" in text_r02
assert "!! TRACKER HEALTH - UNKNOWN STATUS (1)" in text_r02
assert "Broken Source" in text_r02 and "Mystery Lib" in text_r02
assert "No Immediate Concerns" not in text_r02

html_r02, alerts_html_r02 = format_report_html([err, unk], TH, TODAY)
assert alerts_html_r02 is True
# Distinct purple health banner; no green all-clear banner.
assert "TRACKER HEALTH: 1 check error(s), 1 unknown status(es)" in html_r02
assert "All products are within support" not in html_r02
assert "background-color:#6a1b9a" in html_r02
assert "CHECK FAILED" in html_r02 and "UNVERIFIED" in html_r02
# Health rows carry the purple-family backgrounds, not the old neutral grey.
assert "background-color:#ede7f6" in html_r02
assert "background-color:#f5f5f5" not in html_r02

# A single-kindle health failure shows its own count line.
html_err_only, _ = format_report_html([err], TH, TODAY)
assert "TRACKER HEALTH: 1 check error(s)" in html_err_only
html_unk_only, _ = format_report_html([unk], TH, TODAY)
assert "1 unknown status(es)" in html_unk_only


# ---------------------------------------------------------------------------
# 6. R-02: unknown stays out of the OK bucket alongside healthy products
# ---------------------------------------------------------------------------
mixed_run = [R("ok", 300, "Healthy One"), unk]
text_mixed, alerts_mixed = format_report_text(mixed_run, TH, TODAY)
assert alerts_mixed is True
ok_section = text_mixed.split("-- No Immediate Concerns")[1].split("!!")[0]
assert "Mystery Lib" not in ok_section
assert "Healthy One" in ok_section


# ---------------------------------------------------------------------------
# 7. Deliberate untracked remains a distinct informational bucket
# ---------------------------------------------------------------------------
untracked = R("untracked", label="PuTTY", message="No automated EOL source available")
text_ut, alerts_ut = format_report_text([untracked], TH, TODAY)
assert alerts_ut is False
assert "??" in text_ut and "UNTRACKED (no EOL source)" in text_ut
assert "TRACKER HEALTH" not in text_ut

html_ut, alerts_html_ut = format_report_html([untracked], TH, TODAY)
assert alerts_html_ut is False
assert "UNTRACKED" in html_ut
# All-clear banner stays green but now discloses the untracked count.
assert "All products are within support (1 untracked)" in html_ut
assert "background-color:#388e3c" in html_ut


# ---------------------------------------------------------------------------
# 8. Combined lifecycle + health run: both signals visible, both fire
# ---------------------------------------------------------------------------
both = [R("eol", None, "Old Thing"), err]
text_both, alerts_both = format_report_text(both, TH, TODAY)
assert alerts_both is True
assert "ALREADY END OF LIFE" in text_both and "TRACKER HEALTH" in text_both

html_both, alerts_html_both = format_report_html(both, TH, TODAY)
assert alerts_html_both is True
assert "past end of life" in html_both            # red lifecycle banner
assert "TRACKER HEALTH:" in html_both             # purple health banner
analysis_both = analyse_results(both, TH)
assert analysis_both["has_lifecycle_alerts"] and analysis_both["has_health_failures"]


# ---------------------------------------------------------------------------
# 9. R-08: npm results built from synthetic registry documents
# ---------------------------------------------------------------------------
NPM_TODAY = date(2026, 1, 15)


def npm_doc(latest=None, versions=None, dep_map=None):
    doc = {"dist-tags": {}, "time": {}, "versions": {}}
    if latest:
        doc["dist-tags"]["latest"] = latest
    for i, v in enumerate(versions or []):
        doc["versions"][v] = {}
        doc["time"][v] = f"2025-{i + 1:02d}-01T00:00:00.000Z"
    for v, msg in (dep_map or {}).items():
        doc["versions"].setdefault(v, {})["deprecated"] = msg
    return doc


entry_full = {"package": "left-pad", "version": "1.0.0", "label": "Left Pad"}

# 9a. In-use version absent from the registry -> unknown (was ok).
r = _npm_result_from_doc(dict(entry_full), npm_doc(latest="2.0.0", versions=["2.0.0"]), NPM_TODAY)
assert r["status"] == "unknown", r
assert "not on npm registry" in r["message"]

# 9b. No version supplied -> unknown.
r = _npm_result_from_doc({"package": "left-pad"}, npm_doc(latest="2.0.0", versions=["2.0.0"]), NPM_TODAY)
assert r["status"] == "unknown", r
assert "not provided" in r["message"]

# 9c. Registry knows nothing -> unknown.
r = _npm_result_from_doc(dict(entry_full), npm_doc(), NPM_TODAY)
assert r["status"] == "unknown", r

# 9d. Deprecation still wins as an EOL alert.
r = _npm_result_from_doc(dict(entry_full), npm_doc(dep_map={"1.0.0": "use v2"}), NPM_TODAY)
assert r["status"] == "eol", r

# 9e. Positively verified, behind latest -> healthy ok (unchanged semantics).
r = _npm_result_from_doc(dict(entry_full), npm_doc(latest="2.0.0", versions=["1.0.0", "2.0.0"]), NPM_TODAY)
assert r["status"] == "ok", r

# 9f. On a fresh latest -> ok.
r = _npm_result_from_doc({"package": "left-pad", "version": "2.0.0"}, npm_doc(latest="2.0.0", versions=["2.0.0"]), NPM_TODAY)
assert r["status"] == "ok", r


# ---------------------------------------------------------------------------
# 10. R-08: maven_central unknown vs ok via seeded caches (network-free)
# ---------------------------------------------------------------------------
MV_ENTRY = {"group": "com.example", "artifact": "lib", "version": "8.0"}
MV_LATEST = ("com.example", "lib")
MV_VERSION = ("com.example", "lib", "8.0")

try:
    # Both caches are pre-seeded so the provider never touches the network;
    # a cached None is what the provider itself stores for a miss.
    # 10a. Artifact exists, in-use version has no Central record -> unknown.
    _MAVEN_LATEST_CACHE.clear()
    _MAVEN_VERSION_CACHE.clear()
    _MAVEN_LATEST_CACHE[MV_LATEST] = {"v": "9.4", "released": date(2025, 12, 1)}
    _MAVEN_VERSION_CACHE[MV_VERSION] = None
    r = _provider_maven_central(MV_ENTRY, NPM_TODAY)
    assert r["status"] == "unknown", r
    assert "not on Maven Central" in r["message"]

    # 10b. Same lookup once Central positively locates the version -> ok.
    _MAVEN_VERSION_CACHE[MV_VERSION] = {"v": "8.0", "released": date(2024, 3, 1)}
    r = _provider_maven_central(MV_ENTRY, NPM_TODAY)
    assert r["status"] == "ok", r

    # 10c. In-use version IS the resolved latest even though the gav query
    #      came back empty -> verified ok (the latest query proves it exists).
    _MAVEN_LATEST_CACHE[MV_LATEST] = {"v": "8.0", "released": date(2024, 3, 1)}
    _MAVEN_VERSION_CACHE[MV_VERSION] = None
    r = _provider_maven_central(MV_ENTRY, NPM_TODAY)
    assert r["status"] == "ok", r
finally:
    _MAVEN_LATEST_CACHE.clear()
    _MAVEN_VERSION_CACHE.clear()


# ---------------------------------------------------------------------------
# 11. Subject lines distinguish lifecycle risk from health degradation
# ---------------------------------------------------------------------------
subj = handler_mod.build_subject(analyse_results([R("eol")], TH), None, TODAY)
assert subj.startswith("[EOL ALERT]") and "TRACKER" not in subj, subj

subj = handler_mod.build_subject(analyse_results([R("error")], TH), None, TODAY)
assert subj.startswith("[TRACKER HEALTH]") and "EOL ALERT" not in subj, subj

subj = handler_mod.build_subject(analyse_results(both, TH), "core-app", TODAY)
assert subj.startswith("[EOL ALERT][TRACKER HEALTH] [core-app] "), subj

subj = handler_mod.build_subject(analyse_results([R("ok")], TH), None, TODAY)
assert subj.startswith("[EOL Report]"), subj


# ---------------------------------------------------------------------------
# 12. Handler end-to-end: alerts_only fires on health failures and undated
#     approaching states, and stays silent for an all-clear run.
# ---------------------------------------------------------------------------
def _run_handler(config, product_results):
    captured = {}

    def fake_send(cfg, report_text, report_html, subject, runtime_overrides=None):
        captured["subject"] = subject
        return [{
            "channel": "console", "required": False, "attempted": True,
            "delivered": True, "skipped": False, "error": None,
            "detail": "captured", "output": None,
        }]

    cfg = dict(config)
    cfg["products"] = [{"source": "fake", "_idx": i} for i in range(len(product_results))]
    with mock.patch.object(handler_mod, "load_config_from_s3", return_value=cfg), \
         mock.patch.object(runner_mod, "check_product",
                           side_effect=lambda entry, today, index=None: product_results[entry["_idx"]]), \
         mock.patch.object(handler_mod, "send_notifications", side_effect=fake_send):
        response = handler_mod.lambda_handler({}, None)
    return response, captured


alerts_cfg = {
    "notify_when": "alerts_only",
    "alert_thresholds_days": TH,
    "notifications": [{"type": "console"}],
}

# 12a. Health failure notifies under alerts_only with a TRACKER HEALTH subject.
resp, cap = _run_handler(alerts_cfg, [err])
assert resp["notified"] is True and resp["has_health_failures"] is True
assert resp["has_alerts"] is False
assert cap["subject"].startswith("[TRACKER HEALTH]"), cap["subject"]

# 12b. Undated approaching notifies under alerts_only as an EOL ALERT (R-01).
resp, cap = _run_handler(alerts_cfg, [undated])
assert resp["notified"] is True and resp["has_alerts"] is True
assert resp["has_health_failures"] is False
assert cap["subject"].startswith("[EOL ALERT]") and "TRACKER" not in cap["subject"]

# 12c. All-clear under alerts_only stays silent.
resp, cap = _run_handler(alerts_cfg, [R("ok", 365, "Fine")])
assert resp["notified"] is False and "subject" not in cap

# 12d. Combined risk + degraded health yields a double-tagged subject.
resp, cap = _run_handler(alerts_cfg, [R("eol"), unk])
assert resp["notified"] is True
assert cap["subject"].startswith("[EOL ALERT][TRACKER HEALTH]"), cap["subject"]


# ---------------------------------------------------------------------------
# 13. Real provider paths (still network-free): manual vs unknown sources
# ---------------------------------------------------------------------------
r_manual_ok = check_product({"source": "manual", "label": "Far Thing", "eol_date": "2099-01-01"}, TODAY)
a_manual_ok = analyse_results([r_manual_ok], TH)
assert a_manual_ok["has_lifecycle_alerts"] is False and len(a_manual_ok["ok"]) == 1

r_bogus = check_product({"source": "does_not_exist_xyz", "label": "Ghost"}, TODAY)
assert r_bogus["status"] == "error"
a_bogus = analyse_results([r_bogus], TH)
assert a_bogus["has_health_failures"] is True
_, alerts_fmt = format_report_text([r_bogus], TH, TODAY)
assert alerts_fmt is True

print("OK test_health_and_alerts")
