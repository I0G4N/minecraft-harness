# P1 soft reset / physics recovery design (working contract)

Branch: `p1-soft-reset` (base `b38d3b6`). Owner: Lead. Implementers follow this file;
if the contract must change, message the Lead before deviating.

## Problem evidence (from the four final 60-minute runs + p8-diag-05/07)

- Hard worker exits per 60-minute run: 62 / 51 / 66 / 62 (241 total).
  Roughly 70-85% of them are `NONFINITE_BOT_STATE` (position/yaw NaN) hard exits.
  The rest are uncaught async errors thrown by agent/or plugin code
  (`Digging aborted`, `TypeError`, deliberate `throw new Error('resync')`), each of
  which currently ends in `process.exit(1)`.
- Every hard restart increments `runtime_epoch`, drops the VM context (agent
  helper library in `globals`, e.g. `globals.H`), kills the running program and
  drops all leftover timers/listeners. This is the real "restart damage".
- One agent (`player5`, p8-f3-01) reported "lost physics engine, cannot walk".
  Its execution records show it was physically trapped in terrain and later
  moved again after digging out; `bot.physics` is intentionally hidden from the
  agent proxy. Do not "fix" that by exposing physics. But do add a stall
  watchdog so a genuinely frozen client (unloaded-chunk early return in
  Mineflayer `tickPhysics`) recovers.

## Goal

1. NaN/precision corruption must never reach `process.exit(1)` directly.
   Catch it at the physics boundary, restore the last good client state, and
   keep the worker alive.
2. When the bot client must be recreated, recreate **inside the same Node
   process** ("soft reset"): VM context, `globals`, `recentLogs`, agent-visible
   state stay alive. Only the Mineflayer connection is replaced.
3. Hard restart stays as the last-resort fallback when a soft reset itself fails.
4. Agents/observers can see a soft reset in the ledger; it must not be counted
   as a paper-style server restart nor as a `globals_lost` hard restart.

## Worker (`bot_runtime/src/worker.js`) changes

### State

```js
let bot;                 // current real Mineflayer bot
let botGeneration = 0;   // increments for every bot client instance
let botReady = false;    // current bot is spawned and instrumented
let agentBot;            // ONE stable proxy for all executions; resolves current bot
let resetState = null;   // { promise, reason } when a soft reset is in flight
let pendingExecutions = []; // execute messages that arrived during reset/startup
```

`makeAgentFacingBot` currently captures `real`; change it to
`makeAgentFacingBot(() => bot)` (getter) so references captured by an old
program cannot silently act on a future bot, and the proxy stays valid for the
next execution. Keep the existing hidden/readonly/snapshot behavior exactly.

### Bot creation (factor out of top level)

`async function createBotClient()`:

- `mineflayer.createBot(...)`, `bot.loadPlugin(pathfinder)`, wait `spawn` with a
  bounded timeout (25 s) and the existing kicked/end/error rejection handling.
- increment `botGeneration`, assign `bot`, install physics guard, instrument
  methods/emitter, install event forwarding, viewer hooks.
- return the bot.

`async function startBot({ initial })`:

- create client; on success `botReady = true`;
- if `initial`: send existing `{type:'ready', ...}` unchanged;
- else: send `{type:'soft_ready', reason, generation, snapshot, duration_ms, soft_resets}`.

### Soft reset (`async function softReset(reason, detail)`)

Single-flight. Sequence:

1. If a soft reset is already running, return the same promise.
2. Rate limit: if 6 soft resets happened in the last 600 s, treat as a hard
   worker fault (`{type:'fatal'}` + `process.exit(1)`) so a poisoned worker
   cannot loop forever.
3. `process.stderr.write('SOFT_RESET_START ' + JSON.stringify({reason, generation, detail, execution_id}) + '\n')`.
4. If a foreground execution is running: complete it exactly once with
   `{name:'SoftResetError', message:'Bot client reset: '+reason, stack}` and
   `needs_reset:false` (server marks the execution failed, not the session).
   Mark its scope `aborted`/closed so no second completion is sent.
5. Close every retained leftover scope: clear timers, remove listeners, mark
   closed. Do not leave program callbacks that can still call the old bot.
6. Quiesce native actions on the old bot (`quiesceNativeActions`).
7. Send `{type:'soft_reset', reason, generation: botGeneration + 1, execution_id, detail}`.
8. Disconnect old bot: `bot.quit()` (best effort); wait for `end`/socket close at
   most 3 s; then continue regardless (kill socket).
9. Create new client with up to 3 attempts, 1.5 s backoff. The identity proxy
   rejects a login while the previous connection is still `active`, so retry is
   required, not optional. On success: `botReady = true`, send `soft_ready`,
   emit stderr `SOFT_RESET_READY`, and start draining `pendingExecutions`.
10. On failure: stderr `SOFT_RESET_FAILED`, send
    `{type:'soft_reset_failed', reason, error}` then `{type:'fatal', ...}` and
    `process.exit(1)`. The server confirms the old sandbox and hard-restarts.

While `resetState` is set or `botReady` is false, `execute` messages are queued
in `pendingExecutions` (bounded: if more than 2, keep the newest and fail the
older with `Worker busy during bot reset`). `abort_execution` / `cancel_execution`
for a queued execution must be answered without starting it.

### Triggers

| Trigger | Action |
| --- | --- |
| physics guard: nonfinite output prevented | restore, stay alive, throttle event |
| physics guard: 3 consecutive bad ticks, or >=10 in 60 s | `softReset('nonfinite_bot_state')` |
| state guard (500 ms) still nonfinite after restore attempts | `softReset('nonfinite_bot_state')` |
| physics stalled (no `physicsTick` for `physicsStallMs`, alive, spawned) | `softReset('physics_stalled')` |
| unexpected `bot.end` / `kicked` | `softReset('bot_disconnected')` with 3 attempts |
| `uncaughtException` / `unhandledRejection` (non pathfinder-rejection) | complete current execution as failed, `softReset('uncaught:' + name)` |
| background job ends with unfinished native actions | `softReset('unfinished_actions')` instead of `process.exit(74)` |
| soft reset failed or loop cap | hard restart via `fatal` (existing path) |

`process.on('disconnect')`, IPC send failures, and unconfirmed execution-boundary
failures remain hard exits.

## Physics guard (`bot_runtime/src/physics-guard.js`, new)

Pure, testable module. No Mineflayer import required; operate on duck-typed objects.

```js
export function clientState(entity)  // {pos:{x,y,z}, vel:{x,y,z}, yaw, pitch, onGround}
export function stateIsFinite(state)
export function installPhysicsGuard(bot, {
  stallMs = 15000,
  onPrevented,   // ({input, output, badFields, recentApi})
  onRepeated,    // (stats) => softReset
  onStalled,     // ({msSinceTick}) => softReset
  onTick,        // track lastPhysicsTickAt
}) // => { uninstall(), stats() }
```

Implementation:

1. Wrap `bot.physics.simulatePlayer` (Mineflayer calls
   `physics.simulatePlayer(new PlayerState(bot, controlState), world).apply(bot)`):
   - snapshot the current `bot.entity` state before the call;
   - call the original;
   - if the returned PlayerState has nonfinite `pos`/`vel` (or the current
     entity yaw/pitch is nonfinite), overwrite the returned state with the last
     known finite state (or the pre-call snapshot) and zero velocity, so
     `.apply(bot)` can never publish NaN;
   - record `badFields`, keep a bounded ring of recent `recentApi` entries;
   - fire `onPrevented`, bump consecutive counter, fire `onRepeated` at 3
     consecutive or 10 in 60 s; reset counters on a good tick.
2. Track `bot.on('physicsTick')`: update last good state and `lastPhysicsTickAt`;
   first tick after `spawn` sets a `spawnedAt` timestamp.
3. `setInterval(500ms)` stall check: if
   `botReady && bot.isAlive !== false && Date.now() - spawnedAt > stallMs` and
   `Date.now() - lastPhysicsTickAt > stallMs`, fire `onStalled` once per stall
   episode (do not spam). This covers Mineflayer `tickPhysics` early-returning
   forever because `bot.blockAt(position) == null` (unloaded chunk/stale
   client position), the concrete "cannot walk despite physicsEnabled" failure.
4. Never throws; all optional chaining / try-catch best effort. `uninstall()`
   restores the original `simulatePlayer` and clears the interval.
5. Keep the existing 500 ms `startBotStateGuard` in worker.js as a final
   backstop, but change its action: try restoring the last good state; if still
   nonfinite after 3 checks (1.5 s) call `softReset`, never `process.exit(1)`.
   Keep the `NONFINITE_BOT_STATE` stderr line for forensics (throttled, max 5).

## Worker -> server IPC (new messages)

```json
{"type":"soft_reset","reason":"nonfinite_bot_state","execution_id":null,
 "generation":4,"detail":{"bad_fields":["pos.x"],"recent_api":[]}}
{"type":"soft_ready","reason":"nonfinite_bot_state","generation":4,
 "snapshot":{"mode":"minecraft","username":"player5","position":{"x":1,"y":2,"z":3},
             "health":20,"food":20,"globals_preserved":true},
 "duration_ms":1832,"soft_resets":3}
{"type":"soft_reset_failed","reason":"nonfinite_bot_state","error":"..."}
```

Ordering guarantees: a `completed` for a running execution (if any) is sent
before `soft_reset`; `soft_ready` is sent only after the new client spawned.

## Session (`bot_runtime/src/server.js`) changes

- Config: `softResetTimeoutMs` (integer, 5000..120000, default 45000, env
  `SOFT_RESET_TIMEOUT_MS`). Pass `physicsStallMs` (integer, 3000..60000,
  default 15000, env `PHYSICS_STALL_MS`) into `workerConfig`; `worker-host.js`
  must copy it into `safeWorker` (allowlist) and validate it.
- New session fields: `softResetting` (bool), `softResetCount` (int),
  `workerGeneration` (int, from soft_ready/ready).
- Message handling:
  - `soft_reset`: set `softResetting=true`, create `softResetDone` promise,
    start `softResetTimer`; emit
    `runtime_soft_reset` with `{reason, execution_id, generation, detail, globals_lost:false}`.
    If `this.current` is still running, finish it as `interrupted` with
    `reset_reason:'soft_reset'` (defensive; normal path already completed it).
  - `soft_ready`: clear timer, `softResetting=false`, `softResetCount++`,
    resolve `softResetDone`; emit `runtime_soft_reset_ready`
    `{generation, duration_ms, soft_resets, snapshot, globals_lost:false}`;
    call `drain()`.
  - `soft_reset_failed`: emit `runtime_soft_reset_failed`, `killWorker(...)`.
  - `softResetTimer` expiry: emit `runtime_soft_reset_timeout`,
    `killWorker('soft_reset_timeout')`.
- `drain()`: before dispatching a queued program, if `softResetting` await
  `softResetDone`; also re-check after `ensureWorker()`. Do not otherwise block
  submission.
- Do **not** change `runtimeEpoch` on soft reset; do not clear `child.ready`;
  do not emit `runtime_reset`/`runtime_ready` with `globals_lost`.
- `killWorker` and `child.once('exit')` must clear the soft reset timer and
  resolve/reject the pending gate so `drain` cannot hang.
- `describe()` exposes `soft_resetting`, `soft_resets`, `worker_generation`.
- `/health` session metrics unchanged apart from `describe()`.

## Ledger / analysis

- Session event `runtime_soft_reset` (globals_lost false) is the authoritative
  record. Worker may additionally send `{type:'event', kind:'soft_reset'}` for
  agent-visible notification; do not duplicate the count.
- `src/minecraft_harness/recorder.py`: map `runtime_soft_reset` /
  `runtime_soft_reset_ready` to `player_event` rows with the same kind (never
  `runtime_reset`). Do not add `globals_lost` rows.
- `scripts/analyze_stability.py`: new flags/counters `runtime_soft_reset`,
  `runtime_soft_reset_ready`, `runtime_soft_reset_failed`, and
  `soft_reset_reasons`; keep `flags.runtime_ready` meaning "bot clients that
  ever became ready" (hard restarts = runtime_ready - 1 for one agent, but
  report hard restarts as count of `runtime_ready` minus initial per agent).
- `src/minecraft_harness/figures.py`: include `runtime_soft_reset` in the
  `runtime_resets` timeline items with their own kind; never in `restarts`.
- `scripts/summarize_rounds.py`: report hard restarts and soft resets
  separately in the aggregated report.

## Tests (must pass in the validation image)

- `bot_runtime/test/physics-guard.test.js`: fake bot/entity/physics; NaN from
  `simulatePlayer` is replaced before apply; consecutive counter triggers
  `onRepeated`; stall interval triggers `onStalled` after the configured time
  (use short stallMs); no throw when physics missing.
- `bot_runtime/test/soft-reset-session.test.js`: session-level gate with an
  injected fake remote worker: `soft_reset` event recorded, program queued but
  not dispatched until `soft_ready`, `soft_reset_timeout` kills the worker,
  runtimeEpoch unchanged, describe fields correct.
- `bot_runtime/tests/protocol-integration.test.js` (extend): real Mineflayer
  against the protocol fixture; a program throws async (or the fixture moves the
  bot into an unsent chunk for the stall trigger) -> worker sends
  `soft_reset`/`soft_ready`; the session records them; the **next** program in
  the same session still sees `globals.marker` written before the reset; no
  `runtime_reset`/`globals_lost` event is emitted.
- `bot_runtime/test/worker-host-config.test.js`: `physicsStallMs` copied and
  range-validated.

## Non-goals for this change

- No agent-facing `bot.physics` exposure.
- No change to `paper_single_program`, `leftover_policy`, lifecycle semantics or
  the execution-boundary security model.
- No phase-2 action coordinator / single-flight pathfinder work in this change.
- No attempt to make the agent "never interrupted": one in-flight program can
  still fail with `SoftResetError`; the point is that the session, globals and
  the bot client recover in ~1-3 s instead of losing everything.

## P1.1 hardening (2026-10-25, after the first real diagnostic and independent verification)

The first 20-minute 8-agent diagnostic (`p8-p1-diag-01`) exposed a reset path the
unit/E2E fixtures did not cover, and the independent verifier found five more
defects. The implementation now additionally guarantees:

- **Soft reset never inherits a closed program ALS store.** `performSoftReset`
  is invoked through `scopes.run(undefined, ...)`; all replacement-client
  creation, listener registration and teardown therefore run with no ambient
  scope. Closed program scopes stay fenced for leftover callbacks (no global
  fence-bypass flag).
- **A replacement client that fails after creation is destroyed**
  (`createBotClient` catch: quit + socket destroy). The identity proxy admits
  one active connection per username, so a leaked login would otherwise make
  every retry fail with `socketClosed`.
- **Bounded retry budget inside the session gate.** Initial startup keeps the
  25 s spawn timeout; replacement clients use 10 s, 3 attempts, 2 s backoff,
  0.5 s login grace and a ≤3 s quit wait (worst case ≈ 37.5 s). The session
  `softResetTimeoutMs` default is 60 000 ms (env override, valid 5 000–120 000).
- **Completion-first gate.** The worker still sends `completed`
  (`reset_reason: soft_reset`) before `soft_reset`. The server opens the
  dispatch gate synchronously from that completion so drain cannot dispatch the
  next program in the IPC gap; exactly one `runtime_soft_reset` event is
  recorded.
- **Generation in the initial `ready` message.** The server adopts it, so the
  stale-generation guards have a defined baseline after a hard restart.
- **Physics guard escalation without a last-good state.** A physicsTick whose
  applied client state is still nonfinite counts as a bad tick (counters are
  only cleared by a finite tick), and when no finite state exists to restore
  the guard escalates on the *first* unrecoverable bad tick (position NaN
  otherwise freezes Mineflayer tickPhysics through its `blockAt(NaN)` gate, so
  the 15 s stall watchdog was the only recovery). The 10-in-60 s branch is
  kept as defense in depth but is normally unreachable because a finite tick
  clears the window.
- `soft_reset` and `soft_ready` generation semantics are unchanged: soft_reset
  advertises the expected new generation, soft_ready the actual installed one.
