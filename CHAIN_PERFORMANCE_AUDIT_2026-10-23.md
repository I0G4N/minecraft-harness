# Chain / link performance audit — Pi runner, model proxy, RPC, ledger, dashboard

**Auditor:** perf-auditor (delegated) · **Date:** 2026-10-23 00:08–00:35 +08:00
**Task:** task-15 (link/path performance audit, rev 2). Read-only: no source edits, no git commits,
no Docker, no experiment/final11, no network beyond loopback. Each benchmark ran well under 10 minutes.
**Repo:** `/ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness` (enclosing git HEAD `83a56f63`;
the harness tree itself is untracked, so the audit revision is pinned by the file SHA-256 list in §9).

---

## 0. TL;DR

- The Pi path is **fast enough at paper scale**: per model attempt the proxy adds **8.4 ms TTFB** (real
  RPC + ledger, integrated), RPC bookkeeping costs 0.1–1 ms per small call, source-snapshot hashing is
  **4.5 ms for 5.9 MB / 275 files**, Pi CLI cold start is **~0.33 s** after a 51 ms Python `pi_runner`
  startup, and the model server round trip dominates everything (seconds).
- The real scaling risks are **disk writes on the Pi path**, not CPU/latency:
  1. **request artifacts grow with the context on every attempt** (full Pi messages re-recorded each
     call: 9.4–9.9 KB at 64 k window start, 61.6 KB after one 100 KB tool output; 2 MiB cap);
  2. **Pi session JSONL grows per turn** (4.6 KB/turn small turns, 114 KB for a 100 KB tool output) and
     is never pruned; at 24 k turns ≈ **0.1–2.7 GiB per agent**;
  3. the proxy records **2 artifacts + 4 ledger events + 8 RPC calls per model attempt** (≈ 48 k artifacts
     and ≈ 96 k events for 24 k calls) — measured integrated overhead 8.4 ms/call, ≈ 3.4 CPU-minutes/24 k
     calls, but ≈ 3–6 GiB of request artifacts at realistic context sizes (see §6 projection).
- Largest dashboard hot spots (sample DBs, read-only): cold `overview` **155 ms @165k events / 336 ms
  @250k**, unfiltered free-text search **423 ms** when there are no matches, cold `figures`
  **447–883 ms** with 402 KB–1.27 MB payloads, cold `programs`/`program_detail` 35–116 ms, `jobs` 32–97 ms.
- All denial/error paths are bounded and correct: budget denial **429 in ~10 ms with no upstream call**,
  oversize proxy body (16 MiB+1) **413 in ~14 ms with zero RPCs**, RPC timeout raises
  `RpcError(TimeoutError, retryable=True)` at the configured deadline and cancels the handler, socket
  close is clean (`FileNotFoundError` in 0.2 ms, socket removed).

---

## 1. Environment and method

| Item | Value |
|---|---|
| Host | 256-core shared host, load average ~40 during the audit (absolute latency conservative) |
| Python | `.test-venv/bin/python` = CPython **3.12.14** (host test env; production runs 3.11 — no perf-relevant difference in these paths) |
| Node / Pi | node **v22.23.2**; Pi 1.4.0 at `pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js` |
| SQLite | 3.53.1 |
| Filesystems | ledger/artifact benchmarks on `/dev/sda` (repo disk, production-like fsync); Pi/RPC fixtures use `/tmp` temp dirs where noted |
| Loopback only | all sockets are unix sockets or `127.0.0.1`; fake upstream/control plane; no external network |
| Not measured | live end-to-end run, real model latency/throughput, real Minecraft, dashboard under many concurrent clients |

Every number below comes from the scripts in `runs/.chainperf/` (gitignored) and the raw JSON in
`runs/.chainperf/results/`. Commands are listed in §9. Benchmark harness reuses the repo's own test
fixtures (`tests/test_pi_runner.py`: `FakeUpstream`, `sse_response`, `tool_call_delta`) so the fake
OpenAI SSE format and Pi event shapes match the tested path.

---

## 2. Pi model proxy overhead (`pi_model_proxy.py`)

Fake upstream streams the same OpenAI SSE payload; “direct” = client → upstream, “proxied” = client →
`PiModelProxy` → upstream. 12 alternating samples per case; same bytes relayed (`same_stream_bytes=true`).

| Case | direct p50 / TTFB | proxied p50 / TTFB | overhead total / TTFB |
|---|---|---|---|
| SSE 1 chunk (2.4 KB) | 3.99 / 3.87 ms | 9.65 / 9.48 ms | **+5.66 / +5.61 ms** |
| SSE 20 chunks (2.5 KB) | 6.16 / 6.00 ms | 11.89 / 11.66 ms | +5.73 / +5.67 ms |
| SSE 200 chunks (2.5 KB) | 7.98 / 7.78 ms | 14.06 / 13.80 ms | +6.08 / +6.03 ms |
| 56.6 KB prompt, 20 chunks | 9.15 ms | 15.30 ms | +6.15 ms |
| **Integrated: real `RpcServer`+`ServiceHub`+`Recorder` on disk, 8.4 KB prompt** | 4.35 ms | 12.87 ms | **+8.51 / +8.39 ms** |

- The overhead is **pre-upstream metering**: 4 budget RPCs (`wait_admission`, `validate_request`,
  `reserve`, `settle`), 2 durable events (`model_request`, `model_response` via `recorder.append`) and
  2 artifacts (`recorder.put_artifact`) = **8 RPC calls per attempt** (`budget.reserve/settle 1+1,
  wait_admission 1, validate 1, recorder.append 2, recorder.put_artifact 2`). TTFB overhead equals full
  overhead because all of it happens before the provider call (`pi_model_proxy.py:_serve_chat`).
- SSE chunk handling is cheap: 1→200 chunks adds only ~0.35 ms (~2 µs/chunk) of proxy work; Pi token
  streams are therefore not a proxy bottleneck.
- Integrated run (10 attempts, real ledger): 40 ledger rows (10 admitted, 10 request, 9 response,
  9 usage — the last settle is still in flight when the client returns), 2 artifacts / 11.5 KB because
  **artifacts are content-addressed and identical requests/response dedupe**. Growing Pi conversations
  never repeat, so dedupe does not help there.
- **Usage parsing** works end-to-end: upstream `prompt_tokens 100 / completion_tokens 1` appeared in the
  settlement and in the `model_response` event payload. Missing usage becomes
  `UNKNOWN_USAGE = {None,None,None}` and is settled conservatively (reservation charged).
- **429 / budget-denial path** (stub RPC): `admission.admitted=false` → **HTTP 429 in 10.9 ms, no upstream
  call**; `budget.validate_request` raising `BudgetExhausted` → **429**, no upstream; `budget.reserve`
  raising `BudgetExhausted` → **429 in 10.5 ms**, no upstream. No retry loop inside the proxy.
- **Max body / size behavior**: body `16 MiB + 7 B` → **413 in 14.4 ms**, zero RPC calls, no upstream
  (`MAX_REQUEST_BODY_BYTES = 16 MiB`). Invalid JSON → 400. Request artifacts are bounded to
  `MAX_REQUEST_ARTIFACT_BYTES = 2 MiB`; response artifacts to 2 MiB.
- **Non-stream JSON response >2 MiB truncates what Pi receives**: a 3,145,728-byte JSON response was
  relayed to the client as **2,097,152 bytes** (silent truncation) while the response artifact was
  524,317 bytes. Pi uses `text/event-stream` today so this is not on the active path, but it is a
  robustness bug if non-stream is ever used; recommend returning an explicit error or relaying fully
  with only the *capture* bounded.
- **Concurrency**: 4 clients × 10 stream requests → p50 **23.44 ms** / p95 37.9 ms, 159.8 rps,
  all 200. (Sequential p50 was 11.9 ms → GIL/HTTP-thread contention ~2× under 4-way load.)

---

## 3. Runner RPC latency / throughput (`runner_rpc.py` + `ServiceHub`)

Real `RpcServer` on a temp unix socket, real `ServiceHub` backed by `Recorder` (disk, WAL+FULL),
`Budget`, `MemoryStore` (FTS5), `ArtifactVault`. Client = real `RpcClient`. 25 calls per row unless noted.

| RPC method | payload | p50 | p95 | max |
|---|---|---|---|---|
| `bench.ping` (connect+hello+frame baseline) | – | **0.110 ms** | 0.267 | 0.560 |
| `recorder.append` | 1 KB | 0.235 ms | 0.647 | 1.81 |
| `recorder.append` | 64 KB | 0.734 ms | 0.815 | 0.845 |
| `recorder.append` | 1 MiB | **10.23 ms** | 12.40 | 12.95 |
| `recorder.put_artifact` | 1 KB | 0.157 ms | 0.269 | 0.50 |
| `recorder.put_artifact` | 64 KB | 0.357 ms | 0.540 | 0.71 |
| `recorder.put_artifact` | 1 MiB | **6.17 ms** | 6.35 | 6.62 |
| `artifact.read` (32 KB page, hashes full file) | 1 KB / 64 KB / 1 MiB | 0.33 / 0.42 / 1.03 ms | ≤1.09 | 1.20 |
| `memory.write` (CAS new note) | 1 KB / 64 KB / 900 KB | 0.64 / 1.72 / **16.26 ms** | ≤19.1 | 19.1 |
| `memory.read` (latest) | 1 KB / 64 KB / 900 KB | 0.40 / 0.55 / 0.76 ms | ≤1.24 | 1.24 |
| `memory.search` (200 team notes, FTS5) | query | **1.04 ms** | 1.24 | 1.24 |
| `memory.list` (private) | – | 0.60 ms | 0.66 | 0.66 |
| `budget.reserve` + `budget.settle` (pair) | usage dict | **0.58 ms** | 0.73 | 0.89 |
| `budget.validate_request` | 256 KiB | 0.181 ms | 0.223 | 0.228 |
| `budget.wait_admission` | – | 0.174 ms | 0.186 | 0.201 |

Throughput / concurrency / lifecycle:

| Test | Result |
|---|---|
| 200 sequential `recorder.append` (1 KB) | **2,480 calls/s** (0.40 ms wall/call incl. RPC+commit) |
| 4 concurrent clients × 25 appends | **2,236 calls/s**, p50 1.58 ms, p95 2.76 ms, 0 errors |
| 8 concurrent clients × 25 appends | **2,099 calls/s**, p50 3.72 ms, p95 5.05 ms, 0 errors |
| async `client.call` gather(20) appends | 1,538 calls/s (fresh connection per call) |
| timeout: `slow` 1 s handler, client timeout 0.2 s | `RpcError(TimeoutError, retryable=True)` after **200.6 ms**; server handler cancelled (2 cancellations observed) |
| timeout 1.5 s ≥ handler latency | returns OK in 1,002 ms |
| call after server close | `RpcError(FileNotFoundError)` in **0.19 ms**, socket unlinked |

Observations:

1. **Transport baseline is 0.11 ms**, so small control-plane calls are cheap; each call opens a fresh
   unix-socket connection + hello frame (no pooling). At ~8 RPC calls/model attempt that is ~0.9 ms of
   pure connection overhead per attempt.
2. **Large frames are JSON-heavy**: a 1 MiB append costs 10.2 ms vs 0.73 ms for 64 KB (14× for 16×
   payload) because the payload is encoded/decoded twice (RPC frame + SQLite JSON text). Pi tool-result
   artifacts are capped at 1 MiB and model request artifacts at 2 MiB, so the worst proxy artifact can
   cost ≈ 6–13 ms on the control plane. Keep large data in artifacts (refs in events), which the current
   design already does for tool results.
3. `budget.reserve+settle` at 0.58 ms pair is fine; it includes two durable `state_event` commits
   (`model_admitted`, `model_usage`).

---

## 4. Ledger / artifact / source snapshot / context summary

### 4.1 Ledger write path (disk, production `Recorder`, WAL + `synchronous=FULL`)

| Test | Result |
|---|---|
| 2,000 × `append(1 KB)` single-commit | p50 **0.098 ms**, p95 0.895 ms, mean 0.242 ms → **4,133 events/s**; 2,000 events in 0.48 s |
| 2,000 × 1 KB group-commit (100/tx) | **42,536 events/s** (0.024 ms/event) → **10.3× faster** than per-event commits |
| 200 × `append(64 KB)` single-commit | p50 0.397 ms, mean 0.444 ms → 2,254 events/s |
| WAL growth | samples 0.14 → **4.14 MB**, stays at 4.14 MB (autocheckpoint at 1000 pages) |
| `PRAGMA wal_checkpoint(TRUNCATE)` | 3.49 ms, WAL → 0 |

At paper scale (240 k events) single-commit append costs ≈ **58 s** of the ~54 h run (≈0.03 %), so the
per-event FULL commit is not a wall-clock bottleneck; it is only a throughput ceiling for bursts
(4.1 k/s vs 42.5 k/s with grouping).

### 4.2 Artifacts

| Operation | 1 KB | 64 KB | 1 MiB | 8 MiB |
|---|---|---|---|---|
| raw `sha256` | 0.001 ms | 0.027 ms | 0.424 ms | 3.41 ms (≈2.35 GB/s) |
| `Recorder.put_artifact` (hash + temp write + fsync + rename + dir fsync) | 0.028 ms | 0.081 ms | 1.046 ms | **7.93 ms** |
| `ArtifactVault.read` (hash whole file + 32 KB page) | 0.109 ms | 0.338 ms | 0.788 ms | 4.44 ms |
| dashboard `artifact_page` (hash whole file + 32 KB page) | – | 0.269 ms | 0.685 ms | 4.04 ms |

Artifact writes are dominated by fsync for large payloads; hashing is 0.4 ms/MiB. A 200 KB Pi tool-result
artifact costs ≈ 0.3–0.4 ms locally (plus RPC framing if written through ServiceHub: 0.36 ms/64 KB scale
→ ≈0.5–0.8 ms).

### 4.3 Source snapshot hash (`runners._source_tree_hashes` / `_source_tree_sha256`)

| Metric | Value |
|---|---|
| Tree | `src/` — **275 files, 5,900,416 bytes** |
| Per-file SHA-256 + walk | **4.51 ms p50** (4.56 mean, 4.79 max) → **1.26 GB/s**, 0.0166 ms/file |
| Canonical digest collapse | 0.028 ms p50 |
| Deterministic digest | `a32044f1…b68dd9c9` (stable across 3 runs) |

Snapshot hashing is negligible (<10 ms once per run and on restart). The copy+fsync of the same tree at
runner start will dominate (~275 file creates/fsyncs; estimated tens of ms, not measured separately).

### 4.4 Context compaction summary (in-process `ContextEngine`)

| Metric | Value |
|---|---|
| `build_summary` with realistic handoff (12 decisions, 12 verified facts, 20 read/80 modified files, pending execs/jobs) | **0.024 ms p50** |
| Summary JSON | **4,753 bytes** (13 fields; largest: files_modified 1,728 B, verified_facts 662 B, decisions 602 B) |
| `context_compacted` event payload (summary + archive name) | **4,852 bytes** |
| `record_compaction` (append to journal) | 0.067 ms; journal grows **≈4.9 KB/compaction** |

In-process summaries are small and cheap. Pi-side compaction is different: `pi_runner.py` maps each Pi
`compaction_end` result to an artifact bounded at `MAX_COMPACTION_ARTIFACT_BYTES = 512 KiB`; the short
e2e runs did not trigger Pi compaction, so the 512 KiB worst case is a declared bound, not a measurement.

---

## 5. Pi runner end-to-end path (real Pi CLI, fake upstream + fake control plane)

Setup: real `node cli.js --mode rpc` via `python -m minecraft_harness.pi_runner`; real `RpcServer` fake
hub recording every RPC; fake SSE model; temp state/workspace; `max_turns` drives continuations.
Each scenario ends with the **test driver killing the still-exiting child** after `runner.finished`
(`exit -9` in the JSON is driver-induced, not a runner failure); stderr was empty in all runs.

### 5.1 Startup

| Phase | p50 |
|---|---|
| `pi_runner` process → `runner.ready` RPC (spec load, proxy start, bridge start, models.json/system prompt) | **51 ms** (54 ms p95) |
| `node cli.js --version` cold | 188.9 ms |
| `runner.ready` → first `model_request` (Pi CLI boot: node start, provider config, session create, first prompt) | **0.325–0.331 s** |
| Total cold start to first model call | **≈ 0.38 s** |

### 5.2 Per-turn behaviour (3 turns, small bash output)

| Metric | Value |
|---|---|
| Turns / model attempts | 3 turns, **4 attempts** (1.33 attempts/turn: first turn tool-loop + continuation turns) |
| Ledger events (Pi-side) | **17 events / 3 turns = 5.67/turn**: `agent_started` 3, `model_request` 4, `model_response` 4, `tool_intent` 1, `tool_result` 1, `agent_turn` 3, `agent_stopped` 1 |
| Artifacts | 9 / 3 turns = **3.0/turn**, 44.8 KB total → **14.9 KB/turn** (2 request + 2 response + 1 tool result per attempt-ish; request artifact grows 9,946 → 11,278 B as context grows) |
| Tool-result artifact | 1,140 B for a small bash output; **30,252 B** for 20 KB output; **203,011 B** for 100 KB output (Pi result record wraps output, ≈2× raw) |
| Next-request artifact after a large tool output | **61,581 B** (the tool output is re-sent inside Pi's messages) |
| Session JSONL | 13,890 B / 3 turns = **4,630 B/turn**, 4 lines/turn; parsing all session lines with `json.loads`: 0.53 ms total = **44 µs/line** (avg line ≈1.16 KB) |
| Runner Python RSS | **34.5–35.9 MB** |
| Pi Node child RSS | **121.8–122.4 MB** per agent |
| Heartbeats | with `heartbeat_s=1` and a 2.2 s bash sleep: **2 heartbeats at +1.004 s, +2.006 s** (covers `turns`, `status`, `rss_bytes`) |
| RPC calls per 3 turns | `recorder.append` 17, `recorder.put_artifact` 9, `budget.reserve/settle` 4+4, `budget.validate_request` 4, `budget.wait_admission` 4, `budget.stop_requested` 3, `runner.ready/finished` 1+1 |
| Large-output turn | session grows to **114,473 B/turn**; artifacts 274,973 B (203 KB tool result + 61.6 KB request + responses) |

Per model attempt in the Pi path the control plane sees **≈8 RPC calls** (4 budget + 2 append + 2
artifact) plus **4 ledger events** (`model_admitted`, `model_request`, `model_usage`, `model_response`),
and Pi adds ~5 lifecycle/tool events per turn. At 24 k model calls / ~18 k turns this is ≈ **190–250 k
events** and ≈ **80–100 k artifacts** — consistent with the “240 k+ events / ~100 k artifacts” target.

### 5.3 JSON parsing costs

- pi_runner parses Pi stdout JSONL line-by-line and **skips `message_update` / `bash_execution_update`
  deltas** (`pi_runner.py:_pump_stdout`), so token streaming does not allocate per delta.
- Session JSONL parse measured at 44 µs/line for ~1.16 KB records. Typical Pi event records (agent/tool
  events, ≤ a few KB) therefore cost tens of µs each; even 250 k events × 50 µs ≈ 12 s across the whole
  run — irrelevant.
- SSE capture parsing in the proxy is incremental and bounded (`_SSECapture`, `MAX_CAPTURE_CHARS` 512 KiB).

---

## 6. Paper-scale projection (24 k model attempts, 4 players, 54 h ceiling)

Grounded on: paper-hour-final sample (165,419 events, 12,992 calls, 56,301 artifacts, 236 MB artifacts,
222 MB DB, 689 MB run dir) and the measured Pi-path per-turn/attempt costs.

| Quantity | Measured basis | 24 k-call projection |
|---|---|---|
| Ledger events | 12.7 events/call (paper-hour sample); 4 proxy + ~5 Pi per turn | **240–300 k** |
| Ledger DB | 2.0–2.1 KB/event (this + previous audit); 222 MB @165 k | **350–500 MB** (+ WAL 4 MB) |
| Proxy artifacts | 2/model attempt; request artifact 9–62 KB in 64 k window, cap 2 MiB | 48 k artifacts, **3–6 GiB** request artifacts at realistic context growth |
| Tool-result artifacts | 0.3–203 KB each (1 MiB cap) | **0.5–2 GiB** |
| Pi session JSONL | 4.6 KB/turn small; 114 KB/turn with 100 KB tool output | **0.1–2.7 GiB per agent**, never pruned |
| Total run dir | paper-hour sample 689 MB (Python runtime) + Pi additions | **≈ 5–10 GiB** (dominated by request artifacts/sessions) |
| Proxy CPU overhead | 8.4 ms/attempt integrated | **≈ 3.4 CPU-min / 24 k calls** (0.35 s per 1 k calls) |
| RPC calls | 8/attempt + Pi lifecycle | **≈ 350–450 k calls**; 0.1–0.3 ms each ⇒ 40–130 s CPU total |
| Pi runners RSS | 35.6 MB Python + 122 MB Node per player | **≈ 0.63 GB for 4 players** (+ supervisor/observer/dashboard) |
| Heartbeats | 1/s default per runner | 86 k RPCs/54 h at 4 players (negligible) |

**Interpretation:** the chain's *latency* budget is fine (proxy +8.4 ms on a multi-second model call;
RPC/ledger overhead ≈ 1 % of wall time). The chain's *disk* budget needs attention: request artifacts
and Pi sessions are the only components that grow without bound per call/turn.

---

## 7. Dashboard / viewer query timing (read-only sample DBs)

Samples: **paper-hour-final** (real paper-shaped run: 165,419 events, 33 kinds, 12,966 submissions,
56,301 artifacts, 232 MB DB), **perf-paper-250k** (synthetic 250,204 events, 583 MB DB, 703 artifacts,
24,000 submissions), **real-mc-run8** (3,431 events, 4.5 MB DB). `dashboard_data` functions called
in-process; figures uses the real cache. No live experiment; **end-to-end live latency not measured**.

| Query | paper-hour-final | perf-paper-250k | real-mc-run8 |
|---|---|---|---|
| `overview` cold | **154.8 ms** | **335.6 ms** | 4.6 ms |
| `overview` warm (TTL cache) | 0.14 ms | 0.12 ms | 0.12 ms |
| `events_page` default / kind / actor / cursor | 0.73–0.86 ms | 0.89–0.97 ms | 0.52–0.89 ms |
| `events_page` category=evidence | **38.6 ms** (6 items) | 0.55 ms | 0.68 ms |
| `events_page` category=program/model | 0.86–0.89 ms | 0.9–0.94 ms | 0.87–1.03 ms |
| `events_page` free-text `q=error` | 1.17 ms (matches) | **422.7 ms** (0 matches → full scan) | 0.76 ms |
| `event_detail` largest payload | 0.31 ms (17 KB) | 0.84 ms (525 KB) | 0.28 ms (5.6 KB) |
| `chat_page` cold / warm | 7.8 / 0.9 ms | 16.8 / 5.4 ms | 7.9 / 0.03 ms |
| `jobs_page` cold | 32.0 ms | 96.6 ms | 0.8 ms |
| `observer_page` | 0.20 ms | 0.26 ms | 1.10 ms |
| `resources_page` | 0.24 ms | 1.43 ms | 0.95 ms |
| `programs_page` cold / warm | 35.4 / 0.31 ms | 103.4 / 0.30 ms | 1.68 / 0.30 ms |
| `program_detail` | 52.1 ms | 116.1 ms | 1.13 ms |
| `report_page` | 0.13 ms | 0.20 ms | 0.14 ms |
| `figures` cold / warm | **446.5 ms** / 0.26 ms, payload **402.6 KB** | **883.2 ms** / 0.29 ms, payload **1.269 MB** | 21.9 ms / 0.19 ms, 26.6 KB |

Loopback HTTP through the real `create_dashboard` server (10 sequential requests; static sample DBs,
no live writer; first `overview` request may include the cold build):

| Endpoint | paper-hour-final p50 / p95 | perf-paper-250k p50 / p95 | response bytes |
|---|---|---|---|
| `/api/overview` | 2.37 / 156.4 ms | 2.28 / 287.7 ms | 12 KB / 25 KB |
| `/api/events` | 3.62 / 4.45 ms | 3.34 / 5.52 ms | 93 KB / 83 KB |
| `/api/events?q=error` | 4.82 / 5.43 ms | **379.9 / 386.7 ms** | 171 KB / 0.3 KB |
| `/api/chat` | 3.55 / 10.11 ms | 10.63 / 24.91 ms | 17 KB / 21 KB |
| `/api/programs` | 3.35 / 3.98 ms | 2.74 / 81.74 ms | 41 KB / 32 KB |
| `/api/report` | 2.16 / 2.98 ms | 2.14 / 3.11 ms | 20 KB / 35 KB |
| `/api/resources` | 2.44 / 2.95 ms | 4.04 / 4.65 ms | 0.36 KB / 146 KB |
| `/api/figures` | 10.80 / 13.09 ms | 14.61 / 16.03 ms | 402.6 KB / 1.27 MB (gzip on wire; cache warm) |

Notes:
- `figures` now has a **30 s TTL cache** (`figures.py:_CACHE_TTL_S = 30.0`) and a build lock; at a 15 s
  dashboard poll the page may lag ≤30 s, but a live ledger no longer forces an O(history) rebuild per
  poll. Cold rebuild is still 0.45–0.88 s and reads every registered log artifact (see previous audit).
- `overview` cold remains an O(all events) aggregate (155–336 ms at 165–250 k events); it is cached 5 s
  with single-flight, so a live dashboard pays it at most once per TTL → **3–7 % of one core**.
- Free-text search (`q`) and rare-category filters still scan the ledger in SQLite while searching for
  the 50-row page: 423 ms when there are zero matches at 250 k events. Live dashboards show this as a
  UI stall on a misspelled/no-result query.
- Programs/detail are no longer full scans in schema terms — `recorder.py` now has
  `events_execution_actor_seq` and `submissions_execution` indexes — but payload projection/decoding of
  100 events × up to 16 KB still costs 35–116 ms cold.
- Live-server latency will be higher than the loopback numbers because a real dashboard also competes
  with the writer and the Python GIL; use the per-query numbers as lower bounds.

---

## 8. Bottlenecks, thresholds, recommendations

Severity: **P0** = must fix before the next long run; **P1** = fix for cost/reliability at paper scale;
**P2** = worthwhile.

### P0 — none found that blocks the next run
No deadlock, unbounded queue, or multi-second per-call stall was found on the measured Pi path. All
denials/timeouts/oversize bodies fail closed and fast.

### P1 — disk growth and per-attempt metering
1. **Request artifacts re-record the whole Pi context on every attempt** (`pi_model_proxy.py:_put_artifact`
   on `body`; up to 2 MiB, measured 9.4→61.6 KB with one 100 KB tool output). At 24 k calls this is the
   dominant disk consumer (3–6 GiB). *Fix:* content-address message chunks/prefixes (reuse the previous
   request artifact + append only new messages), or gzip JSON artifacts ≥64 KiB (text JSON compresses
   3–5×), or store the request artifact only when it differs by more than a threshold from the previous
   turn. Keep the durable pointer in the event.
2. **Pi session JSONL is never pruned** (`pi_runner.py` uses `--session-dir <state>/pi-sessions`;
   resume via `--continue`). Measured 4.6 KB/turn small, 114 KB/turn with a 100 KB tool output. *Fix:*
   after N turns (or on each settlement) rotate sessions: keep the last k for `--continue`, archive/delete
   older files; the ledger is the durable record, Pi sessions only need resume state.
3. **Proxy performs 8 serial RPC round trips before the upstream call** (measured +8.4 ms TTFB with
   real RPC). This is invisible against a multi-second provider call, but it is ~192 k RPC calls per
   24 k-call run and makes the proxy the second-largest control-plane client after Pi itself. *Fix:*
   collapse `wait_admission`+`validate_request`+`reserve` into one `budget.begin_model_call`, and
   `put_artifact`+`append(model_request)` into one `recorder.append_with_artifact`; then
   `settle`+`put_artifact(response)`+`append(model_response)` into one post-call RPC. 8 → 2–3 calls,
   TTFB overhead → ~3–4 ms, RPC count halved.

### P2 — dashboard cold paths and large frames
4. **Free-text search / rare-category scans** (`dashboard_data.events_page`): 423 ms (no matches) at
   250 k events, 38.6 ms for evidence with 6 hits at 165 k. *Fix:* FTS5 over kind/actor/payload preview
   (with a hard LIMIT/time budget), or restrict `q` to a recent window by default, or materialize
   category/severity columns at insert time (recorder is the single writer).
5. **`overview` cold aggregate** is O(all events): 155 ms @165 k, 336 ms @250 k → will reach ~0.6–1 s at
   500 k. *Fix:* maintain per-kind/per-actor counters in `state` (increment in `Recorder.append`) and
   read them in `overview`; then cold overview is O(1). Threshold: p95 < 100 ms after warm-up.
6. **`figures` cold rebuild** 447–883 ms and payload 0.40–1.27 MB; `programs`/`program_detail`/`jobs`
   cold 32–116 ms. *Fix:* incremental figure projection (chat/death/advancement facts persisted as they
   arrive instead of re-reading every log artifact), server-side payload cap/pagination for chats, and
   keep the existing TTL; threshold cold < 300 ms, payload < 300 KB.
7. **Large RPC frames**: 1 MiB append 10.2 ms, 1 MiB artifact put 6.2 ms, 8 MiB local artifact 7.9 ms.
   Current design keeps big data in artifacts, which is correct. If Pi tool outputs often hit the 1 MiB
   artifact cap, consider a binary/base64 artifact fast-path or gzip before RPC; threshold p95 < 15 ms.

### P3 — cheap wins / notes
8. **Per-event FULL commits** are 4.1 k/s vs 42.5 k/s with 100-event group commit. At 240 k events the
   absolute cost is only ≈58 s/run, so this is not urgent; an `append_many` for the 5–8 events a turn
   emits would cut RPCs and commits together.
9. **Fresh unix-socket + hello per RPC call** costs 0.11 ms baseline (8 calls/attempt ≈0.9 ms). Pooling
   or a framed multiplexed connection would remove it, at the cost of new lifecycle complexity.
10. **Source snapshot hash** 4.51 ms / 5.9 MB is already negligible; do not optimize.
11. **Non-stream >2 MiB silent truncation** in the proxy (§2) is a correctness edge, not perf; fix if a
    non-stream provider path is ever added.
12. **Process sizing**: plan for ≈0.16 GB per Pi player (35.6 MB Python + 122 MB Node) ≈0.63 GB for four
    players, plus supervisor/observer/dashboard; keep the existing 512 MB worker RSS cap in mind for the
    bot workers (separate from Pi).

**Suggested acceptance thresholds for the next long run** (measure with the same scripts):
proxy overhead TTFB < 20 ms; RPC p95 < 2 ms for ≤64 KB and < 15 ms for 1 MiB; ledger append p95 < 2 ms;
heartbeat cadence within ±20 % of config; `runner.ready` < 200 ms; Pi boot→first model < 2 s;
dashboard warm overview/events < 5 ms, cold overview < 500 ms @250 k, free-text search < 200 ms;
figures payload < 300 KB; proxy request-artifact bytes per 1 k calls < 100 MB; Pi session bytes per
1 k turns < 100 MB.

---

## 9. Exact commands (all read-only, loopback/temp dirs)

```sh
cd /ssd_data/caizihao/minecraft/zkdx/experiments/minecraft_harness
W=$PWD/runs/.chainperf           # gitignored scratch dir (scripts + results)

# 1. Pi model proxy overhead (fake upstream; stub RPC)
PYTHONPATH=$PWD/src .test-venv/bin/python $W/bench_proxy.py
# 1b. Integrated proxy: real RpcServer/ServiceHub/Recorder behind the proxy
PYTHONPATH=$PWD/src:$PWD/tests .test-venv/bin/python $W/bench_proxy_integrated.py

# 2. Runner RPC latency/throughput/payloads/timeouts/close (real ServiceHub on a temp unix socket)
PYTHONPATH=$PWD/src .test-venv/bin/python $W/bench_rpc.py

# 3. Ledger append/WAL, artifact hash/write/read, source snapshot hash, context summary
PYTHONPATH=$PWD/src .test-venv/bin/python $W/bench_chain_ledger.py

# 4. Real Pi CLI + fake upstream/control plane: startup, turns, events, artifacts, sessions, heartbeats
PYTHONPATH=$PWD/src:$PWD/tests .test-venv/bin/python $W/bench_pi_runner.py

# 5. Pi tool bridge (extension -> loopback HTTP -> RPC) latency + limits
PYTHONPATH=$PWD/src .test-venv/bin/python $W/bench_pi_bridge.py

# 6. Dashboard query + loopback HTTP timing over read-only sample DBs
PYTHONPATH=$PWD/src .test-venv/bin/python $W/bench_dashboard.py
```

Raw results: `runs/.chainperf/results/{proxy,proxy_integrated,rpc,chain_ledger,pi_runner,pi_bridge,dashboard}.json`.
Sample DBs are opened with `?mode=ro`; no experiment container, no final11, no source edits.

Environment facts recorded for reproducibility: HEAD `83a56f63`; Python 3.12.14; Node v22.23.2; SQLite
3.53.1; Pi CLI 1.4.0. File SHA-256 (audit revision):

```
pi_model_proxy.py  770c77768a2c1b6c4a091fb1d411485f5e735a4bff7ca8e3657096899c1f4783
pi_runner.py       0106e5e4960ac2dea78c002efdfada8cd3573455933011be88b04c9475949dc3
pi_tool_bridge.py  157290837000a082f9c59750bc3b232e55d7918b4d1369e8fc5f3350eea2ecbd
runner_rpc.py      9b8a156957d8a27ecdf4c5fa7ad861ccaa8902cb31e31a7f1028093c08136e1a
runners.py         7662a32155009103945d6ae72ca2143e9b5ba4e10d6c06615ee694bc95bc80bf
runner.py          475f97ac95f035630d2dbe05a385eaf7441d4dabfc9f3669c8cb6deb8e0926d9
recorder.py        6a81a0173a3b949ce66b9d69b50fcdc46837b5738130011e957d1e158ff561f0
budget.py          04cb9faa974ae10b10638a28a6f1af02cc862ff170455567582066c828569388
agents/context.py  36816fef9d0548af2940b1f7165f64062f6d5dfe6c130ff600f37b4f0103bd93
memory/store.py    fa18ee84046d9b817185b80a2f777423e37a0a3c396e99c283dba52ff472eb5f
figures.py         0ce9efc421f2f44907c49bbfea41faf7ab2fdbea8ac1366c9ca62880c954532c
dashboard_data.py  04e436c311a4bc92b549fab2b01d4627aaae4112160bf2a517039f024c74cbf5
pi extension       a9d3de9d9996fabcd3d4b2d4ade7b27dcfae2a2609a9ad88dfa2eeb5d5fa3fb7
Pi CLI bundle      e79626f2dd6f94aa45d30f3fa63cd84319a6eefcd150b353cfaf274366926774
```

---

## 10. Limitations / what would change these numbers

- The model upstream is a scripted loopback SSE server: provider latency, retries, rate limits and real
  token streams (thousands of chunks) were not exercised end-to-end. Proxy SSE cost scales at
  ~2 µs/chunk, so a 4,096-token stream (~2–4 k chunks) would add ≈4–8 ms of proxy work — still minor.
- `bench_pi_runner` uses the repo's fake control plane (budget events `model_admitted`/`model_usage` are
  not inserted there), so per-turn ledger-event counts undercount production by 2 events/attempt; the
  integrated proxy bench measures the real budget path and confirms 4 ledger rows/attempt.
- The dashboard HTTP measurements are loopback against idle sample DBs; live latency includes GIL/
  writer contention and will be higher. No live experiment was started (by design).
- Host load average was ~40 during the audit; fsync-dominated and JSON-dominated numbers are stable,
  but absolute tail latencies are conservative.
- The harness tree is untracked in the enclosing git repo, so the revision is pinned by the SHA-256 list
  above; re-run the §9 commands after any change and compare with the §8 thresholds.
