#!/usr/bin/env bash
# Turnkey: bring up a self-contained Lakekeeper stack and run generic_tables_lance
# end-to-end against it — in-network, so the vended storage endpoint is reachable
# (no host port juggling, no endpoint rewriting). Needs docker or podman.
#
#   ./examples/run.sh          # run the example (leaves the stack up)
#   ./examples/run.sh --down   # afterwards, tear the stack down
set -euo pipefail

cd "$(dirname "$0")/.."  # -> python/
ENGINE="$(command -v docker || command -v podman)"
COMPOSE=("$ENGINE" compose -p pylk-example -f tests/integration/docker-compose.yaml)

if [[ "${1:-}" == "--down" ]]; then
  "${COMPOSE[@]}" --profile tools down -v --remove-orphans
  exit 0
fi

echo ">> starting stack (Lakekeeper + Postgres + SeaweedFS + Keycloak)…"
"${COMPOSE[@]}" up -d

echo ">> provisioning + running the example in-network…"
"${COMPOSE[@]}" --profile tools run --rm example

echo
echo ">> done. Stack is still up. Tear it down with:  ./examples/run.sh --down"
