"""Notification channels with structured delivery outcomes.

Dispatches a rendered report to every channel listed in the config:
console, HTML file, SNS, or SES (multiple may be enabled at once). ``boto3``
is imported lazily inside the SNS/SES paths so the stdlib-only channels stay
dependency-free.

Every invocation returns a per-channel outcome record:

    {
      "channel":    <configured type>,
      "required":   True when this channel participates in Lambda failure,
      "attempted":  True if a delivery was actually tried,
      "delivered":  True if the channel confirmed the send/write,
      "skipped":    True if the channel was never attempted
                    (unknown type, or missing routing configuration),
      "error":      None, or an exception summary (recipient-free),
      "detail":     human-readable explanation (recipient-free),
      "output":     optional local artifact path (html_file only),
    }

Failures are isolated: one failed channel never prevents a later independent
channel from being attempted. When *every* configured channel ends up
undelivered, :func:`delivery_failed` is True and the handler raises
:class:`DeliveryFailureError` while running on AWS Lambda so asynchronous
retries, the dead-letter queue, and CloudWatch alarms engage (see README,
"Delivery outcomes and failure handling").

Recipient addresses are deliberately excluded from both log messages and
outcome records. ``html_file`` output is local-only by default: inside AWS
Lambda (detected via ``AWS_LAMBDA_FUNCTION_NAME``) a relative path or any
absolute path outside ``/tmp`` makes the channel skip instead of writing.
"""

import os
import posixpath
from datetime import datetime

from .core import logger

# Environment variable that exists only inside an AWS Lambda execution
# environment (runtime sets it automatically).
LAMBDA_MARKER_ENV = "AWS_LAMBDA_FUNCTION_NAME"

# Only destinations under this absolute directory are writable by html_file
# in Lambda mode (the runtime's writable scratch space). Module constant so
# tests can retarget it without touching the filesystem.
LAMBDA_TMP_ROOT = "/tmp"

_DURABLE_CHANNELS = {"sns", "ses"}


class DeliveryFailureError(RuntimeError):
    """Raised when every required configured delivery path failed.

    Raised only in Lambda mode (from ``lambda_handler``) so that the
    invocation is treated as a failure: Lambda retries asynchronously and,
    after all retries, sends the event to the function's dead-letter queue;
    CloudWatch alarms page the operators.
    """


def running_in_lambda():
    """True when executing inside an AWS Lambda environment."""
    return bool(os.environ.get(LAMBDA_MARKER_ENV))


def _under_tmp(path_str):
    """Lexically decide whether *path_str* resolves under ``LAMBDA_TMP_ROOT``.

    Works purely on string shape (no filesystem access): normalises forward
    slashes, collapses separators and ``..`` segments, then accepts the path
    only when its normalised form equals or starts with the configured root
    followed by a separator (so ``/tmpfoo`` can never qualify). Backslashes
    are treated as ordinary characters converted to separators, which lets a
    test retarget the root without platform surprises.
    """
    norm = posixpath.normpath(path_str.replace("\\\\", "/").replace("\\", "/"))
    return norm == LAMBDA_TMP_ROOT or norm.startswith(LAMBDA_TMP_ROOT + "/")


def _exc_summary(exc):
    """Recipient-free exception class for logs and structured outcomes."""
    return type(exc).__name__


def _project_from_base(base):
    """Derive the project segment from an html_file base name.

    'eol_report_a'    -> 'a'
    'eol_report'      -> 'default'
    anything else     -> the base itself (best effort)
    """
    if base == "eol_report":
        return "default"
    if base.startswith("eol_report_"):
        return base[len("eol_report_"):] or "default"
    return base or "default"


def _html_output_plan(notif_path, in_lambda):
    """Decide where (and whether) the HTML-file channel may write.

    Returns ``(skip_reason, root_dir)``:

    - Local mode (never in Lambda): relative config is honoured and output is
      rooted at ``reports/`` — behaviour is unchanged.
    - Lambda mode: ``html_file`` is local-only by default. It proceeds ONLY
      when the configured path is absolute under ``/tmp``; then output is
      rooted at the path's own directory. Otherwise a skip reason explains
      why nothing was written.

    This function performs no filesystem work; tests exercise it directly.
    """
    if not in_lambda:
        return None, "reports"
    path = notif_path or ""
    if not _under_tmp(path):
        shown = path.strip()
        if not shown:
            shown = "<empty>"
        return (
            "html_file skipped in Lambda mode: '{}' is not an absolute path "
            "under /tmp (the channel is local-only unless an explicit /tmp "
            "destination is configured)".format(shown),
            None,
        )
    base_dir = posixpath.dirname(posixpath.normpath(path.replace("\\", "/")))
    return None, base_dir


def _notify_console(report_text, **_kwargs):
    """Print the plain-text report to stdout."""
    print(report_text)
    return {"status": "delivered", "detail": "plain-text report printed to stdout"}


def _notify_html_file(report_html, notif_config, **_kwargs):
    """Write the HTML report file honouring local-vs-Lambda rules (R-12).

    Local runs (and Lambda runs with an explicit /tmp destination) write
    <root>/<project>/<year>/<month>/<day>/<base>_<timestamp>.<ext>. The
    project is derived from the configured path's base name; the dated folders
    and the filename's timestamp both come from the current local time, so the
    folder path and filename always agree. Each run produces a uniquely named
    file, e.g. reports/a/2026/05/03/eol_report_a_2026-05-03_1430.html.
    """
    path = notif_config.get("path", "eol_report.html")
    skip_reason, root_dir = _html_output_plan(path, running_in_lambda())
    if skip_reason:
        logger.warning(skip_reason)
        return {"status": "skipped", "detail": skip_reason}

    base, ext = os.path.splitext(os.path.basename(path))
    now = datetime.now()
    out_dir = os.path.join(
        root_dir, _project_from_base(base),
        now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"),
    )
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "{}_{:%Y-%m-%d_%H%M}{}".format(base, now, ext))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    logger.info("HTML report written to %s", out_path)
    return {
        "status": "delivered",
        "detail": "HTML report written to %s" % out_path,
        "output": out_path,
    }


def _notify_sns(report_text, subject, notif_config, runtime_overrides=None, **_kwargs):
    """Publish the plain-text report to an SNS topic.

    Topic ARN resolution: notif.topic_arn > event override > SNS_TOPIC_ARN env var.
    Returns a skipped outcome (instead of raising) when no topic is configured:
    routing omissions are configuration problems surfaced as 'skipped'.
    """
    overrides = runtime_overrides or {}
    topic_arn = (
        notif_config.get("topic_arn")
        or overrides.get("sns_topic_arn")
        or os.environ.get("SNS_TOPIC_ARN")
    )
    if not topic_arn:
        msg = "SNS notification skipped: no topic_arn in config, event, or SNS_TOPIC_ARN env var"
        logger.error(msg)
        return {"status": "skipped", "detail": "topic_arn missing (config/event/SNS_TOPIC_ARN)"}
    import boto3

    sns = boto3.client("sns")
    sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=report_text)
    # No topic ARN or subscription endpoint in logs/outcomes (destinations
    # stay out of operational records).
    logger.info("SNS publish succeeded")
    return {"status": "delivered", "detail": "plain-text report published to the configured SNS topic"}


def _notify_ses(report_html, subject, notif_config, runtime_overrides=None, **_kwargs):
    """Send the HTML report as an email via SES.

    Address resolution: notif fields > event overrides > SES_FROM_EMAIL/SES_TO_EMAILS env vars.
    Returns a skipped outcome (instead of raising) when sender/recipients are
    unconfigured. Logs carry a recipient COUNT only — never addresses.
    """
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
        msg = "SES notification skipped: sender/recipients not configured"
        logger.error(msg)
        return {"status": "skipped", "detail": "sender or recipients missing (config/event/env)"}

    import boto3

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
    logger.info("SES email sent to %d recipient(s)", len(to_emails))
    return {"status": "delivered", "detail": "HTML email sent to %d recipient(s)" % len(to_emails)}


_NOTIFIERS = {
    "console":   _notify_console,
    "html_file": _notify_html_file,
    "sns":       _notify_sns,
    "ses":       _notify_ses,
}


def _required_channel(notif):
    """Whether a channel participates in Lambda total-delivery failure.

    SNS and SES are durable by default. Console and local HTML are optional by
    default so a successful log/write cannot mask failure of every durable
    route. Any channel may opt in/out explicitly with a boolean ``required``.
    """
    explicit = notif.get("required")
    if isinstance(explicit, bool):
        return explicit
    return notif.get("type") in _DURABLE_CHANNELS


def _outcome(channel, required, attempted, delivered, skipped, error, detail,
             output=None):
    return {
        "channel": channel,
        "required": required,
        "attempted": attempted,
        "delivered": delivered,
        "skipped": skipped,
        "error": error,
        "detail": detail,
        "output": output,
    }


def send_notifications(config, report_text, report_html, subject, runtime_overrides=None):
    """Dispatch the report to every notification channel listed in config.

    Returns a list of per-channel outcome dicts (module docstring documents
    the shape). Channels are attempted in configuration order; a failed
    channel never prevents later independent channels from being attempted.

    *runtime_overrides* carries per-invocation routing values (e.g. SNS topic ARN
    supplied by EventBridge so each project routes to its own topic).

    """
    notifications = config.get("notifications", [{"type": "sns"}])

    outcomes = []
    for notif in notifications:
        ntype = notif.get("type")
        required = _required_channel(notif)
        handler = _NOTIFIERS.get(ntype)
        if handler is None:
            detail = "unknown notification type '%s'" % ntype
            logger.warning("Unknown notification type: %s — skipping", ntype)
            outcomes.append(_outcome(
                ntype, required, False, False, True, None, detail))
            continue
        try:
            res = handler(
                report_text=report_text,
                report_html=report_html,
                subject=subject,
                notif_config=notif,
                runtime_overrides=runtime_overrides,
            )
        except Exception as exc:
            summary = _exc_summary(exc)
            logger.error("Notification '%s' failed (%s)", ntype, summary)
            outcomes.append(_outcome(
                ntype, required, True, False, False, summary,
                "%s delivery failed" % ntype))
            continue
        status = res.get("status")
        if status == "skipped":
            outcomes.append(_outcome(
                ntype, required, False, False, True, None,
                res.get("detail") or "skipped"))
        elif status == "delivered":
            outcomes.append(_outcome(
                ntype, required, True, True, False, None,
                res.get("detail") or "delivered", res.get("output")))
        else:  # defensive: notifier misbehaved
            outcomes.append(_outcome(
                ntype, required, True, False, False, None,
                "channel reported unclear status"))
            logger.error("Notification '%s' returned unclear status %r", ntype, status)
    return outcomes


def delivery_failed(outcomes):
    """True when required channels exist and none of them delivered."""
    required = [o for o in outcomes if o.get("required")]
    return bool(required) and not any(o["delivered"] for o in required)


def summarize_outcomes(outcomes):
    """Compact multi-line, recipient-free summary of per-channel outcomes."""
    if not outcomes:
        return "(no notification channels were invoked)"
    lines = ["%s: %s%s" % (
        "%s%s" % (o["channel"], " (required)" if o.get("required") else ""),
        "delivered" if o["delivered"] else ("skipped" if o["skipped"] else "failed"),
        (" - %s" % o["detail"]) if o["detail"] else "",
    ) for o in outcomes]
    return "\n".join(lines)
