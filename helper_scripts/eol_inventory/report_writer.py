"""Human-readable inventory rendering: Markdown and CSV.

`build_inventory_view` flattens a generated config into a plain view
dict: product rows (label, version, provider, inferred state,
provenance, ecosystem), container rows kept separately, unmapped
records, warnings, and summary counts. `render_markdown` and
`render_csv` turn that view into deterministic reports. Legacy configs
without `_inventory` render with "not recorded" placeholders instead of
failing, and legacy `_skipped_npm_packages` are surfaced as unmapped
rows. Standard-library only; no network; output is ASCII for ASCII
inputs.
"""

import csv
import io
import re
from datetime import date

from .models import sort_locations


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
    """One provenance location with the keys the sort/format helpers need."""
    norm = {"path": loc.get("path", ""), "manifest": loc.get("manifest", "")}
    if loc.get("line") is not None:
        norm["line"] = loc["line"]
    if loc.get("locator"):
        norm["locator"] = loc["locator"]
    return norm


def _norm_locations(locations):
    return sort_locations([_norm_location(loc) for loc in locations or []])


def _product_row(entry):
    """One normalized tracked/container row from a product entry."""
    provenance = _norm_locations(entry.get("_found_in"))
    ecosystem = _infer_ecosystem(entry, provenance)
    return {
        "label": _product_label(entry),
        "version": entry.get("version"),
        "provider": entry.get("source") or "endoflife_date",
        "inferred": "auto-derived" in _comment_text(entry),
        "ecosystem": ecosystem,
        "container": ecosystem == "container",
        "provenance": provenance,
    }


def _unmapped_row(item):
    """One normalized unmapped row from an `_inventory.unmapped` item."""
    return {
        "ecosystem": item.get("ecosystem", "other"),
        "name": str(item.get("name", "")),
        "version": item.get("version"),
        "version_spec": item.get("version_spec"),
        "reason": str(item.get("reason", "")),
        "found_in": _norm_locations(item.get("found_in")),
    }


def _legacy_unmapped_rows(config, known_names):
    """Rows for legacy `_skipped_npm_packages` with no structured
    counterpart (matched by package name)."""
    rows = []
    for skipped in config.get("_skipped_npm_packages") or []:
        if not isinstance(skipped, dict):
            continue
        name = skipped.get("name")
        if not name or name in known_names:
            continue
        known_names.add(name)
        rows.append({
            "ecosystem": "node",
            "name": str(name),
            "version": skipped.get("version"),
            "version_spec": None,
            "reason": "legacy: skipped npm package (no mapping at "
                      "generation time)",
            "found_in": [{"path": str(skipped.get("source", "")),
                          "manifest": "npm"}],
        })
    return rows


def build_inventory_view(config, project_name=None):
    """Normalize a config (new `_inventory` model or legacy) into a view."""
    inventory = config.get("_inventory")
    if not isinstance(inventory, dict):
        inventory = {}
    summary = inventory.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    manifests = inventory.get("manifests") or []
    warnings = [
        {"category": w.get("category", ""), "path": w.get("path", ""),
         "message": w.get("message", "")}
        for w in (inventory.get("warnings") or []) if isinstance(w, dict)
    ]

    files_scanned = summary.get("files")
    if files_scanned is None and manifests:
        files_scanned = len(manifests)

    products = []
    containers = []
    for entry in config.get("products") or []:
        if not isinstance(entry, dict) or entry.get("_section"):
            continue
        row = _product_row(entry)
        (containers if row["container"] else products).append(row)

    structured = [item for item in (inventory.get("unmapped") or [])
                  if isinstance(item, dict)]
    known_names = {item.get("name") for item in structured}
    unmapped = [_unmapped_row(item) for item in structured]
    unmapped.extend(_legacy_unmapped_rows(config, known_names))
    unmapped.sort(key=lambda r: (r["ecosystem"], r["name"],
                                 str(r["version"] or ""),
                                 str(r["version_spec"] or "")))

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
            "scan_date": date.today().isoformat(),
            "generator_version": inventory.get("generator_version")
            or "unknown",
            "files_scanned": files_scanned,
            "warning_count": len(warnings),
            "project": (project_name if project_name is not None
                        else (inventory.get("scan_root") or "")),
        },
        "products": products,
        "containers": containers,
        "unmapped": unmapped,
        "warnings": warnings,
        "summary": {
            "by_ecosystem": {k: by_ecosystem[k] for k in sorted(by_ecosystem)},
            "by_provider": {k: by_provider[k] for k in sorted(by_provider)},
            "by_review_state": {
                "tracked": tracked,
                "inferred": inferred,
                "unmapped": len(unmapped),
            },
            "products_without_provenance": without_provenance,
        },
    }


# ---------------------------------------------------------------------------
# Location formatting (shared by both renderers)
# ---------------------------------------------------------------------------

def format_found_in(locations):
    """Provenance text: `path:line`, `path (locator)`, or plain `path`.

    Multiple locations join with "; ". Returns "" when nothing was
    recorded (renderers substitute "not recorded"). Markdown escaping
    is applied per renderer, never here.
    """
    parts = []
    for loc in locations or []:
        path = str(loc.get("path", ""))
        line = loc.get("line")
        locator = loc.get("locator")
        if line is not None:
            parts.append(f"{path}:{line}")
        elif locator:
            parts.append(f"{path} ({locator})")
        elif path:
            parts.append(path)
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _md_cell(value):
    """One Markdown table cell with pipes escaped."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|")


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
        title += f": {meta['project']}"
    lines.append(title)
    lines.append("")
    lines.append(f"- Scan date: {meta['scan_date']}")
    lines.append(f"- Generator version: {meta['generator_version']}")
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
        lines.append(f"### {ecosystem} / {provider}")
        lines.append("")
        lines.append("| Product | Version | Source | Found in | Inferred |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in groups[(ecosystem, provider)]:
            found = format_found_in(row["provenance"]) or "not recorded"
            lines.append("| {} | {} | {} | {} | {} |".format(
                _md_cell(row["label"]),
                _md_cell(row["version"] or ""),
                _md_cell(row["provider"]),
                _md_cell(found),
                "yes" if row["inferred"] else "",
            ))
        lines.append("")

    if containers or container_unmapped:
        lines.append("## Container images")
        lines.append("")
        if containers:
            lines.append("### Tracked images")
            lines.append("")
            lines.append("| Image | Tag | Source | Found in |")
            lines.append("| --- | --- | --- | --- |")
            for row in containers:
                lines.append("| {} | {} | {} | {} |".format(
                    _md_cell(row["label"]),
                    _md_cell(row["version"] or ""),
                    _md_cell(row["provider"]),
                    _md_cell(format_found_in(row["provenance"])
                             or "not recorded"),
                ))
            lines.append("")
        if container_unmapped:
            lines.append("### Unmapped images")
            lines.append("")
            lines.append("| Image | Version | Reason | Found in |")
            lines.append("| --- | --- | --- | --- |")
            for item in container_unmapped:
                lines.append("| {} | {} | {} | {} |".format(
                    _md_cell(item["name"]),
                    _md_cell(item["version"] or item["version_spec"] or ""),
                    _md_cell(item["reason"]),
                    _md_cell(format_found_in(item["found_in"])
                             or "not recorded"),
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
            lines.append("- [{category}] {path}: {message}".format(**warning))
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
    writer.writerow(["kind", "ecosystem", "provider", "name", "version",
                     "review_state", "inferred", "found_in"])
    for row in tracked + containers:
        writer.writerow([
            "product",
            row["ecosystem"],
            row["provider"],
            row["label"],
            row["version"] or "",
            "inferred" if row["inferred"] else "tracked",
            "yes" if row["inferred"] else "",
            format_found_in(row["provenance"]) or "not recorded",
        ])
    for item in view["unmapped"]:
        writer.writerow([
            "unmapped",
            item["ecosystem"],
            "",
            item["name"],
            item["version"] or item["version_spec"] or "",
            "unmapped",
            "",
            format_found_in(item["found_in"]) or "not recorded",
        ])
    return buf.getvalue()


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
