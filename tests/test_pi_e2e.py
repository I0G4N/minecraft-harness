"""Host-level end-to-end tests for the Pi RPC runtime (no Docker, loopback only).

These tests independently exercise the real integration that ships to a paper
run:

* the real ``python -m minecraft_harness.pi_runner`` process;
* the real pinned Pi coding-agent CLI (``pi_runtime/.../cli.js``, Node >= 22);
* the real ``RpcServer`` control-plane wire protocol with a fake ledger/budget
  service that records everything the runner does;
* a fake OpenAI-compatible SSE upstream on ``127.0.0.1`` that impersonates the
  provider (no external network is used).

Covered contract points:
- Pi receives the paper ``paper-framework-v4`` prompt (plus the Pi environment
  appendix) and executes its built-in ``bash`` tool, writing a proof file;
- each model attempt is reserved and settled exactly once, model/tool events
  reach the recorder, and ``runner.ready``/heartbeat/``runner.finished`` are
  honoured;
- SIGTERM aborts the active Pi run and finishes with ``status=stopped``;
- a restarted runner resumes the previous Pi session (``--continue``);
- the model API key, the runner RPC token and the HARNESS secret environment
  never reach the ledger, artifacts, Pi child environment or runner output.

The tests skip when Node or the pinned Pi bundle is unavailable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
VENV_SITE_PACKAGES = REPO / ".venv" / "lib" / "python3.11" / "site-packages"
PI_CLI = (REPO / "pi_runtime" / "node_modules" / "@earendil-works" /
          "pi-coding-agent" / "dist" / "bundle" / "cli.js")
NODE = shutil.which("node")

MODEL_API_KEY = "sk-pi-e2e-secret-that-must-never-leak"
RUNNER_TOKEN = "tok-pi-e2e-secret-that-must-never-leak"
GOAL_TOKEN = "E2E_PAPER_GOAL_TOKEN_DRAGON"
PROOF_NAME = "pi-e2e-proof.txt"
PROOF_TEXT = "pi-e2e-bash-proof-ok"

# Harness-internal vocabulary the paper prompt must not expose.
FORBIDDEN_PROMPT_TERMS = (
    "history_read", "context_compacted", "mc_pending", "mc_execute",
    "artifact_read", "artifact", "evidence_ref", "leftover_policy",
    "paper-framework", "Prompt version",
)
PAPER_PROMPT_PHRASES = (
    "Observe the environment before acting",
    "Validate a new tool on a short task",
    "Pursue the objective rather than exploit flaws",
    "mc exec --file",
    "Mineflayer",
)


def _pi_available() -> bool:
    return bool(NODE) and PI_CLI.is_file()


@unittest.skipUnless(_pi_available(), "requires Node.js >= 22 and the pinned Pi CLI bundle")
class _PiE2EBase(unittest.IsolatedAsyncioTestCase):
    """Shared fixtures: fake upstream, fake control plane, runner process."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pi-e2e-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.reference = self.root / "reference"
        self.team_memory = self.root / "memory" / "team"
        self.state = self.root / "state"
        for directory in (self.workspace, self.reference, self.team_memory, self.state):
            directory.mkdir(parents=True, exist_ok=True)
        self.run_id = hashlib.sha256(self.root.name.encode()).hexdigest()[:32]
        self.servers = []
        self.upstreams = []
        self.processes = []

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
        for server in self.servers:
            try:
                await server.close()
            except Exception:
                pass
        for upstream in self.upstreams:
            upstream.stop()

    # -- fake OpenAI SSE upstream -------------------------------------
    def make_upstream(self, scenario):
        upstream = FakeUpstream(scenario)
        self.upstreams.append(upstream)
        return upstream

    # -- fake control plane -------------------------------------------
    async def start_control_plane(self):
        from minecraft_harness.runner_rpc import RpcServer

        hub = FakeControlPlane()
        token = RUNNER_TOKEN + "-" + self.run_id[:8]
        hub.token = token
        socket_path = self.root / "rpc" / "control.sock"
        server = RpcServer(str(socket_path), {token: {"agent_id": "player1", "role": "player"}},
                           hub.handle)
        await server.start()
        self.servers.append(server)
        return hub, token, socket_path

    # -- pi_runner process --------------------------------------------
    def _spec(self, upstream, *, max_turns=1, model=None):
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "agent_id": "player1",
            "role": "player",
            "state_root": str(self.state),
            "heartbeat_s": 0.1,
            "shell_enabled": False,
            "runner_runtime": "pi",
            "pi_cli": str(PI_CLI),
            "pi_context_window": 64000,
            "workspace_root": str(self.workspace),
            "reference_root": str(self.reference),
            "team_memory_root": str(self.team_memory),
            "broker_socket": None,
            "cli_environment": {},
            "pi_model": model or {
                "kind": "chat_completions",
                "model": "deepseek-flash",
                "base_url": upstream.base_url,
                "api_key_env": "PI_E2E_API_KEY",
                "max_output_tokens": 4096,
                "timeout_s": 60,
                "prices_per_million": {"input": 0.3, "output": 1.2},
            },
            "agent_config": {
                "agent_id": "player1",
                "goal": f"{GOAL_TOKEN} defeat the Ender Dragon in the shared world",
                "peers": ["player2", "player3", "player4"],
                "profile": "paper_aligned",
                "lifecycle": "paper_aligned",
                "game_chat": True,
                "chat_delivery": "program_output",
                "max_turns": max_turns,
                "context_chars": 48000,
                "web": {"enabled": True, "provider": "custom", "search_url": None,
                        "fetch_url": None, "allowed_hosts": []},
            },
            "model": {"kind": "fake"},
        }

    async def start_runner(self, hub, token, socket_path, upstream, *, max_turns=1,
                           extra_env=None, role="player"):
        spec_path = self.root / "spec.json"
        spec = self._spec(upstream, max_turns=max_turns)
        spec["role"] = role
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        spec_path.chmod(0o600)
        pythonpath = os.pathsep.join([str(SRC), str(VENV_SITE_PACKAGES)])
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": pythonpath,
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "HARNESS_RUNNER_SOCKET": str(socket_path),
            "HARNESS_RUNNER_TOKEN": token,
            "HARNESS_RUNNER_AGENT": "player1",
            "HARNESS_RUNNER_ROLE": role,
            "HARNESS_RUNNER_SPEC": str(spec_path),
            "HARNESS_RUNNER_RUN_ID": self.run_id,
            "HARNESS_RUNNER_RPC_TIMEOUT_S": "60",
            "PI_E2E_API_KEY": MODEL_API_KEY,
            "HARNESS_E2E_SECRET": "harness-secret-must-not-reach-pi",
        }
        (self.root / "home").mkdir(exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)
        environment.update(extra_env or {})
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "minecraft_harness.pi_runner",
            cwd=str(self.workspace), env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.processes.append(process)
        return process

    async def finish(self, process, timeout=180.0):
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        return process.returncode, stdout, stderr

    # -- helpers ------------------------------------------------------
    @staticmethod
    def _flatten(value):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    def _state_texts(self):
        texts = {}
        for path in sorted(self.state.rglob("*")):
            if path.is_file() and path.stat().st_size <= 4 * 1024 * 1024:
                try:
                    texts[str(path.relative_to(self.root))] = path.read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    continue
        return texts


class FakeUpstream:
    """Loopback OpenAI-compatible SSE upstream driven by a scenario callback.

    ``scenario(index, body)`` returns ``{"chunks": [...], "usage": {...}}``.
    Every request body and authorization header is recorded for assertions.
    """

    def __init__(self, scenario):
        self.scenario = scenario
        self.requests = []
        self.authorization = []
        self.paths = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                outer._record(f"GET {self.path}", None)
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("content-length", "0") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except (UnicodeDecodeError, ValueError):
                    body = {}
                outer._record(self.path, body, self.headers.get("authorization"))
                outer._respond(self, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def _record(self, path, body, authorization=None):
        with self._lock:
            self.paths.append(path)
            if body is not None:
                self.requests.append(body)
            if authorization is not None:
                self.authorization.append(authorization)

    def _respond(self, handler, body):
        with self._lock:
            index = len(self.requests) - 1
        result = self.scenario(index, body)
        usage = result.get("usage") or {"prompt_tokens": 11, "completion_tokens": 7,
                                        "total_tokens": 18}
        chunks = list(result.get("chunks") or [])
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.send_header("connection", "close")
        handler.end_headers()
        try:
            for chunk in chunks:
                handler.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
            final = {
                "id": "chatcmpl-pi-e2e",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            }
            handler.wfile.write(b"data: " + json.dumps(final).encode("utf-8") + b"\n\n")
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        handler.close_connection = True

    def stop(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass


class FakeControlPlane:
    """Minimal recorder/budget runner.* service that records every RPC.

    It mirrors the control-plane contract from ``runners.ServiceHub`` (bound
    agent identity, reservation ids, settle-once) without starting a Supervisor.
    """

    def __init__(self):
        self.token = ""
        self.events = []
        self.artifacts = {}
        self.ready = None
        self.heartbeats = []
        self.finished = []
        self.reservations = {}
        self.settlements = []
        self.unknown_methods = []
        self.ready_event = asyncio.Event()
        self.heartbeat_event = asyncio.Event()
        self.finished_event = asyncio.Event()
        self.tool_intent_event = asyncio.Event()
        self.stop_requested = False
        self._reservation_counter = 0

    async def handle(self, agent_id, role, method, params):
        params = params if isinstance(params, dict) else {}
        if method == "runner.ready":
            self.ready = dict(params)
            self.ready_event.set()
            return {"accepted": True}
        if method == "runner.heartbeat":
            self.heartbeats.append(dict(params))
            self.heartbeat_event.set()
            return {"accepted": True}
        if method == "runner.finished":
            self.finished.append(dict(params.get("result") or {}))
            self.finished_event.set()
            return {"accepted": True}
        if method == "recorder.append":
            kind = str(params.get("kind"))
            payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
            kwargs = params.get("kwargs") if isinstance(params.get("kwargs"), dict) else {}
            actor = kwargs.get("actor_id") or agent_id
            self.events.append({"kind": kind, "payload": dict(payload), "actor_id": actor,
                                "kwargs": dict(kwargs)})
            if kind == "tool_intent":
                self.tool_intent_event.set()
            return {"event_seq": len(self.events), "kind": kind, "actor_id": actor,
                    "payload": payload}
        if method == "recorder.put_artifact":
            text = params.get("text")
            if isinstance(text, str):
                data = text.encode("utf-8")
            else:
                self.unknown_methods.append("recorder.put_artifact:no-text")
                raise ValueError("unsupported artifact fixture payload")
            digest = hashlib.sha256(data).hexdigest()
            self.artifacts[digest] = data
            return {"hash": digest, "sha256": digest, "path": "artifacts/" + digest}
        if method in {"recorder.read_events"}:
            return []
        if method == "recorder.latest_event_seq":
            return len(self.events)
        if method == "budget.wait_admission":
            return {"admitted": True, "stop": bool(self.stop_requested)}
        if method == "budget.validate_request":
            return None
        if method == "budget.stop_requested":
            return bool(self.stop_requested)
        if method == "budget.reserve":
            if self.stop_requested:
                raise RuntimeError("BudgetExhausted")
            self._reservation_counter += 1
            reservation = f"reservation-{self._reservation_counter}"
            self.reservations[reservation] = {
                "agent_id": agent_id, "role": role,
                "purpose": params.get("purpose"), "settled": False}
            return reservation
        if method == "budget.settle":
            reservation = params.get("reservation_id")
            usage = params.get("usage") if isinstance(params.get("usage"), dict) else {}
            if reservation not in self.reservations:
                raise KeyError("unknown reservation")
            if self.reservations[reservation]["settled"]:
                raise RuntimeError("reservation settled twice")
            self.reservations[reservation]["settled"] = True
            self.settlements.append({"reservation_id": reservation,
                                      "usage": dict(usage)})
            return None
        if method == "budget.admit_web":
            return {"admitted": True}
        self.unknown_methods.append(method)
        raise ValueError("unknown fixture method " + method)


class PiE2ERuntimeTests(_PiE2EBase):
    async def test_pi_runtime_end_to_end_prompt_bash_ledger_and_budget(self):
        def scenario(index, body):
            messages = body.get("messages") or []
            has_tool_result = any(isinstance(m, dict) and m.get("role") == "tool"
                                  for m in messages)
            if has_tool_result:
                return {"chunks": [{
                    "id": "chatcmpl-pi-e2e", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": body.get("model"),
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant", "content": "proved and done"},
                                 "finish_reason": None}]}]}
            command = (
                f"printf %s {PROOF_TEXT} > {PROOF_NAME}; "
                "cat /proc/$PPID/environ > proc-env-copy.txt 2>/dev/null; "
                "echo $? > proc-env-exit.txt; "
                "target=${HARNESS_PI_BRIDGE_URL#http://}; "
                "host=${target%%:*}; port=${target##*:}; "
                "exec 3<>/dev/tcp/$host/$port 2>/dev/null; "
                "printf 'GET /tool HTTP/1.0\r\n\r\n' >&3 2>/dev/null; "
                "head -c 400 <&3 > peer-response.txt 2>/dev/null; "
                "exec 3<&- 2>/dev/null; "
                "echo done"
            )
            return {"chunks": [{
                "id": "chatcmpl-pi-e2e", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": body.get("model"),
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": None,
                                                   "tool_calls": [{
                                                       "index": 0, "id": "call-pi-e2e-1",
                                                       "type": "function",
                                                       "function": {"name": "bash",
                                                                    "arguments": json.dumps({"command": command})}}]},
                             "finish_reason": None}]}]}

        upstream = self.make_upstream(scenario)
        hub, token, socket_path = await self.start_control_plane()
        process = await self.start_runner(hub, token, socket_path, upstream, max_turns=1)
        await asyncio.wait_for(hub.ready_event.wait(), 120)
        # Live same-uid /proc read must be denied by the LD_PRELOAD prctl guard.
        pi_pid = None
        for _attempt in range(1200):
            starts = [event for event in hub.events if event["kind"] == "agent_started"]
            if starts and starts[0]["payload"].get("pi_pid"):
                pi_pid = starts[0]["payload"]["pi_pid"]
                break
            await asyncio.sleep(0.05)
        self.assertIsInstance(pi_pid, int)
        with self.assertRaises(PermissionError):
            Path(f"/proc/{pi_pid}/environ").read_bytes()
        await asyncio.wait_for(hub.finished_event.wait(), 60)
        returncode, stdout, stderr = await self.finish(process)

        # 1. Control protocol: ready / heartbeat / finished, clean exit.
        self.assertEqual(returncode, 0, stderr.decode(errors="replace")[-3000:])
        self.assertIsNotNone(hub.ready)
        self.assertEqual(hub.ready.get("agent_id"), "player1")
        self.assertEqual(hub.ready.get("role"), "player")
        self.assertRegex(str(hub.ready.get("prompt_sha256")), r"^[0-9a-f]{64}$")
        self.assertRegex(str(hub.ready.get("tools_sha256")), r"^[0-9a-f]{64}$")
        self.assertTrue(hub.heartbeat_event.is_set(), "no heartbeat reached the control plane")
        self.assertTrue(hub.heartbeats[0].get("status"))
        self.assertEqual(hub.heartbeats[0].get("runtime"), "pi")
        self.assertEqual(hub.finished[0].get("status"), "turns_exhausted")
        self.assertEqual(hub.finished[0].get("turns"), 1)

        # 2. Pi received the paper v4 prompt plus the Pi environment appendix.
        self.assertGreaterEqual(len(upstream.requests), 2)
        first_request = upstream.requests[0]
        prompt_text = self._flatten(first_request.get("messages") or [])
        for phrase in PAPER_PROMPT_PHRASES:
            self.assertIn(phrase, prompt_text)
        self.assertIn(GOAL_TOKEN, prompt_text)
        self.assertIn("Built-in tools: read, write, edit, bash", prompt_text)
        for forbidden in FORBIDDEN_PROMPT_TERMS:
            self.assertNotIn(forbidden, prompt_text, forbidden)
        tool_names = {item.get("function", {}).get("name")
                      for item in (first_request.get("tools") or [])
                      if isinstance(item, dict)}
        self.assertEqual(tool_names, {
            "read", "write", "edit", "bash",
            "memory_read", "memory_write", "memory_list", "memory_search",
            "web_search", "web_fetch",
        })

        # 3. The built-in bash tool actually executed in the workspace, and the
        #    model-directed shell could not read the Pi environment or talk to
        #    the loopback bridge directly.
        proof = self.workspace / PROOF_NAME
        self.assertTrue(proof.is_file(), "Pi bash tool did not write the proof file")
        self.assertEqual(proof.read_text(encoding="utf-8"), PROOF_TEXT)
        self.assertEqual((self.workspace / "proc-env-copy.txt").read_bytes(), b"")
        self.assertNotEqual(
            (self.workspace / "proc-env-exit.txt").read_text().strip(), "0")
        peer_response = (self.workspace / "peer-response.txt").read_bytes()
        self.assertNotIn(b"HTTP/", peer_response)
        self.assertNotIn(b"401", peer_response)

        # 4. Tool + model events are durable in the ledger with the bound actor.
        kinds = [event["kind"] for event in hub.events]
        for expected in ("agent_started", "tool_intent", "tool_result",
                         "model_request", "model_response", "agent_turn",
                         "agent_stopped"):
            self.assertIn(expected, kinds, kinds)
        self.assertTrue(all(event["actor_id"] == "player1" for event in hub.events))
        intent = next(event for event in hub.events if event["kind"] == "tool_intent")
        self.assertEqual(intent["payload"].get("tool"), "bash")
        self.assertEqual(intent["payload"].get("idempotency"), "side_effect")
        self.assertRegex(str(intent["payload"].get("args_sha256")), r"^[0-9a-f]{64}$")
        result_event = next(event for event in hub.events if event["kind"] == "tool_result")
        self.assertFalse(result_event["payload"].get("is_error"))
        result_ref = result_event["payload"].get("result_ref") or {}
        self.assertIn(result_ref.get("hash"), hub.artifacts)
        self.assertEqual(result_event["payload"].get("runtime"), "pi")

        # 5. Every model attempt was admitted, reserved and settled exactly once.
        model_requests = [event for event in hub.events if event["kind"] == "model_request"]
        model_responses = [event for event in hub.events if event["kind"] == "model_response"]
        self.assertEqual(len(model_requests), len(upstream.requests))
        self.assertEqual(len(model_requests), 2)
        self.assertEqual(len(model_responses), 2)
        self.assertEqual(len(hub.reservations), 2)
        self.assertEqual(len(hub.settlements), 2)
        settled_ids = [item["reservation_id"] for item in hub.settlements]
        self.assertEqual(len(set(settled_ids)), 2)
        self.assertEqual(set(settled_ids), set(hub.reservations))
        for event in model_requests:
            self.assertIn(event["payload"].get("reservation_id"), hub.reservations)
            self.assertEqual(event["payload"].get("runtime"), "pi")
            self.assertEqual(event["payload"].get("role"), "player")
        for settlement in hub.settlements:
            self.assertGreater(settlement["usage"].get("total_tokens") or 0, 0)

        # 6. Credentials: the proxy used the model key upstream, but no secret
        #    reached the ledger, artifacts, Pi child environment or output.
        self.assertIn("Bearer " + MODEL_API_KEY, upstream.authorization)
        recorded = self._flatten([hub.events, {k: v.decode("utf-8", "replace")
                                               for k, v in hub.artifacts.items()}])
        output = stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace")
        state_texts = self._flatten(self._state_texts())
        for secret in (MODEL_API_KEY, RUNNER_TOKEN, "harness-secret-must-not-reach-pi"):
            self.assertNotIn(secret, recorded, secret)
            self.assertNotIn(secret, output, secret)
            self.assertNotIn(secret, state_texts, secret)
        # The Pi environment itself is no longer readable by a same-uid sibling
        # (checked live above), and Pi's local credential file was removed.

        # 7. Loopback-only wiring: Pi's local credential file was removed after
        #    the first authenticated request, and the redaction is ledgered.
        models_path = self.state / "home" / ".pi" / "agent" / "models.json"
        self.assertFalse(models_path.exists())
        self.assertIn("pi_cli_auth_redacted",
                      [event["kind"] for event in hub.events])
        system_prompt = (self.state / "system-prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(hub.ready.get("prompt_sha256"),
                         hashlib.sha256(system_prompt.encode()).hexdigest())

    def _pi_child_environments(self):
        """Read environments of this test's Pi CLI child process (best effort)."""
        environments = []
        state_root = str(self.state)
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            try:
                cmdline = (directory / "cmdline").read_bytes()
            except OSError:
                continue
            if b"cli.js" not in cmdline:
                continue
            try:
                raw = (directory / "environ").read_bytes()
            except OSError:
                continue
            environment = {}
            for item in raw.split(b"\0"):
                if b"=" not in item:
                    continue
                key, value = item.split(b"=", 1)
                environment[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
            agent_dir = environment.get("PI_CODING_AGENT_DIR", "")
            if agent_dir.startswith(state_root):
                environments.append(environment)
        return environments

    async def _capture_child_environments(self, until, *, timeout):
        """Poll /proc while the Pi run is active and return distinct child envs."""
        captured = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            environments = self._pi_child_environments()
            if environments:
                if environments[0] not in captured:
                    captured.extend(environments)
            if until.is_set():
                break
            await asyncio.sleep(0.02)
        return captured

    async def test_sigterm_aborts_active_pi_run_and_finishes_cleanly(self):
        def scenario(index, body):
            command = "sleep 30 >/dev/null 2>&1; echo late > pi-late.txt"
            return {"chunks": [{
                "id": "chatcmpl-pi-e2e", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": body.get("model"),
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": None,
                                                   "tool_calls": [{
                                                       "index": 0, "id": "call-pi-e2e-slow",
                                                       "type": "function",
                                                       "function": {"name": "bash",
                                                                    "arguments": json.dumps({"command": command})}}]},
                             "finish_reason": None}]}]}

        upstream = self.make_upstream(scenario)
        hub, token, socket_path = await self.start_control_plane()
        process = await self.start_runner(hub, token, socket_path, upstream, max_turns=100)
        await asyncio.wait_for(hub.ready_event.wait(), 120)
        await asyncio.wait_for(hub.tool_intent_event.wait(), 120)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(hub.finished_event.wait(), 120)
        returncode, _stdout, stderr = await self.finish(process, timeout=60)
        self.assertEqual(returncode, 0, stderr.decode(errors="replace")[-3000:])
        self.assertEqual(hub.finished[0].get("status"), "stopped")
        kinds = [(event["kind"], event["payload"].get("reason"))
                 for event in hub.events if event["kind"] == "agent_stopped"]
        self.assertTrue(any(kind == "agent_stopped" and reason in {"signal", "budget_stop"}
                            for kind, reason in kinds), kinds)
        self.assertFalse((self.workspace / "pi-late.txt").exists())

    async def test_pi_session_is_resumed_with_continue_after_restart(self):
        def first_scenario(index, body):
            messages = body.get("messages") or []
            if any(isinstance(m, dict) and m.get("role") == "tool" for m in messages):
                return {"chunks": [{
                    "id": "c1", "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": body.get("model"),
                    "choices": [{"index": 0, "delta": {"role": "assistant",
                                                       "content": "first session done"},
                                 "finish_reason": None}]}]}
            command = f"printf first-session > {PROOF_NAME}"
            return {"chunks": [{
                "id": "c1", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": body.get("model"),
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": None,
                                                   "tool_calls": [{
                                                       "index": 0, "id": "call-first",
                                                       "type": "function",
                                                       "function": {"name": "bash",
                                                                    "arguments": json.dumps({"command": command})}}]},
                             "finish_reason": None}]}]}

        def second_scenario(index, body):
            return {"chunks": [{
                "id": "c2", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": body.get("model"),
                "choices": [{"index": 0, "delta": {"role": "assistant",
                                                   "content": "resumed session done"},
                             "finish_reason": None}]}]}

        # First run: complete one bounded Pi run and persist the session.
        upstream1 = self.make_upstream(first_scenario)
        hub1, token1, socket1 = await self.start_control_plane()
        process1 = await self.start_runner(hub1, token1, socket1, upstream1, max_turns=1)
        await asyncio.wait_for(hub1.ready_event.wait(), 120)
        await asyncio.wait_for(hub1.finished_event.wait(), 240)
        returncode1, _out1, err1 = await self.finish(process1)
        self.assertEqual(returncode1, 0, err1.decode(errors="replace")[-2000:])
        sessions = list((self.state / "pi-sessions").rglob("*.jsonl"))
        self.assertTrue(sessions, "Pi did not persist a session file")
        session_count = len(sessions)
        self.assertEqual((self.workspace / PROOF_NAME).read_text(encoding="utf-8"),
                         "first-session")

        # Second run: same state/session dir must resume with --continue.
        upstream2 = self.make_upstream(second_scenario)
        hub2, token2, socket2 = await self.start_control_plane()
        process2 = await self.start_runner(hub2, token2, socket2, upstream2, max_turns=1)
        await asyncio.wait_for(hub2.ready_event.wait(), 120)
        await asyncio.wait_for(hub2.finished_event.wait(), 240)
        returncode2, _out2, err2 = await self.finish(process2)
        self.assertEqual(returncode2, 0, err2.decode(errors="replace")[-2000:])
        self.assertTrue(upstream2.requests, "resumed Pi run made no model request")
        resumed_text = self._flatten(upstream2.requests[0].get("messages") or [])
        self.assertIn("Resume the same task after a restart", resumed_text)
        self.assertIn("first session done", resumed_text)
        self.assertGreaterEqual(len(upstream2.requests[0].get("messages") or []), 4)
        self.assertEqual(len(list((self.state / "pi-sessions").rglob("*.jsonl"))),
                         session_count, "restart must continue the existing Pi session")
        resumed_events = [event for event in hub2.events if event["kind"] == "agent_started"]
        self.assertTrue(resumed_events)
        self.assertTrue(resumed_events[0]["payload"].get("resumed"))

    async def test_pi_runner_fails_closed_for_observer_role(self):
        upstream = self.make_upstream(lambda index, body: {"chunks": []})
        hub, token, socket_path = await self.start_control_plane()
        # pi_runner must refuse a non-player role before announcing readiness.
        process = await self.start_runner(hub, token, socket_path, upstream,
                                          role="observer")
        returncode, _stdout, stderr = await self.finish(process, timeout=120)
        self.assertEqual(returncode, 2)
        document = json.loads(stderr.decode("utf-8", errors="replace").strip().splitlines()[-1])
        self.assertEqual(document["__runner_error__"]["error"], "PiRunnerError")
        self.assertFalse(hub.ready_event.is_set())


if __name__ == "__main__":
    unittest.main()
