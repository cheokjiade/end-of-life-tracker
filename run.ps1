<#
.SYNOPSIS
    Run the EOL tracker locally against a chosen config file (Windows).

.DESCRIPTION
    Native Windows PowerShell wrapper around `python lambda_function.py <config>`.
    On macOS / Linux (or Git Bash / WSL) use run.sh instead.

.EXAMPLE
    .\run.ps1
    Shows an interactive menu of available eol_config.*.json files.

.EXAMPLE
    .\run.ps1 a
    Shorthand — runs eol_config.a.json.

.EXAMPLE
    .\run.ps1 eol_config.a.json
    Explicit file name (a full path also works).

.EXAMPLE
    .\run.ps1 -List
    Lists available configs and exits.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Config,

    [switch]$List
)

$ErrorActionPreference = 'Stop'

# Always operate from the repo root (the directory this script lives in).
Set-Location -Path $PSScriptRoot

# --- pick a Python interpreter -------------------------------------------
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) {
    Write-Error "Python 3.9+ is required but none of 'python', 'python3', or 'py' were found on PATH. Install Python and try again."
    exit 1
}

# --- collect available configs -------------------------------------------
$configs = @(Get-ChildItem -Path . -Filter 'eol_config.*.json' -File |
    Sort-Object Name |
    Select-Object -ExpandProperty Name)

# --- -List flag ----------------------------------------------------------
if ($List) {
    if ($configs.Count -eq 0) {
        Write-Host "No eol_config.*.json files found in $(Get-Location)."
        exit 1
    }
    $configs | ForEach-Object { Write-Output $_ }
    exit 0
}

# --- resolve an argument to a config file --------------------------------
# Accepts: an existing path, or a shorthand ("a" -> eol_config.a.json).
function Resolve-ConfigName([string]$Name) {
    if (Test-Path -LiteralPath $Name -PathType Leaf) { return $Name }
    $short = "eol_config.$Name.json"
    if (Test-Path -LiteralPath $short -PathType Leaf) { return $short }
    return $null
}

# --- main ----------------------------------------------------------------
if ($Config) {
    $resolved = Resolve-ConfigName $Config
    if (-not $resolved) {
        Write-Host "Error: no config matching '$Config'."
        Write-Host "       Tried '$Config' and 'eol_config.$Config.json'."
        Write-Host "Available configs:"
        $configs | ForEach-Object { Write-Host "  $_" }
        exit 1
    }
}
else {
    if ($configs.Count -eq 0) {
        Write-Error "No eol_config.*.json files found in $(Get-Location)."
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
}

Write-Host "Running EOL tracker with config: $resolved"
Write-Host "----------------------------------------------------------------------"

# The tracker logs to stderr (e.g. "SNS notification skipped ..."). Under
# $ErrorActionPreference='Stop', PowerShell 5.1 would promote that normal
# stderr output to a terminating error if the caller redirects streams
# (e.g. `.\run.ps1 a > log.txt 2>&1`). Relax the preference so Python's
# own exit code — not its stderr — decides success.
$ErrorActionPreference = 'Continue'
& $python lambda_function.py $resolved
exit $LASTEXITCODE
