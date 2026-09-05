"""Generate an EOL tracker config and inventory from a project's dependencies.

Scans a folder for Java, Node, Python, Go, and .NET manifests plus
Dockerfile and GitLab CI image declarations; emits an
eol_config.<project>.json file suitable for use with lambda_function.py.
Every mapped product carries structured provenance (`_found_in`), and the
config carries an ignored `_inventory` object with warnings and unmapped
items. Standard-library only; project files are never executed.

Supported formats:
    pom.xml, *.gradle.kts, build.gradle   — Maven / Gradle (multi-module)
    package.json                          — Node (lock files resolve ranges)
    requirements*.txt, pyproject.toml,
    Pipfile, Pipfile.lock, .python-version, runtime.txt — Python
    go.mod                                — Go
    *.csproj, *.fsproj, *.vbproj,
    Directory.Packages.props, global.json — .NET
    Dockerfile, Dockerfile.*, *.Dockerfile — container images
    .gitlab-ci.yml/.yaml, .gitlab/*.yml   — GitLab CI images

Usage:
    python helper_scripts/generate_config.py <folder> [--name PROJECT]
           [--output FILE] [--exclude PATTERN] [--update | --replace]
           [--include-transitive] [--strict]

Examples:
    python helper_scripts/generate_config.py "project-b" --name b
    python helper_scripts/generate_config.py ssg-frontend --name frontend --update
"""

import argparse
import os
import re
import shlex
import sys
import tempfile

from eol_inventory import generate_config, scan_folder
from eol_inventory.config_io import (
    ConfigLoadError,
    ConfigTooLargeError,
    dump_bounded_config,
    load_bounded_config,
)
from eol_inventory.parsers.docker import split_image_reference
from eol_inventory.redact import redact_image_reference, redact_urls


def _live_smoke_command(output, executable=None, platform=None):
    """Return a copy-pasteable tracker command for the current shell family."""
    executable = executable or sys.executable
    platform = platform or os.name
    if platform == "nt":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        return f"& {quote(executable)} lambda_function.py {quote(output)}"
    return shlex.join((str(executable), "lambda_function.py", str(output)))


def _merge_identity(entry):
    """Identity excluding version, used for curation-preserving updates.

    NuGet package IDs match case-insensitively (the registry grammar),
    so the identity key case-folds them while display casing stays
    untouched."""
    source = entry.get("source") or "endoflife_date"
    keys = ("product", "package", "module", "group", "artifact", "sdk",
            "major", "label" if source == "manual" else "_unused")
    values = [entry.get(key) for key in keys]
    if source == "nuget_registry":
        # NuGet package IDs match case-insensitively; the scanner emits
        # them in `package` (the provider fallback uses `product`).
        for index in (1, 0):
            if isinstance(values[index], str):
                values[index] = values[index].lower()
    identity = (source,) + tuple(values)
    if source == "manual":
        identity += (entry.get("note"),)
    return identity


def _dockerfile_from_key(locator):
    """Dockerfile FROM locator keyed on the image repository.

    The repository identifies the declaration site (the tag is the
    version, not the site), so distinct images in one file get distinct
    provenance keys while a re-pinned tag still matches the row generated
    for the same image. Legacy configs may carry a bare "FROM" locator;
    it keys as-is and the update path matches it against every FROM in
    the same file. Legacy pre-redaction locators (registry-authority
    credentials) are redacted before splitting, so they key onto the
    fresh redacted row instead of onto the username.
    """
    alias = re.search(r"\s+AS\s+(\S+)\s*$", locator, re.IGNORECASE)
    if alias:
        locator = locator[:alias.start()].rstrip()
    repo, _, _ = split_image_reference(
        redact_image_reference(locator[5:].strip()))
    return f"FROM {repo}" if repo else "FROM"


def _provenance_keys(entry):
    """Stable declaration-site identities for scanner-generated entries."""
    keys = set()
    for location in entry.get("_found_in") or []:
        if not isinstance(location, dict) or not location.get("path"):
            continue
        manifest = location.get("manifest")
        locator = location.get("locator")
        if manifest == "dockerfile" and isinstance(locator, str) \
                and locator.upper().startswith("FROM "):
            locator = _dockerfile_from_key(locator)
        keys.add((location.get("path"), manifest, locator))
    return keys


def _unmapped_item_key(name, version, found_in):
    """Identity of one unmapped inventory item or its generated row.

    Name, version (the row's `version` is the item's `version` or
    `version_spec`), and the full stable provenance keys, so two
    same-name items at distinct sites, or at distinct locators in one
    file, never collide.
    """
    return (
        "" if name is None else str(name),
        None if version is None else str(version),
        frozenset(_provenance_keys({"_found_in": found_in})),
    )


def _generated_unmapped_defaults(existing, entry):
    """Generator-owned note/comment values for one old unmapped row.

    The inventory item must be the row's own sibling (same provenance
    keys, name, and version), so items that merely share a file never
    lend their reason to another row's note comparison.
    """
    old_keys = _provenance_keys(entry)
    if not old_keys:
        return {}
    inventory = existing.get("_inventory")
    if not isinstance(inventory, dict):
        return {}
    label = entry.get("label")
    version = entry.get("version")
    for item in inventory.get("unmapped") or []:
        if not isinstance(item, dict):
            continue
        item_keys = _provenance_keys({"_found_in": item.get("found_in")})
        if old_keys.isdisjoint(item_keys):
            continue
        if item.get("name") != label:
            continue
        item_version = item.get("version") or item.get("version_spec")
        if item_version is not None and item_version != version:
            continue
        return {
            "note": item.get("reason"),
            "_comment": f"Untracked {item.get('ecosystem')} inventory item",
        }
    return {}


def _merge_existing_config(existing, generated):
    """Merge scan evidence into an existing config without deleting curation."""
    fresh = [p for p in generated.get("products", [])
             if isinstance(p, dict) and not p.get("_section")]
    fresh_by_identity = {}
    for index, product in enumerate(fresh):
        fresh_by_identity.setdefault(_merge_identity(product), []).append(index)
    fresh_by_provenance = {}
    fresh_dockerfile_sites = {}
    for index, product in enumerate(fresh):
        for key in _provenance_keys(product):
            fresh_by_provenance.setdefault(key, set()).add(index)
            if key[1] == "dockerfile":
                fresh_dockerfile_sites.setdefault(key[0], set()).add(index)
    old_identity_counts = {}
    for old in existing.get("products", []):
        if isinstance(old, dict) and not old.get("_section"):
            identity = _merge_identity(old)
            old_identity_counts[identity] = \
                old_identity_counts.get(identity, 0) + 1
    used = set()
    products = []
    stats = {"added": 0, "changed": 0, "unchanged": 0,
             "retained_not_observed": 0}
    retained_unmapped_keys = set()
    for old in existing.get("products", []):
        if not isinstance(old, dict) or old.get("_section"):
            products.append(old)
            continue
        identity = _merge_identity(old)
        candidates = [index for index in fresh_by_identity.get(identity, [])
                      if index not in used]
        exact = [index for index in candidates
                 if fresh[index].get("version") == old.get("version")]
        remapped = False
        selected = None
        # When several OLD rows share one merge identity (one dependency
        # declared at several sites), a fresh row is only assignable to
        # the old row at its own declaration site: the exact-version and
        # sole-candidate fallbacks below would otherwise hand the one
        # surviving site's row to whichever old row is processed first
        # (moving that site's curation and leaving a stale duplicate).
        # Such rows may only match by unique provenance; anything else
        # retains conservatively. The gate mirrors the provenance
        # branches below (`_comment` marks scanner-generated rows): a
        # row that cannot be provenance-matched keeps its fallbacks,
        # otherwise it could never match and would be retained AND
        # re-added on every update.
        site_bound = old_identity_counts.get(identity, 0) > 1 \
            and bool(old.get("_comment")) and bool(_provenance_keys(old))
        if len(candidates) > 1 and old.get("_comment") \
                and _provenance_keys(old):
            # Several fresh rows share the merge identity: a unique
            # stable-provenance match outranks the exact-version
            # fallback, which can cross curation between declaration
            # sites when both versions changed.
            provenance_candidates = set()
            for key in _provenance_keys(old):
                provenance_candidates.update(
                    fresh_by_provenance.get(key, ()))
                if key[1] == "dockerfile" and key[2] == "FROM":
                    provenance_candidates.update(
                        fresh_dockerfile_sites.get(key[0], ()))
            provenance_candidates.difference_update(used)
            if old.get("_inventory_generated") != "unmapped":
                provenance_candidates &= set(candidates)
            if len(provenance_candidates) == 1:
                selected = next(iter(provenance_candidates))
        if selected is None:
            if exact and not site_bound:
                selected = exact[0]
            elif len(candidates) == 1 and not site_bound:
                selected = candidates[0]
            else:
                provenance_candidates = set()
                if old.get("_comment") and _provenance_keys(old):
                    for key in _provenance_keys(old):
                        provenance_candidates.update(
                            fresh_by_provenance.get(key, ()))
                        if key[1] == "dockerfile" and key[2] == "FROM":
                            provenance_candidates.update(
                                fresh_dockerfile_sites.get(key[0], ()))
                    provenance_candidates.difference_update(used)
                    if old.get("_inventory_generated") != "unmapped":
                        # A tracked row may use provenance only to
                        # disambiguate among its OWN identity's fresh rows;
                        # with zero identity candidates the mapping went
                        # stale: only fresh unmapped rows (the same site
                        # now unmapped) may claim it.
                        if candidates:
                            provenance_candidates &= set(candidates)
                        else:
                            provenance_candidates = {
                                index for index in provenance_candidates
                                if fresh[index].get(
                                    "_inventory_generated") == "unmapped"}
                if len(provenance_candidates) == 1:
                    selected = next(iter(provenance_candidates))
                    # A same-identity (site-bound) match is a normal
                    # same-component update, not a mapping change.
                    if selected not in candidates:
                        remapped = True
                else:
                    products.append(old)
                    stats["retained_not_observed"] += 1
                    if old.get("_inventory_generated") == "unmapped":
                        retained_unmapped_keys.add(_unmapped_item_key(
                            old.get("product") or old.get("label"),
                            old.get("version"), old.get("_found_in")))
                    continue
        new = fresh[selected]
        used.add(selected)
        mapping_changed = (
            old.get("_inventory_generated") == "unmapped"
            or new.get("_inventory_generated") == "unmapped")
        # A provenance-selected tracked-to-tracked match is a normal
        # same-component update: merge from the old row so matching
        # order never changes merge semantics.
        merged_entry = dict(new) if remapped or mapping_changed \
            else dict(old)
        if not (remapped or mapping_changed):
            merged_entry.update(new)
        curated_keys = [
            "policy_note", "reference_url", "eol_date", "latest"]
        generated_defaults = _generated_unmapped_defaults(existing, old) \
            if old.get("_inventory_generated") == "unmapped" else {}
        for key in ("note", "_comment"):
            if key in old and (
                    key not in generated_defaults
                    or old[key] != generated_defaults[key]):
                curated_keys.append(key)
        for key in curated_keys:
            if key in old:
                merged_entry[key] = old[key]
        products.append(merged_entry)
        if old.get("version") == new.get("version"):
            stats["unchanged"] += 1
        else:
            stats["changed"] += 1

    additions = [p for index, p in enumerate(fresh) if index not in used]
    if additions:
        section = next((index for index, item in enumerate(products)
                        if isinstance(item, dict) and item.get("_section") ==
                        "=== Newly Discovered ==="), None)
        if section is None:
            products.append({"_section": "=== Newly Discovered ==="})
            products.extend(additions)
        else:
            insert_at = next(
                (index for index in range(section + 1, len(products))
                 if isinstance(products[index], dict)
                 and products[index].get("_section")),
                len(products))
            products[insert_at:insert_at] = additions
        stats["added"] = len(additions)

    merged = dict(generated)
    for key, value in existing.items():
        # maven_repositories is regenerated, not curated: a fresh scan is
        # the truth about which repositories the project declares today.
        if key not in ("products", "_inventory", "_skipped_npm_packages",
                       "maven_repositories"):
            merged[key] = value
    merged["products"] = products
    # Deep-copy the _inventory dict so the caller's generated dict is
    # never mutated (sequential merges share their generated argument).
    merged["_inventory"] = dict(generated.get("_inventory") or {})
    if retained_unmapped_keys:
        # Carry forward structured unmapped metadata for retained
        # generated-unmapped products: the fresh scan no longer observes
        # them, so its unmapped list omits them and the report would
        # silently drop the inventory (the plan's "never silently drop"
        # rule). An old item is carried only for the retained row it was
        # generated for: same name, same version, and the same full
        # stable provenance (path, manifest, locator), so a same-name
        # item at a site that is now tracked, or at another locator in
        # the same file, is never borrowed. Deduplicate against fresh
        # structured items on that same key. The unmapped list is copied
        # so the caller's generated dict is never mutated.
        old_inv = existing.get("_inventory")
        old_unmapped = old_inv.get("unmapped") if isinstance(
            old_inv, dict) else []
        if isinstance(old_unmapped, list):
            merged["_inventory"]["unmapped"] = list(
                merged["_inventory"].get("unmapped") or [])
            fresh_unmapped = merged["_inventory"]["unmapped"]
            fresh_keys = set()
            for u in fresh_unmapped:
                if not isinstance(u, dict):
                    continue
                fresh_keys.add(_unmapped_item_key(
                    u.get("name"), u.get("version") or u.get("version_spec"),
                    u.get("found_in")))
            for item in old_unmapped:
                if not isinstance(item, dict):
                    continue
                key = _unmapped_item_key(
                    item.get("name"),
                    item.get("version") or item.get("version_spec"),
                    item.get("found_in"))
                if key in retained_unmapped_keys and key not in fresh_keys:
                    fresh_unmapped.append(item)
                    fresh_keys.add(key)
    merged["_inventory"]["update_summary"] = stats
    return merged


def _atomic_write_json(config, output):
    """Write config JSON atomically: temp file in the target dir + os.replace.

    Output is deterministic ASCII (ensure_ascii=True, fixed indent).
    Serialization happens first, through `dump_bounded_config`, so a
    config over the shared size limit raises ConfigTooLargeError before
    any temp file is created and neither a partial nor an oversize file
    can reach disk.
    """
    text = dump_bounded_config(config)
    dir_name = os.path.dirname(os.path.abspath(output))
    fd, tmp_path = tempfile.mkstemp(
        dir=dir_name, prefix=".eol_config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, output)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main(argv=None):
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(
        description=doc.split("\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=doc.split("Usage:", 1)[1] if "Usage:" in doc else "",
    )
    parser.add_argument("folder", help="Folder to scan (recursively) for dependency files")
    parser.add_argument("--name", help="Project name (default: folder basename)", default=None)
    parser.add_argument("--output", help="Output file (default: eol_config.<name>.json)", default=None)
    parser.add_argument("--exclude", action="append", default=[],
                        help="Extra exclusion pattern (repeatable; same syntax as .eolignore)")
    replacement = parser.add_mutually_exclusive_group()
    replacement.add_argument("--update", action="store_true",
                             help="Merge scan results into an existing config, preserving curation")
    replacement.add_argument("--replace", action="store_true",
                             help="Replace an existing config wholesale")
    parser.add_argument("--include-transitive", action="store_true",
                        help="Include indirect/lockfile dependencies (direct only by default)")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero when any scan warning is emitted (for CI)")
    args = parser.parse_args(argv)

    folder = args.folder
    project_name = args.name or os.path.basename(
        os.path.abspath(os.path.normpath(folder))).replace(" ", "-").lower()
    output = args.output or f"eol_config.{project_name}.json"

    if os.path.exists(output) and not (args.replace or args.update):
        print(f"Refusing to overwrite existing file: {output}", file=sys.stderr)
        print("Re-run with --update to preserve curation, or --replace to replace it.",
              file=sys.stderr)
        return 2

    print(f"Scanning {folder!r}...")
    scan = scan_folder(folder, exclude=args.exclude)

    print(f"  Files scanned        : {len(scan['files'])}")
    print(f"  Dependency records   : {len(scan['records'])}")
    print(f"  Scan warnings        : {len(scan['warnings'])}")

    config = generate_config(scan, project_name,
                             include_transitive=args.include_transitive)
    if args.update and os.path.exists(output):
        try:
            existing = load_bounded_config(output)
            existing_products = existing.get("products")
            if not isinstance(existing_products, list):
                raise ValueError("products value is not an array")
            if any(not isinstance(item, dict) for item in existing_products):
                raise ValueError("products entries must be objects")
            config = _merge_existing_config(existing, config)
        except (ConfigLoadError, OSError, TypeError, ValueError) as exc:
            print(f"Could not update existing config: {exc}", file=sys.stderr)
            return 2
    inventory = config["_inventory"]

    try:
        _atomic_write_json(config, output)
    except ConfigTooLargeError as exc:
        print(f"Refusing to write {output}: {exc}", file=sys.stderr)
        return 2

    print(f"\nWrote {output}")
    print(f"  Tracker entries     : {inventory['summary']['products']}")
    print(f"  Unmapped items      : {inventory['summary']['unmapped']}"
          " (see _inventory.unmapped - review)")
    if inventory["warnings"]:
        print("  Warnings:")
        for warning in inventory["warnings"]:
            print(f"    - [{warning['category']}] {warning['path']}: "
                  f"{redact_urls(warning['message'])}")

    if args.strict and inventory["warnings"]:
        print(f"\n--strict: {inventory['summary']['warnings']} warning(s) "
              "emitted; failing.", file=sys.stderr)
        return 1

    print("\nNext: review the file, then run")
    print(f"  {_live_smoke_command(output)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
