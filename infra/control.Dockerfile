ARG BOT_IMAGE=minecraft-harness-bot:research
FROM ${BOT_IMAGE}
COPY bot_runtime/src /app/src
USER node
CMD ["node", "src/server.js"]
