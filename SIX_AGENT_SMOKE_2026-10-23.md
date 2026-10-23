# Six-agent real Minecraft full-stack smoke — 2026-10-23

This was the operator-approved restart after archiving the paused final11 run.

## What was restarted / stopped

- `final11` Supervisor was stopped via its control API, the supervisor container
  was stopped, its 4-agent compose stack was taken down, and the final11 run
  directory was archived to
  `validation/final11-archive-20261023/final11-20261023T132948.tar.gz`
  (5.5 MB).
- The static 4-agent stack and its networks were removed; the old run evidence
  was preserved and not modified.

## Six-agent deployment

Generated with the checked-in dynamic deployment generator:

```text
agents=6 mode=strict project=mc-paper-6-smoke
runner_mode=container runner_runtime=pi
```

- Fresh `infra/server-data-6` (new world, EULA accepted).
- 6 identity/launcher/bot triples, one managed server, 6 worker networks.
- 7 runner containers per run: 6 player Pi runners + 1 LLM Observer runner.
- `runner_egress=model_only` (default for paper Pi containers), host UDS model
  forwarder, runner containers `--network none`.
- Real DeepSeek `deepseek-flash`; wall limit 3 minutes, 40 model calls.

Two deployment bugs were found and fixed during this smoke:

1. `deployment.py` rejected more than five coding workspace identities. It now
   accepts the dynamic bound up to 16 players + Observer (17).
2. The wrapper returned immediately after `wait_world` when `level.dat` appeared;
   a fresh server was still generating spawn and the Supervisor raced it with
   `RemoteProtocolError`. The wrapper now waits for the server's `Done (` log
   line before creating the world marker and starting the Supervisor.
   It also honors `HARNESS_PYTHON` (host `.venv` may be root/container-only) and
   archives the configured world root rather than a hard-coded final11 path.
3. The server image entrypoint was stale (did not apply `MAX_PLAYERS`). It was
   rebuilt from `infra/server.Dockerfile` so exact-N deployments get the right
   `max-players`; the smoke also bind-mounted the current entrypoint for the
   test.

## Smoke result

Run dir: `mc-runs/six-agent-smoke-20261023/` (local evidence).

- 6/6 `runtime_ready` events; all six bots joined the fresh world:
  player1..player6, overworld positions observed.
- `runtime_started`: 1
- `model_request` / `model_response`: **40 / 40**
- `tool_intent` / `tool_result`: **52 / 51**
- `agent_started`: 22, `agent_stopped`: 7
- `infrastructure_error`: **0**, `agent_degraded`: 0
- final status: **budget_exhausted** (40-call smoke cap reached)
- world saved and server stopped; compose stack was brought down; no harness
  runner containers left.
- Observer LLM runner started; no `observer_report` was published before the
  40-call cap (short smoke, accepted residual).

Comparison with final11: final11 was a paused 4-agent inprocess run; the new
smoke is a fresh 6-agent container/Pi run with real model and real Minecraft
world. It is a smoke, not a 54h paper run and not a capability result.

## Reproduce

See `validation/six-agent-smoke-20261023/` for the generated
`config.yaml` (no secrets), `compose.yaml`, wrapper log and reports.
The secret-bearing generated `.env` was removed after the run and is not in the
repository.

## Remaining work before a paid run

- Full Observer report under a longer interval/budget.
- Docker-gated complete-compose tests and a dedicated container Pi smoke.
- Address final11 archival data and decide whether to retain/delete the old
  world backups.
