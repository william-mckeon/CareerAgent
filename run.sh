#!/usr/bin/env bash
#
# run.sh — bring the whole CareerAgent stack up (or down) via Docker.
#
# careeragent-os pins versions and documents the order; this script automates the
# manual, directory-by-directory bring-up from the README runbook. Each service
# is its own compose stack on the shared external `careeragent-network` (owned by
# careeragent-logger). Order matters: dependencies come up first.
#
#   logger (+ shared Postgres)  →  infra  →  memory (optional)  →  api  →  frontend
#
# Usage:
#   ./run.sh                 # build + start the whole stack (memory included if it has a .env)
#   ./run.sh --no-memory     # start without the optional RAG layer
#   ./run.sh --no-build      # start without rebuilding images
#   ./run.sh --down          # stop & remove all service containers (keeps volumes)
#   ./run.sh --down --volumes  # ...and remove named volumes too (DROPS the databases)
#
# Notes:
#   - Each service reads its OWN .env. This script does not create secrets; it
#     warns if a service's .env is missing. Shared secrets must match across
#     boundaries (see the umbrella README "Secrets must match" note).
#   - Health is probed by polling the host port until the service answers HTTP
#     at all (200, or 401 for the auth-gated logger/api health) — i.e. "is it
#     listening?", not "is the model warm?" (cold starts are absorbed at call
#     time, per the README).

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NETWORK="careeragent-network"

# --- arg parsing -----------------------------------------------------------
DO_DOWN=0
WITH_MEMORY=1
BUILD=1
REMOVE_VOLUMES=0
for arg in "$@"; do
  case "$arg" in
    --down)      DO_DOWN=1 ;;
    --no-memory) WITH_MEMORY=0 ;;
    --no-build)  BUILD=0 ;;
    --volumes|-v) REMOVE_VOLUMES=1 ;;
    -h|--help)
      grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --- prerequisites ---------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not on PATH." >&2
  exit 127
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: 'docker compose' (v2) is required." >&2
  exit 127
fi

# Curl is used for health polling; fall back to a TCP check if absent.
have_curl=0
command -v curl >/dev/null 2>&1 && have_curl=1

# --- helpers ---------------------------------------------------------------

# wait_for_http <name> <url> <timeout_seconds>
# Succeeds as soon as the URL returns ANY HTTP status code (service is up).
wait_for_http() {
  local name="$1" url="$2" timeout="${3:-90}" waited=0 code
  printf "   waiting for %s (%s) " "$name" "$url"
  while [ "$waited" -lt "$timeout" ]; do
    if [ "$have_curl" -eq 1 ]; then
      code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$url" 2>/dev/null)"
      # Any non-000 code means the service answered (200, 401, 422, ...).
      if [ -n "$code" ] && [ "$code" != "000" ]; then
        echo " up (HTTP $code)"
        return 0
      fi
    fi
    printf "."
    sleep 2
    waited=$((waited + 2))
  done
  echo " TIMEOUT after ${timeout}s"
  return 1
}

# compose_in <dir> <args...>
compose_in() {
  local dir="$1"; shift
  ( cd "$ROOT/$dir" && docker compose "$@" )
}

up_args=( up -d )
[ "$BUILD" -eq 1 ] && up_args+=( --build )

# --- teardown path ---------------------------------------------------------
if [ "$DO_DOWN" -eq 1 ]; then
  down_args=( down )
  [ "$REMOVE_VOLUMES" -eq 1 ] && down_args+=( --volumes )
  echo "Tearing down the stack (reverse order)..."
  # Reverse of bring-up order.
  for svc in CareerAgent-frontend CareerAgent-api careeragent-memory CareerAgent-infra CareerAgent-logger; do
    if [ -f "$ROOT/$svc/docker-compose.yml" ]; then
      echo "--- $svc ---"
      compose_in "$svc" "${down_args[@]}" || true
    fi
  done
  echo "Done. (Network '$NETWORK' is left in place; remove with: docker network rm $NETWORK)"
  exit 0
fi

# --- preflight: .env presence ---------------------------------------------
echo "Preflight: checking each service for a .env ..."
missing_required=0
for svc in CareerAgent-logger CareerAgent-infra CareerAgent-api CareerAgent-frontend; do
  if [ ! -f "$ROOT/$svc/.env" ]; then
    echo "  ! $svc/.env is MISSING — copy $svc/.env.example to $svc/.env and fill it in."
    missing_required=1
  fi
done
if [ "$missing_required" -eq 1 ]; then
  echo "ERROR: one or more required services have no .env. See the README 'Secrets must match' note." >&2
  exit 1
fi

# Memory is optional; auto-skip if it has no .env.
if [ "$WITH_MEMORY" -eq 1 ] && [ ! -f "$ROOT/careeragent-memory/.env" ]; then
  echo "  ! careeragent-memory/.env is MISSING — running WITHOUT memory."
  echo "    (To enable it: copy careeragent-memory/.env.example to .env, fill it in, then rerun.)"
  WITH_MEMORY=0
fi

# --- 1. shared network -----------------------------------------------------
echo ""
echo "1/6  Shared network '$NETWORK'"
if docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "   already exists"
elif [ -f "$ROOT/CareerAgent-logger/scripts/setup-network.sh" ]; then
  bash "$ROOT/CareerAgent-logger/scripts/setup-network.sh"
else
  docker network create "$NETWORK"
fi

# --- 2. logger (+ shared Postgres) ----------------------------------------
echo ""
echo "2/6  careeragent-logger (+ shared Postgres) :8003"
compose_in CareerAgent-logger "${up_args[@]}" || { echo "logger failed to start" >&2; exit 1; }
# /health is auth-gated when LOGGER_API_KEY is set, so 401 still means "up".
wait_for_http "careeragent-logger" "http://localhost:8003/health" 120 || true

# --- 3. infra --------------------------------------------------------------
echo ""
echo "3/6  careeragent-infra :8002"
compose_in CareerAgent-infra "${up_args[@]}" || { echo "infra failed to start" >&2; exit 1; }
wait_for_http "careeragent-infra" "http://localhost:8002/health" 90 || true

# --- 4. memory (optional) --------------------------------------------------
echo ""
if [ "$WITH_MEMORY" -eq 1 ]; then
  echo "4/6  careeragent-memory :8004 (optional)"
  compose_in careeragent-memory "${up_args[@]}" || { echo "memory failed to start" >&2; exit 1; }
  wait_for_http "careeragent-memory" "http://localhost:8004/health" 90 || true
else
  echo "4/6  careeragent-memory — SKIPPED"
fi

# --- 5. api ----------------------------------------------------------------
echo ""
echo "5/6  careeragent-api :8001"
compose_in CareerAgent-api "${up_args[@]}" || { echo "api failed to start" >&2; exit 1; }
wait_for_http "careeragent-api" "http://localhost:8001/health" 90 || true

# --- 6. frontend -----------------------------------------------------------
echo ""
echo "6/6  careeragent-frontend :8000"
compose_in CareerAgent-frontend "${up_args[@]}" || { echo "frontend failed to start" >&2; exit 1; }
wait_for_http "careeragent-frontend" "http://localhost:8000/_stcore/health" 90 || true

# --- summary ---------------------------------------------------------------
echo ""
echo "============================================================"
echo " Stack is up. Open the UI:  http://localhost:8000"
echo "------------------------------------------------------------"
echo "  frontend  http://localhost:8000"
echo "  api       http://localhost:8001"
echo "  infra     http://localhost:8002"
echo "  logger    http://localhost:8003"
[ "$WITH_MEMORY" -eq 1 ] && echo "  memory    http://localhost:8004" || echo "  memory    (disabled)"
echo ""
echo "  logs:   docker compose -f <service>/docker-compose.yml logs -f"
echo "  down:   ./run.sh --down            (add --volumes to drop the databases)"
echo "============================================================"
