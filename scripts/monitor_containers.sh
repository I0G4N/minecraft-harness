#!/bin/bash
# Sample Docker cgroup/CPU metrics for one run until its Supervisor exits.
# Usage: monitor_containers.sh <compose-project> <output.csv> [interval-seconds]
set -euo pipefail
PROJECT=${1:?project}
OUT=${2:?output csv}
INTERVAL=${3:-30}
mkdir -p "$(dirname "$OUT")"
printf 'timestamp,container,cpu_percent,mem_usage,mem_percent,pids\n' > "$OUT"
SUP="${PROJECT}-supervisor"
for _ in $(seq 1 260); do
  state=$(docker inspect -f '{{.State.Status}}' "$SUP" 2>/dev/null || echo missing)
  [ "$state" = running ] || break
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ids=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null || true)
  ids="$ids $(docker ps -q --filter "name=mc-worker-${PROJECT}-" 2>/dev/null || true)"
  # shellcheck disable=SC2086
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.PIDs}}' $ids 2>/dev/null |
    while IFS=, read -r name cpu mem mempct pids; do
      printf '%s,%s,%s,%s,%s,%s\n' "$ts" "$name" "$cpu" "$mem" "$mempct" "$pids"
    done >> "$OUT"
  sleep "$INTERVAL"
done
