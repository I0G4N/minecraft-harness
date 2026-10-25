# Validation-only overlay image for P1 soft-reset work (launcher image).
# The launcher runs privileged worker-host.js; the base image keeps USER root
# (needed for the docker CLI and sandbox lifecycle), so do not switch users.
ARG BASE=minecraft-harness-launcher:research
FROM ${BASE}
COPY bot_runtime/src /app/src
