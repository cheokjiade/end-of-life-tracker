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
    parse_gradle,
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


# --- C2: dotted block names + the pluginManagement exclusion -----------------

# `project.repositories { ... }` is a dependency-repositories block (the
# dotted spelling used to be missed entirely because the captured name was
# compared verbatim). publishing and pluginManagement repositories stay
# excluded — via an enclosing block or via the dotted spelling itself.
DOTTED = """
project.repositories {
    maven { url = uri("https://project-dotted.example/repo") }
}

publishing.repositories {
    maven { url = uri("https://publish-dotted.example/deploy") }
}

pluginManagement {
    repositories {
        maven { url = uri("https://plugins.example/maven2") }
    }
}

pluginManagement.repositories {
    maven { url = uri("https://plugins-dotted.example/maven2") }
}

repositories {
    maven { url = uri("https://plain.example/repo") }
}
"""
urls = _gradle_repo_urls(DOTTED)
assert urls == [
    "https://project-dotted.example/repo",
    "https://plain.example/repo",
], urls
print("OK dotted project.repositories collected; publishing/pluginManagement (block + dotted) excluded")


# --- C3: settings.gradle(.kts); unterminated blocks ---------------------------

# Modern Gradle declares dependency repositories in settings files under
# dependencyResolutionManagement { repositories { ... } } — collected.
# pluginManagement { repositories { ... } } there holds plugin repos and
# stays excluded.
SETTINGS_KTS = """
pluginManagement {
    repositories {
        maven { url = uri("https://plugins.example/maven2") }
    }
}
dependencyResolutionManagement {
    repositories {
        maven { url = uri("https://settings-deps.example/maven2") }
    }
}
rootProject.name = "demo"
"""
with tempfile.TemporaryDirectory() as tmp:
    _write(tmp, "settings.gradle.kts", SETTINGS_KTS)
    scan = scan_folder(tmp)
assert scan["repositories"] == ["https://settings-deps.example/maven2"], \
    scan["repositories"]
print("OK settings.gradle.kts: dependencyResolutionManagement repos collected; pluginManagement excluded")

# The plain Groovy spelling is scanned for repositories too.
SETTINGS_GROOVY = """
pluginManagement {
    repositories {
        gradlePluginPortal()
        maven { url 'https://plugins-groovy.example/maven2' }
    }
}
dependencyResolutionManagement {
    repositories {
        maven { url 'https://settings-groovy.example/maven2' }
    }
}
rootProject.name = 'demo'
"""
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "settings.gradle", SETTINGS_GROOVY)
    scan = scan_folder(tmp)
assert scan["repositories"] == ["https://settings-groovy.example/maven2"], \
    scan["repositories"]
assert "settings.gradle" in [Path(f).name for f in scan["files"]], scan["files"]
print("OK settings.gradle: dependencyResolutionManagement repos collected; pluginManagement excluded")

# Plain settings content (no plugin blocks) yields no dependency rows via
# the direct dep scanner: the dep patterns require dependency-configuration
# keywords (implementation/api/classpath/...). Plugin blocks differ — pinned
# below.
with tempfile.TemporaryDirectory() as tmp:
    p = _write(tmp, "settings.gradle.kts", SETTINGS_KTS)
    deps = parse_gradle(p)
assert deps == [], deps
print("OK settings files yield no dependency declarations")


# --- C4: settings-file dep-scanning asymmetry (.kts scanned, Groovy not) ------

# settings.gradle.kts matches the *.gradle.kts pass, so it is additionally
# dep/plugin-scanned: a pluginManagement { plugins { ... } } block produces a
# gradle-plugin tracker row (deliberate — such plugins are real, versioned,
# trackable artifacts), while pluginManagement repositories stay excluded.
SETTINGS_KTS_PLUGINS = """
pluginManagement {
    plugins {
        id("com.example.some-plugin") version "1.0.0"
    }
    repositories {
        maven { url = uri("https://plugins.example/maven2") }
    }
}
dependencyResolutionManagement {
    repositories {
        maven { url = uri("https://settings-deps.example/maven2") }
    }
}
rootProject.name = "demo"
"""
with tempfile.TemporaryDirectory() as tmp:
    _write(tmp, "settings.gradle.kts", SETTINGS_KTS_PLUGINS)
    scan = scan_folder(tmp)
assert [(g, a, v, kind) for g, a, v, _p, kind in scan["java"]] == [
    ("com.example.some-plugin", "some-plugin-gradle-plugin", "1.0.0",
     "gradle-plugin"),
], scan["java"]
assert scan["repositories"] == ["https://settings-deps.example/maven2"], \
    scan["repositories"]
print("OK settings.gradle.kts pluginManagement plugin produces the gradle-plugin row")

# The identical Groovy settings.gradle is repository-only: the same plugin
# block produces NO dep rows while repo collection still works.
SETTINGS_GROOVY_PLUGINS = """
pluginManagement {
    plugins {
        id 'com.example.some-plugin' version '1.0.0'
    }
    repositories {
        maven { url 'https://plugins.example/maven2' }
    }
}
dependencyResolutionManagement {
    repositories {
        maven { url 'https://settings-deps.example/maven2' }
    }
}
rootProject.name = 'demo'
"""
with tempfile.TemporaryDirectory() as tmp:
    _write(tmp, "settings.gradle", SETTINGS_GROOVY_PLUGINS)
    scan = scan_folder(tmp)
assert scan["java"] == [], scan["java"]
assert scan["repositories"] == ["https://settings-deps.example/maven2"], \
    scan["repositories"]
print("OK identical Groovy settings.gradle produces no dep rows (repos still collected)")

# An unterminated repositories block yields NOTHING: a block is recorded
# only when its closing brace is seen, so a truncated file silently drops
# the incomplete tail rather than emit a wrong URL.
UNTERMINATED = """
repositories {
    maven { url = uri("https://unterminated.example/repo") }
"""
assert _gradle_repo_urls(UNTERMINATED) == []
print("OK unterminated repositories block yields nothing")


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

# --- F: plugin-coordinate aliases apply to settings.gradle.kts plugin rows ---

SETTINGS_KTS_PLUGIN = """\
pluginManagement {
    plugins {
        id("io.spring.dependency-management") version "1.1.7"
    }
}
dependencyResolutionManagement {
    repositories {
        maven {
            url = uri("https://build.shibboleth.net/nexus/content/repositories/releases/")
        }
    }
}
"""

with tempfile.TemporaryDirectory() as tmp:
    _write(tmp, "settings.gradle.kts", SETTINGS_KTS_PLUGIN)
    scan = scan_folder(tmp)
    assert scan.get("repositories") == [SHIB], scan.get("repositories")
    config = generate_config(scan, "settings-plugin-probe")
    rows = [p for p in config["products"]
            if p.get("artifact") == "dependency-management-plugin"]
    assert len(rows) == 1, config["products"]
    assert rows[0].get("group") == "io.spring.gradle", rows
    assert rows[0].get("version") == "1.1.7", rows
print("OK settings.gradle.kts plugin rows use the coordinate alias table")

print("OK test_generate_repositories")
