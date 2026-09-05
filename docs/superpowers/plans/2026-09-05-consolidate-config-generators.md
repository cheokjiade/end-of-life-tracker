# Consolidate Config Generators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the six capabilities that exist only in root `generate_config.py` into `helper_scripts/eol_inventory/`, prove parity on the shared fixtures, then delete the root script and its tests.

**Architecture:** Each capability lands in the inventory package next to the parser or table it extends (repositories and transitive resolvers as new modules; catalogs in `java.py`; lockfile graph in `node.py`; mapping rules in `mappings.py`; declarations in `config_writer.py`). A temporary parity gate runs both generators over `tests/fixtures/generate_config/*` with an allowlist of not-yet-ported categories; each task removes its category, and the retirement task deletes the gate with the root script.

**Tech Stack:** Python 3.9+ stdlib only (`re`, `json`, `subprocess`, `tempfile`, `xml.etree`, `pathlib`). Standalone `python tests/<file>.py` scripts, no pytest. Existing inventory test files use a `TESTS = [...]` list; root-script tests are top-level-assert scripts.

**Spec:** `docs/superpowers/specs/2026-09-05-consolidate-config-generators-design.md`

## Global Constraints

1. Work only in `E:\Git\end-of-life-tracker-worktrees\consolidate-generators` on branch `feat/consolidate-config-generators`. Never push; never touch `main`.
2. `eoltracker/` (the runtime) is not modified, except the docstring at `eoltracker/parsers/maven_central.py:55-58` in Task 8.
3. Stdlib only. Python 3.9+ syntax (no `X | Y` annotations). Never execute external tools except inside `eol_inventory/resolvers.py` behind `--resolve-transitive`.
4. TDD: failing test first (command and output recorded), then the change, then green. Never delete or weaken an assertion; a root-script assertion that pins a root-only output shape is retargeted to the consolidated shape with a one-line note.
5. Root-script test code moves, it is not rewritten: copy the assert blocks, change the import to the inventory module, keep the temp-tree fixtures they build. Every moved test lands in a file that `tests/check_test_registration.py` accepts (register in `TESTS` when the file has one).
6. Output keys the runtime reads keep their shapes: top-level `maven_repositories` is a list of URL strings; per-entry `repository` is a string.
7. `--update` merge semantics from PR #37 are unchanged. Task 3 changes only which top-level key is regenerated (`maven_repositories`), Task 7 only drops `_discovered_dependencies`.
8. Commits follow `docs/commit-conventions.md` and end with the trailer `Claude-Session: https://claude.ai/code/session_01Dxjfq5wEbszHksHxbxJZZB`.
9. Bytecode redirected: `PYTHONPYCACHEPREFIX=C:\Users\Me\AppData\Local\Temp\claude\E--Git-end-of-life-tracker\d893da00-b6a6-4d7f-8630-dfd76299366a\scratchpad\pycache`. Every script run with a timeout (300 s for `tests/test_generate_config.py`).
10. Gate at the end of every task: the task's named test files, `python tests/test_generator_parity.py`, and `python tests/check_test_registration.py` exit 0. Gate at the end of the plan: every `tests/test_*.py` and `tests/check_*.py` exits 0, `python -m compileall -q eoltracker helper_scripts tests` exits 0, `python tests/check_agent_docs.py` exits 0.

## File map

| File | Responsibility after this plan |
|---|---|
| `tests/test_generator_parity.py` (new, temporary) | Runs both generators over the fixtures; allowlist of unported categories; deleted in Task 8 |
| `helper_scripts/eol_inventory/mappings.py` | All Java and npm mapping rules, including Jackson provider entries and Vue cycle mapping |
| `helper_scripts/eol_inventory/parsers/maven_repositories.py` (new) | Repository URL collection from pom, `build.gradle(.kts)`, `settings.gradle(.kts)` |
| `helper_scripts/eol_inventory/parsers/java.py` | Adds version-catalog parsing and `libs.*` resolution |
| `helper_scripts/eol_inventory/parsers/node.py` | Adds lockfile graph enumeration as `direct=False` records |
| `helper_scripts/eol_inventory/resolvers.py` (new) | `mvn`/`gradle` runners and output parsers; only caller of `subprocess` |
| `helper_scripts/eol_inventory/discovery.py` | Dispatch rows for catalogs and settings files; collects `maven_repositories` into the scan dict |
| `helper_scripts/eol_inventory/config_writer.py` | Emits `maven_repositories`, `_inventory.declarations`, declaration counts |
| `helper_scripts/eol_inventory/report_writer.py` | Declarations section in view, Markdown, CSV, HTML |
| `helper_scripts/generate_config.py` | `--resolve-transitive` flag; merge regenerates `maven_repositories`, drops `_discovered_dependencies` |
| `tests/test_inventory_mappings.py` (new) | Moved from `test_generate_mappings.py`, `test_generate_npm_mappings.py`, `test_generate_jackson_entries.py` |
| `tests/test_inventory_maven_repositories.py` (new) | Moved from `test_generate_repositories.py` |
| `tests/test_inventory_java.py` (new) | Moved from `test_generate_parsing.py` (pom, gradle, catalog parts) |
| `tests/test_inventory_node.py` | Gains lockfile-graph tests moved from `test_generate_parsing.py` and `test_generate_transitive_parsers.py` |
| `tests/test_inventory_resolvers.py` (new) | Moved from `test_generate_transitive_parsers.py` and `test_generate_transitive_merge.py` |
| `tests/test_inventory_report.py`, `tests/test_generate_config.py` | Gain declarations tests moved from `test_generate_full_picture.py` |

---

### Task 1: Parity gate with a shrinking allowlist

**Files:**
- Create: `tests/test_generator_parity.py`
- Modify: `AGENTS.md` (testing section: one line naming the gate as temporary)

**Interfaces:**
- Produces: `PENDING_CATEGORIES = {"mappings", "repositories", "catalogs", "npm_graph", "declarations"}` at module top. Later tasks each remove one name. `compare(fixture_dir) -> dict` returns `{"missing_products": [...], "missing_repositories": [...], "missing_skipped": [...], "missing_declarations": [...]}` where each list is empty when parity holds.

- [ ] **Step 1: Write the gate so it fails on the current tree**

```python
"""Temporary parity gate: consolidated scanner output must be a superset of the
root generate_config.py output on tests/fixtures/generate_config/*.
Deleted together with the root script in the retirement task."""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "generate_config"
PENDING_CATEGORIES = {"mappings", "repositories", "catalogs", "npm_graph", "declarations"}

def _run(cmd, cwd):
    return subprocess.run([sys.executable] + cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120)

def _root_config(fixture, out):
    r = _run([str(REPO / "generate_config.py"), str(fixture), "--name", "parity", "--output", str(out)], REPO)
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text(encoding="utf-8"))

def _helper_config(fixture, out):
    r = _run([str(REPO / "helper_scripts" / "generate_config.py"), str(fixture), "--name", "parity", "--output", str(out), "--include-transitive"], REPO)
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text(encoding="utf-8"))

def _identity(entry):
    if "group" in entry:
        return ("maven", entry.get("source"), entry["group"], entry["artifact"], entry.get("version"), entry.get("repository"))
    return ("product", entry.get("source", "endoflife_date"), entry.get("product") or entry.get("package"), entry.get("version"))

def _products(config):
    return {_identity(e) for e in config.get("products", []) if isinstance(e, dict) and "_section" not in e}

def compare(fixture_dir):
    with tempfile.TemporaryDirectory() as tmp:
        root = _root_config(fixture_dir, Path(tmp) / "root.json")
        helper = _helper_config(fixture_dir, Path(tmp) / "helper.json")
    missing_products = sorted(str(i) for i in _products(root) - _products(helper))
    missing_repos = sorted(set(root.get("maven_repositories", [])) - set(helper.get("maven_repositories", [])))
    missing_skipped = sorted(set(root.get("_skipped_npm_packages", [])) - set(helper.get("_skipped_npm_packages", [])))
    decl_root = {(d["decl"], d["file"], d["kind"]) for d in root.get("_discovered_dependencies", [])}
    decl_helper = {(d["decl"], d["file"], d["kind"]) for d in helper.get("_inventory", {}).get("declarations", [])}
    return {"missing_products": missing_products, "missing_repositories": missing_repos,
            "missing_skipped": missing_skipped, "missing_declarations": sorted(str(d) for d in decl_root - decl_helper)}

CATEGORY_OF = {"missing_products": "mappings", "missing_repositories": "repositories",
               "missing_skipped": "npm_graph", "missing_declarations": "declarations"}

def main():
    failures = []
    for fixture in sorted(p for p in FIXTURES.iterdir() if p.is_dir() and p.name != "samples"):
        result = compare(fixture)
        for key, items in result.items():
            if items and CATEGORY_OF[key] not in PENDING_CATEGORIES:
                failures.append(f"{fixture.name}: {key}: {items}")
            elif items:
                print(f"pending[{CATEGORY_OF[key]}] {fixture.name}: {key}: {len(items)} item(s)")
    if failures:
        print("\n".join(failures)); return 1
    print("OK test_generator_parity"); return 0

if __name__ == "__main__":
    sys.exit(main())
```

Note: `missing_products` covers both `mappings` (Jackson, Vue) and `catalogs` (catalog-resolved Gradle deps). Task 2 removes `"mappings"` only after confirming the remaining missing products on `gradle/` are catalog-resolved; Task 4 removes `"catalogs"`. To make that separable, `CATEGORY_OF["missing_products"]` is a function in the final version: return `"catalogs"` when the missing identity's artifact appears in a `libs.versions.toml` under the fixture, else `"mappings"`. Implement that function in this task (read the toml with the same `_TOML_FIELD_RE` approach as `generate_config.py:872`, or a simple `re.findall(r'module\s*=\s*"([^"]+)"')`).

- [ ] **Step 2: Run it**

Run: `python tests/test_generator_parity.py`
Expected: exit 0 with several `pending[...]` lines (every gap is allowlisted). If it fails with a non-pending failure, the identity function is wrong; fix before continuing.

- [ ] **Step 3: Register in AGENTS.md** testing section: "`tests/test_generator_parity.py` — temporary: both generators over the shared fixtures; removed with the root script." Run `python tests/check_agent_docs.py`.

- [ ] **Step 4: Commit** `test(parity): gate consolidated scanner output against the root generator`

---

### Task 2: Port mapping rules (Jackson provider, Vue cycles, npm table, Jackson titles)

**Files:**
- Modify: `helper_scripts/eol_inventory/mappings.py` (`_JAVA_MAPPINGS` at :60, `_NPM_MAPPINGS` at :181, `_map_npm_dep` :225)
- Create: `tests/test_inventory_mappings.py` (moved from `tests/test_generate_mappings.py`, `tests/test_generate_npm_mappings.py`, `tests/test_generate_jackson_entries.py`)
- Delete: those three root test files
- Modify: `tests/test_generator_parity.py` (remove `"mappings"` from `PENDING_CATEGORIES`)

**Interfaces:**
- Consumes: `_eol_entry(product, version, label)`, `_mc_entry(group, artifact, version, label)`, `_major_minor(v)` from `mappings.py`.
- Produces: `_jackson_entry(group, artifact, version) -> dict` returning `{"source": "jackson_lifecycle", "group", "artifact", "version": <major.minor>, "label": f"Jackson {title} {mm}"}`; `_jackson_artifact_title(artifact) -> str` (copied from `generate_config.py:163-171`); `_vue_cycle(version) -> Optional[str]` (logic of `generate_config.py:323-346`: needs at least two numeric segments; `1.x` maps to `"1"`; else `major.minor`; bare major, `3.x`, `v3.5.3` return `None`). `_map_java_dep` gains a rule `(lambda g, a: g.startswith("com.fasterxml.jackson"), _jackson_entry)` placed before any generic `com.fasterxml` rule. Also port `_shibboleth_mc_entry(group, artifact, version)` and its rule (`generate_config.py:159-174`): the entry is `_mc_entry(...)` plus `"repository": "<the _SHIBBOLETH_REPOSITORY constant value>"` and the `policy_note` text, so the per-entry `repository` key the runtime reads (`eoltracker/parsers/maven_central.py:331-355`) is produced; if `mappings.py` already has a Shibboleth rule, keep whichever sets `repository` and assert on it. `_NPM_MAPPINGS` gains the keys from `generate_config.py:349-368` that it lacks (`react-dom -> None`, `vue` via `_vue_cycle`, `@angular/core`, `next`, `nuxt`, `node`, `express`, `ckeditor`, `@ckeditor/ckeditor5-core`); for a key present on both sides with different rules, keep the inventory rule and add the root assertion as a retargeted test with a note.

- [ ] **Step 1: Create `tests/test_inventory_mappings.py`** by concatenating the three root test files' bodies. Change `from generate_config import ...` to `from eol_inventory.mappings import ...` with `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "helper_scripts"))` at the top (copy the header of `tests/test_inventory_redaction.py` for the path setup). Wrap each original file's top-level asserts in one function per file (`test_java_mappings`, `test_npm_mappings`, `test_jackson_entries`) and add `TESTS = [...]` plus the standard runner loop from `tests/test_inventory_redaction.py`.

- [ ] **Step 2: Run** `python tests/test_inventory_mappings.py`. Expected: FAIL with `ImportError` for `_jackson_entry` / `_vue_cycle` or `AssertionError` on Jackson/Vue mappings. Record the first failure line.

- [ ] **Step 3: Implement** `_jackson_artifact_title`, `_jackson_entry`, `_vue_cycle` in `mappings.py`, add the Jackson rule to `_JAVA_MAPPINGS`, extend `_NPM_MAPPINGS`. Keep the functions' bodies identical to the root script's lines cited above.

- [ ] **Step 4: Run** `python tests/test_inventory_mappings.py`, `python tests/test_generate_config.py`, `python tests/test_inventory_node.py`. Expected: all `OK`.

- [ ] **Step 5: Remove `"mappings"` from `PENDING_CATEGORIES`**, run `python tests/test_generator_parity.py`. Expected: OK; only `pending[catalogs|repositories|npm_graph|declarations]` lines remain.

- [ ] **Step 6: Delete** the three root test files; run `python tests/check_test_registration.py`.

- [ ] **Step 7: Commit** `feat(inventory): port Jackson, Vue, and npm mapping rules from the root generator`

---

### Task 3: Maven repository collection and emission

**Files:**
- Create: `helper_scripts/eol_inventory/parsers/maven_repositories.py`
- Modify: `helper_scripts/eol_inventory/discovery.py` (`_MANIFEST_PATTERNS` :122-136, scan dict :289-295)
- Modify: `helper_scripts/eol_inventory/config_writer.py` (config dict :421-445)
- Modify: `helper_scripts/generate_config.py` (`_merge_existing_config` :321-324)
- Create: `tests/test_inventory_maven_repositories.py` (moved from `tests/test_generate_repositories.py`)
- Delete: `tests/test_generate_repositories.py`
- Modify: `tests/test_generator_parity.py` (remove `"repositories"`)

**Interfaces:**
- Produces in `maven_repositories.py`: `repositories_blocks(text) -> list[tuple[str, bool]]` (from `generate_config.py:766`), `gradle_repo_urls(text) -> list[str]` (:830), `parse_gradle_repositories(path, rel_path) -> tuple[list[str], list[dict]]` (:851, warnings via `new_warning("unreadable", rel_path, msg)` instead of stderr), `parse_pom_repositories(path, rel_path) -> tuple[list[str], list[dict]]` (root-level `<repositories><repository><url>` only, as `generate_config.py:579-589`; uses `models.load_safe_xml`). Order-stable, deduplicated.
- Produces in `discovery.py`: `scan["maven_repositories"]: list[str]` collected from every `pom*.xml`, `build.gradle(.kts)`, and `settings.gradle(.kts)` in discovery order, deduplicated. `settings.gradle(.kts)` gets its own `_MANIFEST_PATTERNS` row with ecosystem `"gradle"` and parser `parse_settings_gradle` that returns `([], warnings)` and records URLs into `scan_state["gradle"]["repositories"]` (row needs `wants_state=True`). Pom and gradle parsers keep their signatures; repository collection for them runs in `discovery.py` right after the parser call, by calling the new functions on the same `(abs, rel)`.
- Produces in `config_writer.py`: `config["maven_repositories"] = scan["maven_repositories"]` emitted only when non-empty, placed after `products` and before `_skipped_npm_packages`.
- Produces in `generate_config.py`: `_merge_existing_config` treats `maven_repositories` as regenerated (add it to the tuple at :321-324) so a fresh scan replaces the list.

- [ ] **Step 1: Create `tests/test_inventory_maven_repositories.py`** from `tests/test_generate_repositories.py`: same temp-tree fixtures, imports switched to `from eol_inventory.parsers.maven_repositories import repositories_blocks, gradle_repo_urls, parse_gradle_repositories, parse_pom_repositories` and `from eol_inventory.discovery import scan_folder`. Assertions that read `scan_folder(...)["declared_repos"]` (root name) are retargeted to `scan_folder(...)["maven_repositories"]` with a note. Add a new test: an existing config with `maven_repositories: ["https://old.invalid/m2"]` updated with `--update` against a tree declaring `https://new.invalid/m2` yields only the new URL. Wrap in functions, `TESTS` list.

- [ ] **Step 2: Run** it. Expected: `ImportError: cannot import name ...`.

- [ ] **Step 3: Implement** the module (move the three functions and the pom loop verbatim, replacing `print(..., file=sys.stderr)` with warnings), the discovery row and collection, the emission, the merge change.

- [ ] **Step 4: Run** `python tests/test_inventory_maven_repositories.py`, `python tests/test_generate_config.py`, `python tests/test_inventory_integration.py`, `python tests/test_helper_wrappers.py`. Expected: OK.

- [ ] **Step 5: Remove `"repositories"`** from the parity allowlist; run the gate. Expected: OK.

- [ ] **Step 6: Delete** `tests/test_generate_repositories.py`; run the registration check.

- [ ] **Step 7: Commit** `feat(inventory): collect declared Maven repositories for the runtime fallback`

---

### Task 4: Gradle version catalogs

**Files:**
- Modify: `helper_scripts/eol_inventory/parsers/java.py` (`parse_gradle_records` :195, `emit` :251)
- Modify: `helper_scripts/eol_inventory/discovery.py` (gradle row :124 gains `wants_state=True`; new row for `gradle/libs.versions.toml`)
- Create: `tests/test_inventory_java.py` (moved from `tests/test_generate_parsing.py`: pom, gradle, and catalog assertions; npm-lock assertions go to Task 5)
- Delete: `tests/test_generate_parsing.py` (after Task 5 has taken its npm-lock part; see Step 6)
- Modify: `tests/test_generator_parity.py` (remove `"catalogs"`)

**Interfaces:**
- Produces in `java.py`: `parse_version_catalog(path, rel_path) -> tuple[dict, dict, list]` returning `(aliases, bundles, warnings)` with `aliases: {norm_alias: (group, artifact, version)}`, `bundles: {norm_name: [norm_alias]}` (logic of `generate_config.py:875-993`; unreadable file yields empty dicts plus an `unreadable` warning). `parse_gradle_records(path, rel_path, catalog=None)`: when `catalog` is given, `libs.*` references (regex `_CATALOG_REF_RE` from :871) are resolved with `_resolve_catalog_refs` and emitted as ordinary dependency records with `version_spec` set to the catalog alias; unresolvable references produce a versionless record and an `unresolved_version` warning (today they are invisible; the plan makes them visible, which is the spec's intent).
- Produces in `discovery.py`: the catalog row parses `gradle/libs.versions.toml` first (ecosystem precedence: place the row before the gradle row) and stores the tuple in `scan_state["gradle"]["catalogs"][<dir of the gradle/ folder>]`; the gradle row's dispatcher looks up the nearest catalog at or above the build file's directory and passes it as `catalog=`. The toml file is listed in `scan["files"]` like any manifest.

- [ ] **Step 1: Create `tests/test_inventory_java.py`** with the pom and gradle assertion blocks from `tests/test_generate_parsing.py` and the catalog blocks, imports retargeted (`parse_pom_records`, `parse_gradle_records`, `parse_version_catalog`, `scan_folder`). Root assertions on `parse_pom(path)` returning a `(deps, properties, repositories)` tuple are retargeted to `parse_pom_records` record dicts with a note; repository assertions were moved in Task 3, skip them. Add a fixture-based test: `tests/fixtures/generate_config/gradle` (add a `gradle/libs.versions.toml` there declaring one library referenced from `build.gradle.kts` as `libs.some.lib`) yields a record with that version.

- [ ] **Step 2: Run** it. Expected: FAIL (`ImportError` for `parse_version_catalog`, then missing catalog-resolved record).

- [ ] **Step 3: Implement** in `java.py` and `discovery.py`.

- [ ] **Step 4: Run** `python tests/test_inventory_java.py`, `python tests/test_generate_config.py`, `python tests/test_inventory_integration.py`. Expected: OK.

- [ ] **Step 5: Remove `"catalogs"`** from the allowlist; run the gate. Expected: OK.

- [ ] **Step 6: Leave `tests/test_generate_parsing.py` in place** with only its npm-lock section (delete the moved blocks from it so nothing is asserted twice). Task 5 deletes the file.

- [ ] **Step 7: Commit** `feat(inventory): resolve Gradle version-catalog references`

---

### Task 5: npm lockfile graph enumeration

**Files:**
- Modify: `helper_scripts/eol_inventory/parsers/node.py` (`_read_lock` :44, `_lock_lookup` :105, `parse_package_json_records` :154)
- Modify: `helper_scripts/eol_inventory/config_writer.py` (npm section :246-276, `summary.indirect` :457-464)
- Modify: `tests/test_inventory_node.py` (add moved npm-lock tests from `tests/test_generate_parsing.py` and the npm part of `tests/test_generate_transitive_parsers.py`)
- Delete: `tests/test_generate_parsing.py`
- Modify: `tests/test_generator_parity.py` (remove `"npm_graph"`)

**Interfaces:**
- Produces in `node.py`: `lock_graph_records(data, rel_lock_path, direct_names) -> list[dict]` (logic of `generate_config.py:1097-1194`: v2/v3 `packages` with scope preserved, v1 `dependencies` recursed, skip root `""`, missing versions, `link:`/`file:` and any version containing `:`). Each returned record is `new_record("npm", name, version=..., direct=False, kind="dependency")` with `add_location(record, rel_lock_path, "npm", locator=f"lock:{name}")`, excluding names in `direct_names`. `parse_package_json_records` appends these records whenever a lock was read (they carry `direct=False`; `config_writer` decides inclusion).
- Produces in `config_writer.py`: npm records with `direct=False` are included only when `include_transitive` is true (same rule the python/go sections use at :286-288 and :306-308) and are counted in `summary.indirect`. `_skipped_npm_packages` logic unchanged.

- [ ] **Step 1: Add tests** to `tests/test_inventory_node.py`: the moved `parse_npm_lockfile` assertions retargeted to `lock_graph_records` (shape `(name, version)` tuples become record dicts; assert on `record["name"], record["version"], record["direct"] is False`), plus: scanning `tests/fixtures/generate_config/node` with `include_transitive=False` yields no `direct=False` products and with `True` yields the lockfile's transitive packages, `summary.indirect > 0`. Register in `TESTS`.

- [ ] **Step 2: Run** `python tests/test_inventory_node.py`. Expected: FAIL, `ImportError: lock_graph_records`.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** `python tests/test_inventory_node.py`, `python tests/test_generate_config.py`, `python tests/test_inventory_integration.py`, `python tests/test_cli_input_safety.py`. Expected: OK.

- [ ] **Step 5: Remove `"npm_graph"`**; run the gate. Expected: OK. Delete `tests/test_generate_parsing.py`; run the registration check.

- [ ] **Step 6: Commit** `feat(inventory): enumerate npm lockfile graphs as indirect records`

---

### Task 6: Transitive resolution behind `--resolve-transitive`

**Files:**
- Create: `helper_scripts/eol_inventory/resolvers.py`
- Modify: `helper_scripts/generate_config.py` (argparser :396-415, scan call :429, `generate_config` call :435-436)
- Modify: `helper_scripts/eol_inventory/config_writer.py` (accept resolver records like any other indirect Java record)
- Create: `tests/test_inventory_resolvers.py` (moved from `tests/test_generate_transitive_parsers.py` (mvn/gradle parts) and `tests/test_generate_transitive_merge.py`, including its `_FakeShutil`/`_FakeSubprocess` classes)
- Delete: those two root test files
- Modify: `AGENTS.md` (CLI description: `--resolve-transitive` executes `mvn`/`gradle`; the only such path), `helper_scripts/README.md` (same note)

**Interfaces:**
- Produces in `resolvers.py`: `parse_mvn_dependency_list(text) -> list[tuple[str, str, str]]` (:1037), `parse_gradle_dump(text) -> list[tuple[str, str, str]]` (:1070), `mvn_dependency_list(pom_path, *, run=subprocess.run, which=shutil.which) -> tuple[Optional[list], Optional[str]]` (:1195), `gradle_dependency_dump(project_dir, *, run=subprocess.run, which=shutil.which) -> tuple[Optional[list], Optional[str]]` (:1230), constants `MVN_TIMEOUT_S = 180`, `GRADLE_TIMEOUT_S = 240`, `GRADLE_INIT_SCRIPT` (verbatim from :1175-1193). `resolve_transitive(scan, root, *, run=subprocess.run, which=shutil.which) -> tuple[list[dict], list[dict]]`: for every pom in `scan["files"]` run mvn, for every directory holding a `build.gradle(.kts)` run gradle once; each `(g, a, v)` not already present as a direct record becomes `new_record("java", f"{g}:{a}", version=v, direct=False, kind="dependency", group=g, artifact=a)` with `add_location(record, rel_manifest, "mvn" or "gradle", locator="transitive")`; each failure becomes `new_warning("transitive_unavailable", rel_manifest, reason)`. The injectable `run`/`which` parameters exist so tests never execute real tools.
- Produces in `generate_config.py`: `--resolve-transitive` flag (help text says it runs `mvn`/`gradle`); when set, `args.include_transitive = True`, and after `scan_folder` the CLI calls `records, warnings = resolve_transitive(scan, folder)` and extends `scan["records"]` and `scan["warnings"]`.

- [ ] **Step 1: Create `tests/test_inventory_resolvers.py`** from the two root files: parser assertions unchanged except imports; merge assertions retargeted from the root `_merge_transitive_deps` failure tuples to `resolve_transitive` warnings (`category == "transitive_unavailable"`, `path`, `message`) and to records with `direct is False`. Keep the fake `subprocess`/`shutil` classes and pass them via `run=`/`which=`. Add: CLI test running `helper_scripts/generate_config.py <fixture mixed> --resolve-transitive` in a subprocess with `PATH` set to an empty temp dir so no tool is found; expect exit 0, a `transitive_unavailable` warning in `_inventory.warnings`, and `include_transitive: true` in `_inventory`.

- [ ] **Step 2: Run** it. Expected: `ImportError`.

- [ ] **Step 3: Implement** `resolvers.py` (move functions verbatim, add the injectable parameters), the flag, the docs lines.

- [ ] **Step 4: Run** `python tests/test_inventory_resolvers.py`, `python tests/test_generate_config.py`, `python tests/test_helper_wrappers.py`, `python tests/check_agent_docs.py`. Expected: OK. Also confirm `grep -rn "subprocess" helper_scripts/eol_inventory/` lists only `resolvers.py`.

- [ ] **Step 5: Delete** the two root test files; registration check; parity gate (no category to remove; must still be OK).

- [ ] **Step 6: Commit** `feat(inventory): add opt-in mvn/gradle transitive resolution`

---

### Task 7: Declarations record in `_inventory`

**Files:**
- Modify: `helper_scripts/eol_inventory/config_writer.py` (`add`/`add_unmapped` :155-193, `_inventory` :450-467)
- Modify: `helper_scripts/eol_inventory/report_writer.py` (`build_inventory_view` :261, `render_markdown` :459, `render_csv` :620, `render_html` :676)
- Modify: `helper_scripts/generate_config.py` (`_merge_existing_config` :321-324)
- Modify: `tests/test_generate_config.py`, `tests/test_inventory_report.py` (moved from `tests/test_generate_full_picture.py`)
- Delete: `tests/test_generate_full_picture.py`
- Modify: `tests/test_generator_parity.py` (remove `"declarations"`; `PENDING_CATEGORIES` is now empty)

**Interfaces:**
- Produces in `config_writer.py`: every record that enters `generate_config` gets exactly one declaration `{"decl": "<group:artifact|name>@<version or '?'>", "file": "<first found_in path>", "kind": <record kind>, "outcome": <str>}` appended to `_inventory["declarations"]`, with outcomes from the root vocabulary: `"tracked: <label>"`, `"duplicate-of: <label>"`, `"skipped: <reason>"`, `"unmapped: <reason>"`, `"unmapped-transitive (tracked in records only)"`; `transitive_unavailable` warnings also produce `{"decl": "<manifest>", "file": rel, "kind": "transitive-maven|transitive-gradle", "outcome": "skipped: transitive resolution unavailable (mvn|gradle not on PATH or failed)"}`. `_inventory["summary"]["declarations"] = {"total": n, "by_outcome": {<prefix>: count}}` (prefix = text before the first `:` or the whole string).
- Produces in `report_writer.py`: `view["declarations"]` (list, same records) and `view["summary"]["by_declaration_outcome"]`; Markdown section `## Declarations` after `## Tracked products` with a table (decl, file, kind, outcome); CSV gains rows with `record_type == "declaration"`; HTML gains the same table. All text passes through the existing `redact_display_text` path like warnings.
- Produces in `generate_config.py`: `_discovered_dependencies` is dropped from the merged config (add it to the not-copied tuple at :321-324).

- [ ] **Step 1: Add tests** to `tests/test_generate_config.py` (declarations present for the `mixed` fixture, outcome vocabulary, summary counts, `_discovered_dependencies` dropped on `--update`) and `tests/test_inventory_report.py` (Markdown/CSV/HTML contain the declarations, sentinel in a declaration `decl` field is redacted). Move `tests/test_generate_full_picture.py` assertions, retargeted from `config["_discovered_dependencies"]` to `config["_inventory"]["declarations"]` and from `_discovered_summary` to `summary["declarations"]["by_outcome"]`.

- [ ] **Step 2: Run** both files. Expected: FAIL on missing `declarations`.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** `python tests/test_generate_config.py`, `python tests/test_inventory_report.py`, `python tests/test_inventory_redaction.py`, `python tests/test_inventory_integration.py`. Expected: OK.

- [ ] **Step 5: Empty the allowlist**; run the gate. Expected: OK with zero `pending` lines. Delete `tests/test_generate_full_picture.py`; registration check.

- [ ] **Step 6: Commit** `feat(inventory): record every declaration and its outcome in _inventory`

---

### Task 8: Retire the root generator

**Files:**
- Delete: `generate_config.py` (root), `tests/test_generator_parity.py`
- Modify: `AGENTS.md` (:24 workflow row, :33-35 coexistence note, :77 tree, :179-190, :320 file table, testing section line from Task 1), `README.md` (:113, :119), `eol_config_generation_prompt.md` (:102, :163, :396-398, :470), `eoltracker/parsers/maven_central.py:55-58` (docstring only), `helper_scripts/README.md` (if it mentions the root script)

- [ ] **Step 1: Confirm** `python tests/test_generator_parity.py` is OK with an empty allowlist and `git grep -n "from generate_config import\|import generate_config" tests/` is empty.

- [ ] **Step 2: Delete** the root script and the gate. Run `git grep -n "generate_config.py" -- ':!docs/**' ':!*.json'` and update every remaining hit outside historical docs so it names `helper_scripts/generate_config.py`. The generated fixtures `eol_config.b-auto.json` and `eol_config.smoke.json` keep their comments.

- [ ] **Step 3: Docs.** Remove the coexistence paragraphs; the workflow row names one generator; the layout tree drops the root entry; the CLI paragraph states that `--resolve-transitive` is the only path executing external tools.

- [ ] **Step 4: Full gate.** Every `tests/test_*.py` and `tests/check_*.py` exits 0; `python -m compileall -q eoltracker helper_scripts tests`; `python tests/check_agent_docs.py`; `python tests/check_test_registration.py`. A scan of `tests/fixtures/generate_config/mixed` with and without `--resolve-transitive` (empty `PATH`) succeeds, the latter with a `transitive_unavailable` warning.

- [ ] **Step 5: Commit** `refactor(config): retire the root generator in favour of the inventory scanner`
