"""Tool-driven transitive resolution (the only module that runs commands).

Reached only from ``helper_scripts/generate_config.py --resolve-transitive``.
Every other module in this package is file-parsing only and never starts a
subprocess. Here, ``mvn dependency:list`` is run once per ``pom*.xml`` and a
Gradle ``eolDumpDeps`` init-script task once per project directory holding a
``build.gradle(.kts)``; the resolved coordinates become normalized
``direct=False`` java records, and a missing tool, non-zero exit, timeout, or
unparseable output degrades to a ``transitive_unavailable`` warning while the
scan continues.

``run``/``which`` are injectable on every entry point so tests exercise the
success and failure paths without a build tool installed.
"""

import fnmatch
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import add_location, new_record, new_warning

MVN_TIMEOUT_S = 180
GRADLE_TIMEOUT_S = 240

# File names that trigger each resolver.
POM_PATTERN = "pom*.xml"
GRADLE_BUILD_FILES = ("build.gradle", "build.gradle.kts")

# Kotlin-DSL init script: registers an eolDumpDeps task on every project
# that prints one "<configuration>:<group>:<artifact>:<version>" line per
# resolved module and a "<configuration>:UNRESOLVED" marker for
# configurations whose resolution failed. Run with:
#   gradle -q --init-script <this-file> eolDumpDeps
GRADLE_INIT_SCRIPT = """\
allprojects {
    tasks.register("eolDumpDeps") {
        doLast {
            project.configurations.matching { it.isCanBeResolved }.all { cfg ->
                try {
                    cfg.resolvedConfiguration.lenientConfiguration.modules.forEach { m ->
                        val id = m.module.id
                        println(cfg.name + ":" + id.group + ":" + id.name + ":" + id.version)
                    }
                } catch (e: Exception) {
                    println(cfg.name + ":UNRESOLVED")
                }
            }
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Output parsers (pure)
# ---------------------------------------------------------------------------

# One resolved mvn dependency line. Maven prints "group:artifact:type:
# version:scope" and, for classified artifacts, a 6th classifier field. An
# optional "[INFO] "-style prefix is tolerated. Every segment must be
# non-empty and colon-free for the line to match; anything else (headers,
# blank lines, download progress) is ignored.
_MVN_GAV_LINE_RE = re.compile(
    r"^(?:\[[A-Z][A-Z ]*\]\s*)?"
    r"([^:\s]+):([^:\s]+):([^:\s]+):([^:\s]+):([^:\s]+)(?::([^:\s]+))?\s*$"
)


def parse_mvn_dependency_list(text):
    """Parse `mvn dependency:list -DoutputFile=...` text -> [(g, a, v)].

    Matches only strict 5-field (group:artifact:type:version:scope) or
    6-field (...:classifier) lines. Test-scoped entries are dropped (the
    tracker follows runtime classpaths, not test trees); 6-field lines have
    their classifier component stripped (a classifier jar duplicates the
    base artifact); duplicates are deduped keeping first occurrence.
    Header noise ("The following files have been resolved"), empty or
    garbage lines, and lines with empty fields are ignored. Pure,
    order-stable.
    """
    deps = []
    seen = set()
    for raw in text.splitlines():
        m = _MVN_GAV_LINE_RE.match(raw.strip())
        if not m:
            continue
        g, a, _type, v, scope, _classifier = m.groups()
        if scope == "test":
            continue
        if (g, a, v) not in seen:
            seen.add((g, a, v))
            deps.append((g, a, v))
    return deps


# One eolDumpDeps output line: "<configuration>:<group>:<artifact>:<version>".
# Every segment must be non-empty and colon-free; anything else (Gradle
# warnings, "<configuration>:UNRESOLVED" markers, blank lines) is ignored.
_GRADLE_DUMP_LINE_RE = re.compile(r"^[^:\s]+:[^:\s]+:[^:\s]+:[^:\s]+$")


def parse_gradle_dump(text):
    """Parse the eolDumpDeps init-script stdout -> [(g, a, v)].

    Lines have the shape "<config-name>:<group>:<artifact>:<version>"; the
    configuration-name prefix is stripped. Skips lines containing
    "UNRESOLVED" (a configuration whose resolution failed), lines that are
    not 4 colon-separated segments, and entries whose version is missing,
    "NONE" or "null" (case-insensitive; Gradle prints these for
    unresolvable module versions). Duplicates deduped; pure, order-stable.
    """
    deps = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "UNRESOLVED" in line:
            continue
        if not _GRADLE_DUMP_LINE_RE.match(line):
            continue
        _cfg, g, a, v = line.split(":")
        if v.lower() in ("none", "null"):
            continue
        if (g, a, v) not in seen:
            seen.add((g, a, v))
            deps.append((g, a, v))
    return deps


# ---------------------------------------------------------------------------
# Tool runners
# ---------------------------------------------------------------------------

def mvn_dependency_list(pom_path, *, run=subprocess.run, which=shutil.which):
    """Run mvn dependency:list for *pom_path*; return (gavs, error).

    (gavs, None) on success; (None, reason) when mvn is not on PATH,
    exits nonzero, times out, or produces no parseable output. Never
    raises. shutil.which("mvn") resolves mvn.cmd on Windows via PATHEXT.
    *run* and *which* are injectable so tests never execute a build tool.
    """
    mvn = which("mvn")
    if not mvn:
        return None, "mvn not on PATH"
    fd, out_path = tempfile.mkstemp(prefix="eol-mvn-deps-", suffix=".txt")
    os.close(fd)
    try:
        proc = run(
            [mvn, "-B", "-q", "-f", str(pom_path), "dependency:list",
             f"-DoutputFile={out_path}"],
            capture_output=True, timeout=MVN_TIMEOUT_S)
        if proc.returncode != 0:
            return None, f"mvn exited with status {proc.returncode}"
        with open(out_path, encoding="utf-8", errors="replace") as f:
            gavs = parse_mvn_dependency_list(f.read())
    except subprocess.TimeoutExpired:
        return None, f"mvn timed out after {MVN_TIMEOUT_S}s"
    except OSError as exc:
        return None, f"mvn failed: {exc}"
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    if not gavs:
        return None, "mvn produced no parseable dependency list"
    return gavs, None


def gradle_dependency_dump(project_dir, *, run=subprocess.run, which=shutil.which):
    """Run the eolDumpDeps init script in *project_dir*; return (gavs, error).

    Writes the Kotlin-DSL init script to a temp file and runs
    `gradle -q --init-script <tmp> eolDumpDeps` once for the project root.
    (gavs, None) on success; (None, reason) when gradle is not on PATH,
    exits nonzero, times out, or produces no parseable output. Never
    raises. *run* and *which* are injectable so tests never execute a
    build tool.
    """
    gradle = which("gradle")
    if not gradle:
        return None, "gradle not on PATH"
    fd, init_path = tempfile.mkstemp(prefix="eol-dump.", suffix=".init.gradle.kts")
    os.close(fd)
    try:
        with open(init_path, "w", encoding="utf-8") as f:
            f.write(GRADLE_INIT_SCRIPT)
        proc = run(
            [gradle, "-q", "--init-script", init_path, "eolDumpDeps"],
            cwd=str(project_dir), capture_output=True, timeout=GRADLE_TIMEOUT_S)
        if proc.returncode != 0:
            return None, f"gradle exited with status {proc.returncode}"
        gavs = parse_gradle_dump(proc.stdout.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        return None, f"gradle timed out after {GRADLE_TIMEOUT_S}s"
    except OSError as exc:
        return None, f"gradle failed: {exc}"
    finally:
        try:
            os.unlink(init_path)
        except OSError:
            pass
    if not gavs:
        return None, "gradle produced no parseable dependency dump"
    return gavs, None


# ---------------------------------------------------------------------------
# Scan-level resolution
# ---------------------------------------------------------------------------

def _gradle_project_files(files):
    """Relative build files, one per project directory, in scan order."""
    seen_dirs = set()
    chosen = []
    for rel in files:
        if posixpath.basename(rel) not in GRADLE_BUILD_FILES:
            continue
        directory = posixpath.dirname(rel)
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        chosen.append(rel)
    return chosen


def resolve_transitive(scan, root, *, run=subprocess.run, which=shutil.which):
    """Resolve the java dependency graph with mvn/gradle; return (records, warnings).

    *scan* is a scan_folder result; *root* the scanned folder. Every
    ``pom*.xml`` in ``scan["files"]`` is resolved with mvn, and every
    directory holding a ``build.gradle(.kts)`` once with gradle. Resolved
    coordinates that are not already a direct java record become
    ``direct=False`` records carrying a ``locator="transitive"`` provenance
    location on the manifest they came from; coordinates reported by more
    than one manifest collapse to one record with both locations. Each
    failure (tool absent, non-zero exit, timeout, unparseable output)
    becomes one ``transitive_unavailable`` warning; nothing raises.
    """
    root_path = Path(root)
    files = scan.get("files") or ()
    direct_gavs = {
        (r.get("group"), r.get("artifact"), r.get("version"))
        for r in scan.get("records") or ()
        if r.get("ecosystem") == "java" and r.get("direct")
    }

    records = []
    by_gav = {}
    warnings = []

    def collect(gavs, rel_manifest, manifest):
        for g, a, v in gavs:
            if (g, a, v) in direct_gavs:
                continue
            record = by_gav.get((g, a, v))
            if record is None:
                record = new_record("java", f"{g}:{a}", version=v, direct=False,
                                    kind="dependency", group=g, artifact=a)
                by_gav[(g, a, v)] = record
                records.append(record)
            add_location(record, rel_manifest, manifest, locator="transitive")

    for rel in files:
        if not fnmatch.fnmatchcase(posixpath.basename(rel), POM_PATTERN):
            continue
        print(f"  Resolving transitive deps via mvn for {rel}...")
        gavs, error = mvn_dependency_list(
            root_path / rel, run=run, which=which)
        if error:
            warnings.append(
                new_warning("transitive_unavailable", rel, error))
            continue
        collect(gavs, rel, "mvn")

    for rel in _gradle_project_files(files):
        project_dir = (root_path / rel).parent
        print(f"  Resolving transitive deps via gradle for {rel}...")
        gavs, error = gradle_dependency_dump(
            project_dir, run=run, which=which)
        if error:
            warnings.append(
                new_warning("transitive_unavailable", rel, error))
            continue
        collect(gavs, rel, "gradle")

    return records, warnings
