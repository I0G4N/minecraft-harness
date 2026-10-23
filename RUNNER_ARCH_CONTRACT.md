# Same-host independent runner architecture — implementation contract

Status: implementation in progress. The paper experiment is paused; do not start or
resume any experiment. Do not touch `minecraft-harness/mc-runs/four-agent-paper-*`
or the running `mc-paper-run` / `minecraft-paper-*` containers.

Repository root (authoritative source): `/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness`.

Tests run in a disposable container because the sandbox user cannot use the host
venv symlink directly:

```bash
docker run --rm \
  -v /ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness:/repo \
  -v /home/swarmbrain:/home/swarmbrain:ro \
  -w /repo minecraft-harness-launcher-base:docker-cli-20261021 \
  /repo/.venv/bin/python -m unittest tests.test_runner_rpc -v
```

Do not edit `src/minecraft_harness/{agents,observer,config,supervisor}.py` unless your
task explicitly owns it. The Lead owns those files.

## Goal

Supervisor/control plane keeps ledger, budget, memory, gateway, workspaces, evaluator,
server control, dashboard. Five independent OS processes (4 players + Observer) each run
one `AgentLoop` / `ObserverAgent` with their own model client and context. Runners access
central services only through an authenticated local Unix-socket RPC. Runner processes
receive no bot runtime tokens and never import `Supervisor`.

## RPC wire protocol

Unix stream socket. Every frame is:

```
4-byte unsigned big-endian length | UTF-8 JSON object
```

Maximum frame payload 512 MiB (header excluded). Reject over-large frames and close.

Client handshake is the first frame of every connection (clients may open a fresh
connection per request; no persistent state is required):

```json
{"id": 0, "method": "hello", "params": {"token": "<run token>", "agent_id": "player1", "role": "player"}}
```

Server replies `{"id": 0, "ok": true, "result": {"agent_id": "...", "role": "..."}}` or an
error frame and closes.

After handshake, a request frame:

```json
{"id": 17, "method": "recorder.append", "params": {...}}
```

Success response:

```json
{"id": 17, "ok": true, "result": {...}}
```

Error response:

```json
{"id": 17, "ok": false, "error": {"type": "BudgetExhausted", "message": "...", "retryable": false}}
```

`id` is an integer >= 1 within a connection. Handshake `id` is 0. Invalid frames close
the connection. One request is in flight per connection at a time.

Server must authenticate the token against `tokens: dict[str, {"agent_id": str, "role":
"player"|"observer"}]`, bind the connection to that identity, and ignore client-supplied
identity overrides. Server must enforce per-method role checks (handler responsibility).

### `src/minecraft_harness/runner_rpc.py`

Public API (exact names):

```python
class RpcError(RuntimeError):
    error_type: str
    retryable: bool
    # str(error) is the server message

class RpcServer:
    def __init__(self, socket_path, tokens, handler, *,
                 max_frame_bytes=512 * 1024 * 1024, backlog=64): ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def cancel_agent(self, agent_id: str) -> int: ...
    # handler: async def handler(agent_id, role, method, params) -> Any

class RpcClient:
    def __init__(self, socket_path, token, agent_id, role,
                 *, max_frame_bytes=512 * 1024 * 1024, connect_timeout_s=10.0): ...
    def call_sync(self, method: str, params: dict | None = None,
                  *, timeout_s: float | None = None) -> Any: ...
    async def call(self, method: str, params: dict | None = None,
                   *, timeout_s: float | None = None) -> Any: ...
```

Requirements:

- `RpcServer.start()` creates the parent directory if needed (mode 0700), unlinks a stale
  socket only when `lstat` says it is a socket (never a symlink/regular file), binds with
  socket mode 0600, serves concurrent connections.
- Track per-agent in-flight `handler` tasks; `cancel_agent` cancels them (used when a
  runner dies so long `gateway.execute` / shell handlers do not block the restarted
  runner). `close()` cancels all connections/handlers, closes the server, and unlinks the
  socket.
- If the client disconnects while its handler is still running, the handler task must be
  cancelled (EOF watcher). No request pipelining is supported; a non-EOF byte arriving
  before the response is a protocol error.
- `RpcClient.call_sync` uses `socket.AF_UNIX`, one fresh connection per call, and must be
  usable from inside an asyncio event loop (it is only called by the runner process, whose
  calls are short). `call` uses `asyncio.open_unix_connection`; it must not impose a read
  timeout unless `timeout_s` is given, because shell/gateway calls can legitimately run
  for hours.
- Errors: server converts any handler exception to an error frame. Known exception type
  names must be preserved (`BudgetExhausted`, `ModelAdmissionError`, `StorageLimitError`,
  `RuntimeUnavailable`, `RuntimeReliabilityError`, `MemoryConflict`,
  `MemoryPermissionError`, `PermissionError`, `FileNotFoundError`, `ValueError`,
  `TimeoutError`); set `retryable` from an attribute when present. Clients raise
  `RpcError`. The runner maps `RpcError` back to the real local exception classes.
- Thread-safe construction is not required; clients are per-process. The sync and async
  clients must not share sockets.

### Test requirements for `tests/test_runner_rpc.py`

Use `unittest.IsolatedAsyncioTestCase` where appropriate and `tempfile.TemporaryDirectory`.
Cover: sync roundtrip; async roundtrip; handshake auth rejection; handler exception type
preserved; 2 MiB payload; concurrent async calls from one client; `cancel_agent` cancels
an in-flight handler; server close unlinks socket; stale non-socket at socket path is not
unlinked (server raises).

## Runner process contract

### `src/minecraft_harness/runner.py`

Invoked by the supervisor as `sys.executable -m minecraft_harness.runner` with a scrubbed
environment. Reads:

- `HARNESS_RUNNER_SOCKET` — control-plane Unix socket path
- `HARNESS_RUNNER_TOKEN` — per-run/per-agent token
- `HARNESS_RUNNER_AGENT` — configured agent id (`player1`..`player16` or `observer`)
- `HARNESS_RUNNER_ROLE` — `player` or `observer`
- `HARNESS_RUNNER_SPEC` — path to a JSON spec file (mode 0600)

Spec JSON:

```json
{
  "schema_version": 1,
  "run_id": "hex",
  "agent_id": "player1",
  "role": "player",
  "state_root": "/abs/run/agents/player1",
  "shell_enabled": true,
  "heartbeat_s": 10,
  "agent_config": { "...": "same dict the in-process AgentLoop receives, including agent_id, peers, goal, profile, lifecycle, web, game_chat, chat_delivery, model_request_timeout_s, program_timeout_ms, leftover_policy, paper_* flags, context_chars, max_* fields" },
  "model": { "...": "config['model'] copy, including api_key_env; never an inline key" }
}
```

The runner must:

1. Build `RpcClient` and remote service proxies (see below).
2. Build `minecraft_harness.models.build_model(spec["model"])`; never receive bot tokens,
   the ledger path, memory DB path, workspace host path, or Docker access.
3. Build an `AgentLoop` (player) or `ObserverAgent` (observer) with the Lead-extended
   constructor keyword arguments:
   - `state_root=Path(spec["state_root"])`
   - `workspace=RemoteWorkspace(...)` (an object exposing `shell_enabled`, `shell_container`,
     `shell_env` plus `read/write/remove/list` sync and `shell` async)
   - `artifact_api=RemoteArtifactAPI(...)`
   - `AgentLoop(config, run_dir, recorder, executor, model, budget, memory, *, state_root=..., workspace=..., artifact_api=...)`
   - `ObserverAgent(config, run_dir, recorder, model, budget, infra_callback=RemoteInfraCallback(...), *, state_root=..., workspace=..., artifact_api=...)`
   - Pass `run_dir=Path(spec["state_root"])`; only `state_root` is used for checkpoint/
     history storage once the Lead change lands.
4. Send `runner.ready` with `{agent_id, role, pid, prompt_sha256, tools_sha256}` where the
   hashes are `sha256` of the system prompt text and of `json.dumps(schemas,
   sort_keys=True)`. If the call fails, exit non-zero.
5. Run a heartbeat task every `heartbeat_s` seconds calling `runner.heartbeat` with
   `{turns, status, rss_bytes}`; swallow transport errors; stop on shutdown.
6. Run the agent. On completion send `runner.finished` with `{"result": <agent result
   dict>}`. On SIGTERM/SIGINT cancel the agent task, await checkpoint/cleanup, send
   `runner.finished` with `{"result": {"agent_id": ..., "status": "stopped", "turns": ...}}`
   when possible, then exit 0. Unhandled startup failure: print a short JSON error to
   stderr, exit 2.
7. Never write to stdout except one final JSON line as a fallback when
   `runner.finished` cannot be delivered: `{"__runner_result__": {...}}`.

Remote proxies (all defined in `runner.py`, constructed from one `RpcClient`):

- `RemoteRecorder`: `append(kind, payload, **kwargs)` sync; `put_artifact(data) ->
  {"hash":..., "sha256":..., "path":...}` sync; `read_events(after=0, limit=1000)`
  sync; `latest_event_seq()` sync. Put artifacts as UTF-8 text when possible, else
  base64: RPC params `{"text": ...}` or `{"base64": ...}`.
- `RemoteBudget`: `async validate_request(request_bytes)`, `async reserve(agent_id, *,
  purpose="agent_decision", role="player")`, `async settle(reservation_id, usage)`,
  `async stop_requested()`, `admission` object with `async wait()`, and **sync**
  `admit_web(agent_id, tool) -> bool` (the WebTools admission hook is synchronous; the
  server must use the bound runner identity). The signature of `reserve` must include
  `purpose` (AgentCore introspects it). `stop_requested` must be `async def` (AgentCore
  awaits awaitables).
- `RemoteMemory`: sync `read/write/list/search` forwarding `actor_id=self.agent_id`.
- `RemoteWorkspace`: attributes `shell_enabled` (from spec), `shell_container="rpc-broker"`
  when enabled else `None`, `shell_env={}`; sync `read/write/remove/list`; async `shell`.
- `RemoteExecutor`: async `execute/submit/status/cancel/wait/jobs/cancel_job/read_events`;
  every call passes `agent_id`; `read_events(agent_id, after_cursor=..., limit=...)`.
- `RemoteArtifactAPI`: sync `grant(agent_id, digest)`, `owned(agent_id, digest) -> bool`
  (cache granted digests locally to avoid repeated RPC), `read(agent_id, hash, offset,
  limit)`.
- `RemoteInfraCallback`: async `__call__(issue)` -> RPC `supervisor.observer_infra_issue`.

Error mapping: map `RpcError.error_type` to the local class (`minecraft_harness.budget.
BudgetExhausted`, `agents.core.ModelAdmissionError`, `recorder.StorageLimitError`,
`execution.gateway.RuntimeUnavailable/RuntimeReliabilityError`, `memory.store.
MemoryConflict/MemoryPermissionError`, `PermissionError`, `FileNotFoundError`,
`ValueError`, `TimeoutError`), re-raise with the original message. `retryable=True`
errors map to a `ConnectionError` subclass only when no better class exists, so agent
retry logic sees `retryable`; prefer `RuntimeUnavailable` for gateway calls.

Runner log/state: only `state_root` is written locally. The runner must not import
`supervisor`, `recorder.Recorder`, `memory.MemoryStore`, or `workspaces`.

### Runner RPC methods handled by the control plane

- `runner.ready`, `runner.heartbeat`, `runner.finished`: handled by `RunnerManager`.
- `recorder.append(kind, payload, kwargs)`, `recorder.put_artifact({text|base64})`,
  `recorder.read_events(after, limit)`, `recorder.latest_event_seq`
- `budget.validate_request(request_bytes)`, `budget.reserve(purpose, role)`,
  `budget.settle(reservation_id, usage)`, `budget.stop_requested`,
  `budget.wait_admission`, `budget.admit_web(agent_id, tool)`
- `memory.read/write/list/search` (server forces `actor_id` to the bound agent)
- `workspace.read/write/remove/list/shell`
- `artifact.grant(hash)`, `artifact.owned(hash)`, `artifact.read(hash, offset, limit)`
- `gateway.execute/submit/status/cancel/wait/jobs/cancel_job/read_events`
- `supervisor.observer_infra_issue(issue)` (observer only)

The control plane (`RunnerManager` / `ServiceHub` in `runners.py`) is owned by the Lead.
`runner.py` only needs to speak these methods.

### Frozen source snapshot and RPC bounds

At `RunnerManager.start()`, before container validation or any spawn, the control
plane freezes the harness package into `<run_dir>/source-snapshot/src`, records a
deterministic `source_sha256` in `<run_dir>/source-snapshot.json` plus a
`source_snapshot_frozen` ledger event, and uses that copy as the runner import
tree (container bind source; process `PYTHONPATH`). On resume/restart the snapshot
is re-verified and changed/missing content fails closed with `RunnerControlError`;
a live tree edit never changes a restarted runner's code. The live `.venv`
site-packages and `bot_runtime` paths are unchanged.

Every `RpcClient.call`/`call_sync` has a default bound from
`runtime.runner_rpc_timeout_s` (default 120 s; propagated to containers through
`HARNESS_RUNNER_RPC_TIMEOUT_S`). Long-running game operations
(`gateway.execute/submit/wait`, `workspace.shell`, `budget.wait_admission`) are
explicitly exempt because their own tool/pause timeouts govern them. A timeout is
reported as retryable `RpcError("TimeoutError")`.

## Pi RPC runtime (`runtime.runner_runtime=pi`)

Optional agent runtime; `python` (the custom `AgentCore`/ObserverAgent runners)
remains the default. When configured, `RunnerManager` launches
`minecraft_harness.pi_runner` for players and `minecraft_harness.pi_observer_runner`
for the Observer. The Python observer fallback remains available when
`runner_runtime=python` or inprocess.

`pi_runner.py` reads the same `HARNESS_RUNNER_*` environment plus a spec with these
additional keys: `runner_runtime` ("pi"), `pi_cli` (absolute CLI bundle path),
`pi_context_window`, `workspace_root`, `reference_root`, `team_memory_root`,
`broker_socket`, `cli_environment` (own `MC_*` game-CLI values) and `pi_model`
(model config copy; an inline `api_key` is rejected). Optional env:
`HARNESS_PI_CLI`, `HARNESS_PI_CONTEXT_WINDOW`, `HARNESS_NODE`; container runners
also get `HARNESS_RUNNER_RPC_TIMEOUT_S` and the pinned `pi_runtime` tree mounted
read-only at the same absolute path.

Control protocol: `runner.ready` with `prompt_sha256` of the Pi system prompt
(paper v4 prompt + Pi environment appendix) and `tools_sha256` over
`{"tools": ["read", "write", "edit", "bash", "memory_read", "memory_write",
"memory_list", "memory_search", "web_search", "web_fetch"]}`; heartbeat every `heartbeat_s`
with `{turns, status, rss_bytes, runtime: "pi"}`; `runner.finished` with
`{agent_id, status, turns, reason, pi_returncode, budget_denials,
upstream_errors}`. SIGTERM/SIGINT abort Pi and finish `status="stopped"`.
Pi JSONL events become durable ledger events: `agent_started`, `tool_intent`
(tool, call_id, args_sha256, idempotency), `tool_result` (+ result artifact),
`context_compacted`, `agent_turn`, `agent_error`, `agent_stopped`; the proxy adds
`model_request`/`model_response` carrying reservation ids. Sessions live under
`<state_root>/pi-sessions` and resume with `--continue` after a restart.

`pi_model_proxy.py` binds `127.0.0.1:<random>/v1` and implements a per-attempt
OpenAI-compatible flow: bounded `budget.wait_admission` (30 s, client-disconnect
check), `budget.validate_request`, `budget.reserve(purpose, role)`, forwarding
SSE/JSON upstream, and settle exactly once. HTTP-error responses with no completion
settle zero usage; transport/aborted attempts settle conservative unknown. Budget
denial returns HTTP 429 without upstream. Consecutive upstream errors are counted;
4xx auth/bad-request is fatal and `pi_runner` ends `model_failed` at
`max_model_failures` instead of continuing. Pi continuation also stops as `stalled`
after `max_no_progress_turns` settled rounds without a successful tool result.
Request bodies/credentials are never logged; the upstream key stays in the runner
process and is not written to `models.json` or passed to the Pi child.

Pi memory/web tools are a local extension (`pi_runtime/extensions/minecraft-tools.js`)
that calls the loopback tool bridge (`pi_tool_bridge.py`, random bearer token), which
forwards validated calls to `memory.*` and `web.search`/`web.fetch` ServiceHub RPC.
The bridge is loopback-only; the token is visible to Pi bash (only grants these six
tools).

Container mounts for Pi players add to the normal runner mounts: own workspace
rw→`/workspace`, `run_dir/reference-docs` ro→`/reference`, team memory dir
rw→`/memory/team`, broker dir ro→`/run/mc-broker` when a broker environment
exists, and the pinned `pi_runtime` tree ro at the same absolute path. Container
runners use `--user <run_dir uid:gid>` (override `runtime.runner_container_user`)
so `--cap-drop=ALL` keeps access to host-owned workspace/state; frozen source
files are normalized 0644/0755 for the same reason. Still no Docker socket,
ledger/memory DB, other agents' state or bot admin token.
Process runners use the frozen source snapshot via `PYTHONPATH`, cwd is the
agent workspace and the Pi session/state stay under the runner state dir.
The source-snapshot manifest additionally hashes the Pi CLI bundle, extension,
package/lock and fails closed on a restart if they changed.

Known deviations/risks for the next paper run (accepted unless noted): memory/web
tools are extension+bridge rather than built-ins; the Pi child env still carries
the bridge token (only the six memory/web tools) and the player
`MC_AGENT_TOKEN` (required by `mc`/the game), so model-directed bash can read its
own env and call the loopback bridge directly, bypassing Pi's
`tool_intent`/`tool_result` projection (ServiceHub still records memory/reference
events) — accepted deviation, never claim complete tool-event audit from it.
The earlier `/proc/<runner>/environ` exposure of the model key/RPC token is
mitigated (`PR_SET_DUMPABLE=0`, no `--init`; only the bridge/MC tokens above
remain). File-based memory paths also exist alongside MemoryStore;
`model.extra_body` is forwarded as Pi `samplingParams`; `max_turns` bounds
continuation prompts, not Pi-internal turns; "never replay an uncertain action"
is a prompt/`mc` CLI convention rather than a harness-enforced journal; process
mode is not a security boundary for Pi bash (use container mode).
Accepted operational residuals: (a) reaching the active-session cap performs a
clean abort/rotation and records an event, but does not auto-restart in-process
and has no operator alert threshold — a player becomes
`agent_degraded`/`infra_failed`, the Observer `observer_degraded`, and the run
continues degraded, so 54 h runs require manual monitoring; (b) run-wide
retention accounting/limit across runners and restarts is not implemented yet
(task-24/P1-5 complete: ServiceHub pi-budget.sqlite3 is the trusted per-agent + global cap; model-writable markers cannot reset usage) — do not claim a run-wide
retention guarantee until it lands; (c) a fresh full container Pi/Compose smoke
was not run in this verification window (3 Docker-gated tests skipped; only a
prior-window one-shot smoke exists) — a dedicated Docker window with recorded
image IDs is required before a 54 h paid run.

Pi Observer (`pi_observer_runner.py`): role observer/agent observer; prompt is
the paper Observer prompt + appendix; tool surface is Pi built-ins `read/write`
plus extension tools `records_read`, `evidence_read`, `observer_report`,
`infra_issue` (no bash/mc/memory/web); bridge calls ServiceHub
`observer.records_read/evidence_read/report` and `supervisor.observer_infra_issue`;
review loop polls ledger important events <=1 s or `interval_s`; session retention
reuses the player Pi pruner; no team-memory/broker/MC_* mounts or env.

Tests: `tests/test_pi_model_proxy.py`, `test_pi_runner.py`,
`test_pi_runner_manager.py`, `test_pi_observer.py`, `test_observer_rpc.py`,
`test_pi_retention.py`, and host-level `test_pi_e2e.py` (real Pi CLI + loopback
fake OpenAI SSE + real RPC server; no Docker). Container-gated Pi e2e still
requires a dedicated Docker window.

## Deployment wrapper preflight

`run-paper.sh --prepare` is the safe default (no stack starts); `--start` is the
only path that runs `compose_up`. Before compose up the wrapper derives the
server container/project/token env file from the frozen config and refuses a live
experiment stack, leftover compose networks, busy 3101-3108 ports, an already
bound world, a non-empty run dir, missing EULA, or missing model key / bot
tokens. It then:

- when `runtime.runner_runtime: pi`, requires BOTH pinned extensions
  (`pi_runtime/extensions/minecraft-tools.js` and `observer-tools.js`);
- performs one bounded no-start container image probe
  (`docker run --rm --network none`, read-only mounts of the live source tree,
  the venv site-packages and the pinned `pi_runtime`). The probe verifies the
  configured `runtime.runner_container_python` can import `sys`/`httpx`/`yaml`
  and, for Pi, that `node --version` is >= 22 and the mounted Pi CLI plus both
  extensions exist. Bound: `PAPER_IMAGE_PROBE_TIMEOUT_S` (default 120 s).

If the Docker daemon is unavailable or the probe fails, preflight reports and
exits before any stack container/network is created (no mutation). It never
stops/starts `final11` or another run and never touches `mc-runs/final11`.

## Verification

Current host status (2026-10-23): full Python suite **745 passed / 3 skipped /
734 subtests**; Pi/Observer targeted **189 passed / 119 subtests**; Node **51**.
The 3 skipped tests are Docker-gated; no fresh container Pi/Compose smoke was run
in this window (only a prior-window one-shot smoke). Wrapper checks:
`bash -n run-paper.sh` plus the fake-Docker `--prepare` preflight smoke (both Pi
extensions required, image probe runs before compose up, daemon-unavailable and
probe-failure paths abort with rc=2 and no compose call).

After the Lead integrates, run in the disposable container:

```bash
/repo/.venv/bin/python -m unittest discover -s tests -q
```

Plus a fake-mode process-runner integration test. Report exact commands and results.
