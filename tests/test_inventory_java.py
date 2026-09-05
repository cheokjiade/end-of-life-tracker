"""Network-free Java parser tests: pom, gradle, and version-catalog coverage.

Moved from tests/test_generate_parsing.py (root generate_config.py) with the
imports retargeted to helper_scripts/eol_inventory. The root script returned
(g, a, v, kind) tuples from parse_gradle and a (deps, props, repos) tuple from
parse_pom; the consolidated parsers return (records, warnings) where every
record is a normalized dict (see eol_inventory.models.new_record). Every
assertion that pinned the root-only tuple shape is retargeted to the record
shape with a `RETARGETED:` note. Standalone assertion script: no pytest, no
network, no subprocesses.

Run from the repository root:  python tests/test_inventory_java.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]
_HELPER_DIR = ROOT / "helper_scripts"
sys.path.insert(0, str(_HELPER_DIR))

from eol_inventory import generate_config, scan_folder
from eol_inventory.mappings import _map_java_dep
from eol_inventory.parsers.java import (
    _GRADLE_PATTERN_QUOTED,
    _is_exact_java_version,
    parse_gradle_records,
    parse_pom_records,
    parse_version_catalog,
)
from eol_inventory.parsers.maven_repositories import _strip_gradle_comments

FIX = ROOT / "tests" / "fixtures" / "generate_config"


def _write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text, encoding="utf-8")
    return p


def _gavs(records):
    return [(r["group"], r["artifact"], r["version"]) for r in records]


def _rows(config):
    return [prod for prod in config["products"] if not prod.get("_section")]


# --- F: POM dependency kinds (managed / unversioned) ------------------------

POM_NAMESPACED = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.3.4</version>
  </parent>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.fasterxml.jackson</groupId>
        <artifactId>jackson-bom</artifactId>
        <version>2.17.0</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
    <dependency>
      <groupId>commons-io</groupId>
      <artifactId>commons-io</artifactId>
      <version>2.16.1</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
"""


def test_pom_namespaced_kinds():
    """RETARGETED: parse_pom(p) -> (deps, props, repos) tuples became
    parse_pom_records(p, rel) -> (records, warnings). The consolidated parser
    has no managed-dep kind (managed dependencies are ordinary "dependency"
    records), but scope-skipped and versionless dependencies keep the root
    kinds so every parsed declaration still gets an outcome."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "pom.xml", POM_NAMESPACED)
        records, warnings = parse_pom_records(p, "pom.xml")
    assert warnings == [], warnings
    deps = [(r["group"], r["artifact"], r["version"], r["kind"]) for r in records]
    assert ("org.springframework.boot", "spring-boot-starter-parent", "3.3.4", "parent") in deps, deps
    assert ("com.fasterxml.jackson", "jackson-bom", "2.17.0", "dependency") in deps, deps
    assert ("org.springframework.boot", "spring-boot-starter-web", None,
            "unversioned-dep") in deps, deps
    assert ("commons-io", "commons-io", "2.16.1", "dependency") in deps, deps
    assert ("junit", "junit", "4.13.2", "test-scope-dep") in deps, deps
    junit = next(r for r in records if r["artifact"] == "junit")
    assert junit["scope"] == "test" and junit["version_spec"] is None, junit
    print("OK namespaced pom: parent/managed-dep/unversioned-dep/dep kinds, test scope recorded")


POM_PLAIN = """<project>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>io.netty</groupId>
        <artifactId>netty-bom</artifactId>
        <version>4.1.111.Final</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>ch.qos.logback</groupId>
      <artifactId>logback-classic</artifactId>
    </dependency>
  </dependencies>
</project>
"""


def test_pom_plain_kinds():
    """RETARGETED: tuple kinds -> record dicts, see test_pom_namespaced_kinds."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "pom.xml", POM_PLAIN)
        records, warnings = parse_pom_records(p, "pom.xml")
    assert warnings == [], warnings
    deps = [(r["group"], r["artifact"], r["version"], r["kind"]) for r in records]
    assert ("io.netty", "netty-bom", "4.1.111.Final", "dependency") in deps, deps
    assert ("ch.qos.logback", "logback-classic", None,
            "unversioned-dep") in deps, deps
    print("OK non-namespaced pom: managed-dep and unversioned-dep kinds")


POM_MANAGED_AND_UNVERSIONED = """<project>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.fasterxml.jackson</groupId>
        <artifactId>jackson-bom</artifactId>
        <version>2.17.0</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
</project>
"""


def test_generate_config_skips_unversioned_keeps_managed():
    """End-to-end: unversioned deps never map (would crash/doom); managed deps
    keep the current behaviour (jackson-bom -> its own jackson_lifecycle row).
    RETARGETED: the root scan dict of (g, a, v, src, kind) tuples became a
    scan_folder() run over the equivalent pom."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "pom.xml", POM_MANAGED_AND_UNVERSIONED)
        scan = scan_folder(tmp)
    config = generate_config(scan, "demo")
    rows = _rows(config)
    assert [r["label"] for r in rows] == ["Jackson BOM 2.17"], rows
    print("OK generate_config skips unversioned deps, keeps managed-dep mapping")


# --- G: Gradle version catalogs ---------------------------------------------

CATALOG_TOML = """
[versions]
commonsLang3 = "3.14.0"
gson = "2.11.0"

[libraries]
commons-lang3 = { module = "org.apache.commons:commons-lang3", version.ref = "commonsLang3" }
netty-http = { group = "io.netty", name = "netty-codec-http", version = "4.1.111.Final" }
gson = { module = "com.google.code.gson:gson", version = { ref = "gson" } }
broken = { module = "com.example:broken", version.ref = "missing" }

[bundles]
common = ["commons-lang3", "netty-http"]
"""

KTS_CATALOG = """
dependencies {
    implementation(libs.commons.lang3)
    implementation(libs.netty.http)
    implementation(libs.bundles.common)
    implementation(libs.broken)
    implementation(libs.versions.commonsLang3.get())
}
"""


def test_version_catalog_parses():
    """RETARGETED: parse_version_catalog(p) -> (aliases, bundles) became
    parse_version_catalog(p, rel) -> (aliases, bundles, warnings)."""
    with tempfile.TemporaryDirectory() as tmp:
        toml_p = _write(tmp, "libs.versions.toml", CATALOG_TOML)
        aliases, bundles, warnings = parse_version_catalog(toml_p, "libs.versions.toml")
    assert warnings == [], warnings
    assert aliases["commons.lang3"] == ("org.apache.commons", "commons-lang3", "3.14.0"), aliases
    assert aliases["netty.http"] == ("io.netty", "netty-codec-http", "4.1.111.Final"), aliases
    assert aliases["gson"] == ("com.google.code.gson", "gson", "2.11.0"), aliases
    assert "broken" not in aliases, aliases
    assert bundles["common"] == ["commons.lang3", "netty.http"], bundles
    print("OK libs.versions.toml parses: module + group/name, ref and table-ref versions")


def test_version_catalog_unreadable():
    """New: an unreadable catalog yields empty tables plus a warning."""
    aliases, bundles, warnings = parse_version_catalog(
        FIX / "gradle" / "gradle" / "nope.versions.toml", "gradle/nope.versions.toml")
    assert aliases == {} and bundles == {}, (aliases, bundles)
    assert [w["category"] for w in warnings] == ["unreadable_file"], warnings


def test_catalog_refs_resolve_end_to_end():
    """RETARGETED: scan["java"] tuples with kind "gradle-catalog" became
    ordinary java records; the unresolvable `libs.broken` reference, which
    the root script silently dropped, is now a versionless record and a
    manual-review row (the spec's intent: unresolved references are visible).
    """
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "libs.versions.toml", CATALOG_TOML)
        _write(tmp, "build.gradle.kts", KTS_CATALOG)
        scan = scan_folder(tmp)
    java = [r for r in scan["records"] if r["ecosystem"] == "java"]
    gavs = _gavs(java)
    assert ("org.apache.commons", "commons-lang3", "3.14.0") in gavs, gavs
    assert ("io.netty", "netty-codec-http", "4.1.111.Final") in gavs, gavs
    assert not any(r["group"] == "com.example" for r in java), gavs
    assert all(r["found_in"][0]["path"] == "build.gradle.kts" for r in java), java
    assert "libs.versions.toml" in " ".join(scan["files"])
    broken = [r for r in java if r["version"] is None]
    assert [(r["name"], r["version_spec"]) for r in broken] == [
        ("libs.broken", "libs.broken")], broken
    assert [w["category"] for w in scan["warnings"]] == ["unresolved_version"], scan["warnings"]
    config = generate_config(scan, "demo")
    rows = _rows(config)
    labels = sorted(r["label"] for r in rows)
    assert labels == ["commons-lang3 3.14.0", "libs.broken",
                      "netty-codec-http 4.1.111.Final"], labels
    print("OK catalog refs resolve end-to-end (direct refs, bundle expansion, dedupe)")


def test_parse_gradle_resolves_passed_catalog():
    """RETARGETED: parse_gradle(p, catalog) tuples became
    parse_gradle_records(p, rel, catalog=...) records; the resolved record
    keeps the reference text as its version_spec."""
    with tempfile.TemporaryDirectory() as tmp:
        toml_p = _write(tmp, "libs.versions.toml", CATALOG_TOML)
        p = _write(tmp, "build.gradle", 'implementation(libs.commons.lang3)\n')
        aliases, bundles, _warnings = parse_version_catalog(toml_p, "libs.versions.toml")
        records, warnings = parse_gradle_records(p, "build.gradle", catalog=(aliases, bundles))
    assert warnings == [], warnings
    assert _gavs(records) == [("org.apache.commons", "commons-lang3", "3.14.0")], records
    assert records[0]["version_spec"] == "libs.commons.lang3", records
    assert records[0]["found_in"] == [{
        "path": "build.gradle", "manifest": "gradle", "line": 1,
        "locator": "dependency:org.apache.commons:commons-lang3"}], records
    print("OK parse_gradle resolves a passed-in catalog directly")


def test_parse_gradle_without_catalog_ignores_refs():
    """New: with no catalog passed, libs.* references stay invisible (the
    discovery layer only passes a catalog when one exists above the file)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", 'implementation(libs.commons.lang3)\n')
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert records == [] and warnings == [], (records, warnings)


def test_fixture_catalog_resolves_nearest_above_build_file():
    """New: the shared gradle fixture declares gradle/libs.versions.toml and
    references libs.commons.lang3 from build.gradle.kts; a nested module
    resolves the same catalog, a sibling tree with its own catalog wins."""
    scan = scan_folder(FIX / "gradle")
    assert "gradle/libs.versions.toml" in scan["files"], scan["files"]
    lang3 = [r for r in scan["records"] if r["artifact"] == "commons-lang3"]
    assert [(r["version"], r["found_in"][0]["path"]) for r in lang3] == [
        ("3.14.0", "build.gradle.kts")], lang3

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "gradle").mkdir()
        _write(root / "gradle", "libs.versions.toml", CATALOG_TOML)
        (root / "app").mkdir()
        _write(root / "app", "build.gradle.kts", "implementation(libs.gson)\n")
        (root / "other" / "gradle").mkdir(parents=True)
        _write(root / "other" / "gradle", "libs.versions.toml",
               '[libraries]\ngson = { module = "org.other:gson-fork", version = "9.9.9" }\n')
        _write(root / "other", "build.gradle", "implementation(libs.gson)\n")
        (root / "settings.gradle.kts").write_text(
            "dependencies { classpath(libs.netty.http) }\n", encoding="utf-8")
        scan = scan_folder(tmp)
    by_path = {r["found_in"][0]["path"]: (r["group"], r["artifact"], r["version"])
               for r in scan["records"]}
    assert by_path["app/build.gradle.kts"] == ("com.google.code.gson", "gson", "2.11.0"), by_path
    assert by_path["other/build.gradle"] == ("org.other", "gson-fork", "9.9.9"), by_path
    assert by_path["settings.gradle.kts"] == ("io.netty", "netty-codec-http", "4.1.111.Final"), by_path
    assert scan["files"] == [
        "app/build.gradle.kts", "gradle/libs.versions.toml", "other/build.gradle",
        "other/gradle/libs.versions.toml", "settings.gradle.kts"], scan["files"]


# --- K4 (pom part): truly empty POM property element -------------------------

def test_truly_empty_pom_property_is_not_a_property():
    """A truly empty element (<logback.version></logback.version>) carries no
    text at all, so the parser never records it as a property: no row and no
    property record either. RETARGETED: parse_pom(p) props dict became
    parse_pom_records(p, rel) records (no property record, no warning)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "pom.xml", """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <properties><logback.version></logback.version></properties>
</project>""")
        records, warnings = parse_pom_records(p, "pom.xml")
    assert not any(r["name"] == "logback.version" for r in records), records
    assert warnings == [], warnings
    print("OK truly empty pom property element is not a property at all")


# --- A: Groovy single-quoted dependency strings -----------------------------

def test_single_and_double_quoted_gav_strings():
    """RETARGETED: parse_gradle(p) (g, a, v, "gradle") tuples became
    parse_gradle_records(p, rel) records with kind "dependency"."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    implementation 'commons-io:commons-io:2.16.1'
    api 'org.apache.commons:commons-lang3:3.14.0'
    classpath 'io.spring.gradle:dependency-management-plugin:1.1.5'
    implementation "com.google.guava:guava:33.0.0-jre"
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert warnings == [], warnings
    deps = _gavs(records)
    assert ("commons-io", "commons-io", "2.16.1") in deps, deps
    assert ("org.apache.commons", "commons-lang3", "3.14.0") in deps, deps
    assert ("io.spring.gradle", "dependency-management-plugin", "1.1.5") in deps, deps
    assert ("com.google.guava", "guava", "33.0.0-jre") in deps, deps
    assert len(deps) == 4, deps
    assert all(r["kind"] == "dependency" for r in records), records
    print("OK single-quoted and double-quoted gradle GAV strings both parse")


# --- B: Groovy map notation and Kotlin DSL named args -----------------------

def test_groovy_map_notation_both_quote_styles():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    implementation group: 'com.google.code.gson', name: 'gson', version: '2.11.0'
    api group: "org.apache.commons", name: "commons-lang3", version: "3.14.0"
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert warnings == [], warnings
    deps = _gavs(records)
    assert ("com.google.code.gson", "gson", "2.11.0") in deps, deps
    assert ("org.apache.commons", "commons-lang3", "3.14.0") in deps, deps
    assert len(deps) == 2, deps
    print("OK groovy map notation parses with both quote styles")


def test_kts_named_arg_form():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle.kts", """
dependencies {
    implementation(group = "io.netty", name = "netty-codec-http", version = "4.1.111.Final")
    implementation(group =
        "ch.qos.logback",
        name = "logback-classic",
        version = "1.5.6")
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle.kts")
    assert warnings == [], warnings
    deps = _gavs(records)
    assert ("io.netty", "netty-codec-http", "4.1.111.Final") in deps, deps
    assert ("ch.qos.logback", "logback-classic", "1.5.6") in deps, deps
    assert len(deps) == 2, deps
    print("OK kts named-arg form parses (single- and multi-line)")


# --- C: platform(...) BOM declarations --------------------------------------

def test_platform_bom_declarations():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle.kts", """
dependencies {
    implementation(platform("org.springframework.boot:spring-boot-dependencies:3.4.5"))
    implementation platform('io.netty:netty-bom:4.1.111.Final')
    api(platform("com.fasterxml.jackson:jackson-bom:2.17.2"))
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle.kts")
    assert warnings == [], warnings
    deps = _gavs(records)
    assert ("org.springframework.boot", "spring-boot-dependencies", "3.4.5") in deps, deps
    assert ("io.netty", "netty-bom", "4.1.111.Final") in deps, deps
    assert ("com.fasterxml.jackson", "jackson-bom", "2.17.2") in deps, deps
    assert len(deps) == 3, deps
    # the root generator did not distinguish platform() wrappers by kind
    assert all(r["kind"] == "dependency" for r in records), records
    print("OK platform(...) BOM declarations parse in kts and groovy forms")

    entry = _map_java_dep("org.springframework.boot", "spring-boot-dependencies", "3.4.5")
    assert entry["product"] == "spring-boot", entry
    assert entry["version"] == "3.4", entry
    print("OK platform BOM maps to the spring-boot endoflife_date row")


# --- D: plugins block --------------------------------------------------------

def _plugins(records):
    return [(r["group"], r["artifact"], r["version"], r["kind"]) for r in records]


def test_kts_plugins_block():
    """RETARGETED: kind "gradle-plugin" tuples became records with kind
    "plugin"."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle.kts", """
plugins {
    id("org.springframework.boot") version "3.4.5" apply false
    kotlin("jvm") version "2.1.20"
    id("java")
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle.kts")
    assert warnings == [], warnings
    deps = _plugins(records)
    assert ("org.springframework.boot", "boot-gradle-plugin", "3.4.5", "plugin") in deps, deps
    assert ("org.jetbrains.kotlin", "kotlin-gradle-plugin", "2.1.20", "plugin") in deps, deps
    assert len(deps) == 2, deps
    assert [r["found_in"][0]["locator"] for r in records] == [
        "plugin:org.springframework.boot", "plugin:org.jetbrains.kotlin.jvm"], records
    print("OK kts plugins block: id(...) and kotlin(...) matched, bare ids ignored")


def test_groovy_plugins_block():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
plugins {
    id 'org.springframework.boot' version '3.4.5'
    id 'java'
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert warnings == [], warnings
    assert _plugins(records) == [
        ("org.springframework.boot", "boot-gradle-plugin", "3.4.5", "plugin")], records
    print("OK groovy plugins block: quoted id form matched, bare ids ignored")

    entry = _map_java_dep("org.jetbrains.kotlin", "kotlin-gradle-plugin", "2.1.20")
    assert "source" not in entry, entry
    assert entry["product"] == "kotlin", entry
    assert entry["version"] == "2.1", entry
    assert entry["label"] == "Kotlin 2.1", entry
    entry = _map_java_dep("org.springframework.boot", "boot-gradle-plugin", "3.4.5")
    assert entry["product"] == "spring-boot", entry
    assert entry["version"] == "3.4", entry
    print("OK plugin coordinates map through the standard java mapping path")


def test_settings_plugin_management_plugins_block():
    """New: pluginManagement { plugins { ... } } in a settings file yields
    plugin records through the settings row."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "settings.gradle.kts", """
pluginManagement {
    plugins {
        id("org.springframework.boot") version "3.4.5"
        kotlin("jvm") version "2.1.20"
    }
}
""")
        scan = scan_folder(tmp)
    assert _plugins(scan["records"]) == [
        ("org.springframework.boot", "boot-gradle-plugin", "3.4.5", "plugin"),
        ("org.jetbrains.kotlin", "kotlin-gradle-plugin", "2.1.20", "plugin")], scan["records"]
    config = generate_config(scan, "demo")
    assert [(p.get("product"), p.get("version")) for p in _rows(config)] == [
        ("spring-boot", "3.4"), ("kotlin", "2.1"), ("spring-security", "6.4")], _rows(config)


# --- D2: plugin-id coordinate alias table ------------------------------------

# io.spring.dependency-management publishes as
# io.spring.gradle:dependency-management-plugin; the generic synthesis would
# guess io.spring.dependency-management:dependency-management-gradle-plugin,
# which is not on Maven Central or normal plugin repos (live-verified: a
# permanent not-found tracker-health error row). Both DSL spellings must
# take the alias coordinates.

def test_plugin_alias_table():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle.kts", """
plugins {
    id("io.spring.dependency-management") version "1.1.7"
}
""")
        records, _warnings = parse_gradle_records(p, "build.gradle.kts")
    assert _plugins(records) == [("io.spring.gradle", "dependency-management-plugin", "1.1.7",
                                  "plugin")], records
    print("OK kts plugins block: io.spring.dependency-management takes the alias coords")

    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
plugins {
    id 'io.spring.dependency-management' version '1.1.7'
}
""")
        records, _warnings = parse_gradle_records(p, "build.gradle")
    assert _plugins(records) == [("io.spring.gradle", "dependency-management-plugin", "1.1.7",
                                  "plugin")], records
    print("OK groovy plugins block: io.spring.dependency-management takes the alias coords")

    entry = _map_java_dep("io.spring.gradle", "dependency-management-plugin", "1.1.7")
    assert entry["source"] == "maven_central", entry
    assert entry["group"] == "io.spring.gradle", entry
    assert entry["artifact"] == "dependency-management-plugin", entry
    assert entry["version"] == "1.1.7", entry
    print("OK alias plugin coordinates map through the standard java mapping path")

    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle.kts", """
plugins {
    id("com.example.foo") version "1.0.0"
}
""")
        records, _warnings = parse_gradle_records(p, "build.gradle.kts")
    assert _plugins(records) == [("com.example.foo", "foo-gradle-plugin", "1.0.0",
                                  "plugin")], records
    print("OK unknown plugin ids keep the generic best-effort synthesis")

    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle.kts", """
plugins {
    id("org.springframework.boot") version "3.4.5"
    kotlin("jvm") version "2.1.20"
}
""")
        records, _warnings = parse_gradle_records(p, "build.gradle.kts")
    deps = _plugins(records)
    assert ("org.springframework.boot", "boot-gradle-plugin", "3.4.5", "plugin") in deps, deps
    assert ("org.jetbrains.kotlin", "kotlin-gradle-plugin", "2.1.20", "plugin") in deps, deps
    assert len(deps) == 2, deps
    print("OK spring-boot and kotlin(jvm) plugin ids unchanged by the alias table")


# --- E: Maven version ranges and Gradle dynamic versions are skipped --------

def test_range_and_dynamic_versions_never_become_rows():
    """RETARGETED: _is_maven_version_range / _is_dynamic_version became the
    parser's single _is_exact_java_version gate, and _map_java_dep(...) is
    None became a versionless record plus an unresolved_version warning (the
    consolidated mapper never sees a non-exact version)."""
    assert not _is_exact_java_version("[2.16.0,)")
    assert not _is_exact_java_version("(1.0,2.0]")
    assert _is_exact_java_version("2.16.1")
    assert not _is_exact_java_version("")
    assert not _is_exact_java_version("2.+")
    assert not _is_exact_java_version("1.2.+")
    assert not _is_exact_java_version("latest.release")
    assert not _is_exact_java_version("latest.integration")
    assert not _is_exact_java_version("latest.version")
    assert _is_exact_java_version("2.16.1")
    print("OK range/dynamic version detection helpers")

    with tempfile.TemporaryDirectory() as tmp:
        lines = "\n".join(
            f"    implementation 'commons-io:commons-io:{bad}'"
            for bad in ("[2.16.0,)", "2.+", "latest.release", "latest.integration", "1.2.+"))
        p = _write(tmp, "build.gradle", "dependencies {\n" + lines + "\n}\n")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert [r["version"] for r in records] == [None] * 5, records
    assert [w["category"] for w in warnings] == ["unresolved_version"] * 5, warnings
    print("OK _map_java_dep skips ranges and dynamic versions")

    # End-to-end: such declarations parse but produce no doomed maven_central
    # row. RETARGETED: the root scan dict became a scan_folder() run; the
    # unresolved declarations surface as manual-review rows (consolidated
    # shape) rather than vanishing.
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    implementation 'commons-io:commons-io:[2.16.0,)'
    implementation 'org.springframework:spring-core:2.+'
    implementation 'com.example:widget:latest.release'
    implementation 'com.example:keeper:1.2.3'
}
""")
        records, _warnings = parse_gradle_records(p, "build.gradle")
        scan = scan_folder(tmp)
    assert len(records) == 4, records
    config = generate_config(scan, "demo")
    rows = _rows(config)
    assert [r["label"] for r in rows if r.get("source") != "manual"] == ["keeper 1.2.3"], rows
    assert [(r["label"], r["version"]) for r in rows if r.get("source") == "manual"] == [
        ("com.example:widget", "latest.release"),
        ("commons-io:commons-io", "[2.16.0,)"),
        ("org.springframework:spring-core", "2.+")], rows
    print("OK ranged/dynamic declarations never become tracker rows")


# --- H: classpath map notation (buildscript blocks) --------------------------

def test_classpath_quoted_and_map_notation():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
buildscript {
    dependencies {
        classpath group: 'com.g', name: 'a', version: '1.0'
        classpath 'io.spring.gradle:dependency-management-plugin:1.1.5'
    }
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert warnings == [], warnings
    deps = _gavs(records)
    assert ("com.g", "a", "1.0") in deps, deps
    assert ("io.spring.gradle", "dependency-management-plugin", "1.1.5") in deps, deps
    assert len(deps) == 2, deps
    print("OK classpath matches both quoted and map-notation forms")


# --- I: classifier / ext version suffixes ------------------------------------

def test_classifier_and_ext_suffixes():
    """RETARGETED (controller ruling): the root skipped classifier variants
    ('1.0:test-jar' -> no row) and truncated ext suffixes in the mapper. The
    consolidated parser strips a trailing :classifier or @ext when the
    preceding segment is an exact version, so the record (and row) carries
    the bare version; anything else stays a versionless record with an
    unresolved_version warning."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    implementation 'com.example:lib:1.0:test-jar'
    implementation 'g:a:1.0@jar'
    implementation 'g:b:@jar'
    implementation 'org.acme:cls:1.0.0:jar'
    implementation "org.acme:interp:${ver}:jar"
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
        scan = scan_folder(tmp)
    assert len(records) == 5, records
    assert [(r["artifact"], r["version"], r["version_spec"]) for r in records] == [
        ("lib", "1.0", None),
        ("a", "1.0", None),
        ("b", None, "@jar"),
        ("cls", "1.0.0", None),
        ("interp", None, "${ver}:jar"),
    ], records
    assert [(w["category"], w["message"]) for w in warnings] == [
        ("unresolved_version", "no exact version for g:b (@jar) at line 5"),
        ("unresolved_version", "no exact version for org.acme:interp (${ver}:jar) at line 7"),
    ], warnings
    config = generate_config(scan, "demo")
    rows = [r for r in _rows(config) if r.get("source") != "manual"]
    assert [r["label"] for r in rows] == ["lib 1.0", "a 1.0", "cls 1.0.0"], rows
    assert rows[1]["version"] == "1.0", rows
    print("OK classifier decls produce no row; @ext row carries the bare version")


# --- J: comment stripping ----------------------------------------------------

def test_strip_gradle_comments():
    """RETARGETED: _strip_gradle_comments lives in
    eol_inventory.parsers.maven_repositories (Task 3); the dependency parser
    masks comments in place with the same quote-aware grammar."""
    assert _strip_gradle_comments('// implementation "g:a:v"') == ""
    assert _strip_gradle_comments('implementation "g:a:v" // upgraded') == 'implementation "g:a:v" '
    assert _strip_gradle_comments('/* implementation "g:a:v" */') == " "
    assert _strip_gradle_comments("/* unterminated to eof") == ""
    stripped = _strip_gradle_comments("// don't ship 'g:a:v'\nimplementation 'g:c:v'")
    assert stripped == "\nimplementation 'g:c:v'", repr(stripped)
    print("OK _strip_gradle_comments handles line/block comments, strings, apostrophes")

    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    // implementation 'com.example:old-lib:1.0'
    implementation "g:a:v" // upgraded
    /* implementation "g:b:v" */
    maven { url = uri("https://plugins.gradle.org/m2/") }
}
""")
        stripped = _strip_gradle_comments(p.read_text(encoding="utf-8"))
        assert 'uri("https://plugins.gradle.org/m2/")' in stripped, stripped
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert warnings == [], warnings
    assert _gavs(records) == [("g", "a", "v")], records
    print("OK commented-out deps yield nothing; URLs inside strings survive")


# --- K: hardened range/dynamic filters ---------------------------------------

def test_hardened_range_and_dynamic_filters():
    """RETARGETED: the root's _is_maven_version_range / _is_dynamic_version
    became _is_exact_java_version. Divergence kept on purpose: the
    consolidated gate treats '1.0+eap' and semver build metadata
    ('1.2.3+build.5') as exact versions (tests/test_generate_config.py pins
    '1.0.0+build.1' as exact); the root skipped any '+'."""
    assert not _is_exact_java_version("[2.16.0")   # unterminated range
    assert not _is_exact_java_version("[2.16.0,)")
    assert not _is_exact_java_version("(1.0,2.0]")
    assert _is_exact_java_version("2.16.1")
    assert not _is_exact_java_version("")
    assert not _is_exact_java_version("latest")          # bare
    assert not _is_exact_java_version("latest.release")
    assert not _is_exact_java_version("latest.integration")
    assert _is_exact_java_version("1.0+eap")             # RETARGETED: exact here
    assert not _is_exact_java_version("2.+")
    assert not _is_exact_java_version("1.2.+")
    assert _is_exact_java_version("1.2.3+build.5")       # RETARGETED: exact here
    assert _is_exact_java_version("2.16.1")
    assert _is_exact_java_version("3.0.0-M1")
    print("OK hardened range/dynamic detection: bare latest, '+', unterminated ranges")

    bad = ("latest", "[2.16.0", "(1.0,", "[2.16.0,)", "2.+",
           "latest.release", "latest.integration", "1.2.+")
    with tempfile.TemporaryDirectory() as tmp:
        lines = "\n".join(f"    implementation 'commons-io:commons-io:{v}'" for v in bad)
        p = _write(tmp, "build.gradle", "dependencies {\n" + lines + "\n}\n")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert [r["version_spec"] for r in records] == list(bad), records
    assert all(r["version"] is None for r in records), records
    assert len(warnings) == len(bad), warnings
    print("OK _map_java_dep skips bare latest, '+' versions, unterminated ranges")

    for good in ("2.16.0", "3.0.0-M1", "1.0-alpha", "33.4.0-jre", "2.21"):
        entry = _map_java_dep("commons-io", "commons-io", good)
        assert entry is not None and entry["version"] == good, (good, entry)
    print("OK legit positives still map: 3.0.0-M1, 1.0-alpha, 33.4.0-jre, 2.21")


# --- K2: any $ in a version is an unresolved placeholder ----------------------

def test_dollar_interpolation_is_unresolved():
    """RETARGETED: _map_java_dep / _map_java_dep_with_reason placeholder
    checks became the parser's interpolation gate; the
    _discovered_dependencies outcome became the _inventory.unmapped reason
    (declarations are ported in a later task)."""
    # Groovy double-quoted interpolation (implementation "g:a:$jacksonVersion")
    # used to slip past the braced-only '${' check and fabricate a phantom
    # maven_central row; ANY '$' must now skip with the placeholder reason.
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    implementation "com.example:lib:$jacksonVersion"
    implementation "com.example:braced:${jacksonVersion}"
    implementation "com.fasterxml.jackson.core:jackson-databind:2.17.0"
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
        scan = scan_folder(tmp)
    assert [(r["group"], r["artifact"], r["version"], r["version_spec"]) for r in records] == [
        ("com.example", "lib", None, "$jacksonVersion"),
        ("com.example", "braced", None, "${jacksonVersion}"),
        ("com.fasterxml.jackson.core", "jackson-databind", "2.17.0", None),
    ], records
    assert [w["category"] for w in warnings] == ["unresolved_version"] * 2, warnings
    print("OK $var and braced ${var} versions both skip as unresolved placeholders")

    config = generate_config(scan, "demo")
    rows = [r for r in _rows(config) if r.get("source") != "manual"]
    assert [r["label"] for r in rows] == ["Jackson Databind 2.17"], rows
    outcomes = {(i["name"], i["version_spec"]): i["reason"]
                for i in config["_inventory"]["unmapped"]}
    assert outcomes[("com.example:lib", "$jacksonVersion")] == (
        "unresolved version expression"), outcomes
    assert outcomes[("com.example:braced", "${jacksonVersion}")] == (
        "unresolved version expression"), outcomes
    print("OK interpolated $var decls produce no row; records cite the placeholder reason")

    # Mixed numeric+placeholder form: '2.16.$minor' (Groovy double-quoted
    # interpolation of just the trailing segment) must skip like a full $var.
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    implementation "org.example:lib:2.16.$minor"
    implementation "com.example:keeper:1.2.3"
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
        scan = scan_folder(tmp)
    assert ("org.example", "lib", None) in _gavs(records), records
    assert [w["category"] for w in warnings] == ["unresolved_version"], warnings
    config = generate_config(scan, "demo")
    rows = [r for r in _rows(config) if r.get("source") != "manual"]
    assert [r["label"] for r in rows] == ["keeper 1.2.3"], rows
    outcomes = {(i["name"], i["version_spec"]): i["reason"]
                for i in config["_inventory"]["unmapped"]}
    assert outcomes[("org.example:lib", "2.16.$minor")] == (
        "unresolved version expression"), outcomes
    print("OK mixed numeric.$var interpolation skips with the placeholder reason")


# --- K3: POM property values that are themselves placeholders ---------------

def _pom_with_property(name, value):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            f'  <properties><{name}>{value}</{name}></properties>\n'
            '</project>\n')


def test_pom_property_placeholder_value_skips():
    """A property whose value is an unresolved placeholder (${undefined.prop})
    used to bypass the $ check in the property-mapping path and fabricate a
    phantom tracker row (probed: "Apache Tomcat ${undefined.prop}" recorded
    as tracked). It must skip with the placeholder reason and produce no row.
    RETARGETED: the root pom_properties scan dict became two poms in a
    scan_folder() run; the _discovered_dependencies outcome became the
    _inventory.unmapped reason."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a").mkdir()
        (root / "b").mkdir()
        _write(root / "a", "pom.xml", _pom_with_property("tomcat.version", "${undefined.prop}"))
        _write(root / "b", "pom.xml", _pom_with_property("tomcat.version", "10.1.28"))
        scan = scan_folder(tmp)
    config = generate_config(scan, "demo")
    rows = [r for r in _rows(config) if r.get("source") != "manual"]
    assert [r["label"] for r in rows] == ["Apache Tomcat 10.1"], rows
    placeholder = [i for i in config["_inventory"]["unmapped"]
                   if i["name"] == "tomcat.version"]
    assert len(placeholder) == 1, placeholder
    assert placeholder[0]["version_spec"] == "${undefined.prop}", placeholder
    assert placeholder[0]["reason"] == "no exact version (${undefined.prop})", placeholder
    tracked = [r for r in scan["records"]
               if r["name"] == "tomcat.version" and r["version"] == "10.1.28"]
    assert len(tracked) == 1 and tracked[0]["kind"] == "property", scan["records"]
    print("OK pom property placeholder value skips; real property still tracks")


# --- K4: blank/whitespace POM property values --------------------------------

def test_pom_property_blank_value_skips():
    """A whitespace-only property value (<logback.version> </logback.version>)
    parses to an empty property and used to fabricate a phantom tracker row
    with an empty version ("Logback Classic "). It must skip with the
    empty-property reason and produce no row. RETARGETED as in K3."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a").mkdir()
        (root / "b").mkdir()
        _write(root / "a", "pom.xml", _pom_with_property("logback.version", " "))
        _write(root / "b", "pom.xml", _pom_with_property("logback.version", "1.5.18"))
        scan = scan_folder(tmp)
    config = generate_config(scan, "demo")
    rows = [r for r in _rows(config) if r.get("source") != "manual"]
    assert [r["label"] for r in rows] == ["Logback Classic 1.5.18"], rows
    blank = [i for i in config["_inventory"]["unmapped"] if i["name"] == "logback.version"]
    assert len(blank) == 1, blank
    # the unmapped item carries neither key: both are empty for a blank value
    assert "version" not in blank[0] and "version_spec" not in blank[0], blank
    assert blank[0]["reason"] == "no version declared", blank
    tracked = [r for r in scan["records"]
               if r["name"] == "logback.version" and r["version"] == "1.5.18"]
    assert len(tracked) == 1 and tracked[0]["kind"] == "property", scan["records"]
    print("OK pom property blank value skips; real property still tracks")


# --- L: test-* configurations and non-dependency declarations ----------------

def test_test_configurations_and_non_dependency_decls_match_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    testImplementation 'junit:junit:4.13.2'
    testCompileOnly 'org.junit.jupiter:junit-jupiter-api:5.10.2'
    testRuntimeOnly 'org.junit.platform:junit-platform-launcher:1.10.2'
    testApi 'com.example:testapi-dep:1.0'
    testFixturesImplementation 'com.example:fixtures-dep:1.0'
    implementation project(":sub")
    implementation files("libs/local.jar")
    implementation fileTree(dir: "libs", include: ["*.jar"])
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert records == [] and warnings == [], (records, warnings)
    print("OK test* configurations and project/files/fileTree decls match nothing")

    # Word-boundary pin: lowercase variants would otherwise slip through on case
    # luck alone (testimplementation contains 'implementation', testapi 'api').
    assert _GRADLE_PATTERN_QUOTED.search("testapi 'g:a:v'") is None
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle",
                   "testapi 'g:a:v'\n"
                   "testimplementation group: 'g', name: 'a', version: 'v'\n")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert records == [] and warnings == [], (records, warnings)
    print("OK word-boundary guard pins test-config exclusion beyond case luck")


# --- M: quoted + map-notation declarations of the same GAV dedupe ------------

def test_quoted_and_map_notation_same_gav_dedupe():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    implementation 'commons-io:commons-io:2.16.1'
    implementation group: 'commons-io', name: 'commons-io', version: '2.16.1'
}
""")
        records, _warnings = parse_gradle_records(p, "build.gradle")
        scan = scan_folder(tmp)
    assert len(records) == 2, records
    config = generate_config(scan, "demo")
    rows = _rows(config)
    assert [r["label"] for r in rows] == ["commons-io 2.16.1"], rows
    assert [loc["line"] for loc in rows[0]["_found_in"]] == [3, 4], rows
    print("OK quoted + map-notation decls of the same GAV dedupe to one row")


# --- N: mismatched quote pairs never match -----------------------------------

def test_mismatched_quotes_never_match():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", """
dependencies {
    implementation "com.example:mixed:a:1.0'
    implementation 'com.example:mixed:b:1.0"
}
""")
        records, warnings = parse_gradle_records(p, "build.gradle")
    assert records == [] and warnings == [], (records, warnings)
    print("OK mixed-quote strings never match")


# --- Fixture: plugin, platform, map-notation declarations flow to products ---

def test_fixture_plugin_platform_map_notation_products():
    """New: the shared gradle fixture's plugins block, platform() BOM, and
    Groovy map-notation declaration each reach the products list."""
    scan = scan_folder(FIX / "gradle")
    kinds = {(r["group"], r["artifact"], r["version"]): r["kind"] for r in scan["records"]}
    assert kinds[("org.jetbrains.kotlin", "kotlin-gradle-plugin", "1.9.22")] == "plugin", kinds
    assert kinds[("org.springframework.boot", "spring-boot-dependencies", "3.2.0")] == "dependency", kinds
    assert kinds[("org.slf4j", "slf4j-api", "2.0.13")] == "dependency", kinds
    config = generate_config(scan, "demo")
    products = {(p.get("product") or p.get("artifact"), p.get("version")) for p in _rows(config)}
    assert {("kotlin", "1.9"), ("spring-boot", "3.2"), ("slf4j-api", "2.0.13")} <= products, products


TESTS = [
    test_pom_namespaced_kinds,
    test_pom_plain_kinds,
    test_generate_config_skips_unversioned_keeps_managed,
    test_version_catalog_parses,
    test_version_catalog_unreadable,
    test_catalog_refs_resolve_end_to_end,
    test_parse_gradle_resolves_passed_catalog,
    test_parse_gradle_without_catalog_ignores_refs,
    test_fixture_catalog_resolves_nearest_above_build_file,
    test_truly_empty_pom_property_is_not_a_property,
    test_single_and_double_quoted_gav_strings,
    test_groovy_map_notation_both_quote_styles,
    test_kts_named_arg_form,
    test_platform_bom_declarations,
    test_kts_plugins_block,
    test_groovy_plugins_block,
    test_settings_plugin_management_plugins_block,
    test_plugin_alias_table,
    test_range_and_dynamic_versions_never_become_rows,
    test_classpath_quoted_and_map_notation,
    test_classifier_and_ext_suffixes,
    test_strip_gradle_comments,
    test_hardened_range_and_dynamic_filters,
    test_dollar_interpolation_is_unresolved,
    test_pom_property_placeholder_value_skips,
    test_pom_property_blank_value_skips,
    test_test_configurations_and_non_dependency_decls_match_nothing,
    test_quoted_and_map_notation_same_gav_dedupe,
    test_mismatched_quotes_never_match,
    test_fixture_plugin_platform_map_notation_products,
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
    print("OK test_inventory_java")
    return 0


if __name__ == "__main__":
    sys.exit(main())
