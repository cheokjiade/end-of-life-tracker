"""Provider-mapping tests for the inventory scanner.

Moved here from the retired root-generator test files
`tests/test_generate_mappings.py` (Java group:artifact rules, including the
exact Kotlin group match and the Shibboleth repository entries),
`tests/test_generate_npm_mappings.py` (npm table: Next.js major-only cycles,
Vue cycle selection) and `tests/test_generate_jackson_entries.py`
(per-artifact `jackson_lifecycle` rows and their titles). The assertions are
the originals; the imports point at `eol_inventory.mappings` and
`eol_inventory.generate_config`, and the handful of assertions that pinned a
root-only behaviour are retargeted with a one-line note marked
"RETARGETED:". Standalone assertion script: no pytest, no network, no
subprocesses.

Run from the repository root:  python tests/test_inventory_mappings.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HELPER_DIR = Path(__file__).resolve().parents[1] / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory.mappings as mappings
from eol_inventory import generate_config
from eol_inventory.mappings import (
    _NPM_MAPPINGS,
    _jackson_artifact_title,
    _map_java_dep,
    _map_npm_dep,
)
from eol_inventory.models import add_location, new_record


def _scan(records, files):
    return {"root": "/", "root_name": "demo", "files": list(files),
            "records": list(records), "warnings": []}


def _rows(config):
    return [p for p in config["products"] if not p.get("_section")]


# ---------------------------------------------------------------------------
# Java group:artifact rules (from tests/test_generate_mappings.py)
# ---------------------------------------------------------------------------

def test_java_mappings():
    # org.jetbrains.kotlinx is a different group and must fall through to the
    # Maven Central staleness fallback, not the endoflife.date kotlin product.
    entry = _map_java_dep(
        "org.jetbrains.kotlinx", "kotlinx-serialization-json", "1.9.0")
    assert entry["source"] == "maven_central", entry
    assert entry["group"] == "org.jetbrains.kotlinx", entry
    assert entry["artifact"] == "kotlinx-serialization-json", entry
    assert entry["version"] == "1.9.0", entry
    assert "product" not in entry, entry
    print("OK kotlinx-serialization-json maps to maven_central fallback")

    entry = _map_java_dep("org.jetbrains.kotlinx", "kotlinx-coroutines-core", "1.10.2")
    assert entry["source"] == "maven_central", entry
    assert entry["artifact"] == "kotlinx-coroutines-core", entry
    assert "product" not in entry, entry
    print("OK kotlinx-coroutines-core maps to maven_central fallback")

    # The exact Kotlin language group still maps to the endoflife.date product
    # (no "source" key: endoflife_date is the default provider).
    entry = _map_java_dep("org.jetbrains.kotlin", "kotlin-stdlib", "2.1.20")
    assert "source" not in entry, entry
    assert entry["product"] == "kotlin", entry
    assert entry["version"] == "2.1", entry
    assert entry["label"] == "Kotlin 2.1", entry
    print("OK kotlin-stdlib maps to endoflife_date kotlin 2.1")

    entry = _map_java_dep("org.jetbrains.kotlin", "kotlin-gradle-plugin", "1.9.25")
    assert "source" not in entry, entry
    assert entry["product"] == "kotlin", entry
    assert entry["version"] == "1.9", entry
    print("OK kotlin-gradle-plugin maps to endoflife_date kotlin 1.9")

    # OpenSAML / Shibboleth groups are emitted as maven_central entries pointed
    # at the Shibboleth repository, with an ASCII policy note and no product key.
    entry = _map_java_dep("org.opensaml", "opensaml-core-api", "5.1.2")
    assert entry["source"] == "maven_central", entry
    assert entry["repository"] == mappings._SHIBBOLETH_REPOSITORY, entry
    assert "policy_note" in entry and entry["policy_note"].isascii(), entry
    assert "OpenSAML 4 EOL 2024-09-01" in entry["policy_note"], entry
    assert entry["label"] == "opensaml-core-api 5.1.2", entry
    assert "product" not in entry, entry
    print("OK opensaml-core-api maps to the Shibboleth repository")

    entry = _map_java_dep("net.shibboleth.utilities", "java-support", "8.4.0")
    assert entry["source"] == "maven_central", entry
    assert entry["artifact"] == "java-support", entry
    assert entry["repository"] == mappings._SHIBBOLETH_REPOSITORY, entry
    # The OpenSAML-specific EOL parenthetical must not leak onto other
    # Shibboleth-hosted artifacts.
    assert "OpenSAML 4 EOL" not in entry["policy_note"], entry
    assert "product" not in entry, entry
    print("OK java-support maps to the Shibboleth repository")

    entry = _map_java_dep("net.shibboleth.intl", "lib", "1.0.0")
    assert entry["repository"] == mappings._SHIBBOLETH_REPOSITORY, entry
    print("OK net.shibboleth.* prefix maps to the Shibboleth repository")

    entry = _map_java_dep("net.shibboleth", "legacy", "1.0.0")
    assert entry["repository"] == mappings._SHIBBOLETH_REPOSITORY, entry
    print("OK bare net.shibboleth group maps to the Shibboleth repository")

    # The prefix match is bounded: sibling groups sharing the first token are not
    # Shibboleth-hosted and keep the plain Maven Central fallback.
    entry = _map_java_dep("net.shibbolethext", "widget", "1.0.0")
    assert entry["source"] == "maven_central", entry
    assert "repository" not in entry, entry
    assert "policy_note" not in entry, entry
    print("OK net.shibbolethext is not captured by the Shibboleth prefix")

    # Generic fallback deps stay plain Maven Central entries: no repository key.
    entry = _map_java_dep("org.example", "widget", "1.0.0")
    assert entry["source"] == "maven_central", entry
    assert "repository" not in entry, entry
    assert "policy_note" not in entry, entry
    print("OK generic fallback entry is unchanged")


# ---------------------------------------------------------------------------
# npm table (from tests/test_generate_npm_mappings.py)
# ---------------------------------------------------------------------------

def test_npm_mappings():
    # RETARGETED: the root generator dropped `typescript` from its table
    # (endoflife.date has no typescript product) and asserted
    # `"typescript" not in _NPM_MAPPINGS`. The inventory table keeps its own
    # typescript rule, so the assertion is retargeted to pin that rule.
    assert "typescript" in _NPM_MAPPINGS, sorted(_NPM_MAPPINGS)
    entry = _map_npm_dep("typescript", "5.9.2")
    assert entry["product"] == "typescript" and entry["version"] == "5.9", entry
    print("OK typescript keeps the inventory table's own mapping")

    # RETARGETED: the root generator left typescript unmapped, so an exact
    # typescript pin landed in _skipped_npm_packages with no product row. The
    # inventory maps it, so the same scan now yields exactly one row.
    record = new_record("node", "typescript", version="5.9.2", scope="dev")
    add_location(record, "package.json", "npm",
                 locator="devDependencies.typescript")
    config = generate_config(_scan([record], ["package.json"]), "demo")
    assert not config.get("_skipped_npm_packages"), config
    rows = _rows(config)
    assert [r["product"] for r in rows] == ["typescript"], rows
    print("OK typescript dependency produces one inventory row")

    # Next.js cycles on endoflife.date are major-only ('16', '15', ...): a
    # major.minor cycle string made every lookup fail with
    # "Cycle '14.2' not found".
    entry = _map_npm_dep("next", "14.2.15")
    assert "source" not in entry, entry
    assert entry["product"] == "nextjs", entry
    assert entry["version"] == "14", entry
    assert entry["label"] == "Next.js 14", entry
    print("OK next 14.2.15 maps to nextjs major-only cycle 14")

    entry = _map_npm_dep("next", "15.3.3")
    assert entry["product"] == "nextjs", entry
    assert entry["version"] == "15", entry
    assert entry["label"] == "Next.js 15", entry
    print("OK next 15.3.3 maps to nextjs major-only cycle 15")

    entry = _map_npm_dep("next", "^16.1.1")
    assert entry["version"] == "16", entry
    assert entry["label"] == "Next.js 16", entry
    print("OK next range spec is cleaned to a bare major")

    # Vue cycles on endoflife.date are major.minor ("3.5", "3.4", "3.3", "2.7",
    # "3.2", "3.1", "3.0", "2.6" ... "2.0") plus the bare-major cycle "1" -
    # verified live against /api/vue.json; there are NO cycles "3", "2" or
    # "1.0". So: a bare-major spec must be skipped (not guessed into a doomed
    # cycle), a numeric 1.x.y pin (e.g. 1.0.27) maps to cycle "1", non-numeric
    # specs ("1.x", "1.x.y" - no such cycle exists) are skipped, and numeric
    # major.minor specs use major.minor.
    entry = _map_npm_dep("vue", "3.5.3")
    assert "source" not in entry, entry
    assert entry["product"] == "vue", entry
    assert entry["version"] == "3.5", entry
    assert entry["label"] == "Vue 3.5", entry
    print("OK vue 3.5.3 maps to vue major.minor cycle 3.5")

    entry = _map_npm_dep("vue", "2.7.16")
    assert entry["version"] == "2.7", entry
    assert entry["label"] == "Vue 2.7", entry
    print("OK vue 2.7.16 maps to vue major.minor cycle 2.7")

    # (a) bare-major specs have no endoflife.date vue cycle to hit: skip them
    # rather than fabricating a doomed row.
    for bare in ("3", "^3", "2"):
        assert _map_npm_dep("vue", bare) is None, bare
    print("OK vue bare-major specs (3, ^3, 2) are skipped")

    # (b) major 1 exists only as the bare cycle "1" (no 1.x minor cycles).
    entry = _map_npm_dep("vue", "1.0.27")
    assert "source" not in entry, entry
    assert entry["product"] == "vue", entry
    assert entry["version"] == "1", entry
    assert entry["label"] == "Vue 1", entry
    entry = _map_npm_dep("vue", "1.0")
    assert entry["version"] == "1", entry
    assert entry["label"] == "Vue 1", entry
    print("OK vue 1.x pins map to the bare-major cycle 1 (label 'Vue 1')")

    entry = _map_npm_dep("vue", "~1.0.0")
    assert entry is not None and entry["version"] == "1", entry
    assert entry["label"] == "Vue 1", entry
    print("OK vue ~1.0.0 cleans to 1.0.0 and maps to the bare-major cycle 1")

    # (c) non-numeric minor segments: both the major and the minor segment
    # must be numeric before any mapping. '3.x' has no endoflife.date cycle
    # (live /api/vue.json: '1', '2.0'..'2.7', '3.0'..'3.5'), so mapping it
    # fabricated a doomed row; '1.x'/'1.x.y' have no cycle either - major 1
    # exists only as the bare cycle '1'. All must stay unmapped.
    for spec in ("3.x", "^3.x", "2.x", "3.X", "1.x", "1.x.y"):
        assert _map_npm_dep("vue", spec) is None, spec
    print("OK vue non-numeric minor specs (3.x, ^3.x, 2.x, 3.X, 1.x, 1.x.y) are skipped")

    # RETARGETED: the root generator skipped 'v3.5.3' because its
    # _clean_version does not strip a leading 'v', leaving a non-numeric major
    # segment. The inventory's _clean_version strips it, so the spec resolves
    # to the published 3.5 cycle instead of being skipped.
    entry = _map_npm_dep("vue", "v3.5.3")
    assert entry is not None and entry["version"] == "3.5", entry
    assert entry["label"] == "Vue 3.5", entry
    print("OK vue v3.5.3 cleans its leading 'v' and maps to the 3.5 cycle")

    # A pinned 3.5.x spec is numeric in both mapped segments: it maps to the
    # published major.minor cycle '3.5' (the trailing .x is simply ignored).
    entry = _map_npm_dep("vue", "3.5.x")
    assert entry is not None and entry["version"] == "3.5", entry
    assert entry["label"] == "Vue 3.5", entry
    print("OK vue 3.5.x maps to the published 3.5 cycle")

    # End-to-end: the skipped bare-major spec must not produce a tracker row.
    # RETARGETED: the inventory never resolves a range into a package version,
    # so the unresolved spec rides in version_spec (what parsers/node.py emits
    # for "vue": "^3" with no lock evidence) instead of the root's version.
    record = new_record("node", "vue", version=None, version_spec="^3")
    add_location(record, "package.json", "npm", locator="dependencies.vue")
    config = generate_config(_scan([record], ["package.json"]), "demo")
    skipped = [s["name"] for s in config.get("_skipped_npm_packages", [])]
    assert "vue" in skipped, config.get("_skipped_npm_packages")
    # RETARGETED: the root produced no product at all; the inventory files the
    # unmapped spec as a "Needs Manual Review" placeholder, so the assertion
    # pins the absence of a *tracked* vue row instead of an empty product list.
    rows = _rows(config)
    assert all(r.get("product") != "vue" for r in rows), config["products"]
    assert [r.get("source") for r in rows] == ["manual"], config["products"]
    print("OK vue ^3 dependency falls into _skipped_npm_packages, no tracked row")

    # End-to-end: a non-numeric 1.x spec lands in _skipped_npm_packages too -
    # it has no published cycle to map to.
    record = new_record("node", "vue", version=None, version_spec="1.x")
    add_location(record, "package.json", "npm", locator="dependencies.vue")
    config = generate_config(_scan([record], ["package.json"]), "demo")
    skipped = [s["name"] for s in config.get("_skipped_npm_packages", [])]
    assert "vue" in skipped, config.get("_skipped_npm_packages")
    # RETARGETED: same placeholder-row difference as the ^3 case above.
    rows = _rows(config)
    assert all(r.get("product") != "vue" for r in rows), config["products"]
    assert [r.get("source") for r in rows] == ["manual"], config["products"]
    print("OK vue 1.x dependency falls into _skipped_npm_packages, no tracked row")


# ---------------------------------------------------------------------------
# jackson_lifecycle entries (from tests/test_generate_jackson_entries.py)
# ---------------------------------------------------------------------------

def test_jackson_entries():
    # Each com.fasterxml.jackson.* artifact becomes its own jackson_lifecycle
    # row: group + artifact are carried on the entry so the per-artifact dedupe
    # key never collapses two artifacts that share a branch version.
    entry = _map_java_dep("com.fasterxml.jackson.core", "jackson-annotations", "2.21")
    assert entry["source"] == "jackson_lifecycle", entry
    assert entry["version"] == "2.21", entry
    assert entry["label"] == "Jackson Annotations 2.21", entry
    assert entry["group"] == "com.fasterxml.jackson.core", entry
    assert entry["artifact"] == "jackson-annotations", entry
    print("OK jackson-annotations maps to its own jackson_lifecycle row")

    entry = _map_java_dep("com.fasterxml.jackson.core", "jackson-databind", "2.19.1")
    assert entry["source"] == "jackson_lifecycle", entry
    assert entry["version"] == "2.19", entry
    assert entry["label"] == "Jackson Databind 2.19", entry
    assert entry["group"] == "com.fasterxml.jackson.core", entry
    assert entry["artifact"] == "jackson-databind", entry
    print("OK jackson-databind maps to its own jackson_lifecycle row")

    entry = _map_java_dep("com.fasterxml.jackson", "jackson-bom", "2.21")
    assert entry["source"] == "jackson_lifecycle", entry
    assert entry["version"] == "2.21", entry
    assert entry["label"] == "Jackson BOM 2.21", entry
    assert entry["group"] == "com.fasterxml.jackson", entry
    assert entry["artifact"] == "jackson-bom", entry
    print("OK jackson-bom maps to its own jackson_lifecycle row (BOM upper-cased)")

    entry = _map_java_dep("com.fasterxml.jackson", "jackson-parent", "2.0")
    assert entry["source"] == "jackson_lifecycle", entry
    assert entry["version"] == "2.0", entry
    assert entry["label"] == "Jackson Parent 2.0", entry
    assert entry["artifact"] == "jackson-parent", entry
    print("OK jackson-parent derives its label from the artifact id")

    assert _jackson_artifact_title("core") == "Core"
    assert _jackson_artifact_title("jackson-dataformat-xml") == "Dataformat"
    assert _jackson_artifact_title("some-other-tool") == "Some"
    print("OK _jackson_artifact_title edge cases")

    # End-to-end: annotations + bom at the same 2.21 branch, plus a duplicate
    # annotations declaration from a second file, must yield exactly two rows
    # (per-artifact dedupe, first occurrence kept).
    records = []
    for group, artifact, version, path in (
            ("com.fasterxml.jackson.core", "jackson-annotations", "2.21",
             "pom.xml"),
            ("com.fasterxml.jackson", "jackson-bom", "2.21", "pom.xml"),
            ("com.fasterxml.jackson.core", "jackson-annotations", "2.21",
             "child-pom.xml")):
        record = new_record("java", f"{group}:{artifact}", version=version,
                            group=group, artifact=artifact)
        add_location(record, path, "maven",
                     locator=f"dependency:{group}:{artifact}")
        records.append(record)
    config = generate_config(
        _scan(records, ["pom.xml", "child-pom.xml"]), "demo")
    rows = [p for p in config["products"] if p.get("source") == "jackson_lifecycle"]
    assert len(rows) == 2, rows
    labels = sorted(p["label"] for p in rows)
    assert labels == ["Jackson Annotations 2.21", "Jackson BOM 2.21"], labels
    annotations_rows = [p for p in rows if p["artifact"] == "jackson-annotations"]
    assert len(annotations_rows) == 1, rows
    assert annotations_rows[0]["_comment"].startswith("From pom.xml "), rows
    print("OK generate_config keeps 2 per-artifact jackson rows, deduping repeats")


TESTS = [
    test_java_mappings,
    test_npm_mappings,
    test_jackson_entries,
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
    print("OK test_inventory_mappings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
