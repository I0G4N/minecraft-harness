#!/bin/bash
# Start one fresh 8-agent paper-aligned round from a generated deployment.
#
# Usage: scripts/start_8agent_round.sh <deployment-dir> <run-dir> <log-file>
#
# The deployment directory contains compose.yaml, config.yaml and env produced
# by prepare_deployment.py.  This script generates a brand-new world, starts the
# detached Supervisor, and waits until the run reports status=running.  It does
# not delete a non-empty run directory and refuses to touch non-/ssd_data paths.
set -euo pipefail

DEPLOY=$(cd "${1:?deployment dir}" && pwd)
RUN=${2:?run dir}
LOG=${3:?log file}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
COMPOSE="$DEPLOY/compose.yaml"
CONFIG="$DEPLOY/config.yaml"
ENV_FILE="$DEPLOY/env"

fail() { echo "start_8agent_round: $*" >&2; exit 2; }
[ -f "$COMPOSE" ] || fail "missing $COMPOSE"
[ -f "$CONFIG" ] || fail "missing $CONFIG"
[ -f "$ENV_FILE" ] || fail "missing $ENV_FILE"
[ -f "$ROOT/run-paper.sh" ] || fail "missing $ROOT/run-paper.sh"

PROJECT=$(python3 - "$COMPOSE" <<'PY'
import sys, yaml
print(yaml.safe_load(open(sys.argv[1]))["name"])
PY
)
SERVER_DATA=$(awk -F= '$1=="SERVER_DATA_DIR"{sub(/^SERVER_DATA_DIR=/,""); print; exit}' "$ENV_FILE")
[ -n "$SERVER_DATA" ] || fail "SERVER_DATA_DIR missing from $ENV_FILE"
case "$SERVER_DATA" in
  /ssd_data/caizihao/*) ;;
  *) fail "refusing non-/ssd_data server data path: $SERVER_DATA" ;;
esac
[ -d "$SERVER_DATA" ] || fail "server data directory missing: $SERVER_DATA"
if [ -e "$RUN" ] && [ -n "$(find "$RUN" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
  fail "run dir already non-empty: $RUN"
fi
active=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null || true)
[ -z "$active" ] || fail "project $PROJECT already has running containers"
# Clear stopped leftovers from an interrupted previous attempt; names/networks
# are project-scoped and no live container for this project exists.
docker compose -p "$PROJECT" -f "$COMPOSE" --env-file "$ENV_FILE" down --remove-orphans >/dev/null 2>&1 || true

echo "ROUND project=$PROJECT server_data=$SERVER_DATA run=$RUN"
rm -rf "$SERVER_DATA/world" "$SERVER_DATA/logs" "$SERVER_DATA/.harness-world.json" \
       "$SERVER_DATA/usercache.json" "$SERVER_DATA/server.properties" "$SERVER_DATA/spigot.yml"
mkdir -p "$SERVER_DATA/logs"
docker compose -p "$PROJECT" -f "$COMPOSE" --env-file "$ENV_FILE" up -d server >/tmp/${PROJECT}-worldgen.log 2>&1
ready=0
for _ in $(seq 1 150); do
  if [ -s "$SERVER_DATA/world/level.dat" ] && grep -q 'Done (' "$SERVER_DATA/logs/latest.log" 2>/dev/null; then ready=1; break; fi
  if ! docker inspect -f '{{.State.Running}}' "${PROJECT}-server" 2>/dev/null | grep -q true; then
    docker logs --tail 50 "${PROJECT}-server" 2>&1 || true
    fail "server exited during fresh world generation"
  fi
  sleep 2
done
[ "$ready" = 1 ] || fail "fresh world generation timed out"
docker compose -p "$PROJECT" -f "$COMPOSE" --env-file "$ENV_FILE" down --remove-orphans >/dev/null 2>&1 || true
rm -rf "$SERVER_DATA/logs" && mkdir -p "$SERVER_DATA/logs"
rm -f "$SERVER_DATA/.harness-world.json"
echo "WORLD_READY $SERVER_DATA/world/level.dat"

export PAPER_ENV_FILE="$ENV_FILE"
export PAPER_CONFIG="$CONFIG"
export PAPER_COMPOSE_FILE="$COMPOSE"
export PAPER_RUN_DIR="$RUN"
export PAPER_LOG="$LOG"
export PAPER_SUPERVISOR_CONTAINER=1
"$ROOT/run-paper.sh" --prepare
"$ROOT/run-paper.sh" --start
name="${PROJECT}-supervisor"
for _ in $(seq 1 60); do
  state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || true)
  [ "$state" = running ] || fail "supervisor container not running ($state)"
  status=$(python3 - "$RUN/events.sqlite3" <<'PY'
import json, sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    row = con.execute("select value_json from state where key='status'").fetchone()
    print(json.loads(row[0]) if row else "")
except Exception:
    print("")
PY
)
  [ "$status" = running ] && { echo "ROUND_RUNNING project=$PROJECT run=$RUN"; exit 0; }
  sleep 2
done
fail "run did not reach status=running"
