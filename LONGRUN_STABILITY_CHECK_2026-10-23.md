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

At check time the default run cap was `storage.max_run_bytes=24 GiB`. A real
8-agent 5-minute run produced **~85 MiB** of run-dir data. If that rate were
sustained linearly, 24 GiB would be reached in roughly **24 hours**, before a
54h run completes. Artifact retention/session pruning may cap this, but no
real 8-agent or 4-agent multi-hour storage curve exists yet.

**Update after this check:** `max_run_bytes` has been raised to
**100 GiB (107,374,182,400 bytes, ~107 GB)** in the default config, all
`configs/*.yaml` templates and the Pi-budget fallback. At the same linear
8-agent rate this gives roughly 100 hours of headroom, so a 54h run fits the
current cap in the crude estimate — but the real multi-hour storage curve is
still required before treating that as proof.

Actions:

- Measure `du run_dir` every minute for a 1–2 hour real run.
- Set an operator alert threshold at 50%/75%/90% of `max_run_bytes`.
- Keep the 100 GiB `max_run_bytes` and set `pi_session_max_bytes`/
  `pi_artifact_max_bytes` from measured growth.
- Do not extrapolate from the 300 s fake soak; it has much higher call rate and
  different artifact sizes.

### 2. Active Pi-session rotation (fixed after this check)

The original check found that `_check_active_session_limit` only stopped the
agent. Since then the cap is 2 GB, 80/90/95% alerts and 60s growth samples are
recorded, and at 95% the player Pi runner performs a bounded in-place
`new_session` rotation with JSONL archive + compaction handoff (default max 3).
The Observer remains on the hard-stop path. See
`IMPLEMENTATION_STATUS.md` 2026-10-23 Pi session cap / auto-rotation.

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
5. Set storage alert thresholds against the 100 GiB run cap based on measurements.
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

Source commit at check time: `43f3b1c`; after the check the cap was raised to 100 GiB. See the storage-risk note above.

## Follow-up 2026-10-23 21:xx：真实 server 崩溃恢复 + 收尾重试

本轮针对 90 分钟/更长 run 的稳定性再检查，发现并修复：

1. **recovery/startup 的 session fence 仍是单次 10s**
   - 正常 `close()` 已带 bounded retry，但 `_recover_runtime`、startup 初始 fence、
     operator checkpoint/freeze 和 `gateway.recover/reset_world` 仍直接调用
     `fence()`；长 run 中一次 runtime fault / server crash 后的 sandbox teardown
     仍可能超过 10s。
   - 修复：新增 `ExecutionGateway.fence_with_cleanup()`，上述路径统一使用同一
     bounded cleanup budget（`close_request_timeout_s` / `close_timeout_s`）。
   - 关键修正：对 **owned/auto_spawn** runtime，检测到 runtime 进程已消失或短
     超时后立即让 `_fence_owned_processes` 兜底，避免把外部 runtime 的 60s
     teardown 预算用在已死亡的进程上（离线 soak 曾因此 60s 超时）。

2. **长 run 存储压力没有分级告警**
   - `_resource_guard` 现在在 `max_run_bytes` 的 50%/75%/90% 发一次
     `storage_alert`（含 measured/limit/free bytes），在硬上限前给 operator
     归档/扩 cap 的时间。

3. **部署源漂移会在付费 run 启动后立即失败**
   - 真实测试时发现 DEST `src` 已更新而 H `src` 仍是旧版，Supervisor 报
     `AttributeError: 'ExecutionGateway' object has no attribute 'fence_with_cleanup'`。
   - `run-paper.sh` preflight 现在比较 `$DEST/src/minecraft_harness` 与
     `$H/src/minecraft_harness`，漂移直接失败（可用
     `PAPER_ALLOW_SOURCE_DRIFT=1` 显式接受）；H 已同步并移除了误拷到
     `src/minecraft_harness/` 顶层的 `gateway.py`/`report.py`。

### 验证

- **真实 server 崩溃恢复**（`longrun-server-crash-2`）：
  - 3 分钟 run，4/4 ready 后 `docker kill -9 paper4-smoke-server`；
  - 21:32:02 检测到 `server readiness failed`，触发
    `runtime_recovery_started`；
  - 21:32:07 server 容器重新 Up，21:32:27 `runtime_recovery_completed`，
    world_epoch=1，4/4 runtime 重新 ready，status 回到 `running`；
  - wall 到点后 `run_status=budget_exhausted`，reports 正常写入，
    `SUPERVISOR_CONTAINER_EXIT ... rc=0`，无 cleanup/finalization 失败；
  - 126 calls / `$0.3517`。
- **120 秒 offline soak**（4 agents，真实 Node，fake model）：
  - **16/16 checks passed**，3 次 SIGKILL runtime 均恢复，pause 期间 calls 稳定，
    pending 清空，无 live child，RSS/tracemalloc 均在阈值内；
  - 输出：`validation/supervisor-soak-20261023-213511-b.json`。
- 修复前的同 soak 曾因 auto_spawn runtime 已死仍在跑 60s cleanup retry 而
  TimeoutError；修复后通过。

### 仍建议

- 正式 90 分钟后、更长 run 前，仍应做 1–2 小时真实 server/storage/RSS 曲线；
- `live-test-1` 的 2 个在途 model call / 2 个 unknown usage 仍会让 report 的
  token/cost 为 null（保守设计）；
- `/proc` credential exposure（P1-7）仍是安全偏差，不是本轮的稳定性阻断。

## Follow-up 2026-10-23 22:xx：bot kick / protocol churn 调优

- 20 分钟 run 基线：64 kicks / 6 DecoderException / 71 runtime restarts，
  属于高负载下 Mineflayer 协议与移动检查的稳定性问题。
- 服务器调优并重建 image：
  - `network-compression-threshold=-1`
  - `moved-too-quickly-multiplier=1000.0`
  - `moved-wrongly-threshold=1.0`
- 5 分钟 fresh-world 验证：3 kicks、0 DecoderException、0 moved-too-quickly
  警告，按 20 分钟折算约 12 kicks（基线 64）；run `rc=0`、无 cleanup 失败。
- 残余 3 次 invalid_player_movement；正式 run 前建议再跑一轮 20 分钟调优。
