"""Tests for the Go and .NET inventory parsers.

Covers helper_scripts/eol_inventory/parsers/go.py and
helper_scripts/eol_inventory/parsers/dotnet.py: normalized records,
provenance locations, warnings, replace-directive handling (local paths
never become public dependencies), central package versions, lock-file
fallback, case-insensitive resolution, and determinism. Standalone
assertion script: no pytest, no network, no subprocesses.

Run from the repository root:  python tests/test_inventory_go_dotnet.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "inventory_go_dotnet"

_HELPER_DIR = ROOT / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory.parsers.dotnet as dotnet_parser
import eol_inventory.parsers.go as go_parser
from eol_inventory import generate_config, scan_folder


def _parse_go(*parts):
    path = FIX.joinpath(*parts)
    return go_parser.parse_go_mod_records(path, "/".join(parts))


def _parse_csproj(*parts):
    path = FIX.joinpath(*parts)
    return dotnet_parser.parse_csproj_records(path, "/".join(parts))


def _records_named(records, name):
    return [r for r in records if r["name"] == name]


def _one(records, name):
    hits = _records_named(records, name)
    assert len(hits) == 1, f"expected exactly one {name!r} record, got {hits}"
    return hits[0]


def _requires(records):
    return [r for r in records
            if r["kind"] == "dependency"
            and any(loc.get("locator", "").startswith("require:")
                    for loc in r["found_in"])]


def _has_warning(warnings, category, substring):
    return any(w["category"] == category and substring in w["message"]
               for w in warnings)


def _locators(record):
    return [loc.get("locator") for loc in record["found_in"]]


# ---------------------------------------------------------------------------
# Go: helpers
# ---------------------------------------------------------------------------

def test_go_strip_v():
    assert go_parser._strip_v("v1.2.3") == "1.2.3"
    assert go_parser._strip_v("V2.0.0+incompatible") == "2.0.0+incompatible"
    assert go_parser._strip_v("1.2.3") == "1.2.3"
    assert go_parser._strip_v(None) is None


def test_go_is_local_path():
    assert go_parser._is_local_path(".")
    assert go_parser._is_local_path("..")
    assert go_parser._is_local_path("./dep")
    assert go_parser._is_local_path("../dep")
    assert go_parser._is_local_path("/abs/dep")
    assert go_parser._is_local_path("C:\\repos\\dep")
    assert not go_parser._is_local_path("github.com/org/dep")


# ---------------------------------------------------------------------------
# Go: go.mod fixture
# ---------------------------------------------------------------------------

def test_go_module_go_and_toolchain_directives():
    records, warnings = _parse_go("go", "basic", "go.mod")
    assert all(w["category"] in ("go_replace", "go_local_replace")
               for w in warnings)  # replaces warn; directives don't

    module = _one(records, "example.com/app")
    assert module["kind"] == "module"
    assert module["ecosystem"] == "go"
    assert module["version"] is None
    assert module["found_in"][0]["line"] == 1
    assert module["found_in"][0]["locator"] == "module"

    go_versions = sorted(
        r["version"] for r in _records_named(records, "go"))
    assert go_versions == ["1.22", "1.22.5"]
    by_locator = {r["found_in"][0]["locator"]: r["version"]
                  for r in _records_named(records, "go")}
    assert by_locator["go"] == "1.22"
    assert by_locator["toolchain"] == "1.22.5"


def test_go_direct_requires_and_indirect_count():
    records, warnings = _parse_go("go", "basic", "go.mod")
    assert not _has_warning(warnings, "parse_error", "")

    requires = _requires(records)
    direct = [r for r in requires if r["direct"]]
    indirect = [r for r in requires if not r["direct"]]
    assert len(requires) == 4          # 2 block + 1 single + 1 indirect
    assert len(direct) == 3
    assert len(indirect) == 1          # the indirect count is derivable

    errors = _one(requires, "github.com/pkg/errors")
    assert errors["version"] == "0.9.1"   # v-prefix stripped
    assert errors["found_in"][0]["line"] == 8
    assert errors["found_in"][0]["locator"] == "require:github.com/pkg/errors"

    cobra = _one(requires, "github.com/spf13/cobra")
    assert cobra["direct"] is True
    assert cobra["found_in"][0]["line"] == 13

    xmod = _one(requires, "golang.org/x/mod")
    assert xmod["direct"] is False
    assert xmod["version"] == "0.17.0"


def test_go_module_replace_warning_provenance_and_target():
    records, warnings = _parse_go("go", "basic", "go.mod")

    target = _one(records, "github.com/new/dep")
    assert target["version"] == "1.2.3"
    assert target["direct"] is True
    assert _locators(target) == ["require:github.com/old/dep",
                                 "replace:github.com/old/dep"]
    assert not _records_named(records, "github.com/old/dep")

    assert _has_warning(warnings, "go_replace", "github.com/old/dep")


def test_go_replace_before_require_is_order_independent():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "go.mod"
        path.write_text(
            "module example.test/app\n"
            "replace github.com/old/dep => github.com/new/dep v1.2.3\n"
            "require github.com/old/dep v0.1.0\n", encoding="utf-8")
        records, warnings = go_parser.parse_go_mod_records(path, "go.mod")
    target = _one(records, "github.com/new/dep")
    assert target["version"] == "1.2.3" and target["direct"] is True
    assert _locators(target) == ["require:github.com/old/dep",
                                 "replace:github.com/old/dep"]
    assert not _records_named(records, "github.com/old/dep")
    assert _has_warning(warnings, "go_replace", "github.com/old/dep")


def test_go_replacement_targets_are_not_chained():
    outputs = []
    orders = (
        ("replace example.test/A => example.test/B v1.0.0",
         "replace example.test/B => example.test/C v1.0.0"),
        ("replace example.test/B => example.test/C v1.0.0",
         "replace example.test/A => example.test/B v1.0.0"),
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "go.mod"
        for directives in orders:
            path.write_text(
                "module example.test/app\n"
                "require example.test/A v0.1.0\n"
                + "\n".join(directives) + "\n", encoding="utf-8")
            records, warnings = go_parser.parse_go_mod_records(path, "go.mod")
            dependencies = [
                (record["name"], record["version"], record["direct"])
                for record in records if record["kind"] == "dependency"]
            outputs.append(dependencies)
            assert len([w for w in warnings if w["category"] == "go_replace"]) == 2
    assert outputs == [
        [("example.test/B", "1.0.0", True)],
        [("example.test/B", "1.0.0", True)],
    ]


def test_go_version_specific_replace_beats_wildcard_in_any_order():
    outputs = []
    orders = (
        ("replace example.test/A => example.test/W v1.0.0",
         "replace example.test/A v0.1.0 => example.test/S v2.0.0"),
        ("replace example.test/A v0.1.0 => example.test/S v2.0.0",
         "replace example.test/A => example.test/W v1.0.0"),
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "go.mod"
        for directives in orders:
            path.write_text(
                "module example.test/app\n"
                "require example.test/A v0.1.0\n"
                + "\n".join(directives) + "\n", encoding="utf-8")
            records, warnings = go_parser.parse_go_mod_records(path, "go.mod")
            outputs.append([
                (record["name"], record["version"], record["direct"])
                for record in records if record["kind"] == "dependency"
            ])
            assert len([w for w in warnings if w["category"] == "go_replace"]) == 2
    assert outputs == [
        [("example.test/S", "2.0.0", True)],
        [("example.test/S", "2.0.0", True)],
    ]


def test_go_unused_same_module_version_replace_is_warning_only():
    records, warnings = _parse_go("go", "basic", "go.mod")

    assert not _records_named(records, "github.com/pinned/dep")
    assert _has_warning(warnings, "go_replace", "github.com/pinned/dep")


def test_go_local_replace_never_public_dependency():
    records, warnings = _parse_go("go", "basic", "go.mod")

    assert not _records_named(records, "./internal/local/dep")
    assert not [r for r in records if r["name"].startswith(".")]
    assert not _records_named(records, "github.com/local/dep")
    assert _has_warning(
        warnings, "go_local_replace", "./internal/local/dep")

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "go.mod"
        for target in (".", ".."):
            path.write_text(
                "module example.test/app\n"
                "require example.test/local v1.0.0\n"
                f"replace example.test/local => {target}\n",
                encoding="utf-8")
            records, warnings = go_parser.parse_go_mod_records(path, "go.mod")
            assert not _records_named(records, target)
            assert not _records_named(records, "example.test/local")
            assert _has_warning(warnings, "go_local_replace", target)


def test_go_malformed_lines_warn_and_parsing_continues():
    records, warnings = _parse_go("go", "broken", "go.mod")

    ok = _one(_requires(records), "github.com/ok/dep")
    assert ok["version"] == "1.0.0"
    assert not _records_named(records, "justonepath")
    assert not _records_named(records, "github.com/one/two")
    assert not _records_named(records, "v1.5.0")  # retract line ignored
    parse_errors = [w for w in warnings if w["category"] == "parse_error"]
    assert len(parse_errors) == 2
    assert any("line 5" in w["message"] for w in parse_errors)
    assert any("line 8" in w["message"] for w in parse_errors)


def test_go_require_rejects_extra_tokens():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "go.mod"
        path.write_text(
            "module example.test/app\n"
            "require example.test/pkg v1.2.3 unexpected\n",
            encoding="utf-8")
        records, warnings = go_parser.parse_go_mod_records(path, "go.mod")
    assert not _records_named(records, "example.test/pkg")
    assert _has_warning(warnings, "parse_error", "malformed require")

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "go.mod"
        path.write_text(
            "module example.test/app\n"
            "require example.test/old v1.2.3\n"
            "replace example.test/old => example.test/new v2.0.0 unexpected\n",
            encoding="utf-8")
        records, warnings = go_parser.parse_go_mod_records(path, "go.mod")
    assert _one(records, "example.test/old")["version"] == "1.2.3"
    assert not _records_named(records, "example.test/new")
    assert _has_warning(warnings, "parse_error", "malformed replace")


def test_go_invalid_version_tokens_remain_untracked():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "go.mod"
        path.write_text(
            "module example.test/app\n"
            "go latest\n"
            "go 0.0\n"
            "go 1.10\u0660\n"
            "toolchain default\n"
            "require example.test/invalid latest\n"
            "require example.test/invalid-prerelease v1.2.3-01\n"
            "require example.test/empty-prerelease v1.2.3-a..b\n"
            "require example.test/empty-build v1.2.3+build..x\n"
            "require example.test/unicode-digit v1.2.3\u0663\n"
            "require example.test/pseudo "
            "v0.0.0-20240101120000-abcdefabcdef\n"
            "replace example.test/invalid => example.test/replacement next\n",
            encoding="utf-8")
        records, warnings = go_parser.parse_go_mod_records(path, "go.mod")
        config = generate_config(scan_folder(tmpdir), "invalid-go")

    invalid = _one(records, "example.test/invalid")
    assert invalid["version"] is None and invalid["version_spec"] == "latest"
    pseudo = _one(records, "example.test/pseudo")
    assert pseudo["version"] == "0.0.0-20240101120000-abcdefabcdef"
    invalid_prerelease = _one(records, "example.test/invalid-prerelease")
    assert invalid_prerelease["version"] is None
    assert invalid_prerelease["version_spec"] == "v1.2.3-01"
    for name, spec in (
            ("example.test/empty-prerelease", "v1.2.3-a..b"),
            ("example.test/empty-build", "v1.2.3+build..x"),
            ("example.test/unicode-digit", "v1.2.3\u0663")):
        invalid_module = _one(records, name)
        assert invalid_module["version"] is None
        assert invalid_module["version_spec"] == spec
    runtimes = _records_named(records, "go")
    assert {record["version_spec"] for record in runtimes} == {
        "latest", "0.0", "1.10\u0660", "default"}
    assert len([warning for warning in warnings
                if warning["category"] == "unresolved_version"]) == 10
    assert not [product for product in config["products"]
                if product.get("product") == "golang"
                or product.get("module") in {
                    "example.test/invalid", "example.test/invalid-prerelease",
                    "example.test/empty-prerelease",
                    "example.test/empty-build", "example.test/unicode-digit",
                    "example.test/replacement"}]
    assert len([product for product in config["products"]
                if product.get("module") == "example.test/pseudo"]) == 1
    unmapped = config["_inventory"]["unmapped"]
    assert {item.get("version_spec") for item in unmapped} >= {
        "latest", "0.0", "1.10\u0660", "default", "v1.2.3-01",
        "v1.2.3-a..b", "v1.2.3+build..x", "v1.2.3\u0663"}


def test_go_parsing_is_deterministic():
    first = _parse_go("go", "basic", "go.mod")
    second = _parse_go("go", "basic", "go.mod")
    assert first == second


# ---------------------------------------------------------------------------
# .NET: helpers
# ---------------------------------------------------------------------------

def test_dotnet_tfm_version():
    f = dotnet_parser._tfm_version
    assert f("net8.0") == "8.0"
    assert f("net8.0-windows") == "8.0"
    assert f("net48") == "4.8"
    assert f("net472") == "4.7.2"
    assert f("net481") == "4.8.1"
    assert f("netcoreapp3.1") == "3.1"
    assert f("netstandard2.1") == "2.1"
    assert f("net") is None
    assert f("net8.0evil") is None
    assert f("something8.0") is None


def test_dotnet_requested_lower_bound():
    assert dotnet_parser._requested_lower_bound("[2.0.1, )") == "2.0.1"
    assert dotnet_parser._requested_lower_bound("[1.0.0]") == "1.0.0"
    assert dotnet_parser._requested_lower_bound("") is None


# ---------------------------------------------------------------------------
# .NET: project fixture (csproj + props + lock + global.json)
# ---------------------------------------------------------------------------

def test_dotnet_packagereference_forms():
    records, _ = _parse_csproj("dotnet", "project", "MyApp.csproj")

    serilog = _records_named(records, "Serilog")
    assert len(serilog) == 2  # Include and Update forms both recorded
    assert all(r["version"] == "4.0.0" and r["direct"] for r in serilog)

    case_pkg = _one(records, "CasePkg")
    assert case_pkg["version"] == "2.1.0"  # lowercase version attribute

    child = _one(records, "NowherePkg")  # <Version> child element form
    assert child["version"] is None
    assert child["version_spec"] == "$(NoSuchProp)"


def test_dotnet_property_resolution_and_unresolved_warning():
    records, warnings = _parse_csproj("dotnet", "project", "MyApp.csproj")

    resolved = _one(records, "Newtonsoft.Json")
    assert resolved["version"] == "13.0.3"
    assert resolved["version_spec"] is None

    unresolved = _one(records, "serilog.sinks.console")
    assert unresolved["version"] is None
    assert unresolved["version_spec"] == "$(MissingProp)"
    assert _has_warning(
        warnings, "unresolved_version", "serilog.sinks.console")
    assert _has_warning(warnings, "unresolved_version", "NowherePkg")


def test_dotnet_central_versions_case_insensitive_first_wins():
    records, warnings = _parse_csproj("dotnet", "project", "MyApp.csproj")

    central = _one(records, "CentralPkg")
    assert central["version"] == "1.5.0"   # 9.9.9 casing variant loses
    assert len(central["found_in"]) == 2
    assert central["found_in"][1]["path"] == \
        "dotnet/project/Directory.Packages.props"
    assert central["found_in"][1]["locator"] == "PackageVersion:centralpkg"
    assert not _has_warning(warnings, "unresolved_version", "CentralPkg")


def test_dotnet_lock_file_fallback():
    records, warnings = _parse_csproj("dotnet", "project", "MyApp.csproj")

    lock_only = _one(records, "LockOnlyPkg")
    assert lock_only["version"] == "2.0.1"
    assert lock_only["found_in"][1]["path"] == \
        "dotnet/project/packages.lock.json"
    assert lock_only["found_in"][1]["locator"] == "lock:LockOnlyPkg"

    # Lock-only transitive packages are a fallback source, not inventory.
    assert not _records_named(records, "TransitivePkg")
    assert not _has_warning(warnings, "unresolved_version", "LockOnlyPkg")


def test_dotnet_target_frameworks_as_runtime_records():
    records, _ = _parse_csproj("dotnet", "project", "MyApp.csproj")
    runtimes = [r for r in records if r["kind"] == "runtime"
                and r["name"] == "dotnet"]
    assert len(runtimes) == 1
    assert runtimes[0]["version"] == "8.0"
    assert runtimes[0]["found_in"][0]["locator"] == "TargetFramework"

    fs_records, _ = _parse_csproj("dotnet", "project", "Lib.fsproj")
    fs_runtimes = sorted(
        r["version"] for r in fs_records
        if r["kind"] == "runtime" and r["name"] == "dotnet")
    assert fs_runtimes == ["6.0", "8.0"]
    assert _one(fs_records, "FSharp.Data")["version"] == "6.4.0"

    vb_records, _ = _parse_csproj("dotnet", "project", "Lib.vbproj")
    assert _one(vb_records, "Humanizer")["version"] == "2.14.1"

    with tempfile.TemporaryDirectory() as tmpdir:
        project = Path(tmpdir) / "Lower.csproj"
        project.write_text(
            '<project><propertygroup><targetframework>net8.0</targetframework>'
            '</propertygroup><itemgroup><packagereference include="Lower.Pkg" '
            'version="1.2.3" /></itemgroup></project>', encoding="utf-8")
        lower_records, lower_warnings = dotnet_parser.parse_csproj_records(
            project, "Lower.csproj", root=tmpdir)
        project.write_text(
            '<Project><PropertyGroup><TargetFramework>net8.0evil</TargetFramework>'
            '</PropertyGroup></Project>', encoding="utf-8")
        invalid_records, invalid_warnings = dotnet_parser.parse_csproj_records(
            project, "Lower.csproj", root=tmpdir)
    assert _one(lower_records, "dotnet")["version"] == "8.0"
    assert _one(lower_records, "Lower.Pkg")["version"] == "1.2.3"
    assert lower_warnings == []
    assert not [r for r in invalid_records if r["name"] == "dotnet"]
    assert _has_warning(
        invalid_warnings, "unresolved_version", "net8.0evil")


def test_dotnet_props_only_parse_first_wins_on_casing():
    path = FIX / "dotnet" / "project" / "Directory.Packages.props"
    records, warnings = dotnet_parser.parse_directory_packages_props(
        path, "dotnet/project/Directory.Packages.props")

    assert len(records) == 1
    assert records[0]["name"] == "centralpkg"
    assert records[0]["version"] == "1.5.0"
    assert records[0]["found_in"][0]["locator"] == "PackageVersion:centralpkg"
    assert warnings == []


def test_dotnet_global_json_sdk():
    path = FIX / "dotnet" / "project" / "global.json"
    records, warnings = dotnet_parser.parse_global_json_records(
        path, "dotnet/project/global.json")

    assert warnings == []
    sdk = _one(records, "dotnet-sdk")
    assert sdk["version"] == "8.0.100"
    assert sdk["kind"] == "runtime"
    assert sdk["found_in"][0]["locator"] == "sdk.version"


def test_dotnet_project_without_siblings_warns():
    with tempfile.TemporaryDirectory() as tmpdir:
        csproj = Path(tmpdir) / "Lone.csproj"
        csproj.write_text(
            "<Project><ItemGroup>"
            "<PackageReference Include=\"Lone\" />"
            "</ItemGroup></Project>", encoding="utf-8")
        records, warnings = dotnet_parser.parse_csproj_records(
            csproj, "Lone.csproj")

    lone = _one(records, "Lone")
    assert lone["version"] is None
    assert lone["version_spec"] is None
    assert _has_warning(
        warnings, "unresolved_version",
        "not in Directory.Packages.props or packages.lock.json")


def test_dotnet_finds_central_versions_at_scan_root():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project_dir = root / "src" / "app"
        project_dir.mkdir(parents=True)
        (root / "Directory.Packages.props").write_text(
            '<Project><ItemGroup><PackageVersion Include="RootPkg" '
            'Version="4.2.0" /></ItemGroup></Project>', encoding="utf-8")
        csproj = project_dir / "App.csproj"
        csproj.write_text(
            '<Project><ItemGroup><PackageReference Include="RootPkg" />'
            '</ItemGroup></Project>', encoding="utf-8")
        records, warnings = dotnet_parser.parse_csproj_records(
            csproj, "src/app/App.csproj", root=root)
    package = _one(records, "RootPkg")
    assert package["version"] == "4.2.0"
    assert package["found_in"][1]["path"] == "Directory.Packages.props"
    assert warnings == []


def test_dotnet_central_property_expressions_remain_unresolved():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        props = root / "Directory.Packages.props"
        props.write_text(
            '<Project><ItemGroup>'
            '<PackageVersion Include="PropertyPkg" '
            'Version="$(SharedVersion)" />'
            '<PackageVersion Include="ItemPkg" '
            'Version="@(SharedVersions)" />'
            '<PackageVersion Include="MetadataPkg" '
            'Version="%(Version.Identity)" />'
            '</ItemGroup></Project>',
            encoding="utf-8")
        project = root / "App.csproj"
        project.write_text(
            '<Project><ItemGroup>'
            '<PackageReference Include="PropertyPkg" />'
            '<PackageReference Include="ItemPkg" />'
            '<PackageReference Include="MetadataPkg" />'
            '<PackageReference Include="DirectItem" '
            'Version="@(DirectVersions)" />'
            '<PackageReference Include="DirectMetadata" '
            'Version="%(Direct.Identity)" />'
            '</ItemGroup></Project>', encoding="utf-8")

        project_records, project_warnings = \
            dotnet_parser.parse_csproj_records(
                project, "App.csproj", root=root)
        props_records, props_warnings = \
            dotnet_parser.parse_directory_packages_props(
                props, "Directory.Packages.props")

    central_expressions = {
        "PropertyPkg": "$(SharedVersion)",
        "ItemPkg": "@(SharedVersions)",
        "MetadataPkg": "%(Version.Identity)",
    }
    for name, expression in central_expressions.items():
        for records, warnings in (
                (project_records, project_warnings),
                (props_records, props_warnings)):
            package = _one(records, name)
            assert package["version"] is None
            assert package["version_spec"] == expression
            assert _has_warning(warnings, "unresolved_version", expression)
        assert len(_one(project_records, name)["found_in"]) == 2

    for name, expression in (
            ("DirectItem", "@(DirectVersions)"),
            ("DirectMetadata", "%(Direct.Identity)")):
        package = _one(project_records, name)
        assert package["version"] is None
        assert package["version_spec"] == expression
        assert _has_warning(
            project_warnings, "unresolved_version", expression)


def test_dotnet_ranges_locks_and_conditional_properties():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Directory.Packages.props").write_text(
            '<Project><ItemGroup>'
            '<PackageVersion Include="CentralRange" Version="[3.0,4.0)" />'
            '<PackageVersion Include="CentralLocked" Version="1.*" />'
            '</ItemGroup><Choose>'
            '<When Condition="\'$(TargetFramework)\' == \'net8.0\'">'
            '<ItemGroup><PackageVersion Include="ConditionalCentral" '
            'Version="5.0.0" /></ItemGroup></When>'
            '<Otherwise><ItemGroup><PackageVersion '
            'Include="ConditionalCentral" Version="6.0.0" />'
            '</ItemGroup></Otherwise></Choose>'
            '<ItemGroup>'
            '<PackageVersion Include="AgreeingCentral" Version="7.0.0" />'
            '<PackageVersion Include="AgreeingCentral" Version="7.0.0" '
            'Condition="\'$(TargetFramework)\' == \'net8.0\'" />'
            '<PackageVersion Include="DisagreeingCentral" Version="7.0.0" />'
            '<PackageVersion Include="DisagreeingCentral" Version="8.0.0" '
            'Condition="\'$(TargetFramework)\' == \'net8.0\'" />'
            '<PackageVersion Include="ChildConditional"><Version '
            'Condition="\'$(TargetFramework)\' == \'net8.0\'">9.0.0'
            '</Version></PackageVersion>'
            '<PackageVersion Include="EmptyConditional" Version="1.0.0" />'
            '<PackageVersion Include="EmptyConditional" '
            'Condition="\'$(TargetFramework)\' == \'net8.0\'" />'
            '</ItemGroup><Target Name="CentralVersions"><ItemGroup>'
            '<PackageVersion Include="TargetConditional" Version="2.0.0" />'
            '</ItemGroup></Target></Project>', encoding="utf-8")
        (root / "packages.lock.json").write_text(json.dumps({
            "dependencies": {"net8.0": {
                "DirectLocked": {"type": "Direct", "resolved": "2.4.0"},
                "DirectChildLocked": {
                    "type": "Direct", "resolved": "3.2.0"},
                "ExpressionLocked": {
                    "type": "Direct", "resolved": "4.4.4"},
                "CentralLocked": {"type": "Direct", "resolved": "1.9.0"},
            }},
        }), encoding="utf-8")
        project = root / "App.csproj"
        project.write_text(
            '<Project>'
            '<PropertyGroup><ChooseVersion>2.0.0</ChooseVersion>'
            '</PropertyGroup>'
            '<PropertyGroup Condition="\'$(Configuration)\' == \'Debug\'">'
            '<PkgVersion>1.0.0</PkgVersion></PropertyGroup>'
            '<PropertyGroup Condition="\'$(Configuration)\' == \'Release\'">'
            '<PkgVersion>2.0.0</PkgVersion></PropertyGroup>'
            '<Choose>'
            '<When Condition="\'$(Configuration)\' == \'Debug\'">'
            '<PropertyGroup><ChooseVersion>1.0.0</ChooseVersion>'
            '</PropertyGroup></When>'
            '<Otherwise><PropertyGroup>'
            '<ChooseVersion>3.0.0</ChooseVersion>'
            '</PropertyGroup></Otherwise>'
            '</Choose>'
            '<ItemGroup>'
            '<PackageReference Include="DirectRange" Version="[1.0,2.0)" />'
            '<PackageReference Include="DirectLocked" Version="2.*" />'
            '<PackageReference Include="DirectChildConditional"><Version '
            'Condition="\'$(TargetFramework)\' == \'net8.0\'">3.1.0'
            '</Version></PackageReference>'
            '<PackageReference Include="DirectChildLocked"><Version '
            'Condition="\'$(TargetFramework)\' == \'net8.0\'">3.*'
            '</Version></PackageReference>'
            '<PackageReference Include="AttributeWins" Version="1.0.0">'
            '<Version Condition="\'$(TargetFramework)\' == \'net8.0\'">'
            '2.0.0</Version><Version '
            'Condition="\'$(TargetFramework)\' == \'net9.0\'">3.0.0'
            '</Version></PackageReference>'
            '<PackageReference Include="EmptyConditionalChild"><Version '
            'Condition="\'$(TargetFramework)\' == \'net8.0\'" />'
            '</PackageReference>'
            '<PackageReference Include="MultiChildConditional"><Version '
            'Condition="\'$(TargetFramework)\' == \'net8.0\'" />'
            '<Version Condition="\'$(TargetFramework)\' == \'net9.0\'">'
            '3.5.0</Version></PackageReference>'
            '<PackageReference Include="ExpressionLocked" '
            'Version="$(Missing)" />'
            '<PackageReference Include="CentralRange" />'
            '<PackageReference Include="CentralLocked" />'
            '<PackageReference Include="ConditionalCentral" />'
            '<PackageReference Include="AgreeingCentral" />'
            '<PackageReference Include="DisagreeingCentral" />'
            '<PackageReference Include="ChildConditional" />'
            '<PackageReference Include="EmptyConditional" />'
            '<PackageReference Include="TargetConditional" />'
            '<PackageReference Include="ConditionalPkg" '
            'Version="$(PkgVersion)" />'
            '<PackageReference Include="ChooseConditionalPkg" '
            'Version="$(ChooseVersion)" />'
            '<PackageReference Include="ExactPkg" Version="1.2.3" />'
            '</ItemGroup></Project>', encoding="utf-8")
        (root / "global.json").write_text(
            '{"sdk": {"version": "8.*"}}', encoding="utf-8")

        records, warnings = dotnet_parser.parse_csproj_records(
            project, "App.csproj", root=root)
        sdk_records, sdk_warnings = dotnet_parser.parse_global_json_records(
            root / "global.json", "global.json")
        config = generate_config(scan_folder(root), "ambiguous-dotnet")

    for name, spec in (
            ("DirectRange", "[1.0,2.0)"),
            ("CentralRange", "[3.0,4.0)"),
            ("ConditionalPkg", "$(PkgVersion)"),
            ("ChooseConditionalPkg", "$(ChooseVersion)")):
        package = _one(records, name)
        assert package["version"] is None
        assert package["version_spec"] == spec
        assert _has_warning(warnings, "unresolved_version", name)

    conditional_central = _one(records, "ConditionalCentral")
    assert conditional_central["version"] is None
    assert conditional_central["version_spec"] == (
        "conditional PackageVersion: 5.0.0 | 6.0.0")
    assert _has_warning(warnings, "unresolved_version", "ConditionalCentral")

    direct_child = _one(records, "DirectChildConditional")
    assert direct_child["version"] is None
    assert direct_child["version_spec"] == "3.1.0"
    assert _has_warning(
        warnings, "unresolved_version", "DirectChildConditional")
    direct_child_locked = _one(records, "DirectChildLocked")
    assert direct_child_locked["version"] == "3.2.0"
    assert direct_child_locked["version_spec"] == "3.*"
    assert direct_child_locked["found_in"][-1]["locator"] == (
        "lock:DirectChildLocked")
    mixed_version = _one(records, "AttributeWins")
    assert mixed_version["version"] is None
    assert mixed_version["version_spec"] == (
        "conditional PackageReference Version: 1.0.0 | 2.0.0 | 3.0.0")
    assert _has_warning(warnings, "unresolved_version", "AttributeWins")
    empty_child = _one(records, "EmptyConditionalChild")
    assert empty_child["version"] is None
    assert empty_child["version_spec"] is None
    assert _has_warning(warnings, "unresolved_version", "is empty")
    multi_child = _one(records, "MultiChildConditional")
    assert multi_child["version"] is None
    assert multi_child["version_spec"] == (
        "conditional PackageReference Version: no version | 3.5.0")
    assert _has_warning(
        warnings, "unresolved_version", "MultiChildConditional")
    expression_locked = _one(records, "ExpressionLocked")
    assert expression_locked["version"] == "4.4.4"
    assert expression_locked["version_spec"] == "$(Missing)"
    assert expression_locked["found_in"][-1]["locator"] == (
        "lock:ExpressionLocked")

    assert _one(records, "AgreeingCentral")["version"] == "7.0.0"
    for name, spec in (
            ("DisagreeingCentral",
             "conditional PackageVersion: 7.0.0 | 8.0.0"),
            ("ChildConditional",
             "conditional PackageVersion: 9.0.0"),
            ("EmptyConditional",
             "conditional PackageVersion: 1.0.0 | no version"),
            ("TargetConditional",
             "conditional PackageVersion: 2.0.0")):
        package = _one(records, name)
        assert package["version"] is None
        assert package["version_spec"] == spec
        assert _has_warning(warnings, "unresolved_version", name)

    direct_locked = _one(records, "DirectLocked")
    assert direct_locked["version"] == "2.4.0"
    assert direct_locked["version_spec"] == "2.*"
    assert direct_locked["found_in"][-1]["locator"] == "lock:DirectLocked"
    central_locked = _one(records, "CentralLocked")
    assert central_locked["version"] == "1.9.0"
    assert central_locked["version_spec"] == "1.*"
    assert [loc["locator"] for loc in central_locked["found_in"]] == [
        "PackageReference:CentralLocked",
        "PackageVersion:CentralLocked",
        "lock:CentralLocked",
    ]
    assert _one(records, "ExactPkg")["version"] == "1.2.3"

    sdk = _one(sdk_records, "dotnet-sdk")
    assert sdk["version"] is None and sdk["version_spec"] == "8.*"
    assert _has_warning(sdk_warnings, "unresolved_version", "not exact")

    tracked_names = {
        p["package"] for p in config["products"]
        if p.get("source") == "nuget_registry"}
    assert {
        "DirectRange", "CentralRange", "ConditionalPkg",
        "ChooseConditionalPkg", "ConditionalCentral",
        "DisagreeingCentral", "ChildConditional", "EmptyConditional",
        "TargetConditional", "DirectChildConditional",
        "EmptyConditionalChild", "MultiChildConditional", "AttributeWins",
    }.isdisjoint(tracked_names)
    assert {"DirectLocked", "CentralLocked", "ExactPkg",
            "AgreeingCentral", "DirectChildLocked",
            "ExpressionLocked"} <= tracked_names
    assert not [p for p in config["products"] if p.get("product") == "dotnet"]


def test_dotnet_package_identity_is_case_insensitive():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "Directory.Packages.props").write_text(
            '<Project><ItemGroup><PackageVersion Include="centralpkg" '
            'Version="1.5.0" /></ItemGroup></Project>', encoding="utf-8")
        (root / "App.csproj").write_text(
            '<Project><ItemGroup><PackageReference Include="CentralPkg" />'
            '</ItemGroup></Project>', encoding="utf-8")
        config = generate_config(scan_folder(root), "case-insensitive")

    packages = [
        product for product in config["products"]
        if product.get("source") == "nuget_registry"
        and product.get("package", "").lower() == "centralpkg"]
    assert len(packages) == 1
    assert packages[0]["package"] == "CentralPkg"
    assert {location["path"] for location in packages[0]["_found_in"]} == {
        "App.csproj", "Directory.Packages.props"}


def test_dotnet_malformed_sidecars_and_entities_warn():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        project = root / "App.csproj"
        project.write_text(
            '<Project><ItemGroup><PackageReference Include="Serilog" '
            'Version="4.0.0" /></ItemGroup></Project>', encoding="utf-8")
        (root / "packages.lock.json").write_text("{broken", encoding="utf-8")
        (root / "Directory.Packages.props").write_text(
            "<Project>", encoding="utf-8")
        records, warnings = dotnet_parser.parse_csproj_records(
            project, "App.csproj", root=root)
        assert _one(records, "Serilog")["version"] == "4.0.0"
        assert _has_warning(warnings, "parse_error", "packages.lock.json")
        assert _has_warning(warnings, "parse_error", "Directory.Packages.props")

        project.write_text(
            '<!DOCTYPE Project [<!ENTITY boom "expanded">]>'
            '<Project><PropertyGroup><TargetFramework>&boom;</TargetFramework>'
            '</PropertyGroup></Project>', encoding="utf-8")
        records, warnings = dotnet_parser.parse_csproj_records(
            project, "App.csproj", root=root)
        assert records == []
        assert _has_warning(warnings, "parse_error", "forbidden DTD/entity")


def test_dotnet_falsey_malformed_lock_structures_warn():
    cases = (
        ({}, "no dependencies object"),
        ({"dependencies": None}, "dependencies value"),
        ({"dependencies": []}, "dependencies value"),
        ({"dependencies": {"net8.0": None}}, "dependency group 'net8.0'"),
        ({"dependencies": {"net8.0": []}}, "dependency group 'net8.0'"),
        ({"dependencies": {"net8.0": {"Pkg": None}}}, "package 'Pkg'"),
        ({"dependencies": {"net8.0": {"Pkg": []}}}, "package 'Pkg'"),
        ({"dependencies": {"net8.0": {"Pkg": {
            "type": [], "resolved": "1.0.0"}}}}, "type is not a string"),
        ({"dependencies": {"net8.0": {"Pkg": {
            "type": "Direct", "resolved": []}}}}, "resolved version"),
        ({"dependencies": {"net8.0": {"Pkg": {
            "type": "Direct", "resolved": "1.*"}}}}, "is not exact"),
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        lock_file = root / "packages.lock.json"
        for document, message in cases:
            lock_file.write_text(json.dumps(document), encoding="utf-8")
            versions, warning = dotnet_parser._read_lock_versions(
                lock_file, root, "packages.lock.json")
            assert versions == {}
            assert warning["category"] == "parse_error"
            assert message in warning["message"]


def test_dotnet_global_json_malformed_and_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        bad = Path(tmpdir) / "global.json"
        bad.write_text("{not json", encoding="utf-8")
        records, warnings = dotnet_parser.parse_global_json_records(
            bad, "global.json")
        assert records == []
        assert warnings and warnings[0]["category"] == "parse_error"

        empty = Path(tmpdir) / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        records, warnings = dotnet_parser.parse_global_json_records(
            empty, "empty.json")
        assert records == [] and warnings == []

        empty.write_text('{"sdk": null}', encoding="utf-8")
        records, warnings = dotnet_parser.parse_global_json_records(
            empty, "empty.json")
        assert records == [] and warnings == []

        for sdk in ([], "", 0, False):
            empty.write_text(json.dumps({"sdk": sdk}), encoding="utf-8")
            records, warnings = dotnet_parser.parse_global_json_records(
                empty, "empty.json")
            assert records == []
            assert _has_warning(warnings, "parse_error", "sdk value")

        for version in (False, 0, [], {}):
            empty.write_text(json.dumps({"sdk": {"version": version}}),
                             encoding="utf-8")
            records, warnings = dotnet_parser.parse_global_json_records(
                empty, "empty.json")
            assert records == []
            assert _has_warning(
                warnings, "parse_error", "sdk.version value")

        empty.write_text('{"sdk": {"version": "   "}}', encoding="utf-8")
        records, warnings = dotnet_parser.parse_global_json_records(
            empty, "empty.json")
        assert records == [] and warnings == []


def test_dotnet_parsing_is_deterministic():
    first = _parse_csproj("dotnet", "project", "MyApp.csproj")
    second = _parse_csproj("dotnet", "project", "MyApp.csproj")
    assert first == second


# ---------------------------------------------------------------------------
# Consumed .NET sidecar listing (Directory.Packages.props / packages.lock.json)
# ---------------------------------------------------------------------------

def test_consumed_dotnet_sidecars_listed_even_when_they_resolve_nothing():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "App.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            "<TargetFramework>net8.0</TargetFramework></PropertyGroup>"
            '<ItemGroup><PackageReference Include="Serilog" '
            'Version="2.12.0" /></ItemGroup></Project>')
        # Both sidecars read and parse cleanly but carry no resolvable
        # entries: zero records contributed, zero warnings, still listed.
        (root / "Directory.Packages.props").write_text("<Project />")
        (root / "packages.lock.json").write_text(json.dumps(
            {"dependencies": {}}))
        scan = scan_folder(root)
    assert scan["warnings"] == []
    assert scan["files"] == ["App.csproj", "Directory.Packages.props",
                             "packages.lock.json"]


def test_only_the_nearest_central_props_sidecar_is_listed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "Directory.Packages.props").write_text(
            '<Project><ItemGroup><PackageVersion Include="Root.Pkg" '
            'Version="1.0.0" /></ItemGroup></Project>')
        (root / "src" / "Directory.Packages.props").write_text(
            '<Project><ItemGroup><PackageVersion Include="Src.Pkg" '
            'Version="2.0.0" /></ItemGroup></Project>')
        (root / "src" / "App.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            "<TargetFramework>net8.0</TargetFramework></PropertyGroup>"
            '<ItemGroup><PackageReference Include="Src.Pkg" /></ItemGroup>'
            "</Project>")
        scan = scan_folder(root)
    # The project file consumes the nearest props walking up (src/);
    # the root props is merely present and stays unlisted. Neither
    # props file is a discovery candidate, so the consumed src props
    # appears in the manifest list only through the sidecar merge.
    assert scan["files"] == ["src/App.csproj",
                             "src/Directory.Packages.props"]
    assert scan["warnings"] == []
    src_pkg = _one(scan["records"], "Src.Pkg")
    assert src_pkg["version"] == "2.0.0"


def test_malformed_dotnet_sidecars_warn_but_are_not_listed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "App.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            "<TargetFramework>net8.0</TargetFramework></PropertyGroup>"
            '<ItemGroup><PackageReference Include="Serilog" '
            'Version="2.12.0" /></ItemGroup></Project>')
        (root / "Directory.Packages.props").write_text("<Project><unclosed>")
        (root / "packages.lock.json").write_text("{not json")
        scan = scan_folder(root)
    assert scan["files"] == ["App.csproj"]
    assert _has_warning(scan["warnings"], "parse_error",
                        "Directory.Packages.props")
    assert _has_warning(scan["warnings"], "parse_error",
                        "packages.lock.json")


def test_dotnet_sidecars_absent_without_project_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "global.json").write_text(
            json.dumps({"sdk": {"version": "8.0.100"}}))
        (root / "Directory.Packages.props").write_text("<Project />")
        (root / "packages.lock.json").write_text(json.dumps(
            {"dependencies": {}}))
        scan = scan_folder(root)
    # global.json never triggers the project sidecar readers: the props
    # and lock files are never read and stay unlisted.
    assert scan["files"] == ["global.json"]
    assert scan["warnings"] == []


# ---------------------------------------------------------------------------

TESTS = [
    test_go_strip_v,
    test_go_is_local_path,
    test_go_module_go_and_toolchain_directives,
    test_go_direct_requires_and_indirect_count,
    test_go_module_replace_warning_provenance_and_target,
    test_go_replace_before_require_is_order_independent,
    test_go_replacement_targets_are_not_chained,
    test_go_version_specific_replace_beats_wildcard_in_any_order,
    test_go_unused_same_module_version_replace_is_warning_only,
    test_go_local_replace_never_public_dependency,
    test_go_malformed_lines_warn_and_parsing_continues,
    test_go_require_rejects_extra_tokens,
    test_go_invalid_version_tokens_remain_untracked,
    test_go_parsing_is_deterministic,
    test_dotnet_tfm_version,
    test_dotnet_requested_lower_bound,
    test_dotnet_packagereference_forms,
    test_dotnet_property_resolution_and_unresolved_warning,
    test_dotnet_central_versions_case_insensitive_first_wins,
    test_dotnet_lock_file_fallback,
    test_dotnet_target_frameworks_as_runtime_records,
    test_dotnet_props_only_parse_first_wins_on_casing,
    test_dotnet_global_json_sdk,
    test_dotnet_project_without_siblings_warns,
    test_dotnet_finds_central_versions_at_scan_root,
    test_dotnet_central_property_expressions_remain_unresolved,
    test_dotnet_ranges_locks_and_conditional_properties,
    test_dotnet_package_identity_is_case_insensitive,
    test_dotnet_malformed_sidecars_and_entities_warn,
    test_dotnet_falsey_malformed_lock_structures_warn,
    test_dotnet_global_json_malformed_and_empty,
    test_dotnet_parsing_is_deterministic,
    test_consumed_dotnet_sidecars_listed_even_when_they_resolve_nothing,
    test_only_the_nearest_central_props_sidecar_is_listed,
    test_malformed_dotnet_sidecars_warn_but_are_not_listed,
    test_dotnet_sidecars_absent_without_project_files,
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
    print("OK test_inventory_go_dotnet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
