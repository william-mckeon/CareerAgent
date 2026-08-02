#!/usr/bin/env bash
#
# run_all_tests.sh — run every service's pytest suite from the umbrella root.
#
# Each service keeps its own tests/ + conftest.py (which fixes sys.path to match
# the Docker PYTHONPATH), so we just invoke pytest from inside each repo. No DB
# or network is required — the suites cover pure logic, auth, and fail-open paths.
#
# Usage:
#   ./run_all_tests.sh            # run all suites
#   ./run_all_tests.sh -v         # pass extra args through to pytest (verbose)
#
# Exit code is 0 only if every suite passes.

set -u

# Resolve the umbrella root (this script's directory) so it works from anywhere.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Service directories, in request-flow order.
SERVICES=(
  "CareerAgent-frontend"
  "CareerAgent-api"
  "CareerAgent-infra"
  "CareerAgent-logger"
  "careeragent-memory"
)

# Pick a Python: prefer python, fall back to python3.
if command -v python >/dev/null 2>&1; then
  PY="python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "ERROR: no python interpreter found on PATH" >&2
  exit 127
fi

# Ensure pytest is importable; if not, install the dev requirements per service.
if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  echo "pytest not found — installing per-service dev requirements as needed..."
fi

PASS=()
FAIL=()
SKIP=()

for svc in "${SERVICES[@]}"; do
  dir="$ROOT/$svc"
  echo ""
  echo "============================================================"
  echo " $svc"
  echo "============================================================"

  if [ ! -d "$dir/tests" ]; then
    echo "  (no tests/ directory — skipping)"
    SKIP+=("$svc")
    continue
  fi

  # Install dev deps if this service ships them and pytest can't be imported.
  if [ -f "$dir/requirements-dev.txt" ] && ! "$PY" -c "import pytest" >/dev/null 2>&1; then
    echo "  installing $svc/requirements-dev.txt ..."
    "$PY" -m pip install -q -r "$dir/requirements-dev.txt"
  fi

  # -p no:cacheprovider avoids a cache-write error under OneDrive-synced paths.
  ( cd "$dir" && "$PY" -m pytest tests/ -p no:cacheprovider "$@" )
  if [ $? -eq 0 ]; then
    PASS+=("$svc")
  else
    FAIL+=("$svc")
  fi
done

echo ""
echo "============================================================"
echo " SUMMARY"
echo "============================================================"
echo "  passed:  ${PASS[*]:-none}"
echo "  failed:  ${FAIL[*]:-none}"
echo "  skipped: ${SKIP[*]:-none}"

# Non-zero exit if anything failed.
[ ${#FAIL[@]} -eq 0 ]
