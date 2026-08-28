"""Characterization tests for the manifest-to-config generator.

Pins the *current* behavior of generate_config.py (Maven POM, Gradle Groovy
and Kotlin DSL, and package.json parsing; section layout; product mapping;
de-duplication; provenance comments; skipped-npm reporting; and the Spring
Security inference) before the generator is moved into the helper_scripts/
package. Standalone assertion script: no pytest, no network, no subprocesses.

Run from the repository root:  python tests/test_generate_config.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

import generate_config as gc

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "generate_config"


def _posix(p):
    return str(p).replace("\\", "/")


def _products(config):
    return [p for p in config["products"] if not p.get("_section")]


def _sections(config):
    return [p["_section"] for p in config["products"] if p.get("_section")]


def _scan(*parts):
    return gc.scan_folder(str(FIX.joinpath(*parts)))


# ---------------------------------------------------------------------------
# Version helpers and entry builders
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


# ---------------------------------------------------------------------------
# Mapping behavior
# ---------------------------------------------------------------------------

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
# Parsers
# ---------------------------------------------------------------------------

def test_parse_pom_namespaced():
    deps, props = gc.parse_pom(FIX / "maven_multi" / "pom.xml")
    assert ("org.springframework.boot", "spring-boot-starter-parent",
            "3.3.4", "parent") in deps
    assert ("com.fasterxml.jackson", "jackson-bom", "2.17.2", "dep") in deps
    assert ("io.netty", "netty-codec-http", "4.1.111.Final", "dep") in deps
    assert ("com.fasterxml.jackson.core", "jackson-databind",
            "2.17.2", "dep") in deps
    assert ("org.webjars", "bootstrap", "5.3.3", "dep") in deps
    assert ("org.webjars.npm", "chart.js", "4.4.3", "dep") in deps
    assert ("internal.tools", "internal-lib", "1.2.3", "dep") in deps
    assert ("com.example", "inflight", "2.0.0-SNAPSHOT", "dep") in deps
    # versionless and test-scoped deps are dropped at parse time
    assert all(a != "spring-boot-starter-web" for _, a, _, _ in deps)
    assert all(a != "junit" for _, a, _, _ in deps)
    assert props["java.version"] == "17"
    assert props["tomcat.version"] == "10.1.54"
    assert props["netty.version"] == "4.1.111.Final"
    assert props["jackson.version"] == "2.17.2"
    assert props["project.version"] == "1.0.0"


def test_parse_pom_plain_and_broken():
    deps, props = gc.parse_pom(FIX / "samples" / "plain_pom.xml")
    assert deps == [("org.example", "plain-lib", "0.9.0", "dep")]
    assert props["project.version"] == "0.9.0"
    assert props["release.version"] == "11"
    deps2, props2 = gc.parse_pom(FIX / "samples" / "broken_pom.xml")
    assert deps2 == [] and props2 == {}


def test_parse_gradle():
    deps = gc.parse_gradle(FIX / "gradle" / "build.gradle")
    # the quoted pattern matches double-quoted declarations only, and the
    # named group form only with `group = "..."` syntax
    assert deps == [
        ("org.apache.commons", "commons-text", "1.11.0", "gradle"),
        ("io.netty", "netty-codec-http", "4.1.111.Final", "gradle"),
        ("org.jetbrains.kotlin", "kotlin-stdlib", "2.0.0", "gradle"),
        ("com.h2database", "h2", "2.2.224", "gradle"),
    ]
    kts = gc.parse_gradle(FIX / "gradle" / "build.gradle.kts")
    assert kts == [
        ("org.jetbrains.kotlinx", "kotlinx-coroutines-core", "1.8.1", "gradle"),
        ("org.springframework.boot", "spring-boot-gradle-plugin",
         "3.3.4", "gradle"),
        ("com.google.guava", "guava", "33.2.1-jre", "gradle"),
    ]
    missing = gc.parse_gradle(FIX / "gradle" / "nope.gradle")
    assert missing == []


def test_parse_package_json():
    deps = gc.parse_package_json(FIX / "node" / "package.json")
    assert deps[0] == ("node", "18")
    assert ("react", "18.2.0") in deps
    assert ("react-dom", "18.2.0") in deps
    assert ("axios", "1.6.8") in deps
    assert ("left-pad", "1.3.0") in deps
    assert ("typescript", "5.4.5") in deps
    assert ("@company/tokens", "1.0.0") in deps
    assert gc.parse_package_json(FIX / "node" / "missing.json") == []


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------

def test_scan_folder_maven_multi():
    scan = _scan("maven_multi")
    files = [_posix(f) for f in scan["files"]]
    assert len(files) == 2
    assert files[0].endswith("maven_multi/pom.xml")
    assert files[1].endswith("maven_multi/service/pom.xml")
    # only the root POM carries a <properties> block
    assert len(scan["pom_properties"]) == 1
    assert _posix(scan["pom_properties"][0][1]).endswith("maven_multi/pom.xml")
    assert scan["node"] == []
    assert scan["java"][0] == ("org.springframework.boot",
                               "spring-boot-starter-parent", "3.3.4",
                               scan["files"][0], "parent")


def test_scan_folder_mixed_file_order():
    scan = _scan("mixed")
    assert len(scan["files"]) == 3
    # POMs are scanned before Gradle files, Gradle before package.json
    ends = [_posix(f).split("generate_config/")[-1] for f in scan["files"]]
    assert ends == ["mixed/pom.xml", "mixed/build.gradle",
                    "mixed/package.json"]


def test_scan_folder_skips_node_modules():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "node_modules" / "left-pad").mkdir(parents=True)
        (root / "node_modules" / "left-pad" / "package.json").write_text(
            json.dumps({"name": "left-pad", "version": "1.3.0"}))
        (root / "package.json").write_text(json.dumps(
            {"name": "app", "dependencies": {"react": "^18.2.0"}}))
        scan = gc.scan_folder(str(root))
        assert len(scan["node"]) == 1
        assert scan["node"][0][0] == "react"
        assert len(scan["files"]) == 1


def test_scan_folder_not_a_directory():
    try:
        gc.scan_folder(str(FIX / "does" / "not" / "exist"))
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit for missing folder")


# ---------------------------------------------------------------------------
# Config generation: sections, mapping, dedup, provenance
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
    # Platforms from POM properties, in mapping-table order
    assert prods[0] == {
        "product": "amazon-corretto", "version": "17",
        "label": "Amazon Corretto (OpenJDK) 17",
        "_comment": "From pom.xml (<java.version>17</java.version>)"}
    assert prods[1]["product"] == "tomcat" and prods[1]["version"] == "10.1"
    assert prods[2] == {
        "source": "maven_central", "group": "io.netty",
        "artifact": "netty-codec-http", "version": "4.1.111.Final",
        "label": "Netty Codec HTTP 4.1.111.Final",
        "_comment": "From pom.xml (<netty.version>4.1.111.Final</netty.version>)"}
    assert prods[3]["artifact"] == "logback-classic"

    java = prods[4:]
    # parent dependency maps to spring-boot with major.minor cycle
    assert java[0]["product"] == "spring-boot" and java[0]["version"] == "3.3"
    assert java[0]["_comment"] == (
        "From pom.xml (org.springframework.boot:spring-boot-starter-parent:3.3.4)")
    # jackson-bom and jackson-databind collapse to one jackson_lifecycle entry,
    # provenance from the first declaration seen
    jacksons = [p for p in java if p.get("source") == "jackson_lifecycle"]
    assert len(jacksons) == 1 and jacksons[0]["version"] == "2.17"
    assert jacksons[0]["_comment"] == (
        "From pom.xml (com.fasterxml.jackson:jackson-bom:2.17.2)")
    # the netty dependency duplicates the property-driven entry and is
    # dropped: exactly one io.netty entry remains, in the Platforms section
    assert len([p for p in prods if p.get("group") == "io.netty"]) == 1
    assert all(p.get("group") != "io.netty" for p in java)
    # full Maven version retained on the maven_central fallback
    lang3 = [p for p in java if p.get("artifact") == "commons-lang3"]
    assert lang3 and lang3[0]["version"] == "3.14.0"
    assert lang3[0]["_comment"] == (
        "From pom.xml (org.apache.commons:commons-lang3:3.14.0)")
    # webjar maps with major-only cycle; full version only in the label
    boot_webjar = [p for p in java if p.get("product") == "bootstrap"]
    assert boot_webjar and boot_webjar[0]["version"] == "5"
    assert "(using 5.3.3)" in boot_webjar[0]["label"]
    # child-POM parent coordinate falls back to maven_central
    mm = [p for p in java if p.get("artifact") == "maven-multi"]
    assert mm and mm[0]["group"] == "com.example" and mm[0]["version"] == "1.0.0"
    # snapshot, internal-group, and webjar-junk deps never become entries
    arts = {p.get("artifact") for p in java}
    assert "inflight" not in arts
    assert "internal-lib" not in arts
    assert "chart.js" not in arts
    # spring-security inferred exactly once from the Boot release train
    sec = [p for p in prods if p.get("product") == "spring-security"]
    assert len(sec) == 1 and sec[0]["version"] == "6.3"
    assert "Auto-derived from Spring Boot 3.3" in sec[0]["_comment"]
    assert not config.get("_skipped_npm_packages")


def test_generate_config_gradle():
    config = gc.generate_config(_scan("gradle"), "gradle-project")
    assert _sections(config) == [
        "=== Java dependencies ===",
        "=== Inferred from Spring Boot release train ===",
    ]
    prods = _products(config)
    # build.gradle.kts is scanned before build.gradle (pattern-loop order),
    # so kotlinx appears first and maps to the kotlin 1.8 lifecycle entry
    assert [(p.get("product") or p.get("artifact"), p.get("version"))
            for p in prods] == [
        ("kotlin", "1.8"),          # kotlinx-coroutines-core (org.jetbrains.kotlin*)
        ("spring-boot", "3.3"),     # spring-boot-gradle-plugin via classpath
        ("guava", "33.2.1-jre"),    # named form in the kts file
        ("commons-text", "1.11.0"),
        ("netty-codec-http", "4.1.111.Final"),
        ("kotlin", "2.0"),          # kotlin-stdlib — distinct version, own entry
        ("h2", "2.2.224"),
        ("spring-security", "6.3"),
    ]
    guava = [p for p in prods if p.get("artifact") == "guava"]
    assert guava[0]["_comment"] == (
        "From build.gradle.kts (com.google.guava:guava:33.2.1-jre)")
    boot = [p for p in prods if p.get("product") == "spring-boot"]
    assert boot[0]["_comment"] == (
        "From build.gradle.kts (org.springframework.boot:spring-boot-gradle-plugin:3.3.4)")
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
        "_comment": "From package.json (node@18)"}
    assert [p for p in prods if p.get("product") == "react"] == [{
        "product": "react", "version": "18", "label": "React 18",
        "_comment": "From package.json (react@18.2.0)"}]
    ts = [p for p in prods if p.get("product") == "typescript"]
    assert ts and ts[0]["version"] == "5.4"
    # unmapped packages land in _skipped_npm_packages with manifest basename
    assert config["_skipped_npm_packages"] == [
        {"name": "axios", "version": "1.6.8", "source": "package.json"},
        {"name": "left-pad", "version": "1.3.0", "source": "package.json"},
        {"name": "@company/tokens", "version": "1.0.0", "source": "package.json"},
    ]
    # react-dom is deliberately not reported (tracked via 'react')
    assert all(s["name"] != "react-dom"
               for s in config["_skipped_npm_packages"])


def test_generate_config_mixed():
    config = gc.generate_config(_scan("mixed"), "mixed")
    assert _sections(config) == [
        "=== Platforms (from POM properties) ===",
        "=== Java dependencies ===",
        "=== npm dependencies ===",
        "=== Inferred from Spring Boot release train ===",
    ]
    prods = _products(config)
    # netty declared in both pom.xml and build.gradle keeps the first
    # declaration's provenance and drops the duplicate
    netty = [p for p in prods if p.get("group") == "io.netty"]
    assert len(netty) == 1
    assert netty[0]["_comment"] == (
        "From pom.xml (io.netty:netty-codec-http:4.1.111.Final)")
    guava = [p for p in prods if p.get("artifact") == "guava"]
    assert guava[0]["_comment"] == (
        "From build.gradle (com.google.guava:guava:33.2.1-jre)")
    node_entry = [p for p in prods if p.get("product") == "nodejs"]
    assert node_entry and node_entry[0]["version"] == "20"
    sec = [p for p in prods if p.get("product") == "spring-security"]
    assert sec and sec[0]["version"] == "6.3"
    assert config["_skipped_npm_packages"] == [
        {"name": "axios", "version": "1.7.2", "source": "package.json"}]


def test_generate_config_no_inference_when_security_explicit():
    scan = {
        "java": [
            ("org.springframework.boot", "spring-boot-starter-parent",
             "3.3.4", "x/pom.xml", "parent"),
            ("org.springframework.security", "spring-security-config",
             "6.4.1", "x/pom.xml", "dep"),
        ],
        "pom_properties": [],
        "node": [],
        "files": ["x/pom.xml"],
    }
    config = gc.generate_config(scan, "explicit-security")
    secs = [p for p in _products(config) if p.get("product") == "spring-security"]
    assert len(secs) == 1 and secs[0]["version"] == "6.4"
    assert not secs[0]["_comment"].startswith("Auto-derived")
    assert "=== Inferred from Spring Boot release train ===" not in _sections(config)


def test_generate_config_entry_key_dedup():
    # entries with identical coordinates across shapes collapse via _entry_key
    scan = {
        "java": [
            ("io.netty", "netty-codec-http", "4.1.111.Final", "a/pom.xml", "dep"),
            ("io.netty", "netty-codec-http", "4.1.111.Final", "b/pom.xml", "dep"),
        ],
        "pom_properties": [],
        "node": [],
        "files": ["a/pom.xml", "b/pom.xml"],
    }
    config = gc.generate_config(scan, "dedup")
    prods = _products(config)
    assert len(prods) == 1
    assert prods[0]["_comment"] == "From pom.xml (io.netty:netty-codec-http:4.1.111.Final)"


TESTS = [
    test_version_helpers,
    test_entry_builders,
    test_map_java_dep,
    test_map_npm_dep,
    test_pom_property_mappings,
    test_parse_pom_namespaced,
    test_parse_pom_plain_and_broken,
    test_parse_gradle,
    test_parse_package_json,
    test_scan_folder_maven_multi,
    test_scan_folder_mixed_file_order,
    test_scan_folder_skips_node_modules,
    test_scan_folder_not_a_directory,
    test_generate_config_maven_multi,
    test_generate_config_gradle,
    test_generate_config_node,
    test_generate_config_mixed,
    test_generate_config_no_inference_when_security_explicit,
    test_generate_config_entry_key_dedup,
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
