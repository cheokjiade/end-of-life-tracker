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
PENDING_CATEGORIES = set()

# `samples/` holds loose pom files, not project directories; neither generator
# is meant to scan it as a project root.
SKIP_FIXTURES = {"samples"}

# Version-catalog aliases look like `module = "group:artifact"` or a
# `name = "artifact"` field alongside a `group = "..."` field.
_TOML_MODULE_RE = re.compile(r'module\s*=\s*"([^"]+)"')
_TOML_NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')


def _run(cmd, cwd, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
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
    """The root generator's output, lockfile graphs included.

    Run with --resolve-transitive so the root parses package-lock.json (its
    only mode that does), and with PATH pointed at an empty directory so mvn
    and gradle cannot be found: the root then degrades with
    transitive_unavailable notes for the build tools while still parsing
    lockfiles, and no external build tool ever runs from the test suite.
    """
    with tempfile.TemporaryDirectory() as empty_path:
        r = _run(
            [
                str(REPO / "generate_config.py"),
                str(fixture),
                "--name",
                "parity",
                "--output",
                str(out),
                "--resolve-transitive",
            ],
            REPO,
            extra_env={"PATH": empty_path},
        )
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _helper_config(fixture, out):
    """The consolidated scanner's output, run exactly like the root.

    Same --resolve-transitive under an empty PATH: mvn and gradle cannot be
    found, so both generators degrade to lockfile parsing and record the
    unavailable build-tool resolution, and no external build tool ever runs
    from the test suite.
    """
    with tempfile.TemporaryDirectory() as empty_path:
        r = _run(
            [
                str(REPO / "helper_scripts" / "generate_config.py"),
                str(fixture),
                "--name",
                "parity",
                "--output",
                str(out),
                "--resolve-transitive",
            ],
            REPO,
            extra_env={"PATH": empty_path},
        )
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text(encoding="utf-8"))


# --- Declaration identity translation ---------------------------------------
# Both generators record every parsed declaration, but the consolidated
# scanner records one declaration per normalized record while the root
# script recorded one per raw parser hit. Four documented differences are
# translated here rather than papered over; everything else must match.
#
# 1. Kind vocabulary. The normalized record model does not distinguish a
#    <dependencyManagement> entry (root kind "managed-dep") or a version
#    catalog alias (root kind "gradle-catalog") from a plain dependency,
#    so those kinds compare as one class.
# 2. File paths. The root kept only the manifest's basename; the
#    consolidated scanner keeps the repo-relative path (strictly more
#    information), so identities compare on the basename.
# 3. npm lockfile packages. A package the lockfile only confirms is one
#    record whose provenance carries both package.json and
#    package-lock.json, not two declarations, so npm identities compare
#    without the file. Packages only the lock names still have their own
#    declaration and must be present.
# 4. Kinds the consolidated scanner never records at all: pom
#    dependencies with a test/provided/system <scope> and pom
#    dependencies without a <version> are skipped in
#    eol_inventory/parsers/java.py, so they are never records. They are
#    listed in SKIPPED_ROOT_KINDS with the reason.
_KIND_CLASS = {
    "dep": "java-dependency",
    "managed-dep": "java-dependency",
    "gradle": "java-dependency",
    "gradle-catalog": "java-dependency",
    "gradle-plugin": "java-plugin",
    "parent": "java-parent",
    "property": "property",
    "transitive-maven": "transitive-maven",
    "transitive-gradle": "transitive-gradle",
    "npm": "npm",
    "npm-lock": "npm",
}

SKIPPED_ROOT_KINDS = {
    "test-scope-dep": "pom test-scope dependencies are skipped at parse time",
    "provided-scope-dep":
        "pom provided-scope dependencies are skipped at parse time",
    "system-scope-dep":
        "pom system-scope dependencies are skipped at parse time",
    "unversioned-dep":
        "pom dependencies without a <version> are skipped at parse time",
}


def _declaration_identity(declaration):
    """Comparable identity of one declaration (see the notes above)."""
    kind = _KIND_CLASS.get(declaration.get("kind"), declaration.get("kind"))
    decl = declaration.get("decl")
    if kind == "npm":
        return (decl, kind)
    path = str(declaration.get("file") or "").replace("\\", "/")
    return (decl, path.rsplit("/", 1)[-1], kind)


def _declarations(declarations):
    return {_declaration_identity(d) for d in declarations or []
            if isinstance(d, dict)
            and d.get("kind") not in SKIPPED_ROOT_KINDS}


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
    decl_root = _declarations(root.get("_discovered_dependencies"))
    decl_helper = _declarations(
        (helper.get("_inventory") or {}).get("declarations"))
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
