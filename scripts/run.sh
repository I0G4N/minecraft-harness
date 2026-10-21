#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[[ -f infra/.env ]] || { echo "run ./setup.sh first" >&2; exit 2; }
[[ -x .venv/bin/mc-harness ]] || { echo "run ./setup.sh --no-install or uv sync" >&2; exit 2; }
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  DEEPSEEK_API_KEY="$(sed -n 's/^DEEPSEEK_API_KEY=//p' infra/.env | tail -1)"
fi
export DEEPSEEK_API_KEY
exec .venv/bin/mc-harness run --config configs/local.yaml \
  --run-dir "runs/local-$(date -u +%Y%m%dT%H%M%SZ)" "$@"
