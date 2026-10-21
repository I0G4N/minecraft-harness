ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS dependencies
ENV NODE_ENV=production
WORKDIR /app
COPY bot_runtime/package.json bot_runtime/package-lock.json ./
COPY bot_runtime/scripts/canvas-shim.mjs ./scripts/canvas-shim.mjs
COPY bot_runtime/.build-npm-cache/_cacache /tmp/npm-cache/_cacache
RUN --network=none npm ci --offline --omit=dev --ignore-scripts --no-audit --no-fund \
    --cache /tmp/npm-cache && node scripts/canvas-shim.mjs && rm -rf /tmp/npm-cache
FROM ${NODE_IMAGE}
ENV NODE_ENV=production
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY bot_runtime/package.json bot_runtime/package-lock.json ./
COPY bot_runtime/src ./src
USER node
EXPOSE 3001
CMD ["node", "src/server.js"]
