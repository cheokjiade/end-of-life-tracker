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


# Vue cycles on endoflife.date are major.minor ("3.5", "3.4", "2.7", ... —
# verified live against /api/vue.json; no bare-major cycle exists), so a
# major-only version string missed the actual cycle.
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

print("OK test_generate_npm_mappings")
