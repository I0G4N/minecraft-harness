# LONGRUN ROBUSTNESS AUDIT — 2026-10-23

Read-only audit of the current experiment framework after the Pi runtime switch.
Repo: `/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness` (H).
Wrapper: `/ssd_data/caizihao/minecraft/minecraft-harness/run-paper.sh`.
Auditor: longrun-auditor. Snapshot time: 2026-10-23 ~00:40 +08; note the deployment wrapper was revised twice during the audit (three hashes in §1). **The work tree was being edited concurrently during this audit** (see §7); all findings are against the hashes in §1.

Constraints honoured: no source edits, no Docker start/stop/exec, no experiment/final11 control, no network beyond loopback, no test >10 min, no git commits. Read-only `docker ps/inspect`, read-only ledger copies to /tmp, host `.test-venv` pytest, one safe `run-paper.sh --prepare`, one loopback-only probe script in `/tmp`.

## 1. Snapshot (SHA256 at audit time)

| file | sha256 |
|---|---|
| `src/minecraft_harness/pi_runner.py` (00:11) | `0106e5e4960ac2dea78c002efdfada8cd3573455933011be88b04c9475949dc3` |
| `src/minecraft_harness/pi_model_proxy.py` (23:02) | `770c77768a2c1b6c4a091fb1d411485f5e735a4bff7ca8e3657096899c1f4783` |
| `src/minecraft_harness/pi_tool_bridge.py` (00:10) | `157290837000a082f9c59750bc3b232e55d7918b4d1369e8fc5f3350eea2ecbd` |
| `pi_runtime/extensions/minecraft-tools.js` (00:09) | `a9d3de9d9996fabcd3d4b2d4ade7b27dcfae2a2609a9ad88dfa2eeb5d5fa3fb7` |
| `src/minecraft_harness/runners.py` (00:09) | `7662a32155009103945d6ae72ca2143e9b5ba4e10d6c06615ee694bc95bc80bf` |
| `src/minecraft_harness/supervisor.py` | `7035b85effd26ed982c46475c73acf8c543f7786106096c80faddd6f768ca007` |
| `configs/minecraft-paper-runner-20261022.yaml` | `b2eb18a28f565d3018fa7845a6c126c1482f391254c9e812b2c6833674453928` |
| `run-paper.sh` at audit start 00:11 | `45e7c0d975297e7ee60432072f0ed5b17681f18f225ce1ce7d3e47e6459ccd89` |
| `run-paper.sh` 00:15:36 (preflight added) | `098456cc871e5b5adb1a884a811844eee0478594119a4c9257d163f098aca168` |
| `run-paper.sh` current 00:17:45 (+compose up/health) | `b0aadf92538c13ee381305a34d9ebcd87c967c37cc3dbc67e9248407b8f3a9a1` |

## 2. Verdict (PASS/FAIL summary)

**PASS (verified read-only or by targeted tests):** Pi model proxy budget metering/reserve/settle; stream usage capture; Pi SIGTERM/abort/session `--continue` e2e; Pi extension tool bridge (memory/web) host-process e2e; RPC 3.12 close fix + per-call timeout; source snapshot for `src`; agent/observer degrade policies + pause clock + final mapping (task-7); compose restart policies; web cap wiring through the Pi bridge; wrapper safe-default `--prepare` and current preflight gates (stack/networks/ports/world marker/progress/key).

**FAIL / risks below:** one P0 start blocker (wrapper), one P0-at-audit-start preflight hazard mitigated by a concurrent wrapper update but still a defense-in-depth gap, eight P1s (Pi upstream-error loop, pause admission threading, no-progress absent, Pi runtime not frozen, container Pi path unvalidated, `/proc` credential exposure, owner-lock isolation, session/storage unvalidated), and P2 hygiene items.

| id | severity | finding | status |
|---|---|---|---|
| P0-1 | P0 | `run-paper.sh --start` preflight contradicted Supervisor's required live bot stack → no stack state could start a run | **FIXED during audit at 00:17:45** (`b0aadf…`): start now runs compose up → wait level.dat → marker → world preflight → bot health → Supervisor. Read-verified; not executed (Docker forbidden). Residual wrapper retry/ID issues P2-4 |
| P0-2 | P0→P1 | At audit start (`run-paper.sh` `45e7c0d9`), `--prepare` passed with paused final11 up; `--start` would have fenced final11 sessions before world admission failed | **Mitigated 00:15:36 by preflight; current wrapper fails safe**; residual supervisor ordering P1-6 |
| P1-1 | P1 | Pi upstream 4xx/5xx loop: error attempts charge conservative usage and the runner keeps sending continuation prompts after `agent_settled`; no `upstream_errors`/`max_model_failures` fail-fast | OPEN |
| P1-2 | P1 | Pause: proxy handler blocks in unbounded `budget.wait_admission`; a client that disconnects while paused still produces a model attempt after resume (probe: reserve=1, settle=1 after resume) | OPEN |
| P1-3 | P1 | Pi runtime has no no-progress guard; `agents.max_no_progress_turns=120` is unused; only `max_turns=100000` continuations bound a stuck agent | OPEN |
| P1-4 | P1 | Pi runtime tree (and new extension JS) is not frozen/hashed; source snapshot covers only `H/src`; restarted runners load a live-mounted Pi tree | OPEN |
| P1-5 | P1 | Container Pi path is unvalidated in this audit/window (no Docker); preflight checks only host Pi CLI+node; `_validate_container_runtime` probes Python/httpx/yaml, not Node/Pi CLI/extension | OPEN (pre-run gate) |
| P1-6 | P1 | Supervisor fences live sessions (`supervisor.py:546`) before world/marker admission (`:554`); wrapper protects the normal path, but direct `mc-harness run`/bypass can kill a live run. Ownership lock is container-local (`ownership.py:36`) | OPEN (defense in depth) |
| P1-7 | P1 | Pi `/proc` credential exposure: model API key + RPC token (and now bridge token) are in `pi_runner` env; Pi bash runs as same container user and can read `/proc/1/environ` | OPEN (known deviation, unmitigated) |
| P1-8 | P1 | Pi session/artifact growth unmeasured; no retention; worst-case per-call artifact bounds imply multi-GB (session + ledger) over 54h | OPEN (must measure before paid run) |
| P2-1 | P2 | Malformed Pi JSON silently dropped; unbounded `asyncio.Queue`; blocking sync RPC calls inside the Pi event loop; Pi child can be orphaned in process mode | OPEN |
| P2-2 | P2 | Team memory is a shared rw directory without CAS/permissions (Pi players) | OPEN (deviation) |
| P2-3 | P2 | `/proc` bridge-token/direct bridge calls bypass Pi `tool_intent/tool_result` ledger projection (only ServiceHub reference_access/memory events remain) | OPEN |
| P2-4 | P2 | Wrapper preflight residual gaps: EULA only warning; BOT tokens/ACCEPT_EULA not hard-fail; Pi CLI version/hash not checked; container Node/Pi not checked; dashboard/viewer port 8899/8896 not checked; no compose up in wrapper; fresh-world generation sequencing | OPEN |
| P2-5 | P2 | Concurrent-edit churn: pi_runner/pi_tool_bridge/extension/wrapper/tests changed during audit; transient e2e failure observed then green; formal run must freeze and record hashes | OPEN (process) |

## 3. P0 findings (details)

### P0-1 (FIXED during audit at 00:17:45; verify in a Docker window): wrapper `--start` could not start a run
- At wrapper `098456cc…` (00:15:36), `preflight` ran for both `--prepare` and `--start` and refused any running `mc-paper-run|minecraft-paper-*` stack, while the Supervisor requires that stack healthy (`supervisor.py:578` `gateway.start(paused=True)`, `:581-582` per-agent `/health`). No stack state could satisfy both.
- The next wrapper revision at 00:17:45 (`b0aadf…`) fixes this: `--start` now runs `preflight()` → `compose_up()` (`run-paper.sh:240-251,361-362`) → `wait_world()` (wait for `level.dat`, `:252-267`) → `prepare_world_marker()` → `preflight_world()` (EULA/marker/progress, `:220-239`) → `wait_bot_health()` (`:294-316`) → Supervisor. A truly fresh server-data dir without `level.dat` is now allowed by the general preflight (`:185-198` only checks an existing world).
- Verified read-only: current `--prepare` with final11 up fails safely as intended (`PREFLIGHT FAILED: an experiment stack is still running: mc-paper-run minecraft-paper-bot1-1 … minecraft-paper-server`). The full `--start` path was NOT executed (would start Docker/experiment; forbidden). Residual: re-running `--start` while the target stack is already up still fails preflight (operator must `compose down`); wrapper invoked inside a container named `mc-paper-run` will match its own active-name check; `COMPOSE_FILE` defaults to static `infra/compose.paper.yaml` (`:117`) with project `minecraft-paper`, so final11's networks/containers must be fully removed before a static-config run. Add a dry-run test that simulates absent/fresh stack and asserts compose-up ordering.

### P0-2 (mitigated; keep for the record): paused final11 could have been fenced by a mistaken start
- At audit start, wrapper `45e7c0d9` had no active-stack/marker preflight; `run-paper.sh --prepare` returned `PREPARED` while final11 was up. A subsequent `--start` would have run `ownership.acquire()` (`supervisor.py:542`) → `gateway.fence()` (`:546`) → `inspect_environment(new run_id)` (`:554`), i.e. DELETE final11 bot sessions first and only then reject the bound world (`environment.py:179-185`: marker run_id `01a25760…`). Because final11's supervisor is still looping, loss of its sessions can drive its recovery path and end the paused run.
- Current state: final11 still `reasoning_paused_world_running` (4,731 events, 258 calls), 14 stack containers Up 6h, 65 workspace containers; final11 ledger only grew via its own resource samples. No experiment/Docker start/stop was performed by this audit.
- Mitigation: wrapper 00:15:36+ preflight refuses active stacks/world markers; verified it fails safely. Residual: keep P1-6 ordering fix (world admission before fence) and do not bypass the wrapper.

## 4. P1 findings (details)

### P1-1 Pi upstream-error continuation loop + conservative charging
- `pi_runner.py::_session_loop` (`:1047-1145`) checks `budget.stop_requested`, `proxy.denials`, and `max_turns` after `agent_settled`, but **never checks `proxy.upstream_errors`** (only `_exit_result` at `:1030-1044` uses it when Pi exits). On a persistent upstream failure (bad key, 401/403, provider 5xx, network error after Pi’s own retries), Pi settles/errors, runner sends `CONTINUATION_PROMPT` (`:1140-1145`), and the loop repeats up to 100,000 continuations / team budget.
- `pi_model_proxy.py`: every request is reserved before upstream (`:395-400`), and any upstream ≥400 is passed through with `upstream_errors += 1` (`:426-444`); the `finally` settles `UNKNOWN_USAGE` (`:486-491`). With configured prices this charges the full admission-time reservation per failed attempt (~`max_input_tokens*input_rate + max_output_rate`; e.g. 250k×$0.30/M + 4096×$1.20/M ≈ $0.08/attempt at current prices). Existing test proves the charge behavior: `tests/test_pi_model_proxy.py:342-350` (`test_upstream_error_settles_unknown_usage`).
- Impact over 54h: a wrong key/provider outage can burn calls/cost (up to `max_cost_usd=1500` ≈ 18.7k phantom error charges) before budget exhaustion, and the run loses its objective window. No harness `max_model_failures`/isolate handling for Pi upstream errors.
- Fix: count consecutive upstream errors (and/or 4xx classes 400/401/403 as fatal) in `PiModelProxy`/`_session_loop`; after `agents.max_model_failures` terminate the runner with `model_failed`; do not charge full reservation for requests that never produced a provider response (settle zero-cost known-failure or cap unknown charge); add a pytest: fake upstream returns 401 N times and assert runner stops with `model_failed` and bounded charges.

### P1-2 Pause/blocked admission: disconnected clients still spend a model call after resume
- Proxy `_serve_chat` calls `self._call("budget.wait_admission")` with no client timeout (`pi_model_proxy.py:338-350`); while paused, ServiceHub’s handler long-polls (`runners.py:405-412`), so the proxy HTTP handler thread blocks. Client disconnect is only noticed when writing a response afterwards; the handler still validates/reserves/upstreams after resume.
- Loopback probe (read-only, `/tmp` script, 2026-10-23): while admission gated, client `urlopen(timeout=0.5)` raised `TimeoutError`; after releasing admission the handler executed `budget.reserve` and `budget.settle` (`reserve=1 settle=1`) for the already-gone client. This creates stale/charged attempts and a burst of model calls on resume if Pi retries/aborts while paused; each blocked handler also holds a thread and an RPC long-poll connection.
- Fix: bound the admission wait in the proxy (e.g. 30 s, then return 429 with `Retry-After`), detect client disconnect before reserving (`select`/`poll` on the handler socket or check `handler.wfile` write after a cheap probe is not enough — use a socket liveness/`Connection: close` policy or per-request cancellation), and cancel/abort in-flight model requests on `pause` where possible. Add a unit test with a blocking fake admission and an aborted client.

### P1-3 No no-progress guard for Pi players
- `agents.max_no_progress_turns: 120` exists in the runner config (`configs/minecraft-paper-runner-20261022.yaml:14`), but `pi_runner.py` never references `max_no_progress`/`no_progress` (grep empty). The only bound is `max_turns` (continuations, `pi_runner.py:1066`, config `max_turns: 100000`) plus team wall/call/cost budgets.
- Impact: a Pi agent that loops on cheap/no-op rounds (or repeatedly hits a tool error) can consume the whole team budget/wall without harness progress detection; Python runtime has a no-progress stop, Pi does not.
- Fix: track `tool_intent/agent_turn` activity per round (or Pi event counters) and stop with `stalled` after `max_no_progress_turns` consecutive rounds without new tool calls/progress; add a fake-Pi test with no-op rounds.

### P1-4 Pi runtime tree/extension not frozen or hashed
- Source snapshot freezes/verifies only `run_dir/source-snapshot/src` (`runners.py:869-1015`, `:1200-1219`); the Pi CLI tree is live-mounted read-only from the host path at runner start (`runners.py:1418-1428`). The new harness logic extension `pi_runtime/extensions/minecraft-tools.js` (memory/web tool bridge) is also outside the snapshot (created 00:09 during this audit).
- Impact: an operator/teammate edit or `npm ci` in `pi_runtime` between runner restarts changes the code loaded by restarted Pi runners while the Supervisor/ledger stay on the old behavior; no `source_snapshot_drift`/manifest hash covers it. Extension semantics (tools exposed to model) can change mid-run.
- Fix: include `pi_runtime` tree hash in the run manifest and verify before every runner spawn/restart (fail closed); or copy/freeze the pinned Pi tree under `run_dir` like `src`. Update `_validate_pi_runtime` to validate extension presence/hash and Pi CLI version.

### P1-5 Container Pi path unvalidated (pre-run gate)
- No Docker was started in this audit (required). Wrapper preflight checks only host `node --version`/Pi CLI file (`run-paper.sh:38-56`); `_validate_container_runtime` probes Python imports only (`runners.py:776-820`), not Node≥22, the Pi CLI, the extension, mount perms, or the broker socket. Docs explicitly note the container Pi e2e is still pending (`PI_METHOD_ALIGNMENT.md` §9.4 item 8).
- Impact: a formal 54h run could fail at runner spawn (missing Node/Pi CLI/extension in the image or wrong mount) or at the first `mc exec` (broker/workspace perms) after the Supervisor has already created workspaces and started paid setup.
- Fix: before the paid run, execute one real container Pi smoke (same command/mounts, fake loopback model) and add a prepare/doctor check for container Node/Pi CLI/extension; record image IDs.

### P1-6 Supervisor fences before world admission; ownership lock is container-local
- `supervisor.py` startup order: `ownership.acquire()` (`:542`) → `gateway.fence()` for configured URLs (`:546`) → `inspect_environment(..., run_id=…)` (`:554`). A wrong/stale config or direct `mc-harness run` can fence a live stack before hitting the marker/fresh-world rejection.
- `RuntimeOwnership` locks live in `Path(tempfile.gettempdir())/minecraft-harness-locks-<uid>` (`ownership.py:36`), which inside the Supervisor container is container-local `/tmp`; two Supervisor containers on the same host do not see each other’s locks. Wrapper preflight is the only cross-container guard.
- Fix (defense in depth): validate world/marker/run identity before any fence, or pass a “fence only after admission” order; make the ownership lock directory a host-shared path (e.g. under the run/data root or `/run`) and include the target server/bot project label.

### P1-7 `/proc` credential exposure (Pi)
- `pi_runner.py` scrubs model/RPC secrets from the Pi **child** env (`_child_environment`, `_FORWARDED_CHILD_ENV_KEYS` ~`:96-104`, `:498-540`), but the model API key and `HARNESS_RUNNER_TOKEN` remain in the `pi_runner` process env (proxy needs them), and the runner container has no `--user` restriction (`runners.py:1395-1440`). Pi bash runs in the same container/UID and can read `/proc/1/environ` (documented in `PI_METHOD_ALIGNMENT.md` §9.4 item 7). The new bridge token is additionally placed in the **Pi child** env (`pi_runner.py:529-530`), so the model can read it and call the loopback memory/web bridge directly.
- Impact: a model can exfiltrate the model key/RPC token (e.g. via bash output/notes/chat), bypass budget metering by calling the provider directly (network bridge is up for the model proxy), and call the bridge outside Pi’s `tool_intent/tool_result` projection.
- Fix: per-agent scoped model keys/proxy egress allowlist, keep the key in a process Pi cannot read (separate UID/namespace or external proxy), run runner as non-root with hidepid/`/proc` restrictions, and treat bridge token as a model-visible capability (audit direct-bridge calls or move bridge outside the runner). This is the highest residual security/integrity risk.

### P1-8 Pi session/artifact growth not measured
- Pi sessions live under `state_root/pi-sessions` (`pi_runner.py:648`), resume via `--continue`, and there is no retention/pruning code. Model request/response artifacts are bounded at 2 MiB (`pi_model_proxy.py:39-43`), tool results at 1 MiB, compaction at 512 KiB (`pi_runner.py:MAX_*`). Model context is 64k tokens (`pi_context_window`), so typical request artifacts are large repeatedly.
- Rough upper bounds for 30k calls: requests ≈ 30k × context bytes (e.g. ~256 KiB → ~7.7 GiB) + tool results (up to 1 MiB each, actual unknown) + session JSONL + ledger (events at ~1.4 KiB/event) + history/compaction copies. The 48 GiB run cap may hold, but actual Pi-run storage/time has never been measured; the storage guard only enforces at 5 s intervals.
- Fix: run a short Pi real-session soak (≤10 min) and measure `du run_dir/agents/*/pi-sessions`, artifacts, ledger growth per call/h; add retention policy for Pi session files (rotate/compress completed sessions) or a documented monitor threshold.

## 5. P2 findings (details)

- **P2-1 Pi framing/backpressure:** `_pump_stdout` silently `continue`s on malformed JSON/non-dict (`pi_runner.py:880-905`) → lost evidence for that record; `asyncio.Queue()` is unbounded (`:1191`) and event handling performs blocking `call_sync` RPC/artifact writes in the event loop (`PreparedPiRunner.append_event/put_artifact`, `:620-637`); a slow control plane stalls heartbeats/Pi pipe and can grow the queue. Pi child is started with `start_new_session=True` (`:1187`); in **process** mode `RunnerManager._kill` kills the pi_runner process only (`runners.py:1556-1600`), so a SIGKILLed/health-timeout pi_runner can leave an orphan Pi/Node process on the host. Paper run must use container mode (container kill removes all processes); document/forbid Pi process mode.
- **P2-2 Team memory:** `run_dir/memory/team` is bind-mounted rw into all runners (`runners.py:1424`) and the Pi prompt points at it; no CAS/permission/central store (unlike Python memory tools). A bad write can clobber team notes; no ledger evidence. Add per-runner write isolation or treat as documented deviation.
- **P2-3 Bridge trust/direct calls:** `PiToolBridge` binds loopback with a random token (`pi_tool_bridge.py:61,84`) and forwards validated calls via RPC (`:169-186`); because the token is in Pi child env, the model can call it from bash outside Pi tool accounting (ServiceHub still records `reference_access` and memory events, but not Pi `tool_intent/tool_result`). Move token out of child env or restrict bridge to Pi process.
- **P2-4 Wrapper residual gaps (current `b0aadf…`):** general `preflight` checks stack/networks/ports/world-if-exists/marker/progress/`DEEPSEEK_API_KEY`/run-dir-empty (`run-paper.sh:147-219`); `preflight_world` hard-fails EULA/marker/progress (`:220-239`). Still missing: `BOTn_ADMIN_TOKEN`/`ACCEPT_EULA` presence check when no world/eula file exists yet (only `preflight_world` handles EULA), Pi CLI version/hash (`:38-56` checks only file + host node), container image Node/Pi CLI/extension, dashboard port 8899/8896, generated-N viewer-range port checks (hard-coded 3101-3108, `:181-184`), `docker compose down` after run (post-run stops containers only), and a dry-run test for the start ordering. Fresh-world path now: fresh server-data (no level.dat) → `--prepare` passes → `--start` compose up → wait level.dat → marker → world preflight → health → Supervisor.
- **P2-5 Concurrent-edit churn:** during the audit `run-paper.sh` changed `45e7c0d9` (00:11) → `098456cc` (00:15:36) → `b0aadf…` (00:17:45); `pi_tool_bridge.py`/extension appeared (00:09-00:10), `pi_runner.py` (00:11), `tests/test_pi_e2e.py` (00:12). A targeted Pi run at ~00:09 failed `test_pi_e2e` (tool list included new memory/web tools) and passed after the test update (47 passed). Formal run must start from a frozen/recorded tree; source snapshot handles `src` but not Pi runtime (P1-4).
- **P2-6 Stale docs vs current tree:** `README.md:213` still says Pi has "**没有** Python v4 的 `memory_*`/`web_search` 工具", while the current tree adds those six tools through `pi_runtime/extensions/minecraft-tools.js` + `pi_tool_bridge.py` and the prompt appendix lists them (`pi_runner.py:404-410`). Update README/PI_METHOD_ALIGNMENT §9 to the current capability set before the run.

## 6. PASS checks (evidence)

- **Pi proxy metering:** admission → validate → reserve → request artifact → upstream → settle exactly once; client-gone settles `UNKNOWN_USAGE` and skips model_response (`pi_model_proxy.py:338-505`); tests `test_pi_model_proxy.py` (reserve denial 429, no upstream call; upstream error settles unknown; missing stream usage conservative) pass.
- **Streaming usage:** Pi’s bundled openai-completions provider sends `stream_options.include_usage=true` (`pi_runtime/.../bundle/chunks/openai-completions-*.js`), and the proxy captures usage per SSE chunk (`_SSECapture`); normal calls should not be unknown-charged.
- **Pi lifecycle:** real Pi CLI host e2e (fake loopback upstream) verifies paper prompt, extension tools, ledger event projection, reserve/settle exactly once, ready/heartbeat/finished, SIGTERM clean stop, `--continue` session resume, no key in ledger/artifacts/Pi child env (`tests/test_pi_e2e.py`, `test_pi_runner.py`, `test_pi_tools.py`).
- **Extension/bridge:** memory read/write/list/search + web search/fetch tool schemas and bridge validation/permissions/truncation (`pi_tool_bridge.py`; `tests/test_pi_tools.py` 10 tests pass); web calls use the same team Budget via ServiceHub `budget.admit_web` + `WebTools(..., budget=...)` (`runners.py:258,396-459`), so `max_web_calls` is enforced for Pi bridge calls (code-level; no dedicated cap e2e for Pi).
- **Control plane (task-7):** RPC 3.12 close fix (cancel tasks before bounded `wait_closed`, `runner_rpc.py:173-196`), per-call default timeout + long-call exemptions + container env forwarding (`runner_rpc.py`, `runners.py:1349-1394`), source snapshot freeze/verify/drift for `src` (`runners.py:869-1015`; `tests/test_source_snapshot.py` pass), agent/observer isolate-degrade + pause clock (`tests/test_stability.py`, `test_paper_supervisor.py` pass), compose `restart: unless-stopped` for identity/launcher/bot and `no` for server (`infra/compose.paper.yaml:13,41,…`; deployment tests pass), runner attempts reset on successful start/restart (`runners.py:1327,1726`).
- **Operational safety:** wrapper default is prepare-only; current wrapper (`b0aadf…`) preflight rejects active stacks, leftover compose networks, busy 3101-3108, bound world markers, existing player progress, non-empty run dirs, and missing DEEPSEEK_API_KEY; `--start` now runs compose up → wait `level.dat` → marker/world preflight → bot `/health` before Supervisor (`run-paper.sh:240-316,361-366`). Verified read-only: current `--prepare` with final11 up fails safely. `--prepare` at 00:11 on the older wrapper had passed (P0-2 record).
- **final11 untouched:** status `reasoning_paused_world_running`, 4,731 events / 258 calls, 14 stack containers Up 6h, 65 workspace containers, 97 total running; ledger growth only from its own resource samples. Audit action side effects: `--prepare` created empty `mc-runs/four-agent-paper-container-next` and log/backup dirs only; ledger copies were read-only to `/tmp`; no Docker/experiment state changed.

## 7. Commands run (read-only / short)

```bash
# selected tests (host .test-venv 3.12, PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages)
python -m pytest -q tests/test_pi_tools.py tests/test_pi_runner.py \
  tests/test_pi_runner_manager.py tests/test_pi_model_proxy.py tests/test_pi_e2e.py \
  tests/test_source_snapshot.py tests/test_stability.py tests/test_longrun_config.py \
  tests/test_paper_interface.py
# => 116 passed, 438 subtests passed in 32.89s (00:2x)
# earlier same-window Pi-only run: 47 passed / 33 subtests after test_pi_e2e 00:12 update;
# at ~00:09 the same Pi set was 77 passed, 1 failed (test_pi_e2e stale tool-list while pi_runner
# was being extended with memory/web tools) — evidence of concurrent churn, not a final failure.

# wrapper safe-default preflight (no start)
/ssd_data/caizihao/minecraft/minecraft-harness/run-paper.sh --prepare
# 00:11 wrapper 45e7c0d9: PREPARED (would have led to P0-2)
# 00:2x wrapper 098456cc / 00:3x wrapper b0aadf: PREFLIGHT FAILED: an experiment stack is still running: mc-paper-run ...

# read-only operational inventory
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'mc-paper-run|minecraft-paper'
docker network ls --format '{{.Name}}\t{{.Driver}}' | grep -E 'minecraft-paper'

# read-only final11 ledger copy to /tmp
# pause/disconnect probe: /tmp script using PiModelProxy + fake RPC + httpx.MockTransport
#   client TimeoutError while admission gated; after resume reserve=1 settle=1
```

Not executed (constraints/risk): full 592-test suite, Docker/container Pi e2e (3 docker-gated runner tests, real container Pi run), 54h/soak runs, any experiment or final11 control, any network beyond loopback.

## 8. Recommended pre-run gate (minimal)

1. Verify the new wrapper start path in a dedicated Docker window (compose up → wait world → marker → bot health → Supervisor; P0-1 fix at 00:17:45) and move world/marker admission before `gateway.fence()` (P1-6 defense in depth).
2. Add Pi fail-fast: upstream-error consecutive counter → `model_failed`; classify 4xx auth as fatal; do not charge conservative usage for attempts with no provider response (P1-1).
3. Bound proxy admission wait and cancel/deny disconnected clients before reserve (P1-2).
4. Implement Pi no-progress guard using `max_no_progress_turns` (P1-3).
5. Freeze/hash `pi_runtime` + extension in the run manifest and verify on every runner start (P1-4).
6. Run one real container Pi smoke on the exact image/mounts and record image IDs before the paid run (P1-5); block `/proc` key exposure or accept as a recorded deviation with mitigations (P1-7).
7. Run a ≤10 min Pi real-session storage/session-growth measurement, then set monitor thresholds (P1-8).
8. Add preflight hard checks for EULA/tokens/Pi version/container runtime, document fresh-world generation → stop → prepare → compose up → start sequence (P2-4).

## 9. Limitations / unverified

- No Docker start/exec allowed: container Pi mount/perms/Node/Pi-CLI/broker-socket path and the 3 docker-gated runner tests remain unverified in this audit (documented known gap).
- Full suite not run; targeted set green at snapshot. The tree was edited concurrently (see §1/§7), so later hashes may move.
- Pi upstream-error loop, pause stale-attempt burst, process-orphan, and session growth are established by code inspection + one loopback probe, not by a full 54h soak; P1-1/P1-3/P1-8 need the suggested tests before the paid run.
- `/proc` exposure was not exercised (no container start); it is documented in `PI_METHOD_ALIGNMENT.md` and confirmed by code (key/token in pi_runner env; no `--user`).

## 10. Lead follow-up (2026-10-23, after the audit)

- **P0-1 fixed**: `run-paper.sh --start` now performs `compose_up → wait_world →
  prepare_world_marker → preflight_world → wait_bot_health → Supervisor`; preflight refuses
  running old stacks/networks/ports, bound worlds, non-empty run dirs, missing EULA/key/token
  env, and missing Pi CLI/extension. Not executed outside the safe preflight path.
- **P0-2 / P1-6 fixed**: `run-paper.sh` preflight blocks the paused final11 stack; Supervisor
  now calls `inspect_environment`/world admission **before** `gateway.fence()`.
- **P1-1 fixed**: Pi proxy tracks consecutive upstream errors, treats auth/4xx as fatal,
  settles zero usage for HTTP errors with no completion, and `pi_runner` stops
  `model_failed` at `max_model_failures`.
- **P1-2 fixed**: admission wait bounded to 30 s with client-disconnect checks; timeout
  returns 503 without reserving.
- **P1-3 fixed**: `max_no_progress_turns` now ends Pi continuation as `stalled`.
- **P1-4 fixed**: source-snapshot manifest hashes Pi CLI/extension/package/lock; restart
  mismatch fails closed.
- **P1-5 verified**: one-shot real container Pi smoke passed (Node 22, Pi extension loaded,
  bash wrote proof, ready/finished, 2 reserves/settles) after adding host-uid container
  `--user`; full compose smoke still pending.
- **P1-7 open**: `/proc/<runner>/environ` still exposes model key/RPC/bridge token to Pi bash.
- **P1-8 partially measured**: perf audit gives per-turn session/artifact sizes; no retention
  policy yet; monitor disk before the long run.
- Real DeepSeek smoke passed: text call and tool-call run (4 successful bash calls, proof file,
  4 reserve/settle) through the Pi proxy.
- Full suite at this follow-up: **618 passed, 3 skipped, 657 subtests**.
