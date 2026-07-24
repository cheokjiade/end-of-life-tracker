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

from ..core import _error_result

PROVIDERS, SOURCE_LABELS, _URL_FNS = {}, {}, {}
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


def check_product(entry, today):
    """Dispatch a config entry to its data-source provider.

    Returns None for non-product entries (those carrying a '_section' marker
    used as visual dividers in the config). Otherwise the provider is
    selected via entry["source"]; defaults to "endoflife_date" when not
    specified. Unknown sources produce an error-shaped result.
    """
    if entry.get("_section"):
        return None
    source = entry.get("source", "endoflife_date")
    provider = PROVIDERS.get(source)
    if provider is None:
        result = _error_result(entry, f"Unknown source '{source}'. Known: {sorted(PROVIDERS)}")
    else:
        result = provider(entry, today)
    note = entry.get("policy_note")
    if note and result is not None:
        result["policy_note"] = note
    return result
