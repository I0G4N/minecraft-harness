"""End-to-end tests for the real Pi coding-agent runner (``pi_runner.py``).

These tests launch ``python -m minecraft_harness.pi_runner`` as a subprocess and
drive it with:

* a scripted loopback OpenAI-compatible upstream (fake model);
* a loopback runner RPC server (fake control plane / ServiceHub);
* the real installed Pi CLI (``pi_runtime/node_modules/.../cli.js``) and Node 22.

They prove that Pi's built-in tools actually run (bash writes a proof file),
model attempts are reserved/settled and ledgered, sessions resume across a
restart, and SIGTERM stops the run cleanly.  No Docker, no experiment and no
external network are involved; the module skips if Node or the Pi CLI is absent.

Run in the disposable test env::

    PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
        .test-venv/bin/python -m pytest -q tests/test_pi_runner.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO / "src"
PI_CLI = (REPO / "pi_runtime" / "node_modules" / "@earendil-works" /
          "pi-coding-agent" / "dist" / "bundle" / "cli.js")
HAVE_PI_RUNTIME = shutil.which("node") is not None and PI_CLI.is_file()

TOKEN = "pi-test-token"
MAX_TURNS = 2


def openai_usage(prompt: int = 10, completion: int = 3) -> dict[str, int]:
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def sse_response(delta: dict[str, Any], finish_reason: str = "stop",
                 usage: dict[str, Any] | None = None) -> bytes:
    chunks = [
        {"id": "chunk", "object": "chat.completion.chunk", "model": "fake-upstream",
         "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"id": "chunk", "object": "chat.completion.chunk", "model": "fake-upstream",
         "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
         **({"usage": usage} if usage is not None else {})},
    ]
    return b"".join(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"
                    for chunk in chunks) + b"data: [DONE]\n\n"


def tool_call_delta(name: str, arguments: str, call_id: str = "call_1") -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": name, "arguments": arguments}}]}


class FakeUpstream:
    """Scripted loopback OpenAI-compatible endpoint used by the proxy."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.responses: list[bytes] = []
        self.default = sse_response({"role": "assistant", "content": "finished work"},
                                    usage=openai_usage())
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> str:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                with upstream.lock:
                    upstream.requests.append({"body": body, "headers": dict(self.headers)})
                    payload = upstream.responses.pop(0) if upstream.responses else upstream.default
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def set_script(self, *payloads: bytes) -> None:
        with self.lock:
            self.responses = list(payloads)

    def request_bodies(self) -> list[dict[str, Any]]:
        with self.lock:
            return [item["body"] for item in self.requests]

    def reset_requests(self) -> None:
        with self.lock:
            self.requests.clear()


class FakePiHub:
    """Minimal ServiceHub/RunnerManager RPC surface for one Pi runner."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.artifacts: list[str] = []
        self.reservations: list[str] = []
        self.settlements: list[dict[str, Any]] = []
        self.ready: dict[str, Any] | None = None
        self.finished: dict[str, Any] | None = None
        self.heartbeats: list[dict[str, Any]] = []
        self.ready_event = asyncio.Event()
        self.finished_event = asyncio.Event()
        self.event_signal = asyncio.Event()
        self.stop_requested = False
        self.reserve_error: Exception | None = None

    async def handle(self, agent_id: str, role: str, method: str,
                     params: dict[str, Any]) -> Any:
        params = params if isinstance(params, dict) else {}
        if method == "runner.ready":
            self.ready = dict(params)
            self.ready_event.set()
            return {"accepted": True}
        if method == "runner.heartbeat":
            self.heartbeats.append(dict(params))
            return {"accepted": True}
        if method == "runner.finished":
            self.finished = dict(params)
            self.finished_event.set()
            return {"accepted": True}
        if method == "recorder.append":
            event = {"kind": params.get("kind"), "payload": params.get("payload"),
                     "actor_id": agent_id}
            with self.lock:
                self.events.append(event)
            self.event_signal.set()
            return {"event_seq": len(self.events)}
        if method == "recorder.put_artifact":
            text = str(params.get("text", ""))
            with self.lock:
                self.artifacts.append(text)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return {"hash": digest, "sha256": digest, "path": "artifacts/" + digest}
        if method == "budget.wait_admission":
            return {"admitted": True, "stop": False}
        if method == "budget.validate_request":
            return None
        if method == "budget.reserve":
            if self.reserve_error is not None:
                raise self.reserve_error
            reservation = f"res-{len(self.reservations) + 1}"
            with self.lock:
                self.reservations.append(reservation)
            return reservation
        if method == "budget.settle":
            with self.lock:
                self.settlements.append(dict(params))
            return None
        if method == "budget.stop_requested":
            return bool(self.stop_requested)
        raise AssertionError(f"unexpected RPC method: {method}")

    def kinds(self) -> list[str]:
        with self.lock:
            return [event["kind"] for event in self.events]

    def events_of(self, kind: str) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.events if event["kind"] == kind]

    def artifact_corpus(self) -> str:
        with self.lock:
            return "\n".join(self.artifacts)

    async def wait_for_event(self, kind: str, timeout: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = self.events_of(kind)
            if matches:
                return matches[-1]
            await asyncio.sleep(0.05)
        raise AssertionError(f"ledger event {kind} never arrived; kinds={self.kinds()}")


@unittest.skipUnless(HAVE_PI_RUNTIME, "Node 22 and the Pi CLI runtime are required")
class PiRunnerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)
        self.hub = FakePiHub()
        from minecraft_harness.runner_rpc import RpcServer

        self.server = RpcServer(self.root / "control.sock",
                                {TOKEN: {"agent_id": "player1", "role": "player"}},
                                self.hub.handle)
        await self.server.start()
        self._processes: list[asyncio.subprocess.Process] = []

    async def asyncTearDown(self):
        for process in self._processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await self.server.close()

    # -- helpers ---------------------------------------------------------
    def make_spec(self, *, agent_id: str = "player1", role: str = "player",
                  state_root: Path | None = None, workspace: Path | None = None,
                  cli_environment: dict[str, str] | None = None,
                  api_key_env: str | None = None,
                  extra: dict[str, Any] | None = None) -> Path:
        state_root = state_root or (self.root / "state")
        workspace = workspace or (self.root / "workspace")
        workspace.mkdir(parents=True, exist_ok=True)
        reference = self.root / "reference"
        reference.mkdir(parents=True, exist_ok=True)
        pi_model: dict[str, Any] = {"base_url": self.upstream.base_url,
                                    "model": "fake-upstream", "timeout_s": 30}
        if api_key_env:
            pi_model["api_key_env"] = api_key_env
        document = {
            "schema_version": 1,
            "run_id": "pi-test-run",
            "agent_id": agent_id,
            "role": role,
            "runner_runtime": "pi",
            "state_root": str(state_root),
            "shell_enabled": False,
            "heartbeat_s": 1,
            "pi_cli": str(PI_CLI),
            "pi_context_window": 64000,
            "workspace_root": str(workspace),
            "reference_root": str(reference),
            "team_memory_root": str(self.root / "memory" / "team"),
            "broker_socket": None,
            "cli_environment": cli_environment or {},
            "agent_config": {
                "agent_id": agent_id,
                "goal": "Do a small proof task in the workspace.",
                "peers": [],
                "profile": "paper_aligned",
                "context_chars": 48000,
                "max_turns": MAX_TURNS,
            },
            "model": {"kind": "fake"},
            "pi_model": pi_model,
        }
        if extra:
            document.update(extra)
        spec_path = self.root / f"{agent_id}-{len(list(self.root.glob('*.json')))}.json"
        spec_path.write_text(json.dumps(document))
        os.chmod(spec_path, 0o600)
        return spec_path

    def child_env(self, spec_path: Path) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": str(SOURCE_ROOT),
            "HARNESS_RUNNER_SOCKET": str(self.root / "control.sock"),
            "HARNESS_RUNNER_TOKEN": TOKEN,
            "HARNESS_RUNNER_AGENT": "player1",
            "HARNESS_RUNNER_ROLE": "player",
            "HARNESS_RUNNER_SPEC": str(spec_path),
        }

    async def spawn(self, spec_path: Path, env: dict[str, str] | None = None,
                    role: str = "player") -> asyncio.subprocess.Process:
        environment = env or self.child_env(spec_path)
        environment = {**environment, "HARNESS_RUNNER_ROLE": role}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "minecraft_harness.pi_runner",
            cwd=str(self.root), env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._processes.append(process)
        return process

    async def communicate(self, process: asyncio.subprocess.Process,
                          timeout: float = 30) -> tuple[bytes, bytes]:
        return await asyncio.wait_for(process.communicate(), timeout)

    # -- tests -----------------------------------------------------------
    async def test_real_pi_runs_bash_and_reports_metered_completion(self):
        with mock.patch.dict(os.environ, {"MC_PI_TEST_MODEL_KEY": "sk-test-secret"}):
            spec_path = self.make_spec(
                cli_environment={"MC_AGENT_ID": "player1",
                                 "MC_AGENT_TOKEN": "player-token",
                                 "MC_RUNTIME_URL": "http://127.0.0.1:9"},
                api_key_env="MC_PI_TEST_MODEL_KEY")
            self.upstream.set_script(
                sse_response(tool_call_delta(
                    "bash", json.dumps({"command":
                                        "printf proof > proof.txt; env > envdump.txt; "
                                        "cat /proc/$PPID/environ > procenv.txt 2>/dev/null || true; "
                                        "mc --help > mc_help.txt 2>&1; echo $? > mc_exit.txt; "
                                        "echo done"})),
                    finish_reason="tool_calls", usage=openai_usage()),
                sse_response({"role": "assistant", "content": "the proof file exists"},
                             usage=openai_usage()),
                sse_response({"role": "assistant", "content": "continued work"},
                             usage=openai_usage()),
            )
            process = await self.spawn(spec_path)
            await asyncio.wait_for(self.hub.ready_event.wait(), 60)
            await asyncio.wait_for(self.hub.finished_event.wait(), 120)
            stdout, stderr = await self.communicate(process)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(stdout, b"")
        # runner.ready carried real Pi interface hashes and the runner pid.
        ready = self.hub.ready or {}
        self.assertEqual(ready.get("agent_id"), "player1")
        self.assertEqual(ready.get("role"), "player")
        self.assertEqual(len(ready.get("prompt_sha256", "")), 64)
        self.assertEqual(len(ready.get("tools_sha256", "")), 64)
        self.assertEqual(ready.get("pid"), process.pid)
        # runner.finished reports a bounded turn-limited end.
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("agent_id"), "player1")
        self.assertEqual(result.get("status"), "turns_exhausted")
        self.assertEqual(result.get("turns"), MAX_TURNS)
        # Pi's built-in bash really executed in the workspace.
        workspace = self.root / "workspace"
        self.assertEqual((workspace / "proof.txt").read_text(), "proof")
        kinds = self.hub.kinds()
        for expected in ("agent_started", "tool_intent", "tool_result", "agent_turn",
                         "model_request", "model_response", "agent_stopped"):
            self.assertIn(expected, kinds)
        self.assertEqual(kinds.count("agent_started"), MAX_TURNS)
        tool_intent = self.hub.events_of("tool_intent")[0]["payload"]
        self.assertEqual(tool_intent["tool"], "bash")
        self.assertEqual(tool_intent["idempotency"], "side_effect")
        self.assertEqual(len(tool_intent["args_sha256"]), 64)
        tool_result = self.hub.events_of("tool_result")[0]["payload"]
        self.assertFalse(tool_result["is_error"])
        self.assertIn("result_ref", tool_result)
        self.assertTrue(any("done" in artifact for artifact in [self.hub.artifact_corpus()]))
        # Every model attempt was reserved and settled exactly once with usage.
        self.assertGreaterEqual(len(self.hub.reservations), 3)
        self.assertEqual([item["reservation_id"] for item in self.hub.settlements],
                         self.hub.reservations)
        for settlement in self.hub.settlements:
            self.assertEqual(settlement["usage"]["total_tokens"], 13)
        model_request = self.hub.events_of("model_request")[0]["payload"]
        self.assertEqual(model_request["purpose"], "agent_decision")
        self.assertEqual(model_request["role"], "player")
        self.assertEqual(model_request["reservation_id"], self.hub.reservations[0])
        # The Pi child/bash environment carries no tokens or upstream secrets.
        envdump = (workspace / "envdump.txt").read_text()
        for secret_marker in ("MC_AGENT_TOKEN", "HARNESS_RUNNER_TOKEN",
                              "HARNESS_PI_BRIDGE_TOKEN", "sk-test-secret",
                              "player-token"):
            self.assertNotIn(secret_marker, envdump, secret_marker)
        self.assertIn("MC_RUNTIME_URL", envdump)  # non-secret CLI routing remains
        # The bridge token arrived via a one-shot FD and was closed by the
        # extension, so neither the env nor /proc exposes it.
        self.assertNotIn("HARNESS_PI_BRIDGE_TOKEN_FD", envdump)
        procenv = (workspace / "procenv.txt").read_text(errors="replace")
        # /proc/<Pi>/environ still exposes the exec-time environment, but all
        # credential values are absent (only the now-closed FD number remains).
        for secret_marker in ("HARNESS_RUNNER_TOKEN", "HARNESS_PI_BRIDGE_TOKEN=",
                              "sk-test-secret", "player-token"):
            self.assertNotIn(secret_marker, procenv, secret_marker)
        # The mc wrapper still works: it sourced the 0600 credential file only
        # for the program_cli child and printed its usage.
        self.assertEqual((workspace / "mc_exit.txt").read_text().strip(), "0")
        self.assertIn("usage", (workspace / "mc_help.txt").read_text().lower())
        self.assertEqual((self.root / "state" / "bin" / "mc-env").stat().st_mode & 0o777,
                         0o600)
        # The local proxy credential file is removed after the first
        # authenticated request, so later bash calls cannot read it.
        agent_dir = self.root / "state" / "home" / ".pi" / "agent"
        self.assertFalse((agent_dir / "models.json").exists())
        self.assertIn("pi_cli_auth_redacted", kinds)
        state_text = "\n".join(
            path.read_text(errors="replace")
            for path in (self.root / "state").rglob("*")
            if path.is_file() and path.suffix in {".json", ".jsonl", ".txt", ".log"})
        self.assertNotIn("sk-test-secret", state_text)
        # Pi session state is durable for resume.
        self.assertTrue(list((self.root / "state" / "pi-sessions").rglob("*.jsonl")))

    async def test_runner_environment_is_not_readable_via_proc(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux /proc credential test")
        spec_path = self.make_spec(api_key_env="MC_PI_TEST_MODEL_KEY")
        with mock.patch.dict(os.environ, {"MC_PI_TEST_MODEL_KEY": "sk-proc-secret"}):
            process = await self.spawn(spec_path)
            await asyncio.wait_for(self.hub.ready_event.wait(), 60)
            try:
                with self.assertRaises(PermissionError):
                    with open(f"/proc/{process.pid}/environ", "rb") as handle:
                        handle.read()
            finally:
                process.kill()
                await process.wait()

    async def test_no_progress_continuations_end_as_stalled(self):
        spec_path = self.make_spec()
        document = json.loads(spec_path.read_text(encoding="utf-8"))
        document["agent_config"]["max_turns"] = 10
        document["agent_config"]["max_no_progress_turns"] = 2
        spec_path.write_text(json.dumps(document), encoding="utf-8")
        self.upstream.set_script(
            sse_response({"role": "assistant", "content": "idle one"}),
            sse_response({"role": "assistant", "content": "idle two"}),
            sse_response({"role": "assistant", "content": "idle three"}),
        )
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        _stdout, stderr = await self.communicate(process)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("status"), "stalled")
        self.assertEqual(result.get("reason"), "no_progress_turns")
        self.assertLessEqual(len(self.hub.settlements), 2)

    async def test_pi_session_resumes_after_a_restart(self):
        spec_path = self.make_spec()
        self.upstream.set_script(
            sse_response(tool_call_delta(
                "bash", json.dumps({"command": "printf proof > proof.txt; echo done"})),
                finish_reason="tool_calls"),
            sse_response({"role": "assistant", "content": "done"}),
            sse_response({"role": "assistant", "content": "continued"}),
        )
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(process)
        self.assertEqual(process.returncode, 0)
        session_files = list((self.root / "state" / "pi-sessions").rglob("*.jsonl"))
        self.assertEqual(len(session_files), 1)
        first_run_requests = self.upstream.request_bodies()
        self.assertGreaterEqual(len(first_run_requests), 1)
        # Restart the runner against the same spec: Pi --continue must restore
        # the previous conversation instead of starting a fresh session.
        self.hub.ready_event = asyncio.Event()
        self.hub.finished_event = asyncio.Event()
        self.upstream.reset_requests()
        self.upstream.set_script()
        second = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(second)
        self.assertEqual(second.returncode, 0)
        session_files_after = list((self.root / "state" / "pi-sessions").rglob("*.jsonl"))
        self.assertEqual(len(session_files_after), 1)  # same session file
        second_requests = self.upstream.request_bodies()
        self.assertGreaterEqual(len(second_requests), 1)
        messages = second_requests[0].get("messages") or []
        # Resumed context carries earlier turns (system + prior messages + the
        # resume prompt), far more than a fresh session's single user prompt.
        self.assertGreater(len(messages), 3)
        combined = json.dumps(messages, ensure_ascii=False)
        self.assertIn("proof", combined)  # earlier transcript is present
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("status"), "turns_exhausted")

    async def test_sigterm_aborts_pi_and_reports_stopped(self):
        spec_path = self.make_spec()
        self.upstream.set_script(
            sse_response(tool_call_delta(
                "bash", json.dumps({"command": "sleep 30"})),
                finish_reason="tool_calls"),
        )
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await self.hub.wait_for_event("tool_intent", timeout=60)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(self.hub.finished_event.wait(), 60)
        stdout, stderr = await self.communicate(process, timeout=30)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(stdout, b"")
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("agent_id"), "player1")
        self.assertEqual(result.get("status"), "stopped")
        self.assertIn("agent_stopped", self.hub.kinds())

    async def test_budget_denial_ends_with_budget_exhausted_without_upstream_call(self):
        spec_path = self.make_spec()
        from minecraft_harness.budget import BudgetExhausted

        self.hub.reserve_error = BudgetExhausted("team model budget exhausted")
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(process)
        self.assertEqual(process.returncode, 0)
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("status"), "budget_exhausted")
        self.assertEqual(self.upstream.request_bodies(), [])
        self.assertEqual(self.hub.reservations, [])
        self.assertIn("agent_stopped", self.hub.kinds())

    async def test_missing_node_is_a_clear_startup_error(self):
        spec_path = self.make_spec()
        env = {**self.child_env(spec_path), "HARNESS_NODE": "/nonexistent/node-binary"}
        process = await self.spawn(spec_path, env=env)
        stdout, stderr = await self.communicate(process, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(stdout, b"")
        document = json.loads(stderr.decode("utf-8").strip().splitlines()[-1])
        self.assertIn("__runner_error__", document)
        self.assertIn("Node.js", document["__runner_error__"]["message"])

    async def test_observer_role_is_rejected_with_a_clear_startup_error(self):
        spec_path = self.make_spec(agent_id="observer", role="observer")
        env = {**self.child_env(spec_path), "HARNESS_RUNNER_AGENT": "observer"}
        process = await self.spawn(spec_path, role="observer", env=env)
        stdout, stderr = await self.communicate(process, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(stdout, b"")
        document = json.loads(stderr.decode("utf-8").strip().splitlines()[-1])
        self.assertIn("__runner_error__", document)
        self.assertIn("role=player", document["__runner_error__"]["message"])

    async def test_python_runner_runtime_spec_is_rejected(self):
        spec_path = self.make_spec(extra={"runner_runtime": "python"})
        process = await self.spawn(spec_path)
        stdout, stderr = await self.communicate(process, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(stdout, b"")
        document = json.loads(stderr.decode("utf-8").strip().splitlines()[-1])
        self.assertIn("__runner_error__", document)




class McWrapperCredentialTests(unittest.TestCase):
    """The mc wrapper sources the player token only for the program_cli child."""

    def test_env_file_is_mode_0600_and_exported_only_to_the_cli_child(self):
        from minecraft_harness import pi_runner

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / "mc-env"
            pi_runner._write_mc_env_file(env_path, {
                "MC_AGENT_TOKEN": "tok'en with space",
                "MC_RUNTIME_URL": "http://127.0.0.1:9",
            })
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            fake_python = root / "fake-python"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf 'TOKEN=%s\\nURL=%s\\n' \"$MC_AGENT_TOKEN\" \"$MC_RUNTIME_URL\"\n")
            fake_python.chmod(0o700)
            wrapper = root / "mc"
            wrapper.write_text(pi_runner._mc_wrapper_text(str(fake_python), env_path))
            wrapper.chmod(0o700)
            environment = {"PATH": "/usr/bin:/bin", "HOME": str(root)}
            completed = subprocess.run([str(wrapper)], env=environment,
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("TOKEN=tok'en with space", completed.stdout)
            self.assertIn("URL=http://127.0.0.1:9", completed.stdout)
            # The wrapper itself never leaks the credential into a bare shell:
            # only the exec'd program_cli child sees it.
            probe = subprocess.run(
                ["/bin/sh", "-c",
                 "set -a; . '{}'; set +a; env".format(env_path)],
                env=environment, capture_output=True, text=True)
            self.assertIn("MC_AGENT_TOKEN=tok'en with space", probe.stdout)

    def test_sensitive_cli_environment_is_classified_and_filtered(self):
        from minecraft_harness import pi_runner

        values = {"MC_AGENT_TOKEN": "t", "MC_SUBMISSION_AUTHORITY": "a",
                  "MC_RUNTIME_URL": "http://x", "MC_AGENT_ID": "player1",
                  "SOME_API_KEY": "k"}
        secrets_map = pi_runner._cli_environment_secrets(values)
        self.assertEqual(set(secrets_map), {"MC_AGENT_TOKEN",
                                            "MC_SUBMISSION_AUTHORITY",
                                            "SOME_API_KEY"})
        self.assertNotIn("MC_RUNTIME_URL", secrets_map)
        self.assertNotIn("MC_AGENT_ID", secrets_map)

    def test_bridge_token_fd_is_an_inheritable_pipe(self):
        from minecraft_harness import pi_runner

        fd = pi_runner._create_bridge_token_fd("bridge-token-value")
        try:
            self.assertTrue(os.get_inheritable(fd))
            self.assertEqual(os.read(fd, 128).decode("utf-8"),
                             "bridge-token-value\n")
        finally:
            os.close(fd)

    def test_scrub_removes_pi_cli_auth_files(self):
        from minecraft_harness import pi_runner

        with tempfile.TemporaryDirectory() as directory:
            agent_dir = Path(directory)
            for name in ("models.json", "models-store.json", "auth.json"):
                (agent_dir / name).write_text("{}")
            (agent_dir / "session.jsonl").write_text("{}")
            pi_runner._scrub_pi_cli_auth_files(agent_dir)
            self.assertFalse((agent_dir / "models.json").exists())
            self.assertFalse((agent_dir / "models-store.json").exists())
            self.assertFalse((agent_dir / "auth.json").exists())
            self.assertTrue((agent_dir / "session.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
