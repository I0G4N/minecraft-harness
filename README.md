# Minecraft Multi-Agent Experiment Harness

Independent, evidence-driven multi coding-agent Minecraft experiment framework based on the
public method of *A team of Gemini 4 Argon agents beats Minecraft* (arXiv:2610.22319).
This standalone repository contains the harness, bot runtime, sandbox infrastructure, tests
and dashboards; it does not import the surrounding product runtime.

The driver model defaults to **DeepSeek `deepseek-flash`** (this project's choice, not the
paper's Gemini), with paper-aligned single-program/tight-interface semantics.

## What is included

- Python 3.11 supervisor: 1–4 programming agents, LLM Observer, durable SQLite event ledger,
  budgets, world/runtime epochs, report and figure reconstruction.
- Node 22 Mineflayer worker: generated async programs, paper mode with preserved
  globals/timers/listeners, loop-deadline instrumentation (including `await`-then CPU loops),
  read-only fs proxy, bounded output.
- Sandbox stack: Docker/gVisor (`runsc`) OCI runtime, read-only containers, seccomp filter,
  fixed identity proxy, module allow-list.
- Dashboard: run overview, timeline, programs/jobs, chat, evidence, Observer, paper figures
  (Fig. 4 13-step, Fig. 13 taxonomy, Fig. 12 etc.), in-game days and a read-only textured
  3D world mesh with per-pixel z-buffer (160×160 default; full-world LOD mode available).
- Tests: 350 Python + 51 Node tests; offline fake-mode smoke, 4-agent soak and scale tests.

## Requirements

Linux/WSL2 with:

- Docker Engine + Compose v2
- `uv` (Python 3.11 will be installed by uv if needed)
- Node.js 22 (for `npm ci` and local runtime tests)
- `cc` (native seccomp sandbox)
- GNU bash, git

Docker root or a user in the `docker` group is required for the Minecraft server/worker
containers. gVisor is strongly recommended for generated code; see
[infra/SANDBOX.md](infra/SANDBOX.md) and `scripts/install_gvisor.sh`.

## One-click setup

```bash
git clone git@github.com:I0G4N/minecraft-harness.git
cd minecraft-harness

export DEEPSEEK_API_KEY=sk-...          # or pass --api-key below
./setup.sh --accept-eula --agents 4 \
  --wall-minutes 60 --max-calls 2000 --max-cost 5
```

`setup.sh`:

1. installs Python dependencies with `uv sync --python 3.11 --locked`;
2. installs `bot_runtime` npm dependencies;
3. builds `infra/.cache/sandbox.so` if missing;
4. generates **`configs/local.yaml`** from the paper-aligned template;
5. generates **`infra/.env`** with random worker/bot tokens, UID/GID, EULA flag and,
   optionally, `DEEPSEEK_API_KEY` (mode 600, gitignored);
6. validates the config with `minecraft_harness.config.validate_config`.

Useful flags:

| Flag | Effect |
|---|---|
| `--accept-eula` | Sets `ACCEPT_EULA=true`; required for a Minecraft server |
| `--api-key KEY` | Writes key into `infra/.env` (prefer env var) |
| `--no-install` | Skip `uv sync` / `npm ci` |
| `--build` | Build all paper-compose images |
| `--up` | Build+start Minecraft server and bot/control/launcher stack |
| `--run` | Start the supervisor after setup |
| `--install-gvisor` | Install+register pinned `runsc` (requires root/sudo) |
| `--force` | Replace an existing `configs/local.yaml` |
| `--agents N` | Number of players, 1–8 (4 = paper-aligned; others use strict/longrun semantics) |
| `--wall-minutes`, `--max-calls`, `--max-cost` | Budgets |
| `--name` | Experiment name |
| `--world`, `--log` | Override server world/log paths |

If you only want an offline demo (no Minecraft server, no model charge):

```bash
uv sync --python 3.11 --locked
npm --prefix bot_runtime ci
uv run mc-harness run --config configs/offline-smoke.yaml
uv run mc-harness serve runs --port 8896
```

## More than four agents

The paper protocol uses four players. The harness itself is N-agent capable: `--agents 1..8`
generates an exact-N compose file and config. Four agents use the paper-aligned profile;
other counts use the strict/longrun profile (same DeepSeek model and worker). Each agent gets a
separate identity proxy, launcher, worker network and loopback bot port (3101..3108).

```bash
./setup.sh --accept-eula --agents 6 --wall-minutes 120 --max-calls 5000 --max-cost 10
```

## Run a real experiment

```bash
# Build and start the Minecraft server + bot/control/launcher containers.
# setup.sh writes compose.local.yaml for the requested agent count.
docker compose -f infra/compose.local.yaml --env-file infra/.env up -d --build

# Run the supervisor on the host; it will wait for the world and connect to the bots.
./scripts/run.sh

# Inspect results (a second terminal).
.venv/bin/mc-harness serve runs --port 8896
# open http://127.0.0.1:8896/
```

The server writes its save under `infra/server-data/world`; the 3D dashboard reads it
read-only. All run state goes to gitignored `runs/<run-id>/`.

## Tests

```bash
# Python
.venv/bin/python -m unittest discover -s tests -q

# Node worker (from the repository root)
npm --prefix bot_runtime test

# Full integration smoke (fake model + fake bot, no Docker world)
uv run mc-harness doctor --config configs/offline-smoke.yaml
uv run mc-harness run --config configs/offline-smoke.yaml
```

## Layout

```text
src/minecraft_harness/     Python supervisor, agents, execution gateway, dashboard, figures
bot_runtime/               Node Mineflayer worker and lifecycle tests
configs/                   paper-aligned, whole-session, longrun and offline templates
infra/                     Dockerfiles, compose files, gVisor docs, seccomp sandbox
scripts/                   setup helpers, world renderer, analysis and validation utilities
tests/                     Python test suite
runs/                      generated run artifacts (gitignored)
```

## Security boundaries

The JS module allow-list and the read-only fs Proxy are **defense in depth, not a security
boundary**. Generated code is confined by:

1. gVisor `runsc` OCI runtime (recommended/default in paper compose);
2. read-only container, dropped capabilities, no-new-privileges, network isolation;
3. native seccomp filter (deny exec/fork/writes) plus fixed identity proxy;
4. only then the Node module policy / fs proxy.

Never run generated programs with plain `runc` and a writable socket on an untrusted host.

## Paper alignment and known limits

See [PAPER_ALIGNMENT.md](PAPER_ALIGNMENT.md) for the figure-by-figure comparison and
[LONG_RUN_VALIDATION.md](LONG_RUN_VALIDATION.md) for scale/long-run results. Known limits:

- asynchronous CPU loops after `await` are interrupted by loop instrumentation; a single
  non-loop long computation still falls back to supervisor abort grace/worker reset;
- default 3D mesh is a 160×160 local view; full-world mode is LOD, not 1:1;
- paper 13-step/Fig. 13 taxonomy is a deterministic reconstruction from the paper figures;
- a full 27-hour real run has not been staged in this repository;
- model/context/token accounting differs from the paper's Gemini setup.

## Third-party assets

Dashboard textures use CC BY-SA 4.0 Pixel Perfection Legacy assets and the SIL OFL 1.1
Cubic 11 font, only for local dashboard visuals. The acorn parser is MIT; attribution files
live beside the assets. These assets are not used for scoring evidence or game runtime.
