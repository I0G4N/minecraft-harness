ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE}
ENV NODE_ENV=production
WORKDIR /app
COPY bot_runtime/package.json bot_runtime/package-lock.json ./
COPY bot_runtime/scripts/canvas-shim.mjs ./scripts/canvas-shim.mjs
COPY bot_runtime/scripts/patch-pathfinder.mjs ./scripts/patch-pathfinder.mjs
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --omit=dev --ignore-scripts --fetch-timeout=20000 --fetch-retries=1 && npm cache clean --force \
    && node scripts/canvas-shim.mjs \
    && node scripts/patch-pathfinder.mjs
COPY bot_runtime/src ./src
USER node
EXPOSE 3001
CMD ["node", "src/server.js"]
