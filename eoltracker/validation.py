"""Network-free structural validation of EOL tracker config files.

Checks a config dict against the schema the providers and formatters actually
consume, using only field access patterns that exist in ``eoltracker`` today.
No network calls are made anywhere in this module: it reads JSON structure,
not live data. Use it before uploading a config to S3 (or applying Terraform,
which uploads configs) so malformed entries are rejected at the workstation
instead of aborting a scheduled Lambda run mid-flight.

Findings are returned as dicts with ``path`` (dotted JSON-pointer style),
``severity`` (``error`` | ``warning``), and ``message``. An *error* means the
run would misbehave if executed as-is; a *warning* means it would run but may
not do what was intended.

Required fields per source mirror each parser's direct indexing:

===================  =========================
source               required fields
===================  =========================
endoflife_date       product, version
aws_rds_scrape       version
aws_sdk_lifecycle    sdk, major
jackson_lifecycle    version
maven_central        group, artifact, version
npm_registry         package, version
manual               (none)
tyk_lifecycle        version
===================  =========================

CLI: ``python lambda_function.py --validate <config.json>`` exits 0 when no
errors were found (warnings permitted), 1 when the config is invalid, and 2
on usage problems.
"""

import json

from .parsers import SOURCE_LABELS

# Sources whose config entries this module knows how to field-check.
# Entries routing to a registered source without rules here get a warning.
REQUIRED_FIELDS = {
    "endoflife_date": ("product", "version"),
    "aws_rds_scrape": ("version",),
    "aws_sdk_lifecycle": ("sdk", "major"),
    "jackson_lifecycle": ("version",),
    "maven_central": ("group", "artifact", "version"),
    "npm_registry": ("package", "version"),
    "manual": (),
    "tyk_lifecycle": ("version",),
}

DEFAULT_SOURCE = "endoflife_date"

# Engines accepted by aws_rds_scrape (keys of its release-calendar scrape map).
VALID_ENGINES = ("aurora-postgresql", "rds-postgresql")

NOTIFY_WHEN_VALUES = ("always", "alerts_only")
NOTIFICATION_TYPES = ("console", "html_file", "sns", "ses")
KNOWN_TOP_LEVEL_KEYS = (
    "products",
    "alert_thresholds_days",
    "notify_when",
    "notifications",
)


def _finding(path, severity, message):
    return {"path": path, "severity": severity, "message": message}


def _is_scalar(value):
    """A filled string/number (bools excluded even though bools are ints)."""
    return (
        isinstance(value, (str, int, float))
        and not isinstance(value, bool)
        and str(value).strip() != ""
    )


def _check_product_entry(entry, prefix, results):
    """Field-check one products[] entry against its resolved provider."""
    source = entry.get("source", DEFAULT_SOURCE)
    if source not in SOURCE_LABELS:
        known = ", ".join(sorted(SOURCE_LABELS))
        results.append(_finding(
            f"{prefix}.source", "error",
            f"unknown source '{source}' (known sources: {known})"))
        return

    required = REQUIRED_FIELDS.get(source)
    if required is None:
        results.append(_finding(
            f"{prefix}.source", "warning",
            f"no field rules defined for source '{source}' "
            "- entry could not be fully validated"))
        return

    for field in required:
        if not _is_scalar(entry.get(field)):
            results.append(_finding(
                f"{prefix}.{field}", "error",
                f"required field '{field}' is missing, empty, or not a "
                "string/number"))

    if source == "aws_rds_scrape":
        engine = entry.get("engine", VALID_ENGINES[0])
        if not _is_scalar(engine) or str(engine) not in VALID_ENGINES:
            results.append(_finding(
                f"{prefix}.engine", "error",
                "engine must be one of: " + ", ".join(VALID_ENGINES)))

    label = entry.get("label")
    if label is not None and not isinstance(label, str):
        results.append(_finding(
            f"{prefix}.label", "warning", "label should be a string"))


def validate_config(config):
    """Return sorted findings (list of {path, severity, message}) for *config*."""
    results = []

    if not isinstance(config, dict):
        results.append(_finding(
            "config", "error", "top-level JSON value must be an object"))
        return results

    # -- products -----------------------------------------------------------
    products = config.get("products")
    if products is None:
        results.append(_finding(
            "products", "error", "config has no 'products' list"))
    elif not isinstance(products, list):
        results.append(_finding(
            "products", "error", "'products' must be a list"))
    elif not products:
        results.append(_finding(
            "products", "error", "'products' list is empty"))
    else:
        seen_labels = {}
        for i, entry in enumerate(products):
            prefix = f"products[{i}]"
            if not isinstance(entry, dict):
                results.append(_finding(
                    prefix, "error", "product entry must be an object"))
                continue
            if entry.get("_section"):
                continue  # config-file divider, skipped by check_product too
            if isinstance(entry.get("label"), str) and entry["label"].strip():
                first = seen_labels.setdefault(entry["label"], i)
                if first != i:
                    results.append(_finding(
                        f"{prefix}.label", "warning",
                        f"duplicate label of products[{first}]"))
            _check_product_entry(entry, prefix, results)

    # -- alert thresholds ---------------------------------------------------
    thresholds = config.get("alert_thresholds_days")
    if thresholds is not None:
        valid = isinstance(thresholds, list) and len(thresholds) > 0 and all(
            isinstance(t, (int, float))
            and not isinstance(t, bool)
            and t > 0
            for t in thresholds
        )
        if not valid:
            results.append(_finding(
                "alert_thresholds_days", "error",
                "'alert_thresholds_days' must be a non-empty list of "
                "positive numbers"))

    # -- notification frequency --------------------------------------------
    notify_when = config.get("notify_when")
    if notify_when is not None and notify_when not in NOTIFY_WHEN_VALUES:
        results.append(_finding(
            "notify_when", "error",
            "'notify_when' must be one of: " + ", ".join(NOTIFY_WHEN_VALUES)))

    # -- notification channels ---------------------------------------------
    notifications = config.get("notifications")
    if notifications is not None:
        if not isinstance(notifications, list):
            results.append(_finding(
                "notifications", "error", "'notifications' must be a list"))
        else:
            _check_notifications(notifications, results)

    # -- stray keys ---------------------------------------------------------
    for key in config:
        if key.startswith("_"):
            continue
        if key not in KNOWN_TOP_LEVEL_KEYS:
            results.append(_finding(
                key, "warning", "unrecognized top-level key "
                "(ignored at runtime - possible typo?)"))

    results.sort(key=lambda r: (r["path"], 0 if r["severity"] == "error" else 1))
    return results


def _check_notifications(notifications, results):
    """Channel-level checks shared by validate_config()."""
    for j, notif in enumerate(notifications):
        prefix = f"notifications[{j}]"
        if not isinstance(notif, dict):
            results.append(_finding(
                prefix, "error", "notification channel must be an object"))
            continue

        ntype = notif.get("type")
        if not isinstance(ntype, str) or ntype not in NOTIFICATION_TYPES:
            valid = ", ".join(NOTIFICATION_TYPES)
            results.append(_finding(
                f"{prefix}.type", "error",
                f"notification type {ntype!r} is not supported (valid: {valid})"))
            continue

        if ntype == "html_file":
            path = notif.get("path")
            if path is not None and (not isinstance(path, str) or not path.strip()):
                results.append(_finding(
                    f"{prefix}.path", "error", "'path' must be a filename"))

        elif ntype == "sns":
            topic_arn = notif.get("topic_arn")
            if topic_arn is not None and not isinstance(topic_arn, str):
                results.append(_finding(
                    f"{prefix}.topic_arn", "error", "'topic_arn' must be a string"))
            elif not topic_arn:
                results.append(_finding(
                    f"{prefix}.topic_arn", "warning",
                    "no topic_arn - falls back to SNS_TOPIC_ARN env var or "
                    "the EventBridge invocation payload at runtime"))

        elif ntype == "ses":
            from_email = notif.get("from_email")
            to_emails = notif.get("to_emails")
            if from_email is not None and not isinstance(from_email, str):
                results.append(_finding(
                    f"{prefix}.from_email", "error", "'from_email' must be a string"))
            if to_emails is not None and not (
                isinstance(to_emails, list)
                and all(isinstance(e, str) and e.strip() for e in to_emails)
            ):
                results.append(_finding(
                    f"{prefix}.to_emails", "error",
                    "'to_emails' must be a list of recipient strings"))
            if not from_email or not to_emails:
                results.append(_finding(
                    prefix, "warning",
                    "incomplete SES channel - missing values fall back to "
                    "SES_FROM_EMAIL/SES_TO_EMAILS env vars or invocation "
                    "overrides at runtime"))


def load_config_json_bytes(raw):
    """Parse raw bytes into a config dict, reporting decode/json failures.

    Decoding intentionally requires ASCII so non-ASCII configs are flagged
    locally instead of failing later inside load_config_from_file's plain
    open() (e.g. under cp1252 Windows locales - keep configs ASCII).
    """
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "config file is not ASCII-only "
            f"({exc})") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON ({exc.msg}, line {exc.lineno} column "
                         f"{exc.colno})") from exc


def validate_config_file(path):
    """Load and validate a config file path; returns the findings list.

    Unreadable/unparsable files produce a single error finding instead of
    raising, so CLI callers get uniform exit-code behaviour.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        return [_finding("config", "error", f"cannot read {path}: {exc}")]
    try:
        config = load_config_json_bytes(raw)
    except ValueError as exc:
        return [_finding("config", "error", f"{path}: {exc}")]
    return validate_config(config)


def print_results(results, label=""):
    """Render findings to stdout; returns True when any error is present."""
    prefix = f"{label}: " if label else ""
    n_errors = sum(1 for r in results if r["severity"] == "error")
    for r in results:
        tag = r["severity"].upper()
        print(f"{tag:7} {r['path']}: {r['message']}")
    n_warnings = len(results) - n_errors
    verdict = "VALID" if n_errors == 0 else "INVALID"
    print(f"{prefix}{verdict}: {n_errors} error(s), {n_warnings} warning(s)")
    return n_errors == 0


def main(argv=None):
    """CLI body behind ``python lambda_function.py --validate <path>``.

    Returns an exit code: 0 valid, 1 invalid, 2 usage error.
    """
    argv = list(argv if argv is not None else [])
    if len(argv) != 1 or argv[0].startswith("-"):
        print("usage: python lambda_function.py --validate <config.json>",
              file=__import__("sys").stderr)
        return 2
    path = argv[0]
    results = validate_config_file(path)
    ok = print_results(results, label=path)
    return 0 if ok else 1
