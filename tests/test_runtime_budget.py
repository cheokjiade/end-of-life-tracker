"""Network-free tests for eoltracker.runner (issue #8 / audit R-04).

Proves, with fake providers and a scripted Lambda context:
  * bounded concurrency (never more than EOL_MAX_WORKERS checks in flight),
  * deterministic result ordering regardless of completion order,
  * deduplication of identical lookups with per-entry label/policy_note,
  * the time-reserve behaviour (stop scheduling, normalise unfinished work),
  * partial report rendering + notification under timeout pressure,
  * lambda_handler still answers with degradation metadata.

Run:  python tests/test_runtime_budget.py
"""

import json
import os
import sys
import tempfile
import threading
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker import runner, handler

TODAY = date(2026, 8, 27)


class FakeContext:
    """Scripted Lambda context: returns prepared readings in order."""

    def __init__(self, *readings):
        self.readings = list(readings)
        self.calls = 0

    def get_remaining_time_in_millis(self):
        self.calls += 1
        if not self.readings:
            raise AssertionError("FakeContext out of scripted readings")
        if len(self.readings) > 1:
            return self.readings.pop(0)
        return self.readings[0]          # stick at final value


def make_result(entry):
    return {
        "label": entry.get("label"),
        "product": entry.get("product"),
        "version": entry.get("version"),
        "status": "ok",
        "message": f"{entry.get('label')} checked",
        "days_remaining": None,
        "source": entry.get("source", "fake"),
    }


def install_fake_provider(delays_by_label, recorder=None):
    """Patch runner.check_product with a sleeping fake.

    Returns (active-monitor, call-counter list). Sleeps per-label come from
    *delays_by_label* (missing label -> fast path).
    """
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def fake_check(entry, today, index=None):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            if recorder is not None:
                recorder.append((entry.get("product"), entry.get("version")))
        time.sleep(delays_by_label.get(entry.get("label"), 0.01))
        with lock:
            state["active"] -= 1
        return make_result(entry)

    runner.check_product = fake_check
    return state


# ---------------------------------------------------------------------------
# 1. Concurrency bound + deterministic ordering
# ---------------------------------------------------------------------------

entries = [
    {"source": "manual", "product": f"prod{i:02d}", "version": "1.0",
     "label": f"L{i:02d}"}
    for i in range(12)
]
state = install_fake_provider({f"L{i:02d}": 0.05 for i in range(12)})
results, meta = runner.run_checks(entries, TODAY, max_workers=3,
                                  time_reserve_ms=0)

assert state["peak"] <= 3, f"concurrency bound violated: peak={state['peak']}"
assert len(results) == 12, results
assert [r["label"] for r in results] == [f"L{i:02d}" for i in range(12)], \
    [r["label"] for r in results]
assert meta["scheduled"] == 12 and meta["degraded"] is False, meta
print("OK concurrency bound preserved and ordering deterministic")

# ---------------------------------------------------------------------------
# 2. Ordering survives mixed completion times and section dividers
# ---------------------------------------------------------------------------

runner_check_original = runner.check_product


def instant_check(entry, today, index=None):
    time.sleep({"slow-a": 0.10, "slow-b": 0.15}.get(entry.get("label"), 0))
    return make_result(entry)


runner.check_product = instant_check
mixed = [
    {"source": "manual", "label": "slow-a"},
    {"_section": "── Divider ──"},
    {"source": "manual", "label": "slow-b"},
    {"source": "manual", "label": "mid-1"},
    {"source": "manual", "label": "fast-1"},
    {"source": "manual", "label": "mid-2"},
]
results, _meta = runner.run_checks(mixed, TODAY, max_workers=6,
                                   time_reserve_ms=0)
labels = [r["label"] for r in results]
assert labels == ["slow-a", "slow-b", "mid-1", "fast-1", "mid-2"], labels
print("OK section dividers dropped, order stable under jittered latencies")

# ---------------------------------------------------------------------------
# 3. Identical lookups execute once; curated fields stay per-entry
# ---------------------------------------------------------------------------

calls = []
state = install_fake_provider({}, recorder=calls)
dupes = [
    {"source": "endoflife_date", "product": "python", "version": "3.9",
     "label": "Alpha runtime"},
    {"source": "endoflife_date", "product": "python", "version": "3.9",
     "label": "Beta runtime", "policy_note": "Team-approved upgrade cadence."},
    {"source": "endoflife_date", "product": "nginx", "version": "1.25",
     "label": "Web proxy"},
]
results, meta = runner.run_checks(dupes, TODAY, max_workers=4,
                                  time_reserve_ms=0)
assert len(calls) == 2, f"expected 2 upstream lookups, got {len(calls)}: {calls}"
assert ("python", "3.9") in calls and ("nginx", "1.25") in calls, calls
assert meta["scheduled"] == 2 and meta["dedup_hits"] == 1, meta
assert len(results) == 3
alpha, beta, web = results
assert alpha["label"] == "Alpha runtime"
assert beta["label"] == "Beta runtime"
assert beta["policy_note"] == "Team-approved upgrade cadence."
assert "policy_note" not in alpha and "policy_note" not in web
assert [r["label"] for r in results] == ["Alpha runtime", "Beta runtime", "Web proxy"]
print("OK lookup dedupe executes once and re-stamps curated fields")

runner.check_product = runner_check_original

# ---------------------------------------------------------------------------
# 4. Time reserve: scheduling stops, unfinished work becomes error results
# ---------------------------------------------------------------------------

delays = {"P0-ok": 0.02, "P1-slow": 0.6, "P2-slow": 0.6, "P3-slow": 0.6}
state = install_fake_provider(delays)
products = [
    {"source": "manual", "product": "p0", "version": "1.0", "label": "P0-ok"},
    {"source": "manual", "product": "p1", "version": "2.0", "label": "P1-slow"},
    {"source": "manual", "product": "p2", "version": "3.0", "label": "P2-slow"},
    {"source": "manual", "product": "p3", "version": "4.0", "label": "P3-slow"},
]
ctx = FakeContext(30000, 30000, 50)
results, meta = runner.run_checks(products, TODAY, context=ctx,
                                  max_workers=1, time_reserve_ms=200,
                                  check_start_guard_ms=200)
assert ctx.calls >= 2, "context remaining-time was not consulted"
assert results[0]["status"] == "ok" and results[0]["label"] == "P0-ok"
for r in results[1:]:
    assert r["status"] == "error", r
    assert r.get("incomplete") is True, r
    assert "time" in r["message"].lower(), r
    assert r["days_remaining"] is None and r["label"].startswith("P"), r
assert [r["label"] for r in results] == ["P0-ok", "P1-slow", "P2-slow", "P3-slow"]
assert meta["scheduled"] == 1, "work must be submitted lazily"
assert meta["unfinished"] == 3 and meta["degraded"] is True, meta
print("OK reserve stops scheduling and normalises unfinished checks")

# ---------------------------------------------------------------------------
# 5. Partial report + notification still delivered under timeout pressure
# ---------------------------------------------------------------------------

runner_check_saved = runner.check_product


def staged_check(entry, today, index=None):
    if entry.get("label") == "Only-Fast":
        return make_result(entry)
    time.sleep(0.5)
    return make_result(entry)


runner.check_product = staged_check
old_cwd = os.getcwd()
tmp_root = tempfile.mkdtemp(prefix="eol-runner-test-")
try:
    os.chdir(tmp_root)
    config_path = os.path.join(tmp_root, "cfg.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({
            "alert_thresholds_days": [30, 60, 90],
            "notifications": [
                {"type": "console"},
                {"type": "html_file", "path": "report.html"},
            ],
            "products": [
                {"source": "manual", "product": "fast-one", "version": "1.0",
                 "label": "Only-Fast"},
                {"source": "manual", "product": "never-a", "version": "2.0",
                 "label": "Never-Checked-A"},
                {"source": "manual", "product": "never-b", "version": "3.0",
                 "label": "Never-Checked-B"},
            ],
        }, f)

    ctx = FakeContext(900000, 900000, 100)
    os.environ["EOL_MAX_WORKERS"] = "1"
    os.environ["EOL_TIME_RESERVE_MS"] = "200"
    os.environ["EOL_CHECK_START_GUARD_MS"] = "200"
    try:
        handler.run_local(config_path, context=ctx)
    finally:
        del os.environ["EOL_MAX_WORKERS"]
        del os.environ["EOL_TIME_RESERVE_MS"]
        del os.environ["EOL_CHECK_START_GUARD_MS"]

    # The html_file channel proves rendering and notification ran despite the
    # budget cutting most checks short.
    hits = []
    for base, _dirs, files in os.walk("reports"):
        for name in files:
            if name.endswith(".html"):
                hits.append(os.path.join(base, name))
    assert len(hits) == 1, f"expected one HTML report, found {hits}"
    with open(hits[0], encoding="utf-8") as f:
        body = f.read()
    assert "Only-Fast" in body, body[:500]
    for missing in ("Never-Checked-A", "Never-Checked-B"):
        assert missing in body, f"{missing} absent from partial report:\n{body[:500]}"
    assert "TRACKER HEALTH" in body, body[:1200]
    assert "insufficient Lambda time" in body, body[:2000]
finally:
    os.chdir(old_cwd)
    runner.check_product = runner_check_saved
print("OK partial report rendered and notified under timeout pressure")

# ---------------------------------------------------------------------------
# 6. lambda_handler surfaces checked/unfinished metadata
# ---------------------------------------------------------------------------

state = install_fake_provider({"Handler-Fast": 0.01, "Handler-Slow": 0.4,
                               "Handler-Skipped": 0.3})

handler_cfg = {
    "notify_when": "always",
    "notifications": [{"type": "console"}],
    "products": [
        {"source": "manual", "product": "h0", "version": "1.0", "label": "Handler-Fast"},
        {"source": "manual", "product": "h1", "version": "2.0", "label": "Handler-Slow"},
        {"source": "manual", "product": "h2", "version": "3.0", "label": "Handler-Skipped"},
    ],
}
original_loader = handler.load_config_from_s3
handler.load_config_from_s3 = lambda key=None: dict(handler_cfg)
try:
    os.environ["EOL_MAX_WORKERS"] = "1"
    os.environ["EOL_TIME_RESERVE_MS"] = "200"
    os.environ["EOL_CHECK_START_GUARD_MS"] = "200"
    resp = handler.lambda_handler({}, FakeContext(60000, 60000, 60))
finally:
    del os.environ["EOL_MAX_WORKERS"]
    del os.environ["EOL_TIME_RESERVE_MS"]
    del os.environ["EOL_CHECK_START_GUARD_MS"]
    handler.load_config_from_s3 = original_loader

assert resp["statusCode"] == 200, resp
assert resp["checked"] == 3, resp
assert resp["unfinished"] == 2, resp
assert resp["has_alerts"] is False and resp["notified"] is True, resp
print("OK lambda_handler reports degradation metadata")

# ---------------------------------------------------------------------------
# 7. Env-var knob parsing degrades safely
# ---------------------------------------------------------------------------

os.environ["EOL_TIME_RESERVE_MS"] = "not-a-number"
try:
    assert runner._positive_int_env("EOL_TIME_RESERVE_MS", 12345) == 12345
finally:
    del os.environ["EOL_TIME_RESERVE_MS"]
os.environ["EOL_MAX_WORKERS"] = "4"
try:
    assert runner._positive_int_env("EOL_MAX_WORKERS", 8) == 4
finally:
    del os.environ["EOL_MAX_WORKERS"]
print("OK environment knobs parse with safe fallbacks")

print("OK test_runtime_budget")
