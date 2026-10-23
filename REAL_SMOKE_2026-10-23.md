# Real Pi credential-isolation smoke — 2026-10-23

Scope: after the fix for residual #2 (Pi-local bridge/proxy/MC credentials), run a
real DeepSeek + real Pi CLI smoke in **process** and **container** modes. This is
a synthetic-runtime connectivity/credential smoke, not a Minecraft capability run.
`final11` was not touched.

## Code state

- Player Pi runner uses a one-shot inherited FD for the tool-bridge token
  (`HARNESS_PI_BRIDGE_TOKEN_FD`); the token is not in the Pi environment and the
  extension closes the FD before model tools run.
- The Pi CLI `models.json` provider gets a random per-run loopback `apiKey`
  (not the upstream key). `PiModelProxy` rejects missing/wrong credentials with
  401 before budget spend/upstream, and after the first authenticated request the
  runner deletes `models.json` (+ auth stores) and records
  `pi_cli_auth_redacted`. Real Pi does not reread that file per model call.
- `MC_AGENT_TOKEN` / `MC_SUBMISSION_AUTHORITY` are not in the Pi environment; the
  `mc` wrapper sources a mode-0600 `mc-env` file only for the `program_cli` child.
- Upstream key remains in the host/runner process; model-only containers use the
  host UDS `PiModelForwarder` and `--network none`.

## Host process smoke

Config: `experiment.task=observe_only`, strict profile, one player,
`runtime.mode=fake`, `runner_mode=process`, `runner_runtime=pi`, real
`deepseek-flash`, max 2 turns / 30 calls / 4 min.

Run dir: `.tmp-pi-credential-run2` (local evidence copied to
`validation/pi-real-smoke-20261023/`).

Result:

- run status: **completed**
- `model_request` = **15**, `model_response` = **15**, `model_error` = 0
- `tool_intent` = **15**, `tool_result` = **15**
- `pi_cli_auth_redacted` = 1; `agent_stopped` = 1 (`turns_exhausted`)
- `models.json` absent; `<state>/bin/mc-env` mode **600**
- bash `env` output: no `HARNESS_PI_BRIDGE_TOKEN`, no `HARNESS_RUNNER_TOKEN`,
  no `MC_AGENT_TOKEN`, no API key; only `HARNESS_PI_BRIDGE_URL`,
  `MC_AGENT_ID`, `MC_RUNTIME_URL`, `MC_RUNTIME_SOCKET`
- `grep -R -F "$DEEPSEEK_API_KEY"` over the run dir: **no matches**
- model usage recorded (e.g. first call 2607 prompt / 53 completion tokens) and
  all reservations settled.

## Container smoke

Config: same model/goal, `runner_mode=container`, `runner_runtime=pi`,
`runner_egress=model_only`, short `runner_container_shared_dir`.

Run dir: `.tmp-c3` (local evidence copied to
`validation/pi-real-smoke-20261023/`).

Manifest:

- `runner_mode=container`, `runner_runtime=pi`, `runner_egress=model_only`
- `model_forwarder.provider_credential_in_runner=false`
- container `/run/mc-model/socket` UDS; runner container `--network none`
- `agent_interfaces.player1.runner_pid = 1` (hardened runner is PID 1)

Result:

- run status: **completed**
- `model_request` = **13**, `model_response` = **13**, `model_error` = 0
- `tool_intent` = **15**, `tool_result` = **15**
- `pi_cli_auth_redacted` = 1; `agent_stopped` = 1 (`turns_exhausted`)
- container bash `env`: no bridge token, no runner token, no MC_AGENT_TOKEN, no
  API key
- `grep -R -F "$DEEPSEEK_API_KEY"` over run dir: no matches
- runner container removed after run (no leftover run containers)

## Bugs found by the smoke and fixed

1. `RunnerManager._validate_model_forwarder` used `Path.is_file()` for a Unix
   domain socket, so `model_only` starts always failed before spawn; fixed to
   `lstat` + `S_ISSOCK`, with the test fixture creating a real AF_UNIX socket.
2. Container RPC socket path was too long for AF_UNIX and `/tmp` was not a
   daemon-visible shared path; smoke used a short host-shared dir. Operators must
   set `runtime.runner_container_shared_dir` to a short path visible to Docker.
3. Proxy client disconnects produced noisy `ConnectionResetError` tracebacks;
   the proxy HTTP server now treats broken pipes/resets as normal disconnects.

## Residuals (documented, not regressions)

- `/proc/<Pi>/environ` remains readable because Linux resets dumpable on
  `execve`; after this fix it contains only paths/non-secret routing and a stale
  closed-FD number, not tokens/keys.
- `<state>/bin/mc-env` (0600) is same-uid-readable by a model-directed shell if it
  discovers the path; the MC token is required for `mc` and is own-game scope.
- Loopback proxy/bridge remain network-reachable but require credentials that
  bash cannot obtain; `curl` without them returns 401 and spends no budget.
- This smoke uses synthetic game runtime, not a real Minecraft world; a
  dedicated full-stack smoke is still required before a 54h paid run.

## Verification

- Full Python suite after the fix: **745 passed, 3 skipped, 734 subtests**
  (`PYTHONPATH=src:.venv/site-packages .test-venv/bin/python -m pytest -q`).
- Pi/Observer targeted: **189 passed, 119 subtests**.
- Both smoke commands used a real DeepSeek key (injected only via the process
  environment, never written to disk) and real Pi CLI 1.4.0; no Docker experiment
  stack was started; no `final11` containers were touched.
