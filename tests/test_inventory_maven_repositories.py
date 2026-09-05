"""Declared Maven repository collection in the inventory scanner.

Moved from tests/test_generate_repositories.py (root generate_config.py) with
the imports retargeted to helper_scripts/eol_inventory. Covers pom
<repositories> (root-level only; profile-conditional repos are ignored) and
gradle `repositories { }` blocks (kts uri(...) / assignment and groovy
assignment / shorthand forms; buildscript included; publishing excluded), the
scan_folder aggregation into scan["maven_repositories"], and the config-level
maven_repositories emission. Standalone assertion script: no pytest, no
network, no subprocesses.

Run from the repository root:  python tests/test_inventory_maven_repositories.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]
_HELPER_DIR = ROOT / "helper_scripts"
sys.path.insert(0, str(_HELPER_DIR))

from eol_inventory import generate_config, scan_folder
from eol_inventory.parsers.maven_repositories import (
    gradle_repo_urls,
    parse_gradle_repositories,
    parse_pom_repositories,
    repositories_blocks,
)
from generate_config import main as generate_config_main

FIX = ROOT / "tests" / "fixtures" / "generate_config"

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


def test_pom_root_level_repositories():
    """RETARGETED: parse_pom(p) -> (deps, props, repos) became the dedicated
    parse_pom_repositories(path, rel_path) -> (urls, warnings)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "pom.xml", POM_REPOS)
        repos, warnings = parse_pom_repositories(p, "pom.xml")
    assert repos == [
        "https://build.shibboleth.net/nexus/content/repositories/releases/",
        "https://repo.spring.io/milestone",
        "https://legacy.example/maven2",
    ], repos
    assert warnings == [], warnings


def test_pom_dependencies_still_parsed():
    """RETARGETED: the dependency half of the same pom is parsed by
    parse_pom_records (records, not (g, a, v, kind) tuples)."""
    from eol_inventory.parsers.java import parse_pom_records
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "pom.xml", POM_REPOS)
        records, _warnings = parse_pom_records(p, "pom.xml")
    names = {(r["name"], r["version"]) for r in records}
    assert ("commons-io:commons-io", "2.16.1") in names, names


def test_pom_without_repositories():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "pom.xml", "<project><dependencies/></project>")
        repos, warnings = parse_pom_repositories(p, "pom.xml")
    assert repos == [], repos
    assert warnings == [], warnings


def test_pom_unreadable_yields_warning():
    """New: stderr prints in the root script became scan warnings here."""
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "absent.xml"
        repos, warnings = parse_pom_repositories(missing, "absent.xml")
    assert repos == [], repos
    assert len(warnings) == 1, warnings
    assert warnings[0]["category"] == "unreadable_file", warnings
    assert warnings[0]["path"] == "absent.xml", warnings


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

GROOVY_REPOS = """
repositories {
    mavenCentral()
    mavenLocal()
    maven { url 'https://repo.spring.io/milestone' }
    maven { url = 'https://groovy-assign.example/repo' }
    maven { url "https://groovy-shorthand.example/repo" }
}
"""


def test_kts_repository_forms():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle.kts", KTS_REPOS)
        repos, warnings = parse_gradle_repositories(p, "build.gradle.kts")
    assert repos == [
        "https://build.shibboleth.net/nexus/content/repositories/releases/",
        "https://kts-uri-single.example/repo",
        "https://kts-assign.example/repo",
    ], repos
    assert warnings == [], warnings


def test_groovy_repository_forms():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", GROOVY_REPOS)
        repos, warnings = parse_gradle_repositories(p, "build.gradle")
    assert repos == [
        "https://repo.spring.io/milestone",
        "https://groovy-assign.example/repo",
        "https://groovy-shorthand.example/repo",
    ], repos
    assert warnings == [], warnings


def test_gradle_unreadable_yields_warning():
    """New: read errors surface as warnings instead of stderr prints."""
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "absent.gradle"
        repos, warnings = parse_gradle_repositories(missing, "absent.gradle")
    assert repos == [], repos
    assert len(warnings) == 1 and warnings[0]["category"] == "unreadable_file", \
        warnings


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


def test_nested_blocks_brace_counting():
    urls = gradle_repo_urls(NESTED)
    assert urls == ["https://nested.example/inside"], urls
    bodies = [body for body, excluded in repositories_blocks(NESTED)
              if not excluded]
    assert len(bodies) == 1, bodies


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


def test_buildscript_included_publishing_excluded():
    urls = gradle_repo_urls(CONTEXTS)
    assert urls == ["https://buildscript.example/maven2"], urls
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", CONTEXTS)
        repos, _warnings = parse_gradle_repositories(p, "build.gradle")
    assert repos == ["https://buildscript.example/maven2"], repos


COMMENTED = """
// repositories {
//     maven { url = uri("https://commented.example/repo") }
// }
/* repositories { maven { url = uri("https://block-commented.example/repo") } } */
"""


def test_commented_out_blocks_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "build.gradle", COMMENTED)
        repos, _warnings = parse_gradle_repositories(p, "build.gradle")
    assert repos == [], repos


# --- C2: dotted block names + the pluginManagement exclusion -----------------

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


def test_dotted_names_and_plugin_management_exclusion():
    urls = gradle_repo_urls(DOTTED)
    assert urls == [
        "https://project-dotted.example/repo",
        "https://plain.example/repo",
    ], urls


# --- C3: settings.gradle(.kts); unterminated blocks ---------------------------

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


def test_settings_kts_dependency_resolution_management():
    """RETARGETED: scan_folder(...)["declared_repos"]/["repositories"] (root
    key) is scan_folder(...)["maven_repositories"] in the inventory scan."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "settings.gradle.kts", SETTINGS_KTS)
        scan = scan_folder(tmp)
    assert scan["maven_repositories"] == [
        "https://settings-deps.example/maven2"], scan["maven_repositories"]


def test_settings_groovy_dependency_resolution_management():
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "settings.gradle", SETTINGS_GROOVY)
        scan = scan_folder(tmp)
    assert scan["maven_repositories"] == [
        "https://settings-groovy.example/maven2"], scan["maven_repositories"]
    assert "settings.gradle" in [Path(f).name for f in scan["files"]], \
        scan["files"]


def test_settings_files_yield_no_dependency_records():
    """RETARGETED: parse_gradle(p) -> [] became parse_gradle_records(p, rel)
    -> ([], [])."""
    from eol_inventory.parsers.java import parse_gradle_records
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, "settings.gradle.kts", SETTINGS_KTS)
        records, warnings = parse_gradle_records(p, "settings.gradle.kts")
    assert records == [], records
    assert warnings == [], warnings


# --- C4: settings-file scanning; both spellings collect repositories ---------

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


def test_settings_kts_plugin_block_repositories():
    """RETARGETED: the root script synthesized a gradle-plugin row from the
    pluginManagement `id(...) version` declaration; the inventory scanner does
    not parse plugin ids at all (no such record in either spelling), so the
    assertion pins the repository half plus the no-record shape."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "settings.gradle.kts", SETTINGS_KTS_PLUGINS)
        scan = scan_folder(tmp)
    assert scan["records"] == [], scan["records"]
    assert scan["maven_repositories"] == [
        "https://settings-deps.example/maven2"], scan["maven_repositories"]


def test_settings_groovy_plugin_block_repositories():
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "settings.gradle", SETTINGS_GROOVY_PLUGINS)
        scan = scan_folder(tmp)
    assert scan["records"] == [], scan["records"]
    assert scan["maven_repositories"] == [
        "https://settings-deps.example/maven2"], scan["maven_repositories"]


UNTERMINATED = """
repositories {
    maven { url = uri("https://unterminated.example/repo") }
"""


def test_unterminated_block_yields_nothing():
    assert gradle_repo_urls(UNTERMINATED) == []


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


def test_scan_folder_aggregates_and_dedupes():
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "pom.xml", POM_REPOS)
        _write(tmp, "build.gradle.kts", GRADLE_DEP)
        scan = scan_folder(tmp)
    # The Shibboleth URL is declared in both files (identical string after the
    # kts uri() wrapper) and must dedupe to one entry, order-stable (pom
    # first, matching the manifest dispatch order).
    assert scan["maven_repositories"] == [
        "https://build.shibboleth.net/nexus/content/repositories/releases/",
        "https://repo.spring.io/milestone",
        "https://legacy.example/maven2",
        "https://gradle-only.example/repo",
    ], scan["maven_repositories"]

    config = generate_config(scan, "repoproject")
    assert config["maven_repositories"] == scan["maven_repositories"], \
        config.get("maven_repositories")
    assert config["maven_repositories"][0].isascii()
    # RETARGETED: the root script appended a maven_repositories line to the
    # _comment checklist; the inventory writer's checklist is fixed, so the
    # emission is pinned by its documented position instead - after
    # `products`, before `_skipped_npm_packages`/`_inventory`.
    keys = list(config)
    assert keys.index("maven_repositories") == keys.index("products") + 1, keys
    assert keys.index("maven_repositories") < keys.index("_inventory"), keys
    rows = [prod for prod in config["products"] if not prod.get("_section")]
    assert any(r.get("artifact") == "commons-io" for r in rows), rows


def test_empty_scan_omits_maven_repositories():
    with tempfile.TemporaryDirectory() as tmp:
        scan = scan_folder(tmp)
    assert scan["maven_repositories"] == [], scan
    config = generate_config(scan, "empty")
    assert "maven_repositories" not in config, list(config)


def test_scan_dict_without_the_key_still_generates():
    """RETARGETED: hand-built scan dicts use the inventory scan shape."""
    config = generate_config(
        {"root": ".", "root_name": "legacy", "files": [], "records": [],
         "warnings": []}, "legacy")
    assert "maven_repositories" not in config, list(config)


# --- E: CLI end-to-end --------------------------------------------------------

def test_cli_writes_maven_repositories():
    """RETARGETED: the root CLI printed "Repositories declared: N"; the
    inventory CLI has no such summary line, so the end-to-end assertion reads
    the written config instead."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "build.gradle.kts", KTS_REPOS)
        out = os.path.join(tmp, "out.json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = generate_config_main(
                [tmp, "--name", "cli-probe", "--output", out])
        assert code in (None, 0), code
        written = json.loads(Path(out).read_text(encoding="utf-8"))
    assert len(written["maven_repositories"]) == 3, written["maven_repositories"]
    assert written["maven_repositories"][0] == SHIB, \
        written["maven_repositories"]


def test_update_regenerates_maven_repositories():
    """New: --update replaces the list wholesale (a fresh scan is the truth
    about which repositories are declared today)."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "build.gradle", """
repositories {
    maven { url = uri("https://new.invalid/m2") }
}
dependencies {
    implementation 'commons-io:commons-io:2.16.1'
}
""")
        out = os.path.join(tmp, "eol_config.updated.json")
        Path(out).write_text(json.dumps({
            "maven_repositories": ["https://old.invalid/m2"],
            "alert_thresholds_days": [30],
            "products": [],
        }), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = generate_config_main(
                [tmp, "--name", "updated", "--output", out, "--update"])
        assert code in (None, 0), code
        merged = json.loads(Path(out).read_text(encoding="utf-8"))
    assert merged["maven_repositories"] == ["https://new.invalid/m2"], \
        merged["maven_repositories"]


# --- F: shared fixtures -------------------------------------------------------

def test_fixture_repositories_collected():
    """New: the shared generate_config fixtures the parity gate runs on."""
    maven_scan = scan_folder(FIX / "maven_multi")
    assert maven_scan["maven_repositories"] == [
        "https://repo.example.invalid/maven2"], \
        maven_scan["maven_repositories"]
    gradle_scan = scan_folder(FIX / "gradle")
    assert gradle_scan["maven_repositories"] == [
        "https://gradle.example.invalid/m2"], \
        gradle_scan["maven_repositories"]
    config = generate_config(gradle_scan, "gradle-project")
    assert config["maven_repositories"] == [
        "https://gradle.example.invalid/m2"], config["maven_repositories"]


TESTS = [
    test_pom_root_level_repositories,
    test_pom_dependencies_still_parsed,
    test_pom_without_repositories,
    test_pom_unreadable_yields_warning,
    test_kts_repository_forms,
    test_groovy_repository_forms,
    test_gradle_unreadable_yields_warning,
    test_nested_blocks_brace_counting,
    test_buildscript_included_publishing_excluded,
    test_commented_out_blocks_ignored,
    test_dotted_names_and_plugin_management_exclusion,
    test_settings_kts_dependency_resolution_management,
    test_settings_groovy_dependency_resolution_management,
    test_settings_files_yield_no_dependency_records,
    test_settings_kts_plugin_block_repositories,
    test_settings_groovy_plugin_block_repositories,
    test_unterminated_block_yields_nothing,
    test_scan_folder_aggregates_and_dedupes,
    test_empty_scan_omits_maven_repositories,
    test_scan_dict_without_the_key_still_generates,
    test_cli_writes_maven_repositories,
    test_update_regenerates_maven_repositories,
    test_fixture_repositories_collected,
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
    print("OK test_inventory_maven_repositories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
