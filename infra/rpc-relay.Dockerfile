ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE}
WORKDIR /app
COPY --chown=node:node bot_runtime/src/rpc-relay.js /app/rpc-relay.mjs
USER node
ENV RELAY_BIND=0.0.0.0
EXPOSE 3001 3002 3003 3004
CMD ["node", "/app/rpc-relay.mjs"]
