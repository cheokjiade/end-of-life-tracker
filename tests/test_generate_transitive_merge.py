"""Network-free --resolve-transitive merge/gating tests (no tools invoked).

mvn and gradle are NOT installed in the test environment and are never
invoked: in the CLI test shutil.which is stubbed to None and
subprocess.run stubbed to raise, so the unavailability path is exercised
deterministically and any accidental tool call fails loudly. Covers the
products-gating rule (mapped transitives may add rows, unmapped
transitives are records-only), the lockfile scan merge, and the flag-off
regression.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.validation import validate_config
import generate_config as gc
from generate_config import generate_config, scan_folder

UNMAPPED_TRANSITIVE = "unmapped-transitive (tracked in records only)"


def one(records, decl):
    """The single record for *decl* (declarations appear exactly once)."""
    hits = [r for r in records if r["decl"] == decl]
    assert len(hits) == 1, (decl, hits)
    return hits[0]


# --- 1. products gating over a hand-built scan --------------------------------

SCAN = {
    "java": [
        # direct declarations (identical behavior with the flag on or off)
        ("com.fasterxml.jackson.core", "jackson-databind", "2.19.0", "pom.xml", "dep"),
        ("io.netty", "netty-codec-http", "4.1.111.Final", "pom.xml", "dep"),
        # transitive-maven: mapped -> promoted, transitive _comment
        ("com.fasterxml.jackson.core", "jackson-core", "2.19.1", "pom.xml",
         "transitive-maven"),
        # transitive-maven: exact duplicate of the DIRECT row -> duplicate-of
        ("io.netty", "netty-codec-http", "4.1.111.Final", "pom.xml",
         "transitive-maven"),
        # transitive-maven: same dedupe key (2.19.x -> "2.19") -> duplicate-of
        ("com.fasterxml.jackson.core", "jackson-databind", "2.19.1", "pom.xml",
         "transitive-maven"),
        # transitive-maven: unmapped (webjars handler declines) -> records only
        ("org.webjars.npm", "popper.js", "1.16.1", "pom.xml", "transitive-maven"),
        # transitive-gradle: mapped -> promoted
        ("org.springframework.boot", "spring-boot-gradle-plugin", "3.3.4",
         "build.gradle", "transitive-gradle"),
        # transitive-gradle: skip-condition version -> records only
        ("com.example", "wip", "1.0-SNAPSHOT", "build.gradle", "transitive-gradle"),
    ],
    "pom_properties": [],
    "node": [
        ("react", "^18.2.0", "package.json"),
        # npm-lock mapped, dedupes with the direct react row
        ("react", "18.3.1", "package-lock.json", "npm-lock"),
        # npm-lock unmapped -> records only, NOT in _skipped_npm_packages
        ("lodash", "4.17.21", "package-lock.json", "npm-lock"),
        # npm-lock mapped -> promoted
        ("vue", "3.5.3", "package-lock.json", "npm-lock"),
    ],
    "files": ["pom.xml", "build.gradle", "package.json", "package-lock.json"],
    "repositories": [],
    "transitive_unavailable": [
        ("transitive-maven", "pom.xml", "mvn not on PATH"),
    ],
}

config = generate_config(SCAN, "transitive-demo")
records = config["_discovered_dependencies"]

# tracked / duplicate-of / unmapped-transitive outcomes
r = one(records, "com.fasterxml.jackson.core:jackson-core:2.19.1")
assert (r["kind"], r["file"]) == ("transitive-maven", "pom.xml"), r
assert r["outcome"] == "tracked: Jackson Core 2.19", r
netty = [x for x in records if x["decl"] == "io.netty:netty-codec-http:4.1.111.Final"]
assert len(netty) == 2, netty
assert {x["outcome"] for x in netty} == {
    "tracked: netty-codec-http 4.1.111.Final",
    "duplicate-of: netty-codec-http 4.1.111.Final",
}, netty
r = one(records, "com.fasterxml.jackson.core:jackson-databind:2.19.1")
assert r["outcome"] == "duplicate-of: Jackson Databind 2.19", r
r = one(records, "org.springframework.boot:spring-boot-gradle-plugin:3.3.4")
assert r["kind"] == "transitive-gradle", r
assert r["outcome"] == "tracked: Spring Boot 3.3", r
r = one(records, "react@18.3.1")
assert r["kind"] == "npm-lock" and r["outcome"] == "duplicate-of: React 18", r
r = one(records, "vue@3.5.3")
assert r["kind"] == "npm-lock" and r["outcome"] == "tracked: Vue 3.5", r

# unmapped transitives: records-only, never rows, never _skipped_npm_packages
for decl in ("org.webjars.npm:popper.js:1.16.1", "com.example:wip:1.0-SNAPSHOT",
             "lodash@4.17.21"):
    r = one(records, decl)
    assert r["outcome"] == UNMAPPED_TRANSITIVE, r
assert "_skipped_npm_packages" not in config, config.keys()
rows = [p for p in config["products"] if not p.get("_section")]
assert not any(p.get("group") == "org.webjars.npm" for p in rows), rows
assert not any(p.get("source") == "maven_central"
               and p.get("artifact") == "popper.js" for p in rows), rows

# the one unavailable resolution became a per-manifest skipped record
r = [x for x in records if x["decl"] == "mvn transitive resolution"]
assert r == [{"decl": "mvn transitive resolution", "file": "pom.xml",
              "kind": "transitive-maven",
              "outcome": "skipped: transitive resolution unavailable "
                         "(mvn not on PATH or failed)"}], r

# promoted rows carry the transitive-provenance _comment
jc = [p for p in rows if p["label"] == "Jackson Core 2.19"][0]
assert jc["_comment"] == ("Transitive via mvn (com.fasterxml.jackson.core:"
                          "jackson-core:2.19.1)"), jc
sb = [p for p in rows if p["label"] == "Spring Boot 3.3"][0]
assert sb["_comment"] == ("Transitive via gradle (org.springframework.boot:"
                          "spring-boot-gradle-plugin:3.3.4)"), sb
vue = [p for p in rows if p["label"] == "Vue 3.5"][0]
assert vue["_comment"] == "Transitive via package-lock.json (vue@3.5.3)", vue
direct = [p for p in rows if p["label"] == "Jackson Databind 2.19"][0]
assert direct["_comment"].startswith("From "), direct

# products: 6 promoted/tracked rows + the inferred Spring Security row
assert sorted(p["label"] for p in rows) == [
    "Jackson Core 2.19",
    "Jackson Databind 2.19",
    "React 18",
    "Spring Boot 3.3",
    "Spring Security 6.3",
    "Vue 3.5",
    "netty-codec-http 4.1.111.Final",
], rows

# tally counts unmapped-transitives; skipped counts the unavailable manifest
summary = [ln for ln in config["_comment"]
           if ln.startswith("Declarations discovered:")]
assert summary == [
    "Declarations discovered: 13 (tracked 6, duplicates 3, skipped 1, "
    "unmapped 0, unmapped-transitive 3) - see _discovered_dependencies "
    "for the complete picture."
], summary

findings = validate_config(config)
assert not [f for f in findings if f["severity"] == "error"], findings
print("OK gating: mapped transitives promote (with provenance _comment),")
print("OK gating: unmapped transitives are records-only, rows and")
print("OK gating: _skipped_npm_packages untouched; duplicates -> duplicate-of")


# --- 2. scan_folder lockfile merge (no tools; flag on vs off) -------------------

PKG = '{"dependencies": {"react": "^18.2.0", "lodash": "4.17.21"}}\n'
LOCK_V3 = json.dumps({
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "app"},
        "node_modules/react": {"version": "18.3.1"},
        "node_modules/@scope/thing": {"version": "1.2.3"},
        "node_modules/linked": {"resolved": "link:../linked", "link": True},
    },
})
PKG_V1 = '{"dependencies": {"express": "^4.19.2"}}\n'
LOCK_V1 = json.dumps({
    "lockfileVersion": 1,
    "dependencies": {
        "express": {"version": "4.19.2"},
        "nested-parent": {"version": "1.0.0",
                          "dependencies": {"react": {"version": "17.0.2"}}},
    },
})

with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "package.json").write_text(PKG, encoding="utf-8")
    (Path(tmp) / "package-lock.json").write_text(LOCK_V3, encoding="utf-8")
    v1dir = Path(tmp) / "v1proj"
    v1dir.mkdir()
    (v1dir / "package.json").write_text(PKG_V1, encoding="utf-8")
    (v1dir / "package-lock.json").write_text(LOCK_V1, encoding="utf-8")

    scan_on = scan_folder(tmp, resolve_transitive=True)
    scan_off = scan_folder(tmp)

    # flag on: lockfile deps merged with kind npm-lock
    lock_entries = [d for d in scan_on["node"] if len(d) > 3]
    assert [(d[0], d[1]) for d in lock_entries] == [
        ("react", "18.3.1"), ("@scope/thing", "1.2.3"),
        ("express", "4.19.2"), ("nested-parent", "1.0.0"),
        ("react", "17.0.2"),
    ], lock_entries
    assert all(d[3] == "npm-lock" for d in lock_entries), lock_entries
    assert all(d[2].endswith("package-lock.json") for d in lock_entries), lock_entries
    # v1 lockfile's nested install recursed; root/linked skipped
    assert ("react", "17.0.2") in [(d[0], d[1]) for d in lock_entries], lock_entries
    assert all(d[0] != "linked" for d in lock_entries), lock_entries
    assert sum(1 for f in scan_on["files"] if f.endswith("package-lock.json")) == 2
    assert scan_on["transitive_unavailable"] == []
    print("OK scan merge: v3 + v1 lockfiles beside package.json merged as npm-lock")

    # flag off: byte-identical direct-only scan — no 4-tuples, no lock files
    assert all(len(d) == 3 for d in scan_off["node"]), scan_off["node"]
    assert not any(f.endswith("package-lock.json") for f in scan_off["files"])
    assert scan_off["transitive_unavailable"] == []
    print("OK scan merge: flag off keeps the direct-deps-only scan unchanged")

cfg_off = generate_config(scan_off, "lockdemo")
kinds_off = {r["kind"] for r in cfg_off.get("_discovered_dependencies", [])}
assert not kinds_off & {"transitive-maven", "transitive-gradle", "npm-lock"}, kinds_off
cfg_on = generate_config(scan_on, "lockdemo")
rows_off = sorted(p["label"] for p in cfg_off["products"] if not p.get("_section"))
rows_on = sorted(p["label"] for p in cfg_on["products"] if not p.get("_section"))
assert rows_off == ["Express 4", "React 18"], rows_off
# nested react 17.0.2 is a distinct dedupe key -> its own promoted row
assert rows_on == ["Express 4", "React 17", "React 18"], rows_on
print("OK regression: flag-off products contain no transitive-derived rows")


# --- 3. CLI flag end-to-end (mvn/gradle unavailable; no tool can be invoked) ----

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


class _FakeShutil:
    @staticmethod
    def which(tool):
        return None


class _FakeSubprocess:
    @staticmethod
    def run(*args, **kwargs):
        raise AssertionError(
            "subprocess.run must never be called when the tool is unavailable")


_real_shutil, _real_subprocess = gc.shutil, gc.subprocess
gc.shutil, gc.subprocess = _FakeShutil(), _FakeSubprocess()

with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "pom.xml").write_text(MINIMAL_POM, encoding="utf-8")
    (Path(tmp) / "build.gradle").write_text(BUILD_GRADLE, encoding="utf-8")
    (Path(tmp) / "package.json").write_text(PKG_REACT, encoding="utf-8")
    (Path(tmp) / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3,
                    "packages": {"node_modules/react": {"version": "18.3.1"}}}),
        encoding="utf-8")
    out_path = os.path.join(tmp, "out.json")

    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        old_argv = sys.argv
        sys.argv = ["generate_config.py", tmp, "--name", "cli-transitive",
                    "--output", out_path, "--resolve-transitive"]
        try:
            gc.main()
        except SystemExit as exc:
            assert exc.code in (None, 0), exc.code
        finally:
            sys.argv = old_argv

    stdout, stderr = buf_out.getvalue(), buf_err.getvalue()
    assert "Resolving transitive deps via mvn for pom.xml..." in stdout, stdout
    assert "Resolving transitive deps via gradle" in stdout, stdout
    assert "Parsing npm lockfile package-lock.json..." in stdout, stdout
    assert "! skipping transitive resolution for pom.xml:" in stderr, stderr
    assert "! skipping transitive resolution for build.gradle:" in stderr, stderr

    with open(out_path, encoding="utf-8") as f:
        cfg = json.load(f)
    outcomes = [r["outcome"] for r in cfg["_discovered_dependencies"]]
    assert outcomes.count("skipped: transitive resolution unavailable "
                          "(mvn not on PATH or failed)") == 1, outcomes
    assert outcomes.count("skipped: transitive resolution unavailable "
                          "(gradle not on PATH or failed)") == 1, outcomes
    rows = [p["label"] for p in cfg["products"] if not p.get("_section")]
    assert rows == ["lib 1.0.0", "React 18"], rows
    print("OK CLI: --resolve-transitive completes exit 0 with mvn/gradle absent;")
    print("OK CLI: unavailable outcomes recorded, no subprocess was ever called")

    # flag-off CLI over the same fixture: no transitive activity at all
    out_off = os.path.join(tmp, "out-off.json")
    buf_out2, buf_err2 = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out2), contextlib.redirect_stderr(buf_err2):
        sys.argv = ["generate_config.py", tmp, "--name", "cli-transitive",
                    "--output", out_off]
        try:
            gc.main()
        except SystemExit as exc:
            assert exc.code in (None, 0), exc.code
        finally:
            sys.argv = old_argv
    stdout2 = buf_out2.getvalue()
    assert "Resolving transitive" not in stdout2, stdout2
    assert "Transitive resolution" not in stdout2, stdout2
    with open(out_off, encoding="utf-8") as f:
        cfg_off = json.load(f)
    off_outcomes = [r["outcome"] for r in cfg_off["_discovered_dependencies"]]
    assert not [o for o in off_outcomes
                if o.startswith("skipped: transitive resolution unavailable")]
    assert not [o for o in off_outcomes if o.startswith("unmapped-transitive")]
    off_kinds = {r["kind"] for r in cfg_off["_discovered_dependencies"]}
    assert not off_kinds & {"transitive-maven", "transitive-gradle", "npm-lock"}
    rows_off = [p["label"] for p in cfg_off["products"] if not p.get("_section")]
    assert rows_off == rows, (rows, rows_off)
    print("OK regression: flag-off CLI output matches the flag-on direct rows")
    print("OK regression: no transitive kinds appear with the flag off")

gc.shutil, gc.subprocess = _real_shutil, _real_subprocess

print("OK test_generate_transitive_merge")
