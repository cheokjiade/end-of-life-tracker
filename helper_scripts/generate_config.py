"""Generate an EOL tracker config from a project's dependency files.

Scans a folder for Maven, Gradle, and Node manifests; emits an
eol_config.<project>.json file suitable for use with lambda_function.py.

Supported formats:
    pom.xml             — Maven (multi-module supported via rglob)
    *.gradle.kts        — Gradle Kotlin DSL
    build.gradle        — Gradle Groovy DSL (same regex patterns)
    package.json        — Node (skips node_modules)

Mapping strategy:
    Java deps   -> known group:artifact patterns map to specific tracker
                   providers (endoflife.date Spring Boot/Framework/Tomcat/
                   Log4j, jackson_lifecycle, aws_sdk_lifecycle); everything
                   else falls back to maven_central staleness.
    POM props   -> known names (tomcat.version, netty.version, logback.version,
                   quartz.version, kotlin.version, java.version) produce the
                   matching tracker entry — catches transitively-managed
                   platforms not declared as explicit <dependency>s.
    Node deps   -> known package names map to endoflife.date entries
                   (react, vue, angular, next, nuxt, typescript, node,
                   express); unmapped packages are listed in
                   _skipped_npm_packages for manual review.

Usage:
    python generate_config.py <folder> [--name PROJECT] [--output FILE]

Examples:
    python generate_config.py "project-b" --name b
    python generate_config.py ssg-frontend --name frontend
"""

import argparse
import json
import os
import sys

from eol_inventory import generate_config, scan_folder


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:", 1)[1] if "Usage:" in __doc__ else "",
    )
    parser.add_argument("folder", help="Folder to scan (recursively) for dependency files")
    parser.add_argument("--name", help="Project name (default: folder basename)", default=None)
    parser.add_argument("--output", help="Output file (default: eol_config.<name>.json)", default=None)
    args = parser.parse_args()

    folder = args.folder
    project_name = args.name or os.path.basename(os.path.normpath(folder)).replace(" ", "-").lower()
    output = args.output or f"eol_config.{project_name}.json"

    print(f"Scanning {folder!r}...")
    scan = scan_folder(folder)

    print(f"  Files scanned        : {len(scan['files'])}")
    print(f"  Java/Maven dep decls : {len(scan['java'])}")
    print(f"  POM property files   : {len(scan['pom_properties'])}")
    print(f"  npm dep decls        : {len(scan['node'])}")

    config = generate_config(scan, project_name)
    real_products = [p for p in config["products"] if not p.get("_section")]

    with open(output, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    print(f"\nWrote {output}")
    print(f"  Tracker entries     : {len(real_products)}")
    skipped = config.get("_skipped_npm_packages") or []
    if skipped:
        print(f"  Unmapped npm pkgs   : {len(skipped)} (listed in _skipped_npm_packages — review)")

    print(f"\nNext: review the file, then run")
    print(f"  python lambda_function.py {output}")


if __name__ == "__main__":
    main()
