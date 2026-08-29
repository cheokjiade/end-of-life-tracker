# Helper scripts: scan a project and track its dependencies

These scripts look at a folder of source code, work out which software and
libraries it uses, and produce two things:

1. an **EOL tracker config** (`eol_config.<project>.json`) that the tracker's
   Lambda can run against, and
2. a **human-readable inventory report** (Markdown, CSV, and HTML) you can
   read, filter, or share before trusting any of it.

No installation step is needed: everything is standard-library Python, and you
only need **Python 3.9+** available as `python`, `python3`, or `py`. The
scanner never runs your project's code, never runs Docker or a package
manager, and never touches the network — only the later tracker run does that.

---

## Quick start (first time? use the wizard)

### Windows (PowerShell)

```powershell
# From anywhere: the wizard starts from the folder you are in
cd C:\code\my-project
& C:\path\to\endoflife-tracker\helper_scripts\generate_config.ps1
```

The wizard asks:

1. which **project directory to scan** (Enter = the folder you were in),
2. the **output file name** (Enter = `eol_config.<project>.json`),
3. **confirmation before overwriting** an existing file (default is No),
4. then prints what it scanned, what it mapped, what needs review, and the
   exact command for the live tracker smoke run.

### macOS / Linux (Bash; Git Bash and WSL also work)

```bash
cd /code/my-project
/path/to/endoflife-tracker/helper_scripts/generate_config.sh
```

Same wizard, same questions.

### Then render the inventory report

The wizard already regenerates the report after each scan. To re-render it
later against an existing config:

```powershell
# Windows
& C:\path\to\endoflife-tracker\helper_scripts\generate_inventory_report.ps1
```

```bash
# macOS / Linux
/path/to/endoflife-tracker/helper_scripts/generate_inventory_report.sh
```

With no arguments this shows a numbered menu of every `eol_config.*.json` in
the repository (the same picker as the root `run.sh` / `run.ps1` tracker
runner). You can also pass a shorthand (`a` → `eol_config.a.json`) or an
explicit file name.

### Run the tracker on the result

```bash
python lambda_function.py eol_config.my-project.json   # from the repository root
```

or use the root `run.sh` / `run.ps1` interactive runner.

---

## What gets scanned

Five language ecosystems, plus container images:

| Ecosystem | Files read |
|---|---|
| Java / Kotlin | `pom*.xml`, `*.gradle`, `*.gradle.kts` (multi-module aware) |
| Node.js / TypeScript | `package.json`; ranges resolved through `package-lock.json` / `npm-shrinkwrap.json` |
| Python | `requirements*.txt`, `pyproject.toml` (PEP 621 and common Poetry tables), `Pipfile` + `Pipfile.lock`, `.python-version`, `runtime.txt` |
| Go | `go.mod` (module, `go`/`toolchain`, direct `require`) |
| .NET | `*.csproj`, `*.fsproj`, `*.vbproj`, `Directory.Packages.props`, `packages.lock.json`, `global.json` |
| Container images | `Dockerfile`, `Dockerfile.*`, `*.Dockerfile` (`FROM` instructions), `.gitlab-ci.yml` / `.gitlab-ci.yaml` and local YAML under `.gitlab/` |

Language runtimes are detected too (`.python-version`, `engines.node`,
`.nvmrc`, Go's `go`/`toolchain` directives, .NET `TargetFramework` and
`global.json` SDK settings) and tracked on endoflife.date.

### Dockerfiles

Multi-stage builds, `--platform` flags, stage aliases, tags, digests, and
simple `ARG NAME=default` substitution used by `FROM` are all handled.
`scratch` images and reuse of a build stage are ignored. Untagged or
`latest` images, digest-only references, and unresolved variables produce
**warnings** (never guessed records). Known images — Python, Node, Go, .NET,
Ubuntu, Debian, Alpine, PostgreSQL, MySQL, Redis, nginx — map to real
lifecycle products when the tag provides a valid version cycle.

### GitLab CI (local-only, never downloaded)

Job-level, `default:`, and top-level `image:` and `services:` entries are
read in both scalar and `name:` object form; variables are used only to
resolve image references and their values are never copied into the config.

- **Local includes** (`include: local: ...` or a relative path) are followed
  only when the target stays **inside the scanned folder** (depth-limited,
  cycle-safe). Anything escaping the folder is warned about and skipped.
- **Remote includes** (`project:`, `remote:`, `template:`, `component:`) are
  *recognized and recorded as warnings* but **never downloaded or fetched**.
- Anchors/aliases/merge keys and inline JSON-style mappings produce warnings
  rather than partial guesses.
- CI configuration is **never executed**.

### What is deliberately skipped

`.git`, `node_modules`, `.venv`, `venv`, `vendor`, `target`, `bin`, `obj`,
`dist`, `build` are never descended into. Add your own exclusions via a
`.eolignore` file in the scanned folder (one pattern per line, `#` comments
allowed) or repeatable `--exclude` flags. Huge scans are bounded (file-size
and file-count guards) and unreadable or oversize files become warnings, not
crashes.

---

## What you get back

### Provenance: every answer shows its evidence

Every mapped product in the config carries a `_found_in` array — the exact
file, and where possible the line or locator (`{"path":
"services/api/requirements.txt", "manifest": "requirements", "line": 14}`).
The same dependency declared in several places merges all of its locations.
Underscore-prefixed keys (`_found_in`, `_comment`, `_inventory`) are ignored
by the tracker runtime; they exist so humans and reports can answer *"where
did this come from?"*.

The config also carries an ignored `_inventory` object: a schema version,
generator version, scan-root name, the list of manifests scanned, summary
counts (files, records, products, unmapped, warnings, indirect), structured
warnings, and the unmapped items. The inventory report is rendered from it.

### Nothing silently disappears

Items the scanner cannot map to a live data source — no lifecycle mapping,
unresolved version expressions, unpinned requirements, unknown container
images — are kept visible twice: in `_inventory.unmapped` with a reason, and
as explicit `manual` tracker rows (with no EOL date) that render as
**UNTRACKED** in every report. You are never left wondering whether an item
was missed.

### Direct dependencies by default

The default inventory is **direct dependencies and explicit runtimes only**;
lock files resolve versions for those declarations rather than producing a
full bill of materials. Pass `--include-transitive` to also include
indirect/lockfile records (Go `// indirect` requires, `Pipfile.lock` and
`packages.lock.json` graph entries), each still provenance-tracked.

---

## Updating an existing config safely

The scanner refuses to overwrite an existing output file by default (exit
code 2 with instructions). You choose one of two explicit modes:

```bash
# macOS / Linux
python helper_scripts/generate_config.py my-project --name my-project --update
python helper_scripts/generate_config.py my-project --name my-project --replace
```

```powershell
# Windows
python helper_scripts\generate_config.py my-project --name my-project --update
python helper_scripts\generate_config.py my-project --name my-project --replace
```

- **`--update` (recommended)** merges fresh scan evidence into the existing
  config **without deleting curation**: `_comment` provenance, `policy_note`s,
  manual dates/notes, and `_section` grouping are all preserved. Versions and
  labels are refreshed where the scan found the same component; baseline
  entries the scan did not observe are **kept, not dropped** (counted as
  `retained_not_observed`); genuinely new components are appended under a
  `=== Newly Discovered ===` section. A summary is written to
  `_inventory.update_summary`.
- **`--replace`** overwrites the config wholesale with what the scan found.
  Use only when you mean it — curation is lost.

---

## The inventory report (Markdown / CSV / HTML)

```bash
# macOS / Linux — all three formats by default
python helper_scripts/generate_inventory_report.py eol_config.my-project.json
```

```powershell
# Windows
python helper_scripts\generate_inventory_report.py eol_config.my-project.json
```

Defaults write to `reports/inventory/<project>-inventory.md`, `.csv`, and
`.html`. Options: `--output FILE` (Markdown), `--csv [FILE]` / `--no-csv`,
`--html [FILE]` / `--no-html`, and `--force` to overwrite every selected
report file, including explicitly named CSV and HTML paths.

The Markdown report contains:

- scan date, generator version, files scanned, and warning count;
- **tracked products grouped by ecosystem and provider**, each with its
  version, source, every provenance location, and an **Inferred** marker for
  auto-derived entries (e.g. Spring Security paired to a Spring Boot version);
- **container images** and their declaration sites (tracked and unmapped);
- **unmapped and unresolved dependencies** with reasons;
- warnings; and summary counts by ecosystem, provider, and review state;
- a **manual review checklist**.

The CSV has one row per product/unmapped item for spreadsheets; the HTML is
self-contained and escaped for sharing. Reports work on **legacy configs**
too: missing provenance renders as `not recorded` rather than failing. The
report is rendered locally — no network calls.

---

## Command reference

All commands run from the repository root. `python` below stands for
whichever of `python` / `python3` / `py` your machine has (the wrappers pick
one for you and version-check it).

### `generate_config.py`

```
python helper_scripts/generate_config.py <folder> [--name PROJECT] [--output FILE]
                                         [--exclude PATTERN] [--update | --replace]
                                         [--include-transitive] [--strict]
```

| Option | Meaning |
|---|---|
| `<folder>` | Directory to scan recursively (required) |
| `--name PROJECT` | Project name (default: the folder's basename, lower-cased, spaces → `-`) |
| `--output FILE` | Output file (default: `eol_config.<name>.json`) |
| `--exclude PATTERN` | Extra exclusion pattern, repeatable (same syntax as `.eolignore`) |
| `--update` | Merge into an existing config, preserving curation (see above) |
| `--replace` | Replace an existing config wholesale (explicit) |
| `--include-transitive` | Also include indirect/lockfile dependencies |
| `--strict` | Exit non-zero if any scan warning was emitted (useful in CI) |

Exit codes: `0` success; `1` `--strict` warnings; `2` refused to overwrite,
or unreadable/unusable existing config on `--update`.

### `generate_inventory_report.py`

```
python helper_scripts/generate_inventory_report.py <config> [--output FILE]
                                                   [--csv [FILE]] [--html [FILE]]
                                                   [--no-csv] [--no-html] [--force]
```

| Option | Meaning |
|---|---|
| `<config>` | Path to an `eol_config.*.json` file (required) |
| `--output FILE` | Markdown path (default: `reports/inventory/<project>-inventory.md`) |
| `--csv [FILE]` | CSV path (default file next to the Markdown; present by default) |
| `--html [FILE]` | HTML path (default file next to the Markdown; present by default) |
| `--no-csv` / `--no-html` | Suppress that format |
| `--force` | Overwrite existing report files |

Exit codes: `0` success; `2` refused to overwrite (no `--force`) or unreadable
config.

### Wrapper scripts

| Script | Platform | Behaviour |
|---|---|---|
| `generate_config.sh` / `generate_config.ps1` | Bash / PowerShell | No arguments → interactive wizard (which also regenerates the inventory report). With arguments → forwarded verbatim to `generate_config.py`; no report is rendered. |
| `generate_inventory_report.sh` / `generate_inventory_report.ps1` | Bash / PowerShell | No arguments → numbered config picker. First argument selects a config (shorthand or path); the rest are forwarded to the Python CLI. |
| `run.sh` / `run.ps1` (repository root) | Bash / PowerShell | Interactive picker that runs the **tracker** (`lambda_function.py`) on a config. |

The four wrappers in `helper_scripts/` locate the repository root from their
own location, preserve quoted paths containing spaces, return the Python exit
code, version-check Python 3.9+, and print copy-paste recovery steps when it is
missing. The repository-root `run.*` scripts are the existing tracker runners.

---

## Troubleshooting

- **"Python 3.9+ is required..."** — install Python from
  <https://www.python.org/downloads/>, tick *"Add python.exe to PATH"* during
  install on Windows, open a **new** terminal, and re-run.
- **"Refusing to overwrite existing file"** — the output already exists.
  Re-run with `--update` (keep curation) or `--replace` (start over), as
  printed in the message.
- **Warnings in the output / report** — each names a file and a reason
  (unreadable file, oversize input, unsupported syntax, remote or escaping
  include, unresolved variable, ...). Fix the cause, add an exclusion, or use
  `--strict` in CI to make warnings fail the build. One bad manifest never
  erases results from other files.
- **A dependency shows as UNTRACKED / unmapped** — that is intentional
  visibility, not a failure: no automated source covers it. Review the
  `=== Needs Manual Review ===` section, add an entry with a real EOL date if
  a vendor publishes one, or leave it as an honest untracked row.

## Layout

```
helper_scripts/
  generate_config.py               CLI: folder scan -> eol_config.<project>.json
  generate_inventory_report.py     CLI: config -> Markdown/CSV/HTML inventory
  generate_config.sh / .ps1        interactive + forwarding wrappers
  generate_inventory_report.sh / .ps1
  eol_inventory/                   importable package (stdlib only)
    models.py                      normalized records, provenance, warnings
    discovery.py                   deterministic folder walk (scan_folder)
    mappings.py                    version helpers + provider mapping tables
    config_writer.py               de-dup, provenance merging, config assembly
    report_writer.py               Markdown/CSV/HTML rendering
    parsers/                       python, node, java, go, dotnet, docker, gitlab_ci
```

Tests live in `tests/` (`tests/test_inventory_*.py`,
`tests/test_generate_config.py`, `tests/test_pypi_registry.py`,
`tests/test_nuget_registry.py`, `tests/test_go_proxy.py`,
`tests/test_helper_wrappers.py`, ...) and run network-free. The whole
directory is excluded from the Terraform Lambda archive.
