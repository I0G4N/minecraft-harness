# Refresh deliberately when updating the Docker client/base OS. Runtime-source
# changes belong to launcher.Dockerfile and never rerun this package install.
ARG BOT_IMAGE=minecraft-harness-bot:research
FROM ${BOT_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends docker.io && rm -rf /var/lib/apt/lists/*
