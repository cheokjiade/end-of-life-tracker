<#
.SYNOPSIS
    Render a Markdown (and optional CSV) inventory report from an EOL
    tracker config file (Windows).

.DESCRIPTION
    Native Windows PowerShell wrapper around
    helper_scripts\generate_inventory_report.py. On macOS / Linux (or
    Git Bash / WSL) use helper_scripts\generate_inventory_report.sh instead.

    The first argument selects the config: an explicit path or a shorthand
    ("a" -> eol_config.a.json). Remaining arguments are forwarded to the
    Python CLI (--output FILE, --csv [FILE], --force).

.EXAMPLE
    .\helper_scripts\generate_inventory_report.ps1
    Shows an interactive menu of available eol_config.*.json files.

.EXAMPLE
    .\helper_scripts\generate_inventory_report.ps1 a
    Shorthand - reports on eol_config.a.json.

.EXAMPLE
    .\helper_scripts\generate_inventory_report.ps1 eol_config.a.json --csv
    Explicit file name; also writes a CSV next to the Markdown report.
#>
param()

$ErrorActionPreference = 'Stop'

# Always operate from the repo root (the parent of the directory this script
# lives in), so relative config names and default report paths resolve like
# they do for the Python CLIs.
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

# --- collect available configs -------------------------------------------
$configs = @(Get-ChildItem -Path . -Filter 'eol_config.*.json' -File |
    Sort-Object Name |
    Select-Object -ExpandProperty Name)

# --- resolve an argument to a config file --------------------------------
# Accepts: an existing path, or a shorthand ("a" -> eol_config.a.json).
function Resolve-ConfigName([string]$Name) {
    if (Test-Path -LiteralPath $Name -PathType Leaf) { return $Name }
    $short = "eol_config.$Name.json"
    if (Test-Path -LiteralPath $short -PathType Leaf) { return $short }
    return $null
}

# --- main ----------------------------------------------------------------
if ($args.Count -gt 0) {
    $resolved = Resolve-ConfigName $args[0]
    if (-not $resolved) {
        Write-Host "Error: no config matching '$($args[0])'."
        Write-Host "       Tried '$($args[0])' and 'eol_config.$($args[0]).json'."
        if ($configs.Count -gt 0) {
            Write-Host "Available configs:"
            $configs | ForEach-Object { Write-Host "  $_" }
        }
        else {
            Write-Host "Run helper_scripts\generate_config.ps1 first to create one."
        }
        exit 1
    }
    $forward = @($args | Select-Object -Skip 1)
}
else {
    if ($configs.Count -eq 0) {
        Write-Error "No eol_config.*.json files found in $(Get-Location)."
        Write-Host "Run helper_scripts\generate_config.ps1 first to create one."
        exit 1
    }
    Write-Host "Available configs:"
    for ($i = 0; $i -lt $configs.Count; $i++) {
        Write-Host ("  {0}) {1}" -f ($i + 1), $configs[$i])
    }
    $choice = Read-Host ("Select a config [1-{0}]" -f $configs.Count)
    if ($choice -notmatch '^\d+$' -or [int]$choice -lt 1 -or [int]$choice -gt $configs.Count) {
        Write-Error "Invalid selection: '$choice'."
        exit 1
    }
    $resolved = $configs[[int]$choice - 1]
    $forward = @()
}

Write-Host "Running inventory report for: $resolved"
Write-Host "----------------------------------------------------------------------"

# The report CLI writes progress to stdout; relax the preference so normal
# stderr output is never promoted to a terminating error under redirection
# (same reasoning as run.ps1 around its tracker invocation).
$ErrorActionPreference = 'Continue'
& $python (Join-Path $PSScriptRoot 'generate_inventory_report.py') $resolved @forward
exit $LASTEXITCODE
