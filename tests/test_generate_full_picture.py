"""Network-free generate_config full-picture tests: _discovered_dependencies.

End-to-end scan_folder + generate_config over a fixture project covering
every outcome class (tracked / duplicate-of / skipped / unmapped) and the
java kinds the scanner emits, asserting that every parsed declaration lands
exactly once in _discovered_dependencies while products stays the deduped
runnable set.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.validation import validate_config
from generate_config import _POM_PROPERTY_MAPPINGS, generate_config, scan_folder


ROOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <properties>
    <tomcat.version>10.1.28</tomcat.version>
  </properties>
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
      <groupId>io.netty</groupId>
      <artifactId>netty-codec-http</artifactId>
      <version>4.1.111.Final</version>
    </dependency>
    <dependency>
      <groupId>com.fasterxml.jackson.core</groupId>
      <artifactId>jackson-databind</artifactId>
      <version>2.17.0</version>
    </dependency>
    <dependency>
      <groupId>commons-io</groupId>
      <artifactId>commons-io</artifactId>
      <version>[2.16.0,)</version>
    </dependency>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>in-flight</artifactId>
      <version>1.0-SNAPSHOT</version>
    </dependency>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>placeholder</artifactId>
      <version>${lib.version}</version>
    </dependency>
    <dependency>
      <groupId>internal.tools</groupId>
      <artifactId>in-house</artifactId>
      <version>1.2.3</version>
    </dependency>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>javax.servlet</groupId>
      <artifactId>javax.servlet-api</artifactId>
      <version>4.0.1</version>
      <scope>provided</scope>
    </dependency>
  </dependencies>
</project>
"""

MODULE_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency>
      <groupId>io.netty</groupId>
      <artifactId>netty-codec-http</artifactId>
      <version>4.1.111.Final</version>
    </dependency>
  </dependencies>
</project>
"""

CATALOG_TOML = """
[libraries]
commons-io = { module = "commons-io:commons-io", version = "2.16.1" }
"""

BUILD_GRADLE = """
dependencies {
    implementation libs.commons.io
}
"""

PACKAGE_JSON = """{
  "dependencies": {
    "react": "^18.2.0",
    "vue": "3",
    "lodash": "4.17.21"
  }
}
"""

KINDS = {
    "parent", "dep", "managed-dep", "unversioned-dep",
    "test-scope-dep", "provided-scope-dep", "system-scope-dep",
    "gradle", "gradle-plugin", "gradle-catalog", "property", "npm",
}


def one(records, decl):
    """The single record for *decl* (declarations must appear exactly once)."""
    hits = [r for r in records if r["decl"] == decl]
    assert len(hits) == 1, (decl, hits)
    return hits[0]


with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    module = Path(tmp) / "module"
    module.mkdir()
    (module / "pom.xml").write_text(MODULE_POM, encoding="utf-8")
    (Path(tmp) / "libs.versions.toml").write_text(CATALOG_TOML, encoding="utf-8")
    (Path(tmp) / "build.gradle").write_text(BUILD_GRADLE, encoding="utf-8")
    (Path(tmp) / "package.json").write_text(PACKAGE_JSON, encoding="utf-8")

    scan = scan_folder(tmp)
    config = generate_config(scan, "fullpix")

records = config["_discovered_dependencies"]
assert records, config

# --- every scanned declaration produced exactly one record ------------------
property_attempts = sum(
    1
    for props, _src in scan["pom_properties"]
    for prop in _POM_PROPERTY_MAPPINGS
    if prop in props
)
total_decls = len(scan["java"]) + len(scan["node"]) + property_attempts
assert len(records) == total_decls == 16, (len(records), total_decls)

# --- tracked: duplicate GAV across two files -> one row, one duplicate-of ---
netty = [r for r in records
         if r["decl"] == "io.netty:netty-codec-http:4.1.111.Final"]
assert len(netty) == 2, netty
assert {r["outcome"] for r in netty} == {
    "tracked: netty-codec-http 4.1.111.Final",
    "duplicate-of: netty-codec-http 4.1.111.Final",
}, netty
assert all(r["kind"] == "dep" and r["file"] == "pom.xml" for r in netty), netty

# --- two distinct jackson artifacts: two rows, two tracked records ----------
r = one(records, "com.fasterxml.jackson:jackson-bom:2.17.0")
assert r["kind"] == "managed-dep", r
assert r["outcome"] == "tracked: Jackson BOM 2.17", r
assert r["file"] == "pom.xml", r
r = one(records, "com.fasterxml.jackson.core:jackson-databind:2.17.0")
assert r["kind"] == "dep", r
assert r["outcome"] == "tracked: Jackson Databind 2.17", r

# --- standardized skip reasons ---------------------------------------------
r = one(records, "commons-io:commons-io:[2.16.0,)")
assert r["kind"] == "dep" and r["outcome"] == "skipped: maven version range", r
r = one(records, "com.example:in-flight:1.0-SNAPSHOT")
assert r["kind"] == "dep" and r["outcome"] == "skipped: SNAPSHOT version", r
r = one(records, "com.example:placeholder:${lib.version}")
assert (r["kind"] == "dep"
        and r["outcome"] == "skipped: unresolved property placeholder"), r
r = one(records, "internal.tools:in-house:1.2.3")
assert r["kind"] == "dep" and r["outcome"] == "skipped: internal group", r
r = one(records, "org.springframework:spring-core:")
assert (r["kind"] == "unversioned-dep"
        and r["outcome"] == "skipped: no version (parent/BOM-managed)"), r
r = one(records, "junit:junit:4.13.2")
assert r["kind"] == "test-scope-dep" and r["outcome"] == "skipped: test scope", r
r = one(records, "javax.servlet:javax.servlet-api:4.0.1")
assert (r["kind"] == "provided-scope-dep"
        and r["outcome"] == "skipped: provided scope"), r

# --- gradle catalog lib ------------------------------------------------------
r = one(records, "commons-io:commons-io:2.16.1")
assert r["kind"] == "gradle-catalog" and r["file"] == "build.gradle", r
assert r["outcome"] == "tracked: commons-io 2.16.1", r

# --- npm: mapped, unmapped (also in _skipped_npm_packages), vue bare-major --
r = one(records, "react@18.2.0")
assert r["kind"] == "npm" and r["outcome"] == "tracked: React 18", r
r = one(records, "lodash@4.17.21")
assert r["kind"] == "npm", r
assert r["outcome"] == "unmapped: see _skipped_npm_packages", r
r = one(records, "vue@3")
assert (r["kind"] == "npm"
        and r["outcome"]
        == "skipped: vue version spec with no matching published cycle"), r
skipped_npm = {s["name"] for s in config["_skipped_npm_packages"]}
assert {"lodash", "vue"} <= skipped_npm, config["_skipped_npm_packages"]

# --- pom property mapping ----------------------------------------------------
r = one(records, "tomcat.version=10.1.28")
assert r["kind"] == "property" and r["file"] == "pom.xml", r
assert r["outcome"] == "tracked: Apache Tomcat 10.1", r

# --- products stays the deduped runnable set --------------------------------
rows = [p for p in config["products"] if not p.get("_section")]
tracked = [r for r in records if r["outcome"].startswith("tracked: ")]
assert len(tracked) == 6, tracked
assert len(rows) == len(tracked), (rows, tracked)
assert sorted(p["label"] for p in rows) == [
    "Apache Tomcat 10.1",
    "Jackson BOM 2.17",
    "Jackson Databind 2.17",
    "React 18",
    "commons-io 2.16.1",
    "netty-codec-http 4.1.111.Final",
], rows

# --- records are well-formed: known kinds, ASCII, no section dividers -------
for r in records:
    assert set(r) == {"decl", "file", "kind", "outcome"}, r
    assert r["kind"] in KINDS, r
    assert r["decl"] and not r["decl"].startswith("==="), r
    assert r["decl"].isascii() and r["file"].isascii(), r
    assert r["kind"].isascii() and r["outcome"].isascii(), r
outcomes = {r["outcome"].split(":", 1)[0] for r in records}
assert outcomes <= {"tracked", "duplicate-of", "skipped", "unmapped"}, outcomes

# --- _comment header carries the tally line ---------------------------------
summary = [ln for ln in config["_comment"]
           if ln.startswith("Declarations discovered:")]
assert summary == [
    "Declarations discovered: 16 (tracked 6, duplicates 1, skipped 8, "
    "unmapped 1) - see _discovered_dependencies for the complete picture."
], summary

# --- the generated config passes structural validation ----------------------
findings = validate_config(config)
assert not [f for f in findings if f["severity"] == "error"], findings

# --- JSON round-trip ----------------------------------------------------------
assert json.loads(json.dumps(config)) == config
print("OK fixture project: 16 declarations, 16 records, 6 deduped rows")
print("OK every outcome class recorded (tracked/duplicate-of/skipped/unmapped)")
print("OK _discovered_dependencies validates and round-trips as JSON")


# --- empty scans omit the key entirely --------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    empty_config = generate_config(scan_folder(tmp), "empty")
assert "_discovered_dependencies" not in empty_config, empty_config.keys()
print("OK empty scan omits _discovered_dependencies")

print("OK test_generate_full_picture")
