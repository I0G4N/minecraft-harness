"""Tests for the Pi Observer runner, bridge and Observer tools extension.

The bridge tests are pure unit tests.  The runtime tests launch
``python -m minecraft_harness.pi_observer_runner`` as a subprocess and drive it
with:

* a scripted loopback OpenAI-compatible upstream (fake model);
* a loopback runner RPC server (fake ServiceHub) implementing the Observer RPC
  contract and the runner control methods;
* the real installed Pi CLI and the Observer tools extension.

They prove the Observer prompt/tool surface, the advisory report flow, ledger
events, ready/finished/heartbeat, stop and session resume, and credential
hygiene.  No Docker, no experiment and no external network are involved; the
runtime tests skip if Node or the Pi CLI is absent.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO / "src"
PI_CLI = (REPO / "pi_runtime" / "node_modules" / "@earendil-works" /
          "pi-coding-agent" / "dist" / "bundle" / "cli.js")
HAVE_PI_RUNTIME = shutil.which("node") is not None and PI_CLI.is_file()

TOKEN = "pi-observer-test-token"
PI_TOOLS = {"read", "write", "records_read", "evidence_read",
            "observer_report", "infra_issue"}
FORBIDDEN_TOOLS = {"bash", "edit", "mc_execute", "memory_read", "memory_write",
                   "memory_list", "memory_search", "web_search", "web_fetch"}


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

    def __init__(self, delay_s: float = 0.0) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.responses: list[bytes] = []
        self.delay_s = delay_s
        self.default = sse_response({"role": "assistant", "content": "reviewed"},
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
                    upstream.requests.append({"body": body,
                                              "headers": dict(self.headers)})
                    payload = (upstream.responses.pop(0) if upstream.responses
                               else upstream.default)
                if upstream.delay_s:
                    time.sleep(upstream.delay_s)
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


class FakeObserverHub:
    """Minimal ServiceHub/RunnerManager RPC surface for one Pi Observer."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.artifacts: list[str] = []
        self.reservations: list[str] = []
        self.settlements: list[dict[str, Any]] = []
        self.ready: dict[str, Any] | None = None
        self.finished: dict[str, Any] | None = None
        self.heartbeats: list[dict[str, Any]] = []
        self.reports: list[dict[str, Any]] = []
        self.infra_issues: list[dict[str, Any]] = []
        self.records_calls: list[dict[str, Any]] = []
        self.evidence_calls: list[dict[str, Any]] = []
        # Trusted control-plane artifact budget (P1-5): the Observer runner must
        # account artifacts here, not only in its model-writable /state marker.
        self.pi_budget = {"used_bytes": 0, "metadata_bytes": 0,
                          "retained_count": 0, "retention_recorded": 0,
                          "pressure_recorded": 0}
        self.pi_budget_limit = 5_000_000
        self.pi_budget_calls: list[str] = []
        self.trigger_event: dict[str, Any] | None = None
        self.ready_event = asyncio.Event()
        self.finished_event = asyncio.Event()
        self.event_signal = asyncio.Event()
        self.stop_requested = False

    async def handle(self, agent_id: str, role: str, method: str,
                     params: dict[str, Any]) -> Any:
        params = params if isinstance(params, dict) else {}
        if method == "runner.ready":
            if role != "observer":
                raise PermissionError("observer runner identity required")
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
        if method == "recorder.read_events":
            with self.lock:
                trigger = self.trigger_event
                self.trigger_event = None
            return [trigger] if trigger is not None else []
        if method == "recorder.latest_event_seq":
            with self.lock:
                return len(self.events)
        if method == "budget.wait_admission":
            return {"admitted": True, "stop": False}
        if method == "budget.validate_request":
            return None
        if method == "budget.reserve":
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
        if method == "pi.budget.snapshot":
            with self.lock:
                self.pi_budget_calls.append(method)
            row = dict(self.pi_budget)
            limit = self.pi_budget_limit
            return {"self": {**row, "limit_bytes": limit,
                             "global_used_bytes": row["used_bytes"],
                             "global_limit_bytes": limit},
                    "agents": [{"agent_id": agent_id, **row}],
                    "limits": {"per_agent_bytes": limit, "global_bytes": limit},
                    "global": {"used_bytes": row["used_bytes"],
                               "limit_bytes": limit}}
        if method == "pi.budget.take":
            size = int(params.get("bytes", 0))
            with self.lock:
                self.pi_budget_calls.append(method)
                row = self.pi_budget
                allowed = row["used_bytes"] + size <= self.pi_budget_limit
                response: dict[str, Any] = {"allowed": allowed,
                                            "limit_bytes": self.pi_budget_limit,
                                            "global_limit_bytes": self.pi_budget_limit}
                if allowed:
                    row["used_bytes"] += size
                else:
                    row["retained_count"] += 1
                    if not row["retention_recorded"]:
                        row["retention_recorded"] = 1
                        response["retention"] = {
                            "event": "pi_artifact_retention",
                            "reason": "pi_artifact_max_bytes",
                            "limit_bytes": self.pi_budget_limit,
                            "used_bytes": row["used_bytes"],
                            "retained_count": row["retained_count"]}
                response.update({"used_bytes": row["used_bytes"],
                                 "metadata_bytes": row["metadata_bytes"],
                                 "retained_count": row["retained_count"],
                                 "retention_recorded": bool(row["retention_recorded"]),
                                 "pressure_recorded": bool(row["pressure_recorded"]),
                                 "global_used_bytes": row["used_bytes"]})
                return response
        if method == "pi.budget.charge_metadata":
            size = int(params.get("bytes", 0))
            with self.lock:
                self.pi_budget_calls.append(method)
                row = self.pi_budget
                row["used_bytes"] += size
                row["metadata_bytes"] += size
                return {"used_bytes": row["used_bytes"],
                        "metadata_bytes": row["metadata_bytes"],
                        "retained_count": row["retained_count"],
                        "limit_bytes": self.pi_budget_limit,
                        "global_used_bytes": row["used_bytes"],
                        "global_limit_bytes": self.pi_budget_limit}
        if method == "observer.records_read":
            with self.lock:
                self.records_calls.append(dict(params))
            return {"events": [{"event_seq": 1, "kind": "success_candidate",
                                "actor_id": "supervisor", "payload": {}}],
                    "next_cursor": 1, "latest_event_seq": 1, "truncated": False}
        if method == "observer.evidence_read":
            with self.lock:
                self.evidence_calls.append(dict(params))
            digest = str(params.get("hash"))
            return {"hash": digest, "content": "evidence-body",
                    "offset": int(params.get("offset", 0)), "next_offset": None,
                    "offset_unit": "utf8_bytes"}
        if method == "observer.report":
            payload = dict(params)
            payload.setdefault("advisory", True)
            with self.lock:
                self.reports.append(payload)
                self.events.append({"kind": "observer_report", "payload": payload,
                                    "actor_id": agent_id})
            return {"status": "recorded", "event_seq": 1, "advisory": True,
                    "progress_summary_present": bool(payload.get("progress_summary"))}
        if method == "supervisor.observer_infra_issue":
            issue = params.get("issue") if isinstance(params.get("issue"), dict) else params
            with self.lock:
                self.infra_issues.append(dict(issue))
            return {"status": "reported", "reason": "noted"}
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


# ---------------------------------------------------------------------------
# Bridge unit tests
# ---------------------------------------------------------------------------

class FakeRpc:
    def __init__(self, *, role: str = "observer", agent_id: str = "observer",
                 response: Any = None) -> None:
        self.role = role
        self.agent_id = agent_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response = {"status": "recorded", "event_seq": 7, "advisory": True,
                         "progress_summary_present": True} if response is None else response

    def call_sync(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, dict(params)))
        if method == "pi.budget.snapshot":
            with self.lock:
                self.pi_budget_calls.append(method)
            row = dict(self.pi_budget)
            limit = self.pi_budget_limit
            return {"self": {**row, "limit_bytes": limit,
                             "global_used_bytes": row["used_bytes"],
                             "global_limit_bytes": limit},
                    "agents": [{"agent_id": agent_id, **row}],
                    "limits": {"per_agent_bytes": limit, "global_bytes": limit},
                    "global": {"used_bytes": row["used_bytes"],
                               "limit_bytes": limit}}
        if method == "pi.budget.take":
            size = int(params.get("bytes", 0))
            with self.lock:
                self.pi_budget_calls.append(method)
                row = self.pi_budget
                allowed = row["used_bytes"] + size <= self.pi_budget_limit
                response: dict[str, Any] = {"allowed": allowed,
                                            "limit_bytes": self.pi_budget_limit,
                                            "global_limit_bytes": self.pi_budget_limit}
                if allowed:
                    row["used_bytes"] += size
                else:
                    row["retained_count"] += 1
                    if not row["retention_recorded"]:
                        row["retention_recorded"] = 1
                        response["retention"] = {
                            "event": "pi_artifact_retention",
                            "reason": "pi_artifact_max_bytes",
                            "limit_bytes": self.pi_budget_limit,
                            "used_bytes": row["used_bytes"],
                            "retained_count": row["retained_count"]}
                response.update({"used_bytes": row["used_bytes"],
                                 "metadata_bytes": row["metadata_bytes"],
                                 "retained_count": row["retained_count"],
                                 "retention_recorded": bool(row["retention_recorded"]),
                                 "pressure_recorded": bool(row["pressure_recorded"]),
                                 "global_used_bytes": row["used_bytes"]})
                return response
        if method == "pi.budget.charge_metadata":
            size = int(params.get("bytes", 0))
            with self.lock:
                self.pi_budget_calls.append(method)
                row = self.pi_budget
                row["used_bytes"] += size
                row["metadata_bytes"] += size
                return {"used_bytes": row["used_bytes"],
                        "metadata_bytes": row["metadata_bytes"],
                        "retained_count": row["retained_count"],
                        "limit_bytes": self.pi_budget_limit,
                        "global_used_bytes": row["used_bytes"],
                        "global_limit_bytes": self.pi_budget_limit}
        if method == "observer.records_read":
            return {"events": [{"event_seq": 1, "kind": "chat"}],
                    "next_cursor": 1, "latest_event_seq": 3, "truncated": False}
        if method == "observer.evidence_read":
            return {"hash": params.get("hash"), "content": "abc", "offset": 0,
                    "next_offset": None, "offset_unit": "utf8_bytes"}
        return self.response


class PiObserverBridgeTests(unittest.TestCase):
    def setUp(self):
        from minecraft_harness.pi_observer_bridge import PiObserverBridge

        self.rpc = FakeRpc()
        self.bridge = PiObserverBridge(self.rpc, max_text_chars=64 * 1024)
        self.url = self.bridge.start()
        self.addCleanup(self.bridge.stop)

    def post(self, tool: Any, arguments: Any, token: str | None = None) -> tuple[int, dict[str, Any]]:
        body = json.dumps({"tool": tool, "arguments": arguments}).encode("utf-8")
        request = urllib.request.Request(
            self.url + "/tool", data=body, method="POST",
            headers={"content-type": "application/json",
                     "authorization": "Bearer " + (self.bridge.token if token is None else token)})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_bridge_requires_the_observer_identity(self):
        from minecraft_harness.pi_observer_bridge import PiObserverBridge
        with self.assertRaises(ValueError):
            PiObserverBridge(FakeRpc(role="player"))
        with self.assertRaises(ValueError):
            PiObserverBridge(FakeRpc(agent_id="player1"))

    def test_authentication_and_unknown_tools(self):
        self.assertEqual(self.post("records_read", {})[0], 200)
        status, payload = self.post("records_read", {}, token="wrong")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        status, payload = self.post("bash", {})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_records_read_forwards_bounded_pagination(self):
        status, payload = self.post("records_read", {"after": 4, "limit": 10})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.rpc.calls[-1],
                         ("observer.records_read", {"after": 4, "limit": 10}))
        self.assertEqual(payload["details"]["next_cursor"], 1)
        self.assertEqual(payload["details"]["latest_event_seq"], 3)
        self.assertEqual(payload["details"]["count"], 1)
        self.assertIn("event_seq", payload["content"])

    def test_records_read_rejects_bad_bounds(self):
        for arguments in ({"after": -1}, {"limit": 0}, {"limit": 101},
                          {"after": True}, {"unexpected": 1}):
            with self.subTest(arguments=arguments):
                status, payload = self.post("records_read", arguments)
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])

    def test_evidence_read_validates_hash_and_forwards(self):
        digest = "a" * 64
        status, payload = self.post("evidence_read",
                                    {"hash": digest, "offset": 3, "limit": 50})
        self.assertEqual(status, 200)
        self.assertEqual(self.rpc.calls[-1],
                         ("observer.evidence_read",
                          {"hash": digest, "offset": 3, "limit": 50}))
        self.assertEqual(payload["details"]["hash"], digest)
        for bad in ({"hash": "XYZ"}, {"hash": "a" * 63},
                    {"hash": digest, "offset": -1},
                    {"hash": digest, "limit": 32001},
                    {"hash": digest, "offset": True}):
            with self.subTest(arguments=bad):
                status, payload = self.post("evidence_read", bad)
                self.assertEqual(status, 400)

    def test_observer_report_validation_and_forwarding(self):
        reference = "b" * 64
        arguments = {
            "progress_summary": "Players reached the Nether.",
            "verified_events": [{"claim": "portal lit", "evidence_refs": [1, reference]}],
            "infrastructure_findings": ["no broken tool observed"],
            "findings": [{"status": "verified", "claim": "portal lit",
                          "evidence_refs": [{"hash": reference}]}],
        }
        status, payload = self.post("observer_report", arguments)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        method, forwarded = self.rpc.calls[-1]
        self.assertEqual(method, "observer.report")
        self.assertEqual(forwarded, arguments)
        self.assertEqual(payload["details"]["event_seq"], 7)

    def test_observer_report_rejects_invalid_shapes(self):
        good_ref = [1]
        cases = [
            {"progress_summary": "x"},  # findings required
            {"findings": [{"status": "bogus", "claim": "x", "evidence_refs": good_ref}]},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": []}]},
            {"findings": [{"status": "verified", "claim": "x",
                           "evidence_refs": ["NOT-A-HASH"]}]},
            {"findings": [{"status": "verified", "claim": "x",
                           "evidence_refs": [{"event_seq": 1}]}]},
            {"findings": [{"status": "verified", "claim": "", "evidence_refs": good_ref}]},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": good_ref}],
             "verified_events": [{"claim": "x"}]},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": good_ref}],
             "infrastructure_findings": ["x" * 3000]},
        ]
        for arguments in cases:
            with self.subTest(arguments=list(arguments)):
                status, payload = self.post("observer_report", arguments)
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])

    def test_infra_issue_validation_and_forwarding(self):
        arguments = {"kind": "runtime_recovery",
                     "reason": "worker disappeared",
                     "evidence_refs": [2, "c" * 64]}
        status, payload = self.post("infra_issue", arguments)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        method, forwarded = self.rpc.calls[-1]
        self.assertEqual(method, "supervisor.observer_infra_issue")
        self.assertEqual(forwarded["kind"], "runtime_recovery")
        self.assertEqual(forwarded["reason"], "worker disappeared")
        self.assertEqual(forwarded["evidence_refs"], [2, "c" * 64])
        self.assertRegex(forwarded["issue_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(payload["details"]["issue_id"], forwarded["issue_id"])
        for bad in ({"kind": "game_strategy", "reason": "x", "evidence_refs": [1]},
                    {"kind": "reconnect", "reason": "", "evidence_refs": [1]},
                    {"kind": "reconnect", "reason": "x", "evidence_refs": []},
                    {"kind": "reconnect", "reason": "x", "evidence_refs": [-1]},
                    {"kind": "reconnect", "reason": "x", "evidence_refs": [1],
                     "issue_id": "model-supplied"}):
            with self.subTest(arguments=bad):
                status, payload = self.post("infra_issue", bad)
                self.assertEqual(status, 400)

    def test_infra_issue_accepts_the_full_paper_kind_set(self):
        from minecraft_harness.observer.agent import ObserverAgent
        from minecraft_harness.pi_observer_bridge import INFRA_ISSUE_KINDS

        self.assertEqual(INFRA_ISSUE_KINDS, ObserverAgent.ISSUE_KINDS)
        for kind in sorted(INFRA_ISSUE_KINDS):
            with self.subTest(kind=kind):
                status, payload = self.post("infra_issue", {
                    "kind": kind, "reason": "environment failure observed",
                    "evidence_refs": [1]})
                self.assertEqual(status, 200)
                self.assertEqual(self.rpc.calls[-1][1]["kind"], kind)

    def test_each_infra_issue_call_gets_a_unique_issue_id(self):
        arguments = {"kind": "broken_tool", "reason": "read tool fails",
                     "evidence_refs": [3]}
        self.post("infra_issue", arguments)
        self.post("infra_issue", arguments)
        first = self.rpc.calls[-2][1]["issue_id"]
        second = self.rpc.calls[-1][1]["issue_id"]
        self.assertRegex(first, r"^[a-f0-9]{32}$")
        self.assertRegex(second, r"^[a-f0-9]{32}$")
        self.assertNotEqual(first, second)

    def test_bridge_errors_do_not_leak_internal_paths_or_rpc_details(self):
        from minecraft_harness.runner_rpc import RpcError

        class RaisingRpc(FakeRpc):
            def __init__(self, error):
                super().__init__()
                self.error = error

            def call_sync(self, method, params):
                self.calls.append((method, dict(params)))
                raise self.error

        from minecraft_harness.pi_observer_bridge import PiObserverBridge

        cases = [
            RpcError("RunnerControlError",
                     "internal path /srv/run/ledger/events.sqlite3 unreadable"),
            FileNotFoundError("/state/artifacts/deadbeef"),
            ValueError("state path /srv/run/artifacts/abc is missing"),
            RpcError("ConnectionError", "control plane /run/mc-runner/control.sock gone"),
            RuntimeError("Traceback: /srv/run/ledger/events.sqlite3"),
            PermissionError("cannot read /state/artifacts/deadbeef"),
            RpcError("ValueError", "api_key sk-secret-value rejected"),
        ]
        for error in cases:
            with self.subTest(error=type(error).__name__):
                bridge = PiObserverBridge(RaisingRpc(error))
                url = bridge.start()
                try:
                    body = json.dumps({"tool": "records_read",
                                       "arguments": {"after": 0}}).encode()
                    request = urllib.request.Request(
                        url + "/tool", data=body, method="POST",
                        headers={"content-type": "application/json",
                                 "authorization": "Bearer " + bridge.token})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        payload = json.loads(response.read())
                finally:
                    bridge.stop()
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"], "observer tool call failed")
                encoded = json.dumps(payload)
                for leaked in ("/srv", "ledger", "artifacts", "sqlite3",
                               "RunnerControlError", "Traceback", "control.sock",
                               "sk-secret", "/state"):
                    self.assertNotIn(leaked, encoded)

    def test_bridge_keeps_only_safe_user_validation_messages(self):
        from minecraft_harness.runner_rpc import RpcError
        from minecraft_harness.pi_observer_bridge import PiObserverBridge

        class RaisingRpc(FakeRpc):
            def __init__(self, error):
                super().__init__()
                self.error = error

            def call_sync(self, method, params):
                self.calls.append((method, dict(params)))
                raise self.error

        safe = RpcError("PermissionError",
                        "Evidence artifact was not delivered to this observer")
        bridge = PiObserverBridge(RaisingRpc(safe))
        url = bridge.start()
        try:
            body = json.dumps({"tool": "evidence_read",
                               "arguments": {"hash": "a" * 64}}).encode()
            request = urllib.request.Request(
                url + "/tool", data=body, method="POST",
                headers={"content-type": "application/json",
                         "authorization": "Bearer " + bridge.token})
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
        finally:
            bridge.stop()
        self.assertFalse(payload["ok"])
        # A bounded path-free user-validation message is actionable and kept.
        self.assertEqual(payload["error"],
                         "Evidence artifact was not delivered to this observer")

    def test_tool_output_is_bounded(self):
        from minecraft_harness.pi_observer_bridge import PiObserverBridge
        rpc = FakeRpc()
        rpc.response = {"status": "recorded", "event_seq": 1, "blob": "z" * 5000}
        bridge = PiObserverBridge(rpc, max_text_chars=256)
        url = bridge.start()
        try:
            body = json.dumps({"tool": "observer_report",
                               "arguments": {"findings": [
                                   {"status": "verified", "claim": "x",
                                    "evidence_refs": [1]}]}}).encode()
            request = urllib.request.Request(
                url + "/tool", data=body, method="POST",
                headers={"content-type": "application/json",
                         "authorization": "Bearer " + bridge.token})
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
        finally:
            bridge.stop()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["details"]["truncated"])
        self.assertIn("truncated at the model-visible limit", payload["content"])
        self.assertLess(len(payload["content"]), 400)


# ---------------------------------------------------------------------------
# Real Pi Observer runtime tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_PI_RUNTIME, "Node 22 and the Pi CLI runtime are required")
class PiObserverRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)
        self.hub = FakeObserverHub()
        from minecraft_harness.runner_rpc import RpcServer

        self.server = RpcServer(self.root / "control.sock",
                                {TOKEN: {"agent_id": "observer", "role": "observer"}},
                                self.hub.handle)
        await self.server.start()
        self._processes: list[asyncio.subprocess.Process] = []

    async def asyncTearDown(self):
        for process in self._processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await self.server.close()

    def make_spec(self, *, state_root: Path | None = None,
                  interval_s: float = 60.0, max_reviews: int = 1,
                  max_turns: int = 1,
                  api_key_env: str | None = None,
                  extra: dict[str, Any] | None = None) -> Path:
        state_root = state_root or (self.root / "state")
        workspace = self.root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        reference = self.root / "reference"
        reference.mkdir(parents=True, exist_ok=True)
        pi_model: dict[str, Any] = {"base_url": self.upstream.base_url,
                                    "model": "fake-upstream", "timeout_s": 30}
        if api_key_env:
            pi_model["api_key_env"] = api_key_env
        document = {
            "schema_version": 1,
            "run_id": "pi-observer-test-run",
            "agent_id": "observer",
            "role": "observer",
            "runner_runtime": "pi",
            "state_root": str(state_root),
            "workspace_root": str(workspace),
            "reference_root": str(reference),
            "pi_cli": str(PI_CLI),
            "pi_context_window": 64000,
            "heartbeat_s": 1,
            "pi_session_max_bytes": 5_000_000,
            "pi_artifact_max_bytes": 5_000_000,
            "interval_s": interval_s,
            "max_reviews": max_reviews,
            "pi_model": pi_model,
            "agent_config": {
                "agent_id": "observer",
                "goal": "Observe the shared survival world.",
                "peers": [],
                "profile": "paper_aligned",
                "context_chars": 48000,
                "max_turns": max_turns,
                "max_model_failures": 5,
            },
            "model": {"kind": "fake"},
        }
        if extra:
            document.update(extra)
        spec_path = self.root / f"observer-{len(list(self.root.glob('*.json')))}.json"
        spec_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(spec_path, 0o600)
        return spec_path

    def child_env(self, spec_path: Path) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": str(SOURCE_ROOT),
            "HARNESS_RUNNER_SOCKET": str(self.root / "control.sock"),
            "HARNESS_RUNNER_TOKEN": TOKEN,
            "HARNESS_RUNNER_AGENT": "observer",
            "HARNESS_RUNNER_ROLE": "observer",
            "HARNESS_RUNNER_RUN_ID": "pi-observer-test-run",
            "HARNESS_RUNNER_SPEC": str(spec_path),
        }

    async def spawn(self, spec_path: Path,
                    env: dict[str, str] | None = None) -> asyncio.subprocess.Process:
        environment = env or self.child_env(spec_path)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "minecraft_harness.pi_observer_runner",
            cwd=str(self.root), env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._processes.append(process)
        return process

    async def communicate(self, process: asyncio.subprocess.Process,
                          timeout: float = 60) -> tuple[bytes, bytes]:
        return await asyncio.wait_for(process.communicate(), timeout)

    def report_arguments(self) -> dict[str, Any]:
        return {
            "progress_summary": "The team gathered wood and made stone tools.",
            "verified_events": [{"claim": "stone tools advanced",
                                 "evidence_refs": [1]}],
            "infrastructure_findings": ["no broken tool observed"],
            "findings": [{"status": "verified", "claim": "stone tools advanced",
                          "evidence_refs": [1]}],
        }

    def report_script(self) -> tuple[bytes, bytes]:
        return (
            sse_response(tool_call_delta(
                "observer_report", json.dumps(self.report_arguments())),
                finish_reason="tool_calls"),
            sse_response({"role": "assistant", "content": "report published"}),
        )

    async def test_real_pi_observer_publishes_report_and_reports_ready_finished(self):
        spec_path = self.make_spec()
        self.upstream.set_script(*self.report_script())
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        stdout, stderr = await self.communicate(process)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(stdout, b"")

        ready = self.hub.ready or {}
        self.assertEqual(ready.get("agent_id"), "observer")
        self.assertEqual(ready.get("role"), "observer")
        self.assertEqual(ready.get("runtime"), "pi-observer")
        self.assertEqual(len(ready.get("prompt_sha256", "")), 64)
        self.assertEqual(len(ready.get("tools_sha256", "")), 64)
        self.assertEqual(ready.get("pid"), process.pid)

        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("agent_id"), "observer")
        self.assertEqual(result.get("status"), "turns_exhausted")
        self.assertEqual(result.get("reviews"), 1)
        self.assertEqual(result.get("reports"), 1)

        reports = self.hub.reports
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["progress_summary"],
                         self.report_arguments()["progress_summary"])
        self.assertEqual(reports[0]["findings"][0]["status"], "verified")
        self.assertEqual(reports[0]["verified_events"][0]["claim"],
                         "stone tools advanced")
        self.assertTrue(self.hub.events_of("observer_report"))

        kinds = self.hub.kinds()
        for expected in ("agent_started", "tool_intent", "tool_result",
                         "agent_turn", "model_request", "model_response",
                         "agent_stopped"):
            self.assertIn(expected, kinds)
        tool_intents = self.hub.events_of("tool_intent")
        self.assertEqual([item["payload"]["tool"] for item in tool_intents],
                         ["observer_report"])
        self.assertEqual(tool_intents[0]["payload"]["idempotency"], "side_effect")
        self.assertEqual(len(tool_intents[0]["payload"]["args_sha256"]), 64)
        tool_result = self.hub.events_of("tool_result")[0]["payload"]
        self.assertFalse(tool_result["is_error"])
        self.assertIn("result_ref", tool_result)

        model_request = self.hub.events_of("model_request")[0]["payload"]
        self.assertEqual(model_request["purpose"], "observer_review")
        self.assertEqual(model_request["role"], "observer")
        self.assertTrue(self.hub.settlements)
        self.assertEqual([item["reservation_id"] for item in self.hub.settlements],
                         self.hub.reservations)

        # The upstream saw exactly the Observer tool surface and prompt.
        bodies = self.upstream.request_bodies()
        self.assertGreaterEqual(len(bodies), 1)
        first = bodies[0]
        names = {tool.get("function", {}).get("name") for tool in first.get("tools") or []}
        self.assertEqual(names, PI_TOOLS)
        self.assertFalse(names & FORBIDDEN_TOOLS)
        messages = first.get("messages") or []
        system = json.dumps(messages[0], ensure_ascii=False)
        self.assertIn("independent witness", system)
        self.assertIn("never plays", system)
        self.assertIn("specification gaming", system)
        self.assertIn("progress", system.lower())
        self.assertIn("records_read", system)

        # Durable system prompt/tool surface and session state on disk.
        state = self.root / "state"
        prompt_text = (state / "system-prompt.txt").read_text(encoding="utf-8")
        self.assertIn("You never join the game or run a program", prompt_text)
        self.assertIn("records_read", prompt_text)
        self.assertIn("observer_report", prompt_text)
        self.assertTrue(list((state / "pi-sessions").rglob("*.jsonl")))
        models_json = (state / "home" / ".pi" / "agent" / "models.json").read_text()
        self.assertIn("mcproxy", models_json)
        self.assertNotIn(TOKEN, models_json)

    async def test_observer_artifact_budget_is_trusted_control_plane_state(self):
        spec_path = self.make_spec()
        # A forged model-writable local marker must not reset the trusted usage.
        self.hub.pi_budget["used_bytes"] = 400
        marker = self.root / "state" / "pi-artifact-budget.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "schema_version": 1, "limit_bytes": 5_000_000, "used_bytes": 0,
            "retained_count": 0, "metadata_bytes": 0,
            "retention_recorded": False, "pressure_recorded": False}))
        self.upstream.set_script(*self.report_script())
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        stdout, stderr = await self.communicate(process)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        # Artifact usage was charged to the control plane, never reset by the
        # marker (which the model can write).
        self.assertGreater(self.hub.pi_budget["used_bytes"], 400)
        self.assertIn("pi.budget.snapshot", self.hub.pi_budget_calls)
        self.assertIn("pi.budget.take", self.hub.pi_budget_calls)
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("status"), "turns_exhausted")

    async def test_real_pi_observer_can_request_an_infrastructure_repair(self):
        spec_path = self.make_spec()
        issue = {"kind": "broken_tool",
                 "reason": "records_read returned a malformed page",
                 "evidence_refs": [1]}
        self.upstream.set_script(
            sse_response(tool_call_delta("infra_issue", json.dumps(issue)),
                         finish_reason="tool_calls"),
            sse_response({"role": "assistant", "content": "repair requested"}),
        )
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(process)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(self.hub.infra_issues), 1)
        forwarded = self.hub.infra_issues[0]
        self.assertEqual(forwarded["kind"], "broken_tool")
        self.assertEqual(forwarded["reason"], issue["reason"])
        self.assertRegex(forwarded["issue_id"], r"^[a-f0-9]{32}$")
        intents = self.hub.events_of("tool_intent")
        self.assertEqual([item["payload"]["tool"] for item in intents],
                         ["infra_issue"])
        self.assertEqual(intents[0]["payload"]["idempotency"], "side_effect")

    async def test_important_event_triggers_the_next_review_before_interval(self):
        spec_path = self.make_spec(interval_s=60.0, max_reviews=2, max_turns=2)
        self.upstream.set_script(
            *self.report_script(),
            sse_response({"role": "assistant", "content": "updated report"}),
        )
        # A runtime reset is important: the next review should be requested
        # immediately rather than after the 60 s interval.
        self.hub.trigger_event = {"event_seq": 9, "kind": "runtime_reset",
                                  "actor_id": "supervisor", "payload": {}}
        process = await self.spawn(spec_path)
        started = time.monotonic()
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(process)
        elapsed = time.monotonic() - started
        self.assertEqual(process.returncode, 0)
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("status"), "turns_exhausted")
        self.assertEqual(result.get("reviews"), 2)
        self.assertLess(elapsed, 45)
        self.assertEqual(len(self.hub.events_of("agent_started")), 2)
        self.assertGreaterEqual(len(self.hub.events_of("model_request")), 3)
        self.assertGreaterEqual(len(self.upstream.request_bodies()), 3)

    async def test_elapsed_interval_triggers_the_next_review(self):
        spec_path = self.make_spec(interval_s=0.25, max_reviews=2, max_turns=2)
        self.upstream.set_script(
            *self.report_script(),
            sse_response({"role": "assistant", "content": "interval review"}),
        )
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(process)
        self.assertEqual(process.returncode, 0)
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("status"), "turns_exhausted")
        self.assertEqual(result.get("reviews"), 2)
        self.assertEqual(len(self.hub.events_of("agent_started")), 2)
        self.assertGreaterEqual(len(self.hub.events_of("model_request")), 3)

    async def test_session_resumes_after_runner_restart(self):
        spec_path = self.make_spec()
        self.upstream.set_script(*self.report_script())
        first = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(first)
        self.assertEqual(first.returncode, 0)
        session_files = list((self.root / "state" / "pi-sessions").rglob("*.jsonl"))
        self.assertEqual(len(session_files), 1)
        self.assertGreaterEqual(len(self.upstream.request_bodies()), 1)

        self.hub.ready_event = asyncio.Event()
        self.hub.finished_event = asyncio.Event()
        self.upstream.reset_requests()
        self.upstream.set_script(*self.report_script())
        second = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(second)
        self.assertEqual(second.returncode, 0)
        session_files_after = list((self.root / "state" / "pi-sessions").rglob("*.jsonl"))
        self.assertEqual(len(session_files_after), 1)
        messages = (self.upstream.request_bodies()[0].get("messages") or [])
        self.assertGreater(len(messages), 3)
        self.assertIn("report published",
                      json.dumps(messages, ensure_ascii=False))

    async def test_oversized_session_is_archived_by_the_shared_retention_policy(self):
        spec_path = self.make_spec()
        self.upstream.set_script(*self.report_script())
        first = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(first)
        self.assertEqual(first.returncode, 0)
        self.assertTrue(list((self.root / "state" / "pi-sessions").rglob("*.jsonl")))
        # Lower the cap after the first run so the next start has to archive the
        # existing oversized session before deciding whether to resume it.
        document = json.loads(spec_path.read_text(encoding="utf-8"))
        document["pi_session_max_bytes"] = 1024
        spec_path.write_text(json.dumps(document), encoding="utf-8")

        # The second start must apply the shared pi_runner retention helper
        # before the resume decision: the oversized session is archived and the
        # runner starts a fresh session instead of loading an unbounded one.
        self.hub.ready_event = asyncio.Event()
        self.hub.finished_event = asyncio.Event()
        self.upstream.reset_requests()
        self.upstream.set_script(*self.report_script())
        second = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.finished_event.wait(), 120)
        await self.communicate(second)
        self.assertEqual(second.returncode, 0)
        self.assertIn("pi_session_rotated", self.hub.kinds())
        archive = self.root / "state" / "pi-sessions-archive"
        self.assertTrue(list(archive.rglob("*.jsonl")))

    async def test_sigterm_aborts_pi_and_reports_stopped(self):
        spec_path = self.make_spec(max_reviews=5)
        self.upstream.delay_s = 3.0
        self.upstream.set_script(
            sse_response({"role": "assistant", "content": "slow review"}))
        process = await self.spawn(spec_path)
        await asyncio.wait_for(self.hub.ready_event.wait(), 60)
        await asyncio.wait_for(self.hub.wait_for_event("model_request", 60), 60)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(self.hub.finished_event.wait(), 60)
        stdout, stderr = await self.communicate(process, timeout=30)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(stdout, b"")
        result = (self.hub.finished or {}).get("result") or {}
        self.assertEqual(result.get("agent_id"), "observer")
        self.assertEqual(result.get("status"), "stopped")
        self.assertIn("agent_stopped", self.hub.kinds())

    async def test_runner_environment_is_not_readable_via_proc(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux /proc credential test")
        with mock.patch.dict(os.environ, {"MC_OBS_TEST_KEY": "sk-observer-secret"}):
            spec_path = self.make_spec(api_key_env="MC_OBS_TEST_KEY")
            process = await self.spawn(spec_path, env={
                **self.child_env(spec_path),
                "MC_OBS_TEST_KEY": "sk-observer-secret"})
            await asyncio.wait_for(self.hub.ready_event.wait(), 60)
            try:
                with self.assertRaises(PermissionError):
                    with open(f"/proc/{process.pid}/environ", "rb") as handle:
                        handle.read()
            finally:
                process.kill()
                await process.wait()

    async def test_wrong_role_and_bad_specs_fail_closed_at_startup(self):
        spec_path = self.make_spec()
        document = json.loads(spec_path.read_text())
        document["role"] = "player"
        spec_path.write_text(json.dumps(document))
        process = await self.spawn(spec_path)
        stdout, stderr = await self.communicate(process, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(stdout, b"")
        self.assertIn("__runner_error__", stderr.decode())

        spec_path = self.make_spec(extra={"cli_environment": {
            "MC_AGENT_TOKEN": "player-token"}})
        process = await self.spawn(spec_path)
        stdout, stderr = await self.communicate(process, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(stdout, b"")
        self.assertIn("player CLI credentials", stderr.decode())

    def test_child_environment_carries_no_runner_or_upstream_secrets(self):
        from minecraft_harness.pi_observer_runner import _child_environment
        parent = {
            "PATH": "/usr/bin:/bin",
            "HARNESS_RUNNER_TOKEN": "runner-token",
            "HARNESS_RUNNER_SPEC": "/tmp/spec.json",
            "MC_MODEL_API_KEY": "sk-upstream",
            "DEEPSEEK_API_KEY": "sk-upstream-2",
            "MC_AGENT_TOKEN": "player-token",
        }
        with mock.patch.dict(os.environ, parent, clear=True):
            environment = _child_environment(
                home=self.root / "home", bin_dir=self.root / "bin",
                tmp_dir=self.root / "tmp", bridge_url="http://127.0.0.1:1",
                bridge_token="bridge-token")
        encoded = json.dumps(environment)
        for secret in ("runner-token", "sk-upstream", "sk-upstream-2",
                       "player-token", "HARNESS_RUNNER_TOKEN",
                       "MC_MODEL_API_KEY", "DEEPSEEK_API_KEY"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(environment["HARNESS_PI_BRIDGE_TOKEN"], "bridge-token")
        self.assertIn("NO_PROXY", environment)


if __name__ == "__main__":
    unittest.main()
