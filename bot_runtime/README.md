# Mineflayer program runtime

This independent Node service runs one persistent worker process per bot. Normal
programs retain `globalThis` data/functions. It installs only Mineflayer and
pathfinder: there are no built-in mining, crafting, combat, navigation strategies,
or survival skills.

Requires Node 22+. Install the exact dependency tree with `npm ci --ignore-scripts`,
and run `npm test` for lifecycle/HTTP tests plus a local protocol fixture without
a Java Minecraft server. `npm run test:protocol` runs only that integration test.
It connects real Mineflayer clients using the 1.20.4 protocol and verifies chunk,
inventory, chat, timeout disconnection, and reconnection. The fixture is explicitly
not Vanilla, performs no server world simulation, and proves no gameplay success.
The `minecraft-data` pin is aligned with the locked protocol/chunk dependency tree
because an older data release crashes current chunk version comparisons.

## Start

`npm start` starts `src/server.js`. Configuration:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIFECYCLE` | `strict` | `strict` preserves v2 cleanup; `paper_aligned` enables explicitly registered bounded background jobs. |
| `MAX_BACKGROUND_JOBS` | `8` | Maximum active registered jobs per session. |
| `MAX_BACKGROUND_TTL_MS` | `129600000` | Absolute session lifetime ceiling; session `remaining_run_ms` may lower it. |
| `WORKER_ENDPOINTS_JSON` | `{}` | Trusted map from agent to `{endpoint:"tcp://host:port",token:"..."}` for the separate launcher. Required for real paper-aligned Minecraft. Never forwarded to a generated worker. |
| `MODE` | `minecraft` | `minecraft` connects real bots; `fake` is only a lifecycle/transport fixture. |
| `BIND` / `PORT` | `127.0.0.1` / `3001` | HTTP listener; use `0.0.0.0` inside an isolated container. |
| `TOKEN` | required | Distinct supervisor bearer credential, at least 16 characters. |
| `AGENT_TOKENS_JSON` | `{}` | Optional JSON object mapping agent IDs to distinct agent bearer credentials. |
| `ALLOWED_AGENT_ID` | unset | Fixes this service to one agent ID and matching Minecraft username, including supervisor requests. Set for each real bot container. |
| `MC_HOST` / `MC_PORT` | `127.0.0.1` / `25565` | Shared Minecraft server. |
| `MC_VERSION` | `1.20.4` | Protocol version. The real-server integration must be verified separately. |
| `STARTUP_TIMEOUT_MS` | `45000` | Maximum time to connect and spawn before session creation fails. |
| `MAX_TIMEOUT_MS` | `180000` | Maximum program deadline; individual default is 60000 ms. |
| `EXIT_ON_LEASE_EXPIRY` | `false` | For self-owned local runtimes only: close HTTP and naturally exit after all session leases expire and workers are fenced. Final uncollected events become unknown. |
| `SUPERVISOR_LEASE_MS` | `30000` | Maximum time without an authenticated supervisor heartbeat. Expiry interrupts work and requires session recreation. |
| `PARENT_WATCHDOG_MS` | `10000` | Independent guardian deadline for pulses from the HTTP runtime process. |
| `MAX_WORKER_RSS_BYTES` | `536870912` | Linux guardian RSS limit per bot worker; exceedance kills the process group. Use container limits on all platforms. |
| `MAX_BUFFERED_EVENTS` | `4096` | Maximum unacknowledged events per session. |
| `MAX_EVENT_BYTES` | `8388608` | Maximum serialized unacknowledged event bytes per session. |
| `MAX_RETAINED_EXECUTIONS` | `128` | Recently completed execution records retained after durable ACK. |
| `MAX_QUEUE` | `32` | Maximum queued programs. Total admission also respects bounded execution retention. |
| `MAX_PROGRAM_LOGS` | `128` | Maximum console messages retained per program. |
| `MAX_PROGRAM_LOG_BYTES` | `262144` | Maximum combined console message bytes per program. |
| `MAX_RESULT_BYTES` | `262144` | Conservative result/snapshot serialization budget. |
| `MAX_RESPONSE_BYTES` | `4194304` | HTTP JSON response cap; event pages are additionally limited by bytes. |
| `MAX_SESSIONS` | `16` | Maximum simultaneous sessions in one HTTP process. |

The exported `start(options)` function accepts camelCase equivalents, including
`port: 0`, `exitOnLeaseExpiry`, `leaseTimeoutMs`, `parentWatchdogMs`, `maxWorkerRssBytes`, `maxEvents`,
`maxEventBytes`, `maxRetainedExecutions`, `maxQueue`, `maxLogs`, `maxLogBytes`,
`maxResultBytes`, `maxResponseBytes`, and `maxSessions`, and returns `{server, address, mode, close}` for integration tests.
Signal shutdown fences active workers before exiting. Production callers must renew the supervisor lease and acknowledge durably recorded event pages; see the long-running protocol below.

## HTTP JSON API

All endpoints except `/health` require `Authorization: Bearer <credential>`.
Supervisor credentials can manage sessions; an agent credential can only inspect,
submit, cancel, and read events for its own agent path. There is no observer
control credential. Responses never contain credentials.

| Method / path | Request | Response |
| --- | --- | --- |
| `GET /health` | — | Protocol version 3 and negotiated capabilities, process memory, configured limits, and per-session buffer/RSS/lease/fault metrics. |
| `POST /v1/sessions` | `{agent_id, username?, run_id?, world_epoch?, paused?, lifecycle?, max_background_jobs?, remaining_run_ms?}` | Session descriptor, after the bot has spawned. Default run is `development`; default paused is false. Supervisor only. |
| `GET /v1/sessions` | — | `{sessions:[...]}`. Supervisor only. |
| `GET /v1/sessions/:agent` | — | Session descriptor including `connected`, `runtime_epoch`, active execution, and cursor. |
| `POST /v1/sessions/:agent/pause` | `{}` | Strict: pause admission/queued dispatch. Paper-aligned: fence all foreground/background work and confirm worker removal. Supervisor only. |
| `POST /v1/sessions/:agent/heartbeat` | `{run_id, world_epoch, session_id?}` | Renew a live supervisor lease. Supervisor only; an expired lease returns 409. |
| `POST /v1/sessions/:agent/events/ack` | `{run_id, world_epoch, session_id?, cursor}` | Idempotently prune durably recorded events through `cursor`. Supervisor only. |
| `POST /v1/sessions/:agent/resume` | `{}` | Resume admission and queued dispatch. Supervisor only. |
| `DELETE /v1/sessions/:agent?after_cursor=0&retain=true&session_id=...` | — | Fence work and return a bounded event page; retain closed session until durable ACK and a final DELETE without `retain=true`. Supervisor only. Legacy deletion without `retain` can return `deleted:false` when more pages remain. |
| `POST /v1/sessions/:agent/programs` | `{request_id, source, run_id, world_epoch, session_id?, request_sequence?, timeout_ms?}` | Execution record, HTTP 202; stale run/epoch or paused session returns 409. |
| `GET /v1/sessions/:agent/programs/:id` | — | Execution record. |
| `POST /v1/sessions/:agent/programs/:id/cancel` | `{}` | Execution record, after any active worker has exited. |
| `GET /v1/sessions/:agent/jobs` | — | `{session_id,world_epoch,runtime_epoch,jobs:[...]}`. |
| `GET /v1/sessions/:agent/jobs/:id` | — | Registered task record. |
| `POST /v1/sessions/:agent/jobs/:id/cancel` | `{}` | Cooperative cancellation, then hard reset after at most 1000 ms if unconfirmed. |
| `GET /v1/sessions/:agent/events?after_cursor=0&limit=100` | — | `{session_id,events,next_cursor,has_more,acked_cursor,reliability_fault,dropped_events}`; max limit 1000 plus byte cap. A pruned cursor returns 410. |

Session descriptors include a unique `session_id`, lease and buffer metrics, `reliability_fault`, `agent_id`, `username`, `run_id`, `paused`, `mode`, `world_epoch`,
`runtime_epoch`, `connected`, `active_execution_id`, `queued`, and `event_cursor`.
The supervisor can configure a Minecraft username at creation; agents cannot
change it. With `ALLOWED_AGENT_ID`, neither role can choose another identity.
Every program requires the matching explicit `run_id` and a nonnegative integer
`world_epoch` equal to the session epoch, including epoch 0. Read the session
descriptor to observe them; do not refresh them automatically to resubmit an old
plan after recovery. A stale in-flight request cannot act on a recreated session
with a newer epoch or a new run UUID that reuses epoch 0. Neither identity is a
secret. Create sessions with `paused:true` to connect observers/bots before
collecting the initial evaluation baseline, then resume admission. Agents cannot
unpause their own session. Session
deletion returns the requested event tail after closing workers and terminating
queued/running programs, so the caller must persist that tail before discarding
the transport cursor or recreating the session.

Program execution states are `accepted`, `running`, `succeeded`, `failed`,
`timed_out`, `cancelled`, or `interrupted`. Records include `execution_id`,
`request_id`, timestamps, `result`, `error`, `logs`, `before`, `after`, `cleanup`,
epochs, and `reset_reason`. A JavaScript `succeeded` result does not indicate an
experiment task was achieved. Inventory and position from this API are client
observations, not privileged evaluator evidence.

Reusing a `request_id` with identical source and deadline returns the same record;
different contents produce HTTP 409. Programs run in one foreground FIFO per bot.
Separate bots do not block each other. HTTP disconnects do not replay or cancel a
program. This Node ledger is in memory; the Python gateway persists accepted
submission intent and resolves unknown outcomes without replay after a service
restart. Deleting a session deliberately destroys this in-memory ledger; retain
the gateway's execution records.

Events have monotonically increasing cursors, parent-received timestamps,
`world_epoch`, `runtime_epoch`, `run_id`, `mode`, provenance, and optional execution
ID. Ignore events from another run when calculating this run's metrics or player
messages; retain them separately for orphaned-session audit if collected during
fencing.
`program_log` is an `agent_claim`; `bot_chat` is a client event. The event payload
for chat is `[username,message,...]`. Both records and events label fake mode;
fake chat is broadcast across fake sessions only to test transport, and provides
no Minecraft performance or task-success evidence.

`execution_accepted` carries the exact `source` text, its SHA-256 `source_hash`,
`request_id`, and `timeout_ms` before dispatch. Polling the event feed therefore
captures programs submitted directly by an external authenticated `mc` CLI as
well as programs submitted through the Python gateway. The separate idempotency
fingerprint includes both source and deadline; `source_hash` hashes only source.
Terminal execution events include `payload.record`, so an external CLI's full result and truncation metadata can be committed before acknowledging its event. The agent can mutate the native bot object, emit client events, and change its
logging behavior. Console output, return values, before/after snapshots, and
`bot_client` events must never be used as authoritative task-success evidence.

## JavaScript interface and cleanup

The `source` is an async function body with `bot`, allowlisted `require`,
`console`, persistent `globalThis`, `isCancelled`, and `jobs` arguments. A persistent
`globals` dictionary is also available as a convenience namespace. Standard
JavaScript intrinsics plus managed `setTimeout`, `setInterval`, `setImmediate`,
and their clear functions are available. The module allowlist contains `vec3`,
`minecraft-data`, and `mineflayer-pathfinder`. Paper-aligned mode also exposes pure
the installed `mineflayer` package and Node modules `assert`, `buffer`, `crypto`, `events`, `path`, `querystring`,
`string_decoder`, `url`, `util`, `zlib` (bare and `node:` names). Health/session
descriptors publish the exact module policy. Module selection is not the security boundary.

Example observation body:

```js
globalThis.observationCount = (globalThis.observationCount ?? 0) + 1;
return {
  count: observationCount,
  position: bot.entity.position,
  inventory: bot.inventory.items().map(item => ({name: item.name, count: item.count}))
};
```

In strict mode, timers and bot event listeners created during a program are removed on return.
Native action promises are tracked, and outstanding native actions or movement
force a process reset even if the async body returned successfully. Common
Mineflayer action methods and `pathfinder.goto` are instrumented; arbitrary native
objects, module APIs, and VM escape routes are not a complete resource-security
boundary. Use only explicitly registered background work in paper-aligned mode. The parent owns the deadline:
an infinite synchronous loop cannot block the watchdog. Timeouts and strict active
cancellation kill the worker process group and await exit before dispatching the
next program. Every reset explicitly reports globals loss. Pure data/function
globals survive ordinary executions. An error without outstanding work preserves
the worker.

## Isolation limits

Running this service on the host executes untrusted generated code with host
permissions. It is an explicitly unsafe development mode. `node:vm` organizes
persistent globals; it is **not a security sandbox**. The negative tests include a
real VM escape and demonstrate that runtime/model credentials are absent from the
worker environment, rather than asserting that code cannot escape VM globals.

Paper-aligned Minecraft requires a **separate generated-worker container** via
`worker-host.js`; the HTTP control process never shares its PID/container identity.
The fixed launcher chooses the image/network/username, supplies no secrets, mounts
no host directories or Docker socket, and uses a read-only root, UID 1000, dropped
capabilities, memory/CPU/PID limits, and a native seccomp filter installed before
Node initialization. The filter denies disk mutation and process creation while
allowing Node threads and reads. It is layered with container isolation and the
fixed identity proxy, not offered as a standalone hostile-code sandbox. The
launcher is trusted infrastructure and has Docker authority; keep its token and
socket inaccessible to generated programs. A separate launcher guardian removes
its labelled containers if the launcher itself dies or stops sending pulses.

`worker-host.js` uses `WORKER_AGENT_ID`, `WORKER_HOST_ID` (stable across restart),
`WORKER_HOST_TOKEN`, `WORKER_BIND/PORT`, `WORKER_SANDBOX_IMAGE`, `WORKER_NETWORK`, and
`WORKER_MC_HOST/PORT`, and `WORKER_IDENTITY_PROXY=true` for a real identity-bound deployment. For real games, the network must contain only this worker
and its identity-bound game proxy; direct Vanilla access is prohibited. Startup
removes orphan containers with the exact launcher label before accepting work.
Control disconnection and heartbeat expiry remove the sandbox; the control facade
only reports exit after launcher confirmation. Inability to confirm removal must
block recreation. Fake local mode remains an explicitly unsafe diagnostic path.

Build and probe without Vanilla or model calls:

```sh
cc -Wall -Wextra -Werror -shared -fPIC -O2 -o infra/.cache/sandbox.so infra/sandbox.c
docker build --network=none -f infra/worker-sandbox.Dockerfile -t minecraft-harness-worker:research .
node bot_runtime/scripts/remote-smoke.js
```

Build the shared library on the target Linux architecture; production builds
should use a compatible compiler/libc toolchain and freeze image digests.

## Registered jobs and asynchronous submission

In `paper_aligned`, `jobs.start(fn,{ttl_ms})` returns a UUID. Each task receives a
separate tracked scope; registered listeners/timers survive foreground return.
The function may return after registering listeners/timers, or await its own loop.
A finished function with no remaining resources becomes `succeeded`. Example:

```js
globals.job = jobs.start(async () => {
  while (!isCancelled()) {
    console.log(bot.health);
    await bot.fake.wait(100); // fixture only; real code uses its own observations
  }
}, {ttl_ms: 10000});
return globals.job;
```

`jobs.status(id)`, `jobs.list()`, and `jobs.cancel(id)` operate on registered tasks.
`isCancelled()` reads the current foreground/task scope dynamically. A cancellation
is cooperative for at most 1000 ms; unconfirmed native work or a blocked event loop
forces removal of the entire worker, marks all affected tasks, and loses globals.
TTL is a positive integer bounded by the original remaining run deadline; worker
recovery does not renew the deadline or replay jobs. Task logs and transitions
carry the creator execution, task UUID, session, and epochs. Records/logs are
bounded and terminal records are pruned independently of the durable event ledger.
Paper-aligned pause fences background work as well as foreground work. Confirmed
worker exit advances the authoritative runtime epoch immediately, so a queued
notification cannot hide globals loss until the next program starts. Tracked
movement controls and pathfinder goals carry scope ownership: foreground cleanup
stops its own controls while registered background controls remain active, and
task cancellation stops that task's controls.

Python `submit()` durably admits and returns immediately; `wait()` expires without
cancelling; `execute()` remains the synchronous compatibility helper. The `mc`
commands `submit`, `exec`, `status`, `cancel`, `jobs`, and `job-cancel` use a
player-scoped Supervisor broker. Its Unix socket can be mounted into an otherwise
network-disabled workspace (`MC_RUNTIME_SOCKET`); credentials authorize only that
player. Direct paper-aligned CLI submission to raw Node is rejected. Model tools
and CLI share the same Recorder reservation/sequence/terminal ledger. Raw Node
credentials must never be passed to workspaces.


## Long-running protocol and failure behavior

`GET /health` advertises `protocol_version:3`; strict clients may still connect to legacy v2 runtimes, while paper-aligned clients require v3 background capabilities. Keep one independent supervisor
maintenance task per gateway alive while models think, shell programs run, and
Minecraft connects. Renew each session with `POST .../heartbeat` at most one third
of `lease_timeout_ms` apart. Heartbeats require the supervisor credential, matching
run/epoch, and should include `session_id`. Lease timers and guardian pulses use a
monotonic clock. Expiry pauses admission, interrupts queued/running executions,
and kills the worker group. A late heartbeat or resume cannot revive that session;
record its final audit events and recreate it under the experiment recovery policy.
Self-owned local fixtures can set `EXIT_ON_LEASE_EXPIRY=true` so an orphaned HTTP
process exits naturally after all leases expire and all workers are fenced.
Uncollected tail outcomes then remain unknown; clients must never promote them to
success. Keep the default false for externally managed containers when retaining
that final audit tail for recovery is required.
A temporary missed HTTP response is different from an observed `lease_expired` or
`reliability_fault`; do not claim that unknown execution outcomes succeeded.

Event cursors remain monotonic when entries are pruned. The recorder must commit
an entire received event page, any derived player events, source/terminal records,
and the transport cursor in one durable transaction **before** sending
`POST .../events/ack`. Retrying an ACK is safe. `after_cursor == acked_cursor` is
valid; an earlier cursor returns 410 rather than silently starting from a newer
point. ACKs and submissions containing a stale `session_id` return 409, even when
run/epoch were reused. Every event and execution record carries `session_id`.

Source admission returns 429 before accepting a program when its source event cannot fit. Events are never overwritten to make room for new events. Exhausting their count
or byte limit permanently sets `reliability_fault:event_buffer_overflow`, records
the first lost kind and loss count in descriptors/feeds, stops dispatch, and fences
the worker. Existing retained history remains readable. A faulted session cannot
resume after an ACK: the run must report the audit gap and follow its explicit
recovery policy. This bounds memory while making audit loss visible. A JavaScript
body finishing at the overflow boundary is still not proof of a valid experiment.

After ACK, old terminal records exceeding `MAX_RETAINED_EXECUTIONS` can be evicted.
Unacknowledged records are protected; further admission returns 429 when retention
or the queue is full. Clients should persist increasing positive safe-integer
`request_sequence` values before submission. Old, evicted sequences return 410;
retained duplicates return their original record, and conflicting contents return
409. V2 execution identity is `(session_id, request_sequence)`; the durable client
ledger owns request ID uniqueness across eviction. Sequence-based submissions do
not populate the legacy Bloom filter. For v1 request IDs without sequences, a fixed 1 MiB Bloom tombstone filter
prevents replay after eviction. Its conservative false positives can reject new
IDs with 410, and saturation can eventually require a new session; it cannot
silently execute a retired ID twice. Request IDs and execution results remain
available in the recorder after runtime eviction.

Production shutdown uses two phases, always with the expected `session_id`:

1. `DELETE ...?retain=true&after_cursor=C&session_id=ID` fences code, confirms the
   old worker exited, and keeps the closed session available for retries.
2. Persist each tail page durably, ACK it, and repeat from `next_cursor` while
   `has_more` is true. The closed audit tail is immutable.
3. `DELETE ...?after_cursor=FINAL&session_id=ID` deletes the closed session after
   its entire tail is ACKed. A previously retained session returns
   `awaiting_ack:true,deleted:false` if any events remain unacknowledged.

Legacy DELETE without `retain=true` preserves v1 behavior and can lose the returned
final tail if its HTTP response is lost. It is unsuitable for production recording.
A delayed DELETE carrying an old `session_id` cannot close its replacement.

An independent guardian process monitors each detached worker group. Runtime
SIGKILL, IPC disconnect, an unresponsive runtime event loop, or Linux worker RSS
exceedance kills the whole group even if generated JavaScript is in a synchronous
infinite loop. The runtime also kills surviving group descendants when a worker
leader exits unexpectedly. This is process cleanup, not a security sandbox:
escaped code can create a new process session, interfere with same-UID processes,
or use native memory outside the intended API. Container cgroup and isolation
limits remain required. Linux worker RSS and runtime `memory` fields in health
are operational measurements; they are not environment success evidence.

HTTP bodies are limited to 1 MiB, source to 512 KiB (or a smaller response-derived
cap), headers/body/socket lifetimes are bounded, and event responses are paginated
by count and bytes. Return serialization avoids getters and `toJSON`, limits depth,
entries, strings, and bytes, and reports `output_truncated`. Circular structures,
accessors, and oversized data use explicit markers. Console output is bounded by
message count, bytes, and IPC pressure; terminal `cleanup` includes
`logs_suppressed`, `logs_truncated`, and `log_bytes`. Flooding non-log IPC events
terminates the worker rather than building an unbounded queue. Arbitrary proxies
can still block the disposable worker during serialization, so the parent deadline
and independent guardian remain authoritative.

## Offline endurance exercise

`npm run test:soak -- --cycles 10000 --agents 4 --fault-every 200` executes 40,000
programs with cross-bot chat, logs, periodic injected synchronous infinite loops,
reconnects, event ACKs, and terminal eviction. `--duration-seconds 3600` additionally
runs for at least one hour; the cycle and duration requirements are both minima.
It never starts Minecraft or accepts an EULA. The JSON summary includes execution
and fault counts, elapsed time, sampled RSS/heap, cache counts, and configured
limits. Its ephemeral ACKs exercise transport only; production runs must use the
durable recorder protocol. Short automated tests additionally exercise parent
SIGKILL, worker descendant cleanup, lease expiry, overflow, bounded output, stale
session fencing, and ACK retry/pruning. These checks establish transport and
lifecycle behavior, not Minecraft gameplay performance or a 24-hour soak result.
Observed measurements from the 40,000-program, 200-timeout exercise are retained in
`validation/2026-10-21-offline-soak.json`; subsequent sequence/exit changes are
covered by the automated fault tests.

For a container limited to 512 MiB total memory, explicitly configure
`MAX_WORKER_RSS_BYTES=268435456` (256 MiB), leaving room for the HTTP process,
guardian, dependency loading, and measurement delay. The standalone 512 MiB worker
default requires a larger total process/container budget; a cgroup can otherwise
OOM-kill the container before its worker guardian acts. An external runtime whose
HTTP parent is unreachable cannot certify that the old worker stopped. The Python
gateway therefore pauses/fails safely until the runtime or its container operator
restores a provable fence. A connected parent can recover an individual failed bot
worker automatically. Self-owned local fake runtimes instead carry a non-secret
`RUNTIME_INSTANCE_ID` in the parent/worker/guardian environment; the gateway
persists this marker and session identities before/after process creation and
confirms their local process groups are gone before recreating them. Credentials
are never saved in that recovery state.

## Host RPC access with internal game networks

Docker internal networks can suppress published-port NAT. The deployment therefore
publishes only a separate fixed TCP relay: host `127.0.0.1:3101..3104` maps to
relay listeners `3001..3004`, which connect only to `bot1..bot4:3001` respectively.
Each bot remains solely on its internal game network, with no default external
route. Only the relay joins the ordinary RPC network plus those internal networks.
The relay carries no runtime tokens, model credentials, or world mounts; endpoint
bearer authentication remains enforced by each bot runtime. It does not accept
CONNECT requests, URLs, destination headers, or runtime target configuration.

`infra/rpc-relay.Dockerfile` runs the dependency-free `src/rpc-relay.js` as the
unprivileged `node` user. `RELAY_ENABLED_AGENTS` is a comma-separated subset of
`bot1,bot2,bot3,bot4` (default all four); an absent bot profile fails its connection
closed. Listener ports and upstream names/ports remain fixed. Limits are
`RELAY_MAX_CONNECTIONS=128` across all listeners, `RELAY_MAX_PER_ROUTE=32`,
`RELAY_CONNECT_TIMEOUT_MS=5000`, and `RELAY_IDLE_TIMEOUT_MS=90000`.
Bidirectional pipe backpressure bounds buffering, preserves TCP half-close, and
cleans both sockets on errors, idle expiry, connection expiry, and shutdown.
SIGTERM/SIGINT close listeners and existing sockets. A disconnected RPC stream
still leaves action outcomes uncertain; it never authorizes a replay.

Python session creation has a separate `runtime.startup_timeout_s` deadline
(default 60 seconds), including its HTTPX read timeout, because real Mineflayer
spawn may take up to the Node startup deadline (45 seconds by default). Ordinary
RPC requests retain their shorter deadline; heartbeat requests have an independent
short lease-derived deadline and cannot wait behind a session's journal drain.

The Python descriptor check is reproducible with
`python -W always::ResourceWarning scripts/gateway_fd_check.py` from the harness
root. It performs 12 local start/close cycles and eight real Node SIGKILL/recovery
cycles, without forcing garbage collection or filtering warnings, and writes
`validation/gateway-fd.json`. Some complete Python test-suite runs have emitted
unclosed socket/transport `ResourceWarning` messages during isolated event-loop
teardown. Focused gateway runs with allocation tracing did not reproduce them;
the descriptor exercise checks whether live descriptors accumulate and whether
shutdown returns to its initial count. The remaining full-suite warning origin
is unresolved; bounded descriptor measurements do not prove that every third-party
transport cleanup path is free of defects.

## Container protocol acceptance without a Vanilla server

`node bot_runtime/scripts/isolated-protocol-smoke.js` creates two temporary,
labelled internal networks and runs the real Mineflayer client through the real
identity proxy into a deterministic protocol fixture. It verifies chunk/inventory
decoding, raw inbound and outbound chat ablation, foreign-login rejection before
backend contact, native write/exec denial, timeout replacement and cleanup. Both
server/proxy fixtures use only read-only mounts of trusted test scripts; generated
workers retain zero mounts. The fixture supplies no Minecraft capability evidence.

On this host, Mihomo's stale local-address set intercepts newly allocated Docker
peer traffic. The optional **test-only** `--temporary-nft-exemption` flag requires
root and adds only member IPv4 addresses inspected on the two newly owned
networks to the existing `inet mihomo inet4_local_address_set`; every added
address is removed in `finally`. It never changes routes, chains or permanent
policy. Without that flag, blocked connectivity remains a blocked result. The
captured `validation/2026-10-21-isolated-protocol.json` records seven passing
checks, proxy counters (two accepted reconnects, one forged rejection, 19 filtered
chat packets), zero backend chat messages, and confirmed removal of all temporary
network/container/set changes. Real Vanilla and paid-model acceptance remain
separate.


## Rebuild trusted control and launcher images

The launcher OS/Docker client base is separate from runtime sources. Updating a
JavaScript file therefore requires only offline COPY layers, with no apt access:

```sh
# Initial base refresh requires the configured trusted package network:
docker build -f infra/launcher-base.Dockerfile -t minecraft-harness-launcher-base:docker-cli-20261021 .
# Subsequent source rebuilds are offline:
docker build --network=none -f infra/control.Dockerfile -t minecraft-harness-control:research .
docker build --network=none -f infra/launcher.Dockerfile -t minecraft-harness-launcher:research .
cc -Wall -Wextra -Werror -shared -fPIC -O2 -o infra/.cache/sandbox.so infra/sandbox.c
docker build --network=none -f infra/worker-sandbox.Dockerfile -t minecraft-harness-worker:research .
uv run python scripts/image_manifest.py
```

For this delivery the already installed Docker-client launcher image
`sha256:2af598098ffef2ea54771c7e75a6ff38e00c4b5c3ca64d74ff9c339f82c0c090`
was preserved as the local launcher base; no packages were downloaded during the
final source rebuild. `validation/paper-images.json` stores actual image IDs,
compiler/recipe/input hashes, and an independently measured JavaScript source
hash inside each control/worker/launcher image. It also checks the workspace CLI
against current Python source. A source mismatch fails manifest generation.
