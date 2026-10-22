# Independent Minecraft research environment

This template starts Mojang Java Edition **1.20.4**, Survival/Easy, with one
runtime per player. It has no dependency on the existing product stack.

## Paper-aligned deployment

Use `compose.paper.yaml` for the separated control/execution boundary. The
commands below build images only; they do not start Java or accept the EULA.
Run them from `experiments/minecraft_harness`:

```bash
python3 infra/fetch_server.py
(cd bot_runtime && npm ci --ignore-scripts --no-audit --no-fund --cache .build-npm-cache)
mkdir -p infra/.cache
cc -Wall -Wextra -Werror -shared -fPIC -O2 -o infra/.cache/sandbox.so infra/sandbox.c
docker build -f infra/bot.offline.Dockerfile -t minecraft-harness-bot:research .
docker build -f infra/control.Dockerfile -t minecraft-harness-control:research .
docker build -f infra/worker-sandbox.Dockerfile -t minecraft-harness-worker:research .
docker build -f infra/launcher-base.Dockerfile -t minecraft-harness-launcher-base:docker-cli-20261021 .
docker build -f infra/launcher.Dockerfile -t minecraft-harness-launcher:research .
docker build -f infra/workspace.Dockerfile -t minecraft-harness-workspace:local .
docker build -f infra/server.Dockerfile -t minecraft-harness-server:1.20.4-research infra
uv run python scripts/image_manifest.py
```

The native library must be compiled for the worker image's Linux architecture.
The launcher base is a separate, deliberately refreshed dependency; normal
runtime source changes rebuild the three thin images without reinstalling the
Docker client. Record inspected immutable image IDs in addition to tags.

Fill `configs/minecraft-paper-aligned.yaml` with the intended model, frozen
prices and explicit budgets. `.env.example` contains placeholders only and
documents tokens for up to 16 players. For managed trials, export unique
`BOT1_ADMIN_TOKEN` through `BOT<agents>_ADMIN_TOKEN` and `WORKER1_TOKEN`
through `WORKER<agents>_TOKEN` in the trusted operator environment. Python
invokes Compose directly for managed trials, so values must be exported; copying
a file to `infra/.env` alone is insufficient for this path.

## Exact-N-agent generated deployment

The checked-in `compose.paper.yaml` is a four-player reference template. For any
other player count (and for reproducible generated files rather than hand
editing), render the full deployment instead of deleting static service blocks:

```bash
# Does not start Docker, a server or a Supervisor.
python3 scripts/prepare_deployment.py --agents 6 --project mc-paper-6
# writes infra/compose.mc-paper-6.yaml, configs/mc-paper-6.yaml and
# infra/.env.mc-paper-6 (mode 0600, fresh per-agent tokens)

# Only after EULA acceptance and after the previous stack is stopped:
docker compose -p mc-paper-6 -f infra/compose.mc-paper-6.yaml \
  --env-file infra/.env.mc-paper-6 up -d
PAPER_ENV_FILE=$PWD/infra/.env.mc-paper-6 PAPER_CONFIG=$PWD/configs/mc-paper-6.yaml \
  PAPER_RUN_DIR=<fresh-run-dir> <deployment>/run-paper.sh   # reads server.managed_container
```

The renderer clones exactly N `identity`/`launcher`/`bot` triples, N worker
networks, N loopback bot ports and N viewer ports. Four players keep the
paper-aligned lifecycle; other counts use the strict lifecycle because the
paper profile is defined for exactly four players. The independent Observer has
no bot triple; with `observer.mode: llm` the Supervisor adds its runner container
dynamically, for `N + 1` runner containers per run. The first four worker
subnets preserve the static paper values; counts above four use non-overlapping
`172.31.*/24` subnets. Run only one Minecraft stack at a time on this host.
`run-paper.sh` derives the project from `server.managed_container` and loads the
matching generated `infra/.env.<project>` (or `PAPER_ENV_FILE`), so an existing
Supervisor container does not keep using another project's bot tokens.
The generated server service carries `MAX_PLAYERS=N`; for a fresh data directory
the server entrypoint rewrites `server.properties` accordingly (rebuild the
server image after changing the entrypoint). Existing data directories are left
untouched and must be provisioned/checked by the operator. Workspace provisioning
supports up to 16 players plus the Observer; counts above four also need a fresh
world and a stack with non-overlapping networks/ports (stop the old project, not
just its containers).

```bash
# Prepares inspectable world/config directories only, without Java or EULA acceptance.
uv run mc-harness prepare-trials --config configs/minecraft-paper-aligned.yaml --seeds random --run-dir runs/prepared
# After explicit EULA acceptance, set ACCEPT_EULA=true in the operator environment.
# This command prepares a separate new batch and runs its trials sequentially.
uv run mc-harness batch --config configs/minecraft-paper-aligned.yaml --seeds random --run-dir runs/batch
```

Each trial gets a unique Compose project, server container, world, run ledger,
workspace and memory. The trusted operator manages ports 3101 through 3100+N
exclusively; concurrent real deployments on those ports are unsupported. The
server uses the trial's prepared `server.properties`, and the supervisor validates
the generated `level.dat` and actual rules before model admission. Preparation
does not create an already generated world; the managed batch waits for Vanilla
readiness first.

Each player has a fixed identity proxy on its private game network. The worker
can reach that proxy; the control service and launcher are on a separate network.
The trusted launcher holds the Docker socket and accepts only authenticated,
fixed-image/fixed-network worker operations. It never runs generated code.
Workers have no host mounts, management credentials, model keys or Docker socket;
native seccomp rejects filesystem mutation and process creation. Cancellation
must confirm worker removal before replacement. A separate launcher guardian
also removes workers after launcher loss.

Coding workspaces use separate network-disabled containers. They mount only
their own source tree, read-only locked API documentation and (players only) a
dedicated Unix submission socket. The Observer has no player socket. Their shell
can edit source and run development tools; game code runs in the restricted
worker, not in that workspace container.

On this host, Mihomo's nftables local-address snapshot omits newly created Docker
subnets. This redirects peer traffic to the host proxy. The bounded probe in
`scripts/proxy_network_probe.py` confirmed the cause with a temporary exemption
for disposable peers, then removed it. Formal deployment needs administrator
configuration of the proxy's local experiment networks. Do not weaken global
firewall rules to work around it. Protocol-fixture validation and its temporary
changes are recorded in `validation/2026-10-21-isolated-protocol.json`; it is not
a Vanilla result.

## Legacy strict deployment

The following older Compose path retains the co-located strict runtime. Use it
for controlled research or regression checks; it does not provide the paper
deployment's control-plane separation or protocol identity boundary.

```bash
cd experiments/minecraft_harness/infra
python3 fetch_server.py
cp .env.example .env
# Edit .env: unique tokens; ACCEPT_EULA=true only after accepting the EULA.
install -d -m 0750 server-data
# Set MC_UID/MC_GID in .env to your operator's id -u / id -g.
# The operator must be able to read/write server-data for checkpoint restore.
docker compose build
docker compose up -d
# For two/four players, supply all profile tokens in .env:
docker compose --profile two up -d
docker compose --profile four up -d
```

`fetch_server.py` downloads only the official Mojang version manifest and server
jar, verifies the metadata/server SHA-1 supplied by Mojang and records a SHA-256
in `.cache/server-lock.json`. It does not accept an EULA or start a server.
Keep this lock with the run manifest. Base image tags must be replaced with
inspected immutable digests in `.env` for recorded experiments; do not claim
reproducibility from the example mutable tags. Inspect and archive the actual
local server/bot image IDs as well.

The operator-side Python supervisor stays outside the bot containers and holds
model credentials. Its runtime URLs are `http://127.0.0.1:3101` through `3104`;
each runtime has its own admin token and exactly one agent token. Player IDs are
`player1` through `player4`. Put only corresponding environment variable names in
experiment configuration, never actual credentials. Do not mount `.env`, an
agent workspace, a server world or run/recorder directory into a bot container.

Only the server mounts `server-data`. Runtime root filesystems are read-only,
with a bounded temporary directory, non-root user, dropped capabilities,
no-new-privileges and resource limits. Each player uses a separate internal
Compose bridge connected to the server. No model access or external network is
provided to the runtime. RPC control ports bind to host loopback through the
fixed relay; these ports are for the trusted local operator, not a public service.
Bots reach `server:25565` over their internal bridge. No host game port is
published: a human Minecraft client would need a separately provisioned,
controlled game relay. This validation did not exercise human game-client traffic. See the
[Compose networking reference](https://docs.docker.com/compose/how-tos/networking/).

## Builds without npm network inside Docker

If the build cannot reach npm through the environment proxy, prepare a package
cache on the host and install it without build-time network:

```bash
cd ..
(cd bot_runtime && npm ci --ignore-scripts --no-audit --no-fund --cache .build-npm-cache)
docker build -f infra/bot.offline.Dockerfile -t minecraft-harness-bot:local .
```

Only public cache content is copied. npm verifies package-lock integrity; the
runtime image contains dependencies, not the cache or host configuration.

## Evidence collection and snapshots

Record a trusted baseline before player actions. Issue `save-all flush` through
the trusted server console before collecting `server-data/world/{stats,advancements}`.
Stopping the server before a final collection gives a consistent saved state.
`ServerEvidenceCollector` only reads these files; it cannot flush or command the
server. Do not copy a running world directory and call it a consistent recovery
checkpoint. For a restartable checkpoint, stop programs, stop the server, copy
the data directory, and record the checkpoint and epoch. Never attach the server
console or grant operator permissions to agents. RCON and command blocks are
disabled in this template.

The managed container is named `minecraft-harness-server`. The trusted
supervisor uses `docker exec minecraft-harness-server harness-control ready`
to check this boot's `Done` log and `... harness-control save` to send exactly
`save-all flush` through an owner-only console FIFO. The save command waits for
a new, exactly matched server `Saved the game` acknowledgement. It rejects chat
text and stale acknowledgements. No generic command argument or network admin
port exists. The helper and FIFO are present only in the server container.

## Legacy strict security boundary and current limitations

The container is the OS boundary around research code. JavaScript `node:vm`, a
module allowlist and stripped child environment are not a security sandbox.
The HTTP parent and JS worker currently share a container and Unix UID; hostile
code that escapes the JS interface may read parent process state, including its
runtime control token. Containers contain **no model keys** and **no world or
recorder mount**, but this does not make their local control token confidential
against an arbitrary same-UID process. Hardened deployments must separate the
control plane and workers by namespace/UID and validate negative probes.

`ALLOWED_AGENT_ID`/`ALLOWED_USERNAME` constrain the runtime API's session identity.
The offline Minecraft protocol still trusts a login username: arbitrary worker
network code could create a new client that impersonates another player. A
protocol-aware identity proxy or authenticated accounts is required before
claiming hostile-code identity isolation. Isolated bridges do not fix this
protocol issue. This template is appropriate for controlled research code,
not multi-tenant execution of adversarial programs.

Images must be built and their actual IDs recorded locally. A successful image
build or protocol-fixture test does not prove Vanilla or live-model integration.

## Long experiment operation

Compose provides server/bot health checks and caps Docker's own logs at three
10 MiB files per service. It deliberately uses `restart: "no"`: the Supervisor
owns fencing and recovery. Rebuild both images after source/helper changes and
record their inspected image IDs for each experiment.

The bot container has a 512 MiB memory limit; its worker RSS watchdog is set
to 256 MiB to leave headroom for the HTTP parent and guardian. Supervisor lease
and parent watchdog defaults are explicitly 30 seconds and 10 seconds.

The operator's `ServerControl` applies one total deadline to each save, stop or
start. Console helpers also run inside a container-side `timeout`, so losing a
Docker client does not leave a save helper running indefinitely. A save helper
releases its lock on TERM. Readiness is tied to the current Java boot; stopping
must be confirmed by Docker before copying world files.

Checkpoint creation requires a stopped, exclusively owned server. It checks
free disk, verifies copies, fsyncs files/directories and atomically publishes the
finished directory. A restore journal is stored beside the data mount root.
After process loss, `resume` verifies and restores the complete pre-restore
backup before starting the world; an invalid/missing backup blocks startup.
A completed restore publishes a durable receipt before removing its journal.
The Supervisor commits the receipt's rollback lineage to SQLite before acknowledging
it, so a crash between filesystem completion and ledger commit remains recoverable.
Do not delete that journal or its backup to bypass a failed recovery.

The run directory and Minecraft data may live on different filesystems. Monitor
both disks: Docker log rotation does not cap `/data` regions or Minecraft's own
log archives. Framework storage limits stop the run before exhaustion; manual
checkpoints/backups remain operator-owned and should be removed only after their
lineage is no longer needed.

If an external runtime gateway is unreachable and the old worker cannot be
confirmed stopped, the framework ends the run safely and records the uncertainty.
The operator must recover and inspect that container; automatic retries cannot
certify process termination through an unavailable control plane.

The operator connects to fixed localhost ports 3101–3104 through the
`rpc-relay` service. Bot containers publish no host ports and stay on their
internal game bridges. The relay alone joins the ordinary RPC bridge used for
Docker port publication; it forwards only to the four configured bot endpoints.
It has no world mount, model key, generic destination forwarding or proxy API.
Its 90-second idle limit allows the 60-second session startup request to wait
for Mineflayer; ordinary Python RPC operations retain their shorter deadline.

Paper workers use `internal: true` **and**
`com.docker.network.bridge.gateway_mode_ipv4: isolated` on each `worker1..4`
frontend. Docker's ordinary internal bridge still has a host-side address; host
services listening on `0.0.0.0` can be reachable through it. Isolated gateway mode
removes that host bridge address. Only the worker and its fixed identity proxy
join each frontend; the separate world/backend remains internal. This requires a
Docker Engine supporting isolated gateway mode, and unsupported engines must not
silently fall back. See the [official Docker gateway-mode documentation](https://docs.docker.com/engine/network/port-publishing/#gateway-modes).

`uv run python scripts/network_boundary_probe.py` compares temporary ordinary and
isolated networks against a host HTTP fixture created by that test, plus public
IP and container-loopback negative probes. It changes no global firewall/proxy
policy. `validation/2026-10-21-network-boundary.json` confirms the isolated bridge
has no host addresses and all probes fail. The ordinary control attempt on this
host also timed out because existing proxy policy can intercept it; the record
marks that reachability comparison inconclusive, instead of claiming ordinary
internal mode is safe. The isolated Minecraft protocol smoke additionally checks
the frontend's actual Docker option and absent host bridge address before spawn.

Formal paper admission now captures `effective_environment.deployment` before
constructing any player/Observer model. The operator inspects only the declared
managed server, containers with that server's exact Compose project label
(maximum 20), owned coding workspaces and declared worker-network peers. The
record includes actual immutable image IDs and available repository digests for
server/control/launcher/identity proxy/worker/workspace, user and namespace
identity, read-only mounts, resource limits, declared ports and network members.
The only projected environment values are `GAME_CHAT`, `MC_HOST`, `MC_PORT` and
`ALLOWED_USERNAME`; credentials, arbitrary environment and command strings are
excluded. Full evidence is operator-only and unavailable to the Observer/player
record tools. Actual saved NBT GameRules and the versioned non-secret
`server.properties` whitelist are stored alongside world identity.

Before formal models are admitted, the trusted Docker inspection must confirm
each player's distinct frontend is internal/isolated, has no gateway or IPv6, and
contains exactly that worker plus an identity proxy in the owned project. The
actual worker must have only this network, a read-only root, no mounts, private
PID/IPC namespaces, dropped capabilities and no-new-privileges. The proxy's
actual allowed username, chat switch and fixed backend must match the experiment.
A missing managed-server image or mismatched network/proxy declaration refuses
admission. Fake diagnostics skip deployment inspection entirely.

`uv run python scripts/deployment_projection_probe.py` validates the real Docker
Go template and selective metadata projection using temporary network-disabled
sleep containers with synthetic secret values. It starts no game/model and
cleans up its containers. The captured
`validation/2026-10-21-deployment-projection.json` confirms actual image IDs,
project-scoped inventory, four selected public values and absence of the
synthetic credential. Inspection output, duration, process groups and cleanup are
bounded, including failed readers whose subprocess pipes fill.
