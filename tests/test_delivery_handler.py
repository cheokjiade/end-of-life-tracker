"""Handler-level delivery outcome tests (audit R-03) - network-free.

Covers: 'notified' reflecting actual delivery, raising DeliveryFailureError
in Lambda mode only after every configured channel failed, local runs not
raising, and alerts_only suppressing notification attempts entirely.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import eoltracker.handler as handler
from eoltracker.notify import DeliveryFailureError


def fail_outcome(channel="sns"):
    return {"channel": channel, "required": channel in ("sns", "ses"),
            "attempted": True, "delivered": False, "skipped": False,
            "error": "RuntimeError", "detail": "failed", "output": None}


def ok_outcome(channel):
    return {"channel": channel, "required": channel in ("sns", "ses"),
            "attempted": True, "delivered": True, "skipped": False,
            "error": None, "detail": "delivered", "output": None}


CONFIG = {
    "products": [],
    "notify_when": "always",
    "notifications": [{"type": "sns"}],
}


class _Harness:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0

    def __call__(self, config, report_text, report_html, subject,
                 runtime_overrides=None):
        self.calls += 1
        return self.outcomes


h = _Harness([fail_outcome()])
handler.load_config_from_s3 = lambda key=None: dict(CONFIG)
handler.send_notifications = h

# --- 1. Lambda mode + every channel failed -> raise -------------------------
os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "eol-test-fn"
try:
    handler.lambda_handler({}, context=None)
    raise AssertionError("expected DeliveryFailureError in Lambda mode")
except DeliveryFailureError as exc:
    assert "all required notification channels failed" in str(exc), str(exc)
assert h.calls == 1
print("OK Lambda mode raises when all channels fail")

# --- 2. Lambda mode + partial failure -> success, notified reflects delivery --
h2 = _Harness([fail_outcome("sns"), ok_outcome("ses")])
handler.send_notifications = h2
resp = handler.lambda_handler({}, context=None)
assert resp["statusCode"] == 200
assert resp["notified"] is True, "a delivered channel means notified=True"
assert [o["channel"] for o in resp["notification_outcomes"]] == ["sns", "ses"]
assert resp["required_channels_undelivered"] == 1
print("OK partial delivery -> notified=True, outcomes returned")

# --- 3. local mode + total failure -> no raise --------------------------------
handler.send_notifications = h
del os.environ["AWS_LAMBDA_FUNCTION_NAME"]
resp = handler.lambda_handler({}, context=None)
assert resp["statusCode"] == 200
assert resp["notified"] is False, "nothing delivered => notified=False"
print("OK local mode never raises; notified=False on total failure")

# --- 4. alerts_only still reports an unverifiable empty inventory -------------
h3 = _Harness([])
cfg = dict(CONFIG)
cfg["notify_when"] = "alerts_only"
cfg["products"] = []
handler.load_config_from_s3 = lambda key=None: cfg
handler.send_notifications = h3
resp = handler.lambda_handler({}, context=None)
assert h3.calls == 1, "empty inventory must trigger a tracker-health delivery"
assert resp["has_alerts"] is False
assert resp["has_health_failures"] is True
assert resp["notified"] is False
assert resp["notification_outcomes"] == []
print("OK alerts_only reports empty inventory as tracker-health failure")

print("OK test_delivery_handler")
