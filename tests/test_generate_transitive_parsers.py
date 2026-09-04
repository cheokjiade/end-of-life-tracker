"""Network-free transitive-resolution parser tests (no build tools invoked).

Covers the three pure parsers behind --resolve-transitive: mvn
dependency:list output, gradle eolDumpDeps init-script output, and npm
package-lock.json (lockfileVersion 2/3 and 1 shapes). mvn and gradle are
NOT installed in the test environment and are never invoked — only the
output parsers run, against synthetic text and temp files.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_config import (
    parse_gradle_dump,
    parse_mvn_dependency_list,
    parse_npm_lockfile,
    parse_package_json,
)


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

deps = parse_mvn_dependency_list(MVN_OUTPUT)
assert deps == [
    ("org.apache.commons", "commons-lang3", "3.14.0"),
    ("ch.qos.logback", "logback-classic", "1.5.6"),
    ("com.example", "lib-with-classifier", "2.0.0"),
    ("com.example", "with-classifier", "1.2.3"),
], deps
print("OK mvn list: compile/runtime kept, header noise and garbage ignored")
print("OK mvn list: test scope skipped, classifier stripped, duplicates deduped")

assert parse_mvn_dependency_list("") == []
assert parse_mvn_dependency_list(
    "[INFO] The following files have been resolved:\n[INFO]\n") == []
print("OK mvn list: empty output and header-only output yield no deps")


# --- parse_gradle_dump ----------------------------------------------------------

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

deps = parse_gradle_dump(GRADLE_DUMP)
assert deps == [
    ("com.example", "lib-a", "1.0.0"),
    ("io.netty", "netty-common", "4.1.111.Final"),
    ("junit", "junit", "4.13.2"),
], deps
print("OK gradle dump: config-name prefix stripped, UNRESOLVED/null/NONE skipped")
print("OK gradle dump: non-4-segment noise and empty segments skipped, deduped")

assert parse_gradle_dump("") == []
print("OK gradle dump: empty output yields no deps")


# --- parse_npm_lockfile -----------------------------------------------------------

def _write_lock(tmp, name, data):
    p = Path(tmp) / name
    p.write_text(json.dumps(data) if not isinstance(data, str) else data,
                 encoding="utf-8")
    return p


with tempfile.TemporaryDirectory() as tmp:
    # lockfileVersion 3: packages map, scoped names, nested installs,
    # root entry, link:/file: resolved entries, version-less entries.
    v3 = _write_lock(tmp, "lock-v3.json", {
        "name": "app", "version": "1.0.0", "lockfileVersion": 3,
        "packages": {
            "": {"name": "app", "version": "1.0.0"},
            "node_modules/react": {"version": "18.3.1"},
            "node_modules/@angular/core": {"version": "17.3.0"},
            "node_modules/@scope/thing": {"version": "1.2.3"},
            "node_modules/react/node_modules/react-dom": {"version": "18.3.1"},
            "node_modules/linked-pkg": {"resolved": "link:../linked-pkg",
                                        "link": True},
            "node_modules/file-pkg": {"resolved": "file:../local",
                                      "version": "0.0.0"},
            "node_modules/no-version": {"resolved": "https://registry.example/x"},
        },
    })
    deps = parse_npm_lockfile(v3)
    assert deps == [
        ("react", "18.3.1"),
        ("@angular/core", "17.3.0"),
        ("@scope/thing", "1.2.3"),
        ("react-dom", "18.3.1"),
    ], deps
    print("OK npm lock v3: scoped names kept, nested installs, root skipped")
    print("OK npm lock v3: link:/file: resolved and version-less entries skipped")

    # lockfileVersion 2: has both shapes — "packages" wins, legacy ignored.
    v2 = _write_lock(tmp, "lock-v2.json", {
        "lockfileVersion": 2,
        "packages": {"node_modules/x": {"version": "1.0.0"}},
        "dependencies": {"x": {"version": "9.9.9"}},
    })
    assert parse_npm_lockfile(v2) == [("x", "1.0.0")], parse_npm_lockfile(v2)
    print("OK npm lock v2: packages map preferred over legacy dependencies")

    # lockfileVersion 1: dependencies tree, recursed including nested installs.
    v1 = _write_lock(tmp, "lock-v1.json", {
        "name": "app", "version": "1.0.0", "lockfileVersion": 1,
        "dependencies": {
            "react": {"version": "18.2.0"},
            "vue": {"version": "3.4.21"},
            "lodash": {"version": "4.17.21", "requires": {"react": "^18"}},
            "nested-parent": {
                "version": "1.0.0",
                "dependencies": {"react": {"version": "17.0.2"}},
            },
            "linked": {"resolved": "link:../x"},
        },
    })
    deps = parse_npm_lockfile(v1)
    assert deps == [
        ("react", "18.2.0"),
        ("vue", "3.4.21"),
        ("lodash", "4.17.21"),
        ("nested-parent", "1.0.0"),
        ("react", "17.0.2"),
    ], deps
    print("OK npm lock v1: tree recursed with nested installs, links skipped")

    # Malformed JSON -> [] (with one stderr warning), never raises.
    bad = Path(tmp) / "lock-bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert parse_npm_lockfile(bad) == []
    print("OK npm lock: malformed JSON -> [] (never raises)")

    # A UTF-8 BOM on a hand-edited lockfile is tolerated (utf-8-sig).
    bom = Path(tmp) / "lock-bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"packages": {"node_modules/x": {"version": "1.0.0"}}}).encode("utf-8"))
    assert parse_npm_lockfile(bom) == [("x", "1.0.0")]
    print("OK npm lock: UTF-8 BOM tolerated")

    # Same tolerance for the package.json parser the lockfiles sit beside.
    pkg_bom = Path(tmp) / "package-bom.json"
    pkg_bom.write_bytes(b"\xef\xbb\xbf" + b'{"dependencies": {"react": "18.2.0"}}')
    assert parse_package_json(pkg_bom) == [("react", "18.2.0")]
    print("OK package.json: UTF-8 BOM tolerated")

    # Missing file -> [] (OSError handled).
    assert parse_npm_lockfile(Path(tmp) / "does-not-exist.json") == []
    print("OK npm lock: missing file -> [] (never raises)")

    # Non-dict top level -> [].
    arr = Path(tmp) / "lock-arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    assert parse_npm_lockfile(arr) == []
    print("OK npm lock: non-dict top level -> []")

print("OK test_generate_transitive_parsers")
