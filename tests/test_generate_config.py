"""Tests for the manifest-to-config generator (helper_scripts/eol_inventory).

Covers the normalized dependency records, provenance merging, deterministic
discovery with exclusions, warnings, `_inventory` metadata, legacy
`_skipped_npm_packages` compatibility, and the CLI safety behavior
(overwrite guard, atomic ASCII output, --strict). Standalone assertion
script: no pytest, no network, no subprocesses.

Run from the repository root:  python tests/test_generate_config.py
"""
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]
_HELPER_DIR = ROOT / "helper_scripts"
sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory as gc
from generate_config import (
    _dockerfile_from_key,
    _live_smoke_command,
    _merge_existing_config,
    main as generate_config_main,
)
import eol_inventory.discovery as discovery_module
import eol_inventory.parsers.gitlab_ci as gitlab_parser
import eol_inventory.parsers.python as python_parser
from eol_inventory.models import add_location, new_record

FIX = ROOT / "tests" / "fixtures" / "generate_config"


def _posix(p):
    return str(p).replace("\\", "/")


def _products(config):
    return [p for p in config["products"] if not p.get("_section")]


def _sections(config):
    return [p["_section"] for p in config["products"] if p.get("_section")]


def _scan(*parts, **kwargs):
    return gc.scan_folder(str(FIX.joinpath(*parts)), **kwargs)


def _locations(entry):
    return [(loc["path"], loc.get("line"), loc.get("locator"))
            for loc in entry["_found_in"]]


# ---------------------------------------------------------------------------
# Version helpers and mapping behavior (preserved from the original generator)
# ---------------------------------------------------------------------------

def test_version_helpers():
    assert gc._major("3.5.7") == "3"
    assert gc._major("17") == "17"
    assert gc._major_minor("3.5.7") == "3.5"
    assert gc._major_minor("18") == "18"
    assert gc._clean_version("^1.2.3") == "1.2.3"
    assert gc._clean_version("~2.0.0") == "2.0.0"
    assert gc._clean_version(">=18 <21") == "18"
    assert gc._clean_version("1.3.0") == "1.3.0"
    assert gc._clean_version("") == ""


def test_live_smoke_command_quotes_interpreter_and_output():
    assert _live_smoke_command(
        r"C:\configs\my project.json",
        executable=r"C:\Program Files\Python\python.exe",
        platform="nt",
    ) == (
        "& 'C:\\Program Files\\Python\\python.exe' lambda_function.py "
        "'C:\\configs\\my project.json'")
    assert _live_smoke_command(
        "/tmp/my project.json",
        executable="/opt/Python Builds/python3",
        platform="posix",
    ) == (
        "'/opt/Python Builds/python3' lambda_function.py "
        "'/tmp/my project.json'")


def test_entry_builders():
    assert gc._eol_entry("react", "18", "React 18") == {
        "product": "react", "version": "18", "label": "React 18"}
    assert gc._mc_entry("io.netty", "netty-codec-http", "4.1.111.Final",
                        "Netty Codec HTTP 4.1.111") == {
        "source": "maven_central", "group": "io.netty",
        "artifact": "netty-codec-http", "version": "4.1.111.Final",
        "label": "Netty Codec HTTP 4.1.111"}


def test_map_java_dep():
    assert gc._map_java_dep(
        "org.springframework.boot", "spring-boot-starter-web", "3.3.4") == {
        "product": "spring-boot", "version": "3.3", "label": "Spring Boot 3.3"}
    assert gc._map_java_dep(
        "org.springframework", "spring-core", "6.1.13") == {
        "product": "spring-framework", "version": "6.1",
        "label": "Spring Framework 6.1"}
    assert gc._map_java_dep(
        "org.springframework.security", "spring-security-config", "6.4.1") == {
        "product": "spring-security", "version": "6.4",
        "label": "Spring Security 6.4"}
    assert gc._map_java_dep(
        "org.apache.tomcat.embed", "tomcat-embed-core", "10.1.30") == {
        "product": "tomcat", "version": "10.1", "label": "Apache Tomcat 10.1"}
    assert gc._map_java_dep(
        "org.apache.logging.log4j", "log4j-core", "2.23.1") == {
        "product": "log4j", "version": "2", "label": "Apache Log4j 2.x"}
    assert gc._map_java_dep(
        "com.fasterxml.jackson.core", "jackson-databind", "2.17.2") == {
        "source": "jackson_lifecycle", "version": "2.17", "label": "Jackson 2.17"}
    assert gc._map_java_dep(
        "software.amazon.awssdk", "sdk-core", "2.25.60") == {
        "source": "aws_sdk_lifecycle", "sdk": "SDK for Java", "major": "2.x",
        "label": "AWS SDK for Java v2"}
    v1 = gc._map_java_dep("com.amazonaws", "aws-java-sdk-s3", "1.12.700")
    assert v1 == {"source": "aws_sdk_lifecycle", "sdk": "SDK for Java",
                  "major": "1.x", "label": "AWS SDK for Java v1 (legacy)"}
    assert gc._map_java_dep(
        "org.jetbrains.kotlin", "kotlin-stdlib", "2.0.0") == {
        "product": "kotlin", "version": "2.0", "label": "Kotlin 2.0"}
    b = gc._map_java_dep("org.webjars", "bootstrap", "5.3.3")
    assert b == {"product": "bootstrap", "version": "5",
                 "label": "Bootstrap 5 (using 5.3.3)"}
    q = gc._map_java_dep("org.webjars.npm", "jquery", "3.7.1")
    assert q == {"product": "jquery", "version": "3",
                 "label": "jQuery 3 (using 3.7.1)"}
    # skipped artifacts and generic webjars
    assert gc._map_java_dep("junit", "junit", "4.13.2") is None
    assert gc._map_java_dep("org.mockito", "mockito-inline", "5.2.0") is None
    assert gc._map_java_dep("com.google.code.gson", "gson", "2.11.0") is None
    assert gc._map_java_dep("org.webjars.npm", "chart.js", "4.4.3") is None
    # silent skips: snapshots, internal groups, unresolved properties
    assert gc._map_java_dep("com.example", "lib", "2.0.0-SNAPSHOT") is None
    assert gc._map_java_dep("com.example", "lib", "2.0.0-snapshot") is None
    assert gc._map_java_dep("internal.tools", "lib", "1.0.0") is None
    assert gc._map_java_dep("org.acme", "lib", "${lib.version}") is None
    # generic fallback keeps the full Maven version
    assert gc._map_java_dep(
        "org.apache.commons", "commons-lang3", "3.14.0") == {
        "source": "maven_central", "group": "org.apache.commons",
        "artifact": "commons-lang3", "version": "3.14.0",
        "label": "commons-lang3 3.14.0"}


def test_map_npm_dep():
    assert gc._map_npm_dep("react", "^18.2.0") == {
        "product": "react", "version": "18", "label": "React 18"}
    assert gc._map_npm_dep("react-dom", "^18.2.0") == {
        "product": "react", "version": "18", "label": "React 18"}
    assert gc._map_npm_dep("vue", "^3.4.0") == {
        "product": "vue", "version": "3", "label": "Vue 3"}
    assert gc._map_npm_dep("@angular/core", "^17.3.0") == {
        "product": "angular", "version": "17", "label": "Angular 17"}
    assert gc._map_npm_dep("next", "^14.2.3") == {
        "product": "nextjs", "version": "14.2", "label": "Next.js 14.2"}
    assert gc._map_npm_dep("nuxt", "^3.11.0") == {
        "product": "nuxt", "version": "3", "label": "Nuxt 3"}
    assert gc._map_npm_dep("typescript", "^5.4.5") == {
        "product": "typescript", "version": "5.4", "label": "TypeScript 5.4"}
    assert gc._map_npm_dep("node", ">=18 <21") == {
        "product": "nodejs", "version": "18", "label": "Node.js 18"}
    assert gc._map_npm_dep("express", "^4.19.2") == {
        "product": "express", "version": "4", "label": "Express 4"}
    assert gc._map_npm_dep("axios", "~1.6.8") is None
    assert gc._map_npm_dep("@company/tokens", "^1.0.0") is None


def test_pom_property_mappings():
    assert gc._POM_PROPERTY_MAPPINGS["java.version"]("17") == {
        "product": "amazon-corretto", "version": "17",
        "label": "Amazon Corretto (OpenJDK) 17"}
    assert gc._POM_PROPERTY_MAPPINGS["maven.compiler.release"]("21") == {
        "product": "amazon-corretto", "version": "21",
        "label": "Amazon Corretto (OpenJDK) 21"}
    assert gc._POM_PROPERTY_MAPPINGS["tomcat.version"]("10.1.54") == {
        "product": "tomcat", "version": "10.1", "label": "Apache Tomcat 10.1"}
    assert gc._POM_PROPERTY_MAPPINGS["kotlin.version"]("1.9.24") == {
        "product": "kotlin", "version": "1.9", "label": "Kotlin 1.9"}
    mc = gc._POM_PROPERTY_MAPPINGS["netty.version"]("4.1.111.Final")
    assert mc["source"] == "maven_central" and mc["version"] == "4.1.111.Final"


# ---------------------------------------------------------------------------
# Normalized record parsers
# ---------------------------------------------------------------------------

def test_parse_pom_records():
    records, warnings = gc.parse_pom_records(
        FIX / "maven_multi" / "pom.xml", "pom.xml")
    assert warnings == []
    by_name = {r["name"]: r for r in records if r["kind"] != "property"}
    parent = by_name["org.springframework.boot:spring-boot-starter-parent"]
    assert parent["version"] == "3.3.4" and parent["kind"] == "parent"
    assert parent["group"] == "org.springframework.boot"
    assert parent["artifact"] == "spring-boot-starter-parent"
    assert parent["ecosystem"] == "java" and parent["scope"] == "runtime"
    assert parent["direct"] is True
    assert parent["found_in"] == [{
        "path": "pom.xml", "manifest": "maven", "locator": "parent"}]
    netty = by_name["io.netty:netty-codec-http"]
    assert netty["version"] == "4.1.111.Final"  # resolved from the property
    assert netty["found_in"] == [{
        "path": "pom.xml", "manifest": "maven",
        "locator": "dependency:io.netty:netty-codec-http"}]
    assert "com.fasterxml.jackson:jackson-bom" in by_name  # depMgmt kept
    # versionless (managed) and test-scoped deps are skipped
    assert "org.springframework.boot:spring-boot-starter-web" not in by_name
    assert "junit:junit" not in by_name
    props = {r["name"]: r["version"] for r in records if r["kind"] == "property"}
    assert props == {"java.version": "17", "tomcat.version": "10.1.54",
                     "netty.version": "4.1.111.Final",
                     "logback.version": "1.5.6"}
    prop_rec = [r for r in records
                if r["kind"] == "property" and r["name"] == "java.version"][0]
    assert prop_rec["found_in"][0]["locator"] == "property:java.version"


def test_parse_pom_records_plain_and_broken():
    records, warnings = gc.parse_pom_records(
        FIX / "samples" / "plain_pom.xml", "samples/plain_pom.xml")
    assert warnings == []
    deps = [r for r in records if r["kind"] == "dependency"]
    assert [(r["group"], r["artifact"], r["version"]) for r in deps] == [
        ("org.example", "plain-lib", "0.9.0")]
    # release.version has no mapping, so no property record for it
    assert [r for r in records if r["kind"] == "property"] == []
    records2, warnings2 = gc.parse_pom_records(
        FIX / "samples" / "broken_pom.xml", "samples/broken_pom.xml")
    assert records2 == []
    assert len(warnings2) == 1
    assert warnings2[0]["category"] == "parse_error"
    assert warnings2[0]["path"] == "samples/broken_pom.xml"


def test_pom_rejects_dtd_and_entities():
    with tempfile.TemporaryDirectory() as td:
        pom = Path(td) / "pom.xml"
        hostile = (
            '<!DOCTYPE project [<!ENTITY boom "expanded">]>'
            '<project><groupId>&boom;</groupId></project>')
        for encoding in ("utf-8", "utf-16", "utf-32"):
            pom.write_text(hostile, encoding=encoding)
            records, warnings = gc.parse_pom_records(pom, "pom.xml")
            assert records == [], encoding
            assert len(warnings) == 1, encoding
            assert warnings[0]["category"] == "parse_error", encoding
            assert "forbidden DTD/entity" in warnings[0]["message"], encoding

        pom.write_text("<project />", encoding="utf-16")
        records, warnings = gc.parse_pom_records(pom, "pom.xml")
        assert records == [] and warnings == []

        for encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            pom.write_bytes(hostile.encode(encoding))
            records, warnings = gc.parse_pom_records(pom, "pom.xml")
            assert records == [], encoding
            assert len(warnings) == 1, encoding
            assert "forbidden DTD/entity" in warnings[0]["message"], encoding


def test_parse_pom_records_unresolved():
    records, warnings = gc.parse_pom_records(
        FIX / "samples" / "pom_unresolved.xml", "samples/pom_unresolved.xml")
    by_name = {r["name"]: r for r in records if r["kind"] != "property"}
    lib = by_name["org.acme:lib"]
    assert lib["version"] is None and lib["version_spec"] == "${lib.version}"
    parent = by_name["com.example:parent-pom"]
    assert parent["version"] is None
    assert parent["version_spec"] == "${parent.rev}"
    # versionless (managed elsewhere) dep never becomes a record
    assert "org.acme:managed" not in by_name
    categories = [w["category"] for w in warnings]
    assert categories == ["unresolved_version", "unresolved_version"]
    assert all(w["path"] == "samples/pom_unresolved.xml" for w in warnings)


def test_pom_coordinate_properties_resolve_or_remain_untracked():
    with tempfile.TemporaryDirectory() as td:
        pom = Path(td) / "pom.xml"
        pom.write_text(
            "<project><properties><dep.group>org.example</dep.group>"
            "<dep.artifact>resolved</dep.artifact></properties><dependencies>"
            "<dependency><groupId>${dep.group}</groupId>"
            "<artifactId>${dep.artifact}</artifactId><version>1.2.3</version>"
            "</dependency><dependency><groupId>${missing.group}</groupId>"
            "<artifactId>unresolved</artifactId><version>2.0.0</version>"
            "</dependency></dependencies></project>", encoding="utf-8")
        records, warnings = gc.parse_pom_records(pom, "pom.xml")

    resolved = next(r for r in records if r["artifact"] == "resolved")
    assert resolved["group"] == "org.example" and resolved["version"] == "1.2.3"
    unresolved = next(r for r in records if r["artifact"] == "unresolved")
    assert unresolved["version"] is None
    assert unresolved["version_spec"] == "2.0.0"
    assert any(w["category"] == "unresolved_identifier" for w in warnings)


def test_parse_gradle_records():
    records, warnings = gc.parse_gradle_records(
        FIX / "gradle" / "build.gradle", "gradle/build.gradle")
    assert warnings == []
    assert [(r["group"], r["artifact"], r["version"], r["found_in"][0]["line"])
            for r in records] == [
        ("org.apache.commons", "commons-text", "1.11.0", 10),
        ("io.netty", "netty-codec-http", "4.1.111.Final", 11),
        ("org.jetbrains.kotlin", "kotlin-stdlib", "2.0.0", 12),
        ("com.h2database", "h2", "2.2.224", 13),
        ("org.acme", "single-quoted", "1.0.0", 15),
    ]
    assert all(r["found_in"][0]["manifest"] == "gradle" for r in records)
    assert all(r["found_in"][0]["path"] == "gradle/build.gradle"
               for r in records)
    kts, warnings_kts = gc.parse_gradle_records(
        FIX / "gradle" / "build.gradle.kts", "gradle/build.gradle.kts")
    assert warnings_kts == []
    assert [(r["artifact"], r["version"], r["found_in"][0]["line"])
            for r in kts] == [
        ("kotlinx-coroutines-core", "1.8.1", 6),
        ("spring-boot-gradle-plugin", "3.3.4", 8),
        ("guava", "33.2.1-jre", 7),   # named form parsed in its own pass
    ]
    missing_rec, missing_warn = gc.parse_gradle_records(
        FIX / "gradle" / "nope.gradle", "gradle/nope.gradle")
    assert missing_rec == []
    assert missing_warn[0]["category"] == "unreadable_file"

    with tempfile.TemporaryDirectory() as td:
        commented = Path(td) / "comments.gradle"
        commented.write_text(
            '// implementation("org.example:line-comment:9.9.9")\n'
            '/* api group: "org.example", name: "block-comment", '
            'version: "9.9.9" */\n'
            'implementation("org.example:live:1.2.3")\n',
            encoding="utf-8")
        comment_records, comment_warnings = gc.parse_gradle_records(
            commented, "comments.gradle")
        assert comment_warnings == []
        assert [(r["artifact"], r["version"]) for r in comment_records] == [
            ("live", "1.2.3")]

        dynamic = Path(td) / "build.gradle.kts"
        dynamic.write_text(
            'dependencies {\n'
            '  implementation("org.example:short:$version")\n'
            '  implementation("org.example:braced:${versions.long}")\n'
            '  implementation("org.example:dotted:$versions.long")\n'
            '  implementation("org.example:unicode:$\u03c0")\n'
            "  implementation('org.example:single:$version')\n"
            '  implementation("org.example:escaped:\\$version")\n'
            '  implementation(group = "org.example", name = "named", '
            'version = "$\u7248\u672c")\n'
            '  implementation("org.example:range:[1.0,2.0)")\n'
            '  implementation("org.example:floating:1.+")\n'
            '  implementation("org.example:latest:latest.integration")\n'
            "  implementation('org.example:latest-single:latest.release')\n"
            '  implementation("org.example:malformed:1..0")\n'
            '  implementation("org.example:trailing-dot:1.")\n'
            '  implementation("org.example:trailing-dash:1.0-")\n'
            '  implementation("org.example:adjacent-separators:1+-2")\n'
            '  implementation("org.example:build:1.0.0+build.1")\n'
            '  implementation(group = "org.example", name = "latest-named", '
            'version = "latest.milestone")\n'
            '}\n', encoding="utf-8")
        dynamic_records, dynamic_warnings = gc.parse_gradle_records(
            dynamic, "build.gradle.kts")
        dynamic_scan = gc.scan_folder(td)
    assert [(r["artifact"], r["version"], r["version_spec"])
            for r in dynamic_records] == [
        ("short", None, "$version"),
        ("braced", None, "${versions.long}"),
        ("dotted", None, "$versions.long"),
        ("unicode", None, "$\u03c0"),
        ("single", None, "$version"),
        ("escaped", None, "\\$version"),
        ("range", None, "[1.0,2.0)"),
        ("floating", None, "1.+"),
        ("latest", None, "latest.integration"),
        ("latest-single", None, "latest.release"),
        ("malformed", None, "1..0"),
        ("trailing-dot", None, "1."),
        ("trailing-dash", None, "1.0-"),
        ("adjacent-separators", None, "1+-2"),
        ("build", "1.0.0+build.1", None),
        ("named", None, "$\u7248\u672c"),
        ("latest-named", None, "latest.milestone"),
    ]
    assert len(dynamic_warnings) == 16
    assert all(w["category"] == "unresolved_version"
               for w in dynamic_warnings)
    dynamic_config = gc.generate_config(dynamic_scan, "dynamic")
    assert not [p for p in _products(dynamic_config)
                if p.get("artifact") in {
                    "short", "braced", "dotted", "unicode", "single",
                    "escaped", "named", "range", "floating", "latest",
                    "latest-single", "latest-named", "malformed",
                    "trailing-dot", "trailing-dash", "adjacent-separators"}
                and p.get("source") == "maven_central"]
    assert len([p for p in _products(dynamic_config)
                if p.get("artifact") == "build"
                and p.get("version") == "1.0.0+build.1"]) == 1
    assert [(item["name"], item["version_spec"])
            for item in dynamic_config["_inventory"]["unmapped"]] == [
        ("org.example:adjacent-separators", "1+-2"),
        ("org.example:braced", "${versions.long}"),
        ("org.example:dotted", "$versions.long"),
        ("org.example:escaped", "\\$version"),
        ("org.example:floating", "1.+"),
        ("org.example:latest", "latest.integration"),
        ("org.example:latest-named", "latest.milestone"),
        ("org.example:latest-single", "latest.release"),
        ("org.example:malformed", "1..0"),
        ("org.example:named", "$\u7248\u672c"),
        ("org.example:range", "[1.0,2.0)"),
        ("org.example:short", "$version"),
        ("org.example:single", "$version"),
        ("org.example:trailing-dash", "1.0-"),
        ("org.example:trailing-dot", "1."),
        ("org.example:unicode", "$\u03c0"),
    ]

    with tempfile.TemporaryDirectory() as td:
        pom = Path(td) / "pom.xml"
        pom.write_text(
            '<project><modelVersion>4.0.0</modelVersion><dependencies>'
            '<dependency><groupId>org.example</groupId>'
            '<artifactId>range</artifactId><version>[1.0,2.0)</version>'
            '</dependency></dependencies></project>', encoding="utf-8")
        pom_records, pom_warnings = gc.parse_pom_records(pom, "pom.xml")
        pom_config = gc.generate_config(gc.scan_folder(td), "dynamic-pom")
    assert pom_records[0]["version"] is None
    assert pom_records[0]["version_spec"] == "[1.0,2.0)"
    assert len(pom_warnings) == 1
    assert not [product for product in _products(pom_config)
                if product.get("source") == "maven_central"]


def test_parse_package_json_records():
    records, warnings = gc.parse_package_json_records(
        FIX / "node" / "package.json", "package.json")
    by_name = {r["name"]: r for r in records}
    node = by_name["node"]
    assert node["kind"] == "runtime" and node["version"] is None
    assert node["version_spec"] == ">=18 <21"
    assert node["found_in"] == [{
        "path": "package.json", "manifest": "npm", "locator": "engines.node"}]
    react = by_name["react"]
    assert react["version"] == "18.2.0" and react["scope"] == "runtime"
    assert react["found_in"][0]["locator"] == "dependencies.react"
    ts = by_name["typescript"]
    assert ts["version"] == "5.4.5" and ts["scope"] == "dev"
    assert ts["found_in"][0]["locator"] == "devDependencies.typescript"
    assert "@company/tokens" in by_name
    assert len(warnings) == 1
    assert warnings[0]["category"] == "unresolved_version"
    missing_rec, missing_warn = gc.parse_package_json_records(
        FIX / "node" / "missing.json", "missing.json")
    assert missing_rec == []
    assert missing_warn[0]["category"] == "parse_error"


# ---------------------------------------------------------------------------
# Deterministic discovery and exclusions
# ---------------------------------------------------------------------------

def test_scan_folder_maven_multi():
    scan = _scan("maven_multi")
    assert scan["root_name"] == "maven_multi"
    assert scan["files"] == ["pom.xml", "service/pom.xml"]
    # 8 root-POM records + 4 property records + 3 service-POM records
    assert len(scan["records"]) == 15
    assert scan["warnings"] == []
    first = scan["records"][0]
    assert first["name"] == "org.springframework.boot:spring-boot-starter-parent"


def test_scan_folder_mixed():
    scan = _scan("mixed")
    assert scan["files"] == [
        "build.gradle", "package-lock.json", "package.json", "pom.xml"]
    # maven records precede gradle records, which precede node records
    ecosystems = [(r["ecosystem"], r["kind"]) for r in scan["records"]]
    assert ecosystems == [
        ("java", "parent"), ("java", "dependency"), ("java", "dependency"),
        ("java", "property"),
        ("java", "dependency"), ("java", "dependency"), ("java", "dependency"),
        ("node", "runtime"), ("node", "dependency"), ("node", "dependency"),
    ]
    assert len(scan["warnings"]) == 1
    assert scan["warnings"][0]["category"] == "unresolved_version"


def test_scan_folder_skips_node_modules():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "node_modules" / "left-pad").mkdir(parents=True)
        (root / "node_modules" / "left-pad" / "package.json").write_text(
            json.dumps({"name": "left-pad", "version": "1.3.0"}))
        (root / "package.json").write_text(json.dumps(
            {"name": "app", "dependencies": {"react": "^18.2.0"}}))
        scan = gc.scan_folder(str(root))
        assert scan["files"] == ["package.json"]
        assert [r["name"] for r in scan["records"]] == ["react"]


def test_scan_folder_default_exclusions():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pom.xml").write_text(
            "<project><dependencies><dependency>"
            "<groupId>org.acme</groupId><artifactId>keep</artifactId>"
            "<version>1.0.0</version></dependency></dependencies></project>")
        for d in ("node_modules", "target", ".git", "venv", ".venv",
                  "vendor", "bin", "obj", "dist", "build"):
            (root / d).mkdir()
            (root / d / "pom.xml").write_text(
                "<project><dependencies><dependency>"
                f"<groupId>org.acme</groupId><artifactId>{d}-dep</artifactId>"
                "<version>1.0.0</version></dependency></dependencies></project>")
        scan = gc.scan_folder(str(root))
        assert scan["files"] == ["pom.xml"]
        assert [r["artifact"] for r in scan["records"]] == ["keep"]
        assert scan["warnings"] == []


def test_scan_folder_eolignore_and_exclude():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".eolignore").write_text("# generated output\nlegacy\n")
        (root / "pom.xml").write_text(_mini_pom("keep"))
        (root / "legacy").mkdir()
        (root / "legacy" / "pom.xml").write_text(_mini_pom("legacy-dep"))
        (root / "skipme").mkdir()
        (root / "skipme" / "pom.xml").write_text(_mini_pom("skipme-dep"))
        (root / "extra" / "nested").mkdir(parents=True)
        (root / "extra" / "nested" / "pom.xml").write_text(
            _mini_pom("nested-dep"))
        scan = gc.scan_folder(str(root), exclude=["skipme", "extra/*"])
        assert scan["files"] == ["pom.xml"]
        assert [r["artifact"] for r in scan["records"]] == ["keep"]


def test_scan_folder_skips_escaping_symlinks():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        (root / "pom.xml").write_text(_mini_pom("keep"))
        outside = base / "outside"
        outside.mkdir()
        (outside / "pom_outside.xml").write_text(_mini_pom("outside-dep"))
        try:
            os.symlink(outside / "pom_outside.xml", root / "pom_link.xml")
            os.symlink(outside, root / "dirlink", target_is_directory=True)
        except (OSError, NotImplementedError):
            print("skip symlink tests (unsupported on this platform)")
            return
        scan = gc.scan_folder(str(root))
        # Neither the escaping file symlink nor the directory symlink is followed
        assert scan["files"] == ["pom.xml"]
        assert [r["artifact"] for r in scan["records"]] == ["keep"]
        escaped = [w for w in scan["warnings"]
                   if w["category"] == "escaped_symlink"]
        assert {warning["path"] for warning in escaped} == {
            "dirlink", "pom_link.xml"}


def test_windows_junctions_are_classified_as_directory_links():
    class JunctionPath:
        @staticmethod
        def is_symlink():
            return False

        @staticmethod
        def is_junction():
            return True

    assert discovery_module._is_directory_link(JunctionPath())

    reparse_flag = getattr(
        discovery_module.stat, "FILE_ATTRIBUTE_REPARSE_POINT", None)
    if reparse_flag is None:
        return

    class ReparseStat:
        st_file_attributes = reparse_flag

    class LegacyJunctionPath:
        @staticmethod
        def is_symlink():
            return False

        @staticmethod
        def lstat():
            return ReparseStat()

    assert discovery_module._is_directory_link(LegacyJunctionPath())


def test_scan_folder_oversize_warning():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        big = root / "pom.xml"
        big.write_bytes(b"x" * (gc.MAX_FILE_BYTES + 1))
        (root / "package.json").write_text(json.dumps(
            {"name": "app", "dependencies": {"react": "^18.2.0"}}))
        scan = gc.scan_folder(str(root))
        assert scan["files"] == ["package.json"]
        oversize = [w for w in scan["warnings"]
                    if w["category"] == "oversize_input"]
        assert len(oversize) == 1 and oversize[0]["path"] == "pom.xml"


def test_scan_folder_refuses_huge_file_count():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for i in range(4):
            (root / f"pom{i}.xml").write_text(_mini_pom(f"dep{i}"))
        original = discovery_module.MAX_FILES
        discovery_module.MAX_FILES = 3
        try:
            gc.scan_folder(str(root))
        except SystemExit:
            pass
        else:
            raise AssertionError("expected SystemExit above MAX_FILES")
        finally:
            discovery_module.MAX_FILES = original


def _mini_pom(artifact):
    return ("<project><dependencies><dependency>"
            f"<groupId>org.acme</groupId><artifactId>{artifact}</artifactId>"
            "<version>1.0.0</version></dependency></dependencies></project>")


def test_scan_folder_not_a_directory():
    try:
        gc.scan_folder(str(FIX / "does" / "not" / "exist"))
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit for missing folder")


def test_scan_discovers_generic_gradle_nvmrc_and_isolates_bad_manifest():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "dependencies.gradle").write_text(
            "implementation 'org.example:lib:1.2.3'\n", encoding="utf-8")
        (root / ".nvmrc").write_text("v20.15.1\n", encoding="utf-8")
        (root / "package.json").write_text(
            '{"engines":{"node":{"bad":true}}}', encoding="utf-8")
        scan = gc.scan_folder(root)
    assert scan["files"] == [".nvmrc", "dependencies.gradle", "package.json"]
    assert any(r["name"] == "org.example:lib" for r in scan["records"])
    assert any(r["name"] == "node" and r["version"] == "20.15.1"
               for r in scan["records"])
    assert any(w["path"] == "package.json" and w["category"] == "parse_error"
               for w in scan["warnings"])


def test_scan_folder_deduplicates_shared_include_graphs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "shared.txt").write_text(
            "requests==2.32.4\n", encoding="utf-8")
        for suffix in ("a", "b"):
            (root / f"requirements-{suffix}.txt").write_text(
                "-r shared.txt\n", encoding="utf-8")
        (root / ".gitlab").mkdir()
        (root / ".gitlab" / "shared.yml").write_text(
            "image: python:3.12\n", encoding="utf-8")
        (root / ".gitlab-ci.yml").write_text(
            "include:\n  - .gitlab/shared.yml\n", encoding="utf-8")
        scan = gc.scan_folder(td)

    assert len([r for r in scan["records"]
                if r["ecosystem"] == "python"
                and r["name"] == "requests"]) == 1
    assert len([r for r in scan["records"]
                if r["ecosystem"] == "container"
                and r["name"] == "python"]) == 1
    assert scan["warnings"] == []


def test_scan_folder_enforces_scanner_wide_include_budgets():
    real_python_limit = python_parser.MAX_FILES
    real_gitlab_limit = gitlab_parser.MAX_FILES
    try:
        python_parser.MAX_FILES = 2
        gitlab_parser.MAX_FILES = 2
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements-a.txt").write_text(
                "-r python-a.txt\n", encoding="utf-8")
            (root / "python-a.txt").write_text(
                "alpha==1.0.0\n", encoding="utf-8")
            (root / "requirements-b.txt").write_text(
                "beta==1.0.0\n", encoding="utf-8")
            (root / ".gitlab").mkdir()
            (root / ".gitlab" / "a.yml").write_text(
                "image: python:3.12\n", encoding="utf-8")
            (root / ".gitlab" / "b.yml").write_text(
                "image: nginx:1.27\n", encoding="utf-8")
            (root / ".gitlab-ci.yml").write_text(
                "include:\n  - .gitlab/a.yml\n  - .gitlab/b.yml\n",
                encoding="utf-8")
            scan = gc.scan_folder(td)
    finally:
        python_parser.MAX_FILES = real_python_limit
        gitlab_parser.MAX_FILES = real_gitlab_limit

    assert [r["name"] for r in scan["records"]
            if r["ecosystem"] == "python"] == ["alpha"]
    assert [r["name"] for r in scan["records"]
            if r["ecosystem"] == "container"] == ["python"]
    assert any(w["category"] == "include_limit" for w in scan["warnings"])
    assert any(w["category"] == "ci_include_limit" for w in scan["warnings"])


# ---------------------------------------------------------------------------
# Config generation: sections, mapping, dedup, provenance, inventory
# ---------------------------------------------------------------------------

def test_generate_config_maven_multi():
    config = gc.generate_config(_scan("maven_multi"), "maven-multi")
    assert _sections(config) == [
        "=== Platforms (from POM properties) ===",
        "=== Java dependencies ===",
        "=== Inferred from Spring Boot release train ===",
        "=== Needs Manual Review ===",
    ]
    assert config["alert_thresholds_days"] == [30, 60, 90]
    assert config["notify_when"] == "always"
    assert config["notifications"][0] == {"type": "console"}
    assert any(n["type"] == "html_file" for n in config["notifications"])
    assert "maven-multi" in config["_comment"][0]

    prods = _products(config)
    # Platforms from POM properties, in mapping-table order, with provenance
    assert prods[0] == {
        "product": "amazon-corretto", "version": "17",
        "label": "Amazon Corretto (OpenJDK) 17",
        "_comment": "From pom.xml (<java.version>17</java.version>)",
        "_found_in": [{"path": "pom.xml", "manifest": "maven",
                       "locator": "property:java.version"}]}
    assert prods[1]["product"] == "tomcat" and prods[1]["version"] == "10.1"
    assert prods[2] == {
        "source": "maven_central", "group": "io.netty",
        "artifact": "netty-codec-http", "version": "4.1.111.Final",
        "label": "Netty Codec HTTP 4.1.111.Final",
        "_comment": "From pom.xml (<netty.version>4.1.111.Final</netty.version>)",
        "_found_in": [
            {"path": "pom.xml", "manifest": "maven",
             "locator": "dependency:io.netty:netty-codec-http"},
            {"path": "pom.xml", "manifest": "maven",
             "locator": "property:netty.version"},
            {"path": "service/pom.xml", "manifest": "maven",
             "locator": "dependency:io.netty:netty-codec-http"},
        ]}
    assert prods[3]["artifact"] == "logback-classic"

    java = prods[4:]
    # parent dependency maps to spring-boot with major.minor cycle
    assert java[0]["product"] == "spring-boot" and java[0]["version"] == "3.3"
    assert java[0]["_comment"] == (
        "From pom.xml (org.springframework.boot:spring-boot-starter-parent:3.3.4)")
    # jackson-bom and jackson-databind collapse to one jackson_lifecycle entry
    # whose _found_in merges both declaration sites
    jacksons = [p for p in java if p.get("source") == "jackson_lifecycle"]
    assert len(jacksons) == 1 and jacksons[0]["version"] == "2.17"
    assert jacksons[0]["_comment"] == (
        "From pom.xml (com.fasterxml.jackson:jackson-bom:2.17.2)")
    assert {loc["locator"] for loc in jacksons[0]["_found_in"]} == {
        "dependency:com.fasterxml.jackson:jackson-bom",
        "dependency:com.fasterxml.jackson.core:jackson-databind"}
    # the netty dependency duplicates the property-driven entry and is dropped
    assert all(p.get("group") != "io.netty" for p in java)
    # full Maven version retained on the maven_central fallback
    lang3 = [p for p in java if p.get("artifact") == "commons-lang3"]
    assert lang3 and lang3[0]["version"] == "3.14.0"
    assert lang3[0]["_comment"] == (
        "From pom.xml (org.apache.commons:commons-lang3:3.14.0)")
    assert lang3[0]["_found_in"] == [{
        "path": "service/pom.xml", "manifest": "maven",
        "locator": "dependency:org.apache.commons:commons-lang3"}]
    # webjar maps with major-only cycle; full version only in the label
    boot_webjar = [p for p in java if p.get("product") == "bootstrap"]
    assert boot_webjar and boot_webjar[0]["version"] == "5"
    assert "(using 5.3.3)" in boot_webjar[0]["label"]
    # child-POM parent coordinate falls back to maven_central
    mm = [p for p in java if p.get("artifact") == "maven-multi"]
    assert mm and mm[0]["group"] == "com.example" and mm[0]["version"] == "1.0.0"
    assert mm[0]["_found_in"][0]["locator"] == "parent"
    # snapshot, internal-group, and webjar-junk deps never become products
    arts = {p.get("artifact") for p in java}
    assert "inflight" not in arts
    assert "internal-lib" not in arts
    assert "chart.js" not in arts
    # spring-security inferred exactly once from the Boot release train
    sec = [p for p in prods if p.get("product") == "spring-security"]
    assert len(sec) == 1 and sec[0]["version"] == "6.3"
    assert "Auto-derived from Spring Boot 3.3" in sec[0]["_comment"]
    assert "_found_in" not in sec[0]  # derived, not declared

    assert not config.get("_skipped_npm_packages")
    # inventory metadata
    inv = config["_inventory"]
    assert inv["schema_version"] == 1
    assert inv["generator_version"] == gc.GENERATOR_VERSION
    assert inv["scan_root"] == "maven_multi"
    assert inv["manifests"] == ["pom.xml", "service/pom.xml"]
    assert inv["warnings"] == []
    assert inv["summary"] == {"files": 2, "records": 15, "products": 10,
                              "unmapped": 3, "warnings": 0, "indirect": 0}
    # unmapped items keep their declaration sites and explicit reasons
    assert [(u["name"], u["reason"]) for u in inv["unmapped"]] == [
        ("com.example:inflight",
         "SNAPSHOT build resolves on no public registry"),
        ("internal.tools:internal-lib",
         "internal coordinate prefix resolves on no public registry"),
        ("org.webjars.npm:chart.js",
         "webjar without useful upstream lifecycle data"),
    ]
    assert all(u["ecosystem"] == "java" and u["found_in"]
               for u in inv["unmapped"])


def test_inventory_metadata_carries_scan_date_and_consumed_manifests():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps(
            {"name": "app", "dependencies": {"left-pad": "1.3.0"}}))
        (root / "package-lock.json").write_text(json.dumps(
            {"lockfileVersion": 3, "packages": {}}))
        config = gc.generate_config(gc.scan_folder(root), "stamp")
    inv = config["_inventory"]
    # The plan requires a deterministic scan date, not a wall-clock
    # timestamp: identical inputs must produce identical configs.
    assert "scan_timestamp" not in inv
    assert inv["scan_date"] == date.today().isoformat()
    # The consumed-but-unused sibling lock joins the manifest list.
    assert inv["manifests"] == ["package-lock.json", "package.json"]
    assert inv["summary"]["files"] == 2


def test_snapshot_properties_stay_unmapped():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pom.xml").write_text(
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            '<properties><java.version>21-SNAPSHOT</java.version>'
            '<netty.version>4.2.0-snapshot</netty.version></properties>'
            '</project>', encoding="utf-8")
        config = gc.generate_config(gc.scan_folder(root), "snapshots")

    assert not [p for p in _products(config)
                if p.get("product") == "amazon-corretto"
                or p.get("artifact") == "netty-codec-http"]
    assert [(item["name"], item["reason"])
            for item in config["_inventory"]["unmapped"]] == [
        ("java.version", "SNAPSHOT build resolves on no public registry"),
        ("netty.version", "SNAPSHOT build resolves on no public registry"),
    ]


def test_generate_config_gradle():
    config = gc.generate_config(_scan("gradle"), "gradle-project")
    assert _sections(config) == [
        "=== Java dependencies ===",
        "=== Inferred from Spring Boot release train ===",
    ]
    prods = _products(config)
    # Files are scanned in deterministic ecosystem-precedence then relative
    # path order (build.gradle before build.gradle.kts), so commons-text
    # appears first and maps to the kotlin 2.0 lifecycle entry before the
    # kotlinx 1.8 declaration
    assert [(p.get("product") or p.get("artifact"), p.get("version"))
            for p in prods] == [
        ("commons-text", "1.11.0"),
        ("netty-codec-http", "4.1.111.Final"),
        ("kotlin", "2.0"),          # kotlin-stdlib
        ("h2", "2.2.224"),
        ("single-quoted", "1.0.0"),
        ("kotlin", "1.8"),          # kotlinx-coroutines-core — distinct version, own entry
        ("spring-boot", "3.3"),     # spring-boot-gradle-plugin via classpath
        ("guava", "33.2.1-jre"),    # named form in the kts file
        ("spring-security", "6.3"),
    ]
    guava = [p for p in prods if p.get("artifact") == "guava"]
    assert guava[0]["_comment"] == (
        "From build.gradle.kts (com.google.guava:guava:33.2.1-jre)")
    assert guava[0]["_found_in"] == [{
        "path": "build.gradle.kts", "manifest": "gradle", "line": 7,
        "locator": "dependency:com.google.guava:guava"}]
    boot = [p for p in prods if p.get("product") == "spring-boot"]
    assert boot[0]["_comment"] == (
        "From build.gradle.kts (org.springframework.boot:spring-boot-gradle-plugin:3.3.4)")
    kotlinx = [p for p in prods if p.get("product") == "kotlin"
               and p.get("version") == "1.8"]
    assert kotlinx[0]["_found_in"] == [{
        "path": "build.gradle.kts", "manifest": "gradle", "line": 6,
        "locator": "dependency:org.jetbrains.kotlinx:kotlinx-coroutines-core"}]
    mc = [(p["group"], p["artifact"], p["version"]) for p in prods
          if p.get("source") == "maven_central"]
    assert ("org.apache.commons", "commons-text", "1.11.0") in mc
    assert ("com.h2database", "h2", "2.2.224") in mc
    # single-quoted Groovy declarations are parsed; testImplementation is not.
    arts = {p.get("artifact") for p in prods}
    assert "single-quoted" in arts and "junit" not in arts


def test_generate_config_keeps_unresolved_mapped_pom_properties_visible():
    with tempfile.TemporaryDirectory() as td:
        pom = Path(td) / "pom.xml"
        pom.write_text(
            '<project><properties><java.version>${jdk.version}</java.version>'
            '</properties></project>', encoding="utf-8")
        config = gc.generate_config(gc.scan_folder(td), "unresolved-pom")

    assert not [p for p in _products(config)
                if p.get("product") == "amazon-corretto"]
    assert _sections(config) == ["=== Needs Manual Review ==="]
    assert config["_inventory"]["unmapped"] == [{
        "ecosystem": "java", "name": "java.version",
        "reason": "no exact version (${jdk.version})",
        "version_spec": "${jdk.version}",
        "found_in": [{
            "path": "pom.xml", "manifest": "maven",
            "locator": "property:java.version"}],
        "scope": "runtime", "direct": True,
    }]
    assert config["_inventory"]["summary"] == {
        "files": 1, "records": 1, "products": 0, "unmapped": 1,
        "warnings": 1, "indirect": 0}


def test_generate_config_node():
    config = gc.generate_config(_scan("node"), "node-project")
    assert _sections(config) == [
        "=== npm dependencies ===", "=== Needs Manual Review ==="]
    prods = _products(config)
    assert not [p for p in prods if p.get("product") == "nodejs"]
    assert [p for p in prods if p.get("product") == "react"] == [{
        "product": "react", "version": "18", "label": "React 18",
        "_comment": "From package.json (react@18.2.0)",
        "_found_in": [
            {"path": "package-lock.json", "manifest": "npm",
             "locator": "lock:react"},
            {"path": "package-lock.json", "manifest": "npm",
             "locator": "lock:react-dom"},
            {"path": "package.json", "manifest": "npm",
             "locator": "dependencies.react"},
            {"path": "package.json", "manifest": "npm",
             "locator": "dependencies.react-dom"}]}]
    ts = [p for p in prods if p.get("product") == "typescript"]
    assert ts and ts[0]["version"] == "5.4"
    # remaining exact direct packages become npm_registry release-recency
    # rows (lock-resolved or pinned), with merged declaration provenance
    axios = [p for p in prods if p.get("package") == "axios"]
    assert axios == [{
        "source": "npm_registry", "package": "axios", "version": "1.6.8",
        "label": "axios 1.6.8",
        "_comment": "From package.json (axios@1.6.8)",
        "_found_in": [
            {"path": "package-lock.json", "manifest": "npm",
             "locator": "lock:axios"},
            {"path": "package.json", "manifest": "npm",
             "locator": "dependencies.axios"}]}]
    left_pad = [p for p in prods if p.get("package") == "left-pad"]
    assert left_pad == [{
        "source": "npm_registry", "package": "left-pad", "version": "1.3.0",
        "label": "left-pad 1.3.0",
        "_comment": "From package.json (left-pad@1.3.0)",
        "_found_in": [{"path": "package.json", "manifest": "npm",
                       "locator": "dependencies.left-pad"}]}]
    tokens = [p for p in prods if p.get("package") == "@company/tokens"]
    assert tokens and tokens[0]["version"] == "1.0.0"
    # react-dom shares React's lifecycle row, with its provenance retained.
    assert all(p.get("package") != "react-dom" for p in prods)
    # every exact package is tracked now: nothing skipped or unmapped
    assert not config.get("_skipped_npm_packages")
    assert config["_inventory"]["unmapped"] == [{
        "ecosystem": "node", "name": "node",
        "reason": "no exact version (>=18 <21)",
        "version_spec": ">=18 <21",
        "found_in": [{"path": "package.json", "manifest": "npm",
                      "locator": "engines.node"}],
        "scope": "runtime",
        "direct": True,
    }]
    assert config["_inventory"]["summary"] == {
        "files": 2, "records": 7, "products": 5, "unmapped": 1,
        "warnings": 1, "indirect": 0}


def test_generate_config_mixed():
    config = gc.generate_config(_scan("mixed"), "mixed")
    assert _sections(config) == [
        "=== Platforms (from POM properties) ===",
        "=== Java dependencies ===",
        "=== npm dependencies ===",
        "=== Inferred from Spring Boot release train ===",
        "=== Needs Manual Review ===",
    ]
    prods = _products(config)
    # netty declared in both pom.xml and build.gradle: one product whose
    # provenance lists both declaration sites, comment from the first seen
    netty = [p for p in prods if p.get("group") == "io.netty"]
    assert len(netty) == 1
    assert netty[0]["_comment"] == (
        "From pom.xml (io.netty:netty-codec-http:4.1.111.Final)")
    # _found_in is sorted by relative path regardless of scan order
    assert [loc["path"] for loc in netty[0]["_found_in"]] == [
        "build.gradle", "pom.xml"]
    guava = [p for p in prods if p.get("artifact") == "guava"]
    assert guava[0]["_comment"] == (
        "From build.gradle (com.google.guava:guava:33.2.1-jre)")
    assert not [p for p in prods if p.get("product") == "nodejs"]
    sec = [p for p in prods if p.get("product") == "spring-security"]
    assert sec and sec[0]["version"] == "6.3"
    # axios has no lifecycle mapping but is lock-resolved to an exact
    # version, so it becomes an npm_registry release-recency row
    axios = [p for p in prods if p.get("package") == "axios"]
    assert axios == [{
        "source": "npm_registry", "package": "axios", "version": "1.7.2",
        "label": "axios 1.7.2",
        "_comment": "From package.json (axios@1.7.2)",
        "_found_in": [
            {"path": "package-lock.json", "manifest": "npm",
             "locator": "lock:axios"},
            {"path": "package.json", "manifest": "npm",
             "locator": "dependencies.axios"}]}]
    assert not config.get("_skipped_npm_packages")
    inv = config["_inventory"]
    assert [(item["name"], item["version_spec"])
            for item in inv["unmapped"]] == [("node", "^20.0.0")]
    assert inv["summary"] == {"files": 4, "records": 10, "products": 9,
                              "unmapped": 1, "warnings": 1, "indirect": 0}


def test_generate_config_no_inference_when_security_explicit():
    boot = new_record("java", "org.springframework.boot:spring-boot-starter-parent",
                      version="3.3.4", kind="parent",
                      group="org.springframework.boot",
                      artifact="spring-boot-starter-parent")
    add_location(boot, "x/pom.xml", "maven", locator="parent")
    sec = new_record("java", "org.springframework.security:spring-security-config",
                     version="6.4.1", group="org.springframework.security",
                     artifact="spring-security-config")
    add_location(sec, "x/pom.xml", "maven",
                 locator="dependency:org.springframework.security:spring-security-config")
    scan = {"root": "/x", "root_name": "x", "files": ["x/pom.xml"],
            "records": [boot, sec], "warnings": []}
    config = gc.generate_config(scan, "explicit-security")
    secs = [p for p in _products(config) if p.get("product") == "spring-security"]
    assert len(secs) == 1 and secs[0]["version"] == "6.4"
    assert not secs[0]["_comment"].startswith("Auto-derived")
    assert "=== Inferred from Spring Boot release train ===" not in _sections(config)


def test_generate_config_entry_key_dedup_merges_provenance():
    rec_a = new_record("java", "io.netty:netty-codec-http",
                       version="4.1.111.Final", group="io.netty",
                       artifact="netty-codec-http")
    add_location(rec_a, "a/pom.xml", "maven",
                 locator="dependency:io.netty:netty-codec-http")
    rec_b = new_record("java", "io.netty:netty-codec-http",
                       version="4.1.111.Final", group="io.netty",
                       artifact="netty-codec-http")
    add_location(rec_b, "b/pom.xml", "maven",
                 locator="dependency:io.netty:netty-codec-http")
    scan = {"root": "/", "root_name": "root",
            "files": ["a/pom.xml", "b/pom.xml"],
            "records": [rec_a, rec_b], "warnings": []}
    config = gc.generate_config(scan, "dedup")
    prods = _products(config)
    assert len(prods) == 1
    assert prods[0]["_comment"] == "From pom.xml (io.netty:netty-codec-http:4.1.111.Final)"
    assert [loc["path"] for loc in prods[0]["_found_in"]] == [
        "a/pom.xml", "b/pom.xml"]


def test_generate_config_deterministic():
    scan1 = _scan("mixed")
    scan2 = _scan("mixed")
    config1 = gc.generate_config(scan1, "mixed")
    config2 = gc.generate_config(scan2, "mixed")
    dump1 = json.dumps(config1, indent=2, ensure_ascii=True)
    dump2 = json.dumps(config2, indent=2, ensure_ascii=True)
    assert dump1 == dump2


def test_warnings_sorted_and_deduplicated():
    warnings = [
        {"category": "parse_error", "path": "b/pom.xml", "message": "z"},
        {"category": "parse_error", "path": "a/pom.xml", "message": "y"},
        {"category": "parse_error", "path": "a/pom.xml", "message": "x"},
        {"category": "parse_error", "path": "a/pom.xml", "message": "x"},  # dup
        {"category": "oversize_input", "path": "c/pom.xml", "message": "big"},
    ]
    assert gc.sort_warnings(warnings) == [
        {"category": "oversize_input", "path": "c/pom.xml", "message": "big"},
        {"category": "parse_error", "path": "a/pom.xml", "message": "x"},
        {"category": "parse_error", "path": "a/pom.xml", "message": "y"},
        {"category": "parse_error", "path": "b/pom.xml", "message": "z"},
    ]


# ---------------------------------------------------------------------------
# CLI safety: overwrite guard, atomic ASCII output, --strict
# ---------------------------------------------------------------------------

def _run_cli(argv):
    return generate_config_main(argv)


def test_cli_refuses_overwrite_without_replace():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps(
            {"name": "app", "dependencies": {"react": "^18.2.0"}}))
        out = root / "out.json"
        out.write_text("keep")
        rc = _run_cli([str(root), "--output", str(out)])
        assert rc == 2
        assert out.read_text() == "keep"
        rc = _run_cli([str(root), "--output", str(out), "--replace"])
        assert rc == 0
        config = json.loads(out.read_text())
        assert config["_inventory"]["scan_root"] == root.name
        # atomic replace leaves no temp files behind
        assert list(root.glob(".eol_config-*")) == []


def test_cli_output_is_ascii_and_deterministic():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps(
            {"name": "app", "dependencies": {"caf\u00e9-pkg": "^1.0.0"}}))
        out = root / "out.json"
        rc = _run_cli([str(root), "--output", str(out), "--replace"])
        assert rc == 0
        raw = out.read_bytes()
        assert all(b < 128 for b in raw)
        first = json.loads(raw.decode("ascii"))
        _run_cli([str(root), "--output", str(out), "--replace"])
        second = json.loads(out.read_bytes().decode("ascii"))
        assert json.dumps(second, indent=2, ensure_ascii=True) == \
            json.dumps(first, indent=2, ensure_ascii=True)
        assert first["_inventory"]["unmapped"][0]["name"] == "café-pkg"


def test_cli_strict_fails_on_warnings():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pom.xml").write_text("<project><unclosed>")
        out = root / "out.json"
        rc = _run_cli([str(root), "--output", str(out), "--replace"])
        assert rc == 0
        rc = _run_cli([str(root), "--output", str(out), "--replace", "--strict"])
        assert rc == 1
        rc = _run_cli([str(root), "--output", str(out), "--replace", "--strict",
                       "--exclude", "pom.xml"])
        assert rc == 0


def test_runtime_ignores_found_in():
    # The Lambda runtime consumes generated configs: underscore-prefixed
    # keys must never leak into provider results. The manual source is
    # checked offline (no network).
    from datetime import date
    from eoltracker.parsers import check_product
    result = check_product(
        {"source": "manual", "label": "Offline Check",
         "_found_in": [{"path": "pom.xml", "manifest": "maven"}]},
        date(2026, 8, 28))
    assert result is not None
    assert "_found_in" not in result
    assert result["label"] == "Offline Check"


def test_transitive_dependencies_are_opt_in():
    py_dep = new_record("python", "urllib3", version="2.2.2",
                        direct=False)
    add_location(py_dep, "Pipfile.lock", "pipfile_lock",
                 locator="default.urllib3")
    go_dep = new_record("go", "golang.org/x/text", version="0.18.0",
                        direct=False)
    add_location(go_dep, "go.mod", "go", locator="require:golang.org/x/text")
    scan = {"root_name": "transitive", "files": ["Pipfile.lock", "go.mod"],
            "records": [py_dep, go_dep], "warnings": []}
    direct = gc.generate_config(scan, "transitive")
    assert _products(direct) == []
    assert direct["_inventory"]["include_transitive"] is False
    complete = gc.generate_config(scan, "transitive", include_transitive=True)
    assert [(p["source"], p.get("package") or p.get("module"))
            for p in _products(complete)] == [
        ("pypi_registry", "urllib3"), ("go_proxy", "golang.org/x/text")]
    assert complete["_inventory"]["include_transitive"] is True


def test_update_merge_preserves_curation_and_unobserved_entries():
    existing = {
        "notify_when": "problems_only",
        "_skipped_npm_packages": [
            {"name": "stale-package", "version": None,
             "source": "package.json"}],
        "products": [
            {"_section": "=== Curated ==="},
            {"source": "pypi_registry", "package": "requests",
             "version": "2.31.0", "label": "Requests",
             "policy_note": "Keep this note", "reference_url": "https://example.test"},
            {"source": "manual", "label": "Internal appliance",
             "version": "7", "note": "Owner maintained"},
        ],
        "_inventory": {"generator_version": "old"},
    }
    generated = {
        "notify_when": "always",
        "_skipped_npm_packages": [
            {"name": "current-package", "version": None,
             "source": "package.json"}],
        "products": [
            {"_section": "=== Python dependencies ==="},
            {"source": "pypi_registry", "package": "requests",
             "version": "2.32.3", "label": "requests 2.32.3",
             "_found_in": [{"path": "requirements.txt", "manifest": "python"}]},
            {"source": "pypi_registry", "package": "flask",
             "version": "3.0.3", "label": "flask 3.0.3"},
        ],
        "_inventory": {"generator_version": "new"},
    }
    merged = _merge_existing_config(existing, generated)
    assert merged["notify_when"] == "problems_only"
    requests = next(p for p in merged["products"] if p.get("package") == "requests")
    assert requests["version"] == "2.32.3"
    assert requests["policy_note"] == "Keep this note"
    assert requests["reference_url"] == "https://example.test"
    assert any(p.get("label") == "Internal appliance" for p in merged["products"])
    assert any(p.get("_section") == "=== Newly Discovered ==="
               for p in merged["products"])
    assert merged["_skipped_npm_packages"] == [{
        "name": "current-package", "version": None,
        "source": "package.json"}]
    assert merged["_inventory"]["update_summary"] == {
        "added": 1, "changed": 1, "unchanged": 0,
        "retained_not_observed": 1}


def test_update_merge_preserves_multiple_versions_and_default_source():
    existing = {"products": [
        {"source": "maven_central", "group": "org.example",
         "artifact": "shared", "version": "1.0", "label": "shared 1"},
        {"source": "maven_central", "group": "org.example",
         "artifact": "shared", "version": "2.0", "label": "shared 2"},
        {"source": "endoflife_date", "product": "python",
         "version": "3.11", "label": "Python"},
    ]}
    generated = {"products": [
        {"source": "maven_central", "group": "org.example",
         "artifact": "shared", "version": "2.0", "label": "shared 2",
         "_found_in": [{"path": "b/pom.xml"}]},
        {"source": "maven_central", "group": "org.example",
         "artifact": "shared", "version": "1.0", "label": "shared 1",
         "_found_in": [{"path": "a/pom.xml"}]},
        {"product": "python", "version": "3.12", "label": "Python 3.12"},
    ], "_inventory": {}}
    merged = _merge_existing_config(existing, generated)
    products = [p for p in merged["products"] if not p.get("_section")]
    shared = [p for p in products if p.get("artifact") == "shared"]
    assert [(p["version"], p["_found_in"][0]["path"]) for p in shared] == [
        ("1.0", "a/pom.xml"), ("2.0", "b/pom.xml")]
    python = next(p for p in products if p.get("product") == "python")
    assert python["version"] == "3.12"
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 2,
        "retained_not_observed": 0}


def test_update_replaces_stale_scanner_mapping_by_provenance():
    location = {
        "path": "package.json", "manifest": "npm",
        "locator": "engines.node"}
    existing = {
        "products": [{
            "product": "nodejs", "version": "18", "label": "Node.js 18",
            "policy_note": "Keep human context",
            "_comment": "From package.json (node@18)",
            "_found_in": [location],
        }],
        "_inventory": {
            "generator_version": "old",
            "unmapped": [{
                "ecosystem": "node", "name": "node",
                "reason": "no exact version (>=18 <21)",
                "found_in": [location],
            }],
        },
    }
    generated = {
        "products": [{
            "source": "manual", "label": "node", "version": ">=18 <21",
            "note": "no exact version (>=18 <21)",
            "_comment": "Untracked node inventory item",
            "_found_in": [location],
            "_inventory_generated": "unmapped",
        }],
        "_inventory": {"generator_version": "new"},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert len(products) == 1
    node = products[0]
    assert node["source"] == "manual"
    assert node["version"] == ">=18 <21"
    assert node["note"] == "no exact version (>=18 <21)"
    assert node["policy_note"] == "Keep human context"
    assert node["_comment"] == "From package.json (node@18)"
    assert node["_found_in"] == [location]
    assert "product" not in node
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 0,
        "retained_not_observed": 0}


def test_update_uses_stable_provenance_and_preserves_unmapped_edits():
    old_location = {
        "path": "Dockerfile", "manifest": "dockerfile", "line": 1,
        "locator": "FROM nginx:latest"}
    new_location = {
        "path": "Dockerfile", "manifest": "dockerfile", "line": 4,
        "locator": "FROM nginx:1.26"}
    existing = {
        "products": [{
            "source": "manual", "label": "nginx", "version": "latest",
            "note": "Curated deployment exception",
            "_comment": "Reviewed by platform team",
            "_found_in": [old_location],
            "_inventory_generated": "unmapped",
        }],
        "_inventory": {"unmapped": [{
            "ecosystem": "container", "name": "nginx",
            "reason": "image has no exact release tag",
            "found_in": [old_location],
        }]},
    }
    generated = {
        "products": [{
            "product": "nginx", "version": "1.26", "label": "nginx 1.26",
            "_comment": "From Dockerfile (nginx:1.26)",
            "_found_in": [new_location],
        }],
        "_inventory": {},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert len(products) == 1
    nginx = products[0]
    assert nginx["product"] == "nginx" and nginx["version"] == "1.26"
    assert nginx["note"] == "Curated deployment exception"
    assert nginx["_comment"] == "Reviewed by platform team"
    assert nginx["_found_in"] == [new_location]
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 0,
        "retained_not_observed": 0}


def test_update_replaces_generated_manual_row_when_now_tracked():
    location = {
        "path": "package.json", "manifest": "npm",
        "locator": "engines.node"}
    existing = {
        "products": [{
            "source": "manual", "label": "node", "version": ">=18 <21",
            "note": "no exact version (>=18 <21)",
            "policy_note": "Keep human context",
            "_comment": "Untracked node inventory item",
            "_found_in": [location],
            "_inventory_generated": "unmapped",
        }],
        "_inventory": {
            "generator_version": "old",
            "unmapped": [{
                "ecosystem": "node", "name": "node",
                "reason": "no exact version (>=18 <21)",
                "found_in": [location],
            }],
        },
    }
    generated = {
        "products": [{
            "product": "nodejs", "version": "20", "label": "Node.js 20",
            "_comment": "From package.json (node@20.0.0)",
            "_found_in": [location],
        }],
        "_inventory": {"generator_version": "new"},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert len(products) == 1
    node = products[0]
    assert node["product"] == "nodejs" and node["version"] == "20"
    assert node["policy_note"] == "Keep human context"
    assert node["_comment"] == "From package.json (node@20.0.0)"
    assert "source" not in node and "note" not in node
    assert "_inventory_generated" not in node
    assert node["_found_in"] == [location]
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 0,
        "retained_not_observed": 0}


def test_update_remaps_distinct_dockerfile_images_by_repository():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Dockerfile").write_text(
            "FROM python:latest\nFROM nginx:latest\n", encoding="utf-8")
        existing = gc.generate_config(gc.scan_folder(str(root)), "demo")
        (root / "Dockerfile").write_text(
            "FROM python:3.13\nFROM nginx:1.26\n", encoding="utf-8")
        generated = gc.generate_config(gc.scan_folder(str(root)), "demo")

        stale = [p for p in _products(existing)
                 if p.get("_inventory_generated") == "unmapped"]
        assert [p["label"] for p in stale] == ["nginx", "python"]
        assert all(p["source"] == "manual" for p in stale)

        merged = _merge_existing_config(existing, generated)
        products = _products(merged)
        assert not any(p.get("source") == "manual" for p in products), products
        assert not any(p.get("_inventory_generated") == "unmapped"
                       for p in products)
        assert not any(p.get("_section") == "=== Newly Discovered ==="
                       for p in merged["products"])
        by_product = {p.get("product"): p for p in products}
        assert by_product["python"]["version"] == "3.13"
        assert by_product["nginx"]["version"] == "1.26"
        assert "note" not in by_product["python"]
        assert "note" not in by_product["nginx"]
        assert by_product["python"]["_found_in"][0]["locator"] == \
            "FROM python:3.13"
        assert merged["_inventory"]["update_summary"] == {
            "added": 0, "changed": 2, "unchanged": 0,
            "retained_not_observed": 0}


def test_dockerfile_from_key_redacts_legacy_credential_locator():
    # R6: a legacy pre-redaction locator keys on the redacted repository,
    # so it matches the fresh redacted row instead of the username, and
    # it cannot collide with a username-named image.
    assert _dockerfile_from_key(
        "FROM ci:token@registry.example.com/app:1.0") == \
        "FROM registry.example.com/app"
    assert _dockerfile_from_key(
        "FROM registry.example.com/app:1.0") == \
        "FROM registry.example.com/app"
    assert _dockerfile_from_key(
        "FROM ci:token@registry.example.com/app:1.0 AS build") == \
        "FROM registry.example.com/app"
    assert _dockerfile_from_key("FROM ci:1.0") == "FROM ci"
    assert _dockerfile_from_key("FROM ci/app:1.0") == "FROM ci/app"
    assert _dockerfile_from_key("FROM") == "FROM"


def test_update_cleanses_legacy_credential_locator_row():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Dockerfile").write_text(
            "FROM ci:token@registry.example.com/app:1.0\n", encoding="utf-8")
        generated = gc.generate_config(gc.scan_folder(str(root)), "demo")
    assert not any("ci:token" in json.dumps(p)
                   for p in _products(generated))
    legacy_locator = "FROM ci:token@registry.example.com/app:1.0"
    existing = {
        "products": [{
            "source": "manual", "label": "ci", "version": "token",
            "note": "image tag provides no endoflife.date cycle",
            "_comment": "Untracked container inventory item",
            "_found_in": [{"path": "Dockerfile", "manifest": "dockerfile",
                           "line": 1, "locator": legacy_locator}],
            "_inventory_generated": "unmapped",
        }],
        "_inventory": {"unmapped": [{
            "ecosystem": "container", "name": "ci", "version": "token",
            "reason": "image tag provides no endoflife.date cycle",
            "found_in": [{"path": "Dockerfile", "manifest": "dockerfile",
                          "line": 1, "locator": legacy_locator}],
        }]},
    }
    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    # The raw-locator row remapped onto the fresh redacted row: cleansed
    # provenance, no duplicate row.
    assert len(products) == 1
    row = products[0]
    assert row["label"] == "registry.example.com/app"
    assert row["version"] == "1.0"
    assert row["_found_in"][0]["locator"] == \
        "FROM registry.example.com/app:1.0"
    assert "ci:token" not in json.dumps(merged)
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 0,
        "retained_not_observed": 0}


def test_update_unmapped_defaults_match_the_right_sibling():
    loc_a = {"path": "Dockerfile", "manifest": "dockerfile", "line": 3,
             "locator": "FROM team/app:1.0"}
    loc_b = {"path": "Dockerfile", "manifest": "dockerfile", "line": 4,
             "locator": "FROM team/app:2.0"}
    existing = {
        "products": [
            {"source": "manual", "label": "app", "version": "1.0",
             "note": "reason one",
             "_comment": "Untracked container inventory item",
             "_found_in": [dict(loc_a)],
             "_inventory_generated": "unmapped"},
            {"source": "manual", "label": "app", "version": "2.0",
             "note": "reason two",
             "_comment": "Untracked container inventory item",
             "_found_in": [dict(loc_b)],
             "_inventory_generated": "unmapped"},
        ],
        "_inventory": {"unmapped": [
            {"ecosystem": "container", "name": "app", "version": "2.0",
             "reason": "reason two", "found_in": [dict(loc_b)]},
            {"ecosystem": "container", "name": "app", "version": "1.0",
             "reason": "reason one", "found_in": [dict(loc_a)]},
        ]},
    }
    generated = {
        "products": [{
            "product": "app", "version": "3.0", "label": "app 3.0",
            "_comment": "From Dockerfile (team/app:3.0)",
            "_found_in": [{"path": "Dockerfile", "manifest": "dockerfile",
                           "line": 3, "locator": "FROM team/app:3.0"}]},
        ],
        "_inventory": {},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    app = next(p for p in products if p.get("product") == "app")
    assert app["version"] == "3.0"
    assert "note" not in app, "wrong sibling's default leaked a stale note"
    assert app["_comment"] == "From Dockerfile (team/app:3.0)"
    retained = [p for p in products if p.get("source") == "manual"]
    assert [p["version"] for p in retained] == ["2.0"]
    assert retained[0]["note"] == "reason two"
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 0,
        "retained_not_observed": 1}


def test_update_legacy_bare_from_key_still_merges_conservatively():
    location = {"path": "Dockerfile", "manifest": "dockerfile",
                "line": 1, "locator": "FROM"}
    existing = {
        "products": [{
            "source": "manual", "label": "nginx", "version": "latest",
            "note": "Unreviewed base image",
            "_comment": "Untracked container inventory item",
            "_found_in": [dict(location)],
            "_inventory_generated": "unmapped",
        }],
        "_inventory": {"unmapped": [{
            "ecosystem": "container", "name": "nginx", "version": "latest",
            "reason": "image tag provides no endoflife.date cycle",
            "found_in": [dict(location)],
        }]},
    }
    generated = {
        "products": [{
            "product": "nginx", "version": "1.26", "label": "nginx 1.26",
            "_comment": "From Dockerfile (nginx:1.26)",
            "_found_in": [{"path": "Dockerfile", "manifest": "dockerfile",
                           "line": 1, "locator": "FROM nginx:1.26"}]},
        ],
        "_inventory": {},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert len(products) == 1
    nginx = products[0]
    assert nginx["product"] == "nginx" and nginx["version"] == "1.26"
    assert nginx["note"] == "Unreviewed base image"
    assert nginx["_comment"] == "From Dockerfile (nginx:1.26)"
    assert nginx["_found_in"][0]["locator"] == "FROM nginx:1.26"
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 0,
        "retained_not_observed": 0}


def test_update_legacy_bare_from_key_never_clobbers_when_ambiguous():
    location = {"path": "Dockerfile", "manifest": "dockerfile",
                "line": 1, "locator": "FROM"}
    existing = {
        "products": [{
            "source": "manual", "label": "base", "version": "latest",
            "note": "Pinned deliberately by platform team",
            "_comment": "Reviewed by platform team",
            "_found_in": [dict(location)],
            "_inventory_generated": "unmapped",
        }],
        "_inventory": {"unmapped": [{
            "ecosystem": "container", "name": "base", "version": "latest",
            "reason": "image tag provides no endoflife.date cycle",
            "found_in": [dict(location)],
        }]},
    }
    generated = {
        "products": [
            {"product": "python", "version": "3.13", "label": "Python 3.13",
             "_found_in": [{"path": "Dockerfile", "manifest": "dockerfile",
                            "line": 1, "locator": "FROM python:3.13"}]},
            {"product": "nginx", "version": "1.26", "label": "nginx 1.26",
             "_found_in": [{"path": "Dockerfile", "manifest": "dockerfile",
                            "line": 2, "locator": "FROM nginx:1.26"}]},
        ],
        "_inventory": {},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    retained = [p for p in products if p.get("source") == "manual"]
    assert len(retained) == 1
    assert retained[0]["note"] == "Pinned deliberately by platform team"
    assert retained[0]["_comment"] == "Reviewed by platform team"
    assert retained[0]["version"] == "latest"
    assert retained[0]["_found_in"] == [dict(location)]
    tracked = {(p.get("product"), p.get("version")) for p in products
               if p.get("source") != "manual"}
    assert tracked == {("python", "3.13"), ("nginx", "1.26")}
    assert merged["_inventory"]["update_summary"] == {
        "added": 2, "changed": 0, "unchanged": 0,
        "retained_not_observed": 1}


def test_update_matches_both_changed_rows_of_one_identity_by_provenance():
    # Sol round-two: two tracked rows sharing one merge identity, both
    # versions changed at distinct provenance sites — each old row must
    # match its own site's fresh row instead of retaining stale
    # duplicates beside fresh additions.
    loc_a = {"path": "a/pom.xml", "manifest": "pom", "line": 3,
             "locator": "shared"}
    loc_b = {"path": "b/pom.xml", "manifest": "pom", "line": 4,
             "locator": "shared"}
    existing = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "label": "shared 1.0.0", "version": "1.0.0",
             "policy_note": "keep me",
             "_comment": "From a/pom.xml",
             "_found_in": [dict(loc_a)]},
            {"source": "maven_central", "product": "shared",
             "label": "shared 2.0.0", "version": "2.0.0",
             "_comment": "From b/pom.xml",
             "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {},
    }
    generated = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "label": "shared 1.1.0", "version": "1.1.0",
             "_comment": "From a/pom.xml",
             "_found_in": [dict(loc_a)]},
            {"source": "maven_central", "product": "shared",
             "label": "shared 2.1.0", "version": "2.1.0",
             "_comment": "From b/pom.xml",
             "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert sorted(p["version"] for p in products) == ["1.1.0", "2.1.0"]
    assert not any(p.get("source") == "manual" for p in products)
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 2, "unchanged": 0,
        "retained_not_observed": 0}
    kept = next(p for p in products if p["version"] == "1.1.0")
    assert kept["policy_note"] == "keep me"
    assert kept["_found_in"][0]["path"] == "a/pom.xml"


def test_update_retains_ambiguous_same_identity_rows():
    # Same identity and identical provenance on both sides: the
    # intersection is ambiguous, so both old rows are retained verbatim
    # and both fresh rows are added — never a guess.
    loc = {"path": "a/pom.xml", "manifest": "pom", "line": 3,
           "locator": "shared"}
    existing = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "label": "shared 1.0.0", "version": "1.0.0",
             "note": "curated one",
             "_comment": "From a/pom.xml",
             "_found_in": [dict(loc)]},
            {"source": "maven_central", "product": "shared",
             "label": "shared 2.0.0", "version": "2.0.0",
             "note": "curated two",
             "_comment": "From a/pom.xml",
             "_found_in": [dict(loc)]},
        ],
        "_inventory": {},
    }
    generated = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "label": "shared 1.1.0", "version": "1.1.0",
             "_comment": "From a/pom.xml",
             "_found_in": [dict(loc)]},
            {"source": "maven_central", "product": "shared",
             "label": "shared 2.1.0", "version": "2.1.0",
             "_comment": "From a/pom.xml",
             "_found_in": [dict(loc)]},
        ],
        "_inventory": {},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    versions = sorted(str(p.get("version")) for p in products)
    assert versions == ["1.0.0", "1.1.0", "2.0.0", "2.1.0"]
    retained = [p for p in products if p.get("version") in
                ("1.0.0", "2.0.0")]
    assert [p["note"] for p in retained] == ["curated one", "curated two"]
    assert merged["_inventory"]["update_summary"] == {
        "added": 2, "changed": 0, "unchanged": 0,
        "retained_not_observed": 2}


def test_update_crossing_partial_upgrade_by_provenance():
    # Sol round-three: exact-version matching must not cross curation
    # between sites when both versions changed across two rows sharing
    # one identity.
    loc_a = {"path": "a/pom.xml", "manifest": "pom", "line": 3,
             "locator": "shared"}
    loc_b = {"path": "b/pom.xml", "manifest": "pom", "line": 4,
             "locator": "shared"}
    existing = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "version": "2.0", "policy_note": "A",
             "_comment": "From a/pom.xml", "_found_in": [dict(loc_a)]},
            {"source": "maven_central", "product": "shared",
             "version": "1.0", "policy_note": "B",
             "_comment": "From b/pom.xml", "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {},
    }
    generated = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "version": "3.0", "_comment": "From a/pom.xml",
             "_found_in": [dict(loc_a)]},
            {"source": "maven_central", "product": "shared",
             "version": "2.0", "_comment": "From b/pom.xml",
             "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {},
    }

    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    by_site = {p["_found_in"][0]["path"]: p for p in products}
    assert by_site["a/pom.xml"]["version"] == "3.0"
    assert by_site["a/pom.xml"]["policy_note"] == "A"
    assert by_site["b/pom.xml"]["version"] == "2.0"
    assert by_site["b/pom.xml"]["policy_note"] == "B"
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 2, "unchanged": 0,
        "retained_not_observed": 0}


def test_update_all_changed_multi_version_by_provenance():
    # Both versions changed at distinct sites: each old row matches its
    # own site's fresh row.
    loc_a = {"path": "a/pom.xml", "manifest": "pom", "line": 3,
             "locator": "shared"}
    loc_b = {"path": "b/pom.xml", "manifest": "pom", "line": 4,
             "locator": "shared"}
    existing = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "version": "1.0.0", "_comment": "From a/pom.xml",
             "_found_in": [dict(loc_a)]},
            {"source": "maven_central", "product": "shared",
             "version": "2.0.0", "_comment": "From b/pom.xml",
             "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {},
    }
    generated = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "version": "1.1.0", "_comment": "From a/pom.xml",
             "_found_in": [dict(loc_a)]},
            {"source": "maven_central", "product": "shared",
             "version": "2.1.0", "_comment": "From b/pom.xml",
             "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {},
    }
    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert sorted(p["version"] for p in products) == ["1.1.0", "2.1.0"]
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 2, "unchanged": 0,
        "retained_not_observed": 0}


def test_update_one_changed_one_unchanged_multi_version():
    loc_a = {"path": "a/pom.xml", "manifest": "pom", "line": 3,
             "locator": "shared"}
    loc_b = {"path": "b/pom.xml", "manifest": "pom", "line": 4,
             "locator": "shared"}
    existing = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "version": "1.0.0", "_comment": "From a/pom.xml",
             "_found_in": [dict(loc_a)]},
            {"source": "maven_central", "product": "shared",
             "version": "2.0.0", "_comment": "From b/pom.xml",
             "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {},
    }
    generated = {
        "products": [
            {"source": "maven_central", "product": "shared",
             "version": "1.1.0", "_comment": "From a/pom.xml",
             "_found_in": [dict(loc_a)]},
            {"source": "maven_central", "product": "shared",
             "version": "2.0.0", "_comment": "From b/pom.xml",
             "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {},
    }
    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert sorted(p["version"] for p in products) == ["1.1.0", "2.0.0"]
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 1,
        "retained_not_observed": 0}


def test_update_nuget_case_insensitive_identity():
    # NuGet package IDs match case-insensitively: a case-only change in
    # the package ID must produce one changed row with curation
    # preserved, not a retained stale row plus a duplicate. The scanner
    # emits the ID in `package` (the canonical key).
    loc = {"path": "x.csproj", "manifest": "csproj", "line": 3,
           "locator": "Newtonsoft.Json"}
    existing = {
        "products": [
            {"source": "nuget_registry", "package": "Newtonsoft.Json",
             "label": "Newtonsoft.Json 13.0.2", "version": "13.0.2",
             "policy_note": "keep", "_comment": "From x.csproj",
             "_found_in": [dict(loc)]},
        ],
        "_inventory": {},
    }
    generated = {
        "products": [
            {"source": "nuget_registry", "package": "newtonsoft.json",
             "label": "newtonsoft.json 13.0.3", "version": "13.0.3",
             "_comment": "From x.csproj",
             "_found_in": [dict(loc)]},
        ],
        "_inventory": {},
    }
    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert len(products) == 1
    assert products[0]["version"] == "13.0.3"
    assert products[0]["package"] == "newtonsoft.json"
    assert products[0]["policy_note"] == "keep"
    assert merged["_inventory"]["update_summary"] == {
        "added": 0, "changed": 1, "unchanged": 0,
        "retained_not_observed": 0}


def test_update_retained_untracked_inventory_visible():
    # A generated-unmapped product retained as unobserved must remain
    # visible as an untracked row (its old unmapped metadata is carried
    # forward), not silently dropped from the report.
    loc = {"path": "d/Dockerfile", "manifest": "dockerfile", "line": 1,
           "locator": "FROM old-dep:1.0"}
    old_unmapped_item = {
        "ecosystem": "container", "name": "old-dep", "version": "1.0",
        "reason": "legacy dependency", "found_in": [dict(loc)],
    }
    existing = {
        "products": [
            {"source": "manual", "label": "old-dep 1.0",
             "product": "old-dep", "version": "1.0",
             "_inventory_generated": "unmapped",
             "note": "legacy dependency",
             "_comment": "Untracked container inventory item",
             "_found_in": [dict(loc)]},
        ],
        "_inventory": {"unmapped": [old_unmapped_item]},
    }
    generated = {
        "products": [],
        "_inventory": {"unmapped": [], "manifests": [],
                       "summary": {"files": 0, "records": 0, "products": 0,
                                   "unmapped": 0, "warnings": 0,
                                   "indirect": 0}},
    }
    merged = _merge_existing_config(existing, generated)
    inv = merged["_inventory"]
    assert isinstance(inv.get("unmapped"), list)
    names = [u.get("name") for u in inv["unmapped"] if isinstance(u, dict)]
    assert "old-dep" in names
    # The retained product row is still in the config (not deleted).
    products = _products(merged)
    assert any(p.get("_inventory_generated") == "unmapped"
               for p in products)
    # No duplicate: the carried-forward item appears once.
    assert names.count("old-dep") == 1


def test_update_observed_unmapped_not_duplicated():
    # A currently observed unmapped row renders once; the carried-
    # forward logic must not duplicate it.
    loc = {"path": "d/Dockerfile", "manifest": "dockerfile", "line": 1,
           "locator": "FROM old-dep:1.0"}
    unmapped_item = {
        "ecosystem": "container", "name": "old-dep", "version": "1.0",
        "reason": "legacy", "found_in": [dict(loc)],
    }
    existing = {
        "products": [
            {"source": "manual", "label": "old-dep 1.0",
             "product": "old-dep", "version": "1.0",
             "_inventory_generated": "unmapped",
             "_comment": "Untracked container inventory item",
             "_found_in": [dict(loc)]},
        ],
        "_inventory": {"unmapped": [dict(unmapped_item)]},
    }
    generated = {
        "products": [],
        "_inventory": {"unmapped": [dict(unmapped_item)],
                       "manifests": [],
                       "summary": {"files": 0, "records": 0, "products": 0,
                                   "unmapped": 1, "warnings": 0,
                                   "indirect": 0}},
    }
    merged = _merge_existing_config(existing, generated)
    inv = merged["_inventory"]
    names = [u.get("name") for u in (inv.get("unmapped") or [])
             if isinstance(u, dict)]
    assert names.count("old-dep") == 1


def test_update_curated_manual_row_remains_product():
    # A curated manual row (not scanner-generated) is a product, not an
    # unmapped metadata entry: the carry-forward must not touch it.
    loc = {"path": "manual.txt", "manifest": "manual", "line": 1,
           "locator": "manual"}
    existing = {
        "products": [
            {"source": "manual", "label": "curated-dep",
             "product": "curated-dep", "policy_note": "reviewed",
             "_comment": "Added by hand", "_found_in": [dict(loc)]},
        ],
        "_inventory": {"unmapped": []},
    }
    generated = {
        "products": [],
        "_inventory": {"unmapped": [], "manifests": [],
                       "summary": {"files": 0, "records": 0, "products": 0,
                                   "unmapped": 0, "warnings": 0,
                                   "indirect": 0}},
    }
    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert len(products) == 1
    assert products[0]["product"] == "curated-dep"
    assert products[0]["policy_note"] == "reviewed"
    assert not merged["_inventory"].get("unmapped")


def test_update_retained_untracked_inventory_visible():
    # A generated-unmapped product retained as unobserved must remain
    # visible as an untracked row (its old unmapped metadata is carried
    # forward), not silently dropped from the report.
    loc = {"path": "d/Dockerfile", "manifest": "dockerfile", "line": 1,
           "locator": "FROM old-dep:1.0"}
    old_unmapped_item = {
        "ecosystem": "container", "name": "old-dep", "version": "1.0",
        "reason": "legacy dependency", "found_in": [dict(loc)],
    }
    existing = {
        "products": [
            {"source": "manual", "label": "old-dep 1.0",
             "product": "old-dep", "version": "1.0",
             "_inventory_generated": "unmapped",
             "note": "legacy dependency",
             "_comment": "Untracked container inventory item",
             "_found_in": [dict(loc)]},
        ],
        "_inventory": {"unmapped": [old_unmapped_item]},
    }
    generated = {
        "products": [],
        "_inventory": {"unmapped": [], "manifests": [],
                       "summary": {"files": 0, "records": 0, "products": 0,
                                   "unmapped": 0, "warnings": 0,
                                   "indirect": 0}},
    }
    merged = _merge_existing_config(existing, generated)
    inv = merged["_inventory"]
    assert isinstance(inv.get("unmapped"), list)
    names = [u.get("name") for u in inv["unmapped"] if isinstance(u, dict)]
    assert "old-dep" in names
    # The retained product row is still in the config (not deleted).
    products = _products(merged)
    assert any(p.get("_inventory_generated") == "unmapped"
               for p in products)
    # No duplicate: the carried-forward item appears once.
    assert names.count("old-dep") == 1
    # The carried-forward item preserves its reason and provenance.
    item = next(u for u in inv["unmapped"]
                if isinstance(u, dict) and u.get("name") == "old-dep")
    assert item["reason"] == "legacy dependency"
    assert item["found_in"][0]["path"] == "d/Dockerfile"


def test_update_same_name_distinct_sites_both_carried():
    # Two same-name retained generated-unmapped rows at distinct sites
    # each carry their own unmapped metadata (no cross-borrowing).
    loc_a = {"path": "a/Dockerfile", "manifest": "dockerfile", "line": 1,
             "locator": "FROM old-dep:1.0"}
    loc_b = {"path": "b/Dockerfile", "manifest": "dockerfile", "line": 1,
             "locator": "FROM old-dep:2.0"}
    existing = {
        "products": [
            {"source": "manual", "label": "old-dep 1.0",
             "product": "old-dep", "version": "1.0",
             "_inventory_generated": "unmapped",
             "note": "site A", "_found_in": [dict(loc_a)]},
            {"source": "manual", "label": "old-dep 2.0",
             "product": "old-dep", "version": "2.0",
             "_inventory_generated": "unmapped",
             "note": "site B", "_found_in": [dict(loc_b)]},
        ],
        "_inventory": {"unmapped": [
            {"ecosystem": "container", "name": "old-dep",
             "version": "1.0", "reason": "site A",
             "found_in": [dict(loc_a)]},
            {"ecosystem": "container", "name": "old-dep",
             "version": "2.0", "reason": "site B",
             "found_in": [dict(loc_b)]},
        ]},
    }
    generated = {
        "products": [],
        "_inventory": {"unmapped": [], "manifests": [],
                       "summary": {"files": 0, "records": 0, "products": 0,
                                   "unmapped": 0, "warnings": 0,
                                   "indirect": 0}},
    }
    merged = _merge_existing_config(existing, generated)
    inv = merged["_inventory"]
    items = [u for u in (inv.get("unmapped") or []) if isinstance(u, dict)]
    assert len(items) == 2
    reasons = sorted(u["reason"] for u in items)
    assert reasons == ["site A", "site B"]


def test_update_observed_unmapped_not_duplicated():
    # A currently observed unmapped row renders once; the carry-forward
    # must not duplicate it.
    loc = {"path": "d/Dockerfile", "manifest": "dockerfile", "line": 1,
           "locator": "FROM old-dep:1.0"}
    unmapped_item = {
        "ecosystem": "container", "name": "old-dep", "version": "1.0",
        "reason": "legacy", "found_in": [dict(loc)],
    }
    existing = {
        "products": [
            {"source": "manual", "label": "old-dep 1.0",
             "product": "old-dep", "version": "1.0",
             "_inventory_generated": "unmapped",
             "_comment": "Untracked container inventory item",
             "_found_in": [dict(loc)]},
        ],
        "_inventory": {"unmapped": [dict(unmapped_item)]},
    }
    generated = {
        "products": [],
        "_inventory": {"unmapped": [dict(unmapped_item)],
                       "manifests": [],
                       "summary": {"files": 0, "records": 0, "products": 0,
                                   "unmapped": 1, "warnings": 0,
                                   "indirect": 0}},
    }
    merged = _merge_existing_config(existing, generated)
    inv = merged["_inventory"]
    names = [u.get("name") for u in (inv.get("unmapped") or [])
             if isinstance(u, dict)]
    assert names.count("old-dep") == 1


def test_update_curated_manual_row_remains_product():
    # A curated manual row (not scanner-generated) is a product, not an
    # unmapped metadata entry: the carry-forward must not touch it.
    loc = {"path": "manual.txt", "manifest": "manual", "line": 1,
           "locator": "manual"}
    existing = {
        "products": [
            {"source": "manual", "label": "curated-dep",
             "product": "curated-dep", "policy_note": "reviewed",
             "_comment": "Added by hand", "_found_in": [dict(loc)]},
        ],
        "_inventory": {"unmapped": []},
    }
    generated = {
        "products": [],
        "_inventory": {"unmapped": [], "manifests": [],
                       "summary": {"files": 0, "records": 0, "products": 0,
                                   "unmapped": 0, "warnings": 0,
                                   "indirect": 0}},
    }
    merged = _merge_existing_config(existing, generated)
    products = _products(merged)
    assert len(products) == 1
    assert products[0]["product"] == "curated-dep"
    assert products[0]["policy_note"] == "reviewed"
    assert not merged["_inventory"].get("unmapped")


def test_update_no_mutation_of_generated_dict():
    # The merge must not mutate the caller's generated dict (aliasing):
    # two sequential merges with a shared generated dict must produce
    # independent results.
    loc = {"path": "d/Dockerfile", "manifest": "dockerfile", "line": 1,
           "locator": "FROM alpha:1.0"}
    existing_a = {
        "products": [
            {"source": "manual", "label": "alpha 1.0",
             "product": "alpha", "version": "1.0",
             "_inventory_generated": "unmapped",
             "_comment": "Untracked", "_found_in": [dict(loc)]},
        ],
        "_inventory": {"unmapped": [
            {"ecosystem": "container", "name": "alpha",
             "version": "1.0", "reason": "unmapped",
             "found_in": [dict(loc)]},
        ]},
    }
    generated = {
        "products": [],
        "_inventory": {"unmapped": [], "manifests": [],
                       "summary": {"files": 0, "records": 0, "products": 0,
                                   "unmapped": 0, "warnings": 0,
                                   "indirect": 0}},
    }
    merged_a = _merge_existing_config(existing_a, generated)
    assert len(merged_a["_inventory"].get("unmapped") or []) == 1
    # The generated dict must be untouched by the first merge.
    assert not generated["_inventory"].get("unmapped")
    # A second merge with the same generated dict still works.
    merged_b = _merge_existing_config(existing_a, generated)
    assert len(merged_b["_inventory"].get("unmapped") or []) == 1


def test_cli_update_rejects_non_object_json():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text("{}", encoding="utf-8")
        output = root / "out.json"
        output.write_text("[]", encoding="utf-8")
        assert generate_config_main([
            str(root), "--name", "demo", "--output", str(output),
            "--update"]) == 2
        assert json.loads(output.read_text(encoding="utf-8")) == []


def test_cli_update_rejects_non_list_products():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text("{}", encoding="utf-8")
        output = root / "out.json"
        original = {"products": None, "owner": "curated"}
        output.write_text(json.dumps(original), encoding="utf-8")
        assert generate_config_main([
            str(root), "--name", "demo", "--output", str(output),
            "--update"]) == 2
        assert json.loads(output.read_text(encoding="utf-8")) == original

        invalid_members = {"products": [None, "bad"]}
        output.write_text(json.dumps(invalid_members), encoding="utf-8")
        assert generate_config_main([
            str(root), "--name", "demo", "--output", str(output),
            "--update"]) == 2
        assert json.loads(output.read_text(encoding="utf-8")) == invalid_members

        unhashable_member = {"products": [{"product": ["bad"]}]}
        output.write_text(json.dumps(unhashable_member), encoding="utf-8")
        assert generate_config_main([
            str(root), "--name", "demo", "--output", str(output),
            "--update"]) == 2
        assert json.loads(output.read_text(encoding="utf-8")) == unhashable_member


def test_scan_ignores_standalone_central_package_declarations():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Directory.Packages.props").write_text(
            '<Project><ItemGroup><PackageVersion Include="NeverReferenced" '
            'Version="1.2.3" /></ItemGroup></Project>', encoding="utf-8")
        scan = gc.scan_folder(str(root))
        config = gc.generate_config(scan, "demo")
    assert scan["records"] == []
    assert not any(p.get("package") == "NeverReferenced"
                   for p in config["products"] if isinstance(p, dict))


def test_terraform_uses_positive_runtime_allowlist():
    terraform = (ROOT / "terraform" / "main.tf").read_text(encoding="utf-8")
    assert "source_dir" not in terraform
    assert "excludes =" not in terraform
    assert '["lambda_function.py"]' in terraform
    assert 'fileset("${path.module}/../eoltracker", "**/*.py")' in terraform
    assert '"eoltracker/${source_file}"' in terraform
    assert 'dynamic "source"' in terraform
    assert 'filename = source.value' in terraform


TESTS = [
    test_version_helpers,
    test_live_smoke_command_quotes_interpreter_and_output,
    test_entry_builders,
    test_map_java_dep,
    test_map_npm_dep,
    test_pom_property_mappings,
    test_parse_pom_records,
    test_parse_pom_records_plain_and_broken,
    test_pom_rejects_dtd_and_entities,
    test_parse_pom_records_unresolved,
    test_pom_coordinate_properties_resolve_or_remain_untracked,
    test_parse_gradle_records,
    test_parse_package_json_records,
    test_scan_folder_maven_multi,
    test_scan_folder_mixed,
    test_scan_folder_skips_node_modules,
    test_scan_folder_default_exclusions,
    test_scan_folder_eolignore_and_exclude,
    test_scan_folder_skips_escaping_symlinks,
    test_windows_junctions_are_classified_as_directory_links,
    test_scan_folder_oversize_warning,
    test_scan_folder_refuses_huge_file_count,
    test_scan_folder_not_a_directory,
    test_scan_discovers_generic_gradle_nvmrc_and_isolates_bad_manifest,
    test_scan_folder_deduplicates_shared_include_graphs,
    test_scan_folder_enforces_scanner_wide_include_budgets,
    test_generate_config_maven_multi,
    test_generate_config_gradle,
    test_generate_config_keeps_unresolved_mapped_pom_properties_visible,
    test_generate_config_node,
    test_generate_config_mixed,
    test_generate_config_no_inference_when_security_explicit,
    test_generate_config_entry_key_dedup_merges_provenance,
    test_generate_config_deterministic,
    test_inventory_metadata_carries_scan_date_and_consumed_manifests,
    test_warnings_sorted_and_deduplicated,
    test_cli_refuses_overwrite_without_replace,
    test_cli_output_is_ascii_and_deterministic,
    test_cli_strict_fails_on_warnings,
    test_runtime_ignores_found_in,
    test_transitive_dependencies_are_opt_in,
    test_update_merge_preserves_curation_and_unobserved_entries,
    test_update_merge_preserves_multiple_versions_and_default_source,
    test_update_replaces_stale_scanner_mapping_by_provenance,
    test_update_replaces_generated_manual_row_when_now_tracked,
    test_update_uses_stable_provenance_and_preserves_unmapped_edits,
    test_update_remaps_distinct_dockerfile_images_by_repository,
    test_dockerfile_from_key_redacts_legacy_credential_locator,
    test_update_cleanses_legacy_credential_locator_row,
    test_update_unmapped_defaults_match_the_right_sibling,
    test_update_legacy_bare_from_key_still_merges_conservatively,
    test_update_legacy_bare_from_key_never_clobbers_when_ambiguous,
    test_update_matches_both_changed_rows_of_one_identity_by_provenance,
    test_update_retains_ambiguous_same_identity_rows,
    test_update_crossing_partial_upgrade_by_provenance,
    test_update_all_changed_multi_version_by_provenance,
    test_update_one_changed_one_unchanged_multi_version,
    test_update_nuget_case_insensitive_identity,
    test_update_retained_untracked_inventory_visible,
    test_update_same_name_distinct_sites_both_carried,
    test_update_observed_unmapped_not_duplicated,
    test_update_curated_manual_row_remains_product,
    test_update_no_mutation_of_generated_dict,
    test_cli_update_rejects_non_object_json,
    test_cli_update_rejects_non_list_products,
    test_scan_ignores_standalone_central_package_declarations,
    test_terraform_uses_positive_runtime_allowlist,
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
    print("OK test_generate_config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
