"""Network-free npm mapping tests: typescript slug dropped, nextjs major-only."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_config import _NPM_MAPPINGS, _map_npm_dep, generate_config


# endoflife.date has no typescript product (/api/typescript.json is a 404 and
# the slug is absent from all.json), so mapping it produced a guaranteed
# tracker-health error row on every run. It must not be auto-mapped; unmapped
# packages land in _skipped_npm_packages instead.
assert "typescript" not in _NPM_MAPPINGS, sorted(_NPM_MAPPINGS)
print("OK typescript is no longer auto-mapped")

scan = {
    "java": [],
    "pom_properties": [],
    "node": [("typescript", "5.9.2", "package.json")],
    "files": ["package.json"],
}
config = generate_config(scan, "demo")
skipped = [s["name"] for s in config.get("_skipped_npm_packages", [])]
assert "typescript" in skipped, config.get("_skipped_npm_packages")
rows = [p for p in config["products"] if not p.get("_section")]
assert not rows, config["products"]
print("OK typescript dependency falls into _skipped_npm_packages")


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
# "3.2", "3.1", "3.0", "2.6" ... "2.0") plus the bare-major cycle "1" —
# verified live against /api/vue.json; there are NO cycles "3", "2" or
# "1.0". So: a bare-major spec must be skipped (not guessed into a doomed
# cycle), a numeric 1.x.y pin (e.g. 1.0.27) maps to cycle "1", non-numeric
# specs ("1.x", "1.x.y" — no such cycle exists) are skipped, and numeric
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
# into _skipped_npm_packages instead of fabricating a doomed row.
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

# (c) non-numeric minor segments and v-prefixed specs: both the major and
# the minor segment must be numeric before any mapping. '3.x' has no
# endoflife.date cycle (live /api/vue.json: '1', '2.0'..'2.7',
# '3.0'..'3.5'), so mapping it fabricated a doomed row; 'v3.5.3' splits
# into a non-numeric major 'v3' because _clean_version does not strip a
# leading 'v'. '1.x'/'1.x.y' have no cycle either — major 1 exists only as
# the bare cycle '1'. All must be skipped into _skipped_npm_packages.
for spec in ("3.x", "^3.x", "2.x", "3.X", "v3.5.3", "1.x", "1.x.y"):
    assert _map_npm_dep("vue", spec) is None, spec
print("OK vue non-numeric minor specs (3.x, ^3.x, 2.x, 3.X, 1.x, 1.x.y) and v-prefixed spec are skipped")

# A pinned 3.5.x spec is numeric in both mapped segments: it maps to the
# published major.minor cycle '3.5' (the trailing .x is simply ignored).
entry = _map_npm_dep("vue", "3.5.x")
assert entry is not None and entry["version"] == "3.5", entry
assert entry["label"] == "Vue 3.5", entry
print("OK vue 3.5.x maps to the published 3.5 cycle")

# End-to-end: the skipped bare-major spec must not produce a tracker row.
scan = {
    "java": [],
    "pom_properties": [],
    "node": [("vue", "^3", "package.json")],
    "files": ["package.json"],
}
config = generate_config(scan, "demo")
skipped = [s["name"] for s in config.get("_skipped_npm_packages", [])]
assert "vue" in skipped, config.get("_skipped_npm_packages")
rows = [p for p in config["products"] if not p.get("_section")]
assert not rows, config["products"]
print("OK vue ^3 dependency falls into _skipped_npm_packages, no row")

# End-to-end: a non-numeric 1.x spec lands in _skipped_npm_packages too —
# it has no published cycle to map to.
scan = {
    "java": [],
    "pom_properties": [],
    "node": [("vue", "1.x", "package.json")],
    "files": ["package.json"],
}
config = generate_config(scan, "demo")
skipped = [s["name"] for s in config.get("_skipped_npm_packages", [])]
assert "vue" in skipped, config.get("_skipped_npm_packages")
rows = [p for p in config["products"] if not p.get("_section")]
assert not rows, config["products"]
print("OK vue 1.x dependency falls into _skipped_npm_packages, no row")

print("OK test_generate_npm_mappings")
