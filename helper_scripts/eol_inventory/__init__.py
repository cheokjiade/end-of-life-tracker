"""eol_inventory — importable manifest-to-config generator package.

Everything the manifest scanner needs lives here as importable, network-free
modules so standalone assertion tests can exercise parsing, mapping, and
config generation without subprocesses:

    mappings       version helpers and the provider mapping tables
    discovery      folder walking and manifest discovery (scan_folder)
    config_writer  de-duplication and EOL config assembly (generate_config)
    parsers.java   Maven POM + Gradle declaration parsing
    parsers.node   package.json parsing

The command-line entry point is the sibling generate_config.py script; this
package holds no CLI.
"""

from .config_writer import _entry_key, generate_config
from .discovery import scan_folder
from .mappings import (
    _JAVA_MAPPINGS,
    _NPM_MAPPINGS,
    _POM_PROPERTY_MAPPINGS,
    _clean_version,
    _eol_entry,
    _major,
    _major_minor,
    _map_java_dep,
    _map_npm_dep,
    _mc_entry,
)
from .parsers import parse_gradle, parse_package_json, parse_pom

__all__ = [
    "scan_folder",
    "generate_config",
    "parse_pom",
    "parse_gradle",
    "parse_package_json",
    "_entry_key",
    "_JAVA_MAPPINGS",
    "_NPM_MAPPINGS",
    "_POM_PROPERTY_MAPPINGS",
    "_clean_version",
    "_eol_entry",
    "_major",
    "_major_minor",
    "_map_java_dep",
    "_map_npm_dep",
    "_mc_entry",
]
