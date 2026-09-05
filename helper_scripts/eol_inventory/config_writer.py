"""De-duplication, provenance merging, and EOL config assembly.

Products keep their historical shape and section order. Each mapped
product additionally carries an ignored `_found_in` array (the Lambda
runtime, like for `_comment`, never reads underscore-prefixed keys), and
the config gains an ignored `_inventory` object with schema metadata,
structured warnings, and unmapped records.

Mapping policy per ecosystem (see the plan:
docs/plans/2026-08-28-project-dependency-inventory.md):

    java       lifecycle mappings, Maven Central fallback (unchanged)
    node       lifecycle mappings, then npm_registry for remaining exact
               direct packages; unresolved specs stay in the inventory
    python     pypi_registry for exact pins; runtime evidence maps to the
               endoflife.date python product; unpinned/URL/local/path
               specs stay in the inventory
    go         go_proxy for exact direct requires; the go/toolchain
               runtime maps to golang; indirect requires are excluded
               from products and counted in the summary
    dotnet     nuget_registry for exact direct packages; TargetFramework
               and SDK evidence maps to the endoflife.date dotnet product
               (never for .NET Framework/netstandard, which have no cycle)
    container  registry-normalized image mappings; unknown images and
               cycle-less tags stay in the inventory

The generator never creates an entry naming an unregistered provider:
every source used here is registered in eoltracker/parsers/.
"""

import os
from datetime import date

from .mappings import (
    _POM_PROPERTY_MAPPINGS,
    _dotnet_runtime_cycle,
    _eol_entry,
    _go_proxy_entry,
    _image_skip_reason,
    _major,
    _major_minor,
    _map_image_dep,
    _map_java_dep,
    _map_npm_dep,
    _npm_registry_entry,
    _nuget_entry,
    _pypi_entry,
)
from .models import (
    GENERATOR_VERSION,
    SCHEMA_VERSION,
    sort_locations,
    sort_warnings,
)

def _entry_key(entry):
    """Stable de-dup key across entry shapes."""
    src = entry.get("source", "endoflife_date")
    package = entry.get("package")
    if src == "nuget_registry" and isinstance(package, str):
        package = package.lower()
    return (
        src,
        entry.get("product"),
        package,
        entry.get("module"),
        entry.get("group"), entry.get("artifact"),
        entry.get("sdk"),   entry.get("major"),
        entry.get("version"),
    )


def _basename(rel_path):
    return rel_path.rsplit("/", 1)[-1] if rel_path else rel_path


def _spec_reason(record):
    """Inventory reason for a record without an exact version."""
    if record["version_spec"]:
        return f"no exact version ({record['version_spec']})"
    return "no version declared"


def _unmapped_item(record, reason):
    item = {
        "ecosystem": record["ecosystem"],
        "name": record["name"],
        "reason": reason,
    }
    if record["version"] is not None:
        item["version"] = record["version"]
    if record["version_spec"]:
        item["version_spec"] = record["version_spec"]
    item["found_in"] = sort_locations(record["found_in"])
    for key in ("image_reference", "image_identity", "registry",
                "repository", "tag", "digest", "scope", "direct"):
        if record.get(key) is not None:
            item[key] = record[key]
    return item


def _python_entry(record):
    """(entry, skip_reason) for a python record; both None never happens."""
    if record["version"]:
        if record["kind"] == "runtime":
            cycle = _major_minor(record["version"])
            return _eol_entry("python", cycle, f"Python {cycle}"), None
        return _pypi_entry(record["name"], record["version"]), None
    return None, _spec_reason(record)


def _go_entry(record):
    """(entry, skip_reason) for a go record; skip_reason None = project."""
    if record["kind"] == "module":
        return None, None  # the project itself, not a dependency
    if record["version"]:
        if record["kind"] == "runtime":
            cycle = _major_minor(record["version"])
            return _eol_entry("golang", cycle, f"Go {cycle}"), None
        return _go_proxy_entry(record["name"], record["version"]), None
    return None, _spec_reason(record)


def _dotnet_entry(record):
    """(entry, skip_reason) for a dotnet record."""
    if record["kind"] == "runtime":
        cycle = _dotnet_runtime_cycle(record["version"])
        if cycle:
            return _eol_entry("dotnet", cycle, f".NET {cycle}"), None
        # .NET Framework / netstandard have no endoflife.date cycle.
        reason = ("no endoflife.date cycle for this target framework"
                  if record["version"] else _spec_reason(record))
        return None, reason
    if record["version"]:
        return _nuget_entry(record["name"], record["version"]), None
    return None, _spec_reason(record)


def _container_entry(record):
    """(entry, skip_reason) for a container image record."""
    identity = record.get("image_identity", record["name"])
    entry = _map_image_dep(identity, record["version"])
    if entry is not None:
        return entry, None
    return None, _image_skip_reason(identity, record["version"])


def generate_config(scan, project_name, include_transitive=False):
    """Build an EOL config dict (with `_inventory`) from a scan result."""
    products = []
    seen_keys = {}          # entry key -> product entry (for _found_in merge)
    unmapped_by_key = {}    # identity key -> unmapped item (merged provenance)
    skipped_npm = []        # legacy mirror of node unmapped items

    def add(entry, record, comment=None):
        """Add a mapped entry; merge provenance on duplicate keys."""
        if entry is None:
            return False
        if comment:
            entry.setdefault("_comment", comment)
        if record["found_in"]:
            entry["_found_in"] = sort_locations(record["found_in"])
        key = _entry_key(entry)
        existing = seen_keys.get(key)
        if existing is not None:
            merged = list(existing.get("_found_in") or [])
            for loc in entry["_found_in"]:
                if loc not in merged:
                    merged.append(loc)
            if merged:
                existing["_found_in"] = sort_locations(merged)
            return False
        seen_keys[key] = entry
        products.append(entry)
        return True

    def add_unmapped(record, reason):
        """Record one unmapped item; identical items merge provenance."""
        item = _unmapped_item(record, reason)
        name = item["name"]
        if item["ecosystem"] == "dotnet":
            name = name.lower()
        key = (item["ecosystem"], name, item.get("version"),
               item.get("version_spec"), item["reason"])
        existing = unmapped_by_key.get(key)
        if existing is None:
            unmapped_by_key[key] = item
            return
        merged = list(existing["found_in"])
        for loc in item["found_in"]:
            if loc not in merged:
                merged.append(loc)
        existing["found_in"] = sort_locations(merged)

    def comment_for(record, raw):
        return f"From {_basename(record['found_in'][0]['path'])} ({raw})"

    records = scan["records"]

    # --- POM property-driven platform versions --------------------------------
    property_records = [r for r in records if r["kind"] == "property"]
    if property_records:
        added_section = False
        for record in property_records:
            mapper = _POM_PROPERTY_MAPPINGS.get(record["name"])
            if mapper is None:
                continue
            if record["version"] is None:
                add_unmapped(record, _spec_reason(record))
                continue
            v = record["version"]
            if v.lower().endswith("-snapshot"):
                add_unmapped(
                    record,
                    "SNAPSHOT build resolves on no public registry")
                continue
            entry = mapper(v)
            if not added_section:
                products.append({
                    "_section": "=== Platforms (from POM properties) ==="})
                added_section = True
            add(entry, record, comment=(
                f"From {_basename(record['found_in'][0]['path'])} "
                f"(<{record['name']}>{v}</{record['name']}>)"))

    # --- Java/Maven dependencies ---------------------------------------------
    java_records = [r for r in records
                    if r["ecosystem"] == "java" and r["kind"] != "property"]
    if java_records:
        added_section = False
        for record in java_records:
            if record["version"] is None:
                add_unmapped(record, "unresolved version expression")
                continue
            entry = _map_java_dep(record["group"], record["artifact"],
                                  record["version"])
            if entry is None:
                add_unmapped(record, _java_skip_reason(record))
                continue
            if not added_section:
                products.append({"_section": "=== Java dependencies ==="})
                added_section = True
            raw = f"{record['group']}:{record['artifact']}:{record['version']}"
            add(entry, record, comment=comment_for(record, raw))

    # --- npm dependencies ----------------------------------------------------
    node_records = [r for r in records if r["ecosystem"] == "node"]
    if node_records:
        added_section = False
        for record in node_records:
            entry = None
            if record["version"]:
                entry = _map_npm_dep(record["name"], record["version"])
                if entry is None:
                    # Remaining exact direct packages get release-recency
                    # tracking from the npm registry.
                    entry = _npm_registry_entry(record["name"],
                                                record["version"])
            if entry is None:
                # Only versionless non-exact records remain unmapped:
                # versioned packages map to a lifecycle product or to
                # npm_registry above. They also join the legacy skipped
                # list for older report consumers.
                add_unmapped(record, _spec_reason(record))
                if record["kind"] == "dependency":
                    skipped_npm.append({
                        "name": record["name"],
                        "version": record["version"],
                        "source": _basename(record["found_in"][0]["path"]),
                    })
                continue
            if not added_section:
                products.append({"_section": "=== npm dependencies ==="})
                added_section = True
            raw = f"{record['name']}@{record['version']}"
            add(entry, record, comment=comment_for(record, raw))

    # --- Python dependencies -------------------------------------------------
    # Pipfile.lock records carry direct=False (the lock is a resolved
    # graph, mirroring go's indirect requires): excluded from products
    # and counted in the summary like all lock-graph records.
    python_records = [r for r in records if r["ecosystem"] == "python"]
    if python_records:
        added_section = False
        for record in python_records:
            if (record["kind"] == "dependency" and not record["direct"]
                    and not include_transitive):
                continue
            entry, reason = _python_entry(record)
            if entry is None:
                add_unmapped(record, reason)
                continue
            if not added_section:
                products.append({"_section": "=== Python dependencies ==="})
                added_section = True
            raw = f"{record['name']}=={record['version']}"
            add(entry, record, comment=comment_for(record, raw))

    # --- Go dependencies ------------------------------------------------------
    # Indirect requires (direct=False, from `// indirect` lines and module
    # replacements) are excluded from products and counted in the summary.
    go_records = [r for r in records if r["ecosystem"] == "go"]
    if go_records:
        added_section = False
        for record in go_records:
            if (record["kind"] == "dependency" and not record["direct"]
                    and not include_transitive):
                continue
            entry, reason = _go_entry(record)
            if entry is None:
                if reason is not None:
                    add_unmapped(record, reason)
                continue
            if not added_section:
                products.append({"_section": "=== Go dependencies ==="})
                added_section = True
            raw = f"{record['name']} v{record['version']}"
            add(entry, record, comment=comment_for(record, raw))

    # --- .NET dependencies ----------------------------------------------------
    dotnet_records = [r for r in records if r["ecosystem"] == "dotnet"]
    if dotnet_records:
        added_section = False
        for record in dotnet_records:
            entry, reason = _dotnet_entry(record)
            if entry is None:
                add_unmapped(record, reason)
                continue
            if not added_section:
                products.append({"_section": "=== .NET dependencies ==="})
                added_section = True
            raw = f"{record['name']} {record['version']}"
            add(entry, record, comment=comment_for(record, raw))

    # --- Container images -----------------------------------------------------
    container_records = [r for r in records if r["ecosystem"] == "container"]
    if container_records:
        added_section = False
        for record in container_records:
            entry, reason = _container_entry(record)
            if entry is None:
                add_unmapped(record, reason)
                continue
            if not added_section:
                products.append({"_section": "=== Container images ==="})
                added_section = True
            raw = record["found_in"][0].get(
                "locator", f"{record['name']}:{record['version']}")
            for key in ("image_reference", "image_identity", "registry",
                        "repository", "tag", "digest"):
                if record.get(key) is not None:
                    entry[key] = record[key]
            add(entry, record, comment=comment_for(record, raw))

    # --- Infer transitive platforms from detected ones -----------------------
    # Spring Boot's release train pairs each Boot minor with a Spring Security
    # minor: Boot 3.x.y -> Security 6.x.y, Boot 2.x.y -> Security 5.x.y.
    # Add the inferred entry only when not already present.
    spring_boot = next(
        (p for p in products if p.get("product") == "spring-boot"),
        None,
    )
    if spring_boot:
        sb_v = spring_boot["version"]  # "3.5"
        sb_parts = sb_v.split(".")
        if len(sb_parts) == 2 and sb_parts[0] in ("2", "3") and not any(
            p.get("product") == "spring-security" for p in products
        ):
            ss_major = "6" if sb_parts[0] == "3" else "5"
            ss_v = f"{ss_major}.{sb_parts[1]}"
            products.append({"_section": "=== Inferred from Spring Boot release train ==="})
            inferred_entry = _eol_entry("spring-security", ss_v, f"Spring Security {ss_v}")
            inferred_entry["_comment"] = (
                f"Auto-derived from Spring Boot {sb_v} (release train pairing). "
                f"Spring Security version is not explicitly pinned in the POMs."
            )
            key = _entry_key(inferred_entry)
            if key not in seen_keys:
                seen_keys[key] = inferred_entry
                products.append(inferred_entry)

    # Unmapped evidence is also represented as manual tracker rows. The manual
    # provider reports these as `untracked`, so the normal EOL report never
    # silently drops inventory that lacks a live data source.
    unmapped = sorted(
        unmapped_by_key.values(),
        key=lambda item: (
            item["ecosystem"], item["name"],
            item.get("version", ""), item.get("version_spec", ""),
            item["found_in"][0]["path"] if item["found_in"] else "",
        ),
    )
    if unmapped:
        products.append({"_section": "=== Needs Manual Review ==="})
        for item in unmapped:
            version = item.get("version") or item.get("version_spec")
            entry = {
                "source": "manual",
                "label": item["name"],
                "version": version,
                "note": item["reason"],
                "_found_in": item["found_in"],
                "_comment": f"Untracked {item['ecosystem']} inventory item",
                "_inventory_generated": "unmapped",
            }
            for key in ("image_reference", "image_identity", "registry",
                        "repository", "tag", "digest", "scope", "direct"):
                if item.get(key) is not None:
                    entry[key] = item[key]
            products.append(entry)

    # A section header whose section lost every product to cross-ecosystem
    # provenance merges would render as an empty divider: drop those.
    products = _drop_empty_sections(products)

    # --- Build config --------------------------------------------------------
    real_products = [p for p in products if not p.get("_section")]
    tracked_products = [p for p in real_products
                        if p.get("_inventory_generated") != "unmapped"]
    warnings = sort_warnings(scan["warnings"])
    config = {
        "_comment": [
            f"EOL config for the {project_name} project.",
            f"Auto-generated by generate_config.py on {date.today()}.",
            f"Files scanned: {len(scan['files'])}.",
            f"Tracked entries: {len(tracked_products)}; "
            f"manual-review entries: {len(real_products) - len(tracked_products)}.",
            "",
            "REVIEW THIS FILE BEFORE DEPLOYING — auto-mapping is best-effort.",
            "Common things to check:",
            "  - Java distribution (amazon-corretto vs eclipse-temurin vs oracle-jdk)",
            "  - 'Latest patch not found' warnings indicate version pins not on Maven Central",
            "  - Unmapped packages (see _inventory.unmapped) need manual entries",
            "",
            "Run with:  python lambda_function.py " + f"eol_config.{project_name}.json",
        ],
        "alert_thresholds_days": [30, 60, 90],
        "notify_when": "always",
        "notifications": [
            {"type": "console"},
            {"type": "html_file", "path": f"eol_report_{project_name}.html"},
            {"type": "sns", "_comment": "topic_arn supplied via EventBridge event input (set by Terraform)"},
        ],
        "products": products,
    }

    if scan.get("maven_repositories"):
        # Config-level, not per-entry: handler.py stamps this list onto
        # maven_central entries lacking an explicit 'repository' at load
        # time (single source of truth, capped there).
        config["maven_repositories"] = list(scan["maven_repositories"])
    if skipped_npm:
        config["_skipped_npm_packages"] = skipped_npm

    config["_inventory"] = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "scan_date": date.today().isoformat(),
        "scan_root": scan["root_name"],
        "manifests": list(scan["files"]),
        "include_transitive": bool(include_transitive),
        "summary": {
            "files": len(scan["files"]),
            "records": len(records),
            "products": len(tracked_products),
            "unmapped": len(unmapped),
            "warnings": len(warnings),
            "indirect": sum(1 for r in records if r["direct"] is False),
        },
        "warnings": warnings,
        "unmapped": unmapped,
    }

    return config


def _drop_empty_sections(products):
    """Remove _section dividers with no products after them (deterministic)."""
    kept = []
    for idx, item in enumerate(products):
        if item.get("_section"):
            nxt = products[idx + 1] if idx + 1 < len(products) else None
            if nxt is None or nxt.get("_section"):
                continue
        kept.append(item)
    return kept


def _java_skip_reason(record):
    """Why a Java coordinate produced no tracker entry (ASCII, stable)."""
    version = record["version"] or record["version_spec"] or ""
    group = record["group"] or ""
    artifact = record["artifact"] or ""
    if version.lower().endswith("-snapshot"):
        return "SNAPSHOT build resolves on no public registry"
    if group.startswith("internal."):
        return "internal coordinate prefix resolves on no public registry"
    if artifact in ("junit", "junit-vintage-engine", "junit-jupiter",
                    "mockito-inline", "awaitility", "spring-boot-starter-test",
                    "spring-security-test", "gson"):
        return "deliberately untracked (no useful lifecycle data)"
    if group.startswith("org.webjars"):
        return "webjar without useful upstream lifecycle data"
    return "no lifecycle mapping for this coordinate"
