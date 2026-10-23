# 8-agent paper-aligned performance smoke — 2026-10-23

Operator-requested test: 8 player agents using the **paper-aligned prompt/tool/lifecycle**
semantics for five minutes, on a real fresh Minecraft world with real DeepSeek, plus
end-to-end chain sampling.

> The paper protocol itself is four players. This run uses
> `experiment.profile: paper_aligned` with an explicit
> `experiment.paper_scale_override: true` (generator `--mode paper --allow-paper-scale`).
> It is a capacity/performance test, **not** a paper replication claim.

## Deployment

```text
project: paper8-smoke
agents: 8 players + 1 LLM Observer
runner_mode: container, runner_runtime: pi
runner_egress: model_only (default)
world: fresh infra/server-data-8
wall: 5 minutes; model: deepseek-flash
```

Ports were moved to 3301..3316 to avoid a stale host listener on 3110.

## Result

- 8/8 players `runtime_ready`; all joined the real overworld.
- 9 Pi runners started (8 players + Observer); 9 `runner_ready`.
- **model_request / model_response: 263 / 262**
- **tool_intent / tool_result: 273 / 266**
- Observer reports: **2** (`observer_report`), 1 observer outcome.
- `execution_submitted/accepted/finished`: 146 / 292 / 133
- `execution_failed`: 24, `execution_timed_out`: 1
- `bot_kicked`: 3, `bot_end`: 4, `runtime_ready`: 12, `runtime_exit`: 12
- `infrastructure_error`: 0
- status: **budget_exhausted** (5-minute wall; cost was still small)
- world saved/stopped; compose stack down; no harness runner containers left.

### Why 12 runtime reads / 12 exits for 8 players

The runtime reports session-level ready/exit as well as runner-level events; the
extra events include the Observer and transient runtime reconnects. No
`infrastructure_error` was recorded.

## Chain measurements (perf probe, 5s sampling)

Probe: `scripts/perf_chain_probe.py`, run dir
`mc-runs/eight-agent-paper-smoke-20261023/`, output
`validation/eight-agent-paper-smoke-20261023/perf.json`.

| Metric | p50 | p95 | max | mean |
|---|---:|---:|---:|---:|
| model request→response | **1.642 s** | **5.834 s** | **18.188 s** | 2.310 s |
| tool duration (ledger) | **207 ms** | **30.671 s** | **65.204 s** | 3.818 s |
| bot `/health` RTT | **1.60 ms** | **2.49 ms** | **4.45 ms** | 1.70 ms |

Other samples:

- Supervisor Python RSS peak: **324,554,752 bytes (~310 MiB)**
- Container CPU peak: **192.76%** (sum across sampled containers)
- Container memory peak: **1,401.9 MiB** (sum across sampled containers)
- Ledger bytes growth: **4.70 MB**
- Run-dir bytes growth: **68.7 MB** (~13.7 MB/min at this 8-agent load)
- Bot health was stable throughout; local control plane/HTTP path is not the
  bottleneck.

## Findings

1. **Model provider latency dominates the model chain**: p50 ~1.6 s, p95 ~5.8 s
   under 8-agent concurrency. This is expected for a remote provider and is the
   main long-tail source.
2. **Tool duration has a very long tail**: p95 ~30.7 s, max ~65.2 s. Minecraft
   program execution/long-running tools dominate this tail; not the RPC/health
   path. Per-tool breakdown should be added before a long run.
3. **Supervisor/event loop resource use is significant at 8 agents**:
   ~310 MiB RSS and 193% aggregate container CPU. A 9-runner stack is viable but
   needs headroom; the host should be monitored.
4. **Heartbeat-timeout noise after an agent stopped**: `player1` stopped at
   ~06:37:34, but the heartbeat watcher repeatedly logged
   `runner_heartbeat_timeout` for its old PID every 5 s from 06:40:29 onward
   (9 events) even though the process had already finished. Fixed in
   `_heartbeat_watch` (returns once `finished_event`/result exists); validated
   by the 2-minute 8-agent recheck below: **0 heartbeat timeouts**.
5. **Observer call was still reserved at shutdown**: budget state had one
   pending observer reservation with reserved cost 3.0 when the run ended.
   Fixed by `Budget.recover_all_pending()` during Supervisor shutdown; validated
   by the recheck: final budget `pending: {}`.
6. **Tool/monitoring chain is not the bottleneck**: bot health RTT <5 ms and
   ledger/artifact append throughput handled 263 model calls in five minutes.

## Reproduce

- Run dir: `mc-runs/eight-agent-paper-smoke-20261023/`
- Generated config/compose/wrapper log and perf JSON:
  `validation/eight-agent-paper-smoke-20261023/`
- Command used `run-paper.sh --start` with the generated 8-agent config and a
  fresh `server-data-8`; final11 was already stopped/archived.

## Next

- Re-run after the heartbeat-watch fix and with a longer provider budget.
- Add per-tool latency breakdown (CLI type, queue wait, execution wait).
- Validate observer in-flight settlement on wall/budget shutdown.
- For 54h: monitor model p95, tool p95, run-dir growth (~14 MB/min at this load),
  supervisor RSS/CPU, and heartbeat timeout rate; set higher
  `runner_heartbeat_timeout_s` / backpressure if 8+ agents are used.


## Fix validation: 2-minute 8-agent recheck (after fixes)

A fresh 8-player paper-scale deployment (`paper8-recheck`, fresh
`server-data-9`, ports 3401..3416) was run after the fixes:

- 8/8 players `runtime_ready`, 9 runners ready.
- **model_request / model_response: 157 / 153**
- **tool_intent / tool_result: 181 / 176**
- `bot_kicked`: **0**
- `runner_heartbeat_timeout`: **0**
- final budget `pending`: **{}**
- `infrastructure_error`: **0**
- status: `budget_exhausted` (2-minute wall configuration).

The recheck also loaded the new `spigot.yml`
(`moved-too-quickly-multiplier=100.0`), eliminating the
`multiplayer.disconnect.invalid_player_movement` kicks seen in the earlier
5-minute run. The 2-minute run did not produce an Observer report (expected
for this short window); the 5-minute run produced two.

The model path now reuses one `httpx.Client` per Pi proxy across turns instead
of recreating a connection pool (and UDS connection for the forwarder path) on
every model call. The recheck's 153 matched request/response pairs had
p50 **1.31 s**, p95 **4.17 s**, max **7.84 s** (the earlier 5-minute run had
p50 1.64 s / p95 5.83 s / max 18.19 s; different window and concurrency, so
this is directional rather than a controlled benchmark).
