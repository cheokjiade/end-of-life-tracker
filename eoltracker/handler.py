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
    CONFIG_KEY      — Optional default S3 key for manual invocations
    SNS_TOPIC_ARN   — Optional default SNS topic for manual invocations
    SES_FROM_EMAIL  — SES sender for HTML email notifications (optional)
    SES_TO_EMAILS   — Optional default comma-separated SES recipients

Time-budget variables (see :mod:`eoltracker.runner`):
    EOL_MAX_WORKERS          — simultaneous provider checks (default: 4)
    EOL_TIME_RESERVE_MS      — rendering/delivery reserve (default: 15000)
    EOL_CHECK_START_GUARD_MS — extra time required to start a provider check
                               (default: 18000)
"""

import json
import os
import time
from datetime import date

from .core import (
    MAX_CONFIG_FILE_BYTES,
    logger,
    read_response_bytes,
)
from .report import (
    analyse_results,
    format_report_html,
    format_report_text,
    sanitize_text,
)
from .notify import (
    DeliveryFailureError,
    delivery_failed,
    running_in_lambda,
    send_notifications,
    summarize_outcomes,
)
from .runner import run_checks
from .validation import (
    config_bounds_error,
    load_validated_config_bytes,
)


# ---------------------------------------------------------------------------
# Subjects and notification decisions
# ---------------------------------------------------------------------------

def build_subject(analysis, project, today):
    """Compose the notification subject from a result analysis.

    Lifecycle risk and tracker-health degradation get distinct tags so a
    recipient can tell them apart at a glance:
      - ``[EOL ALERT]``         - eol/approaching products;
      - ``[TRACKER HEALTH]``    - unverifiable results or an empty inventory;
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
    return sanitize_text(
        f"{prefix}{proj_tag} Software End-of-Life Status - {today}")


def _emit_delivery_metrics(outcomes):
    """Emit a durable signal when any required channel is undelivered."""
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    undelivered = [
        outcome for outcome in outcomes
        if outcome.get("required") and not outcome.get("delivered")
    ]
    if not function_name or not undelivered:
        return
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "EOLTracker",
                "Dimensions": [["FunctionName"]],
                "Metrics": [{
                    "Name": "RequiredChannelsUndelivered",
                    "Unit": "Count",
                }],
            }],
        },
        "FunctionName": function_name,
        "RequiredChannelsUndelivered": len(undelivered),
    }
    print(json.dumps(payload, separators=(",", ":")))


def _emit_partial_run_metrics(unfinished):
    """Emit CloudWatch Embedded Metric Format only inside AWS Lambda."""
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        return
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "EOLTracker",
                "Dimensions": [["FunctionName"]],
                "Metrics": [
                    {"Name": "PartialRuns", "Unit": "Count"},
                    {"Name": "UnfinishedChecks", "Unit": "Count"},
                ],
            }],
        },
        "FunctionName": function_name,
        "PartialRuns": 1,
        "UnfinishedChecks": unfinished,
    }
    print(json.dumps(payload, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _log_product_findings(findings, origin):
    """Warn about per-product findings that did not reject the config."""
    for f in findings:
        logger.warning("%s: %s (%s): %s", origin, f["path"], f["severity"],
                       f["message"])


# Cap on how many config-level maven_repositories are offered per entry.
_MAX_STAMPED_REPOSITORIES = 8


def _stamp_maven_repositories(config, origin):
    """Offer declared repositories to maven_central entries at load time.

    Configs generated from dependency manifests carry a top-level
    ``maven_repositories`` list (artifact-repository URLs declared in the
    manifests). Every ``maven_central`` entry that declares neither an
    explicit ``repository`` nor its own ``repositories`` list gains
    ``entry["repositories"]`` = the first 8 URLs in config order, so the
    provider can fall back to them when an artifact is not found on Maven
    Central. Entries with an explicit ``repository`` (e.g. Shibboleth) or
    a hand-written ``repositories`` list are untouched. Validation has
    already enforced the list-of-strings shape; only counts are logged,
    never the URLs themselves.
    """
    declared = config.get("maven_repositories")
    if not isinstance(declared, list) or not declared:
        return
    if len(declared) > _MAX_STAMPED_REPOSITORIES:
        logger.warning(
            "%s: %d maven_repositories declared; offering the first %d only",
            origin, len(declared), _MAX_STAMPED_REPOSITORIES)
    offered = declared[:_MAX_STAMPED_REPOSITORIES]
    stamped = 0
    for entry in config.get("products") or []:
        if (isinstance(entry, dict)
                and entry.get("source") == "maven_central"
                and "repository" not in entry
                and "repositories" not in entry):
            entry["repositories"] = list(offered)
            stamped += 1
    if stamped:
        logger.info(
            "%s: offered %d declared repositories to %d maven_central "
            "entries", origin, len(offered), stamped)


def load_config_from_s3(key=None):
    """Load product configuration from S3.

    *key* overrides the CONFIG_KEY env var when supplied. EventBridge rules
    pass it via the invocation event so a single Lambda can fan out across
    many per-project config files.

    Raises :class:`~eoltracker.validation.ConfigValidationError` for
    structurally unusable configs (bad root/products/thresholds/notifications)
    before any provider runs; malformed individual product entries are logged
    and later surface as error rows instead of aborting the run. After
    validation, config-level ``maven_repositories`` are stamped onto
    ``maven_central`` entries lacking an explicit repository (see
    :func:`_stamp_maven_repositories`).
    """
    key = key or os.environ.get("CONFIG_KEY")
    if not key:
        raise ValueError(
            "no config_key was provided and CONFIG_KEY is not configured")
    import boto3

    bucket = os.environ["CONFIG_BUCKET"]
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    origin = f"s3://{bucket}/{key}"
    try:
        raw = read_response_bytes(obj["Body"], max_bytes=MAX_CONFIG_FILE_BYTES)
    except ValueError as exc:
        raise config_bounds_error(origin, str(exc)) from exc
    config, product_findings = load_validated_config_bytes(
        raw, origin=origin)
    _log_product_findings(product_findings, origin)
    _stamp_maven_repositories(config, origin)
    return config


def load_config_from_file(path):
    """Load product configuration from a local file (for testing).

    Reads bytes and enforces the same bounded-JSON contract as the S3
    loader and the ``--validate`` linter (size, nesting depth, UTF-8, JSON
    syntax, top-level object; see :func:`eoltracker.validation.check_config_bounds`), then
    applies :func:`enforce_valid_config` exactly like S3 loading —
    invalid top-level or runtime shapes raise
    :class:`ConfigValidationError` before providers run. After validation,
    config-level ``maven_repositories`` are stamped onto ``maven_central``
    entries lacking an explicit repository (see
    :func:`_stamp_maven_repositories`).
    """
    with open(path, "rb") as f:
        raw = f.read(MAX_CONFIG_FILE_BYTES + 1)
    config, product_findings = load_validated_config_bytes(raw, origin=path)
    _log_product_findings(product_findings, path)
    _stamp_maven_repositories(config, path)
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
    tracker-health failures (including an empty or section-only inventory).
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

    results, run_meta = run_checks(products, today, context=context)
    if run_meta.get("degraded"):
        logger.warning(
            "Time budget reached before %d product(s) could be checked; "
            "delivering a partial report",
            run_meta["unfinished"],
        )
        # Emit a human-searchable marker plus an EMF metric that pages the
        # operations topic. Keep both free of project/config data.
        logger.warning("EOL_PARTIAL_RUN unfinished=%d", run_meta["unfinished"])
        _emit_partial_run_metrics(run_meta["unfinished"])

    for r in results:
        logger.info("%s: %s", r["label"], r["message"])

    analysis = analyse_results(results, thresholds)
    report_text, _ = format_report_text(results, thresholds, today)
    report_html, _ = format_report_html(results, thresholds, today)

    # alerts_only must still fire on tracker-health failures:
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
        _emit_delivery_metrics(outcomes)
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
        "unfinished": run_meta.get("unfinished", 0),
        "required_channels_undelivered": sum(
            1 for o in outcomes
            if o.get("required") and not o.get("delivered")
        ),
    }


# ---------------------------------------------------------------------------
# Local testing  (python lambda_function.py [config.json])
# ---------------------------------------------------------------------------

def run_local(config_path, context=None):
    """Run a check against a local config file and dispatch notifications.

    Holds the body of the original ``__main__`` block so the shim's CLI and
    any caller can invoke it directly. Local runs never raise on delivery
    failure; per-channel outcomes are printed instead. *context* is optional;
    pass an object exposing ``get_remaining_time_in_millis()`` to exercise the
    Lambda budget in a test.
    """
    config = load_config_from_file(config_path)

    today = date.today()
    products = config["products"]
    thresholds = config.get("alert_thresholds_days", [30, 60, 90])

    results, _run_meta = run_checks(products, today, context=context)
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
