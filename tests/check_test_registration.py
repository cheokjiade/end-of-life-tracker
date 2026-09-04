"""Guard against unregistered and shadowed standalone tests.

Standalone assertion script (repo convention: no framework, no network).
Every `tests/test_*.py` is parsed with `ast` only — never imported or
executed — and checked for two defect classes that let a regression rot
silently while the suite stays green:

- Shadowed definition: a module-level `def test_*` defined more than once.
  Python keeps only the last definition, so the earlier body (and whatever
  it was regression-testing) silently stops running.
- Unregistered test: in files that assign a module-level `TESTS = [...]`
  list of test-function names and loop over it to run them, a module-level
  `def test_*` that never appears in that list never runs. Files with no
  `TESTS` list are top-level-assert scripts (every statement runs simply by
  importing/executing the module) and are exempt from this rule.

Run from the repository root:  python tests/check_test_registration.py
Self-test (no repository scan):  python tests/check_test_registration.py --self-test
"""

import ast
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"


def _module_level_test_defs(tree):
    """Return [(name, lineno), ...] for every module-level `def test_*`, in
    source order, including duplicates."""
    defs = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            defs.append((node.name, node.lineno))
    return defs


def _tests_list_names(tree):
    """Return the set of names in a module-level `TESTS = [...]` assignment
    (an ast.List of ast.Name elements), or None if there is no such
    assignment."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "TESTS" for t in node.targets):
            continue
        if not isinstance(node.value, ast.List):
            continue
        return {elt.id for elt in node.value.elts if isinstance(elt, ast.Name)}
    return None


def find_violations(source, filename="<module>"):
    """Parse `source` and return a list of violation strings for that one
    module. `filename` is used only for message text."""
    tree = ast.parse(source, filename=filename)
    violations = []

    defs = _module_level_test_defs(tree)
    seen = {}
    for name, lineno in defs:
        if name in seen:
            violations.append(
                f"{filename}: shadowed definition '{name}' at lines {seen[name]} and {lineno}"
            )
        else:
            seen[name] = lineno

    tests_list = _tests_list_names(tree)
    if tests_list is not None:
        for name, lineno in defs:
            if name not in tests_list:
                violations.append(
                    f"{filename}: unregistered '{name}' at line {lineno} (not in TESTS)"
                )

    return violations


def check_file(path):
    source = path.read_text(encoding="utf-8")
    return find_violations(source, filename=path.name)


def check_repository():
    violations = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        violations.extend(check_file(path))
    return violations


def _run_self_test():
    shadowed_src = (
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "def test_a():\n"
        "    assert True\n"
    )
    unregistered_src = (
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
        "\n"
        "TESTS = [test_a]\n"
    )
    clean_src = (
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
        "\n"
        "TESTS = [test_a, test_b]\n"
    )
    clean_no_tests_list_src = (
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        files = {
            "test_shadowed.py": shadowed_src,
            "test_unregistered.py": unregistered_src,
            "test_clean.py": clean_src,
            "test_clean_no_list.py": clean_no_tests_list_src,
        }
        for name, src in files.items():
            (tmp_path / name).write_text(src, encoding="utf-8")

        shadowed_violations = check_file(tmp_path / "test_shadowed.py")
        assert len(shadowed_violations) == 1, shadowed_violations
        assert "shadowed definition 'test_a'" in shadowed_violations[0], shadowed_violations

        unregistered_violations = check_file(tmp_path / "test_unregistered.py")
        assert len(unregistered_violations) == 1, unregistered_violations
        assert "unregistered 'test_b'" in unregistered_violations[0], unregistered_violations

        clean_violations = check_file(tmp_path / "test_clean.py")
        assert clean_violations == [], clean_violations

        clean_no_list_violations = check_file(tmp_path / "test_clean_no_list.py")
        assert clean_no_list_violations == [], clean_no_list_violations

    print("check_test_registration --self-test: OK")


def main():
    if "--self-test" in sys.argv:
        _run_self_test()
        return 0

    violations = check_repository()
    if violations:
        for v in violations:
            print(v)
        print(f"{len(violations)} violation(s)")
        return 1
    print("check_test_registration: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
