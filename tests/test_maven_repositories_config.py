"""Network-free maven_repositories tests: validation + load-time stamping.

Covers the top-level ``maven_repositories`` config key end to end, without
network: structural validation (accepted and rejected shapes, fatality) and
the handler's load-time stamping of the declared list onto ``maven_central``
entries that lack an explicit ``repository``/``repositories`` (capped at 8,
config order).
"""

import json
import logging
import os
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.handler import _stamp_maven_repositories, load_config_from_file
from eoltracker.validation import (
    ConfigValidationError,
    enforce_valid_config,
    validate_config,
)


def errors(results):
    return [r for r in results if r["severity"] == "error"]


def warnings(results):
    return [r for r in results if r["severity"] == "warning"]


def by_path(results, path):
    return [r for r in results if r["path"] == path]


DECLARED = ["https://repo.example/one", "https://repo.example/two"]


def maven_entry(**extra):
    entry = {"source": "maven_central", "group": "org.example",
             "artifact": "widget", "version": "1.0.0"}
    entry.update(extra)
    return entry


# --- validation: optional top-level key --------------------------------------

assert not errors(validate_config({"products": [maven_entry()]}))
assert not errors(validate_config({"products": [maven_entry()],
                                   "maven_repositories": DECLARED}))
assert not errors(validate_config({"products": [maven_entry()],
                                   "maven_repositories": []}))
# The key is known now: no 'unrecognized top-level key' typo warning.
res = validate_config({"products": [maven_entry()],
                       "maven_repositories": DECLARED})
assert not [w for w in warnings(res) if w["path"] == "maven_repositories"], res

for bad in ("https://repo.example/one", 42, {"u": 1}, True,
            [""], ["   "], ["ok", 7], [None], [[DECLARED]]):
    res = validate_config({"products": [maven_entry()],
                           "maven_repositories": bad})
    errs = by_path(errors(res), "maven_repositories")
    assert errs, (bad, res)
    assert "maven_repositories" in errs[0]["message"], (bad, res)
    assert [e["path"] for e in errors(res)] == ["maven_repositories"], (bad, res)
    # Runtime-critical shape: the load is rejected before any provider runs.
    try:
        enforce_valid_config({"products": [maven_entry()],
                              "maven_repositories": bad})
        raise AssertionError(f"invalid maven_repositories accepted: {bad!r}")
    except ConfigValidationError as exc:
        assert any(f["path"] == "maven_repositories" for f in exc.findings), \
            exc.findings
print("OK maven_repositories validation: list of non-empty strings, fatal otherwise")


# --- stamping helper (in place, capped, config order) ------------------------

def _config_with(entries, declared=None):
    config = {"products": entries}
    if declared is not None:
        config["maven_repositories"] = declared
    return config


entries = [
    maven_entry(),
    maven_entry(repository="https://shib.example/releases"),
    maven_entry(repositories=["https://mine.example/repo"]),
    {"product": "python", "version": "3.13"},
    {"_section": "=== divider ==="},
]
config = _config_with(entries, DECLARED)
_stamp_maven_repositories(config, "test-origin")
assert entries[0]["repositories"] == DECLARED, entries[0]
assert entries[0]["repositories"] is not DECLARED  # stamped copy, not the config list
assert "repositories" not in entries[1], entries[1]
assert entries[2]["repositories"] == ["https://mine.example/repo"], entries[2]
assert "repositories" not in entries[3], entries[3]
assert "repositories" not in entries[4], entries[4]
print("OK stamping hits plain maven_central entries only; "
      "explicit repository/repositories untouched")

# Only the first 8 URLs are offered.
ten = [f"https://repo.example/{i}" for i in range(10)]
entries = [maven_entry()]
config = _config_with(entries, ten)
_stamp_maven_repositories(config, "cap-origin")
assert entries[0]["repositories"] == ten[:8], entries[0]
print("OK stamping caps the offered list at 8 URLs (config order)")

# No declared list -> nothing stamped.
entries = [maven_entry()]
_stamp_maven_repositories(_config_with(entries), "empty-origin")
assert "repositories" not in entries[0], entries[0]
print("OK no maven_repositories key -> no stamping")


# --- load-path stamping (public loader, same path as S3) ---------------------

@contextmanager
def capture_logs():
    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Capture()
    logging.getLogger().addHandler(handler)
    try:
        yield captured
    finally:
        logging.getLogger().removeHandler(handler)


tmpdir = tempfile.mkdtemp()


def _write_config(name, cfg):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


path = _write_config("stamped.json", _config_with(
    [maven_entry(), maven_entry(repository="https://shib.example/releases")],
    DECLARED))
loaded = load_config_from_file(path)
products = loaded["products"]
assert products[0]["repositories"] == DECLARED, products[0]
assert "repositories" not in products[1], products[1]
print("OK load_config_from_file stamps declared repositories like S3 loading")

path = _write_config("capped.json", _config_with(
    [maven_entry()], [f"https://repo.example/{i}" for i in range(10)]))
with capture_logs() as captured:
    loaded = load_config_from_file(path)
assert loaded["products"][0]["repositories"] == [
    f"https://repo.example/{i}" for i in range(8)], loaded["products"][0]
assert any("offering the first 8 only" in m for m in captured), captured
print("OK overloaded maven_repositories cap at 8 with one warning (count only)")

path = _write_config("unstamped.json", {"products": [maven_entry()]})
loaded = load_config_from_file(path)
assert "repositories" not in loaded["products"][0], loaded["products"][0]
print("OK config without maven_repositories loads unchanged")

path = _write_config("badrepos.json", _config_with(
    [maven_entry()], "https://repo.example/one"))
try:
    load_config_from_file(path)
    raise AssertionError("malformed maven_repositories shape was accepted")
except ConfigValidationError as exc:
    assert any(f["path"] == "maven_repositories" for f in exc.findings), \
        exc.findings
print("OK load rejects a malformed maven_repositories list before providers run")

print("OK test_maven_repositories_config")
