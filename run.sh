#!/usr/bin/env bash
#
# run.sh — run the EOL tracker locally against a chosen config file.
#
# Works on macOS and Linux (and on Windows via Git Bash / WSL). For native
# Windows PowerShell, use run.ps1 instead.
#
# Usage:
#   ./run.sh                      # interactive menu of available configs
#   ./run.sh a                  # shorthand -> eol_config.a.json
#   ./run.sh eol_config.a.json  # explicit file name
#   ./run.sh path/to/custom.json  # any path to a config file
#   ./run.sh --list               # list available configs and exit
#
set -euo pipefail

# Always operate from the repo root (the directory this script lives in),
# so the script works no matter where it is invoked from.
cd "$(dirname "$0")"

# --- pick a Python interpreter -------------------------------------------
# Different platforms expose the interpreter under different names.
PYTHON=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "Error: Python 3.9+ is required but none of 'python3', 'python', or 'py'" >&2
  echo "       were found on PATH. Install Python and try again." >&2
  exit 1
fi

# --- collect available configs -------------------------------------------
# Every eol_config.*.json file in the repo root, one per line, sorted.
CONFIGS=()
while IFS= read -r line; do
  CONFIGS+=("$line")
done < <(find . -maxdepth 1 -name 'eol_config.*.json' -type f | sed 's|^\./||' | sort)

# --- --list flag ---------------------------------------------------------
if [ "${1:-}" = "--list" ] || [ "${1:-}" = "-l" ]; then
  if [ "${#CONFIGS[@]}" -eq 0 ]; then
    echo "No eol_config.*.json files found in $(pwd)."
    exit 1
  fi
  printf '%s\n' "${CONFIGS[@]}"
  exit 0
fi

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
  if ! CONFIG="$(resolve_config "$1")"; then
    echo "Error: no config matching '$1'." >&2
    echo "       Tried '$1' and 'eol_config.$1.json'." >&2
    echo "Available configs:" >&2
    printf '  %s\n' "${CONFIGS[@]}" >&2
    exit 1
  fi
else
  CONFIG="$(choose_config)"
fi

echo "Running EOL tracker with config: $CONFIG"
echo "----------------------------------------------------------------------"
exec "$PYTHON" lambda_function.py "$CONFIG"
