"""Notification channels.

Dispatches a rendered report to every channel listed in the config:
console, HTML file, SNS, or SES (multiple may be enabled at once). ``boto3``
is imported lazily inside the SNS/SES paths so the stdlib-only channels stay
dependency-free.
"""

import os
from datetime import datetime

from .core import logger


def _notify_console(report_text, **_kwargs):
    """Print the plain-text report to stdout."""
    print(report_text)


def _project_from_base(base):
    """Derive the project segment from an html_file base name.

    'eol_report_a'  -> 'a'
    'eol_report'      -> 'default'
    anything else     -> the base itself (best effort)
    """
    if base == "eol_report":
        return "default"
    if base.startswith("eol_report_"):
        return base[len("eol_report_"):] or "default"
    return base or "default"


def _notify_html_file(report_html, notif_config, **_kwargs):
    """Write the HTML report under reports/<project>/<year>/<month>/<day>/.

    The project is derived from the configured path's base name; the dated
    folders and the filename's timestamp both come from the current local time,
    so the folder path and filename always agree. Each run produces a uniquely
    named file, e.g.
    reports/a/2026/05/03/eol_report_a_2026-05-03_1430.html.
    """
    path = notif_config.get("path", "eol_report.html")
    base, ext = os.path.splitext(os.path.basename(path))
    now = datetime.now()
    out_dir = os.path.join(
        "reports", _project_from_base(base),
        now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"),
    )
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{base}_{now:%Y-%m-%d_%H%M}{ext}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_html)
    logger.info("HTML report written to %s", path)


def _notify_sns(report_text, subject, notif_config, runtime_overrides=None, **_kwargs):
    """Publish the plain-text report to an SNS topic.

    Topic ARN resolution: notif.topic_arn > event override > SNS_TOPIC_ARN env var.
    """
    import boto3

    overrides = runtime_overrides or {}
    topic_arn = (
        notif_config.get("topic_arn")
        or overrides.get("sns_topic_arn")
        or os.environ.get("SNS_TOPIC_ARN")
    )
    if not topic_arn:
        logger.error("SNS notification skipped: no topic_arn in config, event, or SNS_TOPIC_ARN env var")
        return
    sns = boto3.client("sns")
    sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=report_text)
    logger.info("SNS notification sent to %s", topic_arn)


def _notify_ses(report_html, subject, notif_config, runtime_overrides=None, **_kwargs):
    """Send the HTML report as an email via SES.

    Address resolution: notif fields > event overrides > SES_FROM_EMAIL/SES_TO_EMAILS env vars.
    """
    import boto3

    overrides = runtime_overrides or {}
    from_email = (
        notif_config.get("from_email")
        or overrides.get("ses_from_email")
        or os.environ.get("SES_FROM_EMAIL")
    )
    to_emails = notif_config.get("to_emails") or []
    if not to_emails:
        raw_to = overrides.get("ses_to_emails") or os.environ.get("SES_TO_EMAILS")
        if raw_to:
            to_emails = [e.strip() for e in raw_to.split(",")]

    if not from_email or not to_emails:
        logger.error("SES notification skipped: from_email or to_emails not configured")
        return

    ses = boto3.client("ses")
    ses.send_email(
        Source=from_email,
        Destination={"ToAddresses": to_emails},
        Message={
            "Subject": {"Data": subject[:100], "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": report_html, "Charset": "UTF-8"},
            },
        },
    )
    logger.info("SES email sent from %s to %s", from_email, to_emails)


_NOTIFIERS = {
    "console":   _notify_console,
    "html_file": _notify_html_file,
    "sns":       _notify_sns,
    "ses":       _notify_ses,
}


def send_notifications(config, report_text, report_html, subject, runtime_overrides=None):
    """Dispatch the report to every notification channel listed in config.

    *runtime_overrides* carries per-invocation routing values (e.g. SNS topic ARN
    supplied by EventBridge so each project routes to its own topic).
    """
    notifications = config.get("notifications", [{"type": "sns"}])

    for notif in notifications:
        ntype = notif.get("type")
        handler = _NOTIFIERS.get(ntype)
        if handler is None:
            logger.warning("Unknown notification type: %s — skipping", ntype)
            continue
        try:
            handler(
                report_text=report_text,
                report_html=report_html,
                subject=subject,
                notif_config=notif,
                runtime_overrides=runtime_overrides,
            )
        except Exception as exc:
            logger.error("Notification '%s' failed: %s", ntype, exc)
