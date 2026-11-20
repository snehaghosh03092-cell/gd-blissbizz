#!/usr/bin/env bash
# Start every implemented service in one terminal with prefixed logs. Ctrl+C stops them all.
# Gateway (public) on :8000. Internal: identity :8101, store :8102, catalog :8103, supply :8104,
# sales :8105, billing :8106, agent :8107 (text AI uses ANTHROPIC_API_KEY in backend/.env; optional). Port 8000 busy? GATEWAY_PORT=8010 make dev-all
# Shops: http://threads-co.localhost:8000/api/v1/storefront (any <slug>.localhost from the seed).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=backend/.venv/bin/python3
# Local service settings (DATABASE_URL and optional provider keys) live here. Export them so
# every uvicorn child sees the same configuration; production supplies real environment variables.
if [[ -f backend/.env ]]; then
  local_database_override="${DATABASE_URL-}"
  set -a
  # shellcheck disable=SC1091
  source backend/.env
  set +a
  if [[ -n "$local_database_override" ]]; then
    export DATABASE_URL="$local_database_override"
  fi
  unset local_database_override
fi
export INTERNAL_SIGNING_SECRET="${INTERNAL_SIGNING_SECRET:-dev-only-internal-secret-0123456789abcdef}"
export IDENTITY_URL="${IDENTITY_URL:-http://127.0.0.1:8101}"
export CATALOG_URL="${CATALOG_URL:-http://127.0.0.1:8103}"
export SUPPLY_URL="${SUPPLY_URL:-http://127.0.0.1:8104}"
export STORE_URL="${STORE_URL:-http://127.0.0.1:8102}"
export SALES_URL="${SALES_URL:-http://127.0.0.1:8105}"
export BILLING_URL="${BILLING_URL:-http://127.0.0.1:8106}"
export AGENT_URL="${AGENT_URL:-http://127.0.0.1:8107}"
pids=()
cleanup() { trap - INT TERM EXIT; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup INT TERM EXIT

start() {  # name port module
  "$PY" -m uvicorn "$3:app" --host 127.0.0.1 --port "$2" --log-level warning \
    > >(awk -v n="$1" '{print "[" n "] " $0; fflush()}') 2>&1 &
  pids+=($!)
}
wait_for() {
  local pid=${pids[${#pids[@]}-1]}
  for _ in $(seq 1 40); do
    kill -0 "$pid" 2>/dev/null || { echo "Service failed to start at $1" >&2; return 1; }
    if curl -fs "$1" >/dev/null 2>&1 && lsof -a -p "$pid" -iTCP -sTCP:LISTEN -t >/dev/null; then return 0; fi
    sleep 0.5
  done
  echo "timed out waiting for $1" >&2; return 1
}
start identity 8101 identity_service.main; wait_for http://127.0.0.1:8101/health
start store 8102 store_service.main;       wait_for http://127.0.0.1:8102/health
start catalog 8103 catalog_service.main;   wait_for http://127.0.0.1:8103/health
start supply 8104 supply_service.main;     wait_for http://127.0.0.1:8104/health
start sales 8105 sales_service.main;       wait_for http://127.0.0.1:8105/health
start billing 8106 billing_service.main;   wait_for http://127.0.0.1:8106/health
start agent 8107 agent_service.main;       wait_for http://127.0.0.1:8107/health
GW="${GATEWAY_PORT:-8000}"
start gateway "$GW" gateway_service.main; wait_for "http://127.0.0.1:$GW/api/ready"
echo "Running 8 services. Admin API: http://127.0.0.1:$GW/api  Shops: http://<slug>.localhost:$GW/api/v1/storefront"
echo "Example: curl 'http://threads-co.localhost:$GW/api/v1/storefront/products?color=black'   Ctrl+C to stop."
wait
