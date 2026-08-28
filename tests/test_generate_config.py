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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

import generate_config as gc
import eol_inventory.discovery as discovery_module
from eol_inventory.models import add_location, new_record

ROOT = Path(__file__).resolve().parents[1]
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
    assert gc._map_npm_dep("react-dom", "^18.2.0") is None
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


def test_parse_package_json_records():
    records, warnings = gc.parse_package_json_records(
        FIX / "node" / "package.json", "package.json")
    assert warnings == []
    by_name = {r["name"]: r for r in records}
    node = by_name["node"]
    assert node["kind"] == "runtime" and node["version"] == "18"
    assert node["found_in"] == [{
        "path": "package.json", "manifest": "npm", "locator": "engines.node"}]
    react = by_name["react"]
    assert react["version"] == "18.2.0" and react["scope"] == "runtime"
    assert react["found_in"][0]["locator"] == "dependencies.react"
    ts = by_name["typescript"]
    assert ts["version"] == "5.4.5" and ts["scope"] == "dev"
    assert ts["found_in"][0]["locator"] == "devDependencies.typescript"
    assert "@company/tokens" in by_name
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
    assert scan["files"] == ["build.gradle", "package.json", "pom.xml"]
    # maven records precede gradle records, which precede node records
    ecosystems = [(r["ecosystem"], r["kind"]) for r in scan["records"]]
    assert ecosystems == [
        ("java", "parent"), ("java", "dependency"), ("java", "dependency"),
        ("java", "property"),
        ("java", "dependency"), ("java", "dependency"), ("java", "dependency"),
        ("node", "runtime"), ("node", "dependency"), ("node", "dependency"),
    ]
    assert scan["warnings"] == []


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
        assert len(escaped) == 1
        assert escaped[0]["path"] == "pom_link.xml"


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


# ---------------------------------------------------------------------------
# Config generation: sections, mapping, dedup, provenance, inventory
# ---------------------------------------------------------------------------

def test_generate_config_maven_multi():
    config = gc.generate_config(_scan("maven_multi"), "maven-multi")
    assert _sections(config) == [
        "=== Platforms (from POM properties) ===",
        "=== Java dependencies ===",
        "=== Inferred from Spring Boot release train ===",
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
                              "unmapped": 3, "warnings": 0}
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
    # single-quoted Groovy declarations and testImplementation are not parsed
    arts = {p.get("artifact") for p in prods}
    assert "single-quoted" not in arts and "junit" not in arts


def test_generate_config_node():
    config = gc.generate_config(_scan("node"), "node-project")
    assert _sections(config) == ["=== npm dependencies ==="]
    prods = _products(config)
    assert prods[0] == {
        "product": "nodejs", "version": "18", "label": "Node.js 18",
        "_comment": "From package.json (node@18)",
        "_found_in": [{"path": "package.json", "manifest": "npm",
                       "locator": "engines.node"}]}
    assert [p for p in prods if p.get("product") == "react"] == [{
        "product": "react", "version": "18", "label": "React 18",
        "_comment": "From package.json (react@18.2.0)",
        "_found_in": [{"path": "package.json", "manifest": "npm",
                       "locator": "dependencies.react"}]}]
    ts = [p for p in prods if p.get("product") == "typescript"]
    assert ts and ts[0]["version"] == "5.4"
    # legacy skipped list is preserved with manifest basename provenance
    assert config["_skipped_npm_packages"] == [
        {"name": "axios", "version": "1.6.8", "source": "package.json"},
        {"name": "left-pad", "version": "1.3.0", "source": "package.json"},
        {"name": "@company/tokens", "version": "1.0.0", "source": "package.json"},
    ]
    # react-dom is deliberately not reported (tracked via 'react')
    assert all(s["name"] != "react-dom"
               for s in config["_skipped_npm_packages"])
    # the same items appear in the structured inventory, sorted by name
    unmapped = config["_inventory"]["unmapped"]
    assert [u["name"] for u in unmapped] == [
        "@company/tokens", "axios", "left-pad"]
    assert all(u["ecosystem"] == "node" for u in unmapped)
    assert unmapped[0]["found_in"] == [{
        "path": "package.json", "manifest": "npm",
        "locator": "devDependencies.@company/tokens"}]
    assert unmapped[1]["version"] == "1.6.8"
    assert config["_inventory"]["summary"] == {
        "files": 1, "records": 7, "products": 3, "unmapped": 3, "warnings": 0}


def test_generate_config_mixed():
    config = gc.generate_config(_scan("mixed"), "mixed")
    assert _sections(config) == [
        "=== Platforms (from POM properties) ===",
        "=== Java dependencies ===",
        "=== npm dependencies ===",
        "=== Inferred from Spring Boot release train ===",
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
    node_entry = [p for p in prods if p.get("product") == "nodejs"]
    assert node_entry and node_entry[0]["version"] == "20"
    sec = [p for p in prods if p.get("product") == "spring-security"]
    assert sec and sec[0]["version"] == "6.3"
    assert config["_skipped_npm_packages"] == [
        {"name": "axios", "version": "1.7.2", "source": "package.json"}]
    inv = config["_inventory"]
    assert inv["summary"] == {"files": 3, "records": 10, "products": 9,
                              "unmapped": 1, "warnings": 0}
    assert inv["unmapped"][0]["name"] == "axios"
    assert inv["unmapped"][0]["found_in"][0]["locator"] == "dependencies.axios"


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
    return gc.main(argv)


def test_cli_refuses_overwrite_without_force():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps(
            {"name": "app", "dependencies": {"react": "^18.2.0"}}))
        out = root / "out.json"
        out.write_text("keep")
        rc = _run_cli([str(root), "--output", str(out)])
        assert rc == 2
        assert out.read_text() == "keep"
        rc = _run_cli([str(root), "--output", str(out), "--force"])
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
        rc = _run_cli([str(root), "--output", str(out), "--force"])
        assert rc == 0
        raw = out.read_bytes()
        assert all(b < 128 for b in raw)
        first = raw
        _run_cli([str(root), "--output", str(out), "--force"])
        assert out.read_bytes() == first
        config = json.loads(raw.decode("ascii"))
        assert config["_inventory"]["unmapped"][0]["name"] == "café-pkg"


def test_cli_strict_fails_on_warnings():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pom.xml").write_text("<project><unclosed>")
        out = root / "out.json"
        rc = _run_cli([str(root), "--output", str(out), "--force"])
        assert rc == 0
        rc = _run_cli([str(root), "--output", str(out), "--force", "--strict"])
        assert rc == 1
        rc = _run_cli([str(root), "--output", str(out), "--force", "--strict",
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


TESTS = [
    test_version_helpers,
    test_entry_builders,
    test_map_java_dep,
    test_map_npm_dep,
    test_pom_property_mappings,
    test_parse_pom_records,
    test_parse_pom_records_plain_and_broken,
    test_parse_pom_records_unresolved,
    test_parse_gradle_records,
    test_parse_package_json_records,
    test_scan_folder_maven_multi,
    test_scan_folder_mixed,
    test_scan_folder_skips_node_modules,
    test_scan_folder_default_exclusions,
    test_scan_folder_eolignore_and_exclude,
    test_scan_folder_skips_escaping_symlinks,
    test_scan_folder_oversize_warning,
    test_scan_folder_refuses_huge_file_count,
    test_scan_folder_not_a_directory,
    test_generate_config_maven_multi,
    test_generate_config_gradle,
    test_generate_config_node,
    test_generate_config_mixed,
    test_generate_config_no_inference_when_security_explicit,
    test_generate_config_entry_key_dedup_merges_provenance,
    test_generate_config_deterministic,
    test_warnings_sorted_and_deduplicated,
    test_cli_refuses_overwrite_without_force,
    test_cli_output_is_ascii_and_deterministic,
    test_cli_strict_fails_on_warnings,
    test_runtime_ignores_found_in,
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
