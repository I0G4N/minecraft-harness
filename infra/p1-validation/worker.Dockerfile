# Validation-only overlay image for P1 soft-reset work.
#
# The production worker image is built from infra/bot.Dockerfile +
# infra/worker-sandbox.Dockerfile.  Docker Hub metadata for the `node:` base is
# intermittently unreachable from this host, so validation overlays the changed
# bot_runtime source onto the already-built worker image (which carries the
# pinned node_modules and P0 pathfinder patches).  Do not use this file to
# produce a release artifact; document the image id in the run report instead.
ARG BASE=minecraft-harness-worker:viewer
FROM ${BASE}
USER root
COPY bot_runtime/src /app/src
USER node
