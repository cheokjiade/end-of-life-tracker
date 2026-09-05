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
from eol_inventory.models import add_location, new_record
from eol_inventory.parsers.java import (
    parse_gradle_records,
    parse_pom_records,
    parse_version_catalog,
)

FIX = ROOT / "tests" / "fixtures" / "generate_config"


def _write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text, encoding="utf-8")
    return p


def _gavs(records):
    return [(r["group"], r["artifact"], r["version"]) for r in records]


def _rows(config):
    return [prod for prod in config["products"] if not prod.get("_section")]


def _scan_of(*records):
    """A minimal scan dict around hand-built records (replaces the root
    script's (g, a, v, src, kind) tuple scan shape)."""
    return {"root_name": "demo", "files": ["build.gradle"],
            "records": list(records), "warnings": []}


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
    has no managed-dep / unversioned-dep / test-scope-dep kinds: managed
    dependencies are ordinary "dependency" records, and versionless or
    test-scoped dependencies are skipped entirely (java.py docstring)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "pom.xml", POM_NAMESPACED)
        records, warnings = parse_pom_records(p, "pom.xml")
    assert warnings == [], warnings
    deps = [(r["group"], r["artifact"], r["version"], r["kind"]) for r in records]
    assert ("org.springframework.boot", "spring-boot-starter-parent", "3.3.4", "parent") in deps, deps
    assert ("com.fasterxml.jackson", "jackson-bom", "2.17.0", "dependency") in deps, deps
    assert not any(r["artifact"] == "spring-boot-starter-web" for r in records), deps
    assert ("commons-io", "commons-io", "2.16.1", "dependency") in deps, deps
    assert not any(r["artifact"] == "junit" for r in records), deps
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
    assert not any(r["artifact"] == "logback-classic" for r in records), deps
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
