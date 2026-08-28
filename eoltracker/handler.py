"""Entry points: config loading, the Lambda handler, and the local CLI runner.

Configuration is loaded from an S3 JSON file so products can be updated
without redeploying the Lambda. Alerts are sent via SNS, SES, console, or
HTML file (multiple channels can be enabled at once).

Environment variables:
    CONFIG_BUCKET   — S3 bucket containing the config file
    CONFIG_KEY      — S3 key for the config file (default: eol_config.json)
    SNS_TOPIC_ARN   — SNS topic ARN for plain-text email notifications
    SES_FROM_EMAIL  — SES sender for HTML email notifications (optional)
    SES_TO_EMAILS   — Comma-separated SES recipients (optional)
"""

import json
import os
from datetime import date

from .core import logger
from .parsers import check_product
from .report import format_report_text, format_report_html, sanitize_text
from .notify import send_notifications


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config_from_s3(key=None):
    """Load product configuration from S3.

    *key* overrides the CONFIG_KEY env var when supplied. EventBridge rules
    pass it via the invocation event so a single Lambda can fan out across
    many per-project config files.
    """
    import boto3

    bucket = os.environ["CONFIG_BUCKET"]
    key = key or os.environ.get("CONFIG_KEY", "eol_config.a.json")
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def load_config_from_file(path):
    """Load product configuration from a local file (for testing)."""
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """Entry point for AWS Lambda.

    The invocation event may carry per-project overrides:
      - config_key      — S3 key of the project's config file
      - project         — display name (used in the subject line and logs)
      - sns_topic_arn   — destination SNS topic for this project
      - ses_from_email  — sender for SES notifications
      - ses_to_emails   — comma-separated recipients for SES notifications

    All overrides fall back to the existing env vars when absent.
    """
    today = date.today()
    event = event or {}

    config_key = event.get("config_key")
    project = event.get("project")
    runtime_overrides = {
        k: v for k, v in {
            "sns_topic_arn":  event.get("sns_topic_arn"),
            "ses_from_email": event.get("ses_from_email"),
            "ses_to_emails":  event.get("ses_to_emails"),
        }.items() if v
    }

    if project:
        logger.info("Running EOL check for project '%s' (config=%s)", project, config_key or "<env default>")

    config = load_config_from_s3(config_key)
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])
    notify_when = config.get("notify_when", "always")  # "always" | "alerts_only"

    logger.info("Checking %d products for EOL status", len(products))

    results = [r for r in (check_product(entry, today) for entry in products) if r is not None]

    for r in results:
        logger.info("%s: %s", r["label"], r["message"])

    report_text, has_alerts = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    should_notify = notify_when == "always" or has_alerts

    if should_notify:
        prefix = "EOL ALERT" if has_alerts else "EOL Report"
        proj_tag = f" [{project}]" if project else ""
        subject = sanitize_text(
            f"[{prefix}]{proj_tag} Software End-of-Life Status - {today}")
        send_notifications(config, report_text, report_html, subject,
                           runtime_overrides=runtime_overrides)
    else:
        logger.info("No alerts and notify_when=alerts_only — skipping notification")

    return {
        "statusCode": 200,
        "project": project,
        "checked": len(results),
        "has_alerts": has_alerts,
        "notified": should_notify,
    }


# ---------------------------------------------------------------------------
# Local testing  (python lambda_function.py [config.json])
# ---------------------------------------------------------------------------

def run_local(config_path):
    """Run a check against a local config file and dispatch notifications.

    Holds the body of the original ``__main__`` block so the shim's CLI and
    any caller can invoke it directly.
    """
    config = load_config_from_file(config_path)

    today = date.today()
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])

    results = [r for r in (check_product(entry, today) for entry in products) if r is not None]
    report_text, has_alerts = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    prefix = "EOL ALERT" if has_alerts else "EOL Report"
    subject = f"[{prefix}] Software End-of-Life Status - {today}"

    send_notifications(config, report_text, report_html, subject)
