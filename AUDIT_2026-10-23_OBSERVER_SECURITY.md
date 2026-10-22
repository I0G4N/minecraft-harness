# Pi Observer / RPC bridge security audit — 2026-10-23

Independent, read-only audit of the new Pi Observer path and its security boundary.
Scope: `ServiceHub` observer RPC, `pi_observer_bridge`, `pi_observer_runner`,
`RunnerManager`/`Supervisor` observer routing/mounts/env, and Linux `/proc`
credential hardening. No source was modified; no Docker/experiment/final11 was
started; all probes were local/loopback only.

## Verdict

The observer identity, ACL, redaction and bridge validation model is **strong** and
passes every concrete attack probe I ran (identity spoofing, forged evidence,
argument smuggling, unauthenticated bridge calls, container mount/env audit,
concurrent access). The full suite is green (`677 passed, 3 skipped, 706
subtests`, 115.7 s) and `tests/test_pi_observer.py` passes (`18 passed, 22
subtests`, 9.5 s).

Two **MEDIUM/LOW evidence-integrity** gaps and one **HIGH** residual hardening gap
remain (details below). The HIGH gap does not allow the *observer* to escalate
privileges (it has no shell/network tool), but it exposes the model API key and
runner RPC token to model-directed process reads in container mode, and is a
direct budget-integrity risk for the player Pi runtime that shares the same
hardening.

## Commands / evidence run

```
# full Python suite (host .test-venv 3.12, no Docker)
PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages .test-venv/bin/python -m pytest -q
# -> 677 passed, 3 skipped, 706 subtests passed in 115.66s

# observer unit/integration suite
... -m pytest -q tests/test_pi_observer.py
# -> 18 passed, 22 subtests passed in 9.48s
```

Additional ad-hoc probes (Python, host, loopback only, no files written):

1. `ServiceHub` observer identity/ACL/report matrix (real `Recorder`,
   `ArtifactVault`, `ServiceHub`): identity denial, operator-only redaction,
   oversized-event bounding, artifact grants, digest/path validation, report
   validation, actor forcing, extra-key injection, 10 concurrent calls.
2. `PiObserverBridge` HTTP probes with a fake RPC client: auth, token, method
   allowlist, argument bounds, body size, JSON errors, truncation, error mapping.
3. `RunnerManager` construction audit (no Docker start): observer module/spec,
   container mounts (bind sources), env whitelist, `--init`/`--user`/read-only/
   cap-drop, PID fencing, Pi runtime hash pinning.
4. `/proc` probes: same-uid environ readability before/after
   `PR_SET_DUMPABLE=0`; real Pi CLI `read` tool reading an absolute
   `/proc/<pid>/environ` path (loopback fake OpenAI SSE upstream).

## PASS — verified security properties

| Area | Result | Evidence |
| --- | --- | --- |
| Observer RPC identity binding | PASS | `ServiceHub._require_observer` requires bound `role=="observer"` **and** `agent_id=="observer"` (`runners.py:516-519`). Probes: player1/player, player1/observer (role spoof), observer/player (agent spoof), notobserver/observer all `PermissionError`; real observer OK. RPC server binds identity from token (`runner_rpc.py` hello) and `_dispatch` additionally checks state role. |
| `observer.records_read` ACL/redaction | PASS | Operator-only records (`effective_environment_verified`, `payload.operator_only`) are replaced with `{operator_only:true, redacted:true}` and grant no artifacts (`runners.py:546-559`); the observer's own events are skipped without returning/granting (`548-550`); oversized payloads are offloaded to a bounded artifact wrapper (`560-567`); returned page is byte-bounded and reports `latest_event_seq`/`next_cursor`/`truncated` (`568-577`). Probe: seed/operator payload absent, own event hidden, oversized event bounded, operator artifact read denied, never-granted artifact denied. |
| `observer.evidence_read` integrity | PASS | SHA256 regex, offset/limit bounds, `ArtifactVault.read` enforces per-agent grant, `O_NOFOLLOW`, regular-file check and re-hash integrity check (`runners.py:243-263,597-609`). Probe: bad digest/limit/offset `ValueError`; unowned digest `PermissionError`; delivered artifact readable. |
| `observer.report` validation | PASS (mostly; see F4) | Actor forced to `observer`, advisory flag added, event is ledger-only and never sets task success (`runners.py:611-615,686-693`); `through_cursor` bounded to latest, findings/verified counts and claim lengths bounded, statuses enumerated, artifact references must be owned (`runners.py:619-683`). Probe: all invalid shapes rejected; stored reports actor=observer, advisory=true; top-level extra keys dropped. |
| Bridge authentication / loopback / token | PASS | Random 43-char `secrets.token_urlsafe` token, constant-time `hmac.compare_digest`, `127.0.0.1` OS-assigned port, only `POST /tool` (`pi_observer_bridge.py:89,110-160`). Probe: no/wrong token → 401; other path → 404; token not in URL/logs. |
| Bridge tool allowlist / argument validation | PASS | Exactly `records_read/evidence_read/observer_report/infra_issue` (`45-47,56-61`); unknown tool → 404 (e.g. `bash`); per-tool required/allowed keys, int/string/SHA bounds, report finding schema, `maxItems` and text-length caps (`189-394`). Probe: `bash` 404, extra args 400, invalid hash 400, report extra keys 400, bad JSON 400, 2 MiB body 413, output truncated at model-visible limit. |
| Observer Pi tool surface | PASS | `OBSERVER_TOOLS = read, write, records_read, evidence_read, observer_report, infra_issue`; no bash, no game/MC client, no memory/web tools (`pi_observer_runner.py:66-68`); explicit `--no-approve/--no-context-files/--no-extensions --extension observer-tools.js/--no-skills/--no-prompt-templates/--no-mcp` (`321-342`). Extension `pi_runtime/extensions/observer-tools.js` registers only those four bridge tools. Probe: container command has no `/state/bin`, no bash; extension pinned. |
| Observer credentials / mounts / env | PASS | Observer spec forces `role=observer`, `agent_id=observer`, rejects `cli_environment`/`broker_socket` (`pi_observer_runner.py:171-232`); manager strips `cli_environment`/`broker_socket`/`team_memory_root` and sets `shell_enabled=false` (`runners.py:1509-1521`). Container mounts: source snapshot ro, spec ro, own state rw, own workspace rw, reference ro, pi_runtime ro, site-packages ro, single RPC socket file; **no** team-memory, broker, docker.sock, ledger/memory DB or other agent state (`runners.py:1728-1795`; probe: exactly those 8 binds). Container env: no `MC_*`, no BOT/admin tokens, no bridge token; only own HARNESS token/model key (proxy needs it) and PI settings (`runners.py:1690-1730`; probe). `--read-only`, `--cap-drop=ALL`, no-new-privileges, pids/memory/cpus, `--user <run_dir uid:gid>` (`runners.py:1744-1786`). |
| Runtime code pinning | PASS | Pi CLI + both extensions + package(-lock) hashed into the manifest and re-verified on restart, fail-closed on change (`runners.py:1114-1140,1250-1260,1270-1280`); observer extension included (probe). |
| `/proc` hardening of the runner process | PASS (see F1 for container gap) | `PR_SET_DUMPABLE=0` helper (`pi_security.py:16-45`), called at startup of `pi_observer_runner.py:1027`, `pi_runner.py:1577`, `runner.py:1047` before any child spawn; failure is fatal (exit 2). Probe: same-uid child can read an unhardened process env, but gets `PermissionError` for a hardened runner; real Pi CLI `read` tool can read absolute `/proc/.../environ` (so PID-1 attack below is reachable). |
| Supervisor observer route / policy | PASS | Spec includes `role/agent_id/cli_environment{}/pi_model/observer config` (`supervisor.py:950-980`); `observer_failure_policy` defaults `degrade` and a failed/unexpected observer is recorded as `observer_degraded` without ending the run (only `fail_run`+required fails) (`supervisor.py:167-215`); Pi observer transport recorded in `observer_started`. PID fencing accepts both Pi modules (`runners.py:64`). |
| Concurrency / durability | PASS | 10 concurrent `records_read`/`report` calls: no errors; recorder/vault locks serialize writes; report actor forced; events durable. |

## FINDINGS

### F1 — HIGH (residual hardening gap): `/proc` hardening does not cover container PID 1, so container env secrets remain model-readable

- The runner process hardens itself with `PR_SET_DUMPABLE=0`, but runner
  containers are started with `--init` (`runners.py:1744`) and secrets are passed
  via `docker run -e` (`_container_environment`, `runners.py:1690-1730`,
  including `HARNESS_RUNNER_TOKEN` and, in real runs, the upstream model API key).
  Docker's init (`tini`, PID 1) inherits that environment and is **not** hardened;
  with `--user` it runs as the same uid as the Pi child.
- Probes: (a) an unhardened same-uid process env is readable
  (`/proc/<pid>/environ` returns the secret); (b) after `PR_SET_DUMPABLE=0` the
  same read raises `PermissionError` (hardening works); (c) the real Pi CLI
  `read` tool successfully reads an absolute `/proc/<pid>/environ` path.
  Therefore a model-directed `read` (`/proc/1/environ`) in a runner container
  obtains the model API key and the runner RPC token. The player Pi runtime also
  has `bash` and general egress, so it can exfiltrate the key and make unmetered
  upstream calls (budget bypass). The Observer has no bash/network tool, so its
  direct impact is secret disclosure into workspace/report/ledger, not an
  outbound channel — but the API key is a run-wide shared credential.
- Docs currently claim this is fixed (`README.md:226-228`,
  `PI_METHOD_ALIGNMENT.md:296-300`); the claim is incomplete for container mode.
  `RUNNER_ARCH_CONTRACT.md:329-333` still lists the old residual (docs
  inconsistency, LOW).
- Fix: keep secrets out of container env (pass via stdin pipe or a 0600 file read
  by the hardened runner, then `os.environ.pop`), and/or drop `--init` so the
  hardened runner is PID 1 (then reap Pi children explicitly), and/or run the
  runner as a different uid than the Pi child with hidepid. Add a gated container
  test that asserts `read /proc/1/environ` from inside a runner container fails.

### F2 — MEDIUM: `records_read` grants artifacts referenced by *untrusted* player payloads

- `_observer_records_page` walks every returned event payload and grants any
  64-hex `hash`/`sha256` to the observer (`runners.py:568-595`). Payloads are
  model-controlled by player runners via `recorder.append`. Probe: an artifact
  granted only to `player2` (`artifacts.grant("player2", h)`) was planted in a
  player2 event; the observer then successfully read it
  (`player_planted_foreign_granted: OK`) even though it was never legitimately
  delivered to the observer.
- Impact: a player can widen the observer's artifact ACL to any digest it knows
  (e.g. a hash observed in chat/team memory), so the observer can read another
  agent's artifact content that was never part of the observer's evidence page;
  this weakens the “Observer reads only what records delivered to it” boundary.
  (Observer is a trusted witness; no privilege escalation for the player itself.)
- Fix: grant only artifacts owned by the event's actor, or only artifacts already
  granted to the observer, or move grants to an explicit supervisor-side
  `artifact.grant` triggered by validated event/actor relationships; add a
  cross-agent planted-hash test.

### F3 — MEDIUM: `observer.report` evidence refs are not tied to viewed records

- `_validate_observer_evidence_refs` accepts any integer `0 <= seq <= latest`
  without checking that the observer ever read that event
  (`runners.py:657-683`). Probe: a report citing the current `latest` seq was
  accepted (`unseen_event_ref_accepted: OK`), even though the observer never read
  it. The Python `ObserverAgent` tracked `seen_events`; the RPC path has no
  equivalent seen-cursor.
- Impact: a buggy/compromised observer runner (or bridge bypass) can publish
  authoritative-looking reports citing arbitrary ledger positions; paper evidence
  integrity for the observer report relies on the observer being honest rather
  than on the harness verifying “viewed”.
- Fix: track the observer's consumed cursor/seen set in `ServiceHub` (e.g. update
  on `records_read`; require report refs ≤ last consumed cursor / in seen set;
  require `through_cursor` ≤ last consumed cursor).

### F4 — LOW: direct-RPC report payload can smuggle extra keys (bridge strips, ServiceHub does not)

- Bridge `_findings`/`_verified_events` rebuild clean dicts and `_require_keys`
  rejects extras (`pi_observer_bridge.py:233-330`) — good. But
  `ServiceHub._validated_observer_report` passes the raw `findings` /
  `verified_events` dicts through and only appends `advisory`/`through_cursor`
  (`runners.py:619-655,686-693`). Probe: a direct `observer.report` RPC with
  `findings:[{...,"success":true}]` and `verified_events:[{...,"reward":1.0}]`
  was accepted and the extra fields were persisted in the ledger event
  (`injected_extra_finding_keys_stored: OK, [True], [1.0]`). Top-level extra keys
  are dropped (probe false/false).
- Impact: LOW (bridge is the normal path and strips them; only a compromised/buggy
  observer runner calling the RPC directly can inject). Advisory-report consumers
  scanning unknown keys could misread `success`/`reward`.
- Fix: rebuild findings/verified-event dicts with exactly
  `{status, claim, evidence_refs}` / `{claim, evidence_refs}` in ServiceHub too.

### F5 — LOW: bridge error mapping can expose internal messages/paths to the model

- `_safe_error_message` passes messages verbatim (≤1000 chars) for whitelisted
  types `ValueError/PermissionError/FileNotFoundError/RunnerControlError`
  (`pi_observer_bridge.py:62-65,388-394`). Probe with
  `RpcError("RunnerControlError", "internal path /srv/run/ledger/events.sqlite3
  unreadable")` returned that message to the tool caller. In practice the
  observer RPC paths mostly raise already-user-facing messages, but
  `FileNotFoundError` from `ArtifactVault.read`'s `os.open` (granted digest whose
  file is missing) would include the artifact path.
- Fix: whitelist only known user-facing validation errors, or strip path-like
  content / pass a fixed message for infra errors.

### F6 — LOW (functional, not a security escalation): `infra_issue` requests dedupe to a single key

- Bridge validates `infra_issue` to `{kind, reason, evidence_refs}`
  (`pi_observer_bridge.py:296-310`); it never sets `issue_id`.
  `Supervisor._observer_infra_issue` dedupes on
  `hash(str(issue.get('issue_id','')))` (`supervisor.py:141-145`), so every
  observer infra request after the first returns `duplicate` for the rest of the
  run. Also the bridge kind set is a subset of the Python observer’s
  (`broken_tool`/`quota_ceiling` are not accepted). No security impact; observer
  repair requests silently stop working after the first one.
- Fix: derive a stable issue_id from kind+reason+refs in the bridge (or accept an
  issue_id) and align the kind allowlist with `ObserverAgent.ISSUE_KINDS`.

### Residual / unverified

- **Container-gated behaviour not run** (Docker start prohibited for this task):
  F1’s `/proc/1/environ` read, `--user` ownership of state/spec/workspace
  mounts, bridge loopback inside the runner container, and Node presence in
  `minecraft-harness-workspace:local` are asserted from code/args and host
  probes, not a live container run. Run these in the dedicated Docker window.
- **Process mode is not a security boundary** (documented): the Supervisor and
  other host processes are not hardened, so model-directed reads (`read` tool or
  player bash) can read same-uid host process env; paper runs must use container
  mode. `pi_runtime` is ro in containers, but writable on the host in process
  mode (frozen-source hash would fail closed on restart, so DoS not code exec).
- Observer `write` tool (workspace notes) is intentional; it can only write its
  own workspace/state in container mode (mounts as audited). Observer bridge
  token is visible to the Pi child env but only authorizes the four observer
  tools (no escalation).
- No rate limit on observer tool calls per review (bounded indirectly by model
  turns/budget); low risk.
- Test coverage gap: `tests/test_pi_observer.py::test_runner_environment_is_not_readable_via_proc`
  (lines 773-788) only tests the runner process env in process mode; it does not
  exercise container PID 1, which is why the suite is green despite F1.

## Recommendation

Do not treat the observer security boundary as fully closed until F1 is
mitigated (HIGH: secrets out of container env / hardened PID 1) and the
container-gated test window is run. F2/F3 should be fixed before the next paper
run if observer evidence is used for paper claims; F4/F5/F6 are low-risk
defense-in-depth/functional items.
