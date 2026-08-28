"""Network-free generate_config tests: declared maven repository collection.

Covers pom <repositories> (root-level only; profile-conditional repos are
ignored) and gradle `repositories { }` blocks (kts uri(...) / assignment and
groovy assignment / shorthand forms; buildscript included; publishing
excluded), the scan_folder aggregation, and the config-level
maven_repositories emission.
"""

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_config import (
    _gradle_repo_urls,
    generate_config,
    parse_gradle_repositories,
    parse_pom,
    scan_folder,
)

SHIB = "https://build.shibboleth.net/nexus/content/repositories/releases/"
SPRING = "https://repo.spring.io/milestone"


def _write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text, encoding="utf-8")
    return p


# --- A: pom.xml root-level <repositories>; <profiles> ignored ----------------

POM_REPOS = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <repositories>
    <repository>
      <id>shibboleth</id>
      <url>https://build.shibboleth.net/nexus/content/repositories/releases/</url>
      <releases><enabled>true</enabled></releases>
    </repository>
    <repository>
      <id>spring-milestones</id>
      <url>https://repo.spring.io/milestone</url>
    </repository>
    <repository>
      <id>legacy-only</id>
      <url>https://legacy.example/maven2</url>
      <releases><enabled>false</enabled></releases>
    </repository>
  </repositories>
  <profiles>
    <profile>
      <id>conditional</id>
      <repositories>
        <repository>
          <id>profile-only</id>
          <url>https://profile.example/repo</url>
        </repository>
      </repositories>
    </profile>
  </profiles>
  <dependencies>
    <dependency>
      <groupId>commons-io</groupId>
      <artifactId>commons-io</artifactId>
      <version>2.16.1</version>
    </dependency>
  </dependencies>
</project>
"""

with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "pom.xml", POM_REPOS)
    deps, props, repos = parse_pom(p)
assert repos == [
    "https://build.shibboleth.net/nexus/content/repositories/releases/",
    "https://repo.spring.io/milestone",
    "https://legacy.example/maven2",
], repos
assert ("commons-io", "commons-io", "2.16.1", "dep") in deps, deps
print("OK pom: root-level <repositories> collected (releases-enabled state ignored); <profiles> repos ignored")

# A pom without <repositories> yields an empty list.
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "pom.xml", "<project><dependencies/></project>")
    _deps, _props, repos = parse_pom(p)
assert repos == [], repos
print("OK pom: no <repositories> yields no URLs")


# --- B: gradle repositories blocks (kts + groovy forms) -----------------------

KTS_REPOS = """
repositories {
    mavenCentral()
    maven {
        url = uri("https://build.shibboleth.net/nexus/content/repositories/releases/")
    }
    maven { url = uri('https://kts-uri-single.example/repo') }
    maven { url = "https://kts-assign.example/repo" }
    google()
}
"""
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle.kts", KTS_REPOS)
    repos = parse_gradle_repositories(p)
assert repos == [
    "https://build.shibboleth.net/nexus/content/repositories/releases/",
    "https://kts-uri-single.example/repo",
    "https://kts-assign.example/repo",
], repos
print("OK kts: mavenCentral()/google() ignored; uri(...)/assignment forms captured")

GROOVY_REPOS = """
repositories {
    mavenCentral()
    mavenLocal()
    maven { url 'https://repo.spring.io/milestone' }
    maven { url = 'https://groovy-assign.example/repo' }
    maven { url "https://groovy-shorthand.example/repo" }
}
"""
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", GROOVY_REPOS)
    repos = parse_gradle_repositories(p)
assert repos == [
    "https://repo.spring.io/milestone",
    "https://groovy-assign.example/repo",
    "https://groovy-shorthand.example/repo",
], repos
print("OK groovy: mavenLocal() ignored; shorthand/assignment/single-quote forms captured")

# Nested blocks: the repositories body contains a maven block plus more
# content after it — brace counting must keep the whole body.
NESTED = """
repositories {
    maven {
        url = uri("https://nested.example/inside")
    }
    mavenCentral()
    exclusiveContent {
        filter { includeGroup "com.example" }
    }
}
"""
urls = _gradle_repo_urls(NESTED)
assert urls == ["https://nested.example/inside"], urls
print("OK nested blocks: brace counting contains the full repositories body")


# --- C: buildscript included; publishing and non-repo contexts excluded ------

CONTEXTS = """
buildscript {
    repositories {
        maven { url = uri("https://buildscript.example/maven2") }
    }
}

publishing {
    repositories {
        maven { url = uri("https://publish.example/deploy") }
    }
    publications {
        mavenJava(MavenPublication) {
            groupId = "com.example"
        }
    }
}

scm {
    url = "https://scm.example/repo.git"
}

ext.repoUrl = "https://ext.example/repo"
"""
urls = _gradle_repo_urls(CONTEXTS)
assert urls == ["https://buildscript.example/maven2"], urls
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", CONTEXTS)
    repos = parse_gradle_repositories(p)
assert repos == ["https://buildscript.example/maven2"], repos
print("OK buildscript repositories included; publishing and scm URLs excluded")

# Commented-out repositories blocks are ignored (comments stripped first).
COMMENTED = """
// repositories {
//     maven { url = uri("https://commented.example/repo") }
// }
/* repositories { maven { url = uri("https://block-commented.example/repo") } } */
"""
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "build.gradle", COMMENTED)
    repos = parse_gradle_repositories(p)
assert repos == [], repos
print("OK commented-out repositories blocks yield nothing")


# --- D: scan_folder aggregation + config-level maven_repositories ------------

GRADLE_DEP = """
repositories {
    maven { url = uri("%s") }
    maven { url = uri("https://gradle-only.example/repo") }
}

dependencies {
    implementation 'commons-io:commons-io:2.16.1'
}
""" % SHIB

with tempfile.TemporaryDirectory() as tmp:
    _write(tmp, "pom.xml", POM_REPOS)
    _write(tmp, "build.gradle.kts", GRADLE_DEP)
    scan = scan_folder(tmp)

# The Shibboleth URL is declared in both files (identical string after the
# kts uri() wrapper) and must dedupe to one entry, order-stable (pom first).
assert scan["repositories"] == [
    "https://build.shibboleth.net/nexus/content/repositories/releases/",
    "https://repo.spring.io/milestone",
    "https://legacy.example/maven2",
    "https://gradle-only.example/repo",
], scan["repositories"]

config = generate_config(scan, "repoproject")
assert config["maven_repositories"] == scan["repositories"], config.get("maven_repositories")
assert config["maven_repositories"][0].isascii()
comment = "\n".join(config["_comment"])
assert "maven_repositories" in comment, comment
rows = [prod for prod in config["products"] if not prod.get("_section")]
assert [r["label"] for r in rows] == ["commons-io 2.16.1"], rows
print("OK scan_folder aggregates and dedupes across files; config carries maven_repositories + checklist note")

# No declared repositories -> no config key, no checklist note.
with tempfile.TemporaryDirectory() as tmp:
    scan = scan_folder(tmp)
assert scan["repositories"] == [], scan
config = generate_config(scan, "empty")
assert "maven_repositories" not in config, config.keys()
assert "maven_repositories" not in "\n".join(config["_comment"])
print("OK empty scan omits maven_repositories entirely")

# Hand-built scan dicts without the key stay green (backward compatibility).
config = generate_config(
    {"java": [], "pom_properties": [], "node": [], "files": []}, "legacy")
assert "maven_repositories" not in config, config.keys()
print("OK hand-built scan dicts without 'repositories' still generate")


# --- E: CLI summary line ------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    _write(tmp, "build.gradle.kts", KTS_REPOS)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        import generate_config as gc
        sys.argv = ["generate_config.py", tmp, "--name", "cli-probe",
                    "--output", os.path.join(tmp, "out.json")]
        try:
            gc.main()
        except SystemExit as exc:
            assert exc.code in (None, 0), exc.code
    out = buf.getvalue()
assert "Repositories declared: 3" in out, out
print("OK CLI summary reports the declared repository count")

print("OK test_generate_repositories")
