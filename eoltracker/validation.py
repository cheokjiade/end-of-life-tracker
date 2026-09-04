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
npm_registry         package (version optional for latest-release watch)
manual               label
tyk_lifecycle        version
===================  =========================

(For ``npm_registry`` an omitted ``version`` is valid by design - the
provider reports the registry's latest release when no in-use version is
given.)

CLI: ``python lambda_function.py --validate <config.json>`` exits 0 when no
errors were found (warnings permitted), 1 when the config is invalid, and 2
on usage problems.

Runtime enforcement: every config load (local file or S3) runs
``enforce_valid_config`` - structurally unusable top-level/runtime shapes
(non-object root, bad ``products`` container, invalid thresholds,
``notify_when``, notification channels, or ``maven_repositories``) raise
:class:`ConfigValidationError` before any provider call. Per-product entry
problems never reject the load; they are returned as findings (for warning
logs) and each affected entry later becomes a normalized error row via
``product_entry_errors`` + ``check_product``, so one malformed product cannot
abort an otherwise valid run.
"""

import math

from .core import (
    MAX_CONFIG_DEPTH,
    MAX_CONFIG_FILE_BYTES,
    validate_bounded_json,
)
from .parsers import SOURCE_LABELS
from .parsers.aws_rds import DEFAULT_ENGINE, _AWS_DOCS_URLS
from .parsers.maven_central import _normalize_repository

# Sources whose config entries this module knows how to field-check.
# Entries routing to a registered source without rules here get a warning.
REQUIRED_FIELDS = {
    "endoflife_date": ("product", "version"),
    "aws_rds_scrape": ("version",),
    "aws_sdk_lifecycle": ("sdk", "major"),
    "jackson_lifecycle": ("version",),
    "maven_central": ("group", "artifact", "version"),
    # npm 'version' is optional by design: the provider reports the registry's
    # latest release when no in-use version is supplied.
    "npm_registry": ("package",),
    "manual": (),
    "tyk_lifecycle": ("version",),
}

# Optional identity fields are still strings when present. In particular,
# JSON numeric versions are lossy (3.10 decodes as 3.1) before a provider can
# compare them.
IDENTIFIER_FIELDS = (
    "product", "version", "group", "artifact", "package", "sdk", "major",
)

DEFAULT_SOURCE = "endoflife_date"


class ConfigValidationError(ValueError):
    """A loaded config failed fatal (top-level/runtime-shape) validation.

    Carries the error-severity findings that caused the rejection in
    ``.findings`` so CLI and log surfaces can print the same field-path
    diagnostics ``--validate`` renders.
    """

    def __init__(self, message, findings=None):
        super().__init__(message)
        self.findings = list(findings or [])


def _is_fatal_finding(finding):
    """True for findings that must reject a config before any provider runs.

    Per-product entry findings (paths like ``products[3].version``) are not
    fatal: those entries convert to error rows at dispatch time while the
    remaining products continue. Everything else - the root object, the
    products container itself, thresholds, notify_when, notification
    channels - is runtime-critical.
    """
    return not finding["path"].startswith("products[")

# Engines accepted by aws_rds_scrape (keys of its release-calendar scrape map).
VALID_ENGINES = tuple(sorted(_AWS_DOCS_URLS))

NOTIFY_WHEN_VALUES = ("always", "alerts_only")
NOTIFICATION_TYPES = ("console", "html_file", "sns", "ses")
KNOWN_TOP_LEVEL_KEYS = (
    "products",
    "alert_thresholds_days",
    "notify_when",
    "notifications",
    "maven_repositories",
)


def _finding(path, severity, message):
    return {"path": path, "severity": severity, "message": message}


def _is_scalar(value):
    """A non-empty string identifier (JSON numbers lose version precision)."""
    return isinstance(value, str) and value.strip() != ""


def _check_product_entry(entry, prefix, results):
    """Field-check one products[] entry against its resolved provider."""
    source = entry.get("source", DEFAULT_SOURCE)
    # isinstance guard: an unhashable source (e.g. a JSON list) must produce a
    # finding, not a TypeError from the registry membership test.
    if not isinstance(source, str) or source not in SOURCE_LABELS:
        results.append(_finding(
            f"{prefix}.source", "error",
            f"unknown source {source!r} "
            f"(known sources: {', '.join(sorted(SOURCE_LABELS))})"))
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
                "string"))
    for field in IDENTIFIER_FIELDS:
        if field in required or field not in entry:
            continue
        value = entry.get(field)
        if value is not None and not _is_scalar(value):
            results.append(_finding(
                f"{prefix}.{field}", "error",
                f"optional field '{field}' must be a non-empty string when present"))

    if source == "aws_rds_scrape":
        engine = entry.get("engine", DEFAULT_ENGINE)
        if not _is_scalar(engine) or str(engine) not in VALID_ENGINES:
            results.append(_finding(
                f"{prefix}.engine", "error",
                "engine must be one of: " + ", ".join(VALID_ENGINES)))

    if source == "maven_central" and "repository" in entry:
        repository = entry.get("repository")
        valid = False
        if _is_scalar(repository):
            try:
                valid = _normalize_repository(repository) is not None
            except ValueError:
                valid = False
        if not valid:
            results.append(_finding(
                f"{prefix}.repository", "error",
                "'repository' must be an absolute http(s) URL when provided"))

    for field in ("label", "policy_note", "reference_url"):
        value = entry.get(field)
        if value is not None and not _is_scalar(value):
            results.append(_finding(
                f"{prefix}.{field}", "error",
                f"'{field}' must be a non-empty string when provided"))

    if source == "manual" and not _is_scalar(entry.get("label")):
        results.append(_finding(
            f"{prefix}.label", "error",
            "manual entries require a non-empty string label"))


def product_entry_errors(entry, index=None):
    """Error-severity field checks for one ``products[]`` entry.

    Network-free and exception-free: safe on partial or entirely wrong-typed
    input. Shared by :func:`validate_config`'s sweep and by
    ``check_product``'s pre-provider gate, which converts failing entries
    into normalized error rows without calling the provider. Returns a list
    of findings whose paths are prefixed ``products[<index>].*`` when an
    index is supplied, else ``entry.*``.
    """
    out = []
    if not isinstance(entry, dict):
        kind = type(entry).__name__
        prefix = f"products[{index}]" if index is not None else "entry"
        out.append(_finding(
            prefix, "error",
            f"product entry must be an object, got {kind}"))
        return out
    prefix = f"products[{index}]" if index is not None else "entry"
    _check_product_entry(entry, prefix, out)
    return [f for f in out if f["severity"] == "error"]


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
        checkable_entries = 0
        for i, entry in enumerate(products):
            prefix = f"products[{i}]"
            if not isinstance(entry, dict):
                results.append(_finding(
                    prefix, "error", "product entry must be an object"))
                continue
            if entry.get("_section"):
                continue  # config-file divider, skipped by check_product too
            checkable_entries += 1
            if isinstance(entry.get("label"), str) and entry["label"].strip():
                first = seen_labels.setdefault(entry["label"], i)
                if first != i:
                    results.append(_finding(
                        f"{prefix}.label", "warning",
                        f"duplicate label of products[{first}]"))
            _check_product_entry(entry, prefix, results)
        if checkable_entries == 0:
            results.append(_finding(
                "products", "error",
                "'products' contains no checkable entries (section dividers only)"))

    # -- alert thresholds ---------------------------------------------------
    thresholds = config.get("alert_thresholds_days")
    if "alert_thresholds_days" in config:
        valid = isinstance(thresholds, list) and len(thresholds) > 0 and all(
            isinstance(t, (int, float))
            and not isinstance(t, bool)
            # JSON integers are arbitrary precision. ``math.isfinite`` first
            # coerces them to float and can raise OverflowError, while every
            # integer is finite by definition.
            and (isinstance(t, int) or math.isfinite(t))
            and t > 0
            for t in thresholds
        )
        if not valid:
            results.append(_finding(
                "alert_thresholds_days", "error",
                "'alert_thresholds_days' must be a non-empty list of "
                "positive finite numbers"))

    # -- declared maven repositories ----------------------------------------
    # The runtime stamps this list onto maven_central entries lacking an
    # explicit repository, so a wrong shape is runtime-critical: reject it
    # before any provider runs (same fatality as thresholds/channels).
    if "maven_repositories" in config:
        maven_repositories = config.get("maven_repositories")
        if not (isinstance(maven_repositories, list) and all(
                isinstance(u, str) and u.strip()
                for u in maven_repositories)):
            results.append(_finding(
                "maven_repositories", "error",
                "'maven_repositories' must be a list of non-empty "
                "repository URL strings"))

    # -- notification frequency --------------------------------------------
    notify_when = config.get("notify_when")
    if "notify_when" in config and notify_when not in NOTIFY_WHEN_VALUES:
        results.append(_finding(
            "notify_when", "error",
            "'notify_when' must be one of: " + ", ".join(NOTIFY_WHEN_VALUES)))

    # -- notification channels ---------------------------------------------
    notifications = config.get("notifications")
    if "notifications" in config:
        if not isinstance(notifications, list):
            results.append(_finding(
                "notifications", "error", "'notifications' must be a list"))
        else:
            if not notifications:
                results.append(_finding(
                    "notifications", "warning",
                    "empty list disables all report delivery channels"))
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


def enforce_valid_config(config, origin=""):
    """Validate a loaded config and reject fatal top-level/runtime shapes.

    Runs :func:`validate_config` and raises :class:`ConfigValidationError`
    when any non-product finding has error severity (non-object root; a
    missing, non-list, or empty ``products`` container; invalid
    ``alert_thresholds_days``, ``notify_when``, or notification channels).
    Loaders call this before any provider work so a structurally unusable
    config fails fast instead of aborting mid-run.

    Per-product entry findings are *not* raised: they are returned (for the
    caller to log) and each affected entry becomes a normalized error row at
    dispatch time while valid products continue.

    *origin* labels where the config came from (file path or s3:// URI) in
    the exception message.
    """
    findings = validate_config(config)
    fatal = [f for f in findings if _is_fatal_finding(f) and f["severity"] == "error"]
    if fatal:
        where = f"{origin}: " if origin else ""
        lines = "; ".join(f"{f['path']}: {f['message']}" for f in fatal)
        raise ConfigValidationError(
            f"{where}invalid EOL tracker config ({lines})", fatal)
    return [f for f in findings if not _is_fatal_finding(f)]


def _check_notifications(notifications, results):
    """Channel-level checks shared by validate_config()."""
    for j, notif in enumerate(notifications):
        prefix = f"notifications[{j}]"
        if not isinstance(notif, dict):
            results.append(_finding(
                prefix, "error", "notification channel must be an object"))
            continue

        if "required" in notif and not isinstance(notif["required"], bool):
            results.append(_finding(
                f"{prefix}.required", "error", "'required' must be a boolean"))

        ntype = notif.get("type")
        if not isinstance(ntype, str) or ntype not in NOTIFICATION_TYPES:
            valid = ", ".join(NOTIFICATION_TYPES)
            results.append(_finding(
                f"{prefix}.type", "error",
                f"notification type {ntype!r} is not supported (valid: {valid})"))
            continue

        required = notif.get("required")
        if required is not None and not isinstance(required, bool):
            results.append(_finding(
                f"{prefix}.required", "error",
                "'required' must be a boolean when present"))

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


def config_bounds_error(origin, message):
    """Uniform :class:`ConfigValidationError` for a bounded-JSON rejection."""
    where = f"{origin}: " if origin else ""
    return ConfigValidationError(
        f"{where}invalid EOL tracker config ({message})",
        [_finding("config", "error", message)])


def check_config_bounds(raw, origin=""):
    """Parse raw config bytes under the shared bounded-JSON contract.

    Delegates to :func:`eoltracker.core.validate_bounded_json`, the single
    implementation of that contract: size, nesting depth, UTF-8 decoding
    (a leading BOM is tolerated), JSON syntax, and a top-level object.
    Every loader comes through here -- the runtime file and S3 loaders in
    :mod:`eoltracker.handler` and the ``--validate`` linter -- so the
    deploy gate and the Lambda can never disagree about a config, and the
    same bad bytes always produce the same message.

    *raw* may be truncated at ``MAX_CONFIG_FILE_BYTES + 1`` bytes by the
    caller; anything longer than the limit is rejected on length alone.

    Returns the parsed config object. Raises :class:`ConfigValidationError`
    carrying one ``config`` error finding for every rejection; the finding
    message never embeds *origin*, which callers prefix themselves.
    """
    try:
        return validate_bounded_json(
            raw, MAX_CONFIG_FILE_BYTES, MAX_CONFIG_DEPTH)
    except ValueError as exc:
        raise config_bounds_error(origin, str(exc)) from exc


def load_validated_config_bytes(raw, origin=""):
    """Decode and runtime-validate config bytes with uniform diagnostics.

    :func:`check_config_bounds` enforces the bounded-JSON contract (size,
    depth, UTF-8, JSON syntax, top-level object) and returns the parsed
    config; :func:`enforce_valid_config` then rejects fatal runtime shapes.
    """
    config = check_config_bounds(raw, origin)
    findings = enforce_valid_config(config, origin=origin)
    return config, findings


def validate_config_file(path):
    """Load and validate a config file path; returns the findings list.

    Unreadable/unparsable files produce a single error finding instead of
    raising, so CLI callers get uniform exit-code behaviour. The bounded-
    JSON contract (:func:`check_config_bounds`) is applied first, so
    ``--validate`` rejects exactly what the runtime loaders reject, with
    the same message, rather than passing a config the Lambda would refuse.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CONFIG_FILE_BYTES + 1)
    except OSError as exc:
        return [_finding("config", "error", f"cannot read {path}: {exc}")]
    try:
        config = check_config_bounds(raw, origin=path)
    except ConfigValidationError as exc:
        return list(exc.findings)
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
    if len(argv) != 1 or not argv[0] or argv[0].startswith("-"):
        print("usage: python lambda_function.py --validate <config.json>",
              file=__import__("sys").stderr)
        return 2
    path = argv[0]
    results = validate_config_file(path)
    ok = print_results(results, label=path)
    return 0 if ok else 1
