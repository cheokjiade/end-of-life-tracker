"""Parser auto-registration and dispatch.

Each module in this package that defines a ``SOURCE`` string and a
``provider`` callable is discovered automatically at import time and wired
into the registries below. Dropping a new ``parsers/<name>.py`` file with
those attributes registers a new data source — no edits here required.

Registration contract (per parser module):
    SOURCE = "<source key>"            # entry["source"] value that routes here
    LABEL  = "<human label>"           # shown in reports (defaults to SOURCE)
    provider = _provider_<name>        # (entry, today) -> result dict
    def url_for(r): ...                # optional; upstream link for a result
"""

import importlib
import pkgutil

from ..core import _error_result, logger

PROVIDERS, SOURCE_LABELS, _URL_FNS = {}, {}, {}
_RESULT_KEYS = frozenset((
    "label", "product", "version", "status", "message",
    "days_remaining", "source",
))
_RESULT_STATUSES = frozenset((
    "eol", "approaching", "ok", "error", "unknown", "untracked",
))
for _finder, _name, _ispkg in pkgutil.iter_modules(__path__):
    _mod = importlib.import_module(f"{__name__}.{_name}")
    src = getattr(_mod, "SOURCE", None)
    if src and hasattr(_mod, "provider"):
        PROVIDERS[src] = _mod.provider
        SOURCE_LABELS[src] = getattr(_mod, "LABEL", src)
        if hasattr(_mod, "url_for"):
            _URL_FNS[src] = _mod.url_for


def source_url_for(result):
    fn = _URL_FNS.get(result.get("source"))
    return fn(result) if fn else None


def _validate_provider_result(result):
    """Reject provider output that cannot satisfy the report contract."""
    if not isinstance(result, dict):
        raise TypeError(
            f"provider returned {type(result).__name__}, expected dict")
    missing = sorted(_RESULT_KEYS.difference(result))
    if missing:
        raise TypeError(f"provider result missing required keys: {missing}")
    for key in ("label", "message", "source"):
        if not isinstance(result[key], str) or not result[key]:
            raise TypeError(f"provider result {key} must be a non-empty string")
    if result["status"] not in _RESULT_STATUSES:
        raise ValueError(f"provider result has unsupported status {result['status']!r}")
    days = result["days_remaining"]
    if days is not None and (not isinstance(days, int) or isinstance(days, bool)):
        raise TypeError("provider result days_remaining must be an integer or null")


def check_product(entry, today, index=None):
    """Dispatch one config entry to its data-source provider, isolated.

    Returns None for non-product entries (those carrying a truthy '_section'
    marker used as visual dividers in the config). Everything else is a
    product and is contained so one bad entry cannot abort the run:

    - a non-dict entry (e.g. a JSON string/number where an object belongs)
      returns an error result without touching any provider;
    - entries failing the structural field checks from :mod:`..validation`
      return an error result *before* the provider is called (no network);
    - unknown sources produce an error-shaped result;
    - unexpected provider exceptions are logged by type only (never exception
      details) and converted into the normalized error shape.
    """
    # Imported lazily: validation imports this package's SOURCE_LABELS at
    # module load, so importing it back here at registration time would cycle.
    from ..validation import product_entry_errors

    if not isinstance(entry, dict):
        kind = type(entry).__name__
        return _error_result(
            entry, f"product entry must be an object, got {kind}")

    if entry.get("_section"):
        return None

    guard = product_entry_errors(entry, index=index)
    if guard:
        detail = "; ".join(f"{f['path']}: {f['message']}" for f in guard)
        result = _error_result(entry, f"invalid product entry - {detail}")
    else:
        source = entry.get("source", "endoflife_date")
        provider = PROVIDERS.get(source)
        if provider is None:
            result = _error_result(
                entry, f"Unknown source '{source}'. Known: {sorted(PROVIDERS)}")
        else:
            try:
                result = provider(entry, today)
            except Exception as exc:  # deliberate per-entry boundary
                logger.error(
                    "%s: unexpected %s while checking source '%s'",
                    entry.get("label", entry.get("product", "?")),
                    type(exc).__name__, source)
                result = _error_result(
                    entry,
                    f"unexpected {type(exc).__name__} while checking source "
                    f"'{source}'")
            try:
                _validate_provider_result(result)
            except (TypeError, ValueError) as exc:
                # A broken provider contract must not leak into the report
                # loop or formatters; normalize it like any other failure.
                logger.error(
                    "provider for source '%s' returned an invalid result: %s",
                    source, exc)
                result = _error_result(
                    entry,
                    f"provider for source '{source}' returned an invalid "
                    f"result ({exc})")
    note = entry.get("policy_note")
    if note and isinstance(result, dict):
        result["policy_note"] = note
    # Provider/config identity fields feed set/dict lookups and sorting in the
    # formatters. Normalize hostile or broken-provider values at the dispatch
    # boundary so a single bad row remains isolated through report delivery.
    if not isinstance(result.get("source"), str):
        result["source"] = "unknown"
    if not isinstance(result.get("label"), str):
        result["label"] = str(result.get("label", "?"))
    return result
