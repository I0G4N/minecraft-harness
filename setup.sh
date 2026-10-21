#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ACCEPT_EULA=false
BUILD=false
UP=false
RUN=false
INSTALL=true
GVISOR=false
API_KEY="${DEEPSEEK_API_KEY:-}"
AGENTS=4
WALL=60
CALLS=2000
COST=5.0
NAME="minecraft-harness-local"
FORCE=""
while (($#)); do
  case "$1" in
    --accept-eula) ACCEPT_EULA=true ;;
    --build) BUILD=true ;;
    --up) UP=true ;;
    --run) RUN=true ;;
    --no-install) INSTALL=false ;;
    --install-gvisor) GVISOR=true ;;
    --force) FORCE="--force" ;;
    --api-key) shift; API_KEY="${1:?--api-key requires a value}" ;;
    --agents) shift; AGENTS="${1:?--agents requires a value}" ;;
    --wall-minutes) shift; WALL="${1:?--wall-minutes requires a value}" ;;
    --max-calls) shift; CALLS="${1:?--max-calls requires a value}" ;;
    --max-cost) shift; COST="${1:?--max-cost requires a value}" ;;
    --name) shift; NAME="${1:?--name requires a value}" ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

for tool in docker node uv cc; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 2; }
done

if $INSTALL; then
  if [[ ! -x .venv/bin/python ]]; then
    uv sync --python 3.11 --locked
  fi
  if [[ ! -d bot_runtime/node_modules ]]; then
    npm --prefix bot_runtime ci
  fi
fi
if $GVISOR; then
  "$ROOT/scripts/install_gvisor.sh"
fi
if [[ ! -f infra/.cache/sandbox.so ]]; then
  mkdir -p infra/.cache
  cc -Wall -Wextra -Werror -shared -fPIC -O2 -o infra/.cache/sandbox.so infra/sandbox.c
fi

API_ARGS=()
if [[ -n "$API_KEY" ]]; then API_ARGS=(--api-key "$API_KEY"); fi
EULA_ARGS=()
if $ACCEPT_EULA; then EULA_ARGS=(--accept-eula); fi
.venv/bin/python scripts/configure.py --name "$NAME" --agents "$AGENTS" --wall-minutes "$WALL" \
  --max-calls "$CALLS" --max-cost "$COST" "${API_ARGS[@]}" "${EULA_ARGS[@]}" $FORCE
MODE=strict
if [[ "$AGENTS" == "4" ]]; then MODE=paper; fi
COMPOSE=infra/compose.local.yaml
.venv/bin/python scripts/render_compose.py --agents "$AGENTS" --mode "$MODE" --output "$COMPOSE"

if $BUILD; then
  docker compose -f "$COMPOSE" --env-file infra/.env build
fi
if $UP; then
  $ACCEPT_EULA || { echo "--up requires --accept-eula" >&2; exit 2; }
  docker compose -f "$COMPOSE" --env-file infra/.env up -d
fi
if $RUN; then
  $ACCEPT_EULA || { echo "--run requires --accept-eula" >&2; exit 2; }
  exec ./scripts/run.sh
fi

cat <<EOF

Configured: configs/local.yaml
Environment: infra/.env (mode 600, gitignored)
Next:
  export DEEPSEEK_API_KEY=...       # if not passed to setup
  docker compose -f "$COMPOSE" --env-file infra/.env up -d --build
  ./scripts/run.sh
  .venv/bin/mc-harness serve runs --port 8896
Dashboard: http://127.0.0.1:8896/
EOF
