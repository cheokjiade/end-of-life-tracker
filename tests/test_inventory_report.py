"""Tests for the human-readable inventory report writer.

Covers helper_scripts/eol_inventory/report_writer.py and the
generate_inventory_report.py CLI: view normalization for new-model and
legacy configs, `_skipped_npm_packages` synthesis, container
separation, pipe escaping, Markdown headings/tables/summary/checklist,
CSV quoting and ordering, determinism, project slugs, provenance
formatting, and CLI safety (overwrite guard, --csv with and without a
value, missing config). Standalone assertion script: no pytest, no
network, no subprocesses.

Run from the repository root:  python tests/test_inventory_report.py
"""
import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_HELPER_DIR = ROOT / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

from eol_inventory.report_writer import (
    build_inventory_view,
    format_found_in,
    project_slug,
    render_csv,
    render_html,
    render_markdown,
)


def _new_model_config():
    """Synthetic config carrying the new `_inventory` model."""
    return {
        "_comment": ["EOL config for the demo project."],
        "alert_thresholds_days": [30, 60, 90],
        "notifications": [{"type": "console"}],
        "products": [
            {"_section": "=== Java dependencies ==="},
            {
                "source": "maven_central",
                "group": "io.netty", "artifact": "netty-codec-http",
                "version": "4.1.111.Final",
                "label": "Netty Codec HTTP 4.1.111.Final",
                "_comment": ("From pom.xml "
                             "(io.netty:netty-codec-http:4.1.111.Final)"),
                "_found_in": [
                    {"path": "pom.xml", "manifest": "maven", "line": 18,
                     "locator": "dependency:io.netty:netty-codec-http"},
                    {"path": "service/pom.xml", "manifest": "maven",
                     "line": 9},
                ],
            },
            {
                "source": "maven_central",
                "group": "org.apache.commons", "artifact": "commons-lang3",
                "version": "3.14.0",
                "label": "commons-lang3 3.14.0",
                "_comment": ("From service/pom.xml "
                             "(org.apache.commons:commons-lang3:3.14.0)"),
            },
            {
                "product": "spring-security", "version": "6.3",
                "label": "Spring Security 6.3",
                "_comment": [
                    "Auto-derived from Spring Boot 3.3 "
                    "(release train pairing).",
                    "Spring Security version is not explicitly pinned.",
                ],
            },
        ],
        "_inventory": {
            "schema_version": 1,
            "generator_version": "1.0.0",
            "scan_root": "demo",
            "manifests": ["pom.xml", "service/pom.xml"],
            "summary": {"files": 2, "records": 6, "products": 3,
                        "unmapped": 2, "warnings": 1},
            "warnings": [
                {"category": "unresolved_version", "path": "service/pom.xml",
                 "message": "line 12: version ${lib.version} is not "
                            "resolvable"},
            ],
            "unmapped": [
                {"ecosystem": "java", "name": "com.example:inflight",
                 "version": "2.0.0-SNAPSHOT",
                 "reason": "SNAPSHOT build resolves on no public registry",
                 "found_in": [{"path": "pom.xml", "manifest": "maven",
                               "line": 21}]},
                {"ecosystem": "java", "name": "internal.tools:internal-lib",
                 "version_spec": "${internal.rev}",
                 "reason": "internal coordinate prefix resolves on no "
                           "public registry",
                 "found_in": [{"path": "service/pom.xml", "manifest": "maven",
                               "line": 9}]},
            ],
        },
    }


def _container_config():
    """Config whose only product is an image plus one unmapped image."""
    return {
        "products": [
            {"product": "python", "version": "3.12", "label": "Python 3.12",
             "_found_in": [{"path": "Dockerfile", "manifest": "dockerfile",
                            "line": 1, "locator": "FROM python:3.12"}]},
        ],
        "_inventory": {
            "schema_version": 1,
            "generator_version": "1.0.0",
            "scan_root": "containers-demo",
            "manifests": ["Dockerfile"],
            "summary": {"files": 1, "products": 1, "unmapped": 1,
                        "warnings": 0},
            "warnings": [],
            "unmapped": [
                {"ecosystem": "container", "name": "registry.example/app",
                 "version": "1.0",
                 "reason": "no lifecycle mapping for this image",
                 "found_in": [{"path": ".gitlab-ci.yml",
                               "manifest": "gitlab_ci", "line": 4}]},
            ],
        },
    }


def _legacy_config():
    """Pre-`_inventory` config with only the legacy skipped-npm mirror."""
    return {
        "products": [
            {"product": "react", "version": "18", "label": "React 18",
             "_comment": "From package.json (react@18.2.0)"},
            {"product": "nodejs", "version": "18", "label": "Node.js 18"},
        ],
        "_skipped_npm_packages": [
            {"name": "axios", "version": "1.6.8", "source": "package.json"},
            {"name": "left-pad", "version": "1.3.0",
             "source": "package.json"},
        ],
    }


def _section(md, heading):
    """Text under a `## <heading>` up to the next `## ` heading."""
    marker = f"## {heading}\n"
    start = md.index(marker) + len(marker)
    rest = md[start:]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


# ---------------------------------------------------------------------------
# View construction
# ---------------------------------------------------------------------------

def test_build_view_new_model():
    view = build_inventory_view(_new_model_config(), project_name="demo")
    meta = view["meta"]
    assert meta["project"] == "demo"
    assert meta["generator_version"] == "1.0.0"
    assert meta["files_scanned"] == 2
    assert meta["warning_count"] == 1
    assert meta["scan_date"] == date.today().isoformat()
    assert len(view["products"]) == 3 and view["containers"] == []
    netty, commons, security = view["products"]
    assert netty["ecosystem"] == "java"
    assert netty["provider"] == "maven_central"
    assert netty["container"] is False and netty["inferred"] is False
    assert [(loc["path"], loc.get("line")) for loc in netty["provenance"]] == [
        ("pom.xml", 18), ("service/pom.xml", 9)]
    assert commons["provenance"] == []
    assert security["inferred"] is True and security["ecosystem"] == "other"
    assert [u["name"] for u in view["unmapped"]] == [
        "com.example:inflight", "internal.tools:internal-lib"]
    assert view["warnings"][0]["category"] == "unresolved_version"
    assert view["summary"]["by_ecosystem"] == {"java": 2, "other": 1}
    assert view["summary"]["by_provider"] == {
        "endoflife_date": 1, "maven_central": 2}
    assert view["summary"]["by_review_state"] == {
        "tracked": 2, "inferred": 1, "unmapped": 2}
    assert view["summary"]["products_without_provenance"] == 2


def test_view_container_separation():
    view = build_inventory_view(_container_config(), project_name="c")
    assert view["products"] == []
    assert len(view["containers"]) == 1
    image = view["containers"][0]
    assert image["ecosystem"] == "container" and image["container"] is True
    assert image["label"] == "Python 3.12"
    assert [u["name"] for u in view["unmapped"]] == ["registry.example/app"]
    assert view["summary"]["by_ecosystem"] == {"container": 1}
    assert view["summary"]["by_review_state"] == {
        "tracked": 1, "inferred": 0, "unmapped": 1}


def test_view_legacy_config():
    view = build_inventory_view(_legacy_config())
    meta = view["meta"]
    assert meta["generator_version"] == "unknown"
    assert meta["files_scanned"] is None
    assert meta["warning_count"] == 0
    assert meta["project"] == ""
    assert len(view["products"]) == 2
    assert all(p["provenance"] == [] for p in view["products"])
    assert [u["name"] for u in view["unmapped"]] == ["axios", "left-pad"]
    axios = view["unmapped"][0]
    assert axios["ecosystem"] == "node"
    assert axios["version"] == "1.6.8"
    assert axios["reason"] == ("legacy: skipped npm package "
                               "(no mapping at generation time)")
    assert axios["found_in"] == [{"path": "package.json",
                                  "manifest": "npm"}]


def test_skipped_npm_not_duplicated():
    config = _legacy_config()
    config["_inventory"] = {
        "generator_version": "1.0.0",
        "warnings": [],
        "unmapped": [
            {"ecosystem": "node", "name": "axios", "version": "1.6.8",
             "reason": "no endoflife.date mapping for this package",
             "found_in": [{"path": "package.json", "manifest": "npm"}]},
        ],
    }
    view = build_inventory_view(config)
    names = [u["name"] for u in view["unmapped"]]
    assert names == ["axios", "left-pad"]
    assert names.count("axios") == 1
    assert view["unmapped"][0]["reason"] == \
        "no endoflife.date mapping for this package"


def test_format_found_in():
    assert format_found_in([
        {"path": "a/pom.xml", "manifest": "maven", "line": 3}]) == \
        "a/pom.xml:3"
    assert format_found_in([
        {"path": "Dockerfile", "manifest": "dockerfile",
         "locator": "FROM python:3.12"}]) == "Dockerfile (FROM python:3.12)"
    assert format_found_in([
        {"path": "b.json", "manifest": "npm"},
        {"path": "a.json", "manifest": "npm", "line": 2}]) == \
        "b.json; a.json:2"
    assert format_found_in([]) == ""
    assert format_found_in(None) == ""


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def test_markdown_new_model():
    md = render_markdown(
        build_inventory_view(_new_model_config(), project_name="demo"))
    assert md.startswith("# Dependency inventory: demo\n")
    assert f"- Scan date: {date.today().isoformat()}" in md
    assert "- Generator version: 1.0.0" in md
    assert "- Files scanned: 2" in md
    assert "- Warnings: 1" in md
    assert "## Tracked products" in md
    assert "### java / maven\\_central" in md
    assert "pom.xml:18; service/pom.xml:9" in md
    assert ("| commons-lang3 3.14.0 | 3.14.0 | maven\\_central "
            "| not recorded |  |  |") in md
    assert ("| Spring Security 6.3 | 6.3 | endoflife\\_date "
            "| not recorded |  | yes |") in md
    assert md.count("| yes |") == 1
    unmapped = _section(md, "Unmapped and unresolved dependencies")
    assert ("| java | com.example:inflight | 2.0.0-SNAPSHOT | SNAPSHOT build "
            "resolves on no public registry | pom.xml:21 |") in unmapped
    assert ("| java | internal.tools:internal-lib | ${internal.rev} | "
            "internal coordinate prefix resolves on no public registry "
            "| service/pom.xml:9 |") in unmapped
    warnings = _section(md, "Warnings")
    assert ("- [unresolved\\_version] service/pom.xml: line 12: version "
            "${lib.version} is not resolvable") in warnings
    summary = _section(md, "Summary")
    assert "| java | 2 |" in summary
    assert "| other | 1 |" in summary
    assert "| Total | 3 |" in summary
    assert "| inferred | 1 |" in summary
    assert "| tracked | 2 |" in summary
    assert "| unmapped | 2 |" in summary
    assert "| Total | 5 |" in summary
    checklist = _section(md, "Manual review checklist")
    assert "- [ ] Review the 2 unmapped dependencies listed above." \
        in checklist
    assert "- [ ] Resolve the 1 scan warnings." in checklist
    assert "- [ ] Confirm the 1 inferred tracker entries are wanted." \
        in checklist
    assert "- [ ] Add provenance for 2 products recorded without it." \
        in checklist
    assert "- [ ] Spot-check versions derived from properties or ranges." \
        in checklist
    assert "- [ ] Add manual entries for products no provider can monitor." \
        in checklist
    assert all(ord(char) < 128 for char in md)


def test_markdown_container_separation():
    md = render_markdown(
        build_inventory_view(_container_config(), project_name="c"))
    assert "## Container images" in md
    images = _section(md, "Container images")
    assert "### Tracked images" in images
    assert ("| Python 3.12 | 3.12 | endoflife\\_date | Dockerfile:1 |  |"
            in images)
    assert "### Unmapped images" in images
    assert ("| registry.example/app | 1.0 | no lifecycle mapping for this "
            "image | .gitlab-ci.yml:4 |  |") in images
    tracked = _section(md, "Tracked products")
    assert "None." in tracked
    assert "Python 3.12" not in tracked
    general = _section(md, "Unmapped and unresolved dependencies")
    assert general.strip() == "None."


def test_markdown_pipe_escaping():
    config = _new_model_config()
    config["products"][1]["label"] = "Weird | Name"
    md = render_markdown(build_inventory_view(config, project_name="demo"))
    assert "Weird \\| Name" in md
    assert "| Weird | Name |" not in md


def test_markdown_escapes_newlines_html_and_warning_markup():
    config = _new_model_config()
    config["products"][1]["label"] = "<b>name</b>\n| injected |"
    config["_inventory"]["warnings"][0]["message"] = \
        "first\n- [click](javascript:alert(1)) <script>"
    md = render_markdown(build_inventory_view(config, project_name="<demo>"))
    assert "<script>" not in md and "<b>name</b>" not in md
    assert "&lt;demo&gt;" in md and "&lt;b&gt;name&lt;/b&gt;" in md
    assert "\n- [click]" not in md


def test_markdown_neutralizes_remote_images_and_inline_markup():
    config = _new_model_config()
    config["products"][1]["label"] = (
        "![probe](https://example.invalid/pixel) *x* _y_ ~~z~~ "
        "https://bare.invalid www.bare.invalid contact@bare.invalid "
        "line\u2028paragraph\u2029end")
    md = render_markdown(build_inventory_view(config, project_name="demo"))
    assert "![probe](https://example.invalid/pixel)" not in md
    assert "*x*" not in md
    assert "_y_" not in md and "~~z~~" not in md
    assert "https://bare.invalid" not in md
    assert "www.bare.invalid" not in md
    assert "contact@bare.invalid" not in md
    assert "\u2028" not in md and "\u2029" not in md
    assert ("\\!\\[probe\\](https&#58;//example.invalid/pixel) \\*x\\* "
            "\\_y\\_ \\~\\~z\\~\\~ https&#58;//bare.invalid "
            "www&#46;bare.invalid contact&#64;bare.invalid "
            "line paragraph end") in md


def test_markdown_legacy_config():
    md = render_markdown(build_inventory_view(_legacy_config()))
    assert md.startswith("# Dependency inventory\n")
    assert "- Generator version: unknown" in md
    assert "- Files scanned: not recorded" in md
    assert "- Warnings: 0" in md
    assert "### other / endoflife\\_date" in md
    assert "| React 18 | 18 | endoflife\\_date | not recorded |  |  |" in md
    unmapped = _section(md, "Unmapped and unresolved dependencies")
    assert ("| node | axios | 1.6.8 | legacy: skipped npm package (no "
            "mapping at generation time) | package.json |") in unmapped
    assert ("| node | left-pad | 1.3.0 | legacy: skipped npm package (no "
            "mapping at generation time) | package.json |") in unmapped
    assert "- [ ] Review the 2 unmapped dependencies listed above." in md


def test_markdown_deterministic():
    md1 = render_markdown(
        build_inventory_view(_new_model_config(), project_name="demo"))
    md2 = render_markdown(
        build_inventory_view(_new_model_config(), project_name="demo"))
    assert md1 == md2


# ---------------------------------------------------------------------------
# CSV rendering
# ---------------------------------------------------------------------------

def test_csv_output():
    view = build_inventory_view(_new_model_config(), project_name="demo")
    text = render_csv(view)
    lines = text.splitlines()
    assert lines[0] == ("kind,ecosystem,provider,name,version,review_state,"
                        "inferred,found_in,details")
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 1 + len(view["products"]) + len(view["unmapped"])
    products = [r for r in rows[1:] if r[0] == "product"]
    unmapped = [r for r in rows[1:] if r[0] == "unmapped"]
    assert len(products) == len(view["products"])
    assert len(unmapped) == len(view["unmapped"])
    assert all(r[2] == "" and r[5] == "unmapped" for r in unmapped)
    assert products[0] == [
        "product", "java", "maven_central", "Netty Codec HTTP 4.1.111.Final",
        "4.1.111.Final", "tracked", "", "pom.xml:18; service/pom.xml:9", ""]
    assert render_csv(view) == render_csv(view)


def test_csv_quotes_special_values():
    config = _new_model_config()
    config["products"][1]["label"] = 'Netty "HTTP", full'
    view = build_inventory_view(config, project_name="demo")
    rows = list(csv.reader(io.StringIO(render_csv(view))))
    assert rows[1][3] == 'Netty "HTTP", full'


def test_csv_neutralizes_spreadsheet_formulas():
    config = _new_model_config()
    config["products"][1]["label"] = '=HYPERLINK("https://evil", "open")'
    config["products"][1]["version"] = "+1"
    rows = list(csv.reader(io.StringIO(render_csv(
        build_inventory_view(config, project_name="demo")))))
    product = rows[1]
    assert product[3].startswith("'=")
    assert product[4] == "'+1"


def test_curated_manual_entry_with_untracked_comment_is_not_hidden():
    config = _new_model_config()
    config["products"].append({
        "source": "manual", "label": "Curated appliance", "version": "7",
        "_comment": "Owner says this remains untracked until migration"})
    view = build_inventory_view(config)
    assert any(row["label"] == "Curated appliance" for row in view["products"])

    config["products"][-1]["_inventory_generated"] = "unmapped"
    view = build_inventory_view(config)
    assert not any(row["label"] == "Curated appliance" for row in view["products"])


def test_html_output_is_escaped_and_has_review_details():
    config = _new_model_config()
    config["products"][1]["label"] = "Netty <unsafe>"
    rendered = render_html(build_inventory_view(config, project_name="demo"))
    assert rendered.startswith("<!doctype html>")
    assert "Netty &lt;unsafe&gt;" in rendered
    assert "Netty <unsafe>" not in rendered
    assert "SNAPSHOT build resolves on no public registry" in rendered
    assert "Manual review checklist" in rendered
    assert "<td>inferred</td>" in rendered


# ---------------------------------------------------------------------------
# Project slugs
# ---------------------------------------------------------------------------

def test_project_slug():
    assert project_slug("eol_config.my-proj.json") == "my-proj"
    assert project_slug("custom.json") == "custom"
    assert project_slug("some/dir/eol_config.my-proj.json") == "my-proj"
    assert project_slug("C:\\x\\y\\eol_config.my-proj.json") == "my-proj"
    assert project_slug("eol_config.json") == "eol_config"


# ---------------------------------------------------------------------------
# CLI safety: overwrite guard, --csv forms, missing config
# ---------------------------------------------------------------------------

_CLI_PATH = ROOT / "helper_scripts" / "generate_inventory_report.py"


def _load_cli():
    """Load the hyphen-named CLI by file path under a private module name."""
    spec = importlib.util.spec_from_file_location(
        "_eol_generate_inventory_report_cli", str(_CLI_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cli = _load_cli()


def test_cli_render_refuse_and_force():
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "eol_config.demo.json"
        cfg.write_text(json.dumps(_new_model_config()), encoding="utf-8")
        out = Path(td) / "report.md"
        base = [str(cfg), "--output", str(out), "--no-csv", "--no-html"]
        assert _cli.main(base) == 0
        first = out.read_text(encoding="utf-8")
        assert first.startswith("# Dependency inventory: demo")
        assert _cli.main(base) == 2
        assert out.read_text(encoding="utf-8") == first
        assert _cli.main(base + ["--force"]) == 0
        assert out.read_text(encoding="utf-8") == first
        assert list(Path(td).glob(".inventory_report-*")) == []


def test_cli_csv_with_and_without_value():
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "eol_config.demo.json"
        cfg.write_text(json.dumps(_new_model_config()), encoding="utf-8")
        old_cwd = os.getcwd()
        os.chdir(td)
        try:
            assert _cli.main([str(cfg), "--csv"]) == 0
            md = Path("reports") / "inventory" / "demo-inventory.md"
            csv_path = Path("reports") / "inventory" / "demo-inventory.csv"
            assert md.is_file() and csv_path.is_file()
            assert (Path("reports") / "inventory" /
                    "demo-inventory.html").is_file()
            assert md.read_text(encoding="utf-8").startswith(
                "# Dependency inventory: demo")
            assert csv_path.read_text(encoding="utf-8").startswith(
                "kind,ecosystem,provider,name,version,review_state,"
                "inferred,found_in,details")
        finally:
            os.chdir(old_cwd)
        with tempfile.TemporaryDirectory() as td2:
            explicit = Path(td2) / "rows.csv"
            rc = _cli.main([str(cfg), "--output", str(Path(td2) / "r.md"),
                            "--csv", str(explicit), "--no-html", "--force"])
            assert rc == 0 and explicit.is_file()
            header = list(csv.reader(
                io.StringIO(explicit.read_text(encoding="utf-8"))))[0]
            assert header[0] == "kind"


def test_cli_missing_config():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.md"
        rc = _cli.main([str(Path(td) / "missing.json"),
                        "--output", str(out)])
        assert rc == 2
        assert not out.exists()


TESTS = [
    test_build_view_new_model,
    test_view_container_separation,
    test_view_legacy_config,
    test_skipped_npm_not_duplicated,
    test_format_found_in,
    test_markdown_new_model,
    test_markdown_container_separation,
    test_markdown_pipe_escaping,
    test_markdown_escapes_newlines_html_and_warning_markup,
    test_markdown_neutralizes_remote_images_and_inline_markup,
    test_markdown_legacy_config,
    test_markdown_deterministic,
    test_csv_output,
    test_csv_quotes_special_values,
    test_csv_neutralizes_spreadsheet_formulas,
    test_curated_manual_entry_with_untracked_comment_is_not_hidden,
    test_html_output_is_escaped_and_has_review_details,
    test_project_slug,
    test_cli_render_refuse_and_force,
    test_cli_csv_with_and_without_value,
    test_cli_missing_config,
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
    print("OK test_inventory_report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
