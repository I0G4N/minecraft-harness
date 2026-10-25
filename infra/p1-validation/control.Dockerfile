# Validation-only overlay image for P1 soft-reset work (control image).
# Used by bot<N> (session RPC server) and identity<N> (identity proxy).
# See infra/validation/worker.Dockerfile for why this overlays a cached base.
ARG BASE=minecraft-harness-control:research
FROM ${BASE}
USER root
COPY bot_runtime/src /app/src
USER node
