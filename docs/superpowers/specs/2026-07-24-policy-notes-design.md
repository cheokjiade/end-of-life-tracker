# Policy / support-observation notes in EOL reports

**Date:** 2026-07-24
**Status:** Approved (design decisions confirmed with user)

## Problem

Many tracked products surface in reports with a **blank EOL Date** and a green **OK**
badge. Parsing the three most recent reports (`c`, `d`, `e`, 2026-07-24) shows these fall
into four categories:

| Category | Examples | Why no EOL date |
|---|---|---|
| **Untracked** (manual, no source anywhere) | PSFTP/PuTTY, Jenkins remoting | Vendor publishes no machine-readable schedule |
| **endoflife.date `eol: false`** | nginx 1.30, Apache HTTP 2.4, Tomcat 10.1/11.0, Squid 7, ElastiCache Redis 6.2, React 18, Bootstrap 5, jQuery 3, Groovy 3.0, Log4j | Tracked upstream, but no EOL *announced* |
| **Libraries (Maven/npm)** | Guava, Jackson, Commons-*, Axios, ~50 more | Those sources only check "on latest"; libraries have no EOL *concept* |
| **AWS SDK lifecycle** | AWS SDK for Java v2 | GA, no announced end date |

For the middle two "no-EOL-date" categories the green **OK** / blank-date presentation is
misleading. nginx is the canonical case: endoflife.date reports it as "no EOL announced,"
which reads as reassuring — but nginx cuts a new stable branch ~yearly and stops updating
the previous one, so an old branch is effectively end-of-life **without a date**. The
report gives false comfort.

The user wants these items to carry a short, human-authored **policy / observation note**
that captures the real support reality, rendered in the reports.

## Goals

1. Add an optional **`policy_note`** field to config entries — free-text, backward
   compatible (absent ⇒ no change).
2. Thread it through to the result dict in **one** place so all providers get it.
3. Render it as a muted **observation sub-line** in both the plain-text and HTML reports.
4. Close a related gap: surface the already-computed **`support_message`** in the HTML
   report (today only the text report shows it).
5. Curate + verify notes for ~14 platform/infra products across the active configs
   (`c`, `d`, `e`) and the checked-in `sample` template.
6. Update config-generation docs so future configs populate `policy_note` for no-EOL
   items.

## Decisions (confirmed with user)

- **Mechanism = config-driven per entry.** A curated `policy_note` string per entry, not a
  central knowledge base and not automated derivation. Rationale: policy observations are
  editorial, slow-changing facts absent from any API (e.g. "older branches dropped once
  superseded"); a per-entry field is transparent, self-contained per project, needs no
  network call, and matches how labels/manual notes already work.
- **Scope = platform/infra + notable-policy items only.** nginx, Apache HTTP, Tomcat,
  Squid, ElastiCache, AWS SDK Java v2, React, Bootstrap, jQuery, Font Awesome, Groovy,
  Log4j, PuTTY, Jenkins remoting. The ~50 Maven/npm libraries are **excluded** — "on
  latest, no formal EOL" is the complete and correct story for a library and per-library
  notes would be noise.
- **Render whenever present, not only for no-EOL items.** The renderer does not special-
  case a blank EOL date; it shows the note for any status. We simply *populate* it mainly
  for no-EOL items. Simpler code, and an `approaching` item can carry a note too.
- **Sub-line, not a new column.** The HTML table is already 7 columns wide; a mostly-empty
  8th column is worse than an inline muted sub-line under Details.
- **Plain-text prefix is ASCII (`Policy:`), HTML marker is `&#9432;` (ⓘ).** The text
  report feeds the console/SNS path, which hits Windows cp1252 where non-ASCII breaks
  (CLAUDE.md gotcha). Keep the text report ASCII; the HTML report is charset utf-8 and can
  use the numeric char-ref `&#9432;` (avoids a literal non-ASCII byte in the source too).
- **Verify, don't fabricate.** Every note's policy claim is confirmed against the upstream
  source during execution before it's written.

## Design

### 1. Data model — one optional field

```json
{ "product": "nginx", "version": "1.30", "label": "Nginx 1.30",
  "policy_note": "New stable branch ~yearly (each April); the previous stable branch stops getting updates once superseded, so an old branch is effectively EOL with no formal date." }
```

`policy_note` is optional on **any** entry regardless of `source`. Absent or empty ⇒ no
result key added ⇒ formatters render nothing ⇒ every existing config is unchanged.

### 2. Injection point — `check_product` (`eoltracker/parsers/__init__.py`)

Copy the note onto whatever dict is returned, covering the provider path **and** the
unknown-source error path, in one place (so none of the 8 provider modules change):

```python
def check_product(entry, today):
    if entry.get("_section"):
        return None
    source = entry.get("source", "endoflife_date")
    provider = PROVIDERS.get(source)
    if provider is None:
        result = _error_result(entry, f"Unknown source '{source}'. Known: {sorted(PROVIDERS)}")
    else:
        result = provider(entry, today)
    note = entry.get("policy_note")
    if note and result is not None:
        result["policy_note"] = note
    return result
```

Rationale: the uniform-result-dict contract means one write here reaches every provider —
manual PuTTY, endoflife.date nginx, and a Maven library entry all pick it up identically.

### 3. Rendering (`eoltracker/report.py`)

**Shared idea:** an "observation" is either the computed `support_message` or the config
`policy_note`. Render both, muted, under Details.

**Plain-text** — add one helper and call it in the eol / approaching / ok / untracked item
loops (replacing the ad-hoc `support_message` append currently inline in the approaching
loop at `report.py:118`):

```python
def _append_notes(lines, r):
    """Append support-status and policy observation sub-lines (ASCII, for console/SNS)."""
    if r.get("support_message"):
        lines.append(f"    {r['support_message']}")
    if r.get("policy_note"):
        lines.append(f"    Policy: {r['policy_note']}")
```

(Error items don't get notes — the loop simply doesn't call the helper there.)

**HTML** — build the Details cell through a helper and use it in `_html_table_rows`
(replacing the bare `{r["message"]}`). Config-authored `policy_note` is **HTML-escaped**
(untrusted free text); the provider-generated `message` stays as-is, consistent with
today:

```python
def _details_html(r):
    detail = r["message"]                      # provider-generated; unchanged behaviour
    notes = []
    if r.get("support_message"):
        notes.append(html.escape(r["support_message"]))
    if r.get("policy_note"):
        notes.append("&#9432; " + html.escape(r["policy_note"]))   # &#9432; = U+24D8 ⓘ
    for n in notes:
        detail += f'<br><span style="color:#888;font-size:12px">{n}</span>'
    return detail
```

This simultaneously delivers Goal 4 (support_message now shows in HTML — today the HTML
formatter silently drops it).

### 4. Curated notes to populate (verified at build time)

Proposed text below; each policy claim is confirmed against the cited source during
execution before writing. Notes are ~1–2 sentences to fit a report sub-line.

| Product (config) | `source` | Proposed `policy_note` (draft — verify) |
|---|---|---|
| **nginx** (`d`, `e`, `sample`) | endoflife_date | Cuts a new stable branch ~yearly (≈April); once a newer stable branch ships, the previous one stops receiving updates — an old stable branch is effectively EOL with no published date. |
| **Apache HTTP 2.4** (`c`) | endoflife_date | Only the latest 2.4.x is maintained (2.2 and earlier are EOL). 2.4 has no published end date — supported until a successor line is declared, so staying on the newest 2.4.x patch is what matters. |
| **Tomcat 10.1 / 11.0** (`c`, `e`) | endoflife_date | Maintains several major branches in parallel, each tied to a Servlet/Jakarta EE version; a branch's EOL is announced (typically ~12 months' notice) when activity moves on — no fixed date until then. |
| **Squid 7** (`e`) | endoflife_date | Only the current stable series is supported; once a new major ships, older majors get no further fixes. New majors arrive roughly yearly. |
| **ElastiCache Redis 6.2** (`e`) | endoflife_date | AWS announces engine-version deprecations with limited notice and then forces upgrades; older engine versions are the first candidates — treat "no EOL" as "deprecation can be announced at any time." |
| **AWS SDK for Java v2** (`d`, `e`) | aws_sdk_lifecycle | Generally Available with no announced end-of-support (v1 went end-of-support 2025-12-31). v2 is the actively developed line — keep reasonably current. |
| **React 18** (`c`) | endoflife_date | No fixed EOL schedule; only the latest major receives patches (deprecation warnings/codemods ease upgrades). Once superseded (18 → 19) the older major gets no further releases. |
| **Bootstrap 5** (`e`) | endoflife_date | Only the latest major is maintained (v4 and earlier get no fixes); no published end date for v5 — supported until v6 supersedes it. |
| **jQuery 3** (`e`) | endoflife_date | 3.x is the only supported line (1.x/2.x are EOL); in long-term low-activity maintenance — no scheduled end, but new development has effectively stopped. |
| **Font Awesome 5** (`e`) | endoflife_date | Only the latest major (6) receives new icons and fixes; v5 gets none. No formal EOL date — v5 is simply superseded. |
| **Groovy 3.0** (`e`) | endoflife_date | Maintains recent branches only; 3.0 is superseded by 4.x/5.x and receives no active fixes. Per-branch EOL is announced by the project. |
| **Log4j** (`e`) | endoflife_date | Only Log4j 2.x is maintained (1.x reached EOL in 2015); within 2.x only the latest release is patched — staying current matters given past critical CVEs (Log4Shell). |
| **PSFTP / PuTTY** (`c`) | manual | No release schedule or support policy: only the newest release is effectively supported, best-effort. Security fixes ship only in the latest version, so upgrading is the only remediation. |
| **Jenkins remoting** (`e`) | manual | The agent "remoting" JAR has no independent lifecycle — it's versioned with the Jenkins controller. Keep it matched to the controller's bundled remoting version rather than tracking it separately. |

Exact per-config entry versions may differ from the report labels above; execution locates
each entry by product/label in each config and adds the note. Notes are ASCII (configs are
written with `ensure_ascii=True`; CLAUDE.md gotcha).

### 5. Make it stick for future configs

- `eol_config_generation_prompt.md`: add `policy_note` to the entry-shape docs; add a rule
  — for a no-EOL-date item that is platform/infra (not a plain library), research and add
  a one-line `policy_note` capturing its release/support policy.
- `.claude/agents/eol-config-extractor.md`: same guidance so the extraction subagent fills
  it in and verifies the claim before writing.
- `eol_config.sample.json`: add a `policy_note` to the nginx entry as a worked example and
  mention the field in the `_comment` block.
- `CLAUDE.md`: one line under conventions describing `policy_note` and where it renders.

### 6. Testing (network-free, matching repo style)

Standalone `python` assertion scripts importing the real modules with synthetic data:

- **Injection:** `check_product` copies `policy_note` through for a representative entry
  (stub a provider) and adds no key when the field is absent; `_section` entries still
  return `None`.
- **Formatters:** `format_report_text` emits an ASCII `Policy:` line when the note is
  present and nothing when absent; `format_report_html` emits the muted `&#9432;` span and
  HTML-escapes a note containing `<`/`&`.
- **support_message-in-HTML:** a result carrying `support_message` now renders it in the
  HTML output (regression for Goal 4).
- **Live verification (execution):** confirm each note's policy claim against its source
  before writing; then one live smoke run per touched config
  (`python lambda_function.py eol_config.<p>.json`) confirming notes render and no new
  `error` rows appear.

## Out of scope

- No notes on the ~50 Maven/npm libraries.
- No new report column, no central policy knowledge base, no automated policy derivation.
- No changes to any provider's status/message logic (injection is additive in
  `check_product`).
- No changes to `b`, `b-auto`, `a` configs (not part of the recent
  report set).
- No commits — `docs/superpowers/` is currently untracked in this repo and the working
  tree has unrelated WIP; changes stay in the working tree unless the user asks to commit.
