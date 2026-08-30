"""Syntax and existence checks for the helper wrapper scripts.

Verifies the four cross-platform launchers under helper_scripts/ exist and
parse cleanly: `bash -n` for the Bash wrappers and the PowerShell parser API
for the PowerShell wrappers. Skips (rather than fails) when no usable bash or
PowerShell host is installed. Standalone assertion script: no pytest, no
network; shells are invoked only for syntax checks. Non-interactive wrapper
integration coverage is a later plan commit.

Run from the repository root:  python tests/test_helper_wrappers.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helper_scripts"

BASH_WRAPPERS = ("generate_config.sh", "generate_inventory_report.sh")
PS_WRAPPERS = ("generate_config.ps1", "generate_inventory_report.ps1")


def _find_bash():
    """Locate a bash that accepts Windows-style paths.

    On Windows, `bash` on PATH is often WSL's system32 bash.exe, which
    mangles `C:\\...` arguments; prefer Git Bash (shipped with Git for
    Windows) when present, then fall back to whatever `bash` resolves to.
    """
    git = shutil.which("git")
    if git:
        git_root = Path(git).resolve().parent.parent
        for rel in ("bin", os.path.join("usr", "bin")):
            candidate = git_root / rel / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash")


def _find_powershell():
    """Locate a PowerShell host: pwsh (7+) first, then Windows PowerShell."""
    return shutil.which("pwsh") or shutil.which("powershell")


def test_wrapper_files_exist():
    for name in BASH_WRAPPERS + PS_WRAPPERS:
        path = HELPERS / name
        assert path.is_file(), f"missing wrapper: {path}"


def test_bash_wrappers_parse():
    bash = _find_bash()
    if bash is None:
        for name in BASH_WRAPPERS:
            print(f"skip  {name}: bash not available")
        return
    for name in BASH_WRAPPERS:
        proc = subprocess.run(
            [bash, "-n", str(HELPERS / name)],
            capture_output=True, text=True)
        assert proc.returncode == 0, (
            f"bash -n {name} failed:\n{proc.stderr.strip()}")


_PS_CHECK = """$toks = $null
$errs = $null
[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$toks, [ref]$errs) | Out-Null
if ($errs.Count -gt 0) {
  foreach ($e in $errs) { Write-Host ('  ' + $e.Message) }
  exit 1
}
exit 0
"""


def test_powershell_wrappers_parse():
    exe = _find_powershell()
    if exe is None:
        for name in PS_WRAPPERS:
            print(f"skip  {name}: neither pwsh nor powershell is available")
        return
    for name in PS_WRAPPERS:
        # Single-quote the path for PowerShell; double embedded quotes.
        quoted = str(HELPERS / name).replace("'", "''")
        command = _PS_CHECK.replace("{path}", quoted)
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True)
        assert proc.returncode == 0, (
            f"PowerShell parse failed for {name}:\n"
            f"{proc.stdout.strip()}\n{proc.stderr.strip()}")


def test_generator_wizards_offer_update_and_explicit_replace():
    bash = (HELPERS / "generate_config.sh").read_text(encoding="utf-8")
    powershell = (HELPERS / "generate_config.ps1").read_text(encoding="utf-8")
    assert 'set -- "$@" --replace' in bash
    assert 'set -- "$@" --update' in bash
    assert "$existingMode = '--replace'" in powershell
    assert "$existingMode = '--update'" in powershell
    assert 'set -- "$@" --force' not in bash
    assert "$genArgs += '--force'" not in powershell


def test_wrapper_documentation_matches_default_report_formats():
    readme = (HELPERS / "README.md").read_text(encoding="utf-8")
    sources = [readme]
    for name in BASH_WRAPPERS + PS_WRAPPERS:
        sources.append((HELPERS / name).read_text(encoding="utf-8"))
    combined = "\n".join(sources)
    assert "Markdown (and optional CSV)" not in combined
    assert "confirmation before overwriting" not in readme
    assert "Markdown, CSV, and HTML" in readme


def test_generator_smoke_commands_are_copy_pasteable():
    bash = (HELPERS / "generate_config.sh").read_text(encoding="utf-8")
    powershell = (HELPERS / "generate_config.ps1").read_text(encoding="utf-8")
    assert "printf '  %q lambda_function.py %q\\n' \"$PYTHON\" \"$output\"" in bash
    assert '$quotedPython = "\'" + $python.Replace("\'", "\'\'") + "\'"' in powershell
    assert '$quotedOutput = "\'" + ([string]$output).Replace("\'", "\'\'") + "\'"' in powershell
    assert 'Write-Host "  & $quotedPython lambda_function.py $quotedOutput"' in powershell
    assert 'Write-Host "  python lambda_function.py $output"' not in powershell


TESTS = [
    test_wrapper_files_exist,
    test_bash_wrappers_parse,
    test_powershell_wrappers_parse,
    test_generator_wizards_offer_update_and_explicit_replace,
    test_wrapper_documentation_matches_default_report_formats,
    test_generator_smoke_commands_are_copy_pasteable,
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
    print("OK test_helper_wrappers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
