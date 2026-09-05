"""Temporary parity gate: consolidated scanner output must be a superset of the
root generate_config.py output on tests/fixtures/generate_config/*.
Deleted together with the root script in the retirement task.

Every gap named in PENDING_CATEGORIES is reported as a `pending[...]` line and
does not fail the run; every other gap is a hard failure. Each task in the
consolidation plan removes one category from PENDING_CATEGORIES, so the
allowlist shrinks to empty before the root script is retired.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "helper_scripts"))

from eol_inventory.mappings import _map_npm_dep  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "generate_config"
PENDING_CATEGORIES = {"declarations"}

# `samples/` holds loose pom files, not project directories; neither generator
# is meant to scan it as a project root.
SKIP_FIXTURES = {"samples"}

# Version-catalog aliases look like `module = "group:artifact"` or a
# `name = "artifact"` field alongside a `group = "..."` field.
_TOML_MODULE_RE = re.compile(r'module\s*=\s*"([^"]+)"')
_TOML_NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')


def _run(cmd, cwd):
    env = dict(os.environ)
    env.setdefault(
        "PYTHONPYCACHEPREFIX",
        os.environ.get("PYTHONPYCACHEPREFIX", str(Path(tempfile.gettempdir()) / "eol_pycache")),
    )
    return subprocess.run(
        [sys.executable] + cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _root_config(fixture, out):
    r = _run(
        [str(REPO / "generate_config.py"), str(fixture), "--name", "parity", "--output", str(out)],
        REPO,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _helper_config(fixture, out):
    r = _run(
        [
            str(REPO / "helper_scripts" / "generate_config.py"),
            str(fixture),
            "--name",
            "parity",
            "--output",
            str(out),
            "--include-transitive",
        ],
        REPO,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _identity(entry):
    """The fields the runtime dispatches on: source, coordinates, version, repository.

    Labels, comments, `_found_in` provenance and `_section` dividers are
    deliberately excluded - they are presentation, not behaviour.
    """
    if "group" in entry:
        return (
            "maven",
            entry.get("source"),
            entry["group"],
            entry["artifact"],
            entry.get("version"),
            entry.get("repository"),
        )
    return (
        "product",
        entry.get("source", "endoflife_date"),
        entry.get("product") or entry.get("package"),
        entry.get("version"),
    )


def _products(config):
    return {
        _identity(e)
        for e in config.get("products", [])
        if isinstance(e, dict) and "_section" not in e
    }


def _npm_tracked(config):
    """Names of npm packages the config tracks as a product entry.

    npm_registry entries carry `package`; lifecycle entries carry `product`
    (the endoflife.date slug, e.g. `react` -> product "react", `@angular/core`
    -> "angular"), so the mapping table is consulted for the slug of every
    root-skipped name rather than guessed from the entry.
    """
    names = set()
    for entry in config.get("products") or []:
        if not isinstance(entry, dict) or "_section" in entry:
            continue
        if entry.get("package"):
            names.add(entry["package"])
        if entry.get("product"):
            names.add(entry["product"])
    return names


def _skipped(config):
    """`_skipped_npm_packages` records keyed on the fields a reviewer acts on.

    Both sides write `{name, version, source}` dicts; the `source` field is the
    manifest that declared the package and is provenance, so it is left out of
    the identity for the same reason `_found_in` is.
    """
    out = set()
    for item in config.get("_skipped_npm_packages") or []:
        if isinstance(item, dict):
            out.add((item.get("name"), item.get("version")))
        else:
            out.add((item, None))
    return out


def compare(fixture_dir):
    with tempfile.TemporaryDirectory() as tmp:
        root = _root_config(fixture_dir, Path(tmp) / "root.json")
        helper = _helper_config(fixture_dir, Path(tmp) / "helper.json")
    missing_products = sorted(str(i) for i in _products(root) - _products(helper))
    missing_repos = sorted(
        set(root.get("maven_repositories") or []) - set(helper.get("maven_repositories") or [])
    )
    # Superset rule for npm: the helper must not silently LOSE a package the
    # root parked in `_skipped_npm_packages`, but tracking it is better than
    # skipping it. A root-skipped name is satisfied when the helper skips it
    # too, or when the helper carries it as a product entry -- an
    # npm_registry row (`package == name`) or the lifecycle product
    # `_map_npm_dep` maps that name to. A name the helper neither skips nor
    # tracks is still a gap.
    helper_skipped = _skipped(helper)
    helper_tracked = _npm_tracked(helper)
    missing_skipped = []
    for name, version in _skipped(root):
        if (name, version) in helper_skipped:
            continue
        if name in helper_tracked:
            continue
        mapped = _map_npm_dep(name, version) if version else None
        if mapped and (mapped.get("product") or mapped.get("package")) in helper_tracked:
            continue
        missing_skipped.append(str((name, version)))
    missing_skipped = sorted(missing_skipped)
    decl_root = {
        (d["decl"], d["file"], d["kind"]) for d in root.get("_discovered_dependencies") or []
    }
    decl_helper = {
        (d["decl"], d["file"], d["kind"])
        for d in (helper.get("_inventory") or {}).get("declarations") or []
    }
    return {
        "missing_products": missing_products,
        "missing_repositories": missing_repos,
        "missing_skipped": missing_skipped,
        "missing_declarations": sorted(str(d) for d in decl_root - decl_helper),
    }


def catalog_artifacts(fixture_dir):
    """Artifact names declared in any `libs.versions.toml` under the fixture."""
    names = set()
    for toml in Path(fixture_dir).rglob("libs.versions.toml"):
        text = toml.read_text(encoding="utf-8", errors="replace")
        for module in _TOML_MODULE_RE.findall(text):
            names.add(module.split(":")[-1])
        names.update(_TOML_NAME_RE.findall(text))
    return {n for n in names if n}


def CATEGORY_OF(key, item="", catalog_names=frozenset()):
    """Which pending category a single gap belongs to.

    A missing product whose artifact is declared in a version catalog is a
    `catalogs` gap (the consolidated scanner cannot yet resolve `libs.*`
    aliases); any other missing product is a `mappings` gap.
    """
    if key == "missing_products":
        return "catalogs" if any(name in item for name in catalog_names) else "mappings"
    return {
        "missing_repositories": "repositories",
        "missing_skipped": "npm_graph",
        "missing_declarations": "declarations",
    }[key]


def main():
    failures = []
    for fixture in sorted(
        p for p in FIXTURES.iterdir() if p.is_dir() and p.name not in SKIP_FIXTURES
    ):
        catalog_names = catalog_artifacts(fixture)
        result = compare(fixture)
        buckets = {}
        for key, items in result.items():
            for item in items:
                buckets.setdefault((CATEGORY_OF(key, item, catalog_names), key), []).append(item)
        for (category, key), items in sorted(buckets.items()):
            if category not in PENDING_CATEGORIES:
                failures.append(f"{fixture.name}: {key} [{category}]: {items}")
            else:
                print(f"pending[{category}] {fixture.name}: {key}: {len(items)} item(s)")
                for item in items:
                    print(f"    {item}")
    if failures:
        print("\n".join(failures))
        return 1
    print("OK test_generator_parity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
