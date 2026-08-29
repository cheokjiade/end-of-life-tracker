# Project Dependency Inventory and Registry Providers Plan

Date: 2026-08-28

## Goal

Turn the existing manifest-to-config helper into a safe, cross-platform project
inventory tool that:

- discovers direct dependencies and runtime versions across Python,
  Node.js/TypeScript, Java/Kotlin, Go, and .NET projects;
- discovers container images in Dockerfiles and GitLab CI configuration;
- retains structured, portable provenance for every discovered item;
- maps supported items into valid EOL tracker product entries;
- reports unresolved or unsupported items without fabricating lifecycle data;
- queries PyPI, NuGet, and the Go module proxy for release-recency signals;
- produces a separate human-readable inventory report; and
- remains approachable for users with no Python command-line experience on
  Linux, macOS, and Windows.

The implementation remains standard-library-only. Registry providers report
release recency and explicit unsafe signals such as yanked, deprecated,
unlisted, or retracted releases when the authoritative registry exposes them.
They do not claim that package release age is an EOL date.

## Implementation status

Complete on branch `codex/dependency-inventory` as of 2026-08. The core
workflow was reconciled in `fedb34e`, followed by the canonical documentation
update. All sixteen incremental steps landed. The completion criteria were met: five
ecosystems plus both container sources scan deterministically, every discovered
item carries structured provenance, the three registry providers operate
through their official public APIs, reports and wrappers work on Windows
PowerShell and Bash, network-free tests pass, live smoke checks were recorded,
and the canonical `.agents/skills/` layout is respected.

Decisions finalized during implementation (this document reflects the shipped
behaviour):

- **Untracked visibility:** the planned user-selectable `--include-untracked`
  mode was replaced by unconditional visibility. Unmapped inventory items
  always become explicit `manual` tracker rows (no EOL date, rendered
  UNTRACKED) under a `=== Needs Manual Review ===` section, in addition to
  their `_inventory.unmapped` records, so reports never silently drop
  inventory. The default therefore needs no flag.
- **Update versus replace:** the planned `--baseline` mode became the mutually
  exclusive `--update` / `--replace` pair. `--update` merges scan evidence
  into an existing config preserving curation fields (`_comment`,
  `policy_note`, `note`, `reference_url`, `eol_date`, `latest`) and sections;
  entries the scan did not observe are retained and counted as
  `retained_not_observed` in `_inventory.update_summary`; additions join a
  `=== Newly Discovered ===` section. `--replace` is the only wholesale
  overwrite path.
- **CLI surface:** `generate_config.py` ships `<folder>`, `--name`, `--output`,
  repeatable `--exclude`, `--update | --replace`, `--include-transitive`, and
  `--strict`. The planned `--dry-run`, `--offline`, and `--force` options were
  not needed (atomic writes plus explicit overwrite refusal cover the same
  risks); the report CLI gained `--csv [FILE]`, `--html [FILE]`, `--no-csv`,
  `--no-html`, and `--force`.
- **Report formats:** Markdown, CSV, and HTML are all written by default under
  `reports/inventory/` (the plan anticipated optional CSV only); `--no-csv` /
  `--no-html` suppress formats. Legacy configs and `_skipped_npm_packages`
  remain reportable.
- **Metadata:** per-entry provenance lands in `_found_in`; the ignored
  `_inventory` object carries schema version 1, generator version, scan root,
  manifest list, `include_transitive`, summary counts (files, records,
  products, unmapped, warnings, indirect), structured warnings, and unmapped
  items. The root `generate_config.py` is removed and Terraform excludes
  `helper_scripts/` from the Lambda archive.

## Issue review

### Issue #1: local helper scripts

Issue #1 is closed and its acceptance intent is already represented by the
root `run.sh` and `run.ps1` scripts. They detect Python, let the user select a
config, and run the tracker locally.

This project will preserve those entry points. The new config-generation and
inventory wrappers will follow the same interaction model and wording rather
than replace the existing runner.

### Issue #5: canonical cross-agent skills

Issue #5 is open, with implementation already present on the
`codex/standardize-agent-skills` branch. It makes `.agents/skills/` the
authoritative home for config-management and provider-maintenance workflows,
leaving only thin Claude compatibility loaders.

This plan incorporates that feature as a prerequisite and integration base:

- generator workflow changes update the canonical
  `manage-eol-config` skill under `.agents/skills/`;
- provider workflow changes update the canonical `add-eol-provider` skill
  under `.agents/skills/`;
- no workflow logic is copied back into `.claude/`;
- documentation integrity tests continue to verify compatibility loaders; and
- implementation work is based on the issue #5 branch so later integration
  does not recreate or conflict with its skill consolidation.

## Current behavior and limitations

The current root `generate_config.py` scans Maven POMs, common Gradle
declarations, and `package.json` files. It maps known dependencies to
endoflife.date or specialized providers, falls back to Maven Central for most
Java dependencies, and records unmapped npm packages separately.

Provenance currently exists only as free-text `_comment` values containing a
manifest basename. Deduplication keeps the first matching product and discards
later locations. Consequently it cannot reliably answer which modules use a
dependency, distinguish direct from inferred evidence, or retain paths in a
portable structure.

The generator also treats cleaned Node version ranges as installed versions,
does not resolve them from a lock file, has no generalized warning model, and
has no tests dedicated to generator behavior.

## Scope decisions

### Supported ecosystems

The first complete version supports exactly five language ecosystems:

| Ecosystem | Initial inputs | Runtime evidence |
|---|---|---|
| Python | `requirements*.txt`, PEP 621 and common Poetry declarations in `pyproject.toml`, `Pipfile.lock` | `.python-version`, `runtime.txt`, `requires-python`, recognized container images |
| Node.js/TypeScript | `package.json`, `package-lock.json`, `npm-shrinkwrap.json` | `engines.node`, `.nvmrc`, recognized container images |
| Java/Kotlin | `pom*.xml`, `*.gradle`, `*.gradle.kts` | POM properties, recognized container images |
| Go | `go.mod` | the `go` or `toolchain` directive, recognized container images |
| .NET | `*.csproj`, `*.fsproj`, `*.vbproj`, `Directory.Packages.props`, `packages.lock.json`, `global.json` | target frameworks, SDK version, recognized container images |

TypeScript is part of the Node ecosystem rather than a separate package
ecosystem. Lock files resolve exact versions for direct declarations where the
format permits it. The initial implementation does not emit every transitive
lock-file package.

### Inventory versus EOL products

The generated JSON remains a valid EOL tracker config. It contains two related
views:

- `products` contains entries backed by a real provider and therefore usable by
  the Lambda runtime;
- `_inventory` contains scan metadata, unresolved dependencies, unsupported
  declarations, and warnings ignored by the runtime.

The generator must never create an entry naming a provider that is not
registered. Unmapped inventory items also become explicit `manual` product
rows without an EOL date (rendered UNTRACKED), so reports never silently drop
inventory; `_inventory.summary` separates tracked products from
manual-review entries.

### Direct dependencies

Direct dependencies and explicitly selected runtimes are the unit of
inventory. Lock files are used to resolve those declarations, not to produce a
complete software bill of materials. Full transitive SBOM generation is a
separate concern and may be added later as CycloneDX or SPDX output.

## Target helper layout

Move generator functionality under `helper_scripts/`:

```text
helper_scripts/
  README.md
  generate_config.py
  generate_inventory_report.py
  generate_config.sh
  generate_config.ps1
  generate_inventory_report.sh
  generate_inventory_report.ps1
  eol_inventory/
    __init__.py
    discovery.py
    models.py
    mappings.py
    config_writer.py
    report_writer.py
    parsers/
      __init__.py
      python.py
      node.py
      java.py
      go.py
      dotnet.py
      docker.py
      gitlab_ci.py
```

The command-line files remain thin entry points. Pure parsing, mapping, and
rendering functions live in importable modules so standalone assertion tests
can exercise them without network or subprocess execution.

The root generator is removed after every in-repository reference is updated.
Terraform excludes the entire helper directory from the Lambda archive.

## Normalized discovery model

All parsers return normalized dependency records before config mapping. The
conceptual record contains:

```json
{
  "ecosystem": "python",
  "name": "requests",
  "version": "2.32.4",
  "scope": "runtime",
  "direct": true,
  "kind": "dependency",
  "found_in": [
    {
      "path": "services/api/requirements.txt",
      "manifest": "requirements",
      "line": 14,
      "locator": "requests"
    }
  ]
}
```

Rules:

- paths are relative to the scan root and use `/` separators;
- line is optional for formats where it cannot be recovered reliably;
- locator identifies an XML element, JSON property, Docker instruction, or CI
  job when a line is unavailable;
- paths outside the scan root are never emitted;
- duplicate records merge all distinct provenance locations;
- sorting is deterministic across platforms;
- unresolved ranges and dynamic expressions are retained as warnings, not
  silently rewritten as exact versions; and
- no manifest contents or unrelated environment-variable values are copied
  into the config.

Mapped product entries receive an ignored `_found_in` array. Unmapped records
remain under `_inventory.unmapped`. The `_inventory` object also includes a
schema version, generator version, scan-root basename, manifest paths, summary
counts, and structured warnings. The report generator reads both the new model
and legacy `_skipped_npm_packages` data.

## Project discovery

Discovery walks the project deterministically without following directory
symlinks outside the scan root. Default exclusions include source-control
metadata, dependency caches, virtual environments, compiled output, and common
generated directories such as `.git`, `node_modules`, `.venv`, `venv`,
`vendor`, `target`, `bin`, `obj`, `dist`, and `build`.

Users can add patterns through `.eolignore` and repeatable `--exclude`
arguments. Warnings are emitted for unreadable files, over-size inputs,
unsupported syntax, and local includes that escape the scan root. One bad
manifest does not erase results from other files, while `--strict` converts
warnings into a non-zero exit code for CI use.

## Ecosystem parsing

### Python

- Parse exact pins, compatible pins, environment markers, editable/direct URL
  declarations, and recursive includes in requirements files.
- Follow included requirements only when they remain within the scan root.
- Read PEP 621 dependency arrays and common Poetry dependency tables using a
  conservative standard-library parser. Unsupported TOML constructs produce a
  warning rather than a guessed record.
- Use `Pipfile.lock` JSON to resolve direct Pipfile declarations when present.
- Map the Python runtime to endoflife.date and packages to `pypi_registry` when
  an exact version is available.
- Keep unpinned, URL-based, local-path, or dynamically computed dependencies in
  inventory with an explicit reason.

### Node.js and TypeScript

- Preserve current `dependencies` and `devDependencies` coverage.
- Use npm lock data to resolve ranges to the installed direct version.
- Never treat `^`, `~`, comparison, workspace, Git, URL, or file references as
  exact versions without lock evidence.
- Map known runtime/framework products to lifecycle providers and remaining
  exact packages to `npm_registry`.

### Java and Kotlin

- Preserve existing Maven parent, dependency, dependency-management, property,
  and common Gradle declaration behavior.
- Retain full Maven versions for registry checks and cycle-specific versions
  only for known lifecycle mappings.
- Keep unresolved Gradle expressions and Maven properties visible as warnings.
- Refactor existing behavior behind the common model before extending it, so
  characterization tests demonstrate that the move is behavior-preserving.

### Go

- Parse the `module`, `go`, `toolchain`, and direct `require` declarations in
  `go.mod`.
- Exclude `// indirect` requirements from default products while retaining a
  summary count.
- Map the Go runtime to endoflife.date and exact direct modules to `go_proxy`.
- Preserve semantic import version suffixes and `replace` directives as
  provenance or warnings; never query a local replacement as a public module.

### .NET

- Parse package references from C#, F#, and Visual Basic project XML.
- Resolve central package versions from `Directory.Packages.props` and direct
  versions from `packages.lock.json` when available.
- Detect target frameworks and SDK versions from project files and
  `global.json`, mapping supported .NET runtimes to endoflife.date.
- Send exact direct NuGet package versions to `nuget_registry`; retain property
  expressions and unresolved central versions as warnings.

## Container image discovery

### Dockerfiles

Scan `Dockerfile`, `Dockerfile.*`, and `*.Dockerfile` for `FROM` instructions.
Support multi-stage builds, `--platform`, stage aliases, image tags, digests,
and simple `ARG NAME=default` substitution used by `FROM`. Ignore `scratch` and
do not mistake a prior stage alias for an external image.

Recognized official or vendor images map to lifecycle products where the tag
provides a valid cycle, including Python, Node, Go, .NET, Ubuntu, Debian,
Alpine, PostgreSQL, MySQL, Redis, and nginx. Unknown images, `latest`, dynamic
variables, and digest-only references remain visible in inventory warnings.

### GitLab CI

Scan `.gitlab-ci.yml`, `.gitlab-ci.yaml`, and local YAML files under `.gitlab/`
for top-level, default, and job-level images plus service images. Support scalar
and `name` object forms and follow only local include paths inside the scan
root.

The parser intentionally recognizes common GitLab CI structures rather than
claiming full YAML support. Remote includes, anchors, complex merges, and
unresolved variables produce warnings. CI configuration is never executed,
and arbitrary variable values are never emitted into the inventory.

## Registry providers

All three providers follow the canonical `add-eol-provider` skill and
auto-registration contract. Each has a cached fetch layer, pure transformation
helpers, a complete normalized result dictionary, network-free tests, source
label, and upstream URL.

### PyPI registry

Source key: `pypi_registry`

Entry shape:

```json
{
  "source": "pypi_registry",
  "package": "requests",
  "version": "2.32.4",
  "label": "Requests 2.32.4"
}
```

Use the official PyPI JSON API. Report pinned release upload date, latest
stable version and date, and yanked status/reason. A yanked pinned release is
an alert. Missing packages, malformed documents, and explicitly requested
versions absent from the registry are errors. Release age is informational,
not EOL.

### NuGet registry

Source key: `nuget_registry`

Entry shape:

```json
{
  "source": "nuget_registry",
  "package": "Newtonsoft.Json",
  "version": "13.0.3",
  "label": "Newtonsoft.Json 13.0.3"
}
```

Use official NuGet V3 endpoints. Report pinned publication date, latest stable
version and date, and deprecation or listed state where authoritative metadata
exposes it. Deprecated or unlisted pinned versions alert. Package IDs are
matched case-insensitively while labels preserve input casing. Release age is
informational, not EOL.

### Go module proxy

Source key: `go_proxy`

Entry shape:

```json
{
  "source": "go_proxy",
  "module": "golang.org/x/net",
  "version": "v0.44.0",
  "label": "golang.org/x/net v0.44.0"
}
```

Use the official Go module proxy protocol, including its uppercase path
escaping rules. Report pinned version timestamp and latest stable semantic
version. If retraction can be determined reliably from official proxy content,
surface it as an alert; otherwise explicitly document that retraction is not
reported. Do not infer EOL from module age.

## Human-readable inventory report

`generate_inventory_report.py` reads a config locally and performs no network
calls. Markdown, CSV, and self-contained HTML are all written by default
under `reports/inventory/<project>-inventory.{md,csv,html}` (`--no-csv` /
`--no-html` suppress a format; `--force` allows overwriting existing
reports).

The report contains:

- scan date, generator version, files scanned, and warning count;
- tracked products grouped by ecosystem and provider;
- current version, tracking source, and every provenance location;
- inferred entries distinguished from explicit declarations;
- unmapped or unresolved dependencies;
- container images and their declaration sites;
- summary counts by ecosystem, provider, and review state; and
- a manual-review checklist.

Legacy configs without `_inventory` remain readable. Missing provenance is
shown as `not recorded` rather than treated as an error.

## Command-line and noob-friendly wrappers

The Python CLIs support explicit arguments for automation plus interactive
wrappers for first-time users.

With no arguments, `generate_config.sh` and `generate_config.ps1`:

1. locate a Python 3 interpreter and validate its version;
2. ask for the project directory, defaulting to the current directory;
3. suggest a safe project slug and output filename;
4. show what file types will be scanned;
5. default to a curation-preserving update when the output exists, while
   offering explicit replace or cancel choices;
6. generate the config and the Markdown/CSV/HTML inventory;
7. summarize mapped, unmapped, and warning counts; and
8. offer the exact command for the live tracker smoke run.

The inventory-report wrappers offer a numbered config picker following the
existing root runner. All wrappers resolve the repository root from their own
location, preserve quoted paths containing spaces, return Python's exit code,
and provide copy-paste recovery instructions when Python is missing.

Useful non-interactive options include `--output`, `--name`, `--exclude`,
`--update`, `--replace`, `--include-transitive`, and `--strict`. `--update`
merges scan evidence into the existing curated config and records added,
version-changed, unchanged, and retained-not-observed counts in
`_inventory.update_summary`; it never deletes curated entries.

## Safety and compatibility

- Keep `eoltracker/` and helper functionality standard-library-only.
- Keep generated configs ASCII-safe with `ensure_ascii=True`.
- Use atomic output replacement. Require `--replace` for generator overwrites;
  the report generator uses `--force` for regenerating local report files.
- Never execute project files, package managers, Dockerfiles, or CI YAML.
- Never follow manifest includes or symlinks outside the scan root.
- Apply file-size and total-file safeguards to avoid accidental huge scans.
- Preserve all existing tracker config behavior; underscore-prefixed metadata
  remains ignored by the Lambda runtime.
- Do not modify ignored per-project configs or generated reports during tests.
- Exclude all helper scripts and tests from the Terraform Lambda archive.

## Testing strategy

Tests remain standalone Python assertion scripts and network-free unless
explicitly named as live smoke checks.

### Characterization and unit coverage

- Capture existing Maven, Gradle, Node mapping, section, and comment behavior
  before refactoring.
- Build fixture projects for every supported ecosystem and a mixed monorepo.
- Test malformed files, duplicate declarations, unresolved versions, nested
  modules, path normalization, exclusion rules, and deterministic output.
- Test Docker multistage builds, ARG defaults, stage aliases, tags, digests,
  and scratch images.
- Test GitLab scalar/object images, services, local includes, variables,
  anchors, and unsafe include paths.
- Test every registry provider with synthetic upstream documents, injected
  caches, missing packages/versions, prereleases, unsafe release signals,
  registration, and URL generation.
- Test inventory Markdown and CSV output by externally visible headings,
  records, escaping, and stable ordering rather than private helper structure.
- Verify legacy configs and `_skipped_npm_packages` remain reportable.

### Wrapper and integration coverage

- Run `bash -n` on both Bash wrappers.
- Parse both PowerShell wrappers through the PowerShell parser API.
- Exercise non-interactive wrapper paths on Windows and one Unix-like shell.
- Parse every generated config with the repository's supported Python launchers.
- Run all existing tests and agent-document integrity checks.
- Run a mixed fixture scan twice and compare output after normalizing the
  generation date.
- Run one live smoke check per new registry provider against a small temporary
  config, then one live tracker run against a generated mixed-project config.

## Incremental commits

Each commit must leave the repository usable and pass its relevant network-free
checks.

1. `docs(plan): define dependency inventory expansion`
   - Add this plan only.

2. `feat(agents): centralize EOL workflows under .agents`
   - Incorporate issue #5 as the implementation baseline.

3. `test(config): characterize existing manifest generation`
   - Add fixtures and tests for current Java and Node behavior before moving
     code.

4. `refactor(config): move generator into helper scripts`
   - Move the existing CLI and behavior into the importable helper layout;
     update repository references and Terraform exclusions.

5. `feat(config): retain structured dependency provenance`
   - Introduce normalized records, merged `_found_in` locations, `_inventory`,
     warnings, deterministic discovery, and safe exclusions.

6. `feat(provider): add PyPI registry source`
   - Add provider, network-free tests, registration, and URL behavior.

7. `feat(provider): add NuGet registry source`
   - Add provider, network-free tests, registration, and URL behavior.

8. `feat(provider): add Go module proxy source`
   - Add provider, network-free tests, path escaping, registration, and URL
     behavior.

9. `feat(config): scan Python projects`
   - Add Python manifests, runtime detection, exact-version handling, PyPI
     mappings, fixtures, and tests.

10. `feat(config): improve Node dependency resolution`
    - Resolve direct npm versions through lock data and preserve unresolved
      specifications instead of guessing.

11. `feat(config): scan Go and .NET projects`
    - Add Go module/runtime parsing and .NET project/central-package/SDK
      parsing with provider mappings and tests.

12. `feat(config): scan container image declarations`
    - Add Dockerfile and GitLab CI image parsing, lifecycle mappings,
      provenance, warnings, fixtures, and tests.

13. `feat(config): generate human-readable inventories`
    - Add Markdown/CSV inventory rendering and legacy compatibility tests.

14. `feat(config): add cross-platform helper launchers`
    - Add interactive Bash and PowerShell generation/report wrappers and their
      syntax checks.

15. `docs(config): document project scanning workflow`
    - Update the README, canonical skills, generation specification, provider
      count/list, provider entry shapes, maintenance guide, and helper README.

16. `test(config): verify end-to-end project inventory workflow`
    - Add mixed-project integration coverage and complete final network-free
      verification.

## OpenCode worktree orchestration

Implementation uses `opencode-go/glm-5.3-flash` with OpenCode's `max`
reasoning variant. Agents receive explicit repository-data egress approval,
must read the applicable canonical skill, work only in their assigned Git
worktree, stage only owned paths, commit verified batches, and never push.

Initial parallel worktrees own non-overlapping provider modules and tests:

- PyPI registry provider;
- NuGet registry provider; and
- Go module proxy provider.

After integrating providers, subsequent worktree waves split the generator by
stable ownership boundaries: core discovery/model/config writing, language
parsers, container parsers, and reporting/wrappers. Shared documentation is
updated only after code integration to avoid parallel merge conflicts.

## First audit round

After implementation and integration tests pass, dispatch fresh read-only GLM
5.3 Flash agents at maximum reasoning. Audit worktrees must not edit, stage,
commit, or push.

1. Provider audit
   - Attempt to disprove PyPI, NuGet, and Go results using malformed documents,
     prereleases, missing versions, unsafe-state metadata, version ordering,
     URL escaping, cache behavior, and normalized result requirements.

2. Scanner and safety audit
   - Attempt to find false positives, missed direct dependencies, path escapes,
     symlink traversal, secret leakage, unbounded scans, accidental execution,
     dynamic-version guessing, and cross-platform path defects.

3. Compatibility and UX audit
   - Check legacy config behavior, runtime ignoring of metadata, Terraform
     packaging, issue #5 canonical skill rules, wrapper usability, report
     completeness, docs accuracy, and test gaps.

Findings must include severity, evidence, impact, and a concrete remediation.
Actionable findings are fixed in follow-up commits and relevant tests rerun.
The completion report distinguishes resolved findings from documented residual
risks.

## Out of scope

- Full transitive SBOM generation
- Vulnerability, malware, or license scanning
- Executing package managers or build tools
- Fetching remote or private GitLab includes
- Authenticating to private package or container registries
- Treating registry staleness as a real lifecycle date
- Automatically mutating an existing curated config in baseline mode
- Publishing branches, opening pull requests, or merging remote changes

## Completion criteria

The work is complete when all five ecosystems and both container sources scan
into deterministic configs, all discovered items retain provenance, the three
new providers operate through official public APIs, inventory reports and
wrappers work across supported platforms, current tests remain green, new
network-free tests pass, live smoke checks are recorded, issue #5's canonical
skill layout is respected, and the first audit round has no unresolved high- or
medium-severity findings.
