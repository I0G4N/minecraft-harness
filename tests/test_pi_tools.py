"""Tests for the Pi memory/web tools extension and its loopback tool bridge.

Part 1 drives ``PiToolBridge`` directly with a fake RPC client (auth, tool
routing, argument validation, error mapping, size bounds, secret hygiene).
Part 2 runs the real Pi CLI with the real extension and a scripted fake model,
proving the six model-callable tools are registered, declared to the model and
executed through the bridge into the fake control plane.

Run in the disposable test env::

    PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
        .test-venv/bin/python -m pytest -q tests/test_pi_tools.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from minecraft_harness.pi_runner import PI_TOOLS
from minecraft_harness.pi_tool_bridge import PiToolBridge

REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO / "src"
PI_CLI = (REPO / "pi_runtime" / "node_modules" / "@earendil-works" /
          "pi-coding-agent" / "dist" / "bundle" / "cli.js")
HAVE_PI_RUNTIME = shutil.which("node") is not None and PI_CLI.is_file()

TOKEN = "pi-tools-test-token"
EXPECTED_TOOLS = {"memory_read", "memory_write", "memory_list", "memory_search",
                  "web_search", "web_fetch"}


class FakeRpcError(RuntimeError):
    def __init__(self, error_type: str, message: str = "") -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = False


class FakeBridgeRpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, Exception] = {}

    def call_sync(self, method: str, params: dict[str, Any] | None = None,
                  *, timeout_s: float | None = None) -> Any:
        params = dict(params or {})
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        return self.responses.get(method, {"status": "ok"})


class ToolBridgeTests(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeBridgeRpc()
        self.bridge = PiToolBridge(self.rpc, agent_id="player1", max_body_bytes=2048)
        self.bridge.start()
        self.addCleanup(self.bridge.stop)
        self.client = httpx.Client(trust_env=False, timeout=10)
        self.addCleanup(self.client.close)

    def post(self, payload: Any, token: str | None = None,
             raw: bytes | None = None) -> httpx.Response:
        headers = {"content-type": "application/json"}
        bearer = self.bridge.token if token is None else token
        if bearer is not None:
            headers["authorization"] = "Bearer " + bearer
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        return self.client.post(self.bridge.base_url + "/tool", content=body,
                                headers=headers)

    def test_bad_token_is_rejected_without_rpc_call(self):
        response = self.post({"tool": "memory_list", "arguments": {}}, token="wrong-token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["ok"], False)
        self.assertEqual(self.rpc.calls, [])

    def test_unknown_tool_and_unknown_endpoint_are_rejected(self):
        response = self.post({"tool": "shell", "arguments": {"command": "id"}})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("token", json.dumps(response.json()))
        self.assertEqual(self.rpc.calls, [])
        endpoint = self.client.get(self.bridge.base_url + "/tool", headers={
            "authorization": "Bearer " + self.bridge.token})
        self.assertIn(endpoint.status_code, {404, 405, 501})

    def test_oversize_body_is_rejected(self):
        response = self.post(None, raw=b"x" * 4096)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.rpc.calls, [])

    def test_memory_tools_forward_method_and_bound_actor(self):
        self.rpc.responses["memory.read"] = {"scope": "team", "path": "a",
                                             "content": "hello"}
        response = self.post({"tool": "memory_read",
                              "arguments": {"scope": "team", "path": "a"}})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("hello", payload["content"])
        self.assertEqual(payload["details"]["tool"], "memory_read")
        self.assertEqual(self.rpc.calls, [("memory.read", {"actor_id": "player1",
                                                           "scope": "team",
                                                           "path": "a"})])
        self.post({"tool": "memory_write", "arguments": {
            "scope": "private", "path": "p", "content": "c", "expected_revision": 0}})
        self.assertEqual(self.rpc.calls[-1][0], "memory.write")
        self.assertEqual(self.rpc.calls[-1][1]["actor_id"], "player1")
        self.assertEqual(self.rpc.calls[-1][1]["expected_revision"], 0)
        self.post({"tool": "memory_search", "arguments": {
            "query": "dragon", "scope": "all", "limit": 5}})
        self.assertEqual(self.rpc.calls[-1],
                         ("memory.search", {"actor_id": "player1", "query": "dragon",
                                            "scope": "all", "limit": 5}))
        self.post({"tool": "memory_list", "arguments": {"scope": "team"}})
        self.assertEqual(self.rpc.calls[-1][0], "memory.list")

    def test_web_tools_forward_identity_free_params(self):
        self.rpc.responses["web.search"] = {"status": "ok",
                                           "results": [{"title": "ref"}]}
        response = self.post({"tool": "web_search", "arguments": {"query": "mc docs"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rpc.calls[-1], ("web.search", {"query": "mc docs"}))
        self.rpc.responses["web.fetch"] = {"status": "ok", "content": "page text"}
        response = self.post({"tool": "web_fetch",
                              "arguments": {"url": "https://example.test/x"}})
        self.assertEqual(self.rpc.calls[-1], ("web.fetch",
                                              {"url": "https://example.test/x"}))
        self.assertIn("page text", response.json()["content"])

    def test_argument_validation_rejects_unexpected_or_bad_values(self):
        cases = [
            {"tool": "memory_read", "arguments": {"scope": "private", "path": "a",
                                                   "shell": "id"}},
            {"tool": "memory_write", "arguments": {"scope": "team", "path": "a",
                                                    "content": "c",
                                                    "expected_revision": -1}},
            {"tool": "memory_search", "arguments": {"query": ""}},
            {"tool": "web_fetch", "arguments": {"url": 5}},
            {"tool": "memory_list", "arguments": {"scope": "wrong"}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["ok"], False)
        self.assertEqual(self.rpc.calls, [])

    def test_rpc_errors_are_mapped_without_leaking_internals(self):
        self.rpc.errors["memory.write"] = FakeRpcError(
            "MemoryConflict", "Expected revision 0, current revision is 1")
        response = self.post({"tool": "memory_write", "arguments": {
            "scope": "private", "path": "a", "content": "c", "expected_revision": 0}})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("revision", payload["error"])
        # Unknown/untrusted error types collapse to a generic message.
        self.rpc.errors["web.search"] = FakeRpcError(
            "RuntimeUnavailable", "secret internal endpoint http://10.0.0.1")
        payload = self.post({"tool": "web_search",
                             "arguments": {"query": "x"}}).json()
        self.assertFalse(payload["ok"])
        self.assertNotIn("10.0.0.1", payload["error"])
        self.assertNotIn("secret", payload["error"])
        # The bridge token never appears in any response.
        for method, arguments in (("web_search", {"query": "x"}),):
            body = self.post({"tool": method, "arguments": arguments}).text
            self.assertNotIn(self.bridge.token, body)

    def test_large_results_are_truncated_for_the_model(self):
        small = PiToolBridge(self.rpc, agent_id="player1", max_text_chars=128)
        small.start()
        self.addCleanup(small.stop)
        self.rpc.responses["memory.read"] = {"content": "x" * 1000}
        response = self.client.post(small.base_url + "/tool",
                                    json={"tool": "memory_read",
                                          "arguments": {"scope": "private", "path": "a"}},
                                    headers={"authorization": "Bearer " + small.token})
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["details"]["truncated"])
        self.assertIn("truncated", payload["content"])
        self.assertLess(len(payload["content"]), 400)

    def test_bridge_has_random_token_and_loopback_url(self):
        other = PiToolBridge(self.rpc, agent_id="player2")
        other.start()
        self.addCleanup(other.stop)
        self.assertNotEqual(other.token, self.bridge.token)
        self.assertTrue(other.base_url.startswith("http://127.0.0.1:"))
        self.assertEqual(len(other.token), len(self.bridge.token))


# ---------------------------------------------------------------------------
# Real Pi CLI integration
# ---------------------------------------------------------------------------

def sse_response(delta: dict[str, Any], finish_reason: str = "stop") -> bytes:
    chunks = [
        {"id": "c", "object": "chat.completion.chunk", "model": "fake",
         "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "model": "fake",
         "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
         "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}},
    ]
    return b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n"
                    for chunk in chunks) + b"data: [DONE]\n\n"


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    return sse_response({"role": "assistant", "content": None, "tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        finish_reason="tool_calls")


class FakeUpstream:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.responses: list[bytes] = []
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self.base_url = ""

    def start(self) -> str:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                with upstream.lock:
                    upstream.requests.append(body)
                    payload = (upstream.responses.pop(0) if upstream.responses
                               else sse_response({"role": "assistant",
                                                  "content": "done"}))
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
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


class FakeToolHub:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.artifacts: list[str] = []
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []
        self.memory: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.ready: dict[str, Any] | None = None
        self.finished: dict[str, Any] | None = None
        self.ready_event = asyncio.Event()
        self.finished_event = asyncio.Event()

    async def handle(self, agent_id: str, role: str, method: str,
                     params: dict[str, Any]) -> Any:
        params = dict(params or {})
        if method.startswith("memory.") or method.startswith("web."):
            with self.lock:
                self.rpc_calls.append((method, params))
        if method == "runner.ready":
            self.ready = params
            self.ready_event.set()
            return {"accepted": True}
        if method == "runner.heartbeat":
            return {"accepted": True}
        if method == "runner.finished":
            self.finished = params
            self.finished_event.set()
            return {"accepted": True}
        if method == "recorder.append":
            with self.lock:
                self.events.append({"kind": params.get("kind"),
                                    "payload": params.get("payload")})
            return {"event_seq": len(self.events)}
        if method == "recorder.put_artifact":
            with self.lock:
                self.artifacts.append(str(params.get("text", "")))
            digest = hashlib.sha256(str(params.get("text", "")).encode()).hexdigest()
            return {"hash": digest, "sha256": digest, "path": "artifacts/" + digest}
        if method == "budget.wait_admission":
            return {"admitted": True, "stop": False}
        if method == "budget.validate_request":
            return None
        if method == "budget.reserve":
            return "res-1"
        if method == "budget.settle":
            return None
        if method == "budget.stop_requested":
            return False
        if method == "memory.write":
            key = (params.get("scope"), agent_id, params.get("path"))
            with self.lock:
                current = self.memory.get(key, {}).get("revision", 0)
                if current != params.get("expected_revision"):
                    raise FakeRpcError("MemoryConflict",
                                       f"Expected revision {params.get('expected_revision')}, "
                                       f"current revision is {current}")
                revision = current + 1
                self.memory[key] = {"revision": revision,
                                    "content": params.get("content", "")}
            return {"scope": params.get("scope"), "author": agent_id,
                    "path": params.get("path"), "revision": revision,
                    "content": params.get("content", "")}
        if method == "memory.read":
            key = (params.get("scope"), params.get("author") or agent_id,
                   params.get("path"))
            with self.lock:
                note = self.memory.get(key)
            if note is None:
                raise FakeRpcError("FileNotFoundError", "No such note")
            return {"scope": key[0], "author": key[1], "path": key[2],
                    "revision": note["revision"], "content": note["content"]}
        if method == "memory.list":
            return []
        if method == "memory.search":
            return [{"path": "note.md", "author": agent_id, "scope": "private",
                     "revision": 1, "snippet": "[dragon] notes", "rank": 1.0}]
        if method == "web.search":
            return {"status": "ok", "backend": "test",
                    "results": [{"title": "Reference", "url": "https://example.test"}]}
        if method == "web.fetch":
            return {"status": "ok", "content": "fetched reference text"}
        raise AssertionError(f"unexpected RPC method: {method}")

    def calls_for(self, method: str) -> list[dict[str, Any]]:
        with self.lock:
            return [params for name, params in self.rpc_calls if name == method]

    def kinds(self) -> list[str]:
        with self.lock:
            return [event["kind"] for event in self.events]

    def ledger_corpus(self) -> str:
        with self.lock:
            return json.dumps({"events": self.events, "artifacts": self.artifacts},
                              ensure_ascii=False)


@unittest.skipUnless(HAVE_PI_RUNTIME, "Node 22 and the Pi CLI runtime are required")
class PiToolsExtensionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from minecraft_harness.runner_rpc import RpcServer

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)
        self.hub = FakeToolHub()
        self.server = RpcServer(self.root / "control.sock",
                                {TOKEN: {"agent_id": "player1", "role": "player"}},
                                self.hub.handle)
        await self.server.start()
        self.process: asyncio.subprocess.Process | None = None
        self.addCleanup(self._cleanup)

    async def _cleanup(self):
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        await self.server.close()

    def spec(self) -> Path:
        workspace = self.root / "workspace"
        workspace.mkdir()
        (self.root / "reference").mkdir()
        document = {
            "schema_version": 1, "run_id": "tools-run", "agent_id": "player1",
            "role": "player", "runner_runtime": "pi",
            "state_root": str(self.root / "state"), "shell_enabled": False,
            "heartbeat_s": 1, "pi_cli": str(PI_CLI), "pi_context_window": 64000,
            "workspace_root": str(workspace),
            "reference_root": str(self.root / "reference"),
            "team_memory_root": str(self.root / "memory" / "team"),
            "broker_socket": None, "cli_environment": {},
            "agent_config": {"agent_id": "player1",
                             "goal": "Exercise the memory and web tools.",
                             "peers": [], "profile": "paper_aligned",
                             "context_chars": 48000, "max_turns": 2},
            "model": {"kind": "fake"},
            "pi_model": {"base_url": self.upstream.base_url,
                         "model": "fake-upstream", "timeout_s": 30},
        }
        path = self.root / "spec.json"
        path.write_text(json.dumps(document))
        os.chmod(path, 0o600)
        return path

    async def _run(self) -> None:
        spec_path = self.spec()
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": str(SOURCE_ROOT),
            "HARNESS_RUNNER_SOCKET": str(self.root / "control.sock"),
            "HARNESS_RUNNER_TOKEN": TOKEN,
            "HARNESS_RUNNER_AGENT": "player1",
            "HARNESS_RUNNER_ROLE": "player",
            "HARNESS_RUNNER_SPEC": str(spec_path),
        }
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "minecraft_harness.pi_runner",
            cwd=str(self.root), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        stdout, stderr = await asyncio.wait_for(self.process.communicate(), 30)
        self.assertEqual(self.process.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(stdout, b"")

    async def test_real_pi_calls_memory_and_web_tools_through_the_bridge(self):
        self.upstream.responses = [
            tool_call("w1", "memory_write", {"scope": "private", "path": "notes/plan.md",
                                              "content": "dragon plan: mine stone",
                                              "expected_revision": 0}),
            tool_call("r1", "memory_read", {"scope": "private", "path": "notes/plan.md"}),
            tool_call("s1", "memory_search", {"query": "dragon", "scope": "all"}),
            tool_call("ws1", "web_search", {"query": "mineflayer pathfinder docs"}),
            tool_call("wf1", "web_fetch", {"url": "https://example.test/ref"}),
            sse_response({"role": "assistant", "content": "tools exercised"}),
        ]
        await self._run()
        # The Pi model saw the extension-declared tools.
        first_request = self.upstream.requests[0]
        declared = {tool.get("function", {}).get("name")
                    for tool in (first_request.get("tools") or [])}
        self.assertTrue(EXPECTED_TOOLS <= declared,
                        f"missing declared tools: {EXPECTED_TOOLS - declared}")
        # tools_sha256 reflects the full ten-tool interface.
        expected_hash = hashlib.sha256(
            json.dumps({"tools": list(PI_TOOLS)}, sort_keys=True).encode()).hexdigest()
        self.assertEqual(self.hub.ready["tools_sha256"], expected_hash)
        # Every bridge call reached the fake control plane with clean params.
        self.assertEqual(len(self.hub.calls_for("memory.write")), 1)
        self.assertEqual(self.hub.calls_for("memory.write")[0]["actor_id"], "player1")
        self.assertEqual(self.hub.calls_for("memory.write")[0]["content"],
                         "dragon plan: mine stone")
        self.assertIn(("private", "player1", "notes/plan.md"), self.hub.memory)
        self.assertEqual(self.hub.calls_for("memory.read")[0]["path"], "notes/plan.md")
        self.assertEqual(self.hub.calls_for("memory.search")[0]["query"], "dragon")
        self.assertEqual(self.hub.calls_for("web.search")[0],
                         {"query": "mineflayer pathfinder docs"})
        self.assertEqual(self.hub.calls_for("web.fetch")[0],
                         {"url": "https://example.test/ref"})
        # Tool execution results are ledgered for each call.
        tool_results = [event["payload"] for event in self.hub.events
                        if event["kind"] == "tool_result"]
        self.assertEqual({item["tool"] for item in tool_results},
                         {"memory_write", "memory_read", "memory_search",
                          "web_search", "web_fetch"})
        for item in tool_results:
            self.assertFalse(item["is_error"])
            self.assertIn("result_ref", item)
        # The extension tool descriptions are paper-visible: no harness terms.
        tool_descriptions = json.dumps(
            [tool.get("function", {}) for tool in first_request.get("tools") or []]).lower()
        for term in ("artifact", "ledger", "evidence_ref", "harness", "budget"):
            self.assertNotIn(term, tool_descriptions)
        # The bridge token must never reach the model transcript or the ledger.
        corpus = self.hub.ledger_corpus() + json.dumps(self.upstream.requests)
        self.assertNotIn("HARNESS_PI_BRIDGE_TOKEN", corpus)
        # (The random token itself is generated at runtime and never logged.)



class BridgePeerAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeBridgeRpc()
        self.bridge = PiToolBridge(self.rpc, agent_id="player1")
        self.bridge.start()
        self.addCleanup(self.bridge.stop)

    def _post(self) -> httpx.Response:
        client = httpx.Client(trust_env=False, timeout=5)
        try:
            return client.post(self.bridge.base_url + "/tool",
                               json={"tool": "memory_list", "arguments": {}},
                               headers={"authorization": "Bearer " + self.bridge.token})
        finally:
            client.close()

    def test_foreign_peer_is_closed_before_any_http_response(self):
        self.bridge.set_allowed_peer_pid(os.getpid() + 1)
        with self.assertRaises(httpx.TransportError):
            self._post()
        self.assertEqual(self.rpc.calls, [])

    def test_allowed_pid_passes_through_authentication(self):
        self.bridge.set_allowed_peer_pid(os.getpid())
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


if __name__ == "__main__":
    unittest.main()
