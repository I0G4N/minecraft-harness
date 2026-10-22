# AUDIT 2026-10-23 — POST-FIXES FULL CODE AUDIT

Read-only independent audit after the three goal fixes: **Pi Observer runner/bridge/extension + ServiceHub observer RPC + RunnerManager/Supervisor routing**, **`/proc` credential hardening**, and **Pi session/artifact retention**.
Repo: `/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness` (H). Wrapper: `/ssd_data/caizihao/minecraft/minecraft-harness/run-paper.sh`.
Task-20 (revision 2), auditor `longrun-auditor`. Snapshot time: 2026-10-23 ~02:17 +08. **The tree was edited concurrently during the audit**: `pi_observer_bridge.py` 02:10, `pi_observer_runner.py` 02:09, `runners.py` 02:11 (observer delivered-evidence/ACL hardening), `tests/test_observer_rpc.py` 02:12, tests then went green. Findings are against the hashes in §1; the transient failing-test window at 02:13 is recorded, current tree is green.

Constraints honoured: no source edits, no Docker/experiment/final11 start-stop-exec, no git, tests <10 min, no external network. Read-only `docker ps`/ledger copies, host `.test-venv` pytest, Node tests, `/tmp` probe scripts only. Side effects: `run-paper.sh --prepare` (safe default, failed preflight), `/tmp` scratch files, host test subprocesses.

## 1. Snapshot (SHA256 at ~02:15)

| file | sha256 |
|---|---|
| `src/minecraft_harness/runners.py` (02:11) | `ee2d0a28eaa5c46ce94e18d1206c91a76b288268213205a1ee4050f99327ccb4` |
| `src/minecraft_harness/pi_runner.py` (01:43) | `80628d1a424dc1727c8a1e03ef0db6df560a5147384bc6eb2551a346097658cd` |
| `src/minecraft_harness/pi_model_proxy.py` (01:43) | `93930afc04903e0b692f97b7efddeff7ea1d05361e4a19a6e609bb7b5b8ba028` |
| `src/minecraft_harness/pi_observer_runner.py` (02:09) | `b2efe0483b4c292d2ab4b72f6f244b3cdd8597768da7fc09de7bdf584142bee3` |
| `src/minecraft_harness/pi_observer_bridge.py` (02:10) | `8678e9cd2b5ecd716348c8dd7826c104ef5a9c9d9f08319e640f84ec35ae34ac` |
| `src/minecraft_harness/supervisor.py` | `97f4613d1492512eeee30fdab18cae454f652b82329b6f6c85d20e9c7eee68ef` |
| `src/minecraft_harness/pi_security.py` | `2433dcca4704a36cd43626a6f85c37e079165d3574cbfa068cf9ace926441533` |
| `configs/minecraft-paper-runner-20261022.yaml` | `f6ee2f911186cd0bf84a92f8304c80cf926df543a3860c65c1bb2850c4573270` |
| `run-paper.sh` (00:38) | `f8f3df4ad160ffedd1dee78dde00a0256b0db78e5b18fef82e8a1529d4ce505b` |

## 2. Verdict

**No P0 found.** The three goal fixes are present and work on the current tree: Pi Observer (identity, read-only tool surface, ACL/report, mounts, policy) passes; `/proc` model-key/RPC-token exposure is mitigated and tested; retention mechanisms (prune/rotate/archive caps, active guard, metadata-only artifacts) are implemented and tested. Four P1 residuals remain: **Pi bundle hash scope**, **mid-stream SSE failure handling**, **model-bash network egress**, and **run-wide retention accounting**. Full Python suite is green (686 passed / 3 skipped / 723 subtests), Node 51 passed; the in-repo "618/657" and "container smoke" claims are stale/unverified (§5, §8).

| id | sev | finding | status |
|---|---|---|---|
| P1-1 | P1 | Runtime-hash freeze covers only 5 files (cli shim + package/lock + extensions), not `cli-runtime.js`/81 bundle chunks/node_modules; tampering a chunk is not detected although docs claim Pi CLI bundle fail-closed | OPEN |
| P1-2 | P1 | Upstream failure mid-SSE writes a second HTTP 502 after chunked terminator, records a bogus `model_response` + `null` artifact, settles UNKNOWN usage | OPEN |
| P1-3 | P1 | Pi player bash runs with `--network bridge`; web budget/search accounting is bypassable via `curl` and token/prompt exfiltration is possible; no egress allowlist | OPEN (container-only; not executed here) |
| P1-4 | P1 | Retention caps are per runner process: ArtifactBudget is recreated on restart, 5×8 GB artifact + session/archive budgets leave little headroom under `max_run_bytes=51.5 GB`, and metadata-only artifacts are not charged; player sessions are not pruned at shutdown | OPEN |
| P2-1 | P2 | Docs/full-suite claim stale: claims 618 passed/657 subtests; current suite is 686/723 (3 skipped) | OPEN |
| P2-2 | P2 | Wrapper preflight checks only the player Pi extension (not `observer-tools.js`), and container runtime validation only probes Python/HTTPX; Node/Pi CLI/extension in the image are not preflighted before compose up | OPEN |
| P2-3 | P2 | Bridge token is in the Pi child env; model bash can call the loopback bridge directly, bypassing Pi `tool_intent/tool_result` projection (documented residual) | OPEN (known deviation) |
| P2-4 | P2 | Active session cap stops the player/observer mid-run (recorded as degraded) but never auto-rotates/restarts; player shutdown does not run retention; no operator alert threshold | OPEN (design residual) |
| P2-5 | P2 | Docs claim a one-time real container Pi smoke; this audit could not run Docker, 3 container-gated tests are skipped, so the claim is unverified in this window | LIMITATION |
| P2-6 | P2 | `/proc` fix leaves the player `MC_AGENT_TOKEN` and bridge token in Pi child env (required for `mc`/bridge), model own-env readable; residual documented by docs | OPEN (accepted residual) |

## 3. P1 findings

### P1-1 Pi runtime hash freeze is incomplete (docs overclaim fail-closed)
- `RunnerManager._pi_runtime_hashes` (`runners.py:1278-1305`) hashes exactly: `pi_cli` (the 160-byte `dist/bundle/cli.js` shim), `pi_runtime/package.json`, `package-lock.json`, `extensions/minecraft-tools.js`, `extensions/observer-tools.js`. It does **not** hash `dist/bundle/cli-runtime.js` (the 1,050-byte loader) nor the 81 `.js` chunk files in `dist/bundle/chunks/`, nor `node_modules` provider code. The manifest stores this as `pi_runtime` and `_verify_source_snapshot` only compares these five files (`runners.py:1422`).
- Docs claim: "`source-snapshot.json` 增加 Pi CLI bundle、extension、package/lock 哈希；重启校验不一致即 fail closed" (`PI_METHOD_ALIGNMENT.md:347-348`). That is false for the bundle chunks.
- Evidence (read-only probe, temp tree): listed hash files were only `cli.js`, `minecraft-tools.js`, `observer-tools.js`, `package.json`, `package-lock.json`; after modifying `cli-runtime.js` from `REAL RUNTIME CODE v1` to `TAMPERED RUNTIME CODE v2`, `_pi_runtime_hashes()["sha256"]` was **unchanged** (`digest_changed_after_tamper: false`). Real tree: `find .../dist/bundle -name '*.js' | wc -l` = 81; changing any chunk/provider file between runner restarts silently changes model behavior.
- Repro (can be run read-only in a temp dir; no repo edits):
  ```python
  from types import SimpleNamespace
  from pathlib import Path
  from minecraft_harness.runners import RunnerManager
  # build temp repo: pi_runtime/{package.json,package-lock.json,extensions/*}, cli.js shim + cli-runtime.js
  fake = SimpleNamespace(runner_runtime="pi", pi_cli=cli_js, container_source_root=tmp_repo)
  h1 = RunnerManager._pi_runtime_hashes(fake)
  (cli_js.parent / "cli-runtime.js").write_text("TAMPERED")   # not in candidates
  h2 = RunnerManager._pi_runtime_hashes(fake)
  assert h1["sha256"] == h2["sha256"]   # observed: bypass succeeds
  ```
- Fix: hash all files under `pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/**/*.js` (or content-address/copy the whole pinned Pi tree) plus package-lock and both extensions, and record the Node binary/version; verify before every spawn/restart. Add a test that modifies a chunk and expects `RunnerControlError`.

### P1-2 Mid-stream upstream failure writes malformed SSE and false model_response/UNKNOWN usage
- In the SSE branch (`pi_model_proxy.py:596-609`) headers and chunks are sent; only `BrokenPipe/ConnectionReset` are handled, then the outer `except httpx.HTTPError` (`:633-637`) calls `handler._send_json(502)` **after** the chunked stream has already sent `0\r\n\r\n`. The `finally` (`:644-650`) then settles UNKNOWN usage and records `model_response` because `sent=True` and `client_gone=False`; `response_document` is still `None`, so the response artifact is `null`.
- Raw-socket probe (`httpx.SyncByteStream` yields one SSE chunk then raises `httpx.ReadError`): client received `HTTP/1.1 200`, the partial chunk, the terminating `0\r\n\r\n`, and then a **second HTTP response** `HTTP/1.1 502`; ledger saw `model_request` + `model_response`, a `null` response artifact, and `budget.settle` with UNKNOWN usage (`prompt_tokens: null...`). Pi's stream parser can mis-handle the trailing bytes; the ledger claims a completion that never completed.
- Repro: fake transport returning `Response(200, headers={"content-type":"text/event-stream"}, stream=BoomStream())` where `BoomStream.__iter__` yields one `data: ...\n\n` then raises `httpx.ReadError`; POST to the proxy and read the raw socket. Output tail: `2 Oct 2026 ... content-type: application/json ... {"error":{"message":"upstream model request failed"...}}` after the SSE terminator.
- Fix: track `response_started`; on a streaming error do **not** send a second HTTP response — close the connection (and don't append a successful `model_response`); settle UNKNOWN or a dedicated error usage and append `model_error`; set `response_document` consistently; add the mid-stream-drop test above. Also make the `finally` artifact append conditional on a complete capture (`response_document is not None`).

### P1-3 Pi player bash has unrestricted bridge-network egress; web accounting is bypassable
- Pi player containers start with `--network <runner_container_network>` (`runners.py:1916`), default `bridge` (`runners.py:1003`; runner config line 72), because the loopback model proxy must reach the upstream provider. Pi's built-in `bash` runs in that same network namespace and the model can run arbitrary `curl`/`wget`/socket commands. Only the `web_search`/`web_fetch` **extension tools** call the metered path (`runners.py` ServiceHub `budget.admit_web` + `WebTools`); direct bash HTTP bypasses `budgets.max_web_calls` and `reference_access` accounting, and gives an exfiltration channel for the player `MC_AGENT_TOKEN` and prompt/context content.
- Not executed here because Docker start is forbidden; code path is unconditional and default config is `bridge`. Repro for the dedicated Docker window: `docker exec mc-runner-<slug>-<player> curl -sS https://example.com -o /dev/null` (should be blocked by an egress policy, currently succeeds) and repeat `curl` to a web endpoint while checking `budget.web_calls`/`reference_access` stay 0.
- Fix: enforce egress allowlist (only model provider host(s) via a relay/proxy sidecar, or a per-container network with an allowlisted HTTP CONNECT proxy); keep the Pi→runner proxy loopback; add a container-gated test that raw egress is blocked but proxy traffic works.

### P1-4 Retention caps do not bound run-wide storage across runner restarts/agents
- `ArtifactBudget` is created per runner process using `pi_artifact_max_bytes` (`pi_runner.py:919`, `pi_observer_runner.py:472`); a runner restart creates a fresh 8 GB budget (config `configs/minecraft-paper-runner-20261022.yaml:136`). 4 players + Observer = 5×8 GB artifact budget, plus 5×1 GB sessions (`:135`) and up to 5×1 GB archives (`PI_SESSION_ARCHIVE_MAX_BYTES=1 GB` each), against `max_run_bytes=51,539,607,552` (`:129`). This is already near the run cap before ledger/API artifacts, and each restart resets the artifact budget.
- Metadata-only projections are stored but **not charged**: `ArtifactBudget.take` only adds full-content bytes; when over cap it returns metadata and never accounts for the metadata artifact (`pi_model_proxy.py:110-155`). Probe: cap 100, first 80-byte full artifact used 80; second 80-byte artifact became metadata (390 bytes stored) and `used_bytes` remained 80 — stored bytes exceed the nominal cap.
- Player sessions are only pruned/rotated at prepare (`pi_runner.py:906`); only the Observer applies retention in `finally` (`pi_observer_runner.py:1009`). A player runner stopped by the active session guard (or normal stop) leaves its oversized session in the run dir until the next resume/start; a 54 h run can continue with multiple players stopped at cap.
- Repro: `pytest -q tests/test_pi_retention.py` (mechanisms pass); probe above; count config/agent caps and compare with `max_run_bytes`. Not a crash, but a 54 h run can hit `resource_exhausted` from the global storage guard before its wall/model budget.
- Fix: persist artifact usage per agent/run in ledger/state (or use one run-wide budget), count metadata bytes, include session/archive bytes in a run-wide projection, apply retention in the player `finally`, and emit an operator warning (`degraded`/`storage_pressure`) well before `max_run_bytes`; regenerate caps so the sum plus ledger has explicit headroom.

## 4. Verified fixes (PASS)

**Pi Observer (task-16/18)** — PASS on current tree:
- `pi_observer_runner.py`: spec fail-closed (`role=observer`, `agent_id=observer`, no `cli_environment`, no broker, player CLI creds rejected; `_validate_spec`); prompt uses paper Observer prompt + read-only appendix; command tools = `read,write,records_read,evidence_read,observer_report,infra_issue`, no bash, no game/memory/web; runner `--continue` session resume; session retention at prepare + finally; heartbeat/ready/finished/signal handling.
- `pi_observer_bridge.py`: per-process random token, loopback only, observer identity binding (`rpc_client.role/agent_id`), bounded body/text, strict argument schemas, report/evidence/infra validation; no runner token/model key exposure.
- `ServiceHub` observer RPC (`runners.py:531-848`): only (`observer`,`observer`) may call `observer.*`; records paging redacts operator-only/large payloads, skips observer-authored events, grants only artifacts belonging to the event actor/control plane (cross-runner planting does not widen observer ACL), **persists viewed event_seq** in `run_dir/observer-seen.sqlite3` and report refs now require `seen` + bounded `through_cursor`; artifact reads require an existing grant; report is rebuilt from known fields and advisory only; infra-issue callback requires an independent Supervisor probe (tests `test_observer_rpc.py:119-335`).
- `RunnerManager`/`Supervisor`: observer routes to `minecraft_harness.pi_observer_runner` for `runner_runtime=pi`; observer spec has no team-memory/broker mounts and no `MC_*` env; container args mount state/workspace/reference/pi_runtime/source/spec/socket only, no docker socket; Supervisor maps observer failure by `observer_failure_policy=degrade` (`observer_degraded` recorded, players continue; `fail_run` preserved) and handles unexpected/expected shutdown; wake via important ledger events ≤1 s (tests).
- Probe: before reading a page, report refs `[0]`, `[1]`, artifact hash all rejected `PermissionError`; after page events 1,2 accepted; unread seq 3 rejected; `through_cursor=99` rejected `ValueError`; granted evidence read returns content; player role/agent combinations denied. This confirms the concurrent 02:11/02:12 delivered-evidence fix. Full suite green.

**`/proc` credential hardening (task-17)** — PASS, with documented residual:
- `pi_security.harden_process_credentials` sets `PR_SET_DUMPABLE=0` (`pi_security.py:23-46`); called first thing in `pi_runner.main` (`pi_runner.py:1577`), `pi_observer_runner.main` (`pi_observer_runner.py:1028`), and Python `runner.main` (`runner.py:1047`); failure is fatal (`CredentialHygieneError`). Container runners are now PID 1 with **no `--init`** (`runners.py:1908-1922` comment), so no unhardened tini PID 1 exposes env; `--cap-drop=ALL` removes `CAP_SYS_PTRACE`, `--user` is derived from run_dir uid:gid.
- Host probe: after hardening, same-uid child reads of parent `/proc/<pid>/environ`, `/proc/<pid>/fd`, and `/proc/<pid>/maps` all fail `Permission denied`; `/proc/<pid>/cmdline` readable but contains no secrets; child own env readable (expected). Tests: `test_pi_runner.py:403` and `test_pi_observer.py:909` `test_runner_environment_is_not_readable_via_proc` pass; child-env tests confirm no model key/RPC token in Pi child env (`test_pi_observer.py` child-env test).
- Residual (documented): player Pi child env still contains bridge token and player `MC_AGENT_TOKEN` (needed for extension/mc); `/proc` fix intentionally does not remove them (`PI_METHOD_ALIGNMENT.md:296-299`); bridge-token direct calls are P2-3.

**Retention mechanisms (task-19)** — mechanisms PASS (`tests/test_pi_retention.py`):
- `apply_session_retention` deletes oldest complete sessions, keeps newest for resume, archives an oversized active session atomically and starts fresh (`pi_runner.py:589-645`); archive bounded to 1 GB/8 files; events `pi_session_pruned`/`pi_session_rotated`.
- Active guard every 30 s (`_check_active_session_limit`, `pi_runner.py:696-707`, `:1091`) emits `pi_session_size_limit` and stops the runner; player maps it to `infra_failed` and Supervisor records `agent_degraded` (DEGRADED includes `infra_failed`), Observer maps to stopped→`observer_degraded`.
- `ArtifactBudget` stores metadata-only projection after cap (`pi_model_proxy.py:80-155`) with one retention event; session/artifact spec caps validated (`pi_runner._validate_spec`; config validation); tests cover caps, archive, resume, active guard, metadata, proxy budget.
- Gaps are accounted in P1-4/P2-4, not hidden.

**Pi model proxy hardening (task-7 fixes verified)** — PASS:
- Admission is bounded to 30 s (`ADMISSION_WAIT_TIMEOUT_S=30`, `pi_model_proxy.py:47,480-484`); `_client_alive` re-checks before admission/validation/reserve (`:412-425,480,494,517`). Probe: client disconnect while admission pending → 0 reserve/0 settle; paused admission timeout → 503/no reserve (test `test_paused_admission_timeout_does_not_reserve`).
- Upstream 4xx auth is fatal and settles `ZERO_USAGE` (`:395-409,581`); 5xx settles zero usage; `consecutive_upstream_errors` resets on success; player/observer loops fail fast at `max_model_failures` (`pi_runner.py:1425-1445`, `pi_observer_runner.py:801-890`). Probe with fake 401: `fatal=True`, `consecutive=1`, settle zero, one reserve; tests `test_auth_upstream_error_is_fatal`, `test_upstream_error_settles_zero_usage`.
- No-progress: successful tool result resets counter (`pi_runner.py:1245`), settled rounds increment, `max_no_progress_turns` → `stalled` (`:1447-1455`); test `test_no_progress_continuations_end_as_stalled`. (Caveat: any successful tool call resets it, so `bash true` loops are not semantically stalled; budget/turn caps still bound.)

**Integration / control plane / wrapper** — PASS:
- Supervisor startup now validates world/config **before** fencing live bot sessions (`supervisor.py:556` inspect_environment → `:563` gateway.fence), fixing the prior ordering hazard.
- Wrapper `--start` chain is `preflight → compose_up → wait_world → prepare_world_marker → preflight_world → wait_bot_health → Supervisor` (`run-paper.sh:382-398`); preflight refuses active old stack, leftover compose networks, busy 3101-3108, bound/used world, non-empty run dir, missing EULA, missing `DEEPSEEK_API_KEY`, and missing player bot tokens. `run-paper.sh --prepare` with final11 still up fails safely with `PREFLIGHT FAILED: an experiment stack is still running: mc-paper-run ...` (observed); no experiment/Docker was started.
- Runner environment: only allowed env keys plus model/web key vars, `HARNESS_RUNNER_RPC_TIMEOUT_S`; frozen source snapshot mounted read-only, mode normalized 0644/0755 for cap-drop containers; Pi runtime tree read-only; state/workspace rw; no docker socket; observer has no team-memory/broker mount or `MC_*` env (tests `test_observer_pi_container_mounts_and_environment_are_read_only` and process spec tests).
- Source snapshot freeze/verify/drift and fail-closed restart for `src`; runtime Pi hash partial (P1-1).
- Full Python suite current: **686 passed, 3 skipped, 723 subtests in 111.73 s**; Node `npm test --prefix bot_runtime`: **51 passed**. (An intermediate combined run during the 02:09-02:12 edit window saw 7 transient observer failures; tests were updated at 02:12 and current runs are green.)
- final11 untouched: status `reasoning_paused_world_running`, 5,522 events, 14 stack containers up, 65 workspace containers; no Docker/experiment/final11 operations by this audit.

## 5. P2 findings

- **P2-1 Docs/full-suite claims stale.** `IMPLEMENTATION_STATUS.md:10`, `PI_METHOD_ALIGNMENT.md:168,175,186` claim **618 passed, 3 skipped, 657 subtests**. Current command `.test-venv/bin/python -m pytest -q` gives **686 passed, 3 skipped, 723 subtests** (111.73 s). Update the docs (and the container-smoke wording) to the current counts; re-run the full suite after any further edits.
- **P2-2 Wrapper/container preflight gaps.** `run-paper.sh:223-224` checks only `pi_runtime/extensions/minecraft-tools.js`; a missing `observer-tools.js` passes preflight and fails only in `RunnerManager._validate_pi_runtime` after `compose_up`/world boot. `_validate_container_runtime` (`runners.py:1149-1194`) probes only `docker version/image inspect/network` + Python/httpx/yaml inside the image; it does **not** check Node ≥22, the Pi CLI bundle, or both extensions inside the image. Add the observer extension to wrapper preflight and a container image/Node/Pi check before starting the stack (or make `prepare` do a no-start image probe).
- **P2-3 Bridge token direct-call bypass.** Player Pi child env contains `HARNESS_PI_BRIDGE_TOKEN` (`pi_runner.py:757`; observer `pi_observer_runner.py:371`); bash can call `$HARNESS_PI_BRIDGE_URL/tool` directly, so memory/web calls bypass Pi's `tool_intent/tool_result` projection (ServiceHub still records memory/reference events). This is acknowledged in `PI_METHOD_ALIGNMENT.md:299`; if audit-grade tool-event completeness is required, move the token out of the model-visible env (file with tighter permissions is not sufficient against same-uid bash; use a bridge Unix socket bound to the Pi process/uid or per-call side channel) or accept and document the deviation in the run manifest.
- **P2-4 Active-retention stop has no auto-rotate/restart.** `_check_active_session_limit` stops the runner at cap; player maps to `infra_failed`/`agent_degraded`, Observer to stopped/`observer_degraded`, but neither restarts/rotates in-process; a 54 h run continues with a lost agent (paper parity/evidence impact). Add an operator alert and either auto-restart after archiving (bounded) or a documented "degraded run" policy; also apply player retention in `finally` like the Observer.
- **P2-5 Container Pi smoke claim unverified.** `PI_METHOD_ALIGNMENT.md:301` says a one-time real container Pi smoke passed; this audit cannot start Docker. Only 3 container-gated tests were skipped, and the host suite does not exercise container Node/Pi/extension/mount/PID-namespace behavior. Dedicated Docker window required before a 54 h paid run.
- **P2-6 `/proc` residual tokens.** Player `MC_AGENT_TOKEN` and bridge token remain in Pi child env by design (`PI_METHOD_ALIGNMENT.md:296-299`); model can read its own env (expected) and direct bridge calls are P2-3. Model key and runner RPC token are no longer exposed by `/proc`; this residual is accepted/documented, not a regression.
- **(optional) P2-7 metadata/seen storage:** `observer-seen.sqlite3` (event-seq PKs) and metadata artifacts grow under run_dir; they are counted by the global storage guard but not by Pi retention caps. Small (30k rows, ~1 KB metadata/artifact), but include in the run-wide retention projection (P1-4).

## 6. Commands / evidence (short, read-only)

```bash
# Full current Python suite (host .test-venv 3.12; no Docker)
PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
  .test-venv/bin/python -m pytest -q
# => 686 passed, 3 skipped, 723 subtests passed in 111.73s

# Observer/retention/targeted
PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
  .test-venv/bin/python -m pytest -q tests/test_observer_rpc.py tests/test_pi_observer.py
# => 40 passed, 62 subtests (current tree; was 7 transient failures at 02:13 during the concurrent edit)
PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
  .test-venv/bin/python -m pytest -q tests/test_pi_retention.py tests/test_pi_runner.py \
  tests/test_pi_model_proxy.py tests/test_pi_runner_manager.py tests/test_pi_tools.py tests/test_pi_e2e.py
# targeted post-fix set: 193 passed / 95 subtests (earlier window; later cases added)

# Node claim
npm test --prefix bot_runtime      # 51 passed

# wrapper safe default (no start); final11 still running -> safe failure
/ssd_data/caizihao/minecraft/minecraft-harness/run-paper.sh --prepare
# run-paper.sh: PREFLIGHT FAILED: an experiment stack is still running: mc-paper-run ...
# run-paper.sh: nothing was started; resolve the condition and re-run --prepare.

# read-only final11/stack state
docker ps ... (14 mc-paper-run/minecraft-paper-* containers; 65 mc-workspace)
# final11 ledger copy: status reasoning_paused_world_running, 5,522 events
```

Probe results (scripts in `/tmp`, no repo edits):
- **/proc**: parent `/proc/<pid>/environ`, `/proc/<pid>/fd`, `/proc/<pid>/maps` all `Permission denied` from a same-uid child; parent cmdline readable but no secrets; child own env readable; `PR_SET_DUMPABLE=0` applied. Tests `test_runner_environment_is_not_readable_via_proc` (player/observer) pass.
- **Observer ACL/evidence**: before any records page, report refs `[0]`, `[1]`, artifact hash → `PermissionError`; after page events 1,2 → recorded; `[0]`/unread `[3]` → `PermissionError`; `through_cursor=99` → `ValueError`; granted `evidence_read` returns content; player-role/observer-id mismatches denied.
- **Proxy**: fake 401 → `fatal=True`, `consecutive=1`, settle `ZERO_USAGE`, one reserve; client disconnect during admission → 0 reserve/0 settle; pause admission timeout → 503, no reserve.
- **Mid-stream SSE**: raw socket: `HTTP/1.1 200` + partial chunk + `0\r\n\r\n` + trailing `HTTP/1.1 502`; settle UNKNOWN; ledger events `model_request`+`model_response`; response artifact `null`.
- **Runtime hash**: hash file list = `cli.js`, `minecraft-tools.js`, `observer-tools.js`, `package.json`, `package-lock.json`; tampering `cli-runtime.js` leaves `sha256` unchanged; real bundle = 81 JS files, `cli.js` is a 160-byte shim.
- **Retention**: oldest pruned, active oversized rotated/archived, archive bounded; `ArtifactBudget` under cap used 80 after full, metadata 390 bytes stored uncounted, `retained_count=1`.
- **Wrapper/final11**: `--prepare` fails safely with final11 stack; no Docker/experiment action; final11 remains `reasoning_paused_world_running`.

## 7. Recommended priority

1. **P1-2 SSE mid-stream**: close instead of double response, no false `model_response`, correct settlement/artifact; add test.
2. **P1-1 runtime hash**: hash/copy the full Pi bundle + node_modules provider files (or pin a content-addressed tree); test chunk tamper fail-closed; update docs claim.
3. **P1-3 egress**: allowlist only provider traffic / proxy sidecar; test raw egress blocked while loopback proxy works.
4. **P1-4 retention**: run-wide budget/accounting incl metadata/sessions, persistence across restarts, player shutdown retention, storage-pressure alarm; adjust caps for 5 agents + ledger headroom.
5. P2: update docs/full-suite counts; wrapper observer-extension + container image/Node/Pi preflight; decide/record bridge-token/tool-ledger deviation; add operator alert for active-session stop; run dedicated Docker container Pi smoke before paid run.

## 8. Limitations / unverified

- No Docker/container execution (forbidden): container Pi smoke, container Node/Pi/extension, PID namespace/`--user`/mount behavior, and P1-3 live egress are code/probe/host-test–based only; 3 container-gated tests skipped.
- No 54 h/30 k-call soak; full suite is host-level (686/723), Node 51; retention/storage analysis is arithmetic + unit probes, not a long-run measurement.
- Worktree changed concurrently at 02:09-02:12 (`pi_observer_bridge.py`, `pi_observer_runner.py`, `runners.py`, tests); an intermediate observer-test run failed transiently and then went green. Docs were last written 02:00 and are stale (§P2-1).
- `run-paper.sh --prepare` was run read-only; it failed safely due final11 stack. No source edits, no git, no experiment/final11/Docker start-stop-exec.
- final11 remains paused/running (14 stack containers, 65 workspace containers, 5,522 events); do not start a new run until it is archived/stopped and a fresh server-data dir is provisioned.
