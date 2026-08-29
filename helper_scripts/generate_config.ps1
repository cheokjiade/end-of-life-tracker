<#
.SYNOPSIS
    Scan a project folder and generate an EOL tracker config plus an
    inventory report (Windows).

.DESCRIPTION
    Native Windows PowerShell wrapper around helper_scripts\generate_config.py
    and helper_scripts\generate_inventory_report.py. On macOS / Linux (or
    Git Bash / WSL) use helper_scripts\generate_config.sh instead.

    With no arguments an interactive wizard asks for the project directory,
    suggests an output file, offers a curation-preserving update when that
    file exists, then generates the config and all inventory formats.

.EXAMPLE
    .\helper_scripts\generate_config.ps1
    Runs the interactive wizard.

.EXAMPLE
    .\helper_scripts\generate_config.ps1 C:\code\my-project --name my-project
    Non-interactive: all arguments are forwarded to generate_config.py.

.EXAMPLE
    .\helper_scripts\generate_config.ps1 C:\code\my-project --replace --strict
    Explicitly replaces any existing output and fails on scan warnings (for CI).
#>
param()

$ErrorActionPreference = 'Stop'

# Remember where the user invoked the script: it is the wizard's default
# scan directory once we move to the repo root.
$invokingDir = (Get-Location).Path

# Always operate from the repo root (the parent of the directory this script
# lives in), so relative output paths resolve like the Python CLIs do.
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $repoRoot

# --- pick a Python interpreter -------------------------------------------
# Relax ErrorActionPreference around native invocations so normal stderr
# output is not promoted to a terminating error under stream redirection
# (same reasoning as run.ps1 around its tracker invocation).
$ErrorActionPreference = 'Continue'

# Different platforms expose the interpreter under different names. Some
# Windows installs only offer broken "App execution alias" stubs under one
# of these names, so each candidate is version-validated before it is
# accepted; the first Python 3.9+ candidate in run.ps1's order wins.
$python = $null
$brokenVersion = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $cmd.Source; break }
    if (-not $brokenVersion) {
        $brokenVersion = (& $candidate --version) 2>&1
    }
}
if (-not $python) {
    if ($brokenVersion) {
        Write-Error "Python 3.9+ is required but found: $brokenVersion"
    }
    else {
        Write-Error "Python 3.9+ is required but none of 'python', 'python3', or 'py' were found on PATH."
        Write-Host ""
        Write-Host "To fix:"
        Write-Host "  1. Install Python 3.9+ from https://www.python.org/downloads/"
        Write-Host '     (on Windows: run the installer and tick "Add python.exe to PATH").'
        Write-Host "  2. Open a NEW terminal so the updated PATH is picked up."
        Write-Host "  3. Re-run this script."
    }
    exit 1
}

# --- forward-all mode ------------------------------------------------------
if ($args.Count -gt 0) {
    & $python (Join-Path $PSScriptRoot 'generate_config.py') @args
    exit $LASTEXITCODE
}

# --- interactive wizard ----------------------------------------------------
# 1) project directory to scan (default: the invoking directory)
$scanDir = Read-Host "Project directory to scan [$invokingDir]"
if (-not $scanDir) { $scanDir = $invokingDir }
if (-not (Test-Path -LiteralPath $scanDir -PathType Container)) {
    Write-Error "Not a directory: '$scanDir'"
    exit 1
}

# 2) suggest a slug and an output file name (default: eol_config.<slug>.json)
$slug = (Split-Path -Leaf $scanDir) -replace ' ', '-'
$slug = $slug.ToLowerInvariant()
if (-not $slug) { $slug = 'project' }
$defaultOutput = "eol_config.$slug.json"
$output = Read-Host "Output file [$defaultOutput]"
if (-not $output) { $output = $defaultOutput }

# 3) preserve curation by default when an existing config is selected
$existingMode = $null
if (Test-Path -LiteralPath $output) {
    $answer = Read-Host ('"' + $output + '" already exists. [U]pdate (recommended), [r]eplace, or [c]ancel')
    switch ($answer.ToLowerInvariant()) {
        { $_ -in @('', 'u', 'update') } { $existingMode = '--update'; break }
        { $_ -in @('r', 'replace') } { $existingMode = '--replace'; break }
        default {
            Write-Host "Aborted; nothing written."
            exit 1
        }
    }
}

# 4) show what file types will be scanned
Write-Host "Scanning Java, Node, Python, Go, .NET, Dockerfile, and GitLab CI manifests"
Write-Host "(plus .eolignore and --exclude patterns; node_modules, .venv, target, dist, build, ... are skipped)"

# 5) generate the config (the generator prints the mapped/unmapped/warning
# counts); a non-zero exit stops here so the report step only runs on success
$genArgs = @((Join-Path $PSScriptRoot 'generate_config.py'), $scanDir, '--name', $slug, '--output', $output)
if ($existingMode) { $genArgs += $existingMode }
& $python @genArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 6) regenerate all inventory formats from the just-written config (--force:
# stale reports from a previous run must not survive)
& $python (Join-Path $PSScriptRoot 'generate_inventory_report.py') $output --force
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 7) the exact command for the live tracker smoke run
Write-Host ""
Write-Host "Next: review the config, then run the live tracker:"
Write-Host "  python lambda_function.py $output"
Write-Host "Or use .\run.ps1 (Windows) or ./run.sh (macOS/Linux/Git Bash) to pick a config interactively."
exit $LASTEXITCODE
