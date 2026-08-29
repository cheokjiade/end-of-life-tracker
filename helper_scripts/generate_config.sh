#!/usr/bin/env bash
#
# generate_config.sh — scan a project folder and generate an EOL tracker
# config plus a Markdown inventory report.
#
# Works on macOS and Linux (and on Windows via Git Bash / WSL). For native
# Windows PowerShell, use generate_config.ps1 instead.
#
# Usage:
#   ./helper_scripts/generate_config.sh                    # interactive wizard
#   ./helper_scripts/generate_config.sh <dir> --name demo  # forwarded to the CLI
#   ./helper_scripts/generate_config.sh <dir> --output FILE --replace --strict
#
set -euo pipefail

# --- locate this script and the repo root ---------------------------------
# The wrappers live in helper_scripts/, one level below the repo root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# The directory the user invoked us from is the wizard's default scan target;
# capture it before moving to the repo root.
INVOKING_DIR="$(pwd)"

# Always operate from the repo root, so relative output paths resolve the
# same way they do for the Python CLIs.
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

# --- non-interactive: forward all arguments -------------------------------
if [ "$#" -gt 0 ]; then
  exec "$PYTHON" "$SCRIPT_DIR/generate_config.py" "$@"
fi

# --- interactive wizard ---------------------------------------------------
# 1) project directory to scan (default: the invoking directory)
printf 'Project directory to scan [%s]: ' "$INVOKING_DIR"
read -r scan_dir
scan_dir="${scan_dir:-$INVOKING_DIR}"
if [ ! -d "$scan_dir" ]; then
  echo "Error: not a directory: '$scan_dir'." >&2
  exit 1
fi

# 2) suggest a slug and an output file name (default: eol_config.<slug>.json)
base="${scan_dir%/}"
base="${base##*/}"
base="${base##*\\}"
base="${base:-project}"
slug="$(printf '%s' "$base" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')"
printf 'Output file [eol_config.%s.json]: ' "$slug"
read -r output
output="${output:-eol_config.${slug}.json}"

# 3) preserve curation by default when an existing config is selected
# ("set --" accumulates the extra generator arguments; expanding an empty
# array breaks under `set -u` on macOS bash 3.2, positional parameters do not)
set --
if [ -e "$output" ]; then
  printf '"%s" already exists. [U]pdate (recommended), [r]eplace, or [c]ancel: ' "$output"
  read -r answer
  case "$answer" in
    "" | u | U | update)
      set -- "$@" --update
      ;;
    r | R | replace)
      set -- "$@" --replace
      ;;
    *)
      echo "Aborted; nothing written." >&2
      exit 1
      ;;
  esac
fi

# 4) show what file types will be scanned
echo "Scanning Java, Node, Python, Go, .NET, Dockerfile, and GitLab CI manifests"
echo "(plus .eolignore and --exclude patterns; node_modules, .venv, target, dist, build, ... are skipped)"

# 5) generate the config (the generator prints the mapped/unmapped/warning
# counts); with set -e its exit code ends this script, so failures are not
# swallowed and the report step only runs on success
"$PYTHON" "$SCRIPT_DIR/generate_config.py" "$scan_dir" --name "$slug" --output "$output" "$@"

# 6) regenerate the Markdown inventory from the just-written config (--force:
# a stale report from a previous run must not survive)
"$PYTHON" "$SCRIPT_DIR/generate_inventory_report.py" "$output" --force

# 7) the exact command for the live tracker smoke run
echo ""
echo "Next: review the config, then run the live tracker:"
echo "  $PYTHON lambda_function.py $output"
echo "Or use ./run.sh (macOS/Linux/Git Bash) or .\\run.ps1 (PowerShell) to pick a config interactively."
