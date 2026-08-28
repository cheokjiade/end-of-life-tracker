# EOL Config Generation Prompt

A reusable, harness-neutral extraction specification. Repository-aware agents
should load `.agents/skills/manage-eol-config/SKILL.md`, which selects this
document for new-config generation. The self-contained **PROMPT** block also
works in a standalone AI chat that cannot see this repository.

**How to use**

1. Open a new AI chat.
2. Copy everything between `=== PROMPT START ===` and `=== PROMPT END ===`.
3. Attach or paste your input files. Replace `<PROJECT_NAME>` if you want a specific name.
4. Send. Review the verification checklist the agent returns *before* deploying the config.

The prompt is self-contained: it does not assume the model can see this repo or run
code. If the chat has web access, the prompt tells it to verify slugs/cycles online;
if not, it flags every uncertain entry for you to confirm with the given `curl` command.

---

=== PROMPT START ===

You are generating a configuration file for an **end-of-life (EOL) tracker**. I will
give you one or more input documents (dependency manifests, Confluence pages, wiki
tables, architecture docs, spreadsheets, or plain prose) that describe the software
components and versions a project runs. Produce a single valid JSON config that tells
the tracker what to monitor.

Target project name: `<PROJECT_NAME>`  (if I didn't specify one, infer a short
lowercase slug from the inputs and tell me what you chose).

## How the tracker uses this config (so you make good judgment calls)

The tracker reads the `products` array and checks each entry against a **data source
("provider")**. Each entry names its provider via a `source` field; if `source` is
omitted it defaults to `endoflife_date`. The eight providers and what they need:

| `source`            | Backing data                                              | Reports            |
|---------------------|-----------------------------------------------------------|--------------------|
| `endoflife_date`    | community API `https://endoflife.date/api/{product}.json` | real EOL dates     |
| `aws_rds_scrape`    | AWS RDS/Aurora release-calendar docs (minor-version EOL)  | AWS support end    |
| `aws_sdk_lifecycle` | AWS SDKs & Tools version-support matrix                   | lifecycle phase    |
| `jackson_lifecycle` | FasterXML Jackson "Releases" wiki (open/closed branches)  | maintained or not  |
| `maven_central`     | Maven Central repository metadata/POMs (release recency)  | staleness, not EOL |
| `npm_registry`      | npm registry `registry.npmjs.org` (recency + deprecation) | staleness; deprecated = alert |
| `manual`            | none — hand-entered or untrackable component              | manual EOL date, else UNTRACKED |
| `tyk_lifecycle`     | Tyk docs LTS support table (parsed from the tyk-docs repo) | Tyk LTS EOL date   |

**Critical mechanic — exact cycle matching.** For `endoflife_date`, the tracker finds
the entry whose `cycle` field *exactly equals* your `version` string. There is no fuzzy
matching: `"3.13"` matches only the cycle literally named `3.13`. A wrong string yields
an error, not a best guess. And endoflife.date is **inconsistent about granularity**:

- Major-only cycles: `postgresql` → `"16"`, `nodejs` → `"20"`, `react` → `"18"`,
  `angular` → `"17"`, `redis` (older) — a single integer.
- Major.minor cycles: `python` → `"3.13"`, `go` → `"1.23"`, `spring-boot` → `"3.3"`,
  `nginx` → `"1.27"`, `terraform` → `"1.11"`, `ubuntu` → `"24.04"`, `vue` → `"3.5"`.

You cannot reliably know which granularity a given product uses from memory. So your
job is to produce a **best-guess cycle string AND flag it for verification** (see the
Verification Checklist requirement below). Never silently guess.

## Output contract

Return, in this order:

1. A short line stating the project name you used and how many tracker entries you produced.
2. **One fenced ```json block** containing the complete config (the file the user will save).
3. A **Manual Verification Checklist** (see format below).
4. A **Needs-Manual-Review** list for anything you could not confidently map.

Do not split the config across multiple blocks. Do not add prose inside the JSON except
via the `_comment` / `_section` fields described below.

## Config schema

Top-level object:

```json
{
  "_comment": ["free-text notes; array of strings; ignored by the tracker"],
  "alert_thresholds_days": [30, 60, 90],
  "notify_when": "always",
  "notifications": [ ... ],
  "products": [ ... ]
}
```

- `alert_thresholds_days`: keep `[30, 60, 90]` unless the inputs imply otherwise.
- `notify_when`: `"always"` (daily report regardless) or `"alerts_only"` (only when
  something is EOL/approaching — including undated at-risk phases — or the tracker
  reports `error`/`unknown` health failures). Default to `"always"`.
- `notifications`: default to console + a timestamped HTML file, and include a commented
  SNS entry for the real deploy:

```json
"notifications": [
  {"type": "console"},
  {"type": "html_file", "path": "eol_report_<PROJECT_NAME>.html"},
  {"type": "sns", "_comment": "topic_arn is supplied at runtime by the deploy; leave as-is"}
]
```

Supported notification types (include only what the inputs call for):
`console` · `html_file` (needs `path`) · `sns` (optional `topic_arn`) ·
`ses` (optional `from_email`, `to_emails: [...]`).

SNS and SES are required delivery paths by default; console and `html_file`
are optional. Add boolean `required` only when the source material explicitly
overrides that default (for example `{"type": "sns", "required": false}`).

`html_file` is local-only by default: inside the AWS Lambda deployment a
relative `path` (or any absolute path outside `/tmp`) makes that channel skip
with a warning. Only emit an explicit absolute `/tmp/...` path when a caller
asks for in-Lambda scratch output — `/tmp` there is ephemeral, so durable
AWS-hosted reports should use SNS/SES or S3 instead. Local runs keep writing
under `reports/<project>/<year>/<month>/<day>/`.

### Product entry shapes — one per source

**1. `endoflife_date` (default — omit `source`).** For languages, runtimes, OS/distros,
databases, web servers, and well-known frameworks/libraries that endoflife.date tracks.

```json
{"product": "python", "version": "3.13", "label": "Python 3.13"}
```

- `product`: the exact endoflife.date **slug** — the last path segment of the product's
  page URL, e.g. `https://endoflife.date/postgresql` → `"postgresql"`. Slugs are
  lowercase, hyphenated.
- `version`: the cycle string (see granularity note above).
- `label`: human-readable, conventionally `"<Name> <version>"`.

Slugs known to this tracker (non-exhaustive — endoflife.date has hundreds; use the exact
slug from the product page): `python`, `nodejs`, `go`, `php`, `ruby`, `nginx`,
`postgresql`, `mysql`, `mariadb`, `redis`, `ubuntu`, `debian`, `alpine-linux`, `rhel`,
`amazon-linux`, `terraform`, `docker-engine`, `kubernetes`, `spring-boot`,
`spring-framework`, `spring-security`, `tomcat`, `log4j`, `kotlin`, `scala`,
`amazon-corretto`, `react`, `vue`, `angular`, `nextjs`, `nuxt`, `express`,
`ckeditor`, `bootstrap`, `jquery`, `font-awesome`, `splunk`, `mongodb`, `rhel`,
`apache-groovy`. If unsure a slug exists, **flag it** rather than invent it.

> Note: `typescript` is NOT currently served at `/api/typescript.json` — track it via
> `npm_registry` (`package: typescript`) instead. `generate_config.py` no longer
> auto-maps it: a `typescript` npm dependency lands in `_skipped_npm_packages`.

**2. `aws_rds_scrape`.** For AWS RDS / Aurora **PostgreSQL minor** versions (endoflife.date
only tracks majors). `engine` must be `"aurora-postgresql"` or `"rds-postgresql"`.

```json
{"source": "aws_rds_scrape", "engine": "aurora-postgresql", "version": "17.5",
 "label": "AWS RDS Aurora PostgreSQL 17.5"}
```

**3. `aws_sdk_lifecycle`.** For AWS SDKs (per major version).

```json
{"source": "aws_sdk_lifecycle", "sdk": "SDK for Java", "major": "2.x",
 "label": "AWS SDK for Java v2"}
```

- `sdk`: as named in the AWS matrix, e.g. `"SDK for Java"`, `"SDK for JavaScript"`,
  `"SDK for Python (Boto3)"`. `major`: e.g. `"2.x"`, `"1.x"`.

**4. `jackson_lifecycle`.** For the FasterXML Jackson library, by branch — one entry
**per artifact** (carry `group` and `artifact`), never one collapsed row per branch.

```json
{"source": "jackson_lifecycle", "group": "com.fasterxml.jackson.core",
 "artifact": "jackson-databind", "version": "2.18", "label": "Jackson Databind 2.18"}
```

- `version`: the branch, `major.minor` (e.g. `2.18`).
- `label`: `"Jackson <Artifact> <major.minor>"`, where `<Artifact>` is derived from
  the artifact id (`jackson-databind` → `Databind`, `jackson-bom` → `BOM`,
  `jackson-annotations` → `Annotations`). Two artifacts on the same branch are two
  separate rows — `com.fasterxml.jackson.core:jackson-annotations:2.21` is
  `Jackson Annotations 2.21`, which is separate from `Jackson BOM 2.21`.

**5. `maven_central`.** For Java/Kotlin libraries that publish **no lifecycle data**
(Apache Commons, Netty, Logback, Quartz, jsoup, OkHttp, etc.). Reports how stale the
pinned version is — no EOL is claimed.

```json
{"source": "maven_central", "group": "io.netty", "artifact": "netty-codec-http",
 "version": "4.1.100.Final", "label": "Netty Codec HTTP 4.1.100"}
```

- Needs `group`, `artifact`, and the **full** version (do not truncate Maven versions).
- Optional `repository`: the absolute http(s) **base URL** of an alternative
  Maven 2 repository layout, used when the artifact is **not published to
  Maven Central**, e.g. Shibboleth-hosted artifacts (`org.opensaml`,
  `net.shibboleth.*`, repository base
  `https://build.shibboleth.net/nexus/content/repositories/releases`). Base
  URL only — no credentials, query string, or fragment (scheme and host are
  lowercased). Omit the field for anything available on Maven Central.

**6. `npm_registry`.** For npm / JavaScript libraries that publish no EOL dates
(Material UI, Axios, Redux, Day.js, DOMPurify, and most webjars/frontend deps). Like
`maven_central`, it reports registry staleness — but it also **alerts** when npm marks
the in-use version `deprecated`.

```json
{"source": "npm_registry", "package": "axios", "version": "1.9.0", "label": "Axios 1.9.0"}
```

- `package`: the exact npm package name. Scoped names are fine (`@mui/material`,
  `@testing-library/react`). `version`: the in-use version; may be **omitted** to report
  only the latest (useful when the inputs give a name but no pinned version).
- **Version-specific package renames:** for a component pinned to an OLD major that has
  since been renamed, use the OLD package name — e.g. React Query v3 → `react-query`
  (NOT `@tanstack/react-query`), React Table v7 → `react-table`. Verify the
  `package`+`version` resolves at `https://registry.npmjs.org/<package>`.

**7. `manual`.** For components with **no automated source anywhere**: commercial
software not covered by endoflife.date or a specialized provider, OS-bundled packages
lacking an upstream endoflife.date slug (OpenSSH), or tools that publish no lifecycle
at all (PuTTY).

```json
{"source": "manual", "label": "Vendor Tool 4.2", "eol_date": "2027-06-30",
 "note": "Date supplied by the inventory document", "reference_url": "https://vendor.example/support"}
```

- `label` required. `eol_date` (`YYYY-MM-DD`), `note`, `reference_url`, `version`,
  `latest` all optional. **With** an `eol_date` the tracker shows a real countdown;
  **without** one the row renders as **UNTRACKED** (visible, but no EOL claimed). Use a
  manual `eol_date` when the input document itself states an EOL/expiry date the tracker
  can't otherwise source. For an OS-bundled package with no upstream slug, set `eol_date`
  to the owning OS's EOL and say so in `note`.

**8. `tyk_lifecycle`.** For Tyk API-gateway components (Gateway, Dashboard, MDCB, Pump).
Tyk isn't on endoflife.date but publishes an LTS support table in its docs, which this
provider scrapes automatically.

```json
{"source": "tyk_lifecycle", "version": "5.8", "label": "Tyk Gateway 5.8"}
```

- `version`: the Tyk **Gateway** LTS line as `major.minor` (`5.8`, `5.13`, …). Dashboard,
  MDCB, and Pump follow the Gateway release train — use the corresponding Gateway
  major.minor for them too and note their own version in `_comment`.

### Organizational fields (optional, improve reviewability)

- Insert divider objects between logical groups:
  `{"_section": "=== Java dependencies ==="}` — the tracker skips these.
- Add provenance to each real entry so a human can trace it:
  `"_comment": "From pom.xml (org.springframework.boot:...:3.3.4)"`.
- Add a `policy_note` to a **no-EOL-date platform/infra** entry (not plain libraries):
  a 1-2 sentence, ASCII observation of its real release/support policy, shown as a muted
  sub-line in the report. Use it where a blank EOL date is misleading - e.g. nginx
  (`"New stable branch about yearly; older branches dropped once superseded."`), Apache
  HTTP, Tomcat, Squid, ElastiCache, AWS SDK, React/Bootstrap/jQuery/Font Awesome/Groovy,
  Log4j, and manual/UNTRACKED tools (PuTTY, Jenkins remoting). Skip it for ordinary
  Maven/npm libraries, where "on latest, no formal EOL" already says everything.

## How to map inputs → entries

Apply this decision order per component you find:

1. **Is it a language / runtime / OS / database / web server / mainstream framework?**
   → `endoflife_date`. Map to the correct slug. Derive the cycle:
   - Strip patch levels and qualifiers: `3.5.7` → `3.5` (or `3` if that product uses
     major-only cycles); `4.1.100.Final` stays full *only* for `maven_central`.
   - Strip semver range operators and build metadata: `^18.2.0`, `>=1.4 <2`,
     `1.2.3-SNAPSHOT` → take the base release (`18` or `18.2`, `1.2`).
2. **Java/Kotlin library with a lifecycle source?** Spring* / Tomcat / Log4j / Kotlin /
   Scala → `endoflife_date`. Jackson (`com.fasterxml.jackson*`) → `jackson_lifecycle`,
   one entry **per artifact** (with `group`/`artifact` keys and a
   `Jackson <Artifact> <major.minor>` label).
   AWS SDK (`software.amazon.awssdk` = v2, `com.amazonaws:aws-java-sdk*` = v1) →
   `aws_sdk_lifecycle`.
3. **Any other Java/Kotlin library?** → `maven_central` with full `group`/`artifact`/`version`.
   Shibboleth-hosted groups (`org.opensaml`, `net.shibboleth`,
   `net.shibboleth.*`) are auto-mapped
   to the Shibboleth repository, not Maven Central: give those entries the
   optional `repository` field described above (they 404 on Maven Central).
4. **npm / JavaScript library** (frontend deps, webjars like Bootstrap/jQuery/Chart.js,
   Node tooling)? → `npm_registry` with the npm `package` name (+ `version` if known). If
   it ALSO has an endoflife.date slug (`nodejs`, `angular`, `vue`, …), prefer
   `endoflife_date` for the real EOL.
5. **AWS RDS/Aurora PostgreSQL minor version** mentioned in ops/infra docs? → `aws_rds_scrape`.
6. **Commercial / infrastructure software?** Check endoflife.date FIRST — many are there
   (`splunk`, `mongodb`, `jenkins`, `rhel`, `amazon-elasticache-redis`, `nginx`, `squid`,
   `tomcat`, …). **Tyk** (Gateway/Dashboard/MDCB/Pump) → `tyk_lifecycle`. Only when there
   is genuinely no automated source anywhere (PuTTY; an OS-bundled tool like OpenSSH with
   no upstream slug) → `manual`: put any input-stated EOL date in `eol_date`, else leave
   it out (renders UNTRACKED). **Never drop a component — make it `manual` so it stays
   visible — but always prefer an automated source over a hardcoded manual date when one
   exists** (a live source stays current; a manual date rots and needs re-checking).
7. **OS-distro packages** (bundled with Amazon Linux, RHEL, Ubuntu…): note the OS
   provenance in `_comment`. If the package has an upstream endoflife.date slug (openssl,
   squid, apache-http-server, nginx), use `endoflife_date`; if it does NOT (openssh), use
   `manual` tied to the OS's EOL date.
8. **Named in prose / a Confluence table**? Treat each row/bullet as a component: pull out
   the product name + version, then apply steps 1–7. Tables like "Component | Version |
   Owner" map cleanly; when a cell is a range or "latest", record your interpretation in
   `_comment` and add it to the verification checklist.
9. **After choosing a source, is the item platform/infra with no EOL date** (endoflife.date
   `eol: false`, an `aws_sdk_lifecycle` GA line, or a `manual` UNTRACKED tool)? Research and
   add an ASCII `policy_note` describing its release/support policy. Verify the claim before
   writing. Do not add notes to ordinary libraries.

### Static scanner coverage (generate_config.py)

For clean dependency folders, `python generate_config.py <folder> --name <project>` parses
these manifest forms automatically; anything the manifests use outside this list must be
extracted manually (the scanner silently misses it):

- `pom.xml`: versioned `<dependency>`s at any depth, `<parent>`, and POM properties
  (`tomcat.version`, `kotlin.version`, ...). Deps inside `<dependencyManagement>` are
  parsed as BOM/version **declarations** (kind `managed-dep`); deps with no `<version>`
  (parent/BOM-managed) are recorded as kind `unversioned-dep` and produce **no** tracker
  entry — add those manually.
- `build.gradle` / `build.gradle.kts`: quoted GAV strings in single or double quotes
  (`implementation 'g:a:v'`), Groovy map notation and kts named args
  (`group: 'g', name: 'a', version: 'v'`), `platform(...)` BOM imports, plugins blocks
  (`id("g.a") version "v"`, `kotlin("jvm") version "v"` — plugin ids are converted to
  best-effort Maven coordinates), and `libs.*` references resolved against
  `libs.versions.toml` (best-effort TOML subset: `[versions]`, `[libraries]`,
  `[bundles]`; unresolvable aliases are skipped).
- Versions that never produce entries (no public registry resolves them): `-SNAPSHOT`,
  `${property}` placeholders, Maven ranges (`[2.0,)`), and Gradle dynamic versions
  (`2.+`, `latest.release`, `latest.integration`).
- `package.json`: `dependencies`/`devDependencies`/`engines.node`; known packages map to
  endoflife.date entries, the rest land in `_skipped_npm_packages`.

The scanner is a heuristic regex/JSON/XML pass, not a build — after running it, diff its
output against the inputs and hand-map whatever it missed. Do not assume silence means
absence.

### Real-world document patterns

Confluence/wiki EOL tables and inventories are messy. Handle these consistently:

- **Strikethrough** (`~~text~~`, or a "decommissioned" / "migrated" / "to be removed"
  note) → the component is gone. **Skip it** (emit no entry).
- **"was X, now Y"** — a struck-out old version beside a new one (`~~3.5~~ 4.0.7`), or
  "upgraded to Y" → track the **current** version (`Y`).
- **Multiple versions in one cell** — several apps/instances on different versions
  (`4.1.132 / 4.2.15.Final`, `2.18.0 / 3.1.2`) → emit ONE entry for the **primary /
  most-current in-use** version and list the rest in `_comment`. Never emit a broken
  combined string as the `version`.
- **Explicit EOL/expiry date stated in the document** — if a row gives an EOL date the
  tracker can't source automatically (commercial software; "License expires 25 Apr 2027")
  → `manual` entry with that date in `eol_date`, citing the row in `note`.
- **Reference URLs are slug hints** — `endoflife.date/nginx` → slug `nginx`;
  `mvnrepository.com/artifact/io.netty/netty-tcnative-classes` → `maven_central`
  `io.netty` / `netty-tcnative-classes`; a GitHub releases link → the project's registry
  (npm or Maven). Use the hint, then verify.
- **"Latest" / "N/A" / blank version** — record the interpretation in `_comment` and the
  checklist; for `npm_registry` / `maven_central` you may omit `version` to report the latest.

### Skip / do not fabricate

- Test & build tooling **can now be tracked** — `npm_registry` (Jest, ESLint, Prettier,
  `@testing-library/*`) or `maven_central` (JUnit, Mockito, Awaitility). Include them as
  staleness entries unless the inputs mark them as being removed.
- Skip internal/first-party artifacts, `-SNAPSHOT` builds, and unresolved `${property}`
  placeholders — they resolve on no public registry.
- If you cannot confidently determine the slug, source, or version for something,
  **do not invent an entry.** Put it in the Needs-Manual-Review list — or, if you at
  least know its name and a vendor EOL date, a `manual` entry.

## Verification Checklist (required)

Because cycle-string granularity varies per product, after the JSON output a checklist
covering **every `endoflife_date` entry** plus anything else you were unsure about.
For each, give the one-line command the user can run to confirm the cycle exists:

```
- python 3.13   → curl -s https://endoflife.date/api/python.json | grep '"cycle"'
- postgresql 16 → curl -s https://endoflife.date/api/postgresql.json | grep '"cycle"'   ⚠ verify major-only vs major.minor
```

Mark with ⚠ any entry where you were uncertain whether the cycle is major-only or
major.minor, or whether the slug is correct. **If this chat has web access, actually
fetch each `https://endoflife.date/api/{slug}.json`, confirm the exact `cycle` value,
correct the config, and note "verified" instead of ⚠.**

## Needs-Manual-Review (required if anything was skipped/ambiguous)

List every component you could not confidently map, with the raw string you saw, the
source file, and why (unknown slug, ambiguous version, no matching provider, etc.). One
line each. Better to surface these than to guess.

## Guardrails

- Prefer flagging over guessing. A flagged entry costs the user 10 seconds; a fabricated
  slug or wrong cycle silently produces a broken "error" row in every future report.
- Do not add products the inputs don't mention. Do not deduplicate away distinct majors
  (e.g. keep both Java 8 and Java 17 if both appear).
- Keep the JSON strictly valid: double-quoted keys/strings, no trailing commas, no comments
  (use `_comment` fields instead).

### Worked mini-example

Input snippet (from a `pom.xml` and a Confluence row):

```
<parent> org.springframework.boot : spring-boot-starter-parent : 3.3.4 </parent>
<dependency> io.netty : netty-codec-http : 4.1.111.Final </dependency>
Confluence "Runtime" table:  PostgreSQL | 16.3 | DBA team
```

Expected entries:

```json
{"_section": "=== Platforms ==="},
{"product": "spring-boot", "version": "3.3", "label": "Spring Boot 3.3",
 "_comment": "From pom.xml (org.springframework.boot:spring-boot-starter-parent:3.3.4)"},
{"product": "postgresql", "version": "16", "label": "PostgreSQL 16",
 "_comment": "From Confluence Runtime table (16.3); major-only cycle — verify"},
{"_section": "=== Java libraries (Maven Central staleness) ==="},
{"source": "maven_central", "group": "io.netty", "artifact": "netty-codec-http",
 "version": "4.1.111.Final", "label": "Netty Codec HTTP 4.1.111",
 "_comment": "From pom.xml (io.netty:netty-codec-http:4.1.111.Final)"}
```

...followed by a verification checklist for `spring-boot 3.3` and `postgresql 16`
(⚠ major-only), and an empty Needs-Manual-Review list.

Now read the inputs I provide next and produce the config.

--- INPUTS BELOW ---

<paste dependency files / Confluence pages / documents here, or attach them>

=== PROMPT END ===
