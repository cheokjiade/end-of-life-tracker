"""Network-free generate_config parsing tests: gradle/pom manifest coverage."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_config import (
    _GRADLE_PATTERN_QUOTED,
    _is_dynamic_version,
    _is_maven_version_range,
    _map_java_dep,
    _map_java_dep_with_reason,
    _strip_gradle_comments,
    parse_gradle,
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


# --- D2: plugin-id coordinate alias table ------------------------------------

# io.spring.dependency-management publishes as
# io.spring.gradle:dependency-management-plugin; the generic synthesis would
# guess io.spring.dependency-management:dependency-management-gradle-plugin,
# which is not on Maven Central or normal plugin repos (live-verified: a
# permanent not-found tracker-health error row). Both DSL spellings must
# take the alias coordinates.

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle.kts", """
plugins {
    id("io.spring.dependency-management") version "1.1.7"
}
""")
    deps = parse_gradle(p)
assert deps == [("io.spring.gradle", "dependency-management-plugin", "1.1.7",
                 "gradle-plugin")], deps
print("OK kts plugins block: io.spring.dependency-management takes the alias coords")

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
plugins {
    id 'io.spring.dependency-management' version '1.1.7'
}
""")
    deps = parse_gradle(p)
assert deps == [("io.spring.gradle", "dependency-management-plugin", "1.1.7",
                 "gradle-plugin")], deps
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
    deps = parse_gradle(p)
assert deps == [("com.example.foo", "foo-gradle-plugin", "1.0.0",
                 "gradle-plugin")], deps
print("OK unknown plugin ids keep the generic best-effort synthesis")

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle.kts", """
plugins {
    id("org.springframework.boot") version "3.4.5"
    kotlin("jvm") version "2.1.20"
}
""")
    deps = parse_gradle(p)
assert ("org.springframework.boot", "boot-gradle-plugin", "3.4.5", "gradle-plugin") in deps, deps
assert ("org.jetbrains.kotlin", "kotlin-gradle-plugin", "2.1.20", "gradle-plugin") in deps, deps
assert len(deps) == 2, deps
print("OK spring-boot and kotlin(jvm) plugin ids unchanged by the alias table")


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


# --- H: classpath map notation (buildscript blocks) --------------------------

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
buildscript {
    dependencies {
        classpath group: 'com.g', name: 'a', version: '1.0'
        classpath 'io.spring.gradle:dependency-management-plugin:1.1.5'
    }
}
""")
    deps = parse_gradle(p)
assert ("com.g", "a", "1.0", "gradle") in deps, deps
assert ("io.spring.gradle", "dependency-management-plugin", "1.1.5", "gradle") in deps, deps
assert len(deps) == 2, deps
print("OK classpath matches both quoted and map-notation forms")


# --- I: classifier / ext version suffixes ------------------------------------

assert _map_java_dep("com.example", "lib", "1.0:test-jar") is None
entry = _map_java_dep("com.example", "lib", "1.0@jar")
assert entry is not None and entry["version"] == "1.0", entry
assert _map_java_dep("com.example", "lib", "@jar") is None
print("OK classifier variants skipped; @ext truncated to the plain version")

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
dependencies {
    implementation 'com.example:lib:1.0:test-jar'
    implementation 'g:a:1.0@jar'
    implementation 'g:b:@jar'
}
""")
    deps = parse_gradle(p)
assert len(deps) == 3, deps
scan = {
    "java": [(g, a, v, str(p), kind) for g, a, v, kind in deps],
    "pom_properties": [],
    "node": [],
    "files": [str(p)],
}
config = generate_config(scan, "demo")
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["a 1.0"], rows
assert rows[0]["version"] == "1.0", rows
print("OK classifier decls produce no row; @ext row carries the bare version")


# --- J: comment stripping ----------------------------------------------------

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
    deps = parse_gradle(p)
assert deps == [("g", "a", "v", "gradle")], deps
print("OK commented-out deps yield nothing; URLs inside strings survive")


# --- K: hardened range/dynamic filters ---------------------------------------

assert _is_maven_version_range("[2.16.0")   # unterminated range
assert _is_maven_version_range("[2.16.0,)")
assert _is_maven_version_range("(1.0,2.0]")
assert not _is_maven_version_range("2.16.1")
assert not _is_maven_version_range("")
assert _is_dynamic_version("latest")          # bare
assert _is_dynamic_version("latest.release")
assert _is_dynamic_version("latest.integration")
assert _is_dynamic_version("1.0+eap")         # gradle '+eap' selector
assert _is_dynamic_version("2.+")
assert _is_dynamic_version("1.2.+")
assert _is_dynamic_version("1.2.3+build.5")   # documented trade-off
assert not _is_dynamic_version("2.16.1")
assert not _is_dynamic_version("3.0.0-M1")
print("OK hardened range/dynamic detection: bare latest, '+', unterminated ranges")

for bad in ("latest", "1.0+eap", "[2.16.0", "(1.0,", "1.2.3+build.5",
            "[2.16.0,)", "2.+", "latest.release", "latest.integration", "1.2.+"):
    assert _map_java_dep("commons-io", "commons-io", bad) is None, bad
print("OK _map_java_dep skips bare latest, '+' versions, unterminated ranges")

for good in ("2.16.0", "3.0.0-M1", "1.0-alpha", "33.4.0-jre", "2.21"):
    entry = _map_java_dep("commons-io", "commons-io", good)
    assert entry is not None and entry["version"] == good, (good, entry)
print("OK legit positives still map: 3.0.0-M1, 1.0-alpha, 33.4.0-jre, 2.21")


# --- K2: any $ in a version is an unresolved placeholder ----------------------

# Groovy double-quoted interpolation (implementation "g:a:$jacksonVersion")
# used to slip past the braced-only '${' check and fabricate a phantom
# maven_central row; ANY '$' must now skip with the placeholder reason.
assert _map_java_dep("com.example", "lib", "$jacksonVersion") is None
assert _map_java_dep("com.example", "lib", "${jacksonVersion}") is None
entry, reason = _map_java_dep_with_reason("com.example", "lib", "$jacksonVersion")
assert entry is None and reason == "unresolved property placeholder", (entry, reason)
print("OK $var and braced ${var} versions both skip as unresolved placeholders")

# End-to-end: the interpolated declarations parse but produce no row, and
# their _discovered_dependencies records cite the placeholder reason.
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
dependencies {
    implementation "com.example:lib:$jacksonVersion"
    implementation "com.example:braced:${jacksonVersion}"
    implementation "com.fasterxml.jackson.core:jackson-databind:2.17.0"
}
""")
    deps = parse_gradle(p)
assert deps == [
    ("com.example", "lib", "$jacksonVersion", "gradle"),
    ("com.example", "braced", "${jacksonVersion}", "gradle"),
    ("com.fasterxml.jackson.core", "jackson-databind", "2.17.0", "gradle"),
], deps
scan = {
    "java": [(g, a, v, str(p), kind) for g, a, v, kind in deps],
    "pom_properties": [],
    "node": [],
    "files": [str(p)],
}
config = generate_config(scan, "demo")
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["Jackson Databind 2.17"], rows
outcomes = {r["decl"]: r["outcome"] for r in config["_discovered_dependencies"]}
assert outcomes["com.example:lib:$jacksonVersion"] == (
    "skipped: unresolved property placeholder"), outcomes
assert outcomes["com.example:braced:${jacksonVersion}"] == (
    "skipped: unresolved property placeholder"), outcomes
print("OK interpolated $var decls produce no row; records cite the placeholder reason")

# Mixed numeric+placeholder form: '2.16.$minor' (Groovy double-quoted
# interpolation of just the trailing segment) must skip like a full $var.
assert _map_java_dep("org.example", "lib", "2.16.$minor") is None
entry, reason = _map_java_dep_with_reason("org.example", "lib", "2.16.$minor")
assert entry is None and reason == "unresolved property placeholder", (entry, reason)
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
dependencies {
    implementation "org.example:lib:2.16.$minor"
    implementation "com.example:keeper:1.2.3"
}
""")
    deps = parse_gradle(p)
assert ("org.example", "lib", "2.16.$minor", "gradle") in deps, deps
scan = {
    "java": [(g, a, v, str(p), kind) for g, a, v, kind in deps],
    "pom_properties": [],
    "node": [],
    "files": [str(p)],
}
config = generate_config(scan, "demo")
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["keeper 1.2.3"], rows
outcomes = {r["decl"]: r["outcome"] for r in config["_discovered_dependencies"]}
assert outcomes["org.example:lib:2.16.$minor"] == (
    "skipped: unresolved property placeholder"), outcomes
print("OK mixed numeric.$var interpolation skips with the placeholder reason")


# --- K3: POM property values that are themselves placeholders ---------------

# A property whose value is an unresolved placeholder (${undefined.prop})
# used to bypass the $ check in the property-mapping path and fabricate a
# phantom tracker row (probed: "Apache Tomcat ${undefined.prop}" recorded
# as tracked). It must skip with the placeholder reason and produce no row.
scan = {
    "java": [],
    "pom_properties": [
        ({"tomcat.version": "${undefined.prop}"}, "pom.xml"),
        ({"tomcat.version": "10.1.28"}, "pom.xml"),
    ],
    "node": [],
    "files": ["pom.xml"],
}
config = generate_config(scan, "demo")
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["Apache Tomcat 10.1"], rows
records = config["_discovered_dependencies"]
placeholder = [r for r in records if r["decl"] == "tomcat.version=${undefined.prop}"]
assert len(placeholder) == 1, records
assert placeholder[0]["kind"] == "property", placeholder
assert placeholder[0]["outcome"] == "skipped: unresolved property placeholder", placeholder
tracked = [r for r in records if r["decl"] == "tomcat.version=10.1.28"]
assert len(tracked) == 1 and tracked[0]["outcome"] == "tracked: Apache Tomcat 10.1", records
print("OK pom property placeholder value skips; real property still tracks")


# --- K4: blank/whitespace POM property values --------------------------------

# A whitespace-only property value (<logback.version> </logback.version>)
# parses to props["logback.version"] == "" and used to fabricate a phantom
# tracker row with an empty version ("Logback Classic "). It must skip with
# the empty-property reason and produce no row.
scan = {
    "java": [],
    "pom_properties": [
        ({"logback.version": " "}, "pom.xml"),
        ({"logback.version": "1.5.18"}, "pom.xml"),
    ],
    "node": [],
    "files": ["pom.xml"],
}
config = generate_config(scan, "demo")
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["Logback Classic 1.5.18"], rows
records = config["_discovered_dependencies"]
blank = [r for r in records if r["decl"] == "logback.version= "]
assert len(blank) == 1, records
assert blank[0]["kind"] == "property", blank
assert blank[0]["outcome"] == "skipped: empty property value", records
tracked = [r for r in records if r["decl"] == "logback.version=1.5.18"]
assert len(tracked) == 1 and tracked[0]["outcome"] == (
    "tracked: Logback Classic 1.5.18"), records
print("OK pom property blank value skips; real property still tracks")

# --- L: test-* configurations and non-dependency declarations ----------------

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
    deps = parse_gradle(p)
assert deps == [], deps
print("OK test* configurations and project/files/fileTree decls match nothing")

# Word-boundary pin: lowercase variants would otherwise slip through on case
# luck alone (testimplementation contains 'implementation', testapi 'api').
assert _GRADLE_PATTERN_QUOTED.search("testapi 'g:a:v'") is None
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle",
               "testapi 'g:a:v'\n"
               "testimplementation group: 'g', name: 'a', version: 'v'\n")
    deps = parse_gradle(p)
assert deps == [], deps
print("OK word-boundary guard pins test-config exclusion beyond case luck")


# --- M: quoted + map-notation declarations of the same GAV dedupe ------------

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
dependencies {
    implementation 'commons-io:commons-io:2.16.1'
    implementation group: 'commons-io', name: 'commons-io', version: '2.16.1'
}
""")
    deps = parse_gradle(p)
assert len(deps) == 2, deps
scan = {
    "java": [(g, a, v, str(p), kind) for g, a, v, kind in deps],
    "pom_properties": [],
    "node": [],
    "files": [str(p)],
}
config = generate_config(scan, "demo")
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["commons-io 2.16.1"], rows
print("OK quoted + map-notation decls of the same GAV dedupe to one row")


# --- N: mismatched quote pairs never match -----------------------------------

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", """
dependencies {
    implementation "com.example:mixed:a:1.0'
    implementation 'com.example:mixed:b:1.0"
}
""")
    deps = parse_gradle(p)
assert deps == [], deps
print("OK mixed-quote strings never match")


print("OK test_generate_parsing (batch: quotes + map notation)")
