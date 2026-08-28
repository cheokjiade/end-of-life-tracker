"""Network-free generate_config parsing tests: gradle/pom manifest coverage."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_config import (
    _is_dynamic_version,
    _is_maven_version_range,
    _map_java_dep,
    parse_gradle,
    parse_pom,
    generate_config,
)


def _write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text, encoding="utf-8")
    return p


# --- A: Groovy single-quoted dependency strings -----------------------------

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
dependencies {
    implementation 'commons-io:commons-io:2.16.1'
    api 'org.apache.commons:commons-lang3:3.14.0'
    classpath 'io.spring.gradle:dependency-management-plugin:1.1.5'
    implementation "com.google.guava:guava:33.0.0-jre"
}
""")
    deps = parse_gradle(p)
assert ("commons-io", "commons-io", "2.16.1", "gradle") in deps, deps
assert ("org.apache.commons", "commons-lang3", "3.14.0", "gradle") in deps, deps
assert ("io.spring.gradle", "dependency-management-plugin", "1.1.5", "gradle") in deps, deps
assert ("com.google.guava", "guava", "33.0.0-jre", "gradle") in deps, deps
assert len(deps) == 4, deps
print("OK single-quoted and double-quoted gradle GAV strings both parse")


# --- B: Groovy map notation and Kotlin DSL named args -----------------------

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
dependencies {
    implementation group: 'com.google.code.gson', name: 'gson', version: '2.11.0'
    api group: "org.apache.commons", name: "commons-lang3", version: "3.14.0"
}
""")
    deps = parse_gradle(p)
assert ("com.google.code.gson", "gson", "2.11.0", "gradle") in deps, deps
assert ("org.apache.commons", "commons-lang3", "3.14.0", "gradle") in deps, deps
assert len(deps) == 2, deps
print("OK groovy map notation parses with both quote styles")

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
    deps = parse_gradle(p)
assert ("io.netty", "netty-codec-http", "4.1.111.Final", "gradle") in deps, deps
assert ("ch.qos.logback", "logback-classic", "1.5.6", "gradle") in deps, deps
assert len(deps) == 2, deps
print("OK kts named-arg form parses (single- and multi-line)")


# --- C: platform(...) BOM declarations --------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle.kts", """
dependencies {
    implementation(platform("org.springframework.boot:spring-boot-dependencies:3.4.5"))
    implementation platform('io.netty:netty-bom:4.1.111.Final')
    api(platform("com.fasterxml.jackson:jackson-bom:2.17.2"))
}
""")
    deps = parse_gradle(p)
assert ("org.springframework.boot", "spring-boot-dependencies", "3.4.5", "gradle") in deps, deps
assert ("io.netty", "netty-bom", "4.1.111.Final", "gradle") in deps, deps
assert ("com.fasterxml.jackson", "jackson-bom", "2.17.2", "gradle") in deps, deps
assert len(deps) == 3, deps
print("OK platform(...) BOM declarations parse in kts and groovy forms")

entry = _map_java_dep("org.springframework.boot", "spring-boot-dependencies", "3.4.5")
assert entry["product"] == "spring-boot", entry
assert entry["version"] == "3.4", entry
print("OK platform BOM maps to the spring-boot endoflife_date row")


# --- D: plugins block --------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle.kts", """
plugins {
    id("org.springframework.boot") version "3.4.5" apply false
    kotlin("jvm") version "2.1.20"
    id("java")
}
""")
    deps = parse_gradle(p)
assert ("org.springframework.boot", "boot-gradle-plugin", "3.4.5", "gradle-plugin") in deps, deps
assert ("org.jetbrains.kotlin", "kotlin-gradle-plugin", "2.1.20", "gradle-plugin") in deps, deps
assert len(deps) == 2, deps
print("OK kts plugins block: id(...) and kotlin(...) matched, bare ids ignored")

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
plugins {
    id 'org.springframework.boot' version '3.4.5'
    id 'java'
}
""")
    deps = parse_gradle(p)
assert deps == [("org.springframework.boot", "boot-gradle-plugin", "3.4.5", "gradle-plugin")], deps
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


# --- E: Maven version ranges and Gradle dynamic versions are skipped --------

assert _is_maven_version_range("[2.16.0,)")
assert _is_maven_version_range("(1.0,2.0]")
assert not _is_maven_version_range("2.16.1")
assert not _is_maven_version_range("")
assert _is_dynamic_version("2.+")
assert _is_dynamic_version("1.2.+")
assert _is_dynamic_version("latest.release")
assert _is_dynamic_version("latest.integration")
assert _is_dynamic_version("latest.version")
assert not _is_dynamic_version("2.16.1")
print("OK range/dynamic version detection helpers")

for bad in ("[2.16.0,)", "2.+", "latest.release", "latest.integration", "1.2.+"):
    assert _map_java_dep("commons-io", "commons-io", bad) is None, bad
print("OK _map_java_dep skips ranges and dynamic versions")

# End-to-end: such declarations parse but produce no doomed maven_central row.
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
dependencies {
    implementation 'commons-io:commons-io:[2.16.0,)'
    implementation 'org.springframework:spring-core:2.+'
    implementation 'com.example:widget:latest.release'
    implementation 'com.example:keeper:1.2.3'
}
""")
    deps = parse_gradle(p)
assert len(deps) == 4, deps
scan = {
    "java": [(g, a, v, str(p), kind) for g, a, v, kind in deps],
    "pom_properties": [],
    "node": [],
    "files": [str(p)],
}
config = generate_config(scan, "demo")
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["keeper 1.2.3"], rows
print("OK ranged/dynamic declarations never become tracker rows")


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

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "pom.xml", POM_NAMESPACED)
    deps, props = parse_pom(p)
assert ("org.springframework.boot", "spring-boot-starter-parent", "3.3.4", "parent") in deps, deps
assert ("com.fasterxml.jackson", "jackson-bom", "2.17.0", "managed-dep") in deps, deps
assert ("org.springframework.boot", "spring-boot-starter-web", None, "unversioned-dep") in deps, deps
assert ("commons-io", "commons-io", "2.16.1", "dep") in deps, deps
assert not any(a == "junit" for _, a, _, _ in deps), deps
print("OK namespaced pom: parent/managed-dep/unversioned-dep/dep kinds, test scope skipped")

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

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "pom.xml", POM_PLAIN)
    deps, props = parse_pom(p)
assert ("io.netty", "netty-bom", "4.1.111.Final", "managed-dep") in deps, deps
assert ("ch.qos.logback", "logback-classic", None, "unversioned-dep") in deps, deps
print("OK non-namespaced pom: managed-dep and unversioned-dep kinds")

# End-to-end: unversioned deps never map (would crash/doom); managed deps keep
# the current behaviour (jackson-bom -> its own jackson_lifecycle row).
scan = {
    "java": [
        ("org.springframework.boot", "spring-boot-starter-web", None,
         "pom.xml", "unversioned-dep"),
        ("com.fasterxml.jackson", "jackson-bom", "2.17.0", "pom.xml", "managed-dep"),
    ],
    "pom_properties": [],
    "node": [],
    "files": ["pom.xml"],
}
config = generate_config(scan, "demo")
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["Jackson BOM 2.17"], rows
print("OK generate_config skips unversioned deps, keeps managed-dep mapping")


print("OK test_generate_parsing (batch: quotes + map notation)")
