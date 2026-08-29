#!/usr/bin/env bash
#
# generate_inventory_report.sh — render a Markdown (and optional CSV)
# inventory report from an EOL tracker config file.
#
# Works on macOS and Linux (and on Windows via Git Bash / WSL). For native
# Windows PowerShell, use generate_inventory_report.ps1 instead.
#
# Usage:
#   ./helper_scripts/generate_inventory_report.sh                    # interactive picker
#   ./helper_scripts/generate_inventory_report.sh a                  # shorthand -> eol_config.a.json
#   ./helper_scripts/generate_inventory_report.sh eol_config.a.json  # explicit file name
#   ./helper_scripts/generate_inventory_report.sh <config> --csv [FILE] --force
#
set -euo pipefail

# --- locate this script and the repo root ---------------------------------
# The wrappers live in helper_scripts/, one level below the repo root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Always operate from the repo root, so relative config names and default
# report paths resolve the same way they do for the Python CLIs.
cd "$REPO_ROOT"

# --- pick a Python interpreter -------------------------------------------
# Different platforms expose the interpreter under different names. Some
# Windows installs only offer broken "App execution alias" stubs under one
# of these names, so each candidate is version-validated before it is
# accepted; the first Python 3.9+ candidate in run.sh's order wins.
PYTHON=""
broken_version=""
for candidate in python3 python py; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
  if [ -z "$broken_version" ]; then
    broken_version="$("$candidate" --version 2>&1 || true)"
  fi
done
if [ -z "$PYTHON" ]; then
  if [ -n "$broken_version" ]; then
    echo "Error: found $broken_version, but Python 3.9+ is required." >&2
    echo "       Install Python 3.9+ and try again." >&2
  else
    echo "Error: Python 3.9+ is required but none of 'python3', 'python', or 'py'" >&2
    echo "       were found on PATH." >&2
    echo "" >&2
    echo "To fix:" >&2
    echo "  1. Install Python 3.9+ from https://www.python.org/downloads/" >&2
    echo '     (on Windows: run the installer and tick "Add python.exe to PATH").' >&2
    echo "  2. Open a NEW terminal so the updated PATH is picked up." >&2
    echo "  3. Re-run this script." >&2
  fi
  exit 1
fi

# --- collect available configs -------------------------------------------
# Every eol_config.*.json file in the repo root, one per line, sorted.
CONFIGS=()
while IFS= read -r line; do
  CONFIGS+=("$line")
done < <(find . -maxdepth 1 -name 'eol_config.*.json' -type f | sed 's|^\./||' | sort)

# --- resolve an argument to a config file --------------------------------
# Accepts: an existing path, or a shorthand ("a" -> eol_config.a.json).
resolve_config() {
  local input="$1"
  if [ -f "$input" ]; then
    printf '%s' "$input"
    return 0
  fi
  if [ -f "eol_config.${input}.json" ]; then
    printf '%s' "eol_config.${input}.json"
    return 0
  fi
  return 1
}

# --- interactive picker --------------------------------------------------
choose_config() {
  if [ "${#CONFIGS[@]}" -eq 0 ]; then
    echo "No eol_config.*.json files found in $(pwd)." >&2
    echo "Run helper_scripts/generate_config.sh first to create one." >&2
    exit 1
  fi
  echo "Available configs:" >&2
  local i=1
  for c in "${CONFIGS[@]}"; do
    echo "  $i) $c" >&2
    i=$((i + 1))
  done
  printf 'Select a config [1-%s]: ' "${#CONFIGS[@]}" >&2
  read -r choice
  case "$choice" in
    '' | *[!0-9]*)
      echo "Invalid selection: '$choice'." >&2
      exit 1
      ;;
  esac
  if [ "$choice" -lt 1 ] || [ "$choice" -gt "${#CONFIGS[@]}" ]; then
    echo "Selection out of range: '$choice'." >&2
    exit 1
  fi
  printf '%s' "${CONFIGS[$((choice - 1))]}"
}

# --- main ----------------------------------------------------------------
if [ "$#" -ge 1 ]; then
  arg="$1"
  shift
  if ! CONFIG="$(resolve_config "$arg")"; then
    echo "Error: no config matching '$arg'." >&2
    echo "       Tried '$arg' and 'eol_config.$arg.json'." >&2
    if [ "${#CONFIGS[@]}" -gt 0 ]; then
      echo "Available configs:" >&2
      printf '  %s\n' "${CONFIGS[@]}" >&2
    else
      echo "Run helper_scripts/generate_config.sh first to create one." >&2
    fi
    exit 1
  fi
else
  CONFIG="$(choose_config)"
fi

echo "Running inventory report for: $CONFIG"
echo "----------------------------------------------------------------------"
exec "$PYTHON" "$SCRIPT_DIR/generate_inventory_report.py" "$CONFIG" "$@"
