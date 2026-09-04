"""Input-safety tests for the helper CLIs (config depth, size, atomicity).

Covers the malformed/unbounded-input audit finding: `load_bounded_config`
shared by both helper CLIs rejects over-deep, oversize, or invalid config
files with a clean error (exit 2 at the CLI) before any output file is
written, and `build_inventory_view` absorbs malformed `_inventory`
structures as structured `malformed_config` warnings instead of raising
TypeError/AttributeError. Standalone assertion script: no pytest, no
network, no subprocesses.

Run from the repository root:  python tests/test_cli_input_safety.py
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]

_HELPER_DIR = ROOT / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory.config_io as config_io
from eol_inventory.config_io import ConfigLoadError, load_bounded_config
from eol_inventory.models import MAX_FILE_BYTES
from eol_inventory.report_writer import (
    build_inventory_view,
    render_csv,
    render_html,
    render_markdown,
)
from generate_config import main as generate_config_main

_CLI_PATH = ROOT / "helper_scripts" / "generate_inventory_report.py"


def _load_report_cli():
    """Load the hyphen-named CLI by file path under a private module name."""
    spec = importlib.util.spec_from_file_location(
        "_eol_input_safety_report_cli", str(_CLI_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_report_cli = _load_report_cli()


def _deep_json(levels):
    """Valid JSON text whose outermost-to-innermost nesting is `levels`."""
    return '{"deep": ' + "[" * (levels - 1) + "]" * (levels - 1) + "}"


def _nested_levels(levels):
    """The Python value `_deep_json(levels)` parses to for its "deep" key."""
    value = []
    for _ in range(levels - 1):
        value = [value]
    return value


def _seed_project(root):
    """A minimal scannable project so the generator CLI reaches the merge.

    The lock resolves react's range to an exact version, so react maps to
    a tracked product instead of an unmapped row.
    """
    (root / "package.json").write_text(
        json.dumps({"name": "app", "dependencies": {"react": "^18.2.0"}}),
        encoding="utf-8")
    (root / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"node_modules/react": {"version": "18.2.0"}},
    }), encoding="utf-8")


def _seed_bulky_project(root, pins):
    """A project whose scan produces `pins` dependency records.

    Every pin becomes its own config record carrying provenance - the
    shape that made a 1.5 MB requirements file (well under
    MAX_FILE_BYTES) serialize to a config far past
    MAX_CONFIG_FILE_BYTES.
    """
    lines = "\n".join(f"pkg-filler-{i:05d}==1.0.{i}" for i in range(pins))
    (root / "requirements.txt").write_text(lines + "\n", encoding="utf-8")


def _captured(err):
    """Context manager: silence stdout, capture stderr into `err`."""
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(err))
    return stack


def _expect_load_error(path, fragment):
    try:
        load_bounded_config(path)
    except ConfigLoadError as exc:
        assert fragment in str(exc), f"{fragment!r} not in {exc!r}"
        assert "\n" not in str(exc), f"message is not one line: {exc!r}"
    else:
        raise AssertionError(f"expected ConfigLoadError for {fragment!r}")


# ---------------------------------------------------------------------------
# Shared bounded loader
# ---------------------------------------------------------------------------

def test_plain_json_load_recurses_on_deep_input():
    # Root cause of the audit finding: the recursive json parser raises
    # RecursionError on deeply nested but valid JSON before any merge
    # logic could run, so the bound must be checked before parsing.
    deep = _deep_json(10_000)
    try:
        json.loads(deep)
    except RecursionError:
        pass
    else:
        raise AssertionError("expected RecursionError from plain json.loads")


def test_config_bounds_are_the_documented_values():
    assert config_io.MAX_CONFIG_DEPTH == 100
    # 20 MB is the ceiling for every config this repo loads *or writes*.
    # A generated config costs ~415 bytes per dependency record, each
    # record carrying its own `_found_in` provenance (measured on the
    # _seed_bulky_project fixture: 2000 pins serialize to 831_041 bytes),
    # so the bound admits roughly 50k records. Past that the generator
    # fails closed (test_generator_refuses_to_write_oversize_generated_
    # config) instead of emitting a config no loader could read back.
    # Matches eoltracker/core.py MAX_CONFIG_FILE_BYTES (runtime/helper
    # parity).
    assert config_io.MAX_CONFIG_FILE_BYTES == 20 * 1024 * 1024


def test_loader_rejects_deep_nesting_with_clear_error():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "deep.json"
        path.write_text(_deep_json(10_000), encoding="utf-8")
        _expect_load_error(path, "nesting depth")


def test_loader_accepts_depth_at_and_just_under_the_limit():
    with tempfile.TemporaryDirectory() as td:
        for levels in (2, 99, config_io.MAX_CONFIG_DEPTH):
            path = Path(td) / f"ok{levels}.json"
            path.write_text(_deep_json(levels), encoding="utf-8")
            config = load_bounded_config(path)
            assert config == {"deep": _nested_levels(levels - 1)}


def test_loader_rejects_depth_just_over_the_limit():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "over.json"
        path.write_text(_deep_json(config_io.MAX_CONFIG_DEPTH + 1),
                        encoding="utf-8")
        _expect_load_error(path,
                           f"exceeds the {config_io.MAX_CONFIG_DEPTH} level")


def test_loader_rejects_oversize_files():
    original = config_io.MAX_CONFIG_FILE_BYTES
    config_io.MAX_CONFIG_FILE_BYTES = 64
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "big.json"
            path.write_text("x" * (config_io.MAX_CONFIG_FILE_BYTES + 1),
                            encoding="utf-8")
            _expect_load_error(path, "byte")
    finally:
        config_io.MAX_CONFIG_FILE_BYTES = original


def test_loader_rejects_invalid_json_non_object_and_unreadable():
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        _expect_load_error(bad, "invalid JSON")
        arr = Path(td) / "arr.json"
        arr.write_text("[1, 2]", encoding="utf-8")
        _expect_load_error(arr, "top-level JSON value is not an object")
        _expect_load_error(Path(td) / "missing.json", "could not read file")


# ---------------------------------------------------------------------------
# Generator CLI (--update): bad existing config never clobbers the output
# ---------------------------------------------------------------------------

def test_generator_update_rejects_deep_config_without_clobber():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_project(root)
        out = root / "out.json"
        out.write_text(_deep_json(10_000), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = generate_config_main(
                [str(root), "--name", "demo", "--output", str(out),
                 "--update"])
        assert rc == 2
        assert "nesting depth" in err.getvalue()
        assert out.read_text(encoding="utf-8") == _deep_json(10_000)
        assert list(root.glob(".eol_config-*")) == []


def test_generator_update_rejects_oversize_config_without_clobber():
    original = config_io.MAX_CONFIG_FILE_BYTES
    config_io.MAX_CONFIG_FILE_BYTES = 64
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_project(root)
            out = root / "out.json"
            out.write_text("x" * 65, encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = generate_config_main(
                    [str(root), "--name", "demo", "--output", str(out),
                     "--update"])
            assert rc == 2
            assert "byte" in err.getvalue()
            assert out.read_text(encoding="utf-8") == "x" * 65
    finally:
        config_io.MAX_CONFIG_FILE_BYTES = original


def test_generator_update_rejects_invalid_json_without_clobber():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_project(root)
        out = root / "out.json"
        out.write_text("{oops", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = generate_config_main(
                [str(root), "--name", "demo", "--output", str(out),
                 "--update"])
        assert rc == 2
        assert "invalid JSON" in err.getvalue()
        assert out.read_text(encoding="utf-8") == "{oops"


def test_generator_update_accepts_depth_just_under_the_limit():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_project(root)
        out = root / "out.json"
        curated = _nested_levels(97)
        existing = {"products": [{"product": "react", "version": "17"}],
                    "curated": curated}
        out.write_text(json.dumps(existing), encoding="utf-8")
        assert load_bounded_config(out)["curated"] == curated
        rc = generate_config_main(
            [str(root), "--name", "demo", "--output", str(out), "--update"])
        assert rc == 0
        merged = json.loads(out.read_text(encoding="utf-8"))
        assert merged["curated"] == curated
        assert any(isinstance(p, dict) and p.get("product") == "react"
                   and p.get("version") == "18" for p in merged["products"])


# ---------------------------------------------------------------------------
# Generator CLI: a generated config never exceeds the shared size limit
# ---------------------------------------------------------------------------

def test_generator_refuses_to_write_oversize_generated_config():
    original = config_io.MAX_CONFIG_FILE_BYTES
    config_io.MAX_CONFIG_FILE_BYTES = 4096
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_bulky_project(root, 200)
            out = root / "out.json"
            err = io.StringIO()
            with _captured(err):
                rc = generate_config_main(
                    [str(root), "--name", "demo", "--output", str(out)])
            assert rc == 2, f"expected exit 2, got {rc}"
            message = err.getvalue()
            assert str(config_io.MAX_CONFIG_FILE_BYTES) in message, message
            counted = re.search(r"for (\d+) dependency record", message)
            assert counted, message
            assert int(counted.group(1)) >= 200, message
            assert not out.exists(), "an oversize config was written"
            assert list(root.glob(".eol_config-*")) == [], "temp file left"
    finally:
        config_io.MAX_CONFIG_FILE_BYTES = original


def test_generator_update_refuses_oversize_merge_without_clobber():
    original = config_io.MAX_CONFIG_FILE_BYTES
    config_io.MAX_CONFIG_FILE_BYTES = 4096
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_bulky_project(root, 200)
            out = root / "out.json"
            existing = json.dumps(
                {"products": [{"product": "react", "version": "17"}]})
            out.write_text(existing, encoding="utf-8")
            err = io.StringIO()
            with _captured(err):
                rc = generate_config_main(
                    [str(root), "--name", "demo", "--output", str(out),
                     "--update"])
            assert rc == 2, f"expected exit 2, got {rc}"
            message = err.getvalue()
            assert str(config_io.MAX_CONFIG_FILE_BYTES) in message, message
            assert out.read_text(encoding="utf-8") == existing, (
                "existing config was clobbered")
            assert list(root.glob(".eol_config-*")) == [], "temp file left"
    finally:
        config_io.MAX_CONFIG_FILE_BYTES = original


def test_generator_writes_config_just_under_the_size_limit():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_bulky_project(root, 50)
        out = root / "out.json"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = generate_config_main(
                [str(root), "--name", "demo", "--output", str(out)])
        assert rc == 0
        written = out.read_bytes()
        assert len(written) <= config_io.MAX_CONFIG_FILE_BYTES
        assert len(json.loads(written.decode("ascii"))["products"]) >= 50


# ---------------------------------------------------------------------------
# Inventory report CLI: bounded read, corrupt input never clobbers output
# ---------------------------------------------------------------------------

def test_report_cli_rejects_deep_config_without_writing_outputs():
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "deep.json"
        cfg.write_text(_deep_json(10_000), encoding="utf-8")
        out = Path(td) / "report.md"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = _report_cli.main([str(cfg), "--output", str(out),
                                   "--no-csv", "--no-html"])
        assert rc == 2
        assert "nesting depth" in err.getvalue()
        assert not out.exists()


def test_report_cli_rejects_oversize_config_without_writing_outputs():
    original = config_io.MAX_CONFIG_FILE_BYTES
    config_io.MAX_CONFIG_FILE_BYTES = 64
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "big.json"
            cfg.write_text("x" * (config_io.MAX_CONFIG_FILE_BYTES + 1),
                           encoding="utf-8")
            out = Path(td) / "report.md"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _report_cli.main([str(cfg), "--output", str(out),
                                       "--no-csv", "--no-html"])
            assert rc == 2
            assert "byte" in err.getvalue()
            assert not out.exists()
    finally:
        config_io.MAX_CONFIG_FILE_BYTES = original


def test_report_cli_corrupt_config_does_not_clobber_output():
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "broken.json"
        cfg.write_text("{oops", encoding="utf-8")
        out = Path(td) / "report.md"
        out.write_text("OLD REPORT", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            # --force passes the overwrite guard so the failure below is
            # the bounded read, not the guard.
            rc = _report_cli.main([str(cfg), "--output", str(out),
                                   "--no-csv", "--no-html", "--force"])
        assert rc == 2
        assert "invalid JSON" in err.getvalue()
        assert out.read_text(encoding="utf-8") == "OLD REPORT"
        assert list(Path(td).glob(".inventory_report-*")) == []


def test_report_cli_renders_malformed_config_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "eol_config.mangled.json"
        cfg.write_text(json.dumps({"products": 5, "_inventory": 7}),
                       encoding="utf-8")
        out = Path(td) / "report.md"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = _report_cli.main([str(cfg), "--output", str(out),
                                   "--no-csv", "--no-html"])
        assert rc == 0
        md = out.read_text(encoding="utf-8")
        assert md.count("None.") >= 2
        # Markdown escaping breaks the category name with a backslash.
        assert "malformed" in md


# ---------------------------------------------------------------------------
# build_inventory_view: junk shapes become empty parts plus warnings
# ---------------------------------------------------------------------------

def _mangled_config():
    return {
        "products": [
            {"label": "ok", "version": "1",
             "_found_in": [{"path": "pom.xml", "manifest": "maven",
                            "line": 3}]},
            {"label": "junk-prov", "_found_in": "pom.xml"},
            {"label": "dict-provider", "source": {"x": 1}},
            "not-a-dict",
        ],
        "_inventory": {
            "manifests": 5,
            "warnings": 5,
            "summary": {"files": 2},
            "unmapped": [
                {"name": {"un": "hashable"}, "ecosystem": None,
                 "found_in": "x"},
                {"name": "good", "ecosystem": "java",
                 "version_spec": 5, "reason": "why"},
                "junk",
            ],
        },
        "_skipped_npm_packages": [
            {"name": ["un"], "source": "package.json"}],
    }


def test_view_absorbs_junk_container_shapes():
    view = build_inventory_view(
        {"products": 5, "_inventory": 7, "_skipped_npm_packages": 3})
    assert view["products"] == [] and view["containers"] == []
    assert view["unmapped"] == []
    assert view["meta"]["warning_count"] == 3
    assert all(w["category"] == "malformed_config"
               for w in view["warnings"])
    paths = {w["path"] for w in view["warnings"]}
    assert paths == {"products", "_inventory", "_skipped_npm_packages"}
    for render in (render_markdown, render_csv, render_html):
        render(view)


def test_view_absorbs_junk_inside_inventory_and_entries():
    view = build_inventory_view(_mangled_config())
    assert [r["label"] for r in view["products"]] == \
        ["ok", "junk-prov", "dict-provider"]
    assert next(r for r in view["products"]
                if r["label"] == "junk-prov")["provenance"] == []
    assert next(r for r in view["products"]
                if r["label"] == "dict-provider")["provider"] == "{'x': 1}"
    assert [(r["ecosystem"], r["name"]) for r in view["unmapped"]] == [
        ("java", "good"), ("node", "['un']"),
        ("other", "{'un': 'hashable'}")]
    assert [r["version_spec"] for r in view["unmapped"]] == [5, None, None]
    categories = [w["category"] for w in view["warnings"]]
    assert categories.count("malformed_config") == 4
    assert view["meta"]["warning_count"] == len(view["warnings"])
    for render in (render_markdown, render_csv, render_html):
        render(view)


def test_view_valid_provenance_still_renders_identically():
    view = build_inventory_view({
        "products": [{"label": "ok", "version": "1", "_found_in": [
            {"path": "b.xml", "manifest": "maven", "line": 2},
            {"path": "a.xml", "manifest": "maven", "line": 1},
        ]}]})
    assert [(loc["path"], loc["line"]) for loc
            in view["products"][0]["provenance"]] == [("a.xml", 1),
                                                      ("b.xml", 2)]
    assert view["warnings"] == []


def test_view_legacy_absent_inventory_stays_warning_free():
    view = build_inventory_view(
        {"products": [{"product": "react", "version": "18"}]})
    assert view["warnings"] == []
    assert view["meta"]["warning_count"] == 0
    assert len(view["products"]) == 1


TESTS = [
    test_plain_json_load_recurses_on_deep_input,
    test_config_bounds_are_the_documented_values,
    test_loader_rejects_deep_nesting_with_clear_error,
    test_loader_accepts_depth_at_and_just_under_the_limit,
    test_loader_rejects_depth_just_over_the_limit,
    test_loader_rejects_oversize_files,
    test_loader_rejects_invalid_json_non_object_and_unreadable,
    test_generator_update_rejects_deep_config_without_clobber,
    test_generator_update_rejects_oversize_config_without_clobber,
    test_generator_update_rejects_invalid_json_without_clobber,
    test_generator_update_accepts_depth_just_under_the_limit,
    test_generator_refuses_to_write_oversize_generated_config,
    test_generator_update_refuses_oversize_merge_without_clobber,
    test_generator_writes_config_just_under_the_size_limit,
    test_report_cli_rejects_deep_config_without_writing_outputs,
    test_report_cli_rejects_oversize_config_without_writing_outputs,
    test_report_cli_corrupt_config_does_not_clobber_output,
    test_report_cli_renders_malformed_config_end_to_end,
    test_view_absorbs_junk_container_shapes,
    test_view_absorbs_junk_inside_inventory_and_entries,
    test_view_valid_provenance_still_renders_identically,
    test_view_legacy_absent_inventory_stays_warning_free,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    if failed:
        print(f"{failed} test(s) failed")
        return 1
    print("OK test_cli_input_safety")
    return 0


if __name__ == "__main__":
    sys.exit(main())
