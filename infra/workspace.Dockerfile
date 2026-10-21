ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE}
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends git ripgrep python3 python3-httpx coreutils util-linux ca-certificates && rm -rf /var/lib/apt/lists/*
COPY src/minecraft_harness/program_cli.py /opt/mc.py
COPY --chmod=0755 infra/mc /usr/local/bin/mc
WORKDIR /workspace
ENV GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/workspace
CMD ["sleep", "infinity"]
