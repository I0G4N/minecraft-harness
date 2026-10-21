ARG LAUNCHER_BASE=minecraft-harness-launcher-base:docker-cli-20261021
FROM ${LAUNCHER_BASE}
COPY bot_runtime/src /app/src
USER root
CMD ["node", "src/worker-host.js"]
