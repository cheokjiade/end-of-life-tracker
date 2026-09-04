"""Network-free tests: jackson_lifecycle entries are per-artifact, per-version."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_config import _jackson_artifact_title, _map_java_dep, generate_config


# Each com.fasterxml.jackson.* artifact becomes its own jackson_lifecycle row:
# group + artifact are carried on the entry so the per-artifact dedupe key
# never collapses two artifacts that share a branch version.
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
scan = {
    "java": [
        ("com.fasterxml.jackson.core", "jackson-annotations", "2.21",
         "pom.xml", "dep"),
        ("com.fasterxml.jackson", "jackson-bom", "2.21", "pom.xml", "dep"),
        ("com.fasterxml.jackson.core", "jackson-annotations", "2.21",
         "child-pom.xml", "dep"),
    ],
    "pom_properties": [],
    "node": [],
    "files": ["pom.xml", "child-pom.xml"],
}
config = generate_config(scan, "demo")
rows = [p for p in config["products"] if p.get("source") == "jackson_lifecycle"]
assert len(rows) == 2, rows
labels = sorted(p["label"] for p in rows)
assert labels == ["Jackson Annotations 2.21", "Jackson BOM 2.21"], labels
annotations_rows = [p for p in rows if p["artifact"] == "jackson-annotations"]
assert len(annotations_rows) == 1, rows
assert annotations_rows[0]["_comment"].startswith("From pom.xml "), rows
print("OK generate_config keeps 2 per-artifact jackson rows, deduping repeats")

print("OK test_generate_jackson_entries")
