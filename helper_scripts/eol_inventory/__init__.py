"""eol_inventory — importable manifest-to-config generator package.

Everything the project scanner needs lives here as importable, network-free
modules so standalone assertion tests can exercise parsing, mapping, and
config generation without subprocesses:

    models         normalized records, provenance, warnings, exclusions
    mappings       version helpers and the provider mapping tables
    discovery      deterministic folder walking (scan_folder)
    config_writer  provenance merging and EOL config assembly (generate_config)
    parsers.java   Maven POM + Gradle declaration parsing
    parsers.node   package.json parsing
    parsers.python requirements/pyproject/Pipfile.lock + runtime evidence
    parsers.go     go.mod parsing
    parsers.dotnet csproj/fsproj/vbproj, central versions, global.json
    parsers.docker Dockerfile FROM instructions
    parsers.gitlab_ci  GitLab CI images and services

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
    _dotnet_runtime_cycle,
    _eol_entry,
    _go_proxy_entry,
    _image_skip_reason,
    _major,
    _major_minor,
    _map_image_dep,
    _map_java_dep,
    _map_npm_dep,
    _mc_entry,
    _npm_registry_entry,
    _nuget_entry,
    _pypi_entry,
)
from .models import (
    DEFAULT_EXCLUDED_DIRS,
    MAX_FILES,
    MAX_FILE_BYTES,
    SCHEMA_VERSION,
    GENERATOR_VERSION,
    add_location,
    format_location,
    is_excluded,
    load_ignore_patterns,
    new_record,
    new_warning,
    sort_locations,
    sort_warnings,
)
from .parsers import (
    parse_csproj_records,
    parse_directory_packages_props,
    parse_dockerfile_records,
    parse_gitlab_ci_records,
    parse_global_json_records,
    parse_go_mod_records,
    parse_gradle_records,
    parse_package_json_records,
    parse_pipfile_records,
    parse_pipfile_lock_records,
    parse_pom_records,
    parse_pyproject_records,
    parse_python_version_records,
    parse_requirements_records,
    parse_runtime_txt_records,
)

__all__ = [
    "scan_folder",
    "generate_config",
    "parse_pom_records",
    "parse_gradle_records",
    "parse_package_json_records",
    "parse_requirements_records",
    "parse_pyproject_records",
    "parse_pipfile_records",
    "parse_pipfile_lock_records",
    "parse_python_version_records",
    "parse_runtime_txt_records",
    "parse_go_mod_records",
    "parse_csproj_records",
    "parse_directory_packages_props",
    "parse_global_json_records",
    "parse_dockerfile_records",
    "parse_gitlab_ci_records",
    "_entry_key",
    "_JAVA_MAPPINGS",
    "_NPM_MAPPINGS",
    "_POM_PROPERTY_MAPPINGS",
    "_clean_version",
    "_dotnet_runtime_cycle",
    "_eol_entry",
    "_go_proxy_entry",
    "_image_skip_reason",
    "_major",
    "_major_minor",
    "_map_image_dep",
    "_map_java_dep",
    "_map_npm_dep",
    "_mc_entry",
    "_npm_registry_entry",
    "_nuget_entry",
    "_pypi_entry",
    "DEFAULT_EXCLUDED_DIRS",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "SCHEMA_VERSION",
    "GENERATOR_VERSION",
    "add_location",
    "format_location",
    "is_excluded",
    "load_ignore_patterns",
    "new_record",
    "new_warning",
    "sort_locations",
    "sort_warnings",
]
