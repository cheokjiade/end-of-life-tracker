# Policy / Support-Observation Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any config entry carry an optional `policy_note`, and surface it (plus the already-computed `support_message`) as a muted observation sub-line in both the plain-text and HTML reports — so "no EOL date" items like nginx explain their real support policy.

**Architecture:** One injection point in `check_product` copies `entry["policy_note"]` onto the uniform result dict, so all 8 providers get it for free. `report.py` grows two small helpers (`_append_notes` for text, `_details_html` for HTML) that render observation sub-lines. The curated notes live in the per-project configs; the config-generation docs teach future runs to add them.

**Tech Stack:** Python 3 standard library only. No test framework — verification is standalone `python` assertion scripts.

## Global Constraints

- **Stdlib only** across `eoltracker/` — no third-party imports.
- **Configs must be ASCII.** `load_config_from_file` opens with no explicit encoding (cp1252 on Windows breaks on non-ASCII). Hand-typed `policy_note` text uses **plain ASCII only** — no em-dash (`—`), arrows (`→`), `≈`, or `ⓘ`. Use `-`, `->`, `~`.
- **Text report is ASCII**, HTML report is utf-8. Plain-text note prefix = `Policy:`; HTML note marker = `&#9432;` (the numeric char-ref for ⓘ — never a literal non-ASCII byte in source).
- **Render whenever present** — the renderer does not special-case a blank EOL date; it shows a note for any status. Populate notes mainly for no-EOL items, but the code stays status-agnostic.
- **No commits.** Repo convention leaves work in the working tree (`docs/superpowers/` is untracked, unrelated WIP is present, and `.claude/agents/eol-config-extractor.md` itself rules "Never run git or commit"). Each task ends at **green tests**, not a git commit; review happens on the working-tree diff. The user decides when/whether to commit.
- **Test invocation:** run `python tests/<file>.py` from the repo root `E:\Git\endoflife`. Each test script bootstraps `sys.path` to the repo root so `import eoltracker` resolves. Fixed "today" for all tests: `date(2026, 7, 24)`.
- **Network-free tests** use the `manual` provider (it makes no network call); live network is used only for the config smoke runs in Task 4.

---

### Task 1: Inject `policy_note` in the dispatch chokepoint

**Files:**
- Modify: `eoltracker/parsers/__init__.py` (`check_product`, lines 36-50)
- Test: `tests/test_policy_injection.py` (create)

**Interfaces:**
- Consumes: `check_product(entry: dict, today: date) -> dict | None` (existing).
- Produces: result dict now carries key `"policy_note"` (str) **iff** the entry had a truthy `policy_note`; otherwise the key is absent. `_section` entries still return `None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy_injection.py`:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from eoltracker.parsers import check_product

TODAY = date(2026, 7, 24)

# 1. A truthy policy_note is copied onto the result (manual provider = no network).
entry = {"source": "manual", "label": "PuTTY", "policy_note": "Only newest release supported."}
r = check_product(entry, TODAY)
assert r is not None
assert r.get("policy_note") == "Only newest release supported.", r.get("policy_note")

# 2. No policy_note in the entry -> key absent on the result.
r2 = check_product({"source": "manual", "label": "X"}, TODAY)
assert "policy_note" not in r2, r2

# 3. An empty policy_note is treated as absent.
r3 = check_product({"source": "manual", "label": "Y", "policy_note": ""}, TODAY)
assert "policy_note" not in r3, r3

# 4. Section dividers still return None (and don't crash on the note copy).
assert check_product({"_section": "=== Group ==="}, TODAY) is None

print("OK test_policy_injection")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_policy_injection.py`
Expected: FAIL — `AssertionError` on assertion 1 (`r.get("policy_note")` is `None`, because nothing injects it yet).

- [ ] **Step 3: Write minimal implementation**

In `eoltracker/parsers/__init__.py`, replace the body of `check_product` (currently lines 44-50) so the note is applied to whatever dict is returned — provider path and unknown-source error path alike:

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

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_policy_injection.py`
Expected: PASS — prints `OK test_policy_injection`.

- [ ] **Step 5: Checkpoint (no commit)**

Leave changes in the working tree. Confirm only `eoltracker/parsers/__init__.py` and the new `tests/test_policy_injection.py` changed (`git status --short`). Ready for review.

---

### Task 2: Render notes in the plain-text report

**Files:**
- Modify: `eoltracker/report.py` (add `_append_notes`; wire into the eol/approaching/ok/untracked loops in `format_report_text`)
- Test: `tests/test_policy_text.py` (create)

**Interfaces:**
- Consumes: `format_report_text(results: list[dict], thresholds: list[int], today: date) -> (str, bool)` (existing); result dicts from `check_product`.
- Produces: new module-level helper `_append_notes(lines: list[str], r: dict) -> None` that appends a `    {support_message}` line and/or a `    Policy: {policy_note}` line when those keys are present.

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy_text.py`:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from eoltracker.parsers import check_product
from eoltracker.report import format_report_text

TODAY = date(2026, 7, 24)
TH = [30, 60, 90]

# policy_note on an UNTRACKED item shows up as an ASCII "Policy:" sub-line.
r = check_product({"source": "manual", "label": "PuTTY",
                   "policy_note": "Only newest release supported."}, TODAY)
text, _ = format_report_text([r], TH, TODAY)
assert "Policy: Only newest release supported." in text, text

# Absent note -> no "Policy:" line anywhere.
r2 = check_product({"source": "manual", "label": "X"}, TODAY)
text2, _ = format_report_text([r2], TH, TODAY)
assert "Policy:" not in text2, text2

# support_message still renders (approaching item); manual-inject the field.
r3 = check_product({"source": "manual", "label": "SM", "eol_date": "2026-09-01"}, TODAY)
r3["support_message"] = "Active support until 2026-08-01 (8 days remaining)"
text3, _ = format_report_text([r3], TH, TODAY)
assert "Active support until 2026-08-01" in text3, text3

print("OK test_policy_text")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_policy_text.py`
Expected: FAIL — `AssertionError` on assertion 1 (the untracked loop emits no `Policy:` line yet).

- [ ] **Step 3: Write minimal implementation**

In `eoltracker/report.py`, add this helper immediately after `_append_version_info` (i.e. after its last line `lines.append(cycle_line)` around line 89, before `format_report_text`):

```python
def _append_notes(lines, r):
    """Append support-status and policy observation sub-lines (ASCII for console/SNS)."""
    if r.get("support_message"):
        lines.append(f"    {r['support_message']}")
    if r.get("policy_note"):
        lines.append(f"    Policy: {r['policy_note']}")
```

Then wire it into the four item loops in `format_report_text`:

- **eol loop** — after `lines.append(f"    {r['message']}")`, add before `_append_version_info(lines, r)`:
  ```python
            _append_notes(lines, r)
  ```
- **approaching loop** — replace the existing two lines
  ```python
            if r.get("support_message"):
                lines.append(f"    {r['support_message']}")
  ```
  with:
  ```python
            _append_notes(lines, r)
  ```
- **ok loop** — after `lines.append(f"  * {r['label']}  -  {r['message']}  [{_source_label(r)}]")`, add before `_append_version_info(lines, r)`:
  ```python
            _append_notes(lines, r)
  ```
- **untracked loop** — after `lines.append(f"  * {r['label']}  -  {r['message']}  [{_source_label(r)}]")`, add before `_append_version_info(lines, r)`:
  ```python
            _append_notes(lines, r)
  ```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_policy_text.py`
Expected: PASS — prints `OK test_policy_text`.

- [ ] **Step 5: Re-run Task 1 test (no regression)**

Run: `python tests/test_policy_injection.py`
Expected: PASS.

- [ ] **Step 6: Checkpoint (no commit)**

Leave changes in the working tree; only `eoltracker/report.py` and `tests/test_policy_text.py` changed. Ready for review.

---

### Task 3: Render notes in the HTML report (and surface `support_message`)

**Files:**
- Modify: `eoltracker/report.py` (add `_details_html`; use it in `_html_table_rows`)
- Test: `tests/test_policy_html.py` (create)

**Interfaces:**
- Consumes: `format_report_html(results, thresholds, today) -> (str, bool)` (existing); `html` module already imported at top of `report.py`.
- Produces: new helper `_details_html(r: dict) -> str` returning the Details-cell HTML: provider `message` (unescaped, as today) followed by muted `<br><span>` sub-lines for `support_message` (escaped) and `policy_note` (escaped, prefixed `&#9432; `).

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy_html.py`:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from eoltracker.parsers import check_product
from eoltracker.report import format_report_html

TODAY = date(2026, 7, 24)
TH = [30, 60, 90]

# policy_note renders with the info marker and is HTML-escaped (untrusted free text).
r = check_product({"source": "manual", "label": "T",
                   "policy_note": "a < b & <script>x</script>"}, TODAY)
out, _ = format_report_html([r], TH, TODAY)
assert "&#9432;" in out, "info marker missing"
assert "&lt;script&gt;" in out, "note not escaped"
assert "<script>" not in out, "unescaped note leaked into HTML"

# Absent note -> no info marker.
r2 = check_product({"source": "manual", "label": "X"}, TODAY)
out2, _ = format_report_html([r2], TH, TODAY)
assert "&#9432;" not in out2, out2

# support_message now appears in HTML (previously dropped by the HTML formatter).
r3 = check_product({"source": "manual", "label": "SM"}, TODAY)
r3["support_message"] = "Active support until 2027-01-01 (161 days remaining)"
out3, _ = format_report_html([r3], TH, TODAY)
assert "Active support until 2027-01-01" in out3, out3

print("OK test_policy_html")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_policy_html.py`
Expected: FAIL — `AssertionError "info marker missing"` (the HTML formatter renders only `r["message"]` today).

- [ ] **Step 3: Write minimal implementation**

In `eoltracker/report.py`, add this helper immediately before `_html_table_rows` (after `_status_label`):

```python
def _details_html(r):
    """Details cell: provider message plus muted observation sub-lines.

    The provider-generated `message` is emitted as-is (consistent with prior
    behaviour); the config-authored `policy_note` is HTML-escaped as untrusted
    free text.
    """
    detail = r["message"]
    notes = []
    if r.get("support_message"):
        notes.append(html.escape(r["support_message"]))
    if r.get("policy_note"):
        notes.append("&#9432; " + html.escape(r["policy_note"]))
    for n in notes:
        detail += f'<br><span style="color:#888;font-size:12px">{n}</span>'
    return detail
```

Then, in `_html_table_rows`, change the Details `<td>` from:

```python
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{r["message"]}</td>'
```

to:

```python
            f'<td style="padding:10px 12px;border-bottom:1px solid #e0e0e0">{_details_html(r)}</td>'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_policy_html.py`
Expected: PASS — prints `OK test_policy_html`.

- [ ] **Step 5: Re-run all unit tests (no regression)**

Run: `python tests/test_policy_injection.py && python tests/test_policy_text.py && python tests/test_policy_html.py`
Expected: three `OK ...` lines.

- [ ] **Step 6: Checkpoint (no commit)**

Leave changes in the working tree; only `eoltracker/report.py` and `tests/test_policy_html.py` changed since the last checkpoint. Ready for review.

---

### Task 4: Populate verified policy notes in the active configs (`c`, `d`, `e`)

**Files:**
- Modify: `eol_config.c.json`, `eol_config.d.json`, `eol_config.e.json`
- Test: `tests/test_configs_have_notes.py` (create) + live smoke runs

**Interfaces:**
- Consumes: the `policy_note` field (Task 1) and its rendering (Tasks 2-3).
- Produces: `policy_note` added to the entries below. Notes are ASCII, 1-2 sentences.

**Notes to add (draft text — VERIFY each claim live before writing; correct the wording if the source disagrees). Match entries by product slug / label; an entry may not exist in a given config if that project doesn't track it — only annotate entries that are present.**

| Match (config) | ASCII `policy_note` |
|---|---|
| `nginx` (d, e) | `New stable branch about yearly (each April); once a newer stable branch ships the previous one stops getting updates, so an old stable branch is effectively EOL with no published date.` |
| `apache-http-server` / Apache HTTP 2.4 (c) | `Only the latest 2.4.x is maintained (2.2 and earlier are EOL). 2.4 has no published end date; staying on the newest 2.4.x patch is what matters.` |
| `tomcat` (c: 10.1, e: 11.0) | `Maintains several major branches in parallel, each tied to a Servlet/Jakarta EE version; a branch's EOL is announced (typically ~12 months notice) when activity moves on.` |
| `squid` (e) | `Only the current stable series is supported; once a new major ships, older majors get no further fixes. New majors arrive roughly yearly.` |
| ElastiCache Redis (e) | `AWS announces engine-version deprecations with limited notice and then forces upgrades; older engine versions are the first candidates. Treat 'no EOL' as 'deprecation can come at any time'.` |
| AWS SDK for Java v2 (`aws_sdk_lifecycle`, d, e) | `Generally Available with no announced end-of-support (v1 reached end-of-support 2025-12-31). v2 is the actively developed line; keep reasonably current.` |
| `react` 18 (c) | `No fixed EOL schedule; only the latest major gets patches (deprecation warnings and codemods ease upgrades). Once superseded (18 -> 19) the older major gets no further releases.` |
| `bootstrap` 5 (e) | `Only the latest major is maintained (v4 and earlier get no fixes); no published end date for v5, supported until v6 supersedes it.` |
| `jquery` 3 (e) | `3.x is the only supported line (1.x/2.x are EOL); long-term low-activity maintenance with no scheduled end, but new development has effectively stopped.` |
| `font-awesome` 5 (e) | `Only the latest major (6) gets new icons and fixes; v5 gets none. No formal EOL date; v5 is simply superseded.` |
| `apache-groovy` 3.0 (e) | `Maintains recent branches only; 3.0 is superseded by 4.x/5.x and gets no active fixes. Per-branch EOL is announced by the project.` |
| `log4j` (e) | `Only Log4j 2.x is maintained (1.x reached EOL in 2015); within 2.x only the latest release is patched. Staying current matters given past critical CVEs (Log4Shell).` |
| PSFTP / PuTTY (`manual`, c) | `No release schedule or support policy: only the newest release is effectively supported, best-effort. Security fixes ship only in the latest version, so upgrading is the only remediation.` |
| Jenkins remoting (`manual`, e) | `The agent 'remoting' JAR has no independent lifecycle; it is versioned with the Jenkins controller. Keep it matched to the controller's bundled remoting version.` |

- [ ] **Step 1: Verify the policy claims live**

For each product above, confirm the claim before writing (per the extractor agent's verify discipline). Cheap checks:
- endoflife.date cadence/branches: `python -c "import json,urllib.request as u; d=json.load(u.urlopen('https://endoflife.date/api/nginx.json')); print([(c['cycle'],c.get('releaseDate')) for c in d[:6]])"` (repeat for `tomcat`, `squid`, `bootstrap`, `jquery`, `font-awesome`, `apache-groovy`, `log4j`, `apache-http-server`).
- Editorial facts not in the API (nginx "older branches dropped", AWS SDK v1 EOS date, Log4j 1.x EOL 2015, PuTTY policy, Jenkins remoting versioning): corroborate with `WebFetch`/`WebSearch` on the vendor/project page.
- If a check contradicts the draft, edit the note text (keep it ASCII, 1-2 sentences) before writing.

- [ ] **Step 2: Add the notes to each config**

For each of `eol_config.c.json`, `eol_config.d.json`, `eol_config.e.json`: locate each present entry from the table (by `product`/`label`/`source`+`sdk`) and add a `"policy_note": "..."` key. Preserve valid JSON (no trailing commas). Keep every character ASCII.

- [ ] **Step 3: Write the validation test**

Create `tests/test_configs_have_notes.py`:

```python
import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECT = {
    "eol_config.c.json": 1,   # >= this many entries carry a policy_note
    "eol_config.d.json": 1,
    "eol_config.e.json": 1,
}
for fname, minimum in EXPECT.items():
    path = os.path.join(ROOT, fname)
    raw = open(path, encoding="ascii").read()          # ascii open == cp1252-safe proof
    cfg = json.loads(raw)
    notes = [p for p in cfg["products"] if p.get("policy_note")]
    assert len(notes) >= minimum, f"{fname}: {len(notes)} notes, want >= {minimum}"
    for p in notes:
        assert p["policy_note"] == p["policy_note"].encode("ascii", "ignore").decode(), \
            f"{fname}: non-ASCII policy_note on {p.get('label')}"
print("OK test_configs_have_notes")
```

- [ ] **Step 4: Run the validation test**

Run: `python tests/test_configs_have_notes.py`
Expected: PASS — prints `OK test_configs_have_notes`. (A non-ASCII char or a JSON typo fails here; `open(..., encoding="ascii")` proves the file is cp1252-safe.)

- [ ] **Step 5: Live smoke run (one representative config)**

Run: `python lambda_function.py eol_config.d.json`
Expected: the report includes the nginx and AWS SDK rows with their `Policy:` sub-line in the console output and no new `?? Errors` rows. (Optionally repeat for `c` and `e`; these hit the network and are slower.)

- [ ] **Step 6: Checkpoint (no commit)**

`eol_config.*.json` are gitignored, so they show only as already-untracked/modified — that's expected. Leave in the working tree. Ready for review.

---

### Task 5: Document the field so future configs keep it (+ sample template)

**Files:**
- Modify: `eol_config_generation_prompt.md` (`### Organizational fields`, line ~216; `## How to map inputs → entries`, line ~223)
- Modify: `.claude/agents/eol-config-extractor.md` (`## Workflow` step 3, line ~76; `## Rules`, line ~92)
- Modify: `eol_config.sample.json` (nginx entry + `_comment` block)
- Modify: `CLAUDE.md` (Conventions & gotchas)
- Test: manual read-back (docs); JSON validity for the sample

- [ ] **Step 1: Extend the generation-prompt schema docs**

In `eol_config_generation_prompt.md`, under `### Organizational fields (optional, improve reviewability)`, append a third bullet:

```markdown
- Add a `policy_note` to a **no-EOL-date platform/infra** entry (not plain libraries):
  a 1-2 sentence, ASCII observation of its real release/support policy, shown as a muted
  sub-line in the report. Use it where a blank EOL date is misleading — e.g. nginx
  (`"New stable branch about yearly; older branches dropped once superseded."`), Apache
  HTTP, Tomcat, Squid, ElastiCache, AWS SDK, React/Bootstrap/jQuery/Font Awesome/Groovy,
  Log4j, and manual/UNTRACKED tools (PuTTY, Jenkins remoting). Skip it for ordinary
  Maven/npm libraries, where "on latest, no formal EOL" already says everything.
```

- [ ] **Step 2: Add a decision-order pointer**

In the same file, at the end of the `## How to map inputs → entries` decision list, add a final numbered item (renumber if needed):

```markdown
9. **After choosing a source, is the item platform/infra with no EOL date** (endoflife.date
   `eol: false`, an `aws_sdk_lifecycle` GA line, or a `manual` UNTRACKED tool)? Research and
   add an ASCII `policy_note` describing its release/support policy. Verify the claim before
   writing. Do not add notes to ordinary libraries.
```

- [ ] **Step 3: Teach the extractor subagent**

In `.claude/agents/eol-config-extractor.md`, in `## Workflow` step 3 (`Write ...`), append one sentence:

```markdown
   For platform/infra entries with no EOL date (endoflife.date `eol:false`, `aws_sdk_lifecycle`
   GA lines, `manual` UNTRACKED tools), add a verified ASCII `policy_note` (1-2 sentences) on
   the real release/support policy; skip notes for ordinary libraries.
```

And add a bullet under `## Rules`:

```markdown
- **Annotate misleading "no EOL" rows.** Where a blank EOL date reads as false comfort
  (nginx, Apache HTTP, Tomcat, Squid, ElastiCache, AWS SDK, React/Bootstrap/jQuery, Log4j,
  PuTTY, Jenkins remoting), add a `policy_note`; verify the claim, keep it ASCII.
```

- [ ] **Step 4: Add a worked example to the sample template**

In `eol_config.sample.json`, add a `policy_note` to the existing nginx entry (currently `{"product": "nginx", "version": "1.29", "label": "Nginx 1.29"}`):

```json
    {
      "product": "nginx",
      "version": "1.29",
      "label": "Nginx 1.29",
      "policy_note": "nginx cuts a new stable branch about yearly; once a newer stable branch ships the previous one stops getting updates, so an old branch is effectively EOL with no published date."
    },
```

And add a line to the `_comment` array describing the field, e.g. after the `notify_when` lines:

```json
    "policy_note (optional, any entry): a short ASCII observation shown as a report",
    "  sub-line -- use for no-EOL-date platform/infra (nginx, Tomcat, PuTTY, ...).",
```

- [ ] **Step 5: Validate the sample JSON**

Run: `python -c "import json; json.load(open('eol_config.sample.json', encoding='ascii')); print('sample OK')"`
Expected: prints `sample OK` (valid JSON, ASCII-clean).

- [ ] **Step 6: Note the convention in CLAUDE.md**

In `CLAUDE.md` under `## Conventions & gotchas`, add a bullet:

```markdown
- **`policy_note`** (optional, any config entry) is a short ASCII observation of a
  product's release/support policy. `check_product` copies it onto the result and both
  formatters render it as a muted sub-line (HTML: a `&#9432;` marker; text: `Policy:`).
  Use it for no-EOL-date platform/infra items where a blank EOL date is misleading.
```

- [ ] **Step 7: Checkpoint (no commit)**

Read back each edited doc to confirm the insertions landed under the right headings. Leave everything in the working tree. Ready for final review.

---

## Self-Review

**Spec coverage:**
- Goal 1 (optional field) → Task 1. Goal 2 (one injection point) → Task 1. Goal 3 (render both formats) → Tasks 2-3. Goal 4 (`support_message` in HTML) → Task 3 (test asserts it). Goal 5 (curate + verify notes) → Task 4. Goal 6 (config-gen docs) → Task 5. Sample template + CLAUDE.md → Task 5. ✓ all covered.

**Placeholder scan:** No "TBD/TODO/handle edge cases" — every code and test step carries complete content. The Task 4 note texts are explicitly draft-to-verify, which is the intended live-verification step, not a placeholder gap.

**Type consistency:** `_append_notes(lines, r)` and `_details_html(r)` names/signatures are used identically where referenced. Result key is `policy_note` everywhere (config, injection, both renderers, tests). `format_report_text`/`format_report_html` return `(str, bool)` and tests unpack `(_ , _)` accordingly. `check_product(entry, today)` signature matches all call sites. Test scripts all use the same `sys.path` bootstrap and `TODAY = date(2026, 7, 24)`.
