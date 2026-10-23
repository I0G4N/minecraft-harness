"""Unit tests for the Pi runtime's loopback OpenAI-compatible model proxy.

Uses a fake loopback upstream HTTP server and a fake synchronous RPC client, so
no control plane, no Node and no external network are required.

Run in the disposable test env::

    PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
        .test-venv/bin/python -m pytest -q tests/test_pi_model_proxy.py
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from minecraft_harness.pi_model_proxy import (
    ArtifactBudget,
    budgeted_artifact_text,
    ENV_MODEL_FORWARDER_TOKEN,
    FORWARDER_TOKEN_HEADER,
    MODEL_FORWARDER_ALLOWED_PATH,
    MODEL_FORWARDER_ENDPOINT,
    MODEL_FORWARDER_SOCKET_NAME,
    PiModelForwarder,
    PiModelProxy,
    ProxyInfraError,
    UNKNOWN_USAGE,
)


class FakeRpcError(RuntimeError):
    def __init__(self, error_type: str, message: str = "") -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = False


class FakeRpc:
    """Duck-typed synchronous RPC client mimicking the runner/ServiceHub API."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events: list[dict[str, Any]] = []
        self.artifacts: dict[str, str] = {}
        self.reservations: list[str] = []
        self.settlements: list[dict[str, Any]] = []
        self.settle_event = threading.Event()
        self.admission: dict[str, Any] = {"admitted": True, "stop": False}
        self.reserve_error: tuple[str, str] | None = None
        self.validate_error: tuple[str, str] | None = None
        self.append_error: bool = False
        self.admission_timeout: bool = False

    def call_sync(self, method: str, params: dict[str, Any] | None = None,
                  *, timeout_s: float | None = None) -> Any:
        params = params if isinstance(params, dict) else {}
        with self.lock:
            self.calls.append((method, json.loads(json.dumps(params))))
        if method == "budget.wait_admission":
            if self.admission_timeout:
                raise FakeRpcError("TimeoutError", "admission wait timed out")
            return dict(self.admission)
        if method == "budget.validate_request":
            if self.validate_error:
                raise FakeRpcError(*self.validate_error)
            return None
        if method == "budget.reserve":
            if self.reserve_error:
                raise FakeRpcError(*self.reserve_error)
            reservation = f"res-{len(self.reservations) + 1}"
            with self.lock:
                self.reservations.append(reservation)
            return reservation
        if method == "budget.settle":
            with self.lock:
                self.settlements.append(dict(params))
            self.settle_event.set()
            return None
        if method == "recorder.append":
            if self.append_error:
                raise FakeRpcError("StorageLimitError", "ledger full")
            with self.lock:
                self.events.append(dict(params))
            return {"event_seq": len(self.events)}
        if method == "recorder.put_artifact":
            text = str(params.get("text", ""))
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            with self.lock:
                self.artifacts[digest] = text
            return {"hash": digest, "sha256": digest, "path": "artifacts/" + digest}
        raise AssertionError(f"unexpected RPC method: {method}")

    def calls_for(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    def event_kinds(self) -> list[str]:
        return [event.get("kind") for event in self.events]

    def artifact_corpus(self) -> str:
        return json.dumps(self.artifacts, ensure_ascii=False)


class FakeUpstream:
    """Loopback OpenAI-compatible upstream driven by a scripted response list."""

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
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
                    upstream.requests.append({"path": self.path,
                                              "headers": dict(self.headers),
                                              "body": body})
                    if upstream.script:
                        response = upstream.script.pop(0)
                    else:
                        response = upstream.default_response(body)
                upstream._respond(self, response)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        port = server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}/v1"
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

    @staticmethod
    def default_response(body: Any) -> dict[str, Any]:
        return {"kind": "sse", "chunks": sse_chunks("hello",
                                                   usage=openai_usage(10, 5))}

    @staticmethod
    def _respond(handler: BaseHTTPRequestHandler, response: dict[str, Any]) -> None:
        kind = response.get("kind")
        if kind == "sse":
            payload = b"".join(response.get("chunks") or [])
            status = int(response.get("status", 200))
            handler.send_response(status)
            handler.send_header("content-type", "text/event-stream")
            handler.send_header("content-length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        elif kind == "json":
            payload = json.dumps(response.get("payload") or {}).encode("utf-8")
            handler.send_response(int(response.get("status", 200)))
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        else:  # error
            payload = response.get("body") or b'{"error":"boom"}'
            handler.send_response(int(response.get("status", 500)))
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        handler.close_connection = True


def openai_usage(prompt: int, completion: int) -> dict[str, int]:
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def sse_chunks(content: str | None = None,
               usage: dict[str, Any] | None = None,
               tool_name: str | None = None,
               tool_arguments: str = "{}") -> list[bytes]:
    records: list[dict[str, Any]] = []
    delta: dict[str, Any] = {"role": "assistant"}
    if content:
        delta["content"] = content
    if tool_name:
        delta["tool_calls"] = [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": tool_name,
                                             "arguments": tool_arguments}}]
    records.append({"id": "chunk", "object": "chat.completion.chunk",
                    "model": "upstream-model", "choices": [{"index": 0, "delta": delta,
                                                            "finish_reason": None}]})
    final: dict[str, Any] = {"id": "chunk", "object": "chat.completion.chunk",
                             "model": "upstream-model",
                             "choices": [{"index": 0, "delta": {},
                                          "finish_reason": "stop"}]}
    if usage is not None:
        final["usage"] = usage
    records.append(final)
    chunks = [b"data: " + json.dumps(record).encode("utf-8") + b"\n\n" for record in records]
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def request_body(stream: bool = True, content: str = "hello") -> dict[str, Any]:
    return {"model": "pi-model-id", "stream": stream,
            "messages": [{"role": "user", "content": content}]}


class ModelProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)
        self.rpc = FakeRpc()

    def proxy(self, *, transport: httpx.BaseTransport | None = None,
              **overrides: Any) -> PiModelProxy:
        model = {"base_url": self.upstream.base_url, "model": "upstream-model",
                 "api_key_env": None, "timeout_s": 10}
        model.update(overrides)
        proxy = PiModelProxy(model, self.rpc, agent_id="player1", role="player",
                             transport=transport)
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def post(self, proxy: PiModelProxy, payload: dict[str, Any],
             stream: bool = False) -> tuple[int, dict[str, str], bytes]:
        client = httpx.Client(trust_env=False, timeout=10)
        try:
            if stream:
                with client.stream("POST", proxy.base_url + "/chat/completions",
                                   json=payload) as response:
                    return response.status_code, dict(response.headers), b"".join(
                        response.iter_bytes())
            response = client.post(proxy.base_url + "/chat/completions", json=payload)
            return response.status_code, dict(response.headers), response.content
        finally:
            client.close()

    def wait_settled(self, timeout: float = 5) -> None:
        self.assertTrue(self.rpc.settle_event.wait(timeout), "no budget settlement recorded")

    # -- success paths ---------------------------------------------------
    def test_streaming_passthrough_settles_usage_and_records_ledger(self):
        self.upstream.script = [{"kind": "sse", "chunks": sse_chunks(
            "hello", usage=openai_usage(11, 7), tool_name="bash",
            tool_arguments='{"command": "ls"}')}]
        proxy = self.proxy()
        status, headers, body = self.post(proxy, request_body(stream=True, content="do work"))
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("content-type", ""))
        self.assertIn(b"hello", body)
        self.assertIn(b"[DONE]", body)
        self.wait_settled()
        self.assertEqual(self.rpc.reservations, ["res-1"])
        self.assertEqual(len(self.rpc.settlements), 1)
        self.assertEqual(self.rpc.settlements[0]["reservation_id"], "res-1")
        self.assertEqual(self.rpc.settlements[0]["usage"], openai_usage(11, 7))
        self.assertEqual(self.rpc.event_kinds(), ["model_request", "model_response"])
        request_event, response_event = self.rpc.events[0]["payload"], self.rpc.events[1]["payload"]
        self.assertEqual(request_event["reservation_id"], "res-1")
        self.assertEqual(request_event["purpose"], "agent_decision")
        self.assertEqual(request_event["role"], "player")
        self.assertEqual(response_event["reservation_id"], "res-1")
        self.assertEqual(response_event["usage"], openai_usage(11, 7))
        self.assertGreaterEqual(response_event["latency_ms"], 0)
        artifacts = self.rpc.artifact_corpus()
        self.assertIn("do work", artifacts)      # request artifact
        self.assertIn("hello", artifacts)        # response artifact
        self.assertIn("bash", artifacts)         # tool-call capture
        # The request reached the upstream with the configured upstream model id.
        self.assertEqual(self.upstream.requests[0]["body"]["model"], "upstream-model")

    def test_non_stream_json_usage_and_body_are_relayed(self):
        self.upstream.script = [{"kind": "json", "payload": {
            "id": "chatcmpl-1", "object": "chat.completion", "model": "upstream-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}],
            "usage": openai_usage(4, 2)}}]
        proxy = self.proxy()
        status, _headers, body = self.post(proxy, request_body(stream=False))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "pong")
        self.wait_settled()
        self.assertEqual(self.rpc.settlements[0]["usage"], openai_usage(4, 2))
        self.assertEqual(self.rpc.event_kinds(), ["model_request", "model_response"])

    def test_models_endpoint_lists_the_configured_model(self):
        proxy = self.proxy()
        client = httpx.Client(trust_env=False, timeout=10)
        try:
            response = client.get(proxy.base_url + "/models")
        finally:
            client.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], "upstream-model")

    # -- denial paths ----------------------------------------------------
    def test_reserve_denial_returns_429_without_upstream_call(self):
        self.rpc.reserve_error = ("BudgetExhausted", "team model budget exhausted")
        proxy = self.proxy()
        status, _headers, body = self.post(proxy, request_body())
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["type"], "budget_exhausted")
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(proxy.denials, 1)
        self.assertEqual(self.rpc.settlements, [])

    def test_admission_stop_denial_returns_429_without_upstream_call(self):
        self.rpc.admission = {"admitted": False, "stop": True}
        proxy = self.proxy()
        status, _headers, body = self.post(proxy, request_body())
        self.assertEqual(status, 429)
        self.assertIn("paused or stopped", json.loads(body)["error"]["message"])
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(proxy.denials, 1)

    def test_request_validation_denial_returns_429_without_upstream_call(self):
        self.rpc.validate_error = ("BudgetExhausted", "request over bound")
        proxy = self.proxy()
        status, _headers, _body = self.post(proxy, request_body())
        self.assertEqual(status, 429)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(proxy.denials, 1)

    # -- failure paths ---------------------------------------------------
    def test_upstream_error_settles_zero_usage(self):
        self.upstream.script = [{"kind": "error", "status": 500,
                                 "body": b'{"error":"upstream exploded"}'}]
        proxy = self.proxy()
        status, _headers, _body = self.post(proxy, request_body())
        self.assertEqual(status, 500)
        self.wait_settled()
        # No completion was produced; don't charge the full reservation.
        self.assertEqual(self.rpc.settlements[0]["usage"], {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        self.assertEqual(proxy.upstream_errors, 1)
        response_event = self.rpc.events[1]["payload"]
        self.assertEqual(response_event["usage"], {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    def test_auth_upstream_error_is_fatal(self):
        self.upstream.script = [{"kind": "error", "status": 401,
                                 "body": b'{"error":"bad key"}'}]
        proxy = self.proxy()
        status, _headers, _body = self.post(proxy, request_body())
        self.assertEqual(status, 401)
        self.wait_settled()
        self.assertTrue(proxy.fatal_upstream_error)
        self.assertEqual(proxy.last_upstream_status, 401)

    def test_paused_admission_timeout_does_not_reserve(self):
        self.rpc.admission_timeout = True
        proxy = self.proxy()
        status, _headers, body = self.post(proxy, request_body())
        self.assertEqual(status, 503)
        self.assertIn("paused", body.decode("utf-8", errors="replace"))
        self.assertEqual(self.rpc.reservations, [])
        self.assertEqual(self.rpc.settlements, [])

    def test_missing_stream_usage_settles_conservative_unknown(self):
        self.upstream.script = [{"kind": "sse", "chunks": sse_chunks("no usage here")}]
        proxy = self.proxy()
        status, _headers, _body = self.post(proxy, request_body())
        self.assertEqual(status, 200)
        self.wait_settled()
        self.assertEqual(self.rpc.settlements[0]["usage"], UNKNOWN_USAGE)

    def test_ledger_failure_is_fail_closed_before_upstream_call(self):
        self.rpc.append_error = True
        proxy = self.proxy()
        status, _headers, _body = self.post(proxy, request_body())
        self.assertEqual(status, 503)
        self.assertEqual(self.upstream.requests, [])
        self.wait_settled()  # the reservation must still be settled (unknown usage)
        self.assertEqual(self.rpc.settlements[0]["usage"], UNKNOWN_USAGE)

    # -- secret hygiene --------------------------------------------------
    def test_upstream_credential_is_never_exposed_to_client_ledger_or_logs(self):
        secret = "sk-test-super-secret-value"
        with mock.patch.dict(os.environ, {"MC_PROXY_TEST_KEY": secret}):
            self.upstream.script = [{"kind": "sse",
                                     "chunks": sse_chunks("ok", usage=openai_usage(1, 1))}]
            proxy = self.proxy(api_key_env="MC_PROXY_TEST_KEY")
            status, _headers, body = self.post(proxy, request_body())
        self.assertEqual(status, 200)
        self.wait_settled()
        # The credential reached the upstream as a bearer token ...
        authorization = self.upstream.requests[0]["headers"].get("authorization")
        self.assertEqual(authorization, "Bearer " + secret)
        # ... and appears nowhere the model/client/ledger can see.
        visible = json.dumps({
            "client_body": body.decode("utf-8", errors="replace"),
            "events": self.rpc.events,
            "artifacts": self.rpc.artifact_corpus(),
            "calls": self.rpc.calls,
            "settlements": self.rpc.settlements,
        }, ensure_ascii=False)
        self.assertNotIn(secret, visible)

    def test_upstream_http_client_is_reused_and_closed_on_stop(self):
        proxy = self.proxy()
        first = proxy._upstream_client()
        second = proxy._upstream_client()
        self.assertIs(first, second)
        proxy.stop()
        self.assertIsNone(proxy._http_client)


class _BoomStream(httpx.SyncByteStream):
    """Yields one partial SSE chunk, then fails like a dropped upstream."""

    def __iter__(self):
        yield (b'data: {"id":"c","object":"chat.completion.chunk","model":"fake",'
               b'"choices":[{"index":0,"delta":{"role":"assistant","content":"partial"},'
               b'"finish_reason":None}]}\n\n')
        raise httpx.ReadError("upstream connection dropped mid-stream")


class _NoTerminatorStream(httpx.SyncByteStream):
    """A well-formed chunk stream that ends without the [DONE] terminator."""

    def __iter__(self):
        yield (b'data: {"id":"c","object":"chat.completion.chunk","model":"fake",'
               b'"choices":[{"index":0,"delta":{"role":"assistant","content":"partial"},'
               b'"finish_reason":"stop"}]}\n\n')


def _raw_post(proxy: PiModelProxy, payload: dict[str, Any]) -> bytes:
    """Send one raw HTTP/1.1 request and capture every byte until close."""
    parsed = urlsplit(proxy.base_url)
    body = json.dumps(payload).encode("utf-8")
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: " + (parsed.hostname or "").encode() + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    try:
        sock.sendall(request)
        chunks = []
        raw = b""
        while True:
            try:
                data = sock.recv(65536)
            except TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
            raw += data
            if raw.endswith(b"0\r\n\r\n"):
                break  # successful chunked terminator
        return b"".join(chunks)
    finally:
        sock.close()


class MidStreamFailureTests(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeRpc()
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)

    def make_proxy(self, transport: httpx.BaseTransport) -> PiModelProxy:
        proxy = PiModelProxy({"base_url": self.upstream.base_url,
                              "model": "upstream-model", "timeout_s": 10},
                             self.rpc, agent_id="player1", role="player",
                             transport=transport)
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def test_mid_stream_failure_sends_no_second_response_or_false_response_event(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_BoomStream()))
        proxy = self.make_proxy(transport)
        raw = _raw_post(proxy, request_body(stream=True))
        # Exactly one HTTP response, no trailing 502 after the partial stream.
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"), raw[:200])
        self.assertEqual(raw.count(b"HTTP/1.1"), 1, raw[-400:])
        self.assertNotIn(b"HTTP/1.1 502", raw)
        self.assertIn(b"partial", raw)
        # The connection is closed without the chunked terminator.
        self.assertFalse(raw.endswith(b"0\r\n\r\n"), raw[-80:])
        # One settlement with conservative unknown usage, a model_error event,
        # and no false model_response / null response artifact.
        self.assertTrue(self.rpc.settle_event.wait(5))
        self.assertEqual(len(self.rpc.settlements), 1)
        self.assertEqual(self.rpc.settlements[0]["usage"], UNKNOWN_USAGE)
        kinds = self.rpc.event_kinds()
        self.assertIn("model_request", kinds)
        self.assertIn("model_error", kinds)
        self.assertNotIn("model_response", kinds)
        self.assertEqual(len(self.rpc.artifacts), 1)  # request artifact only
        self.assertNotIn("null", "".join(self.rpc.artifacts.values()))
        model_error = next(event for event in self.rpc.events
                           if event.get("kind") == "model_error")
        self.assertEqual(model_error["payload"]["phase"], "upstream_stream")
        self.assertNotIn("response_ref", model_error["payload"])

    def test_stream_without_done_terminator_is_a_model_error(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_NoTerminatorStream()))
        proxy = self.make_proxy(transport)
        raw = _raw_post(proxy, request_body(stream=True))
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"))
        self.assertNotIn(b"HTTP/1.1 502", raw)
        self.assertFalse(raw.endswith(b"0\r\n\r\n"))
        self.assertTrue(self.rpc.settle_event.wait(5))
        self.assertEqual(len(self.rpc.settlements), 1)
        self.assertEqual(self.rpc.settlements[0]["usage"], UNKNOWN_USAGE)
        kinds = self.rpc.event_kinds()
        self.assertIn("model_error", kinds)
        self.assertNotIn("model_response", kinds)
        self.assertEqual(len(self.rpc.artifacts), 1)

    def test_normal_stream_terminates_exactly_once(self):
        self.upstream.script = [{"kind": "sse", "chunks": sse_chunks(
            "hello", usage=openai_usage(4, 2))}]
        proxy = self.make_proxy(None)
        raw = _raw_post(proxy, request_body(stream=True))
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"))
        self.assertEqual(raw.count(b"HTTP/1.1"), 1)
        self.assertTrue(raw.endswith(b"0\r\n\r\n"), raw[-80:])
        self.assertTrue(self.rpc.settle_event.wait(5))
        self.assertEqual(len(self.rpc.settlements), 1)
        kinds = self.rpc.event_kinds()
        self.assertIn("model_response", kinds)
        self.assertNotIn("model_error", kinds)


class PiModelProxyRestrictedEgressTests(unittest.TestCase):
    """The Pi-side proxy in restricted mode talks only to the host forwarder."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rpc = FakeRpc()
        self.token = "forwarder-token-" + "x" * 24
        self.forwarder_socket = self.root / "model-forwarder" / MODEL_FORWARDER_SOCKET_NAME
        self.forwarder_socket.parent.mkdir(parents=True)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._respond)
        self.model = {
            "kind": "chat_completions", "model": "pi-model-id",
            "base_url": "https://provider.example.test/v1",
            "api_key_env": "FAKE_PROVIDER_KEY",
            "model_forwarder_socket": str(self.forwarder_socket),
            "timeout_s": 30, "max_output_tokens": 4096,
        }

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = b"".join(sse_chunks("restricted hello", usage=openai_usage(7, 3)))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=payload)

    def _proxy(self):
        proxy = PiModelProxy(self.model, self.rpc, agent_id="player1",
                             transport=self.transport)
        self.addCleanup(proxy.stop)
        proxy.start()
        return proxy

    def test_restricted_proxy_uses_forwarder_endpoint_and_token(self):
        with mock.patch.dict(os.environ, {ENV_MODEL_FORWARDER_TOKEN: self.token,
                                          "FAKE_PROVIDER_KEY": "provider-secret"}):
            proxy = self._proxy()
            raw = _raw_post(proxy, request_body(stream=True))
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"restricted hello", raw)
        self.assertTrue(raw.endswith(b"0\r\n\r\n"))
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), MODEL_FORWARDER_ENDPOINT)
        self.assertEqual(request.headers.get(FORWARDER_TOKEN_HEADER), self.token)
        # The provider credential must never leave the Supervisor in restricted mode.
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("provider-secret", raw.decode("utf-8", errors="replace"))
        self.assertNotIn("provider-secret", json.dumps(self.rpc.artifacts))
        self.assertTrue(self.rpc.settle_event.wait(5))
        self.assertEqual(len(self.rpc.settlements), 1)
        self.assertEqual(self.rpc.settlements[0]["usage"], openai_usage(7, 3))

    def test_restricted_proxy_requires_a_forwarder_token(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_MODEL_FORWARDER_TOKEN, None)
            with self.assertRaises(ProxyInfraError):
                PiModelProxy(self.model, self.rpc, agent_id="player1",
                             transport=self.transport)

    def test_restricted_proxy_rejects_blank_forwarder_socket(self):
        model = dict(self.model, model_forwarder_socket="")
        with self.assertRaises(ValueError):
            PiModelProxy(model, self.rpc, agent_id="player1",
                         transport=self.transport)


class ModelForwarderTests(unittest.TestCase):
    """Host-side egress allowlist: one provider path, one credential, no secrets."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)
        self.token = "forwarder-token-" + "y" * 24
        self.env = mock.patch.dict(os.environ, {"FAKE_PROVIDER_KEY": "provider-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.model = {
            "kind": "chat_completions", "model": "upstream-model",
            "base_url": self.upstream.base_url,
            "api_key_env": "FAKE_PROVIDER_KEY",
            "timeout_s": 10, "max_request_bytes": 4096,
            "max_response_bytes": 4096,
        }
        self.socket_path = self.root / "model-forwarder" / MODEL_FORWARDER_SOCKET_NAME
        self.forwarder = PiModelForwarder(self.model, socket_path=self.socket_path,
                                          token=self.token)
        self.forwarder.start()
        self.addCleanup(self.forwarder.stop)

    def _client(self):
        client = httpx.Client(transport=httpx.HTTPTransport(uds=str(self.socket_path)),
                              base_url="http://model-forwarder", timeout=10)
        self.addCleanup(client.close)
        return client

    def _post(self, payload, *, token=None, path=MODEL_FORWARDER_ALLOWED_PATH):
        headers = {"content-type": "application/json"}
        supplied = self.token if token is None else token
        if supplied:
            headers[FORWARDER_TOKEN_HEADER] = supplied
        return self._client().post(path, content=json.dumps(payload).encode("utf-8"),
                                   headers=headers)

    def test_socket_and_mount_dir_are_owned_by_the_runner_uid(self):
        path = self.root / "owned-forwarder" / MODEL_FORWARDER_SOCKET_NAME
        forwarder = PiModelForwarder(
            self.model, socket_path=path, token=self.token,
            owner_uid=os.getuid(), owner_gid=os.getgid())
        forwarder.start()
        self.addCleanup(forwarder.stop)
        stat_result = path.stat()
        self.assertEqual((stat_result.st_uid, stat_result.st_gid),
                         (os.getuid(), os.getgid()))
        self.assertEqual(stat_result.st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o750)
        client = httpx.Client(transport=httpx.HTTPTransport(uds=str(path)),
                              base_url="http://model-forwarder", timeout=10)
        self.addCleanup(client.close)
        headers = {"content-type": "application/json", FORWARDER_TOKEN_HEADER: self.token}
        response = client.post(MODEL_FORWARDER_ALLOWED_PATH,
                               content=json.dumps(request_body()).encode("utf-8"),
                               headers=headers)
        self.assertEqual(response.status_code, 200)

    def test_relays_sse_and_injects_provider_credential(self):
        response = self._post(request_body(stream=True, content="forwarded"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers.get("content-type", ""))
        self.assertIn(b"hello", response.content)
        self.assertIn(b"[DONE]", response.content)
        self.assertEqual(len(self.upstream.requests), 1)
        sent = self.upstream.requests[0]
        self.assertEqual(sent["headers"].get("authorization"), "Bearer provider-secret")
        self.assertNotIn(FORWARDER_TOKEN_HEADER, sent["headers"])
        self.assertEqual(sent["body"]["messages"][0]["content"], "forwarded")

    def test_relays_non_stream_json(self):
        self.upstream.script = [{"kind": "json", "payload": {
            "id": "x", "choices": [{"message": {"content": "json-body"}}]}}]
        response = self._post(request_body(stream=False))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "json-body")
        self.assertNotIn("authorization", self.upstream.requests[0]["headers"].get(
            FORWARDER_TOKEN_HEADER, ""))

    def test_rejects_bad_token_unknown_path_and_get(self):
        self.assertEqual(self._post(request_body(), token="wrong-token").status_code, 401)
        self.assertEqual(self._post(request_body(), token="").status_code, 401)
        self.assertEqual(self._post(request_body(), path="/v1/models").status_code, 404)
        self.assertEqual(self._client().get(MODEL_FORWARDER_ALLOWED_PATH).status_code, 405)
        self.assertEqual(self.upstream.requests, [])

    def test_rejects_oversized_request_body(self):
        oversized = request_body()
        oversized["messages"] = [{"role": "user", "content": "x" * 8192}]
        self.assertEqual(self._post(oversized).status_code, 413)
        self.assertEqual(self.upstream.requests, [])

    def test_forwards_provider_error_status(self):
        self.upstream.script = [{"kind": "error", "status": 429,
                                 "body": b'{"error":{"message":"rate limited"}}'}]
        response = self._post(request_body())
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"rate limited", response.content)

    def test_mid_stream_upstream_failure_does_not_append_a_second_response(self):
        def drop(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_BoomStream())

        failing = PiModelForwarder(self.model,
                                   socket_path=self.root / "failing" / MODEL_FORWARDER_SOCKET_NAME,
                                   token=self.token, transport=httpx.MockTransport(drop))
        failing.start()
        self.addCleanup(failing.stop)
        client = httpx.Client(transport=httpx.HTTPTransport(
            uds=str(self.root / "failing" / MODEL_FORWARDER_SOCKET_NAME)),
            base_url="http://model-forwarder", timeout=10)
        self.addCleanup(client.close)
        with client.stream("POST", MODEL_FORWARDER_ALLOWED_PATH,
                           content=json.dumps(request_body()).encode("utf-8"),
                           headers={"content-type": "application/json",
                                    FORWARDER_TOKEN_HEADER: self.token}) as response:
            self.assertEqual(response.status_code, 200)
            with self.assertRaises(httpx.HTTPError):
                b"".join(response.iter_raw())

    def test_restricted_proxy_end_to_end_through_the_forwarder(self):
        """Real UDS path: PiModelProxy -> host forwarder -> fake provider."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fake_upstream = FakeUpstream()
        fake_upstream.start()
        self.addCleanup(fake_upstream.stop)
        socket_path = root / "model-forwarder" / MODEL_FORWARDER_SOCKET_NAME
        token = "forwarder-token-" + "q" * 24
        model = {"kind": "chat_completions", "model": "pi-model-id",
                 "base_url": fake_upstream.base_url,
                 "api_key_env": "FAKE_PROVIDER_KEY",
                 "model_forwarder_socket": str(socket_path),
                 "timeout_s": 20, "max_output_tokens": 4096}
        forwarder = PiModelForwarder(model, socket_path=socket_path, token=token)
        forwarder.start()
        self.addCleanup(forwarder.stop)
        rpc = FakeRpc()
        with mock.patch.dict(os.environ, {ENV_MODEL_FORWARDER_TOKEN: token,
                                          "FAKE_PROVIDER_KEY": "provider-secret"}):
            proxy = PiModelProxy(model, rpc, agent_id="player1")
            self.addCleanup(proxy.stop)
            proxy.start()
            raw = _raw_post(proxy, request_body(stream=True, content="end to end"))
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"hello", raw)
        self.assertTrue(raw.endswith(b"0\r\n\r\n"))
        self.assertEqual(len(fake_upstream.requests), 1)
        sent = fake_upstream.requests[0]
        self.assertEqual(sent["headers"].get("authorization"), "Bearer provider-secret")
        self.assertEqual(sent["body"]["messages"][0]["content"], "end to end")
        self.assertTrue(rpc.settle_event.wait(5))
        self.assertEqual(rpc.settlements[0]["usage"], openai_usage(10, 5))
        self.assertIn("model_response", rpc.event_kinds())
        # The forwarder token and provider key never appear in runner-visible bytes.
        visible = raw.decode("utf-8", errors="replace") + json.dumps(rpc.artifacts)
        self.assertNotIn(token, visible)
        self.assertNotIn("provider-secret", visible)

    def test_stop_unlinks_socket_and_rejects_non_socket_path(self):
        self.assertTrue(self.socket_path.exists())
        self.forwarder.stop()
        self.assertFalse(self.socket_path.exists())
        occupied = self.root / "occupied.sock"
        occupied.write_text("not a socket")
        with self.assertRaises(ValueError):
            PiModelForwarder(self.model, socket_path=occupied,
                             token=self.token).start()
        self.assertEqual(occupied.read_text(), "not a socket")



class _FakeCentralBudget:
    """Duck-typed synchronous ServiceHub pi-budget RPC for ArtifactBudget."""

    def __init__(self, *, limit=100, global_limit=1000):
        self.limit = limit
        self.global_limit = global_limit
        self.used = 0
        self.metadata = 0
        self.retained = 0
        self.pressure_recorded = False
        self.retention_recorded = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    def _row(self):
        return {"used_bytes": self.used, "metadata_bytes": self.metadata,
                "retained_count": self.retained,
                "retention_recorded": self.retention_recorded,
                "pressure_recorded": self.pressure_recorded,
                "limit_bytes": self.limit,
                "global_used_bytes": self.used,
                "global_limit_bytes": self.global_limit}

    def call_sync(self, method, params=None, *, timeout_s=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        if method == "pi.budget.snapshot":
            row = self._row()
            return {"self": row, "agents": [{"agent_id": "player1", **row}],
                    "limits": {"per_agent_bytes": self.limit,
                               "global_bytes": self.global_limit},
                    "global": {"used_bytes": self.used,
                               "limit_bytes": self.global_limit}}
        if method == "pi.budget.take":
            size = int(params.get("bytes", 0))
            allowed = self.used + size <= self.limit
            response: dict[str, Any] = {}
            if allowed:
                self.used += size
                if (not self.pressure_recorded and self.limit > 0
                        and self.used >= self.limit * 0.9):
                    self.pressure_recorded = True
                    response["pressure"] = {
                        "event": "pi_retention_pressure", "kind": "artifact",
                        "reason": "pi_artifact_max_bytes_pressure",
                        "used_bytes": self.used, "limit_bytes": self.limit,
                        "percent": 100.0 * self.used / self.limit,
                        "threshold": 0.9}
            else:
                self.retained += 1
                if not self.retention_recorded:
                    self.retention_recorded = True
                    response["retention"] = {
                        "event": "pi_artifact_retention",
                        "reason": "pi_artifact_max_bytes",
                        "limit_bytes": self.limit, "used_bytes": self.used,
                        "retained_count": self.retained}
            response.update({"allowed": allowed, **self._row()})
            return response
        if method == "pi.budget.charge_metadata":
            size = int(params.get("bytes", 0))
            self.used += size
            self.metadata += size
            return {"used_bytes": self.used, "metadata_bytes": self.metadata,
                    "retained_count": self.retained, "limit_bytes": self.limit,
                    "pressure_recorded": self.pressure_recorded,
                    "global_used_bytes": self.used,
                    "global_limit_bytes": self.global_limit}
        raise AssertionError(method)


class CentralArtifactBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.marker = self.root / "state" / "pi-artifact-budget.json"
        self.marker.parent.mkdir(parents=True, exist_ok=True)

    def central(self, fake, *, state_path=None, max_bytes=100):
        return ArtifactBudget(max_bytes, rpc_client=fake, agent_id="player1",
                              state_path=state_path if state_path is not None
                              else self.marker)

    def test_central_snapshot_loads_usage_and_ignores_a_forged_marker(self):
        fake = _FakeCentralBudget(limit=100)
        fake.used = 60
        self.marker.write_text(json.dumps({
            "schema_version": 1, "limit_bytes": 100, "used_bytes": 0,
            "retained_count": 0, "metadata_bytes": 0,
            "retention_recorded": False, "pressure_recorded": False}))
        budget = self.central(fake)
        self.assertEqual(budget.used_bytes, 60)
        self.assertEqual(budget.state_status, "central")
        self.assertFalse(any(method == "pi.budget.take" and params.get("bytes") == 0
                             for method, params in fake.calls))

    def test_take_and_metadata_are_charged_to_the_control_plane(self):
        fake = _FakeCentralBudget(limit=100)
        budget = self.central(fake)
        events: list[dict] = []
        budget._event = events.append
        self.assertEqual(budgeted_artifact_text(budget, "model_request", "a" * 60),
                         "a" * 60)
        self.assertEqual(fake.used, 60)
        projection = budgeted_artifact_text(budget, "model_response", "b" * 100)
        self.assertEqual(json.loads(projection)["retention"], "metadata_only")
        self.assertGreater(fake.metadata, 0)
        self.assertEqual(fake.retained, 1)
        self.assertEqual(budget.retained_count, 1)
        self.assertTrue(any(item.get("event") == "pi_artifact_retention"
                            for item in events))
        self.assertEqual(budget.snapshot()["global_limit_bytes"], 1000)

    def test_pressure_event_comes_from_the_control_plane(self):
        fake = _FakeCentralBudget(limit=100)
        budget = self.central(fake)
        events: list[dict] = []
        budget._event = events.append
        self.assertTrue(budget.take(90))
        self.assertTrue(budget.take(5))
        pressure = [item for item in events
                    if item.get("event") == "pi_retention_pressure"]
        self.assertEqual(len(pressure), 1)
        self.assertEqual(pressure[0]["limit_bytes"], 100)

    def test_central_rpc_failure_fails_closed(self):
        fake = _FakeCentralBudget(limit=100)
        fake.error = RuntimeError("control plane unavailable")
        budget = self.central(fake)
        self.assertEqual(budget.used_bytes, 100)
        self.assertEqual(budget.state_status, "conservative_central_unavailable")
        self.assertFalse(budget.take(1))
        self.assertTrue(budget.snapshot()["pressure_recorded"])





class LocalProxyAuthTests(unittest.TestCase):
    """The loopback proxy must reject shells that lack the per-run Pi credential."""

    def setUp(self):
        self.rpc = FakeRpc()
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)
        self.authenticated: list[int] = []

    def proxy(self, key: str = "local-pi-key") -> PiModelProxy:
        proxy = PiModelProxy(
            {"base_url": self.upstream.base_url, "model": "upstream-model",
             "timeout_s": 10}, self.rpc, agent_id="player1",
            client_api_key=key,
            on_client_authenticated=lambda: self.authenticated.append(1))
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def post(self, proxy, headers=None):
        client = httpx.Client(trust_env=False, timeout=10)
        try:
            return client.post(proxy.base_url + "/chat/completions",
                               json=request_body(stream=False), headers=headers or {})
        finally:
            client.close()

    def test_missing_or_wrong_credential_returns_401_without_spend(self):
        proxy = self.proxy()
        for headers in ({}, {"authorization": "Bearer wrong-key"},
                        {"authorization": "local-pi-key"}):
            with self.subTest(headers=headers):
                response = self.post(proxy, headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(json.loads(response.content)["error"]["type"],
                                 "invalid_request_error")
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.rpc.calls_for("budget.reserve"), [])
        self.assertEqual(self.rpc.settlements, [])
        self.assertEqual(self.authenticated, [])

    def test_models_endpoint_requires_the_credential(self):
        proxy = self.proxy()
        client = httpx.Client(trust_env=False, timeout=10)
        self.addCleanup(client.close)
        self.assertEqual(client.get(proxy.base_url + "/models").status_code, 401)
        response = client.get(proxy.base_url + "/models", headers={
            "authorization": "Bearer local-pi-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], "upstream-model")

    def test_correct_credential_authenticates_once_and_sets_upstream_call(self):
        self.upstream.script = [{"kind": "sse", "chunks": sse_chunks(
            "hello", usage=openai_usage(3, 2))}]
        proxy = self.proxy()
        response = self.post(proxy, {"authorization": "Bearer local-pi-key"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.rpc.settle_event.wait(5))
        self.assertEqual(len(self.authenticated), 1)
        # A second authenticated request does not re-trigger credential cleanup.
        self.upstream.script = [{"kind": "sse", "chunks": sse_chunks(
            "again", usage=openai_usage(3, 2))}]
        self.rpc.settle_event.clear()
        self.assertEqual(self.post(proxy, {"authorization": "Bearer local-pi-key"}).status_code,
                         200)
        self.assertTrue(self.rpc.settle_event.wait(5))
        self.assertEqual(len(self.authenticated), 1)
        self.assertEqual(len(self.upstream.requests), 2)



class PeerAllowlistTests(unittest.TestCase):
    """Only the spawned Pi pid may use the loopback proxy."""

    def setUp(self):
        self.rpc = FakeRpc()
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)

    def proxy(self) -> PiModelProxy:
        proxy = PiModelProxy(
            {"base_url": self.upstream.base_url, "model": "upstream-model",
             "timeout_s": 10}, self.rpc, agent_id="player1")
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def test_foreign_peer_is_closed_before_any_http_response(self):
        proxy = self.proxy()
        proxy.set_allowed_peer_pid(os.getpid() + 1)
        client = httpx.Client(trust_env=False, timeout=5)
        try:
            with self.assertRaises(httpx.TransportError):
                client.post(proxy.base_url + "/chat/completions",
                            json=request_body(stream=False))
        finally:
            client.close()
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.rpc.calls_for("budget.reserve"), [])
        self.assertEqual(self.rpc.settlements, [])

    def test_allowed_pid_receives_http_response(self):
        self.upstream.script = [{"kind": "sse", "chunks": sse_chunks(
            "hello", usage=openai_usage(3, 2))}]
        proxy = self.proxy()
        proxy.set_allowed_peer_pid(os.getpid())
        client = httpx.Client(trust_env=False, timeout=5)
        try:
            response = client.post(proxy.base_url + "/chat/completions",
                                   json=request_body(stream=False))
        finally:
            client.close()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.rpc.settle_event.wait(5))


if __name__ == "__main__":
    unittest.main()
