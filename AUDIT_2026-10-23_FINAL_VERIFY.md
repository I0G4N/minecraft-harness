# AUDIT 2026-10-23 — FINAL VERIFICATION OF P1 FIXES

Read-only final verification after the P1 fixes (Task-27, revision 2). Repo: `/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness` (H). Wrapper: `/ssd_data/caizihao/minecraft/minecraft-harness/run-paper.sh`.
Auditor: `longrun-auditor`. Snapshot time: 2026-10-23 ~02:53 +08.
Constraints honoured: no source edits, no Docker/experiment/final11 start-stop-exec, no git, each test <10 min, no external network. `run-paper.sh` was intentionally **not executed**: its current `preflight` runs a disposable `docker run` image probe even for `--prepare` (see P2-3). Worktree was edited concurrently at 02:44–02:46 (proxy/supervisor/docs); all final tests below were run after the last edit.

## 1. Snapshot (SHA256 at ~02:53)

| file | sha256 |
|---|---|
| `src/minecraft_harness/pi_model_proxy.py` (02:46) | `8f4925d75ee6a784d0e568c1f5115df7b6d9f5fc7a248ff0de1aec4bf76a92d5` |
| `src/minecraft_harness/runners.py` (02:29) | `12321f523d58c71d75be6faf7f1546f24fff4a913bb34c96f25b93b3b2071079` |
| `src/minecraft_harness/pi_runner.py` (02:21) | `982c0fff35fae4bf7289a4276b039b99bf49cde1332142362939cc104ec6ce33` |
| `src/minecraft_harness/pi_observer_runner.py` (02:09) | `0f26d53e636adb0c19700ec44bb3322dbc88fc7c3b4dbcf178e67a7ab0b5ad70` |
| `src/minecraft_harness/pi_observer_bridge.py` (02:10) | `8678e9cd2b5ecd716348c8dd7826c104ef5a9c9d9f08319e640f84ec35ae34ac` |
| `src/minecraft_harness/supervisor.py` (02:46) | `90bc2da1e6669ef4fbf07529c5c04ecb40e51b497f9852ab540b9a1d76af8a6d` |
| `src/minecraft_harness/pi_security.py` | `2433dcca4704a36cd43626a6f85c37e079165d3574cbfa068cf9ace926441533` |
| `configs/minecraft-paper-runner-20261022.yaml` (02:21) | `54e2d49e00dae4662b3f65f25876ea55fd8124042b6e2eaf4a1bd07c836a3ab1` |
| `run-paper.sh` (02:26) | `5539af298385e270ccc50a9cf9b03032f01e39630cc10decc93529b606126753` |

Real full-tree Pi hash: **15,014 files, sha256 `56df312bca07d13f…`, 1.082 s per computation** (probe on the real `pi_runtime` tree).

## 2. Verdict

**No P0. The four requested P1 fixes are present and verified on the current tree**; Observer F1–F6 are green; the full host suite is green. Exact results:

- Full suite: **718 passed, 3 skipped, 723 subtests in 117.82 s**.
- Broad Pi/Observer targeted set (9 files: `test_pi_model_proxy.py`, `test_pi_runner_manager.py`, `test_pi_runner.py`, `test_pi_e2e.py`, `test_pi_tools.py`, `test_servicehub_web.py`, `test_pi_retention.py`, `test_pi_observer.py`, `test_observer_rpc.py`): **162 passed, 108 subtests in 50.97 s** (matches the 162/108 docs claim).
- Documented §9.5 six-file command (`test_pi_model_proxy`, `runner_manager`, `pi_runner`, `pi_e2e`, `pi_tools`, `servicehub_web`): **95 passed, 42 subtests in 39.18 s** — docs still claim 162/108 for this command; stale (P2-2).
- Node: `npm test --prefix bot_runtime` → **51 passed, 0 failed**.
- Skipped: **3** Docker-gated tests. No Docker was started in this window; the container Pi smoke was not re-run.

Residuals: one new P1-adjacent retention-bypass residual (P1-5), plus P2 docs/wrapper/run-wide-storage residuals. No test regression.

| id | sev | finding | status |
|---|---|---|---|
| P1-1 | PASS | Full-tree Pi runtime hash covers cli-runtime/bundle/node_modules; tamper changes digest; restart fail-closed | VERIFIED |
| P1-2 | PASS | Mid-stream SSE: one HTTP response, no trailing 502/terminator, `model_error` (no false `model_response`/null artifact), exactly one settlement | VERIFIED |
| P1-3 | PASS | Restricted egress: paper Pi+container defaults `model_only`; `--network none`; host UDS forwarder/token; provider key absent from runner env; only `/v1/chat/completions`; bridge-mode regression intact | VERIFIED (container execution unverified, no Docker) |
| P1-4 | PASS* | Restart-safe ArtifactBudget (metadata charged, pressure, missing/corrupt marker), player/Observer shutdown retention/flush, paper caps 6 GB/1 GB with 40 GB total vs 51.5 GB run cap | mechanisms VERIFIED; see P1-5, P2-4 |
| P1-5 | **P1 residual** | Budget state marker is under model-writable `/state`; forging a valid marker (or deleting it) resets restart-safe usage after runner restart | OPEN (new hardening finding) |
| P2-1 | P2 | Docs stale/inconsistent: §9.5 command gives 95/42 not 162/108; README target 130/108; caps say 1 GB/8 GB while config is 6 GB; README says `--prepare` "不启动容器" but it runs a disposable image container; task note "686/~714" outdated (docs now 718) | OPEN (do not edit) |
| P2-2 | P2 | Wrapper `--prepare` now starts a disposable Docker container (image probe) and fails without Docker; docs describe it as safe/no-container; image probe not executed here (Docker forbidden) | OPEN |
| P2-3 | P2 | Run-wide retention still only per-runner caps; docs itself says run-wide budget/task-24 in progress; no 54 h storage guarantee | OPEN (documented residual) |
| P2-4 | P2 | Observer-specific artifact-budget restart test absent (shared ArtifactBudget mechanism tested; Observer code passes state_path/flush); minor test gap | OPEN (test gap) |
| P3-1 | P3 | Full-tree hash costs ~1.08 s and hashes 15,014 files on every spawn/restart (5 agents ≈5.4 s startup latency); acceptable but worth monitoring | NOTE |
| P3-2 | P3 | Forwarder UDS is mode 0600 owned by the Supervisor uid; if `runtime.runner_container_user` is overridden to a different uid, the socket is inaccessible (fail-closed at runtime, no preflight check) | NOTE |
| P3-3 | P3 | Active-session cap still stops an agent (recorded degraded) without auto-rotate/restart/operator alert; accepted residual from prior audit | NOTE (accepted) |

## 3. P1-1 full-tree Pi runtime hash — VERIFIED

- `RunnerManager._pi_runtime_hashes` (`runners.py:1358-1394`) now walks the complete `pi_runtime` tree (`rglob("*")`), hashes every regular non-symlink file into a relative-path→sha256 map, checks the CLI resolves inside the tree, and returns `{root,file_count,files,sha256}`. `_verify_source_snapshot` compares the whole map to the manifest and raises on mismatch (`runners.py:1460-…`).
- Probe (temp tree): before/after editing `node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli-runtime.js`, `file_count=3`, `chunk_hashed=True`, `digest_changed=True`. Real tree: `file_count=15014`, `sha256=56df312b…`, **1.082 s**.
- Test: `tests/test_pi_runner_manager.py:400 test_pi_runtime_hash_covers_bundle_chunks_not_just_the_cli_shim` passes; `test_pi_runtime_hashes_pin_both_extensions` passes; manifest/egress tests pass.
- Residual note P3-1: full-tree hash is recomputed on every `_ensure_frozen_source`/spawn/restart; ~1.08 s each, so 5 runner starts add ~5.4 s. No correctness issue found.

## 4. P1-2 mid-stream SSE — VERIFIED

- Proxy stream path (`pi_model_proxy.py:816-836`, `:860-925`): on `httpx.HTTPError` after headers/chunks, it records `stream_error`, sets `response_document=None`, `handler.close_connection=True`, never calls `_end_stream`, never sends a second HTTP response; the outer handler only sends 502 when `not response_started` (`:867-875`). The `finally` settles exactly once; it writes `model_response` only if `response_document is not None and stream_error is None`, otherwise appends `model_error` with `phase=upstream_stream` (`:885-925`). Missing `[DONE]` is also a `model_error` (`:832-835`).
- Tests: `test_mid_stream_failure_sends_no_second_response_or_false_response_event` (`test_pi_model_proxy.py:502`), `test_stream_without_done_terminator_is_a_model_error` (`:531`), `test_normal_stream_terminates_exactly_once` (`:548`) pass in the broad/full runs.
- Independent probe (current code): raw socket got **1** `HTTP/1.1` response, no `HTTP/1.1 502`, no trailing chunk terminator, ledger events `model_request` + `model_error` only, **1** settlement with `UNKNOWN_USAGE`, request artifact only (no `null` response artifact). Normal stream test confirms exactly one terminator and `model_response`. Forwarder mid-stream test (`test_mid_stream_upstream_failure_does_not_append_a_second_response`) passes.

## 5. P1-3 restricted egress — VERIFIED (code/tests; no Docker execution)

- Default: `resolve_runner_egress` (`runners.py:81-100`) returns `model_only` when `runner_mode=container` and `runner_runtime=pi`, else `bridge`; explicit `bridge` preserves legacy; invalid values raise. Paper runner config resolves to `model_only` (test `test_paper_runner_config_resolves_to_restricted_egress`).
- Runner container: `_container_network_for` returns `none` for restricted agents (`runners.py:1212-1213`), and `_container_args` uses it (`:2026`); the forwarder socket directory is bind-mounted read-only at `/run/mc-model` (`:2039-…`); the state dir is still rw `/state` (`:2037`); `--user` is the run-dir uid:gid; `--cap-drop=ALL`, no `--init`, PID1 hardened.
- Forwarder: host-side `PiModelForwarder` (`pi_model_proxy.py:1051-…`), UDS socket mode 0600, random per-run token (`HARNESS_MODEL_FORWARDER_TOKEN`), only `POST /v1/chat/completions` (`_handle_post`, `:1186-…`; GET 405, wrong path 404, bad/missing token 401, oversized 413), adds the provider credential from the **Supervisor** env, never forwards the forwarder token upstream, `trust_env=False`, no second response on midstream error.
- Pi proxy restricted mode: `PiModelProxy` reads `model_forwarder_socket`, requires `HARNESS_MODEL_FORWARDER_TOKEN`, sets endpoint `http://model-forwarder/v1/chat/completions`, uses `httpx.HTTPTransport(uds=...)`, sends only the forwarder token header (no provider key) — `pi_model_proxy.py:480-505, 770-800`.
- Provider key absence: restricted `_container_environment` only adds allowed base env + `HARNESS_MODEL_FORWARDER_TOKEN`, explicitly excludes model/web key env vars (`runners.py:1954-2001`); test `test_restricted_container_has_no_network_and_uses_forwarder` asserts `--network none`, forwarder UDS mount/token, and no `FAKE_API_KEY` provider env; `test_bridge_mode_keeps_model_key_environment` confirms legacy bridge regression. Pi child env forwarding set excludes `HARNESS_MODEL_FORWARDER_TOKEN` (`pi_runner.py:122-125`, `_child_environment` around `:765-805`).
- Tests pass (broad target): `test_restricted_container_has_no_network_and_uses_forwarder`, `test_bridge_mode_keeps_model_key_environment`, `test_restricted_mode_requires_socket_token_and_container`, `test_restricted_start_fails_closed_when_forwarder_is_absent`, `test_restricted_proxy_uses_forwarder_endpoint_and_token`, `test_restricted_proxy_end_to_end_through_the_forwarder`, `test_relays_sse_and_injects_provider_credential`, `test_rejects_bad_token_unknown_path_and_get`, `test_forwards_provider_error_status`, `test_mid_stream_upstream_failure_does_not_append_a_second_response`.
- Host probe: forwarder returned 200 with valid token, 401 bad token, 405 GET; upstream received `authorization: Bearer …` but **not** the forwarder token; restricted proxy endpoint was `http://model-forwarder/v1/chat/completions` with a forwarder token set.
- **Not verified**: no runner container/compose execution in this window (Docker forbidden); 3 container-gated tests skipped. The docs explicitly say the prior one-time container smoke is not evidence for this window.

## 6. P1-4 restart-safe retention / Observer retention — VERIFIED mechanisms; new P1-5 residual

Verified:
- `ArtifactBudget` is now run-wide-persistent **per agent**: `state_path` = `spec.state_root/"pi-artifact-budget.json"` for player (`pi_runner.py:966`) and Observer (`pi_observer_runner.py:476`); state is atomically written (`fsync`+`os.replace`, 0600), loaded at startup, metadata projections are charged too (`pi_model_proxy.py:95-350`), one `pi_retention_pressure` event at ≥90 %, corrupt marker → conservative full budget, missing marker → fresh.
- Flush/lifecycle: heartbeats flush/snapshot (`pi_runner.py:1101`, observer flush in finally `pi_observer_runner.py:1018`); player shutdown now `_apply_shutdown_retention` (`pi_runner.py:688-711`, called at `:1535` and `:1616`) prunes/rotates sessions and flushes budget; Observer applies retention+flush in `finally` (`pi_observer_runner.py:1012-1018`).
- Tests (broad/full): `test_usage_survives_a_simulated_restart`, `test_missing_marker_is_a_fresh_budget`, `test_corrupt_marker_is_conservative_metadata_only`, `test_pressure_event_fires_once_at_ninety_percent_and_persists`, `test_player_shutdown_prunes_sessions_and_flushes_budget`, `test_paper_runner_caps_leave_run_wide_headroom` (asserts config 6,000,000,000 artifacts, 1 GB sessions, 5 agents total 40 GB < 51.5 GB with ≥10 GiB headroom) all pass.
- Observer session retention/resume tests pass (`test_session_resumes_after_runner_restart`, `test_oversized_session_is_archived_by_the_shared_retention_policy`); Observer uses the shared ArtifactBudget implementation.
- Caps: paper config `pi_artifact_max_bytes=6000000000`, `pi_session_max_bytes=1000000000`, 4 players + Observer = 40 GB + ledger headroom vs `max_run_bytes=51,539,607,552`; test passes. (Docs still say 8 GB — P2-1.)

Residual **P1-5 (new)**: the budget state marker is inside the model-writable `/state`:
- `state_path` is `/state/pi-artifact-budget.json` in container mode (`spec.state_root="/state"`, `pi_runner.py:966`/observer `:476`); `_container_args` mounts the state dir **rw** at `/state` (`runners.py:2037`) and the Pi child runs as the same uid (`--user` derived from run_dir; `--cap-drop=ALL` does not prevent same-uid writes).
- Probe on current code: budget used 600 → write a valid forged marker `used_bytes=0` → new `ArtifactBudget` reports `used_bytes=0`, `state_status=restored`; deleting the marker yields `used_bytes=0`, `state_status=fresh`. A model with bash (player) or write tool (observer, for its own state) can therefore reset its per-agent artifact budget on the next runner restart; the global Supervisor storage guard is then the only bound.
- Recommended fix: make the marker tamper-evident (HMAC with a key that stays in the runner process env and is not forwarded to the Pi child; missing/invalid marker after first run → conservative full budget) or move budget accounting to the Supervisor/RPC authority; add an adversarial test for forged/deleted markers. This is a hardening residual, not a regression of the specified restart/corrupt-marker cases.

## 7. Observer F1–F6 and Pi Observer path — VERIFIED

- F1 container PID1 env: no `--init`, hardened Python runner PID1, `PR_SET_DUMPABLE=0`; same-uid `/proc/1/environ` denial tested (`test_pi_observer.py:909`, `test_pi_runner.py:403`).
- F2 artifact grant ownership: `_grant_observer_artifacts(actor_id, …)` grants only actor-owned/control-plane artifacts and refuses another runner's planted artifact; tests `test_records_read_grants_actor_owned_embedded_artifacts`, `test_records_read_does_not_grant_another_agents_artifact`, `test_records_read_grants_control_plane_artifacts` pass.
- F3 viewed-event evidence: `observer-seen.sqlite3` persists viewed event_seq; report `through_cursor`/evidence refs must be seen; tests `test_report_requires_viewed_event_references`, `test_viewed_event_index_survives_servicehub_restart` pass.
- F4 report key injection: report reconstructed from known fields; tests `test_report_records_advisory_event_with_clean_fields`, `test_report_strips_unknown_finding_and_reference_keys` pass.
- F5 bridge error leakage: `_safe_error_message` whitelist; test `test_bridge_errors_do_not_leak_internal_paths_or_rpc_details` passes.
- F6 infra issue id/kinds: bridge generates unique `issue_id`, validates the full paper kind set; Supervisor dedupes/probes; tests `test_infra_issue_accepts_the_full_paper_kind_set`, `test_each_infra_issue_call_gets_a_unique_issue_id`, `test_infra_issue_callback_remains_observer_only` pass.
- Observer Pi path still green: real Pi observer report/ready/finished, wake/interval, session resume, oversized-session retention, SIGTERM/stopped, role fail-closed, no game credentials/no shell/no team-memory/broker mounts (broad target + full suite).

## 8. Exact test commands/results

```bash
# full suite (host .test-venv 3.12, no Docker)
PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages .test-venv/bin/python -m pytest -q
# 718 passed, 3 skipped, 723 subtests in 117.82s

# broad Pi/Observer targeted set (9 files, matches docs 162/108)
... pytest -q tests/test_pi_model_proxy.py tests/test_pi_runner_manager.py \
  tests/test_pi_runner.py tests/test_pi_e2e.py tests/test_pi_tools.py \
  tests/test_servicehub_web.py tests/test_pi_retention.py tests/test_pi_observer.py \
  tests/test_observer_rpc.py
# 162 passed, 108 subtests in 50.97s

# documented PI_METHOD_ALIGNMENT §9.5 command (6 files)
... pytest -q tests/test_pi_model_proxy.py tests/test_pi_runner_manager.py \
  tests/test_pi_runner.py tests/test_pi_e2e.py tests/test_pi_tools.py tests/test_servicehub_web.py
# 95 passed, 42 subtests in 39.18s  (docs claim 162/108 -> command/count stale)

npm test --prefix bot_runtime          # 51 passed, 0 failed
```

Probes (all read-only, `/tmp` only): full-tree hash/tamper (15,014 files, 1.082 s, tamper changes digest); SSE raw socket (1 response, no 502, no terminator, `model_error`, 1 settlement UNKNOWN); budget marker forge/delete (600→0 restored/fresh); forwarder UDS (200/401/405; provider auth upstream; forwarder token not forwarded); no Docker/experiment/final11 operations.

## 9. Residuals / things to fix before the paid run

1. **P1-5** budget marker model-writable (`/state/pi-artifact-budget.json`): HMAC/supervisor authority; missing-after-start should be conservative, not fresh; adversarial test.
2. **P2-1 docs** (do not edit in this task): §9.5 6-file command is stale (95/42 vs claimed 162/108); README says 130/108; retention caps sentence says 1 GB/8 GB while config is 6 GB; README `--prepare` says "不启动容器" but the image probe runs `docker run`; task note "docs 686 / suite ~714" is outdated — docs now say 718 and the measured suite is 718.
3. **P2-2 wrapper**: `--prepare` runs the disposable image probe through `preflight` (README line 119 contradiction); without Docker it fails preflight for `--prepare` too, although docs describe only `--start` refusal. The probe was not executed here; dedicated Docker window required.
4. **P2-3 run-wide retention**: docs admits task-24 in progress; per-agent caps + shutdown retention are implemented, but no run-wide budget guarantee; treat 54 h storage as monitored, not guaranteed. The P1-5 marker issue makes this stricter.
5. **P2-4 test gap**: no Observer-specific artifact-budget restart test; shared ArtifactBudget tests cover the mechanism and Observer passes state_path/flush.
6. **P3-1** full-tree hash ~1.08 s/15k files per spawn; **P3-2** forwarder UDS uid assumption under `runner_container_user` override; **P3-3** active-session stop no auto-rotate/alert (accepted).

## 10. Limitations / state

- No Docker: container Pi/compose smoke, the wrapper image probe, real `--network none` execution, forwarder-in-container, and 3 Docker-gated tests remain unverified this window. Tests are host-level + unit/mock UDS; 3 skipped.
- No 54 h/30 k-call soak; full suite 118 s; Pi hash/tamper and retention probes are unit/loopback only.
- `run-paper.sh` was not run: its preflight now includes a disposable Docker image probe; running it would violate the no-Docker constraint (and final11 stack is still up anyway).
- final11 untouched: status `reasoning_paused_world_running`, 5,770 events, 14 stack containers up, 65 workspace containers; events grow only from its own sampling. No source edits, no git, no experiment/Docker start-stop-exec by this audit (only `/tmp` probes and this report).
