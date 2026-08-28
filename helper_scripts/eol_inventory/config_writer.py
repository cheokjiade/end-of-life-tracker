"""De-duplication, provenance merging, and EOL config assembly.

Products keep their historical shape and section order. Each mapped
product additionally carries an ignored `_found_in` array (the Lambda
runtime, like for `_comment`, never reads underscore-prefixed keys), and
the config gains an ignored `_inventory` object with schema metadata,
structured warnings, and unmapped records.
"""

import os
from datetime import date

from .mappings import (
    _POM_PROPERTY_MAPPINGS,
    _eol_entry,
    _map_java_dep,
    _map_npm_dep,
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
    return (
        src,
        entry.get("product"),
        entry.get("group"), entry.get("artifact"),
        entry.get("sdk"),   entry.get("major"),
        entry.get("version"),
    )


def _basename(rel_path):
    return rel_path.rsplit("/", 1)[-1] if rel_path else rel_path


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
    return item


def generate_config(scan, project_name):
    """Build an EOL config dict (with `_inventory`) from a scan result."""
    products = []
    seen_keys = {}          # entry key -> product entry (for _found_in merge)
    unmapped = []
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

    def comment_for(record, raw):
        return f"From {_basename(record['found_in'][0]['path'])} ({raw})"

    records = scan["records"]

    # --- POM property-driven platform versions --------------------------------
    property_records = [r for r in records if r["kind"] == "property"]
    if property_records:
        products.append({"_section": "=== Platforms (from POM properties) ==="})
        for record in property_records:
            mapper = _POM_PROPERTY_MAPPINGS.get(record["name"])
            if mapper is None or record["version"] is None:
                continue
            v = record["version"]
            entry = mapper(v)
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
                unmapped.append(_unmapped_item(
                    record, "unresolved version expression"))
                continue
            entry = _map_java_dep(record["group"], record["artifact"],
                                  record["version"])
            if entry is None:
                unmapped.append(_unmapped_item(
                    record, _java_skip_reason(record)))
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
                # react-dom is tracked via 'react'; other unmapped packages
                # land in the inventory (and the legacy skipped list).
                if record["name"] not in {"react-dom"}:
                    reason = ("no version declared" if not record["version"]
                              else "no endoflife.date mapping for this package")
                    unmapped.append(_unmapped_item(record, reason))
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

    # --- Build config --------------------------------------------------------
    real_products = [p for p in products if not p.get("_section")]
    warnings = sort_warnings(scan["warnings"])
    unmapped.sort(key=lambda item: (
        item["ecosystem"], item["name"],
        item.get("version", ""), item.get("version_spec", ""),
        item["found_in"][0]["path"] if item["found_in"] else "",
    ))

    config = {
        "_comment": [
            f"EOL config for the {project_name} project.",
            f"Auto-generated by generate_config.py on {date.today()}.",
            f"Files scanned: {len(scan['files'])}.",
            f"Tracker entries: {len(real_products)}.",
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

    if skipped_npm:
        config["_skipped_npm_packages"] = skipped_npm

    config["_inventory"] = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "scan_root": scan["root_name"],
        "manifests": list(scan["files"]),
        "summary": {
            "files": len(scan["files"]),
            "records": len(records),
            "products": len(real_products),
            "unmapped": len(unmapped),
            "warnings": len(warnings),
        },
        "warnings": warnings,
        "unmapped": unmapped,
    }

    return config


def _java_skip_reason(record):
    """Why a Java coordinate produced no tracker entry (ASCII, stable)."""
    version = record["version"] or record["version_spec"] or ""
    group = record["group"] or ""
    artifact = record["artifact"] or ""
    if version.endswith("-SNAPSHOT"):
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
