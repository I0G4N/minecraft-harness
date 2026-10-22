# Pinned Pi coding-agent runtime

This directory pins the Pi coding-agent version used as the player agent runtime
for the paper experiment. Pi is MIT-licensed (see
https://github.com/earendil-works/pi); `package-lock.json` is the source of
truth. Node.js >= 22.19 is required.

Install/restore the exact dependency tree before a run:

```bash
cd /path/to/minecraft_harness
npm ci --prefix pi_runtime --ignore-scripts --no-audit --no-fund
```

`node_modules/` is intentionally git-ignored. Nothing in this directory talks to
a model directly: `src/minecraft_harness/pi_runner.py` writes a loopback
OpenAI-compatible `models.json`, starts the Pi CLI in `--mode rpc`, and meters
every model attempt through the harness budget/ledger.

The pinned CLI path is:

```text
pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js
```

If `pi_runtime` is mounted read-only into a runner container, the same absolute
path must exist inside the container so `HARNESS_PI_CLI` resolves.
