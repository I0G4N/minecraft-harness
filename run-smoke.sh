#!/usr/bin/env bash
set -uo pipefail
H=/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness
DEST=/ssd_data/caizihao/minecraft/minecraft-harness
RUN_DIR="$DEST/mc-runs/four-agent-smoke-20261022-r7"
LOG="$DEST/mc-logs/four-agent-smoke-20261022-r7.log"
mkdir -p "$RUN_DIR" "$DEST/mc-logs" "$H/infra/server-data-backups"
cd "$H"
{
  echo "START $(date -u +%FT%TZ) run_dir=$RUN_DIR"
  "$H/.venv/bin/python" - <<'PYMARK'
import json,secrets
from pathlib import Path
p=Path('/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness/infra/server-data/.harness-world.json')
p.write_text(json.dumps({'schema_version':1,'world_id':secrets.token_hex(16),'origin':'new_seeded','seed':49},indent=2)+'\n'); p.chmod(0o600)
PYMARK
  PYTHONPATH="$H/src" "$H/.venv/bin/python" -m minecraft_harness.cli run \
    --config configs/minecraft-paper-smoke-20261022.yaml \
    --run-dir "$RUN_DIR"
  RC=$?
  echo "SUPERVISOR_EXIT $(date -u +%FT%TZ) rc=$RC"
  if docker inspect -f '{{.State.Running}}' minecraft-paper-server 2>/dev/null | grep -q true; then
    docker exec minecraft-paper-server harness-control save >/dev/null 2>&1 || true
    docker stop --time 30 minecraft-paper-server >/dev/null 2>&1 || true
  fi
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  ARCHIVE="$H/infra/server-data-backups/world-smoke-$STAMP.tar.gz"
  tar -czf "$ARCHIVE" -C "$H/infra/server-data" world logs 2>/dev/null || true
  echo "WORLD_ARCHIVE $ARCHIVE"
  docker ps --filter name=minecraft-paper -q | xargs -r docker stop --time 20 >/dev/null 2>&1 || true
  echo "DONE $(date -u +%FT%TZ)"
  exit $RC
} >> "$LOG" 2>&1
