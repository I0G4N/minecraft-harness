# P1: worker restart reduction and in-process soft bot reset

Date: 2026-10-25. Branch `p1-soft-reset`, base `b38d3b6` (P0). Commits:
`296c306` (P1), `7e365b1` (ALS hotfix), `e978968` (verifier hardening),
`a71e8f4` (immediate physics escalation), `08d9f50`/docs.

## 1. Problem

The four final 60-minute, 8-agent paper-aligned runs (`p8-f1-02` .. `p8-f4-01`)
were stable in the narrow sense (no server kicks, no SIGABRT, no budget
failures) but the bot workers were restarting about once a minute:

| run | wall (min) | hard worker restarts | hard restarts/hour | globals_lost |
| --- | ---: | ---: | ---: | ---: |
| p8-f1-02 | 60.1 | 54 | 53.9 | 54 |
| p8-f2-02 | 60.2 | 43 | 42.9 | 43 |
| p8-f3-01 | 60.1 | 58 | 57.9 | 50 |
| p8-f4-01 | 60.1 | 54 | 53.9 | 54 |
| p8-diag-05 (20 min) | 20.1 | 16 | 47.8 | 16 |
| p8-diag-07 (20 min, P0 image) | 20.2 | 17 | 50.5 | 17 |

(`hard worker restarts` = `runtime_ready` events minus the 8 initial startups; the
older P0 report's "25 restarts / 20 min" counted `runtime_exit` including the 8
shutdown exits, so its comparable in-run number is 17.)

Each hard restart means: the Node worker process exits, the VM context (agent
helper library in `globals`, e.g. `globals.H`), all leftover timers/listeners
and the in-flight program are destroyed, a new sandboxed container is created,
Mineflayer reconnects and the agent has to re-bootstrap. 70-85% of exits were
`NONFINITE_BOT_STATE` (client physics produced NaN position/yaw, the worker
hammered the server with malformed movement and was killed after a 30 s grace);
the rest were uncaught async program/plugin errors (`Digging aborted`,
`TypeError`, agents deliberately throwing `Error('resync')` to force a restart).

The goal of P1 was therefore two-fold:

1. stop treating nonfinite client physics and ordinary async program errors as
   worker-fatal;
2. when the Mineflayer client really must be recreated, do it **in the same
   Node process** so the VM context and agent globals survive.

## 2. Root causes found (beyond the original diagnosis)

- The client physics wrapper is the right boundary: Mineflayer calls
  `physics.simulatePlayer(new PlayerState(bot, control), world).apply(bot)`; a
  NaN `pos`/`vel`/`yaw` published by that call freezes `tickPhysics` (the
  `blockAt(NaN)` early return) and poisons all subsequent movement packets.
- Hard restarts were being triggered by *ordinary program bugs*. Any uncaught
  async throw produced `process.exit(1)`.
- Real-client validation (diagnostic 1, `p8-p1-diag-01`) exposed an
  AsyncLocalStorage bug: a soft reset triggered from a program's closed scope
  created/instrumented the replacement client inside that scope. The
  production `chatDelivery=program_output` listener registration threw
  `Execution has ended`, the replacement client had already logged in, the
  identity proxy's single-active-connection slot stayed occupied and all three
  retries failed with `socketClosed` -> hard restart. Unit/E2E tests had used
  the default `chatDelivery=events` and missed it.
- Independent adversarial verification then found: a completion->drain
  microtask race that could dispatch a program into the reset window (it would
  run but be recorded interrupted), a leaked replacement login on
  `createBotClient` spawn timeout, a retry budget (83 s) longer than the
  session soft-reset timeout (45 s), a generation-reset ambiguity after hard
  restarts, a no-last-good physics escalation gap, and a global fence bypass
  during the reset window. All were fixed and re-audited.

## 3. What changed

### Bot worker (`bot_runtime/src/worker.js`, `physics-guard.js`)

- `physics-guard.js` wraps `bot.physics.simulatePlayer`: nonfinite `pos`/`vel`
  (and nonfinite entity `yaw`/`pitch`) are replaced with the last known-good
  finite state **before** `.apply(bot)`, so a bad tick can never publish NaN.
  It records bad fields/recent API, escalates when a bad state cannot be
  restored (immediate when there is no last-good state, otherwise 3
  consecutive / 10 in 60 s), and runs a `physicsTick` stall watchdog
  (`physicsStallMs`, default 15 s) for the unloaded-chunk freeze.
- Bot creation is factored into `createBotClient()`/`startBot()`; one stable
  agent-facing proxy resolves the current client through a getter, so the
  hidden/readonly/snapshot/look/pathfinder semantics are unchanged.
- `softReset(reason)`: single-flight; completes the in-flight program with
  `SoftResetError` (`needs_reset:false`); closes leftover scopes/timers/
  listeners; quiesces native actions; sends `{type:'soft_reset', reason,
  generation, execution_id, detail}`; quits the old client (≤3 s wait); gives
  the identity proxy 500 ms to observe the close; creates a replacement client
  (10 s spawn cap, 3 attempts, 2 s backoff, failed clients destroyed) and sends
  `{type:'soft_ready', reason, generation, snapshot, duration_ms,
  soft_resets}`. The VM context and its `globals` are untouched;
  `runtime_epoch` does not change.
- Triggers: unrecoverable nonfinite state, physics stall, unexpected
  `bot.end`/kick, uncaught exception/unhandled rejection, unfinished native
  actions. A 6 resets / 600 s loop cap then falls back to a hard restart.
- Executions that race the reset boundary are queued (bound 2) and drained
  after `soft_ready`; `abort_execution`/`cancel_execution` for queued work are
  answered without running it.
- Viewer hooks are re-attached to every new client (chunk cache cleared,
  repaint scheduled); the viewer thread itself survives a soft reset.

### Session (`bot_runtime/src/server.js`, `worker-host.js`)

- New session gate: `runtime_soft_reset` / `runtime_soft_reset_ready` /
  `runtime_soft_reset_failed` / `runtime_soft_reset_timeout` events with
  `globals_lost:false`; `drain()` waits on the gate instead of dispatching into
  the reset window; `runtimeEpoch` and `child.ready` are unchanged.
- A completion carrying `reset_reason:'soft_reset'` opens the gate
  synchronously before the next program can be dispatched; the following
  `soft_reset` message only fills in the metadata and records exactly one
  runtime event.
- Config: `softResetTimeoutMs` (default 60 000, env `SOFT_RESET_TIMEOUT_MS`,
  range 5 000-120 000) and `physicsStallMs` (default 15 000, env
  `PHYSICS_STALL_MS`, range 3 000-60 000). `physicsStallMs` is copied through
  the launcher's allowlist into the sandbox.
- Initial `ready` advertises the client generation; describe() exposes
  `soft_resetting`, `soft_resets`, `worker_generation`.

### Ledger / analysis (Python)

`recorder.py` maps soft-reset events to `player_event` rows with their own
kinds (never `runtime_reset`/`globals_lost`); `analyze_stability.py` reports
`hard_restarts` and soft-reset counters/reasons separately;
`summarize_rounds.py` and `figures.py` keep soft resets out of the paper-style
server-restart series. Old analysis JSONs remain readable.

### Tests

- `bot_runtime/test/physics-guard.test.js`: NaN replaced before apply, yaw/pitch
  repair, counter/window escalation, no-last-good immediate escalation,
  stall watchdog episodes, no-throw without physics, uninstall fidelity,
  rate-limit helper.
- `bot_runtime/test/soft-reset-session.test.js`: gate ordering, running-program
  interruption, timeout/failure hard-restart path, completion-first gate race,
  duplicate/stale generations, startup/close races, config validation,
  worker-host allowlist.
- `bot_runtime/tests/protocol-integration.test.js`: real Mineflayer soft-reset
  E2E with the production `chatDelivery=program_output` config proving
  `globals` survive, exactly one `runtime_ready`, no `runtime_reset`, and that
  the next program sees pre-reset globals; plus a deterministic fake-mode
  queued-execution-during-reset regression.
- Full bot runtime suite: **89/89** (10 consecutive clean runs on the final
  tree). Independent verifier re-audited all six defects and re-ran the suite
  3x on the final image: 89/89 each.

## 4. Validation runs

All runs use the same 8-agent, paper-aligned, DeepSeek flash configuration as
the four final rounds (`paper_single_program`, `leftover_policy=allow`,
`chatDelivery=program_output`, 60 s default program timeout), a fresh seed-42
world, and a 20-minute (diagnostics) or 60-minute (confirmation) wall budget.
Images were overlay builds on the cached P0 worker/control/launcher images
(see caveat in section 6).

| run | duration | hard worker restarts | soft resets | soft reset failures | kicks / decoder / SIGABRT | worker_fatal | program calls | deaths |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| p8-p1-diag-01 (pre-hotfix) | ~4 min (aborted) | 1 | 1 failed -> hard restart | 1 | 0 / 0 / 0 | 0 | ~250 | 0 |
| p8-p1-diag-02 (ALS hotfix) | 20.2 min | **0** | 8 (all ready; 98-194 ms) | 0 | 0 / 0 / 0 | 0 | 1 573 | 24 |
| p8-p1-diag-03 (final) | 20.2 min | **0** | 2 (both at shutdown; 148 ms) | 0 | 0 / 0 / 0 | 0 | 936 | 27 |
| p8-p1-confirm-01 (final, 60 min) | 60 min | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |

Soft-reset reasons across the diagnostics: `uncaught:TypeError` 2,
`physics_stalled` 4, `bot_disconnected` 4 (2 mid-run, 2 at shutdown). Every
soft reset that occurred mid-run succeeded in ~0.1-0.2 s with
`globals_preserved:true`; no `soft_reset_failed`/`timeout` occurred on the
final image.

`p8-p1-diag-03` server log: 0 decoder exceptions, 0 invalid-move packets,
0 SIGABRT, 10 join/leave pairs (8 initial + 2 soft resets). `moved too quickly`
warnings were 1 793 / 20 min, higher than the baseline `p8-diag-07`
(484 / 20 min, 829 in `diag-05`); see residual risks.

## 5. Before / after

Hard worker restarts during a run (initial startups and shutdown excluded):

| configuration | 20-minute sample | per hour |
| --- | ---: | ---: |
| baseline P0 image (`p8-diag-05/07`) | 16 / 17 | 47.8 / 50.5 |
| four final rounds (P0) | - | 42.9-57.9 |
| **P1 final image, 20-minute diagnostics** | **0** | **0** |

Soft resets replace the hard restarts: cheap (~0.1-0.2 s), same worker
process, VM globals preserved, no container rebuild. The in-flight program is
still failed with a clear `SoftResetError`; everything else recovers.

## 6. Residual risks and limits

1. A soft reset still interrupts the foreground program once. The agent sees
   `SoftResetError` and must resubmit; the helper globals survive, so no
   re-bootstrap is needed.
2. Rate cap: 6 soft resets / 600 s, then a hard restart. This is strictly
   better than the old "first async throw -> hard restart", but a
   deliberately misbehaving program can still force a hard restart at that
   rate. Monitor `runtime_soft_reset` reasons.
3. Physics-stall detection is 15 s by design (false-positive safety). A frozen
   client stays unusable for at most that window, then soft-resets.
4. `moved too quickly` server warnings increased in the 20-minute final
   diagnostic (1 793 vs 484 baseline). This may be the cost of the local
   last-good-state restore (client/server position corrections) and/or of the
   two soft resets / agent behaviour; it did not produce kicks or invalid-move
   packets, but should be watched in the 60-minute confirmation run.
5. The pre-existing loop-deadline instrumentation (`instrumentLoopDeadlines`)
   corrupts some agent programs (SyntaxError `__mcWindowCheck` insertions),
   causing a high program error rate (29% failures in `diag-03`, 15-25% in the
   P0 rounds). This is unrelated to P1 and should be fixed separately.
6. Validation images were overlay builds on cached bases because the Docker Hub
   `node:` base pull times out from this host; the source is committed and the
   production Dockerfiles (bot -> worker/control/launcher) need one clean
   network build before a paper run.
7. This is still a 20-minute/1-hour evidence base, not the paper's 36h soak.
   The claim is "hard worker restarts and globals loss are removed in these
   runs", not "the framework is proven for 36h".
8. Archived run `p8-f3-01` `events.sqlite3` failed SQLite integrity checking on
   2026-10-25 (its run-time `reports/stability-analysis.json` is intact and was
   used above). Cause not investigated; unrelated to P1 runtime code, but worth
   checking the archive storage.

## 7. How to reproduce

```
# tests
cd bot_runtime && node --test test/*.test.js tests/*.test.js     # 89/89

# validation images (from cached P0 bases)
scripts/build_p1_validation_images.sh

# 20-minute 8-agent diagnostic
cd <runtime-harness-dir> && \
  PYTHONPATH=src python3 scripts/prepare_deployment.py --agents 8 --project paper8p1 \
  --mode paper --allow-paper-scale --runner-mode container \
  --server-data .../infra/server-data-8-p1 --compose-output .../.tmp-8-p1/compose.yaml \
  --config-output .../.tmp-8-p1/config.yaml --env-output .../.tmp-8-p1/env \
  --wall-minutes 20 --seed 42 --accept-eula --api-key "$DEEPSEEK_API_KEY" --force
scripts/start_8agent_round.sh <deployment-dir> mc-runs/<run> mc-logs/<run>.log
python3 scripts/analyze_stability.py mc-runs/<run> --server-log <server-data>/logs/latest.log
```

## 8. Artifacts

- Code: branch `p1-soft-reset`, HEAD `a71e8f4` (+ docs `08d9f50`), diff
  `b38d3b6..HEAD`.
- Design contract: `bot_runtime/P1_SOFT_RESET_DESIGN.md`.
- Tests: `bot_runtime/test/physics-guard.test.js`,
  `bot_runtime/test/soft-reset-session.test.js`,
  `bot_runtime/test/worker-host-config.test.js`,
  `bot_runtime/tests/protocol-integration.test.js`.
- Validation runs: `mc-runs/p8-p1-diag-01` (failed pre-fix evidence),
  `mc-runs/p8-p1-diag-02`, `mc-runs/p8-p1-diag-03`, `mc-runs/p8-p1-confirm-01`.
- Analysis: each run's `reports/stability-analysis.json`; worker stderr for the
  confirmation run under `mc-logs/p8-p1-confirm-01-workers/`.
