"""Network-free Java mapping tests: exact Kotlin group match (bugfix)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_config
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


# OpenSAML / Shibboleth groups are emitted as maven_central entries pointed
# at the Shibboleth repository, with an ASCII policy note and no product key.
entry = _map_java_dep("org.opensaml", "opensaml-core-api", "5.1.2")
assert entry["source"] == "maven_central", entry
assert entry["repository"] == generate_config._SHIBBOLETH_REPOSITORY, entry
assert "policy_note" in entry and entry["policy_note"].isascii(), entry
assert "OpenSAML 4 EOL 2024-09-01" in entry["policy_note"], entry
assert entry["label"] == "opensaml-core-api 5.1.2", entry
assert "product" not in entry, entry
print("OK opensaml-core-api maps to the Shibboleth repository")

entry = _map_java_dep("net.shibboleth.utilities", "java-support", "8.4.0")
assert entry["source"] == "maven_central", entry
assert entry["artifact"] == "java-support", entry
assert entry["repository"] == generate_config._SHIBBOLETH_REPOSITORY, entry
# The OpenSAML-specific EOL parenthetical must not leak onto other
# Shibboleth-hosted artifacts.
assert "OpenSAML 4 EOL" not in entry["policy_note"], entry
assert "product" not in entry, entry
print("OK java-support maps to the Shibboleth repository")

entry = _map_java_dep("net.shibboleth.intl", "lib", "1.0.0")
assert entry["repository"] == generate_config._SHIBBOLETH_REPOSITORY, entry
print("OK net.shibboleth.* prefix maps to the Shibboleth repository")

entry = _map_java_dep("net.shibboleth", "legacy", "1.0.0")
assert entry["repository"] == generate_config._SHIBBOLETH_REPOSITORY, entry
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

print("OK test_generate_mappings")
