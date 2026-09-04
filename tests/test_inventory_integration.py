"""End-to-end tests for the project scanner integration.

Scans the mixed fixture project (Node, Python, Go, .NET, Dockerfiles,
GitLab CI) through the real discovery and config-assembly pipeline and
verifies the externally visible results: deterministic files and records,
product sections and provenance merges, unmapped inventory items with
explicit reasons, the `_inventory` summary, and that every generated
tracker entry names a source registered in the Lambda runtime's parser
registry. Standalone assertion script: no pytest, no network (the CLI
runs in-process), no subprocesses.

Run from the repository root:  python tests/test_inventory_integration.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]
_HELPER_DIR = ROOT / "helper_scripts"
sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory as gc
from generate_config import main as generate_config_main
from eoltracker.parsers import PROVIDERS

FIX = ROOT / "tests" / "fixtures" / "inventory_mixed"


def _products(config):
    return [p for p in config["products"] if not p.get("_section")]


def _sections(config):
    return [p["_section"] for p in config["products"] if p.get("_section")]


def _locations(entry):
    return [(loc["path"], loc.get("line"), loc.get("locator"))
            for loc in entry["_found_in"]]


def test_scan_mixed_fixture_discovers_all_ecosystems():
    scan = gc.scan_folder(str(FIX))
    assert scan["root_name"] == "inventory_mixed"
    assert scan["files"] == [
        ".gitlab-ci.yml", ".gitlab/ci/deploy.yml", ".python-version",
        "Directory.Packages.props", "Dockerfile", "Dockerfile.edge",
        "apps/web/package-lock.json", "apps/web/package.json",
        "global.json", "go.mod", "legacy/Legacy.csproj", "mono.csproj",
        "pyproject.toml", "requirements.txt",
    ]
    # package-lock.json resolves sibling versions, so the consumed lock
    # is listed; the fixture has no packages.lock.json sidecar, and a
    # merely-present-but-never-read file must stay unlisted.
    assert "apps/web/package-lock.json" in scan["files"]
    assert not any(f.endswith("packages.lock.json") for f in scan["files"])
    by_eco = {}
    for r in scan["records"]:
        by_eco.setdefault(r["ecosystem"], []).append(r)
    assert sorted((eco, len(recs)) for eco, recs in by_eco.items()) == [
        ("container", 5), ("dotnet", 5), ("go", 4), ("node", 2),
        ("python", 6),
    ]
    # GitLab include-following and direct discovery share one visited-file
    # set, so deploy.yml contributes exactly one record.
    redis = [r for r in by_eco["container"] if r["name"] == "redis"]
    assert len(redis) == 1
    assert all(r["found_in"][0]["path"] == ".gitlab/ci/deploy.yml"
               for r in redis)
    categories = sorted({w["category"] for w in scan["warnings"]})
    assert categories == ["latest_tag", "unresolved_version"]


def test_config_sections_and_products():
    config = gc.generate_config(gc.scan_folder(str(FIX)), "mixed")
    assert _sections(config) == [
        "=== npm dependencies ===",
        "=== Python dependencies ===",
        "=== Go dependencies ===",
        "=== .NET dependencies ===",
        "=== Container images ===",
        "=== Needs Manual Review ===",
    ]
    prods = _products(config)
    # python runtime evidence and the matching Dockerfile image merge into
    # one product whose provenance spans both declaration sites
    py = [p for p in prods if p.get("product") == "python"]
    assert py == [{
        "product": "python", "version": "3.12", "label": "Python 3.12",
        "_comment": "From .python-version (python==3.12.1)",
        "_found_in": [
            {"path": ".python-version", "manifest": "python",
             "locator": "python-version"},
            {"path": "Dockerfile", "manifest": "dockerfile", "line": 1,
             "locator": "FROM python:3.12-slim"},
        ]}]
    # .NET runtime: TFM and global.json SDK pins collapse to one product
    dn = [p for p in prods if p.get("product") == "dotnet"]
    assert len(dn) == 1 and dn[0]["version"] == "8"
    assert [(loc["path"], loc["locator"]) for loc in dn[0]["_found_in"]] == [
        ("global.json", "sdk.version"),
        ("mono.csproj", "TargetFramework"),
    ]
    # central package version: project reference and props declaration
    # merge into one nuget_registry row
    nj = [p for p in prods if p.get("package") == "Newtonsoft.Json"]
    assert nj == [{
        "source": "nuget_registry", "package": "Newtonsoft.Json",
        "version": "13.0.3", "label": "Newtonsoft.Json 13.0.3",
        "_comment": "From mono.csproj (Newtonsoft.Json 13.0.3)",
        "_found_in": [
            {"path": "Directory.Packages.props", "manifest": "dotnet",
             "locator": "PackageVersion:Newtonsoft.Json"},
            {"path": "mono.csproj", "manifest": "dotnet",
             "locator": "PackageReference:Newtonsoft.Json"},
        ]}]
    # registry rows for the remaining exact packages
    by_pkg = {(p.get("package"), p.get("version")): p for p in prods
              if p.get("source") == "pypi_registry"}
    assert set(by_pkg) == {("httpx", "0.27.0"), ("requests", "2.32.3"),
                           ("flask", "3.0.3")}
    assert by_pkg[("requests", "2.32.3")]["_comment"] == (
        "From requirements.txt (requests==2.32.3)")
    npm = [p for p in prods if p.get("source") == "npm_registry"]
    assert [(p["package"], p["version"]) for p in npm] == [
        ("lodash", "4.17.21")]
    go = [p for p in prods if p.get("source") == "go_proxy"]
    assert go == [{
        "source": "go_proxy", "module": "golang.org/x/net",
        "version": "v0.44.0", "label": "golang.org/x/net v0.44.0",
        "_comment": "From go.mod (golang.org/x/net v0.44.0)",
        "_found_in": [{"path": "go.mod", "manifest": "go", "line": 5,
                       "locator": "require:golang.org/x/net"}]}]
    # container images map to lifecycle products; the redis record parsed
    # twice (include + direct discovery) merges into one product with one
    # provenance location
    imgs = {p["product"]: p for p in prods
            if p.get("product") in ("nodejs", "postgresql", "redis")}
    assert imgs["nodejs"]["version"] == "20"
    assert imgs["postgresql"]["version"] == "16"
    assert imgs["postgresql"]["_found_in"] == [{
        "path": ".gitlab-ci.yml", "manifest": "gitlab_ci", "line": 6,
        "locator": "db-test:image"}]
    assert imgs["redis"]["version"] == "7.2"
    assert imgs["redis"]["_found_in"] == [{
        "path": ".gitlab/ci/deploy.yml", "manifest": "gitlab_ci", "line": 1,
        "locator": "image"}]
    # react resolves through the sibling npm lock file
    react = [p for p in prods if p.get("product") == "react"]
    assert react and react[0]["version"] == "18"


def test_config_unmapped_items_and_summary():
    config = gc.generate_config(gc.scan_folder(str(FIX)), "mixed")
    unmapped = config["_inventory"]["unmapped"]
    assert [(u["ecosystem"], u["name"], u["reason"]) for u in unmapped] == [
        ("container", "nginx", "image tag provides no endoflife.date cycle"),
        ("dotnet", "dotnet",
         "no endoflife.date cycle for this target framework"),
        ("python", "numpy", "no exact version (~=1.26.0)"),
        ("python", "python", "no exact version (>=3.11)"),
    ]
    nginx = unmapped[0]
    assert nginx["image_reference"] == "nginx:latest"
    assert nginx["registry"] == "docker.io"
    netfx = unmapped[1]
    assert netfx["version"] == "4.8"
    assert netfx["found_in"] == [{
        "path": "legacy/Legacy.csproj", "manifest": "dotnet",
        "locator": "TargetFramework"}]
    # the latest-tag image remains explicit as an untracked manual row.
    assert not any(p.get("product") == "nginx" for p in _products(config))
    manual = [p for p in _products(config) if p.get("source") == "manual"]
    assert len(manual) == 4
    assert next(p for p in manual if p["label"] == "nginx")["tag"] == "latest"
    assert config["_inventory"]["warnings"] == [
        {"category": "latest_tag", "path": "Dockerfile.edge",
         "message": "line 1: image 'nginx:latest' uses the latest tag"},
        {"category": "unresolved_version", "path": "requirements.txt",
         "message": "numpy has no exact version (~=1.26.0); not guessed"},
    ]
    assert config["_inventory"]["summary"] == {
        "files": 14, "records": 22, "products": 14, "unmapped": 4,
        "warnings": 2, "indirect": 1}
    assert config["_inventory"]["include_transitive"] is False
    assert not config.get("_skipped_npm_packages")


def test_config_is_deterministic():
    config1 = gc.generate_config(gc.scan_folder(str(FIX)), "mixed")
    config2 = gc.generate_config(gc.scan_folder(str(FIX)), "mixed")
    dump1 = json.dumps(config1, indent=2, ensure_ascii=True)
    dump2 = json.dumps(config2, indent=2, ensure_ascii=True)
    assert dump1 == dump2


def test_cli_output_only_names_registered_sources():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.json"
        rc = generate_config_main(
            [str(FIX), "--output", str(out), "--replace"])
        assert rc == 0
        raw = out.read_bytes()
        assert all(b < 128 for b in raw)  # generated configs stay ASCII
        config = json.loads(raw.decode("ascii"))
        for entry in config["products"]:
            if entry.get("_section"):
                continue
            source = entry.get("source", "endoflife_date")
            assert source in PROVIDERS, (source, entry)
            if source == "endoflife_date":
                assert entry.get("product") and entry.get("version"), entry
            elif source != "manual":
                key = "module" if source == "go_proxy" else "package"
                assert entry.get(key) and entry.get("version"), entry
            else:
                assert entry.get("label") and entry.get("_found_in"), entry
        inv = config["_inventory"]
        assert inv["summary"]["products"] == len(
            [p for p in config["products"]
             if not p.get("_section") and p.get("source") != "manual"])
        # the config is directly consumable: the runtime ignores the
        # underscore metadata, so simulate dispatching one registry entry
        from datetime import date
        from eoltracker.parsers import check_product
        result = check_product(
            {"source": "manual", "label": "Shape Check",
             "_found_in": inv["unmapped"][0]["found_in"]},
            date(2026, 8, 28))
        assert result is not None and "_found_in" not in result


TESTS = [
    test_scan_mixed_fixture_discovers_all_ecosystems,
    test_config_sections_and_products,
    test_config_unmapped_items_and_summary,
    test_config_is_deterministic,
    test_cli_output_only_names_registered_sources,
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
    print("OK test_inventory_integration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
