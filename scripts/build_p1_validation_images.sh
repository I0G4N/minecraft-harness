#!/usr/bin/env bash
# Build the P1 validation images from cached bases.
#
# Why overlays: a clean `infra/bot.Dockerfile` build needs the `node:` base from
# Docker Hub, whose metadata request times out from this host.  The cached bot /
# worker / control / launcher images already contain the pinned node_modules and
# P0 pathfinder patches, so validation copies only bot_runtime/src on top.
#
# The previous tags are preserved as *-pre-p1 so a later clean rebuild can be
# compared and so the stack can be rolled back.
#
# The Docker build context is a small staged tree (not the repo root) because
# mc-runs is large and is not excluded by the repository .dockerignore.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

DOCKER=${DOCKER:-docker}
BASE_BOT=${BASE_BOT:-minecraft-harness-bot:p0-test}
WORKER_IMAGE=${WORKER_IMAGE:-minecraft-harness-worker:viewer}
CONTROL_IMAGE=${CONTROL_IMAGE:-minecraft-harness-control:research}
LAUNCHER_IMAGE=${LAUNCHER_IMAGE:-minecraft-harness-launcher:research}
TEST_IMAGE=${TEST_IMAGE:-minecraft-harness-worker:p1-test}
BUILDCTX=${BUILDCTX:-$(mktemp -d /tmp/mc-p1-buildctx.XXXXXX)}

cleanup() { rm -rf "$BUILDCTX"; }
trap cleanup EXIT

stage_context() {
  mkdir -p "$BUILDCTX/bot_runtime" "$BUILDCTX/infra/p1-validation"
  cp -a bot_runtime/src "$BUILDCTX/bot_runtime/src"
  cp -a infra/p1-validation/*.Dockerfile "$BUILDCTX/infra/p1-validation/"
}

stage_test_context() {
  cp -a bot_runtime/test "$BUILDCTX/bot_runtime/test"
  cp -a bot_runtime/tests "$BUILDCTX/bot_runtime/tests"
  cp -a bot_runtime/package.json "$BUILDCTX/bot_runtime/package.json"
}

backup_tag() {
  local image=$1 backup=$2
  if ! "$DOCKER" image inspect "$backup" >/dev/null 2>&1; then
    echo "backup: $image -> $backup"
    "$DOCKER" tag "$image" "$backup"
  else
    echo "backup exists: $backup"
  fi
}

backup_tag "$WORKER_IMAGE" "${WORKER_IMAGE}-pre-p1"
backup_tag "$CONTROL_IMAGE" "${CONTROL_IMAGE}-pre-p1"
backup_tag "$LAUNCHER_IMAGE" "${LAUNCHER_IMAGE}-pre-p1"

stage_context
stage_test_context

echo "building $TEST_IMAGE (node --test runtime)"
"$DOCKER" build -f "$BUILDCTX/infra/p1-validation/test.Dockerfile" \
  --build-arg "BASE=$BASE_BOT" -t "$TEST_IMAGE" "$BUILDCTX"

echo "building $WORKER_IMAGE (sandboxed code-execution worker)"
"$DOCKER" build -f "$BUILDCTX/infra/p1-validation/worker.Dockerfile" \
  --build-arg "BASE=${WORKER_IMAGE}-pre-p1" -t "$WORKER_IMAGE" "$BUILDCTX"

echo "building $CONTROL_IMAGE (bot session RPC + identity proxy)"
"$DOCKER" build -f "$BUILDCTX/infra/p1-validation/control.Dockerfile" \
  --build-arg "BASE=${CONTROL_IMAGE}-pre-p1" -t "$CONTROL_IMAGE" "$BUILDCTX"

echo "building $LAUNCHER_IMAGE (worker-host launcher)"
"$DOCKER" build -f "$BUILDCTX/infra/p1-validation/launcher.Dockerfile" \
  --build-arg "BASE=${LAUNCHER_IMAGE}-pre-p1" -t "$LAUNCHER_IMAGE" "$BUILDCTX"

for image in "$TEST_IMAGE" "$WORKER_IMAGE" "$CONTROL_IMAGE" "$LAUNCHER_IMAGE"; do
  printf '%s %s\n' "$image" "$("$DOCKER" image inspect "$image" --format '{{.Id}}')"
done
