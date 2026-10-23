#!/bin/bash
# Detached-container supervisor entrypoint.
#
# The host wrapper starts the Minecraft/bot stack and then starts this script
# inside a *Docker* container with the Docker socket and same-absolute-path
# mounts.  Docker owns the long-lived process, so a DSH/SSH client disconnect
# cannot SIGTERM the Supervisor along with the agent process group.
set -uo pipefail

CONFIG=${1:?config path}
RUN_DIR=${2:?run directory}
COMPOSE_FILE=${3:?compose file}
PROJECT=${4:?compose project}
ENV_FILE=${5:-}
LOG=${6:?log path}
DEST=${7:?deployment dir}
H=${8:?harness dir}
PYTHON=${9:-python3}

mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1

cleanup() {
  rc=$?
  echo "SUPERVISOR_CONTAINER_EXIT $(date -u +%FT%TZ) rc=$rc"
  if docker inspect -f '{{.State.Running}}' "${PROJECT}-server" 2>/dev/null | grep -q true; then
    docker exec "${PROJECT}-server" harness-control save >/dev/null 2>&1 || true
    docker stop --time 45 "${PROJECT}-server" >/dev/null 2>&1 || true
  fi
  if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down --remove-orphans >/dev/null 2>&1 || true
  else
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

cd "$DEST"
echo "START $(date -u +%FT%TZ) run_dir=$RUN_DIR"
export PYTHONPATH="${H}/src:${H}/.venv/lib/python3.11/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
"$PYTHON" -m minecraft_harness.cli run --config "$CONFIG" --run-dir "$RUN_DIR"
