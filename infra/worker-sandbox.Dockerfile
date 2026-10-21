ARG BOT_IMAGE=minecraft-harness-bot:research
FROM ${BOT_IMAGE}
# Build infra/.cache/sandbox.so from infra/sandbox.c on the target Linux
# architecture first. No package install or network access is needed here.
COPY infra/.cache/sandbox.so /opt/harness/sandbox.so
COPY bot_runtime/src /app/src
ENV LD_PRELOAD=/opt/harness/sandbox.so
USER node
WORKDIR /app
ENTRYPOINT ["node"]
CMD ["src/worker-stdio.js"]
