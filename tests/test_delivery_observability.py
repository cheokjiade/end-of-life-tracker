"""Network-free delivery metric and routing-default tests (issue #7)."""

import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eoltracker.handler as handler


OUTCOMES = [
    {"channel": "sns", "required": True, "delivered": False},
    {"channel": "ses", "required": True, "delivered": True},
    {"channel": "console", "required": False, "delivered": True},
]

original_function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
try:
    os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "eol-test"
    captured = io.StringIO()
    with redirect_stdout(captured):
        handler._emit_delivery_metrics(OUTCOMES)
finally:
    if original_function_name is None:
        os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    else:
        os.environ["AWS_LAMBDA_FUNCTION_NAME"] = original_function_name

emf = json.loads(captured.getvalue())
assert emf["FunctionName"] == "eol-test", emf
assert emf["RequiredChannelsUndelivered"] == 1, emf
directive = emf["_aws"]["CloudWatchMetrics"][0]
assert directive["Namespace"] == "EOLTracker", directive
assert directive["Dimensions"] == [["FunctionName"]], directive
assert directive["Metrics"][0]["Name"] == "RequiredChannelsUndelivered"

# Optional failures do not create alarm noise, and local runs emit no EMF.
captured = io.StringIO()
with redirect_stdout(captured):
    handler._emit_delivery_metrics([
        {"channel": "console", "required": False, "delivered": False},
    ])
assert captured.getvalue() == ""

# A multi-project deployment must never guess which S3 object to load.
original_config_key = os.environ.pop("CONFIG_KEY", None)
try:
    try:
        handler.load_config_from_s3()
        raise AssertionError("missing config key was accepted")
    except ValueError as exc:
        assert "no config_key" in str(exc), exc
finally:
    if original_config_key is not None:
        os.environ["CONFIG_KEY"] = original_config_key

print("OK test_delivery_observability")
