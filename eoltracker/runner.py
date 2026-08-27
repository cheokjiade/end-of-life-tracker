"""Bounded provider execution with a Lambda reporting-time reserve.

Provider lookups use a small thread pool so independent network waits overlap.
Work is submitted lazily: at most ``max_workers`` checks exist at once, and no
new lookup starts unless enough Lambda time remains for both a provider timeout
guard and the reporting/notification reserve. Results retain config order.

The runner cannot forcibly stop a Python thread blocked in ``urlopen``. When
the reporting reserve is reached, any still-running lookup is detached and an
error-shaped result is emitted for it. Built-in HTTP providers use 10-15 second
socket timeouts; the default 18-second start guard is deliberately larger.

Environment variables:
    EOL_MAX_WORKERS          concurrent provider checks (default: 4)
    EOL_TIME_RESERVE_MS      time kept for rendering/delivery (default: 15000)
    EOL_CHECK_START_GUARD_MS minimum time allowed for a new check (default:
                             18000, in addition to the reserve)
"""

import json
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .core import _error_result, logger
from .parsers import check_product
from .validation import product_entry_errors


DEFAULT_MAX_WORKERS = 4
DEFAULT_TIME_RESERVE_MS = 15000
DEFAULT_CHECK_START_GUARD_MS = 18000
_POLL_SECONDS = 0.05

_NOT_STARTED_MESSAGE = (
    "Skipped: insufficient Lambda time remained to safely start this check"
)
_ABANDONED_MESSAGE = (
    "Incomplete: Lambda reporting reserve was reached while this check was "
    "still running"
)


def _positive_int_env(name, default, minimum=1, maximum=None):
    """Read a bounded positive integer without logging its raw value."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring non-integer %s", name)
        return default
    if value < minimum or (maximum is not None and value > maximum):
        logger.warning("Ignoring out-of-range %s", name)
        return default
    return value


def _remaining_ms(context):
    """Return the Lambda time budget, or ``None`` outside Lambda."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if getter is None:
        return None
    try:
        return max(0, int(getter()))
    except Exception as exc:  # context is supplied by the runtime
        logger.warning(
            "Could not read Lambda remaining time (%s); using unbounded mode",
            type(exc).__name__,
        )
        return None


def _lookup_identity(entry, index):
    """Return a dedupe key only for structurally valid product entries."""
    if not isinstance(entry, dict) or product_entry_errors(entry, index=index):
        return None
    payload = {
        key: value
        for key, value in entry.items()
        if key not in ("label", "policy_note")
    }
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _lookup_entry(entry):
    """Remove report-only fields so deduped results do not inherit a label."""
    if not isinstance(entry, dict):
        return entry
    return {
        key: value
        for key, value in entry.items()
        if key not in ("label", "policy_note")
    }


def _stamp_result(entry, base):
    """Copy a shared lookup result and restore this config row's curation."""
    if base is None:
        return None
    result = dict(base)
    if isinstance(entry, dict):
        label = entry.get("label")
        if isinstance(label, str) and label:
            result["label"] = label
        note = entry.get("policy_note")
        if isinstance(note, str) and note:
            result["policy_note"] = note
        else:
            result.pop("policy_note", None)
    return result


def _incomplete_result(entry, message):
    result = _error_result(entry, message)
    result["incomplete"] = True
    return result


def run_checks(products, today, context=None, *, max_workers=None,
               time_reserve_ms=None, check_start_guard_ms=None):
    """Return ``(ordered_results, metadata)`` for all non-divider entries.

    Provider exceptions and malformed results remain isolated by
    :func:`check_product`. Identical valid lookups run once per invocation;
    labels and policy notes are restored independently for every config row.
    """
    workers = max_workers if max_workers is not None else _positive_int_env(
        "EOL_MAX_WORKERS", DEFAULT_MAX_WORKERS, maximum=32)
    reserve = (
        time_reserve_ms if time_reserve_ms is not None
        else _positive_int_env("EOL_TIME_RESERVE_MS", DEFAULT_TIME_RESERVE_MS)
    )
    start_guard = (
        check_start_guard_ms if check_start_guard_ms is not None
        else _positive_int_env(
            "EOL_CHECK_START_GUARD_MS", DEFAULT_CHECK_START_GUARD_MS)
    )
    workers = max(1, min(int(workers), 32))
    reserve = max(0, int(reserve))
    start_guard = max(0, int(start_guard))

    meta = {
        "scheduled": 0,
        "dedup_hits": 0,
        "unfinished": 0,
        "degraded": False,
    }
    slots = [None] * len(products)

    groups = []
    identities = {}
    for index, entry in enumerate(products):
        if isinstance(entry, dict) and entry.get("_section"):
            continue
        identity = _lookup_identity(entry, index)
        if identity is not None and identity in identities:
            owner = groups[identities[identity]]
            owner["lookup"] = _lookup_entry(owner["lookup"])
            owner["members"].append((index, entry))
            meta["dedup_hits"] += 1
            continue
        group = {
            "lookup": entry,
            "validation_index": index,
            "members": [(index, entry)],
        }
        groups.append(group)
        if identity is not None:
            identities[identity] = len(groups) - 1

    def mark_group(group, message):
        for index, entry in group["members"]:
            slots[index] = _incomplete_result(entry, message)
            meta["unfinished"] += 1

    def settle(group, base):
        for index, entry in group["members"]:
            slots[index] = _stamp_result(entry, base)

    next_group = 0
    futures = {}
    pool = None

    def enough_time_to_start():
        remaining = _remaining_ms(context)
        return remaining is None or remaining > reserve + start_guard

    try:
        if groups:
            pool = ThreadPoolExecutor(max_workers=min(workers, len(groups)))

        def submit_available():
            nonlocal next_group
            while next_group < len(groups) and len(futures) < workers:
                if not enough_time_to_start():
                    break
                group = groups[next_group]
                next_group += 1
                future = pool.submit(
                    check_product,
                    group["lookup"],
                    today,
                    index=group["validation_index"],
                )
                futures[future] = group
                meta["scheduled"] += 1

        submit_available()

        while futures:
            remaining = _remaining_ms(context)
            if remaining is not None and remaining <= reserve:
                break

            timeout = _POLL_SECONDS
            if remaining is not None:
                timeout = min(timeout, max(0, remaining - reserve) / 1000.0)
            done, _pending = wait(
                tuple(futures), timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                group = futures.pop(future)
                settle(group, future.result())
            submit_available()

        for future, group in list(futures.items()):
            if future.done():
                settle(group, future.result())
            else:
                future.cancel()
                mark_group(group, _ABANDONED_MESSAGE)
        futures.clear()

        # Lazy scheduling means these groups never entered an executor queue.
        for group in groups[next_group:]:
            mark_group(group, _NOT_STARTED_MESSAGE)
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    meta["degraded"] = meta["unfinished"] > 0
    ordered = [result for result in slots if result is not None]
    if meta["degraded"]:
        logger.warning(
            "Provider budget produced a partial report (%d result rows, %d "
            "unfinished)", len(ordered), meta["unfinished"])
    return ordered, meta
