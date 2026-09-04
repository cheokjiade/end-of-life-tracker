"""Bounded, depth-checked config JSON loading shared by the helper CLIs.

Config files are untrusted input: both the generator (`--update`) and the
inventory report CLI load hand-edited or machine-written JSON whose size
and nesting are unknown. A deeply nested but *valid* document makes the
recursive json parser raise an uncaught RecursionError, and an unbounded
read can exhaust memory. This module gives both CLIs one loader that

    - reads at most MAX_CONFIG_FILE_BYTES bytes with the bounded
      ``read(MAX + 1)`` pattern used for manifests (MAX_FILE_BYTES in
      models.py). Generated configs carry full `_found_in` provenance and
      `_inventory` metadata for up to MAX_FILES manifests, so they can
      legitimately dwarf any single manifest; the bound is a generous
      10x the manifest limit (20 MB);
    - counts {} / [] nesting iteratively (no recursion) and rejects
      documents deeper than MAX_CONFIG_DEPTH *before* json parsing. A
      generated config nests only a handful of levels (top-level object >
      products > entry > _found_in), so 100 leaves orders of magnitude of
      headroom for hand-curated configs while keeping parsing, dumping,
      and merge walking far inside the interpreter's default recursion
      limit;
    - requires valid UTF-8 JSON with a top-level object.

``dump_bounded_config`` applies the same size bound on the way out, so
the generator can never write a config that these loaders (or the
runtime) would refuse to read back.

This is the standalone twin of the runtime's single implementation,
``eoltracker.core.validate_bounded_json`` (reached by every runtime loader
via ``eoltracker.validation.check_config_bounds``); keep the two in step.

Every rejection raises ConfigLoadError with a single-line actionable
message, so a CLI can exit 2 without touching any output file.
Standard-library only; no network.
"""

import json
import re

from .models import MAX_FILE_BYTES

# These values MUST match eoltracker/core.py MAX_CONFIG_FILE_BYTES and
# MAX_CONFIG_DEPTH (the runtime-owned shared bounds). The parity is
# verified by tests/test_provider_safety.py::test_runtime_config_bounds
# and tests/test_cli_input_safety.py::test_config_bounds_are_the_
# documented_values.
MAX_CONFIG_DEPTH = 100
MAX_CONFIG_FILE_BYTES = 20 * 1024 * 1024  # 20 MB

# JSON strings with escapes (linear pattern: the alternatives are disjoint
# on their first character). Strings are stripped before counting so
# braces inside values never count toward the depth.
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_STRUCTURE_RE = re.compile(r"[{}\[\]]")


class ConfigLoadError(Exception):
    """A config file could not be loaded within the safety bounds."""


class ConfigTooLargeError(Exception):
    """A generated config would exceed MAX_CONFIG_FILE_BYTES on disk."""


def _config_record_count(config):
    """Dependency records in a config: tracked products plus unmapped rows."""
    count = 0
    products = config.get("products")
    if isinstance(products, list):
        count += len(products)
    inventory = config.get("_inventory")
    if isinstance(inventory, dict):
        unmapped = inventory.get("unmapped")
        if isinstance(unmapped, list):
            count += len(unmapped)
    return count


def dump_bounded_config(config):
    """Serialize a config to its exact on-disk text, refusing oversize output.

    The write-side twin of load_bounded_config: the generator can turn a
    single in-bounds manifest (MAX_FILE_BYTES, 2 MB) into a config far
    past MAX_CONFIG_FILE_BYTES, because every record carries `_found_in`
    provenance. Checking the serialized text here - before any file is
    opened - means an over-limit config is never written at all, rather
    than written and then rejected by every loader that reads it.

    Returns the ASCII text to write (deterministic indent, trailing
    newline). Raises ConfigTooLargeError, whose single-line message names
    the limit, the actual size, and the record count, when the text would
    exceed MAX_CONFIG_FILE_BYTES. Never truncates: an over-limit config
    is a scan that must be narrowed, not silently shortened.
    """
    text = json.dumps(config, indent=2, ensure_ascii=True) + "\n"
    size = len(text.encode("ascii"))
    if size > MAX_CONFIG_FILE_BYTES:
        records = _config_record_count(config)
        raise ConfigTooLargeError(
            f"generated config is {size} bytes for {records} dependency "
            f"record(s), over the {MAX_CONFIG_FILE_BYTES} byte config "
            "limit; narrow the scan with --exclude or split the project, "
            "then re-run")
    return text


def _max_nesting_depth(text):
    """Deepest {} / [] nesting outside JSON strings (iterative, O(n))."""
    depth = max_depth = 0
    for char in _STRUCTURE_RE.findall(_STRING_RE.sub("", text)):
        if char in "{[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        else:
            depth -= 1
    return max_depth


def load_bounded_config(path):
    """Load a config JSON object with bounded read size and nesting depth.

    Returns the parsed top-level object (a dict). Raises ConfigLoadError
    with a one-line actionable message when the file is missing or
    unreadable, exceeds MAX_CONFIG_FILE_BYTES, nests deeper than
    MAX_CONFIG_DEPTH, is not valid UTF-8 JSON, or its top level is not an
    object. All bound checks run before json parsing, so rejected input
    never reaches the recursive parser.
    """
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_CONFIG_FILE_BYTES + 1)
    except OSError as exc:
        raise ConfigLoadError(f"could not read file: {exc}") from exc
    if len(raw) > MAX_CONFIG_FILE_BYTES:
        raise ConfigLoadError(
            f"file exceeds the {MAX_CONFIG_FILE_BYTES} byte config limit; "
            "trim or split the config")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigLoadError(
            "file is not valid UTF-8; re-save the config as UTF-8") from None
    depth = _max_nesting_depth(text)
    if depth > MAX_CONFIG_DEPTH:
        raise ConfigLoadError(
            f"JSON nesting depth {depth} exceeds the {MAX_CONFIG_DEPTH} "
            "level config limit; flatten or regenerate the config")
    try:
        config = json.loads(text)
    except ValueError as exc:
        raise ConfigLoadError(f"invalid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigLoadError("top-level JSON value is not an object")
    return config
