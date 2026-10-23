# Long-run framework stability check — 2026-10-23

Scope: current `minecraft-harness` after the 8-agent paper-scale fixes.
This is a **mechanism/soak check**, not a 54-hour Minecraft capability run.
No paid model or real Minecraft server was started for this check.

## Verdict

**PASS for bounded offline orchestration, crash recovery, budget durability,
pause/resume, process lifecycle, memory stability and the known real-Minecraft
8-agent smoke path.**

**NOT a sign-off for a 54-hour real Minecraft run.** The remaining blockers are
storage headroom, active Pi-session rotation/operator alerting, and the absence
of a real long-duration server/network soak. Details below.

## What was run

| Check | Result | Evidence |
|---|---|---|
| Long-run targeted tests | **87 passed, 73 subtests** | `tests/test_stability.py`, `test_supervisor_longrun.py`, `test_cli_longrun.py`, `test_recorder_longrun.py`, `test_gateway_longrun.py`, `test_world_checkpoints.py`, `test_budget_longrun.py`, `test_longrun_config.py` |
| Offline 4-agent Supervisor soak (300 s, real Node, fake model) | **PASS 16/16 checks**, 4,447 completed model calls | `validation/supervisor-soak-20261023-check.json`; run `runs/longrun-soak-check-20261023/` |
| 3 × SIGKILL runtime faults | all recovered | crashes at 75/150/225 s; recovery 0.31–0.68 s |
| 2 × pause/resume windows | stable | calls unchanged during pause; no admission burst after resume |
| Node runtime tests | **51 passed** | `npm --prefix bot_runtime test` with the pinned host dependency tree |
| FD cleanup check | **PASS** | `validation/gateway-fd-20261023.json`: 12 start/close cycles at 7 FDs; SIGKILL-recovery 14–15, final 7 |
| Full Python suite | **757 passed, 3 skipped, 734 subtests** | current tree |
| 8-agent real Minecraft smoke | 5-minute run had 3 bot kicks / 9 old heartbeat-watch timeouts; post-fix 2-minute recheck had **0 / 0**, pending `{}` | `mc-runs/eight-agent-paper-smoke-20261023/`, `mc-runs/paper8-recheck-20261023/` |

## Soak details

The 300-second offline soak used four fake agents and real Node worker
processes; it killed the runtime three times and paused twice.

- **4,447** model completions; **4,438** submissions; **4,434** succeeded.
- **4,447** `model_admitted` == **4,447** `model_usage`; final `pending: {}`.
- No duplicate execution dispatch; submission request sequences unique and contiguous.
- All agent event buffers acknowledged before stop; no live child processes after stop.
- Budget never reset across crashes; pause windows did not admit calls.
- Recovery events durable; 3/3 runtime recoveries completed.
- Runtime final state: world epoch 3, 4 sessions connected, no lease expiry/dropped events.
- Python RSS peak **88.8 MiB**; Node peak **73.7 MiB**; Python post-warmup growth **10.1 MiB**; final-window range **1.3 MiB**.
- tracemalloc net live growth after warmup **0.59 MiB** (limit 8 MiB); no evidence of a per-round leak in this window.
- Run dir after soak: **302 MiB** (SQLite ledger 82 MiB + artifacts/sessions).

## Current mechanisms confirmed

- Durable SQLite ledger, submission/execution state, recovery epochs, world checkpoints and evidence IDs.
- Budget wall/call/cost admission and settlement; pending reservations are cleared at shutdown.
- Runner lease/watchdog, SIGKILL recovery, source snapshot, runtime recovery limits.
- Pause/resume gate, active-wall accounting, no admission during pause.
- Supervisor event-loop responsiveness: three forced runtime deaths recovered in under 0.7 s while the 300 s workload continued.
- Pi runtime tree/package hashes are frozen and verified on restart.
- Pi session/artifact retention now prunes/archives completed sessions; active-session cap aborts cleanly.
- Docker-gated compose tests remain the only skipped Python tests (3).
- Node runtime tests pass when run against the installed pinned dependency tree.

## Residual risks — must address before a 54h run

### 1. Storage headroom is the first hard limit

The current run cap is `storage.max_run_bytes=24 GiB` (default). A real
8-agent 5-minute run produced **~85 MiB** of run-dir data. If that rate were
sustained linearly, 24 GiB would be reached in roughly **24 hours**, before a
54h run completes. Artifact retention/session pruning may cap this, but no
real 8-agent or 4-agent multi-hour storage curve exists yet.

Actions:

- Measure `du run_dir` every minute for a 1–2 hour real run.
- Set an operator alert threshold at 50%/75%/90% of `max_run_bytes`.
- Decide formal 4-agent small-run `max_run_bytes` (likely 48 GiB) and
  `pi_session_max_bytes`/`pi_artifact_max_bytes` from measured growth.
- Do not extrapolate from the 300 s fake soak; it has much higher call rate and
  different artifact sizes.

### 2. Active Pi-session cap stops the agent, not auto-rotate

`_check_active_session_limit` sets `session_limit`, stops the Pi runner, and
records `pi_session_size_limit`; the Supervisor does not automatically rotate
that agent's session in place or raise an operator alert. A long run can lose
one agent while continuing. Pre-spawn retention/rotation works, but active-run
rotation is not automated.

Actions:

- Add an in-run rotation/restart policy for `pi_session_size_limit`, or
  explicitly accept agent degradation with an operator alert.
- Add alert thresholds for `pi_retention_pressure`, `agent_degraded`,
  `observer_degraded`, repeated runner recovery and `agent_event_transport_error`.

### 3. Real-world long-duration coverage

The soak validates orchestration and fake-runtime recovery, not:

- 54h real Minecraft server/tick continuity;
- real server crash/restart plus world checkpoint recovery;
- provider outage/rate-limit behaviour over hours;
- model/session growth over thousands of real Pi turns;
- the 3 Docker-gated compose tests (still skipped in this host window);
- real network/latency jitter at 8-agent concurrency.

Actions before the paid run: one 1–2h real/fake-model Minecraft soak with a
server restart, one provider-failure injection, and one container Pi storage
measurement; then a staged increase to 54h.

### 4. Supervisor memory at 8-agent scale

The soak kept Python RSS at ~89 MiB and stable. The real 8-agent performance
run observed a ~310 MiB Supervisor RSS peak (different workload and artifacts).
There is no hard memory cap in the Supervisor; only disk/run quota is enforced.

Actions: monitor RSS in the real run; add an alert/soft guard for sustained
growth. Do not claim an 8-agent 54h memory bound from the 5-minute run.

### 5. Residual security/integrity deviations

The `/proc` credential guard and loopback peer allowlist are in place, but the
Pi model/RPC/bridge token threat model remains a documented deviation. The
container Pi path and bridge are verified by smoke, not by a red-team run.

## Recommended gate for a real 54h run

1. 1–2h real Minecraft + fake model soak with one server restart and storage/RSS curve.
2. 1h real Pi session with retention/rotation intentionally triggered.
3. Provider failure injection (401/429/5xx) proving `model_failed` fail-fast and bounded billing.
4. Real container Pi smoke on the exact final image/mounts; record image IDs.
5. Set storage alert thresholds and a 48 GiB run cap based on measurements.
6. Freeze the tree/source/Pi runtime hashes and run from the frozen checkout.
7. Start 54h only with a human monitor for `agent_degraded`, `observer_degraded`, storage and model errors.

## Reproduction

```bash
# targeted
PYTHONPATH=$PWD/src:$.../.venv/lib/python3.11/site-packages \
  python -m pytest -q tests/test_stability.py tests/test_supervisor_longrun.py \
  tests/test_cli_longrun.py tests/test_recorder_longrun.py \
  tests/test_gateway_longrun.py tests/test_world_checkpoints.py \
  tests/test_budget_longrun.py tests/test_longrun_config.py

# bounded offline soak
uv run python scripts/longrun_soak.py --duration-s 300 --trace-memory \
  --run-dir runs/longrun-soak-check-20261023 \
  --output validation/supervisor-soak-20261023-check.json

# fd lifecycle
uv run python scripts/gateway_fd_check.py --output validation/gateway-fd-20261023.json
```

Source commit at check time: `43f3b1c` (plus local evidence only; no code edits made by this check).
