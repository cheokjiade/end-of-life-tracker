"""Tests for opt-in mvn/gradle transitive resolution (no build tools invoked).

Covers helper_scripts/eol_inventory/resolvers.py: the two pure output
parsers (`mvn dependency:list` text and the eolDumpDeps init-script dump),
the two tool runners with their `run=`/`which=` injection points, and
`resolve_transitive`, which turns resolved coordinates into indirect
(`direct=False`) java records and every failure into a
`transitive_unavailable` warning.

mvn and gradle are NOT installed in the test environment and are never
invoked: every unit test injects fake `run`/`which` callables (a real call
raises), and the end-to-end CLI test runs the scanner in a subprocess with
PATH pointed at an empty directory, so tool lookup fails deterministically.

Moved from tests/test_generate_transitive_parsers.py (parser assertions
unchanged) and tests/test_generate_transitive_merge.py (its `_FakeShutil`/
`_FakeSubprocess` classes are kept and injected instead of monkey-patched;
assertions that pinned the root script's `(kind, file, message)` failure
tuples and its `_discovered_dependencies` outcomes are retargeted to the
consolidated shapes: `transitive_unavailable` warnings and `direct=False`
records gated by `--include-transitive`).

Run from the repository root:  python tests/test_inventory_resolvers.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_HELPER_DIR = ROOT / "helper_scripts"
if str(_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPER_DIR))

from eol_inventory.config_writer import generate_config
from eol_inventory.discovery import scan_folder
from eol_inventory.resolvers import (
    GRADLE_TIMEOUT_S,
    MVN_TIMEOUT_S,
    gradle_dependency_dump,
    mvn_dependency_list,
    parse_gradle_dump,
    parse_mvn_dependency_list,
    resolve_transitive,
)


# --- fakes: no test may ever reach a real mvn/gradle --------------------------

class _FakeShutil:
    """shutil stand-in: no build tool is ever found on PATH."""

    @staticmethod
    def which(tool):
        return None


class _FakeSubprocess:
    """subprocess stand-in: any call is a test failure."""

    @staticmethod
    def run(*args, **kwargs):
        raise AssertionError(
            "subprocess.run must never be called when the tool is unavailable")


class _Proc:
    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def _which_found(tool):
    return f"/usr/bin/{tool}"


def _mvn_run(text, returncode=0):
    """A fake subprocess.run for mvn that writes *text* to -DoutputFile."""

    def run(cmd, **kwargs):
        out_path = None
        for arg in cmd:
            if str(arg).startswith("-DoutputFile="):
                out_path = str(arg).split("=", 1)[1]
        assert out_path, cmd
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return _Proc(returncode=returncode)

    return run


def _gradle_run(text, returncode=0):
    def run(cmd, **kwargs):
        return _Proc(returncode=returncode, stdout=text.encode("utf-8"))

    return run


def _raiser(exc):
    def run(cmd, **kwargs):
        raise exc

    return run


# --- parse_mvn_dependency_list ------------------------------------------------

MVN_OUTPUT = """
[INFO] Scanning for projects...
[INFO]
[INFO] The following files have been resolved:

org.apache.commons:commons-lang3:jar:3.14.0:compile
ch.qos.logback:logback-classic:jar:1.5.6:runtime
junit:junit:jar:4.13.2:test
com.example:lib-with-classifier:jar:2.0.0:provided:classes
com.example:with-classifier:jar:1.2.3:compile:sources
  org.apache.commons:commons-lang3:jar:3.14.0:compile
this is not a dependency line at all
org.example:empty-scope:jar:9.9.9:
[INFO] ------------------------------------------------------------------------
[INFO] BUILD SUCCESS
"""

GRADLE_DUMP = """
Welcome noise from gradle
compileJava:com.example:lib-a:1.0.0
runtimeClasspath:io.netty:netty-common:4.1.111.Final
testCompileClasspath:junit:junit:4.13.2
annotationProcessor:org.example:boom:UNRESOLVED
compileClasspath:org.example:no-version:null
compileClasspath:org.example:none-version:NONE
only:three:segments
one:too:many:segments:here
:empty:lead:1.0.0
compileJava:com.example:lib-a:1.0.0
"""


def test_mvn_list_keeps_compile_and_runtime_and_ignores_noise():
    deps = parse_mvn_dependency_list(MVN_OUTPUT)
    assert deps == [
        ("org.apache.commons", "commons-lang3", "3.14.0"),
        ("ch.qos.logback", "logback-classic", "1.5.6"),
        ("com.example", "lib-with-classifier", "2.0.0"),
        ("com.example", "with-classifier", "1.2.3"),
    ], deps


def test_mvn_list_empty_and_header_only_output():
    assert parse_mvn_dependency_list("") == []
    assert parse_mvn_dependency_list(
        "[INFO] The following files have been resolved:\n[INFO]\n") == []


def test_gradle_dump_strips_config_prefix_and_skips_unresolved():
    deps = parse_gradle_dump(GRADLE_DUMP)
    assert deps == [
        ("com.example", "lib-a", "1.0.0"),
        ("io.netty", "netty-common", "4.1.111.Final"),
        ("junit", "junit", "4.13.2"),
    ], deps


def test_gradle_dump_empty_output():
    assert parse_gradle_dump("") == []


# --- tool runners (injected run=/which=) --------------------------------------

def test_mvn_runner_returns_gavs_on_success():
    gavs, err = mvn_dependency_list(
        "pom.xml", run=_mvn_run(MVN_OUTPUT), which=_which_found)
    assert err is None, err
    assert gavs[0] == ("org.apache.commons", "commons-lang3", "3.14.0"), gavs


def test_mvn_runner_failure_modes():
    gavs, err = mvn_dependency_list(
        "pom.xml", run=_FakeSubprocess.run, which=_FakeShutil.which)
    assert (gavs, err) == (None, "mvn not on PATH"), (gavs, err)

    gavs, err = mvn_dependency_list(
        "pom.xml", run=_mvn_run("", returncode=1), which=_which_found)
    assert (gavs, err) == (None, "mvn exited with status 1"), (gavs, err)

    gavs, err = mvn_dependency_list(
        "pom.xml", run=_mvn_run("noise only\n"), which=_which_found)
    assert (gavs, err) == (
        None, "mvn produced no parseable dependency list"), (gavs, err)

    gavs, err = mvn_dependency_list(
        "pom.xml",
        run=_raiser(subprocess.TimeoutExpired("mvn", MVN_TIMEOUT_S)),
        which=_which_found)
    assert (gavs, err) == (
        None, f"mvn timed out after {MVN_TIMEOUT_S}s"), (gavs, err)

    gavs, err = mvn_dependency_list(
        "pom.xml", run=_raiser(OSError("boom")), which=_which_found)
    assert (gavs, err) == (None, "mvn failed: boom"), (gavs, err)


def test_gradle_runner_failure_modes():
    gavs, err = gradle_dependency_dump(
        ".", run=_FakeSubprocess.run, which=_FakeShutil.which)
    assert (gavs, err) == (None, "gradle not on PATH"), (gavs, err)

    gavs, err = gradle_dependency_dump(
        ".", run=_gradle_run("", returncode=2), which=_which_found)
    assert (gavs, err) == (None, "gradle exited with status 2"), (gavs, err)

    gavs, err = gradle_dependency_dump(
        ".", run=_gradle_run("noise\n"), which=_which_found)
    assert (gavs, err) == (
        None, "gradle produced no parseable dependency dump"), (gavs, err)

    gavs, err = gradle_dependency_dump(
        ".",
        run=_raiser(subprocess.TimeoutExpired("gradle", GRADLE_TIMEOUT_S)),
        which=_which_found)
    assert (gavs, err) == (
        None, f"gradle timed out after {GRADLE_TIMEOUT_S}s"), (gavs, err)

    gavs, err = gradle_dependency_dump(
        ".", run=_raiser(OSError("nope")), which=_which_found)
    assert (gavs, err) == (None, "gradle failed: nope"), (gavs, err)


def test_gradle_runner_returns_gavs_on_success():
    gavs, err = gradle_dependency_dump(
        ".", run=_gradle_run(GRADLE_DUMP), which=_which_found)
    assert err is None, err
    assert ("io.netty", "netty-common", "4.1.111.Final") in gavs, gavs


# --- resolve_transitive -------------------------------------------------------

MINIMAL_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>lib</artifactId>
      <version>1.0.0</version>
    </dependency>
  </dependencies>
</project>
"""
BUILD_GRADLE = "dependencies {\n    implementation 'com.example:lib:1.0.0'\n}\n"
PKG_REACT = '{"dependencies": {"react": "^18.2.0"}}\n'


def _java_tree(tmp):
    (Path(tmp) / "pom.xml").write_text(MINIMAL_POM, encoding="utf-8")
    (Path(tmp) / "build.gradle").write_text(BUILD_GRADLE, encoding="utf-8")
    (Path(tmp) / "package.json").write_text(PKG_REACT, encoding="utf-8")
    return scan_folder(tmp)


def test_resolve_transitive_records_indirect_java_records():
    """Retargeted from the root merge test's promotion assertions: resolved
    coordinates become normalized direct=False records with transitive
    provenance instead of root `(g, a, v, file, kind)` tuples."""
    mvn_text = ("org.apache.commons:commons-lang3:jar:3.14.0:compile\n"
                "com.example:lib:jar:1.0.0:compile\n")
    gradle_text = "runtimeClasspath:io.netty:netty-common:4.1.111.Final\n"

    def run(cmd, **kwargs):
        if any(str(a).startswith("-DoutputFile=") for a in cmd):
            return _mvn_run(mvn_text)(cmd, **kwargs)
        return _gradle_run(gradle_text)(cmd, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        scan = _java_tree(tmp)
        records, warnings = resolve_transitive(
            scan, tmp, run=run, which=_which_found)

    assert warnings == [], warnings
    names = sorted(r["name"] for r in records)
    # com.example:lib:1.0.0 is already a direct record in both manifests, so
    # only the genuinely new coordinates become records.
    assert names == ["io.netty:netty-common",
                     "org.apache.commons:commons-lang3"], names
    for record in records:
        assert record["ecosystem"] == "java", record
        assert record["direct"] is False, record
        assert record["kind"] == "dependency", record
        assert record["group"] and record["artifact"], record
        assert [loc["locator"] for loc in record["found_in"]] == [
            "transitive"], record

    by_name = {r["name"]: r for r in records}
    assert by_name["org.apache.commons:commons-lang3"]["found_in"][0] == {
        "path": "pom.xml", "manifest": "mvn", "locator": "transitive"}
    assert by_name["io.netty:netty-common"]["found_in"][0] == {
        "path": "build.gradle", "manifest": "gradle", "locator": "transitive"}


def test_resolve_transitive_warns_once_per_manifest_when_tools_absent():
    """Retargeted from the root merge test's `unavailable` tuples: each
    failure is now a structured `transitive_unavailable` warning."""
    with tempfile.TemporaryDirectory() as tmp:
        scan = _java_tree(tmp)
        records, warnings = resolve_transitive(
            scan, tmp, run=_FakeSubprocess.run, which=_FakeShutil.which)

    assert records == [], records
    assert [(w["category"], w["path"], w["message"]) for w in warnings] == [
        ("transitive_unavailable", "pom.xml", "mvn not on PATH"),
        ("transitive_unavailable", "build.gradle", "gradle not on PATH"),
    ], warnings


def test_resolve_transitive_runs_gradle_once_per_project_directory():
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("cwd")))
        if any(str(a).startswith("-DoutputFile=") for a in cmd):
            return _mvn_run("org.example:a:jar:1.0:compile\n")(cmd, **kwargs)
        return _gradle_run("c:org.example:b:2.0\n")(cmd, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app").mkdir()
        (root / "app" / "build.gradle").write_text(BUILD_GRADLE, encoding="utf-8")
        (root / "app" / "settings.gradle").write_text(
            "rootProject.name = 'app'\n", encoding="utf-8")
        (root / "lib").mkdir()
        (root / "lib" / "build.gradle.kts").write_text(
            BUILD_GRADLE, encoding="utf-8")
        scan = scan_folder(tmp)
        records, warnings = resolve_transitive(
            scan, tmp, run=run, which=_which_found)

    gradle_calls = [c for c in calls
                    if not any(str(a).startswith("-DoutputFile=") for a in c[0])]
    assert len(gradle_calls) == 2, gradle_calls
    assert sorted(os.path.basename(str(cwd)) for _cmd, cwd in gradle_calls) == [
        "app", "lib"], gradle_calls
    assert warnings == [], warnings
    assert [r["name"] for r in records] == ["org.example:b"], records
    assert len(records[0]["found_in"]) == 2, records[0]


def test_transitive_records_reach_products_only_when_asked():
    """Retargeted from the root merge test's products-gating block: indirect
    java records follow the same `--include-transitive` rule as npm/python/go
    lock-graph records, and are counted in the summary either way."""
    mvn = _mvn_run("com.fasterxml.jackson.core:jackson-databind:"
                   "jar:2.19.0:compile\n")
    gradle = _gradle_run("")  # gradle resolves nothing here

    def run(cmd, **kwargs):
        if any(str(a).startswith("-DoutputFile=") for a in cmd):
            return mvn(cmd, **kwargs)
        return gradle(cmd, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        scan = _java_tree(tmp)
        records, warnings = resolve_transitive(
            scan, tmp, run=run, which=_which_found)
    assert [w["message"] for w in warnings] == [
        "gradle produced no parseable dependency dump"], warnings
    scan["records"].extend(records)
    scan["warnings"].extend(warnings)

    off = generate_config(scan, "transitive-demo")
    on = generate_config(scan, "transitive-demo", include_transitive=True)

    labels_off = [p["label"] for p in off["products"] if not p.get("_section")]
    labels_on = [p["label"] for p in on["products"] if not p.get("_section")]
    assert not [x for x in labels_off if x.startswith("Jackson")], labels_off
    assert [x for x in labels_on if x.startswith("Jackson")], labels_on
    assert set(labels_off) <= set(labels_on), (labels_off, labels_on)
    assert off["_inventory"]["summary"]["indirect"] >= 1, off["_inventory"]
    assert (off["_inventory"]["summary"]["indirect"]
            == on["_inventory"]["summary"]["indirect"])


# --- CLI end-to-end with an empty PATH (no tool can be found) -----------------

def test_cli_resolve_transitive_with_no_tools_on_path():
    fixture = ROOT / "tests" / "fixtures" / "generate_config" / "mixed"
    with tempfile.TemporaryDirectory() as tmp:
        empty_bin = Path(tmp) / "empty-bin"
        empty_bin.mkdir()
        out_path = Path(tmp) / "out.json"
        env = dict(os.environ)
        env["PATH"] = str(empty_bin)
        env["PYTHONPYCACHEPREFIX"] = str(Path(tmp) / "pycache")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "helper_scripts" / "generate_config.py"),
             str(fixture), "--name", "cli-transitive",
             "--output", str(out_path), "--resolve-transitive"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        config = json.loads(out_path.read_text(encoding="utf-8"))

    inventory = config["_inventory"]
    assert inventory["include_transitive"] is True, inventory
    unavailable = [w for w in inventory["warnings"]
                   if w["category"] == "transitive_unavailable"]
    assert sorted((w["path"], w["message"]) for w in unavailable) == [
        ("build.gradle", "gradle not on PATH"),
        ("pom.xml", "mvn not on PATH"),
    ], unavailable


TESTS = [
    test_mvn_list_keeps_compile_and_runtime_and_ignores_noise,
    test_mvn_list_empty_and_header_only_output,
    test_gradle_dump_strips_config_prefix_and_skips_unresolved,
    test_gradle_dump_empty_output,
    test_mvn_runner_returns_gavs_on_success,
    test_mvn_runner_failure_modes,
    test_gradle_runner_failure_modes,
    test_gradle_runner_returns_gavs_on_success,
    test_resolve_transitive_records_indirect_java_records,
    test_resolve_transitive_warns_once_per_manifest_when_tools_absent,
    test_resolve_transitive_runs_gradle_once_per_project_directory,
    test_transitive_records_reach_products_only_when_asked,
    test_cli_resolve_transitive_with_no_tools_on_path,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    if failed:
        print(f"{failed} test(s) failed")
        return 1
    print("OK test_inventory_resolvers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
