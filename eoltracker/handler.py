"""Entry points: config loading, the Lambda handler, and the local CLI runner.

Configuration is loaded from an S3 JSON file so products can be updated
without redeploying the Lambda. Alerts are sent via SNS, SES, console, or
HTML file (multiple channels can be enabled at once).

On AWS Lambda a failed delivery is an invocation failure: when every required
channel ends up undelivered (all errored or skipped), the handler
raises ``DeliveryFailureError`` so Lambda's asynchronous retries, dead-letter
queue, and CloudWatch alarms engage. Locally, runs print the per-channel
outcomes and never raise. See README ("Delivery outcomes and failure
handling") and ``eoltracker/notify.py`` for the outcome contract.

Environment variables:
    CONFIG_BUCKET   — S3 bucket containing the config file
    CONFIG_KEY      — S3 key for the config file (default: eol_config.json)
    SNS_TOPIC_ARN   — SNS topic ARN for plain-text email notifications
    SES_FROM_EMAIL  — SES sender for HTML email notifications (optional)
    SES_TO_EMAILS   — Comma-separated SES recipients (optional)
"""

import os
from datetime import date

from .core import logger
from .parsers import check_product
from .report import analyse_results, format_report_text, format_report_html
from .notify import (
    DeliveryFailureError,
    delivery_failed,
    running_in_lambda,
    send_notifications,
    summarize_outcomes,
)
from .validation import ConfigValidationError, load_validated_config_bytes


# ---------------------------------------------------------------------------
# Subjects and notification decisions
# ---------------------------------------------------------------------------

def build_subject(analysis, project, today):
    """Compose the notification subject from a result analysis.

    Lifecycle risk and tracker-health degradation get distinct tags so a
    recipient can tell them apart at a glance:
      - ``[EOL ALERT]``         - eol/approaching products;
      - ``[TRACKER HEALTH]``    - error/unknown results (check failures);
      - both may appear together as ``[EOL ALERT][TRACKER HEALTH]``.
    A run with neither is an informational ``[EOL Report]``.
    """
    tags = []
    if analysis.get("has_lifecycle_alerts"):
        tags.append("EOL ALERT")
    if analysis.get("has_health_failures"):
        tags.append("TRACKER HEALTH")
    prefix = "[" + "][".join(tags) + "]" if tags else "[EOL Report]"
    proj_tag = f" [{project}]" if project else ""
    return f"{prefix}{proj_tag} Software End-of-Life Status - {today}"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _log_product_findings(findings, origin):
    """Warn about per-product findings that did not reject the config."""
    for f in findings:
        logger.warning("%s: %s (%s): %s", origin, f["path"], f["severity"],
                       f["message"])


def load_config_from_s3(key=None):
    """Load product configuration from S3.

    *key* overrides the CONFIG_KEY env var when supplied. EventBridge rules
    pass it via the invocation event so a single Lambda can fan out across
    many per-project config files.

    Raises :class:`~eoltracker.validation.ConfigValidationError` for
    structurally unusable configs (bad root/products/thresholds/notifications)
    before any provider runs; malformed individual product entries are logged
    and later surface as error rows instead of aborting the run.
    """
    import boto3

    bucket = os.environ["CONFIG_BUCKET"]
    key = key or os.environ.get("CONFIG_KEY", "eol_config.a.json")
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    origin = f"s3://{bucket}/{key}"
    config, product_findings = load_validated_config_bytes(
        obj["Body"].read(), origin=origin)
    _log_product_findings(product_findings, origin)
    return config


def load_config_from_file(path):
    """Load product configuration from a local file (for testing).

    Reads bytes and enforces the same ASCII-only + JSON rules as the
    ``--validate`` linter (see :mod:`eoltracker.validation`), then applies
    :func:`enforce_valid_config` exactly like S3 loading — invalid top-level
    or runtime shapes raise :class:`ConfigValidationError` before providers
    run.
    """
    with open(path, "rb") as f:
        raw = f.read()

    config, product_findings = load_validated_config_bytes(raw, origin=path)
    _log_product_findings(product_findings, path)
    return config


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

    With ``notify_when: "alerts_only"`` a notification is sent for lifecycle
    alerts (eol/approaching, including undated at-risk phases) and for
    tracker-health failures (error/unknown results).
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

    results = []
    for i, entry in enumerate(products):
        result = check_product(entry, today, index=i)
        if result is not None:
            results.append(result)

    for r in results:
        logger.info("%s: %s", r["label"], r["message"])

    analysis = analyse_results(results, thresholds)
    report_text, _ = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    # alerts_only must still fire on tracker-health failures (error/unknown):
    # an unverifiable run is never a reason to stay silent.
    should_notify = (
        notify_when == "always"
        or analysis["has_lifecycle_alerts"]
        or analysis["has_health_failures"]
    )

    outcomes = []
    if should_notify:
        subject = build_subject(analysis, project, today)
        outcomes = send_notifications(
            config, report_text, report_html, subject,
            runtime_overrides=runtime_overrides,
        ) or []
        for line in summarize_outcomes(outcomes).splitlines():
            logger.info("Delivery %s", line)
        if delivery_failed(outcomes):
            # No required channel delivered. In Lambda mode this must be an
            # invocation failure so retries/DLQ/alarms engage (R-03); local
            # runs just surface the summary and continue.
            if running_in_lambda():
                required_failures = [o for o in outcomes if o.get("required")]
                raise DeliveryFailureError(
                    "all required notification channels failed to deliver: "
                    + "; ".join(
                        "{}: {}".format(o["channel"],
                                        "skipped: " + o["detail"] if o["skipped"]
                                        else o["error"] or o["detail"])
                        for o in required_failures
                    )
                )
            logger.error("All required notification channels failed to deliver")
    else:
        logger.info("No alerts and notify_when=alerts_only — skipping notification")

    return {
        "statusCode": 200,
        "project": project,
        "checked": len(results),
        "has_alerts": analysis["has_lifecycle_alerts"],
        "has_health_failures": analysis["has_health_failures"],
        # True only when at least one configured channel actually delivered.
        "notified": any(o["delivered"] for o in outcomes),
        "notification_outcomes": outcomes,
    }


# ---------------------------------------------------------------------------
# Local testing  (python lambda_function.py [config.json])
# ---------------------------------------------------------------------------

def run_local(config_path):
    """Run a check against a local config file and dispatch notifications.

    Holds the body of the original ``__main__`` block so the shim's CLI and
    any caller can invoke it directly. Local runs never raise on delivery
    failure; per-channel outcomes are printed instead.
    """
    config = load_config_from_file(config_path)

    today = date.today()
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])

    results = []
    for i, entry in enumerate(products):
        result = check_product(entry, today, index=i)
        if result is not None:
            results.append(result)
    analysis = analyse_results(results, thresholds)
    report_text, _ = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    subject = build_subject(analysis, None, today)

    outcomes = send_notifications(config, report_text, report_html, subject)
    print("Notification channels:")
    print(summarize_outcomes(outcomes))
    if delivery_failed(outcomes):
        print("WARNING: no required notification channel delivered; see details above.")
        return outcomes
    return outcomes
