"""Delivery-outcome tests for eoltracker.notify (audit R-03) - network-free.

Covers: structured attempted/delivered/skipped/error outcomes per channel,
later channels being attempted after earlier failures, skip semantics for
missing routing config, and recipient redaction in logs and outcomes.
boto3 is faked via sys.modules injection (never imported for real).
"""
import io
import json
import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eoltracker.notify as notify


ORIGINAL_BOTO3 = sys.modules.get("boto3")


def dump(obj):
    return json.dumps(obj)


class FakeSNS:
    """Fake boto3 SNS client."""

    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    def publish(self, **kwargs):
        if self.fail:
            raise RuntimeError("ThrottlingException: rate exceeded")
        self.published.append(kwargs)


class FakeSES:
    """Fake boto3 SES client."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_email(self, **kwargs):
        if self.fail:
            raise RuntimeError("MessageRejected: sending not verified")
        self.sent.append(kwargs)


def install_fake_boto3(sns=None, ses=None):
    mod = types.ModuleType("boto3")
    mod.client = lambda name, *a, **k: {"sns": sns, "ses": ses}[name]
    sys.modules["boto3"] = mod


OUTCOME_KEYS = {
    "channel", "required", "attempted", "delivered", "skipped", "error",
    "detail", "output",
}

CFG_SNS_SES_FAIL_FIRST = {
    "products": [],
    "notifications": [
        {"type": "sns", "topic_arn": "arn:aws:sns:eu-west-1:123:eol"},
        {"type": "ses", "from_email": "noreply@example.com",
         "to_emails": ["team@example.com"]},
    ],
}


def outcomes_by_channel(outcomes):
    return {o["channel"]: o for o in outcomes}


# --- 1. partial failure: later channel still attempted after earlier error ---
sns, ses = FakeSNS(fail=True), FakeSES()
install_fake_boto3(sns=sns, ses=ses)
out = notify.send_notifications(CFG_SNS_SES_FAIL_FIRST, "text", "<html>", "subj")
assert len(out) == 2, out
for o in out:
    assert set(o.keys()) == OUTCOME_KEYS, o
byc = outcomes_by_channel(out)
assert byc["sns"]["attempted"] is True
assert byc["sns"]["delivered"] is False
assert byc["sns"]["error"] == "RuntimeError", byc["sns"]
assert byc["sns"]["required"] is True and byc["ses"]["required"] is True
assert byc["ses"]["attempted"] is True, "SES must be attempted after SNS failed"
assert byc["ses"]["delivered"] is True
assert byc["ses"]["error"] is None
assert len(ses.sent) == 1, "fake SES must have been called exactly once"
assert notify.delivery_failed(out) is False, "partial delivery is not total failure"
print("OK partial-failure ordering + outcome shape")

# --- 2. total failure: every channel errors -> delivery_failed true ---
install_fake_boto3(sns=FakeSNS(fail=True), ses=FakeSES(fail=True))
out = notify.send_notifications(CFG_SNS_SES_FAIL_FIRST, "text", "<html>", "subj")
byc = outcomes_by_channel(out)
assert all(not o["delivered"] for o in out), out
assert byc["sns"]["error"] == "RuntimeError"
assert byc["ses"]["error"] == "RuntimeError"
assert notify.delivery_failed(out) is True
print("OK total-failure detection")

# --- 3. missing routing config is 'skipped', not 'error' ---
cfg_missing = {"notifications": [{"type": "sns"}, {"type": "ses"}]}
out = notify.send_notifications(cfg_missing, "t", "<html/>", "s")
byc = outcomes_by_channel(out)
assert byc["sns"]["skipped"] is True and byc["sns"]["attempted"] is False
assert byc["sns"]["delivered"] is False and byc["sns"]["error"] is None
assert byc["ses"]["skipped"] is True and byc["ses"]["attempted"] is False
assert notify.delivery_failed(out) is True, "all-skipped means nothing delivered"
print("OK skip semantics for unconfigured channels")

# --- 4. unknown notification type is skipped, attempted stays False ---
cfg_unknown = {"notifications": [{"type": "carrier_pigeon"}]}
out = notify.send_notifications(cfg_unknown, "t", "<html/>", "s")
o = out[0]
assert o["channel"] == "carrier_pigeon"
assert o["skipped"] is True and o["attempted"] is False and o["delivered"] is False
print("OK unknown type skipped")

# --- 5. delivered when everything works; no recipients leak into logs ---
captured = io.StringIO()
h = logging.StreamHandler(captured)
h.setLevel(logging.INFO)
notify.logger.addHandler(h)
try:
    install_fake_boto3(sns=FakeSNS(), ses=FakeSES())
    out = notify.send_notifications(CFG_SNS_SES_FAIL_FIRST, "t", "<html/>", "s")
    byc = outcomes_by_channel(out)
    assert byc["sns"]["delivered"] and byc["ses"]["delivered"]
    log_text = captured.getvalue()
    assert "@" not in log_text, "recipient address leaked into logs: %r" % log_text
    assert "team@example.com" not in dump(out), \
        "recipient address leaked into delivery outcomes"
finally:
    notify.logger.removeHandler(h)
print("OK redaction: no recipient details in logs or outcomes")

# --- 6. optional delivery cannot mask failure of every durable route -------
install_fake_boto3(sns=FakeSNS(fail=True), ses=FakeSES())
cfg_console_and_sns = {"notifications": [
    {"type": "console"},
    {"type": "sns", "topic_arn": "arn:aws:sns:eu-west-1:123:eol"},
]}
out = notify.send_notifications(cfg_console_and_sns, "t", "<html/>", "s")
byc = outcomes_by_channel(out)
assert byc["console"]["delivered"] and not byc["console"]["required"]
assert not byc["sns"]["delivered"] and byc["sns"]["required"]
assert notify.delivery_failed(out) is True

# With no required channels, a local-only config never asks Lambda to retry.
out = notify.send_notifications(
    {"notifications": [{"type": "carrier_pigeon"}]}, "t", "<html/>", "s")
assert notify.delivery_failed(out) is False

# Operators can deliberately make any channel required.
out = notify.send_notifications(
    {"notifications": [{"type": "carrier_pigeon", "required": True}]},
    "t", "<html/>", "s")
assert notify.delivery_failed(out) is True
print("OK required-channel semantics")

# --- 7. Config routing wins visibly when an invocation also supplies routing.
captured = io.StringIO()
h = logging.StreamHandler(captured)
notify.logger.addHandler(h)
try:
    install_fake_boto3(sns=FakeSNS(), ses=FakeSES())
    out = notify.send_notifications(
        CFG_SNS_SES_FAIL_FIRST, "t", "<html/>", "s",
        runtime_overrides={
            "sns_topic_arn": "arn:aws:sns:eu-west-1:123:event-topic",
            "ses_from_email": "event-sender@example.com",
            "ses_to_emails": "event-recipient@example.com",
        },
    )
finally:
    notify.logger.removeHandler(h)
assert all(o["delivered"] for o in out), out
conflict_log = captured.getvalue()
assert "overrides the invocation routing value" in conflict_log, conflict_log
assert "event-topic" not in conflict_log and "@" not in conflict_log, conflict_log
print("OK routing conflict warnings are destination-free")

if ORIGINAL_BOTO3 is None:
    sys.modules.pop("boto3", None)
else:
    sys.modules["boto3"] = ORIGINAL_BOTO3

print("OK test_delivery_outcomes")
