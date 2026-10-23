#!/usr/bin/env bash
set -uo pipefail
H=/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness
DEST=/ssd_data/caizihao/minecraft/minecraft-harness

# Safe default: prepare only.  Starting a 54-hour paid paper run is always an
# explicit operator action (`--start`).  The paused final11 run keeps its own
# explicit PAPER_CONFIG/PAPER_RUN_DIR environment and is intentionally not
# resumed into a different runner mode in place.
MODE=prepare
for argument in "$@"; do
  case "$argument" in
    --start) MODE=start ;;
    --prepare) MODE=prepare ;;
    -h|--help)
      echo "usage: $0 [--prepare|--start]"
      echo "  --prepare  validate config/world/paths, Pi extensions and the runner image; do not start (default)"
      echo "  --start    explicitly start the configured paper run"
      exit 0
      ;;
    *)
      echo "$0: unknown argument: $argument" >&2
      exit 2
      ;;
  esac
done

RUN_DIR="${PAPER_RUN_DIR:-$DEST/mc-runs/four-agent-paper-container-next}"
LOG="${PAPER_LOG:-$DEST/mc-logs/four-agent-paper-container-next.log}"
mkdir -p "$RUN_DIR" "$DEST/mc-logs" "$H/infra/server-data-backups"
cd "$H"
CONFIG="${PAPER_CONFIG:-configs/minecraft-paper-runner-20261022.yaml}"

# Host wrapper Python.  The pinned uv venv may be readable only by the
# supervisor container user on some deployments; allow an explicit override and
# fall back to the disposable test venv/python3 when needed.
PYTHON_BIN="${HARNESS_PYTHON:-$H/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ] && [ -x "$H/.test-venv/bin/python" ]; then
  PYTHON_BIN="$H/.test-venv/bin/python"
fi
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
[ -n "$PYTHON_BIN" ] || { echo "$0: no usable Python interpreter" >&2; exit 2; }

# Container runner settings for the no-start image preflight probe.  These are
# overwritten from the frozen config by read_runner_settings() during preflight;
# the defaults mirror config.py and never start anything by themselves.
RUNNER_MODE=""
RUNNER_IMAGE="minecraft-harness-workspace:local"
RUNNER_PYTHON="/usr/bin/python3.11"
PI_RUNTIME_ROOT="${HARNESS_PI_RUNTIME_ROOT:-$H/pi_runtime}"

# Pi players need the pinned Pi CLI plus Node >= 22.  Check this during
# prepare-only too: a missing runtime must fail before an operator starts a
# paid experiment.
RUNTIME_KIND=python
PI_CLI="${HARNESS_PI_CLI:-$PI_RUNTIME_ROOT/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js}"
if grep -Eq '^[[:space:]]*runner_runtime:[[:space:]]*pi([[:space:]]|$)' "$CONFIG" 2>/dev/null; then
  RUNTIME_KIND=pi
  if [ ! -f "$PI_CLI" ]; then
    echo "run-paper.sh: Pi runtime selected but CLI is missing: $PI_CLI" >&2
    echo "install it with: npm ci --prefix $H/pi_runtime --ignore-scripts --no-audit --no-fund" >&2
    exit 2
  fi
  if ! command -v node >/dev/null 2>&1; then
    echo "run-paper.sh: Pi runtime selected but node is not on PATH" >&2
    exit 2
  fi
  if ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' >/dev/null 2>&1; then
    echo "run-paper.sh: Pi runtime requires Node >= 22 (found $(node --version 2>/dev/null))" >&2
    exit 2
  fi
fi

# Generated exact-N deployments use a project-specific server container. Derive
# it from the frozen config so the wrapper saves/stops the right server and bot
# stack; the historical default keeps the original minecraft-paper-server name.
SERVER_CONTAINER="${PAPER_SERVER_CONTAINER:-}"
if [ -z "$SERVER_CONTAINER" ] && [ -f "$CONFIG" ]; then
  # Prefer the harness venv, but allow a host python3 with PyYAML when the
  # wrapper itself is inspected outside the launcher container.
  for parser in "$PYTHON_BIN" python3; do
    command -v "$parser" >/dev/null 2>&1 || continue
    parsed="$(PYTHONPATH="$H/src" "$parser" - "$CONFIG" 2>/dev/null <<'PYCONFIG'
import sys, yaml
try:
    data = yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}
    print((data.get('server') or {}).get('managed_container') or '')
except Exception:
    print('')
PYCONFIG
)"
    if [ -n "$parsed" ]; then
      SERVER_CONTAINER="$parsed"
      break
    fi
  done
fi
if [ -z "$SERVER_CONTAINER" ] && [ -n "${PAPER_CONFIG:-}" ]; then
  # Never silently save/stop the static project when the operator explicitly
  # selected a generated config whose server container cannot be read.
  echo "run-paper.sh: cannot read server.managed_container from PAPER_CONFIG=$CONFIG" >&2
  exit 2
fi
SERVER_CONTAINER="${SERVER_CONTAINER:-minecraft-paper-server}"

# Generated exact-N deployments have their own token file. Load it without
# executing shell syntax so JSON values and token characters are preserved; the
# pre-existing supervisor container may otherwise still carry the old project's
# BOT_* tokens in its process environment.
ENV_FILE="${PAPER_ENV_FILE:-}"
if [ -z "$ENV_FILE" ] && [[ "$SERVER_CONTAINER" == *-server ]]; then
  candidate="$H/infra/.env.${SERVER_CONTAINER%-server}"
  [ -f "$candidate" ] && ENV_FILE="$candidate"
fi
if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$key=$value"
  done < "$ENV_FILE"
fi

if [ -n "${PAPER_STACK_FILTER:-}" ]; then
  STACK_SELECTOR=(--filter "name=$PAPER_STACK_FILTER")
elif [[ "$SERVER_CONTAINER" == *-server ]]; then
  STACK_PROJECT="${PAPER_STACK_PROJECT:-${SERVER_CONTAINER%-server}}"
  STACK_SELECTOR=(--filter "label=com.docker.compose.project=$STACK_PROJECT")
else
  STACK_SELECTOR=(--filter "name=$SERVER_CONTAINER")
fi

COMPOSE_FILE="${PAPER_COMPOSE_FILE:-$H/infra/compose.paper.yaml}"
STACK_START_TIMEOUT_S="${PAPER_STACK_START_TIMEOUT_S:-300}"

fail() {
  echo "run-paper.sh: PREFLIGHT FAILED: $*" >&2
  echo "run-paper.sh: nothing was started; resolve the condition and re-run --prepare." >&2
  exit 2
}

resolve_world_path() {
  # Resolve the configured world path with the harness venv, falling back to a
  # host python3 that has PyYAML. Never invent a different path silently.
  local resolved=""
  for parser in "$PYTHON_BIN" python3; do
    command -v "$parser" >/dev/null 2>&1 || continue
    resolved="$(PYTHONPATH="$H/src" "$parser" - "$CONFIG" 2>/dev/null <<'PYWORLD'
import sys
from pathlib import Path
try:
    from minecraft_harness.config import load_config
    print(Path(load_config(sys.argv[1])['server']['world_dir']).resolve())
except Exception:
    pass
PYWORLD
)"
    [ -n "$resolved" ] && break
  done
  printf '%s\n' "${resolved:-$H/infra/server-data/world}"
}

read_runner_settings() {
  # Resolve container runner settings from the frozen config.  The wrapper must
  # not guess these: the no-start image probe needs the actual image/python and
  # whether a Pi runtime is selected.
  local parser resolved=""
  for parser in "$PYTHON_BIN" python3; do
    command -v "$parser" >/dev/null 2>&1 || continue
    resolved="$(PYTHONPATH="$H/src" "$parser" - "$CONFIG" 2>/dev/null <<'PYRUNNER'
import sys
try:
    import yaml
    data = yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}
    runtime = data.get('runtime') or {}
    defaults = {'runner_mode': 'inprocess',
                'runner_container_image': 'minecraft-harness-workspace:local',
                'runner_container_python': '/usr/bin/python3.11'}
    for key, default in defaults.items():
        value = runtime.get(key)
        print(key + '=' + (value if isinstance(value, str) and value else default))
except Exception:
    pass
PYRUNNER
)"
    [ -n "$resolved" ] && break
  done
  while IFS='=' read -r key value; do
    [ -n "$key" ] || continue
    case "$key" in
      runner_mode) RUNNER_MODE="$value" ;;
      runner_container_image) RUNNER_IMAGE="$value" ;;
      runner_container_python) RUNNER_PYTHON="$value" ;;
    esac
  done <<< "$resolved"
}

# >>> run-paper container image probe (unit-testable block) >>>
probe_runner_image() {
  # No-start probe: one disposable `docker run --rm --network none` against the
  # configured runner image.  It verifies the Python imports every container
  # runner needs and, for Pi, Node >= 22 plus the pinned Pi CLI and BOTH
  # extensions as they are bind-mounted into the container.  It never creates or
  # starts the experiment stack, never mounts final11, and never mutates
  # host/Docker state.  Docker unavailability is reported via a nonzero return.
  local image="${RUNNER_IMAGE:-minecraft-harness-workspace:local}"
  local python_bin="${RUNNER_PYTHON:-/usr/bin/python3.11}"
  local site_packages="" candidate
  for candidate in "$H"/.venv/lib/python3.*/site-packages; do
    [ -d "$candidate" ] && site_packages="$candidate" && break
  done
  if ! command -v docker >/dev/null 2>&1; then
    echo "run-paper.sh: WARNING: docker CLI unavailable; skipped container image probe (nothing was started)" >&2
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "run-paper.sh: WARNING: docker daemon unavailable; cannot probe runner image $image (nothing was started)" >&2
    return 1
  fi
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "run-paper.sh: runner image is not available locally: $image" >&2
    return 1
  fi
  if ! command -v timeout >/dev/null 2>&1; then
    echo "run-paper.sh: WARNING: timeout(1) unavailable; cannot bound the container image probe (nothing was started)" >&2
    return 1
  fi
  local mounts=(
    "--mount" "type=bind,src=$H/src,dst=$H/src,readonly"
  )
  if [ "$RUNTIME_KIND" = pi ]; then
    mounts+=("--mount" "type=bind,src=$PI_RUNTIME_ROOT,dst=$PI_RUNTIME_ROOT,readonly")
  fi
  if [ -n "$site_packages" ]; then
    mounts+=("--mount" "type=bind,src=$site_packages,dst=$site_packages,readonly")
  fi
  local probe_script='set -eu
if [ "${PROBE_RUNTIME_KIND:-python}" = "pi" ]; then
  command -v node >/dev/null 2>&1 || { echo "node is missing from the runner image"; exit 1; }
  node_version="$(node --version 2>/dev/null)" || { echo "node --version failed"; exit 1; }
  node_major="${node_version#v}"
  node_major="${node_major%%.*}"
  case "$node_major" in ""|*[!0-9]*) echo "cannot parse node version: $node_version"; exit 1 ;; esac
  if [ "$node_major" -lt 22 ]; then echo "node $node_version is older than 22"; exit 1; fi
  if [ ! -f "$PROBE_PI_CLI" ]; then echo "Pi CLI is missing in the mounted pi_runtime: $PROBE_PI_CLI"; exit 1; fi
  for extension in minecraft-tools.js observer-tools.js; do
    if [ ! -f "$PROBE_PI_RUNTIME/extensions/$extension" ]; then
      echo "Pi extension is missing in the mounted pi_runtime: $PROBE_PI_RUNTIME/extensions/$extension"
      exit 1
    fi
  done
fi
if [ ! -x "$PROBE_PYTHON" ]; then echo "container python is missing or not executable: $PROBE_PYTHON"; exit 1; fi
PYTHONPATH="$PROBE_PYTHONPATH" "$PROBE_PYTHON" -c "import sys, httpx, yaml" || { echo "container python cannot import sys/httpx/yaml"; exit 1; }
echo "ok runtime=${PROBE_RUNTIME_KIND:-python} python=$PROBE_PYTHON node=${node_version:-n/a}"
'
  local output
  if ! output="$(timeout "${PAPER_IMAGE_PROBE_TIMEOUT_S:-120}" docker run --rm --network none \
        --cap-drop=ALL --security-opt=no-new-privileges \
        "${mounts[@]}" \
        -e "PROBE_RUNTIME_KIND=$RUNTIME_KIND" \
        -e "PROBE_PYTHON=$python_bin" \
        -e "PROBE_PYTHONPATH=$H/src${site_packages:+:$site_packages}" \
        -e "PROBE_PI_RUNTIME=$PI_RUNTIME_ROOT" \
        -e "PROBE_PI_CLI=$PI_CLI" \
        --entrypoint /bin/sh "$image" -c "$probe_script" 2>&1)"; then
    echo "run-paper.sh: container image probe failed for $image: ${output:-probe exited nonzero}" >&2
    return 1
  fi
  echo "run-paper.sh: container image probe OK: $output" >&2
  return 0
}
# <<< run-paper container image probe (unit-testable block) <<<

preflight() {
  [ -f "$CONFIG" ] || fail "config not found: $CONFIG"
  command -v docker >/dev/null 2>&1 || fail "docker CLI is required"
  # Report (never mutate) when Docker itself is unavailable: the remaining
  # preflight checks and any --start would otherwise fail midway.
  docker info >/dev/null 2>&1 || fail "docker daemon is unavailable; preflight needs read-only container access (nothing was started)"

  # A new run must use an empty run directory. This prevents silently appending
  # to another run's ledger/checkpoints.
  if [ -d "$RUN_DIR" ] && [ -n "$(find "$RUN_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    fail "run_dir is not empty: $RUN_DIR (choose a new PAPER_RUN_DIR)"
  fi

  # Never fence a live run while preparing/starting another one. final11 is a
  # paused but RUNNING supervisor: starting a second supervisor against the same
  # bot URLs would DELETE its sessions before world admission fails.
  local active
  active="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^(mc-paper-run|minecraft-paper-server|minecraft-paper-(bot|identity|launcher)[0-9]+-|minecraft-paper-rpc-relay)' || true)"
  if [ -n "${STACK_PROJECT:-}" ]; then
    project_active="$(docker ps --filter "label=com.docker.compose.project=$STACK_PROJECT" --format '{{.Names}}' 2>/dev/null || true)"
    if [ -n "$project_active" ]; then
      active="${active}"$'\n'"${project_active}"
    fi
  fi
  active="$(printf '%s\n' "$active" | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ')"
  if [ -n "$active" ]; then
    fail "an experiment stack is still running: $active Archive/stop the paused run first (do not start a second supervisor)."
  fi

  # Reusing compose networks is also unsafe even if containers are stopped.
  local networks
  if [ -n "${STACK_PROJECT:-}" ]; then
    networks="$(docker network ls --format '{{.Name}}' 2>/dev/null | grep -E "^${STACK_PROJECT}_(world|control|worker[0-9]+)$" || true)"
    if [ -n "$networks" ]; then
      fail "compose networks for $STACK_PROJECT still exist: $(printf '%s' "$networks" | tr '\n' ' '). Run docker compose down for the paused run first."
    fi
  fi

  # Port collisions would break the bot/viewer stack even if names differ.
  if command -v ss >/dev/null 2>&1; then
    local busy
    busy="$(ss -ltn 2>/dev/null | awk '{print $4}' | grep -E ':(310[1-8])$' || true)"
    if [ -n "$busy" ]; then
      fail "bot/viewer ports are still in use: $(printf '%s' "$busy" | tr '\n' ' ')"
    fi
  fi

  # If a world already exists, refuse to start a server on a world that is
  # bound to another run or already contains player progress. A truly fresh
  # server-data directory (no level.dat yet) is allowed: --start will boot the
  # server, wait for level.dat and then bind the marker.
  local world marker category
  world="$(resolve_world_path)"
  if [ -f "$world/level.dat" ]; then
    marker="$(dirname "$world")/.harness-world.json"
    if [ -f "$marker" ] && grep -q '"run_id"' "$marker"; then
      fail "world is already bound to another run (marker $marker); provision a fresh server-data/world for this run"
    fi
    for category in playerdata stats advancements; do
      if [ -d "$world/$category" ] && [ -n "$(find "$world/$category" -mindepth 1 -print -quit 2>/dev/null)" ]; then
        fail "world contains existing player progress: $world/$category"
      fi
    done
  fi

  # EULA must be accepted before the server is created/started.
  local eula_world
  eula_world="$(resolve_world_path)"
  if [ -f "$(dirname "$eula_world")/eula.txt" ]; then
    grep -q '^eula=true' "$(dirname "$eula_world")/eula.txt" || fail "EULA is not accepted in $(dirname "$eula_world")/eula.txt"
  elif [ "${ACCEPT_EULA:-}" != "true" ]; then
    fail "EULA is not accepted: set ACCEPT_EULA=true before starting the server"
  fi

  # The model credential must be in this process environment (or generated env
  # file) before the supervisor starts; never write it to the run directory.
  if grep -q 'api_key_env:[[:space:]]*DEEPSEEK_API_KEY' "$CONFIG" 2>/dev/null && [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    fail "DEEPSEEK_API_KEY is unset; export the model key before --start"
  fi
  if [ "$RUNTIME_KIND" = pi ]; then
    local extension
    for extension in minecraft-tools.js observer-tools.js; do
      if [ ! -f "$PI_RUNTIME_ROOT/extensions/$extension" ]; then
        fail "Pi extension is missing: $PI_RUNTIME_ROOT/extensions/$extension (run npm ci --prefix $H/pi_runtime)"
      fi
    done
    if [ ! -f "$PI_RUNTIME_ROOT/prctl_guard.so" ]; then
      fail "Pi prctl guard is missing: $PI_RUNTIME_ROOT/prctl_guard.so (build it from prctl_guard.c)"
    fi
  fi

  # Per-player bot/admin tokens are required before the Supervisor can admit
  # sessions. Check names from the frozen config without printing values.
  local token_names token_name
  token_names="$(PYTHONPATH="$H/src" python3 - "$CONFIG" 2>/dev/null <<'PYTOKENS'
import sys
try:
    import yaml
    data = yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}
    for value in ((data.get('runtime') or {}).get('token_envs') or {}).values():
        if isinstance(value, str) and value:
            print(value)
except Exception:
    pass
PYTOKENS
)"
  while IFS= read -r token_name; do
    [ -n "$token_name" ] || continue
    [ -n "${!token_name:-}" ] || fail "required bot token environment variable is unset: $token_name"
  done <<< "$token_names"

  if [ -f "$(dirname "$world")/eula.txt" ] && ! grep -q '^eula=true' "$(dirname "$world")/eula.txt"; then
    echo "run-paper.sh: WARNING: $(dirname "$world")/eula.txt does not say eula=true" >&2
  fi

  # Final no-start gate: verify the configured runner image can run the Python
  # runner and, for Pi, Node >= 22 with the mounted CLI and BOTH extensions.
  # This must fail before compose_up creates any stack container.
  read_runner_settings
  if [ -z "$RUNNER_MODE" ]; then
    fail "cannot read runtime.runner_mode from $CONFIG for the container image preflight"
  fi
  if [ "$RUNNER_MODE" = container ]; then
    probe_runner_image || fail "container image preflight failed for ${RUNNER_IMAGE:-unknown}; fix the runner image/runtime before --start (nothing was started)"
  else
    echo "run-paper.sh: runner_mode=$RUNNER_MODE; container image probe not required" >&2
  fi
}

preflight_world() {
  local world marker category
  world="$(resolve_world_path)"
  [ -f "$world/level.dat" ] || fail "world level.dat is missing at $world; the server must generate the fresh world before admission"
  marker="$(dirname "$world")/.harness-world.json"
  if [ -f "$marker" ] && grep -q '"run_id"' "$marker"; then
    fail "world is already bound to another run (marker $marker); provision a fresh server-data/world for this run"
  fi
  for category in playerdata stats advancements; do
    if [ -d "$world/$category" ] && [ -n "$(find "$world/$category" -mindepth 1 -print -quit 2>/dev/null)" ]; then
      fail "world contains existing player progress: $world/$category"
    fi
  done
  if [ -f "$(dirname "$world")/eula.txt" ]; then
    grep -q '^eula=true' "$(dirname "$world")/eula.txt" || fail "EULA is not accepted in $(dirname "$world")/eula.txt"
  elif [ "${ACCEPT_EULA:-}" != "true" ]; then
    fail "EULA is not accepted: set ACCEPT_EULA=true before --start"
  fi
}

compose_up() {
  [ -f "$COMPOSE_FILE" ] || fail "compose file not found: $COMPOSE_FILE (set PAPER_COMPOSE_FILE)"
  local args=(-p "$STACK_PROJECT" -f "$COMPOSE_FILE")
  if [ -n "${ENV_FILE:-}" ] && [ -f "$ENV_FILE" ]; then
    args+=(--env-file "$ENV_FILE")
  fi
  echo "COMPOSE_UP project=$STACK_PROJECT file=$COMPOSE_FILE"
  if ! docker compose "${args[@]}" up -d; then
    fail "docker compose up failed; inspect the compose output and server logs"
  fi
}

wait_world() {
  local world deadline
  world="$(resolve_world_path)"
  deadline=$((SECONDS + STACK_START_TIMEOUT_S))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -s "$world/level.dat" ]; then
      return 0
    fi
    if ! docker inspect -f '{{.State.Running}}' "$SERVER_CONTAINER" 2>/dev/null | grep -q true; then
      fail "server container $SERVER_CONTAINER is not running while waiting for $world/level.dat"
    fi
    sleep 2
  done
  fail "timed out after ${STACK_START_TIMEOUT_S}s waiting for $world/level.dat"
}

wait_server_ready() {
  # level.dat appears while the server is still generating the spawn area. The
  # Supervisor must not create bot sessions until the server has finished
  # "Done ("; otherwise the first runtime transport call can race the server.
  local world log deadline
  world="$(resolve_world_path)"
  log="$(dirname "$world")/logs/latest.log"
  deadline=$((SECONDS + STACK_START_TIMEOUT_S))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -f "$log" ] && grep -q 'Done (' "$log" 2>/dev/null; then
      return 0
    fi
    if ! docker inspect -f '{{.State.Running}}' "$SERVER_CONTAINER" 2>/dev/null | grep -q true; then
      fail "server container $SERVER_CONTAINER stopped before readiness"
    fi
    sleep 2
  done
  fail "timed out after ${STACK_START_TIMEOUT_S}s waiting for Minecraft server readiness"
}

bot_health_urls() {
  local parser out=""
  for parser in "$PYTHON_BIN" python3; do
    command -v "$parser" >/dev/null 2>&1 || continue
    out="$(PYTHONPATH="$H/src" "$parser" - "$CONFIG" 2>/dev/null <<'PYURLS'
import sys
try:
    from minecraft_harness.config import load_config
    config = load_config(sys.argv[1])
    runtime = config.get('runtime') or {}
    values = []
    if runtime.get('url'):
        values.append(str(runtime['url']))
    values.extend(str(value) for value in (runtime.get('urls') or {}).values())
    for value in values:
        print(value.rstrip('/'))
except Exception:
    pass
PYURLS
)"
    [ -n "$out" ] && break
  done
  printf '%s
' "$out"
}

wait_bot_health() {
  local urls url deadline
  urls="$(bot_health_urls)"
  if [ -z "$urls" ]; then
    echo "run-paper.sh: WARNING: no runtime URLs in config; relying on Supervisor health checks" >&2
    sleep 5
    return 0
  fi
  deadline=$((SECONDS + STACK_START_TIMEOUT_S))
  while [ "$SECONDS" -lt "$deadline" ]; do
    local ready=1
    while IFS= read -r url; do
      [ -n "$url" ] || continue
      if ! curl -fsS --max-time 2 "$url/health" >/dev/null 2>&1; then
        ready=0
        break
      fi
    done <<< "$urls"
    [ "$ready" = 1 ] && return 0
    sleep 2
  done
  fail "timed out after ${STACK_START_TIMEOUT_S}s waiting for bot /health endpoints"
}

prepare_world_marker() {
  # A fresh world marker must use the seed that the server actually generated.
  # Never invent an independent random seed here: the Supervisor rejects a
  # marker whose seed does not match level.dat. Existing markers are immutable.
  local WORLD_PATH
  WORLD_PATH="$(resolve_world_path)"
  if [ ! -s "$(dirname "$WORLD_PATH")/.harness-world.json" ]; then
    local WORLD_PY="$PYTHON_BIN"
    [ -x "$WORLD_PY" ] || WORLD_PY=python3
    PYTHONPATH="$H/src" "$WORLD_PY" - "$WORLD_PATH" <<'PYMARK'
import json, secrets, sys
from pathlib import Path
from minecraft_harness.environment import read_level
world = Path(sys.argv[1])
marker = world.parent / '.harness-world.json'
if marker.exists():
    raise SystemExit(0)
if not (world / 'level.dat').is_file():
    raise SystemExit('level.dat is missing; preflight must run before marker creation')
data = read_level(world / 'level.dat')
settings = data.get('WorldGenSettings') if isinstance(data.get('WorldGenSettings'), dict) else {}
seed = settings.get('seed', data.get('RandomSeed'))
if type(seed) is not int:
    raise SystemExit('generated level.dat has no integer seed; refusing to create a mismatching marker')
marker.write_text(json.dumps({'schema_version': 1, 'world_id': secrets.token_hex(16),
                              'origin': 'new_random', 'seed': seed}, indent=2) + '\n')
marker.chmod(0o600)
PYMARK
  fi
}

if [ "$MODE" = prepare ]; then
  preflight
  echo "PREPARED $(date -u +%FT%TZ)"
  echo "  config=$CONFIG"
  echo "  run_dir=$RUN_DIR"
  echo "  log=$LOG"
  echo "  server_container=$SERVER_CONTAINER"
  echo "  runtime=$RUNTIME_KIND"
  echo "NEXT: $0 --start"
  exit 0
fi

preflight
compose_up
wait_world
wait_server_ready
prepare_world_marker
preflight_world
wait_bot_health

{
  echo "START $(date -u +%FT%TZ) run_dir=$RUN_DIR"
  if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
    echo "ENV_FILE $ENV_FILE"
  fi
  PYTHONPATH="$H/src:$H/.venv/lib/python3.11/site-packages" "$PYTHON_BIN" -m minecraft_harness.cli run \
    --config "$CONFIG" \
    --run-dir "$RUN_DIR"
  RC=$?
  echo "SUPERVISOR_EXIT $(date -u +%FT%TZ) rc=$RC"
  # Ensure the latest world is flushed and the server is stopped before archiving.
  echo "SERVER_CONTAINER $SERVER_CONTAINER STACK_SELECTOR ${STACK_SELECTOR[*]}"
  if docker inspect -f '{{.State.Running}}' "$SERVER_CONTAINER" 2>/dev/null | grep -q true; then
    docker exec "$SERVER_CONTAINER" harness-control save >/dev/null 2>&1 || true
    docker stop --time 45 "$SERVER_CONTAINER" >/dev/null 2>&1 || true
  fi
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  ARCHIVE="$H/infra/server-data-backups/world-$STAMP-after-four-agent-paper.tar.gz"
  WORLD_ROOT="$(dirname "$(resolve_world_path)")"
  tar -czf "$ARCHIVE" -C "$WORLD_ROOT" world logs 2>/dev/null || true
  echo "WORLD_ARCHIVE $ARCHIVE"
  # Stop remaining bot stack to release resources; preserve containers for inspection.
  docker ps "${STACK_SELECTOR[@]}" -q | xargs -r docker stop --time 20 >/dev/null 2>&1 || true
  echo "DONE $(date -u +%FT%TZ)"
  exit $RC
} >> "$LOG" 2>&1
