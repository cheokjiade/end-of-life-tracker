"""Package marker for the manifest parsers.

Every parser returns (records, warnings) in the normalized model of
`eol_inventory.models`. `parse_gitlab_ci_records` additionally accepts
the scan root as its third argument so local includes stay inside it.
"""

from .docker import parse_dockerfile_records
from .dotnet import (
    parse_csproj_records,
    parse_directory_packages_props,
    parse_global_json_records,
)
from .gitlab_ci import parse_gitlab_ci_records
from .go import parse_go_mod_records
from .java import parse_gradle_records, parse_pom_records
from .node import parse_package_json_records
from .python import (
    parse_pipfile_lock_records,
    parse_pyproject_records,
    parse_python_version_records,
    parse_requirements_records,
    parse_runtime_txt_records,
)

__all__ = [
    "parse_pom_records",
    "parse_gradle_records",
    "parse_package_json_records",
    "parse_requirements_records",
    "parse_pyproject_records",
    "parse_pipfile_lock_records",
    "parse_python_version_records",
    "parse_runtime_txt_records",
    "parse_go_mod_records",
    "parse_csproj_records",
    "parse_directory_packages_props",
    "parse_global_json_records",
    "parse_dockerfile_records",
    "parse_gitlab_ci_records",
]
