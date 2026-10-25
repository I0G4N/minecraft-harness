# Validation-only test image: worker base (full pinned node_modules) plus the
# bot runtime sources and tests, so `node --test` runs exactly what the worker
# will run.  Kept out of production image builds on purpose.
ARG BASE=minecraft-harness-bot:p0-test
FROM ${BASE}
USER root
COPY bot_runtime/src /app/src
COPY bot_runtime/test /app/test
COPY bot_runtime/tests /app/tests
COPY bot_runtime/package.json /app/package.json
WORKDIR /app
USER node
ENTRYPOINT ["node"]
