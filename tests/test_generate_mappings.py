"""Network-free Java mapping tests: exact Kotlin group match (bugfix)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_config import _map_java_dep


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

print("OK test_generate_mappings")
