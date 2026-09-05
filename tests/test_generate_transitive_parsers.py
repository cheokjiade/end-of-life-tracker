"""Network-free transitive-resolution parser tests (no build tools invoked).

Covers the two pure parsers behind --resolve-transitive: mvn
dependency:list output and gradle eolDumpDeps init-script output. mvn and
gradle are NOT installed in the test environment and are never invoked —
only the output parsers run, against synthetic text.

The npm package-lock.json blocks that used to live here moved to
tests/test_inventory_node.py (consolidation Task 5) as
lock_graph_records tests; the file itself is deleted in the retirement task.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_config import (
    parse_gradle_dump,
    parse_mvn_dependency_list,
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


print("OK test_generate_transitive_parsers")
