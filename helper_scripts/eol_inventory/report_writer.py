"""Human-readable inventory rendering: Markdown, CSV, and HTML.

`build_inventory_view` flattens a generated config into a plain view
dict: product rows (label, version, provider, inferred state,
provenance, ecosystem), container rows kept separately, unmapped
records, declarations (every parsed declaration and its outcome),
warnings, and summary counts. `render_markdown` and
`render_csv` turn that view into deterministic reports. Legacy configs
without `_inventory` render with "not recorded" placeholders instead of
failing, and legacy `_skipped_npm_packages` are surfaced as unmapped
rows. Malformed structures (non-object `_inventory`, non-array
containers, junk provenance, unhashable names) never raise: they render
as empty or absent parts plus a structured `malformed_config` warning,
while valid configs keep rendering byte-identically. Standard-library
only; no network; output is ASCII for ASCII inputs.
"""

import csv
import html
import io
import re
from datetime import date

from .models import new_warning, sort_locations
from .redact import redact_display_text, redact_urls


# ---------------------------------------------------------------------------
# Ecosystem inference
# ---------------------------------------------------------------------------

# Registry sources imply the language ecosystem outright.
_PROVIDER_ECOSYSTEMS = {
    "maven_central": "java",
    "jackson_lifecycle": "java",
    "aws_sdk_lifecycle": "java",
    "npm_registry": "node",
    "pypi_registry": "python",
    "nuget_registry": "dotnet",
    "go_proxy": "go",
}

# Manifest names recorded in `_found_in` imply an ecosystem; first match
# wins. Tokens match exactly or as a substring, so the parser manifest
# names ("go", "dotnet", "dockerfile") and file-shaped names ("go.mod",
# "global.json") both resolve.
_MANIFEST_ECOSYSTEM_RULES = (
    ("maven", "java"),
    ("gradle", "java"),
    ("npm", "node"),
    ("requirements", "python"),
    ("pipfile", "python"),
    ("pyproject", "python"),
    ("python", "python"),
    ("go.mod", "go"),
    ("go", "go"),
    ("csproj", "dotnet"),
    ("props", "dotnet"),
    ("packages.lock", "dotnet"),
    ("global.json", "dotnet"),
    ("dotnet", "dotnet"),
    ("docker", "container"),
    ("gitlab_ci", "container"),
)


def _infer_ecosystem(entry, provenance):
    """Deterministic ecosystem for one product entry (never raises)."""
    provider = str(entry.get("source") or "endoflife_date").lower()
    if provider in _PROVIDER_ECOSYSTEMS:
        return _PROVIDER_ECOSYSTEMS[provider]
    for loc in provenance:
        manifest = str(loc.get("manifest", "")).lower()
        for token, ecosystem in _MANIFEST_ECOSYSTEM_RULES:
            if manifest == token or token in manifest:
                return ecosystem
    return "other"


# ---------------------------------------------------------------------------
# View construction
# ---------------------------------------------------------------------------

def _scalar_text(value, default=""):
    """Display text for a scalar field: strings pass through, junk is
    stringified so sorting and rendering never compare exotic types."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _hashable(value):
    """`value` when hashable, else its string form (set keys never raise)."""
    try:
        hash(value)
    except TypeError:
        return str(value)
    return value


def _as_list(value, path, sink):
    """The value as a JSON array: absent stays empty, junk warns once."""
    if isinstance(value, list):
        return value
    if value is not None:
        sink.append(new_warning(
            "malformed_config", path,
            "expected a JSON array; value ignored"))
    return []


def _redacted_text(value):
    """Display text for a spec field: strings pass through the display
    sanitizer (URLs plus SSH/SCP references) and JSON scalars (numbers,
    booleans) pass through -- they have no credential capacity; any
    other JSON value (a hostile dict or list carrying credential text)
    redacts through its string form, which is exactly what the
    renderers would otherwise emit raw. None stays None (absent/empty
    rendering)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return redact_display_text(value) if isinstance(value, str) \
            else value
    return redact_display_text(str(value))


def _comment_text(entry):
    """The entry's `_comment` as one lowercase string (string or list)."""
    comment = entry.get("_comment")
    if isinstance(comment, (list, tuple)):
        return " ".join(str(part) for part in comment).lower()
    if comment is None:
        return ""
    return str(comment).lower()


def _product_label(entry):
    """Best display label for any entry shape (label > product > coords)."""
    for key in ("label", "product"):
        if entry.get(key):
            return str(entry[key])
    group, artifact = entry.get("group"), entry.get("artifact")
    if group and artifact:
        return f"{group}:{artifact}"
    if artifact:
        return str(artifact)
    for key in ("package", "module", "name"):
        if entry.get(key):
            return str(entry[key])
    sdk = entry.get("sdk")
    if sdk:
        major = entry.get("major")
        return f"{sdk} {major}" if major else str(sdk)
    return "unnamed product"


def _norm_location(loc):
    """One provenance location with the keys the sort/format helpers need.

    Non-string paths/manifests are stringified and non-integer lines are
    dropped, so deterministic sorting never compares exotic types.
    """
    norm: dict[str, object] = {
        "path": redact_display_text(_scalar_text(loc.get("path"))),
        "manifest": redact_display_text(_scalar_text(loc.get("manifest")))}
    line = loc.get("line")
    if isinstance(line, int) and not isinstance(line, bool):
        norm["line"] = line
    if loc.get("locator"):
        norm["locator"] = redact_display_text(str(loc["locator"]))
    return norm


def _norm_locations(locations, sink, path):
    """Sorted normalized provenance; junk shapes become empty with a
    structured `malformed_config` warning (never raises)."""
    norm = []
    for loc in _as_list(locations, path, sink):
        if not isinstance(loc, dict):
            sink.append(new_warning(
                "malformed_config", path,
                "expected a JSON object for each provenance location; "
                "entry ignored"))
            continue
        norm.append(_norm_location(loc))
    return sort_locations(norm)


def _product_row(entry, sink):
    """One normalized tracked/container row from a product entry."""
    provenance = _norm_locations(entry.get("_found_in"), sink, "_found_in")
    ecosystem = _infer_ecosystem(entry, provenance)
    provider = entry.get("source") or "endoflife_date"
    if not isinstance(provider, str):
        provider = str(provider)
    provider = redact_display_text(provider)
    details = []
    for key in ("policy_note", "note", "image_reference", "registry",
                "repository", "tag", "digest", "reference_url"):
        if entry.get(key) not in (None, ""):
            details.append(f"{key}={entry[key]}")
    return {
        "label": redact_display_text(_product_label(entry)),
        "version": _redacted_text(entry.get("version")),
        "provider": provider,
        "inferred": "auto-derived" in _comment_text(entry),
        "ecosystem": ecosystem,
        "container": ecosystem == "container",
        "provenance": provenance,
        "details": redact_display_text("; ".join(details)),
    }


def _unmapped_row(item, sink):
    """One normalized unmapped row from an `_inventory.unmapped` item."""
    details = []
    for key in ("image_reference", "registry", "repository", "tag", "digest"):
        if item.get(key) not in (None, ""):
            details.append(f"{key}={item[key]}")
    ecosystem = item.get("ecosystem") or "other"
    if not isinstance(ecosystem, str):
        ecosystem = str(ecosystem)
    return {
        "ecosystem": redact_display_text(ecosystem),
        "name": redact_display_text(str(item.get("name", ""))),
        "version": _redacted_text(item.get("version")),
        "version_spec": _redacted_text(item.get("version_spec")),
        "reason": redact_display_text(str(item.get("reason", ""))),
        "found_in": _norm_locations(item.get("found_in"), sink, "found_in"),
        "details": redact_display_text("; ".join(details)),
    }


def _legacy_unmapped_rows(config, known_names, sink):
    """Rows for legacy `_skipped_npm_packages` with no structured
    counterpart (matched by package name)."""
    rows = []
    for skipped in _as_list(config.get("_skipped_npm_packages"),
                            "_skipped_npm_packages", sink):
        if not isinstance(skipped, dict):
            continue
        name = skipped.get("name")
        if not name or _hashable(name) in known_names:
            continue
        known_names.add(_hashable(name))
        rows.append({
            "ecosystem": "node",
            "name": redact_display_text(str(name)),
            "version": _redacted_text(skipped.get("version")),
            "version_spec": None,
            "reason": "legacy: skipped npm package (no mapping at "
                      "generation time)",
            "found_in": [{"path": redact_display_text(
                              str(skipped.get("source", ""))),
                          "manifest": "npm"}],
            "details": "",
        })
    return rows


def build_inventory_view(config, project_name=None):
    """Normalize a config (new `_inventory` model or legacy) into a view.

    Malformed structures never raise: junk containers and provenance are
    treated as empty or absent and surface as structured
    `malformed_config` warnings, so a hand-mangled config still renders a
    sane report.
    """
    malformed = []
    inventory = config.get("_inventory")
    if inventory is None:
        inventory = {}
    elif not isinstance(inventory, dict):
        malformed.append(new_warning(
            "malformed_config", "_inventory",
            "expected a JSON object; value ignored"))
        inventory = {}
    summary = inventory.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    manifests = _as_list(inventory.get("manifests"),
                         "_inventory.manifests", malformed)
    warnings = [
        {"category": redact_display_text(str(w.get("category", ""))),
         "path": redact_display_text(str(w.get("path", ""))),
         "message": redact_display_text(str(w.get("message", "")))}
        for w in _as_list(inventory.get("warnings"),
                          "_inventory.warnings", malformed)
        if isinstance(w, dict)
    ]

    # Declaration text comes from scanned manifests, so every field is
    # redacted on its string form exactly like a warning's fields.
    declarations = [
        {"decl": redact_display_text(str(d.get("decl", ""))),
         "file": redact_display_text(str(d.get("file", ""))),
         "kind": redact_display_text(str(d.get("kind", ""))),
         "outcome": redact_display_text(str(d.get("outcome", "")))}
        for d in _as_list(inventory.get("declarations"),
                          "_inventory.declarations", malformed)
        if isinstance(d, dict)
    ]
    by_declaration_outcome = {}
    for declaration in declarations:
        outcome = declaration["outcome"].split(":", 1)[0]
        by_declaration_outcome[outcome] = \
            by_declaration_outcome.get(outcome, 0) + 1

    files_scanned = summary.get("files")
    if files_scanned is None and manifests:
        files_scanned = len(manifests)

    products = []
    containers = []
    for entry in _as_list(config.get("products"), "products", malformed):
        if not isinstance(entry, dict) or entry.get("_section"):
            continue
        if entry.get("_inventory_generated") == "unmapped" and inventory:
            continue
        row = _product_row(entry, malformed)
        (containers if row["container"] else products).append(row)

    structured = [
        item for item in _as_list(inventory.get("unmapped"),
                                  "_inventory.unmapped", malformed)
        if isinstance(item, dict)
    ]
    known_names = {_hashable(item.get("name")) for item in structured}
    unmapped = [_unmapped_row(item, malformed) for item in structured]
    unmapped.extend(_legacy_unmapped_rows(config, known_names, malformed))
    unmapped.sort(key=lambda r: (r["ecosystem"], r["name"],
                                 str(r["version"] or ""),
                                 str(r["version_spec"] or "")))
    warnings.extend(malformed)

    by_ecosystem = {}
    by_provider = {}
    tracked = inferred = 0
    without_provenance = 0
    for row in products + containers:
        by_ecosystem[row["ecosystem"]] = \
            by_ecosystem.get(row["ecosystem"], 0) + 1
        by_provider[row["provider"]] = \
            by_provider.get(row["provider"], 0) + 1
        if row["inferred"]:
            inferred += 1
        else:
            tracked += 1
        if not row["provenance"]:
            without_provenance += 1

    return {
        "meta": {
            # Loaded config content is untrusted: every rendered metadata
            # value passes through the display sanitizer on its string
            # form (URLs plus SSH/SCP/VCS references).
            "scan_date": redact_display_text(str(
                inventory.get("scan_date") or date.today().isoformat())),
            # Additive full-timestamp field; legacy configs without it
            # render exactly as before (renderers skip the absent line).
            "scan_timestamp": _redacted_text(
                inventory.get("scan_timestamp")),
            "generator_version": redact_display_text(str(
                inventory.get("generator_version") or "unknown")),
            "files_scanned": _redacted_text(files_scanned),
            "warning_count": len(warnings),
            "project": redact_display_text(str(
                project_name if project_name is not None
                else (inventory.get("scan_root") or ""))),
        },
        "products": products,
        "containers": containers,
        "unmapped": unmapped,
        "warnings": warnings,
        "declarations": declarations,
        "summary": {
            "by_ecosystem": {k: by_ecosystem[k] for k in sorted(by_ecosystem)},
            "by_provider": {k: by_provider[k] for k in sorted(by_provider)},
            "by_review_state": {
                "tracked": tracked,
                "inferred": inferred,
                "unmapped": len(unmapped),
            },
            "products_without_provenance": without_provenance,
            "by_declaration_outcome": {
                k: by_declaration_outcome[k]
                for k in sorted(by_declaration_outcome)},
        },
    }


# ---------------------------------------------------------------------------
# Location formatting (shared by both renderers)
# ---------------------------------------------------------------------------

def format_found_in(locations):
    """Provenance text: `path:line`, `path (locator)`, or plain `path`.

    Multiple locations join with "; ". Returns "" when nothing was
    recorded (renderers substitute "not recorded"). Locator text derives
    from scanned files, so it passes through URL redaction; Markdown
    escaping is applied per renderer, never here.
    """
    parts = []
    for loc in locations or []:
        if not isinstance(loc, dict):
            continue
        path = str(loc.get("path", ""))
        line = loc.get("line")
        locator = loc.get("locator")
        if line is not None:
            parts.append(f"{path}:{line}")
        elif locator:
            parts.append(f"{path} ({locator})")
        elif path:
            parts.append(path)
    return redact_urls("; ".join(parts))


# ---------------------------------------------------------------------------
# Text normalization (shared by the Markdown and CSV renderers)
# ---------------------------------------------------------------------------

# ECMA-48/ANSI escape sequences: CSI (ESC [ parameters intermediates final),
# OSC/DCS/SOS/PM/APC strings closed by BEL or ST, two-byte ESC sequences,
# and a lone ESC.
_ESCAPE_SEQUENCE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|[@-Z\\-_]"
    r"|[ -/]*[@-~])?")

# C0 controls other than tab/CR/LF, DEL, and C1 controls.
_CONTROL_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f]")


def _sanitize_text(value):
    """Text with terminal escape sequences and control characters removed.

    Strips ECMA-48/ANSI escape sequences (CSI, OSC, other ESC introducers,
    lone ESC) plus the C0/C1 control characters and DEL, keeping tab, CR,
    and LF for the renderers to handle: Markdown collapses them to spaces,
    CSV normalizes CR to LF and neutralizes line-leading formula triggers.
    Printable characters, including non-ASCII, pass through unchanged.
    """
    text = "" if value is None else str(value)
    text = _ESCAPE_SEQUENCE_RE.sub("", text)
    return _CONTROL_CHARS_RE.sub("", text)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _md_cell(value):
    """One single-line Markdown table cell with active markup escaped."""
    return _md_text(value).replace("|", "\\|")


def _md_text(value):
    """Plain Markdown text safe for headings, bullets, and table cells."""
    if value is None:
        return ""
    text = html.escape(_sanitize_text(value), quote=True)
    for separator in ("\r", "\n", "\t", "\u2028", "\u2029"):
        text = text.replace(separator, " ")
    text = re.sub(r"(?i)((?:https?|ftp))://", r"\1&#58;//", text)
    text = re.sub(r"(?i)www\.", "www&#46;", text)
    text = text.replace("@", "&#64;")
    return re.sub(r"([\\`*_~\[\]!])", r"\\\1", text)


def _sorted_rows(rows):
    """Alphabetical by ecosystem, then provider, then label."""
    return sorted(rows, key=lambda r: (r["ecosystem"], r["provider"],
                                       r["label"]))


def render_markdown(view):
    """Render the view as a deterministic Markdown report."""
    meta = view["meta"]
    tracked = _sorted_rows(view["products"])
    containers = _sorted_rows(view["containers"])
    container_unmapped = [u for u in view["unmapped"]
                          if u["ecosystem"] == "container"]
    general_unmapped = [u for u in view["unmapped"]
                        if u["ecosystem"] != "container"]

    lines = []
    title = "# Dependency inventory"
    if meta["project"]:
        title += f": {_md_text(meta['project'])}"
    lines.append(title)
    lines.append("")
    lines.append(f"- Scan date: {_md_text(meta['scan_date'])}")
    if meta.get("scan_timestamp"):
        lines.append(
            f"- Scan timestamp: {_md_text(meta['scan_timestamp'])}")
    lines.append(f"- Generator version: {_md_text(meta['generator_version'])}")
    files_scanned = meta["files_scanned"]
    lines.append("- Files scanned: {}".format(
        files_scanned if files_scanned is not None else "not recorded"))
    lines.append(f"- Warnings: {meta['warning_count']}")
    lines.append("")

    lines.append("## Tracked products")
    lines.append("")
    groups = {}
    for row in tracked:
        groups.setdefault((row["ecosystem"], row["provider"]), []).append(row)
    if not groups:
        lines.append("None.")
        lines.append("")
    for ecosystem, provider in sorted(groups):
        lines.append(f"### {_md_text(ecosystem)} / {_md_text(provider)}")
        lines.append("")
        lines.append("| Product | Version | Source | Found in | Details | Inferred |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for row in groups[(ecosystem, provider)]:
            found = format_found_in(row["provenance"]) or "not recorded"
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                _md_cell(row["label"]),
                _md_cell(row["version"] or ""),
                _md_cell(row["provider"]),
                _md_cell(found),
                _md_cell(row["details"]),
                "yes" if row["inferred"] else "",
            ))
        lines.append("")

    # Declarations: every parsed manifest declaration and what became of
    # it. Absent from legacy configs, whose reports render as before.
    if view["declarations"]:
        lines.append("## Declarations")
        lines.append("")
        lines.append("| Declaration | File | Kind | Outcome |")
        lines.append("| --- | --- | --- | --- |")
        for declaration in view["declarations"]:
            lines.append("| {} | {} | {} | {} |".format(
                _md_cell(declaration["decl"]),
                _md_cell(declaration["file"] or "not recorded"),
                _md_cell(declaration["kind"]),
                _md_cell(declaration["outcome"]),
            ))
        lines.append("")

    if containers or container_unmapped:
        lines.append("## Container images")
        lines.append("")
        if containers:
            lines.append("### Tracked images")
            lines.append("")
            lines.append("| Image | Tag | Source | Found in | Details |")
            lines.append("| --- | --- | --- | --- | --- |")
            for row in containers:
                lines.append("| {} | {} | {} | {} | {} |".format(
                    _md_cell(row["label"]),
                    _md_cell(row["version"] or ""),
                    _md_cell(row["provider"]),
                    _md_cell(format_found_in(row["provenance"])
                             or "not recorded"),
                    _md_cell(row["details"]),
                ))
            lines.append("")
        if container_unmapped:
            lines.append("### Unmapped images")
            lines.append("")
            lines.append("| Image | Version | Reason | Found in | Details |")
            lines.append("| --- | --- | --- | --- | --- |")
            for item in container_unmapped:
                lines.append("| {} | {} | {} | {} | {} |".format(
                    _md_cell(item["name"]),
                    _md_cell(item["version"] or item["version_spec"] or ""),
                    _md_cell(item["reason"]),
                    _md_cell(format_found_in(item["found_in"])
                             or "not recorded"),
                    _md_cell(item["details"]),
                ))
            lines.append("")

    lines.append("## Unmapped and unresolved dependencies")
    lines.append("")
    if not general_unmapped:
        lines.append("None.")
        lines.append("")
    else:
        lines.append("| Ecosystem | Name | Version | Reason | Found in |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in general_unmapped:
            lines.append("| {} | {} | {} | {} | {} |".format(
                _md_cell(item["ecosystem"]),
                _md_cell(item["name"]),
                _md_cell(item["version"] or item["version_spec"] or ""),
                _md_cell(item["reason"]),
                _md_cell(format_found_in(item["found_in"]) or "not recorded"),
            ))
        lines.append("")

    lines.append("## Warnings")
    lines.append("")
    if not view["warnings"]:
        lines.append("None.")
        lines.append("")
    else:
        for warning in view["warnings"]:
            lines.append("- [{}] {}: {}".format(
                _md_text(warning.get("category", "")),
                _md_text(warning.get("path", "")),
                _md_text(warning.get("message", ""))))
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    by_ecosystem = view["summary"]["by_ecosystem"]
    lines.append("| Ecosystem | Products |")
    lines.append("| --- | --- |")
    for ecosystem in sorted(by_ecosystem):
        lines.append(f"| {_md_cell(ecosystem)} | {by_ecosystem[ecosystem]} |")
    lines.append(f"| Total | {sum(by_ecosystem.values())} |")
    lines.append("")
    by_state = view["summary"]["by_review_state"]
    lines.append("| Review state | Count |")
    lines.append("| --- | --- |")
    for state in sorted(k for k, v in by_state.items() if v):
        lines.append(f"| {state} | {by_state[state]} |")
    lines.append(f"| Total | {sum(by_state.values())} |")
    lines.append("")

    lines.append("## Manual review checklist")
    lines.append("")
    if view["unmapped"]:
        lines.append(f"- [ ] Review the {len(view['unmapped'])} unmapped "
                     "dependencies listed above.")
    if meta["warning_count"]:
        lines.append(f"- [ ] Resolve the {meta['warning_count']} scan "
                     "warnings.")
    inferred_count = view["summary"]["by_review_state"]["inferred"]
    if inferred_count:
        lines.append(f"- [ ] Confirm the {inferred_count} inferred tracker "
                     "entries are wanted.")
    without_provenance = view["summary"]["products_without_provenance"]
    if without_provenance:
        lines.append(f"- [ ] Add provenance for {without_provenance} products "
                     "recorded without it.")
    lines.append("- [ ] Spot-check versions derived from properties or "
                 "ranges.")
    lines.append("- [ ] Add manual entries for products no provider can "
                 "monitor.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CSV rendering
# ---------------------------------------------------------------------------

def render_csv(view):
    """Render the view as deterministic CSV (products then unmapped rows)."""
    tracked = _sorted_rows(view["products"])
    containers = _sorted_rows(view["containers"])
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    def write_row(values):
        writer.writerow([_csv_cell(value) for value in values])

    write_row(["kind", "ecosystem", "provider", "name", "version",
               "review_state", "inferred", "found_in", "details"])
    for row in tracked + containers:
        write_row([
            "product",
            row["ecosystem"],
            row["provider"],
            row["label"],
            row["version"] or "",
            "inferred" if row["inferred"] else "tracked",
            "yes" if row["inferred"] else "",
            format_found_in(row["provenance"]) or "not recorded",
            row["details"],
        ])
    for item in view["unmapped"]:
        write_row([
            "unmapped",
            item["ecosystem"],
            "",
            item["name"],
            item["version"] or item["version_spec"] or "",
            "unmapped",
            "",
            format_found_in(item["found_in"]) or "not recorded",
            "; ".join(part for part in (item["reason"], item["details"])
                      if part),
        ])
    for declaration in view["declarations"]:
        # The declaration's own kind is a provider-shaped column here: the
        # first column names the record type the report consumer filters on.
        write_row([
            "declaration",
            "",
            declaration["kind"],
            declaration["decl"],
            "",
            declaration["outcome"].split(":", 1)[0],
            "",
            declaration["file"],
            declaration["outcome"],
        ])
    return buf.getvalue()


def _csv_cell(value):
    """Prevent spreadsheet programs from evaluating inventory text.

    Terminal escapes and control characters are removed and CR is
    normalized to LF; formula trigger characters are then neutralized with
    a leading apostrophe at the start of the cell AND immediately after
    any embedded newline, so a hostile multi-line value cannot place a
    live formula at the start of a new row in a CSV parser that does not
    honour quoting. Embedded newlines stay inside the quoted cell per the
    csv module contract; benign values are returned unchanged.
    """
    text = _sanitize_text(value).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(
        "'" + line if line.startswith(("=", "+", "-", "@", "\t")) else line
        for line in text.split("\n"))


def render_html(view):
    """Render a deterministic, self-contained and escaped HTML inventory."""
    esc = lambda value: html.escape(str(value if value is not None else ""))
    meta_bits = [f"Scan date: {esc(view['meta']['scan_date'])}"]
    if view["meta"].get("scan_timestamp"):
        meta_bits.append(
            f"Scan timestamp: {esc(view['meta']['scan_timestamp'])}")
    meta_bits.append(f"Files scanned: {esc(view['meta']['files_scanned'])}")
    meta_bits.append(f"Warnings: {esc(view['meta']['warning_count'])}")
    meta_line = " | ".join(meta_bits)
    rows = []
    for row in _sorted_rows(view["products"] + view["containers"]):
        state = "inferred" if row["inferred"] else "tracked"
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td><td>{}</td></tr>".format(
                state, esc(row["ecosystem"]), esc(row["label"]),
                esc(row["version"]), esc(row["provider"]),
                esc(format_found_in(row["provenance"]) or "not recorded"),
                esc(row["details"])))
    for item in view["unmapped"]:
        rows.append(
            "<tr class=\"untracked\"><td>untracked</td><td>{}</td>"
            "<td>{}</td><td>{}</td><td>manual</td><td>{}</td><td>{}</td></tr>".format(
                esc(item["ecosystem"]), esc(item["name"]),
                esc(item.get("version") or item.get("version_spec")),
                esc(format_found_in(item["found_in"]) or "not recorded"),
                esc("; ".join(part for part in
                    (item["reason"], item["details"]) if part))))
    declaration_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(d["decl"]), esc(d["file"] or "not recorded"),
            esc(d["kind"]), esc(d["outcome"]))
        for d in view["declarations"])
    declarations_section = (
        "<h2>Declarations</h2>\n<table><thead><tr><th>Declaration</th>"
        "<th>File</th><th>Kind</th><th>Outcome</th></tr></thead>"
        "<tbody>{}</tbody></table>\n".format(declaration_rows)
        if declaration_rows else "")
    warning_items = "".join(
        "<li><strong>{}</strong> {}: {}</li>".format(
            esc(w["category"]), esc(w["path"]), esc(w["message"]))
        for w in view["warnings"])
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dependency inventory: {project}</title>
<style>body{{font:16px system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#18212b}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd5df;padding:.5rem;text-align:left;vertical-align:top}}th{{background:#eef3f7}}.untracked{{background:#fff4d6}}code{{word-break:break-all}}</style></head>
<body><h1>Dependency inventory: {project}</h1>
<p>{meta_line}</p>
<table><thead><tr><th>State</th><th>Ecosystem</th><th>Product</th><th>Version</th><th>Provider</th><th>Found in</th><th>Details</th></tr></thead><tbody>{rows}</tbody></table>
{declarations}<h2>Warnings</h2><ul>{warnings}</ul>
<h2>Manual review checklist</h2><ul><li>Review every untracked row and warning.</li><li>Confirm inferred lifecycle mappings before deployment.</li></ul>
</body></html>
""".format(project=esc(view["meta"]["project"]),
           meta_line=meta_line,
           rows="".join(rows), declarations=declarations_section,
           warnings=warning_items)


# ---------------------------------------------------------------------------
# Config-path helpers
# ---------------------------------------------------------------------------

def project_slug(config_path):
    """`eol_config.<slug>.json` -> `<slug>`; otherwise the filename stem."""
    name = str(config_path).replace("\\", "/").rsplit("/", 1)[-1]
    match = re.fullmatch(r"eol_config\.(.+)\.json", name)
    if match:
        return match.group(1)
    return name.rsplit(".", 1)[0] if "." in name else name
