"""Wire-protocol, authentication and lifecycle tests for ``runner_rpc``."""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# The repository is a src-layout package; make the documented disposable-container
# command (``python -m unittest tests.test_runner_rpc``) importable without an install.
_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from minecraft_harness.agents.core import ModelAdmissionError
from minecraft_harness.budget import BudgetExhausted
from minecraft_harness.execution.gateway import RuntimeReliabilityError, RuntimeUnavailable
from minecraft_harness.memory.store import MemoryConflict, MemoryPermissionError
from minecraft_harness.recorder import StorageLimitError
from minecraft_harness.runner_rpc import RpcClient, RpcError, RpcServer

TOKENS = {
    "token-player1": {"agent_id": "player1", "role": "player"},
    "token-player2": {"agent_id": "player2", "role": "player"},
    "token-observer": {"agent_id": "observer", "role": "observer"},
}

KNOWN_EXCEPTIONS = (
    BudgetExhausted,
    ModelAdmissionError,
    StorageLimitError,
    RuntimeUnavailable,
    RuntimeReliabilityError,
    MemoryConflict,
    MemoryPermissionError,
    PermissionError,
    FileNotFoundError,
    ValueError,
    TimeoutError,
)


def encode_frame(value):
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


async def read_raw_frame(reader):
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    payload = await reader.readexactly(length)
    return json.loads(payload.decode("utf-8"))


class RpcSyncClientTests(unittest.TestCase):
    """The runner's sync proxies call the control plane from inside its own event loop."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.socket_path = self.root / "control" / "runner.sock"
        self.server = None
        self.loop = None
        self.thread = None

    def start_server(self, handler):
        self.server = RpcServer(self.socket_path, TOKENS, handler)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="rpc-test-server", daemon=True)
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.server.start(), self.loop).result(10)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        if self.loop is None:
            return
        try:
            if self.server is not None:
                asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
            self.loop.close()
            self.loop = None

    def client(self, token="token-player1", agent_id="player1", role="player", **kwargs):
        return RpcClient(self.socket_path, token, agent_id, role, **kwargs)

    def test_sync_roundtrip(self):
        async def handler(agent_id, role, method, params):
            return {"method": method, "params": params, "agent_id": agent_id, "role": role}

        self.start_server(handler)
        result = self.client().call_sync("recorder.latest_event_seq")
        self.assertEqual(result, {"method": "recorder.latest_event_seq", "params": {},
                                  "agent_id": "player1", "role": "player"})
        result = self.client().call_sync("recorder.append", {"kind": "bot_chat", "payload": {"text": "hi"}})
        self.assertEqual(result["params"], {"kind": "bot_chat", "payload": {"text": "hi"}})

    def test_sync_client_usable_inside_asyncio_event_loop(self):
        async def handler(agent_id, role, method, params):
            return {"pong": True}

        self.start_server(handler)
        client = self.client()

        async def main():
            return client.call_sync("ping")

        self.assertEqual(asyncio.run(main()), {"pong": True})

    def test_sync_handshake_auth_rejection(self):
        async def handler(agent_id, role, method, params):
            return {"pong": True}

        self.start_server(handler)
        with self.assertRaises(RpcError) as raised:
            self.client(token="forged-token").call_sync("ping")
        self.assertEqual(raised.exception.error_type, "PermissionError")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(str(raised.exception), "authentication failed")
        # A rejected connection must not disturb the server for valid runners.
        self.assertEqual(self.client().call_sync("ping"), {"pong": True})

    def test_sync_handler_exception_type_and_retryable_preserved(self):
        by_name = {cls.__name__: cls for cls in KNOWN_EXCEPTIONS}

        async def handler(agent_id, role, method, params):
            raise by_name[params["kind"]](params["message"])

        self.start_server(handler)
        client = self.client()
        for cls in KNOWN_EXCEPTIONS:
            with self.subTest(error_type=cls.__name__):
                message = f"boom:{cls.__name__}"
                with self.assertRaises(RpcError) as raised:
                    client.call_sync("explode", {"kind": cls.__name__, "message": message})
                error = raised.exception
                self.assertEqual(error.error_type, cls.__name__)
                self.assertEqual(str(error), message)
                self.assertEqual(error.retryable, bool(getattr(cls, "retryable", False)))

    def test_sync_two_mib_payload_roundtrip(self):
        blob = "y" * (2 * 1024 * 1024)

        async def handler(agent_id, role, method, params):
            text = params["text"]
            return {"length": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}

        self.start_server(handler)
        result = self.client().call_sync("recorder.put_artifact", {"text": blob})
        self.assertEqual(result, {"length": len(blob),
                                  "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()})

    def test_sync_no_read_timeout_by_default(self):
        async def handler(agent_id, role, method, params):
            await asyncio.sleep(0.15)
            return {"slow": True}

        self.start_server(handler)
        started = time.monotonic()
        self.assertEqual(self.client().call_sync("gateway.wait"), {"slow": True})
        self.assertGreaterEqual(time.monotonic() - started, 0.1)

    def test_sync_timeout_s_raises_timeout_error(self):
        async def handler(agent_id, role, method, params):
            await asyncio.sleep(5)
            return None

        self.start_server(handler)
        with self.assertRaises(RpcError) as raised:
            self.client().call_sync("gateway.execute", timeout_s=0.2)
        self.assertEqual(raised.exception.error_type, "TimeoutError")
        self.assertTrue(raised.exception.retryable)


class RpcAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.socket_path = self.root / "runtime" / "control.sock"
        self.server = None

    async def start_server(self, handler, **kwargs):
        self.server = RpcServer(self.socket_path, TOKENS, handler, **kwargs)
        await self.server.start()
        self.addAsyncCleanup(self.close_server)
        return self.server

    async def close_server(self):
        if self.server is not None:
            await self.server.close()

    def client(self, token="token-player1", agent_id="player1", role="player", **kwargs):
        return RpcClient(self.socket_path, token, agent_id, role, **kwargs)

    async def test_async_roundtrip_and_binding(self):
        calls = []

        async def handler(agent_id, role, method, params):
            calls.append((agent_id, role, method, params))
            return {"method": method, "params": params, "agent_id": agent_id, "role": role}

        await self.start_server(handler)
        result = await self.client().call("budget.reserve", {"purpose": "agent_decision", "role": "player"})
        self.assertEqual(result, {"method": "budget.reserve",
                                  "params": {"purpose": "agent_decision", "role": "player"},
                                  "agent_id": "player1", "role": "player"})
        self.assertEqual(calls, [("player1", "player", "budget.reserve",
                                  {"purpose": "agent_decision", "role": "player"})])

    async def test_identity_override_from_client_is_ignored(self):
        async def handler(agent_id, role, method, params):
            return {"agent_id": agent_id, "role": role}

        await self.start_server(handler)
        liar = RpcClient(self.socket_path, "token-player1", "observer", "observer")
        self.assertEqual(await liar.call("supervisor.observer_infra_issue"),
                         {"agent_id": "player1", "role": "player"})

    async def test_async_handler_exception_preserved(self):
        async def handler(agent_id, role, method, params):
            raise ValueError("params must be an object")

        await self.start_server(handler)
        with self.assertRaises(RpcError) as raised:
            await self.client().call("recorder.append", {"kind": "bot_chat"})
        self.assertEqual(raised.exception.error_type, "ValueError")
        self.assertEqual(str(raised.exception), "params must be an object")
        self.assertFalse(raised.exception.retryable)

    async def test_two_mib_payload_roundtrip(self):
        blob = "x" * (2 * 1024 * 1024)

        async def handler(agent_id, role, method, params):
            text = params["text"]
            return {"length": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}

        await self.start_server(handler)
        result = await self.client().call("recorder.put_artifact", {"text": blob})
        self.assertEqual(result, {"length": len(blob),
                                  "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()})

    async def test_concurrent_async_calls_from_one_client(self):
        async def handler(agent_id, role, method, params):
            await asyncio.sleep(params["delay"])
            return {"value": params["value"], "agent_id": agent_id}

        await self.start_server(handler)
        client = self.client()
        results = await asyncio.gather(*(
            client.call("gateway.execute", {"value": index, "delay": (12 - index) * 0.005})
            for index in range(12)))
        self.assertEqual([result["value"] for result in results], list(range(12)))
        self.assertEqual({result["agent_id"] for result in results}, {"player1"})

    async def test_cancel_agent_cancels_inflight_handler(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(agent_id, role, method, params):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        await self.start_server(handler)
        call = asyncio.create_task(self.client().call("gateway.execute", {"source": "while True: pass"}))
        await asyncio.wait_for(started.wait(), timeout=5)
        self.assertEqual(await self.server.cancel_agent("player1"), 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(await self.server.cancel_agent("player1"), 0)
        with self.assertRaises(RpcError):
            await asyncio.wait_for(call, timeout=5)

    async def test_client_disconnect_cancels_handler(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(agent_id, role, method, params):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        await self.start_server(handler)
        call = asyncio.create_task(self.client().call("gateway.execute", {"source": "sleep 3600"}))
        await asyncio.wait_for(started.wait(), timeout=5)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(cancelled.wait(), timeout=5)

    async def test_pipelined_request_is_a_protocol_error(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(agent_id, role, method, params):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        await self.start_server(handler)
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(encode_frame({"id": 0, "method": "hello",
                                       "params": {"token": "token-player1", "agent_id": "player1", "role": "player"}}))
            await writer.drain()
            self.assertTrue((await read_raw_frame(reader))["ok"])
            writer.write(encode_frame({"id": 1, "method": "gateway.execute", "params": {}}))
            await writer.drain()
            await asyncio.wait_for(started.wait(), timeout=5)
            writer.write(encode_frame({"id": 2, "method": "runner.heartbeat", "params": {}}))
            await writer.drain()
            await asyncio.wait_for(cancelled.wait(), timeout=5)
            self.assertEqual(await asyncio.wait_for(reader.read(1), timeout=5), b"")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_close_unlinks_socket_and_sets_permissions(self):
        async def handler(agent_id, role, method, params):
            return {}

        await self.start_server(handler)
        info = self.socket_path.lstat()
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.socket_path.parent.stat().st_mode), 0o700)
        await self.server.close()
        self.assertFalse(self.socket_path.exists())
        await self.server.close()  # closing twice is safe

    async def test_close_cancels_inflight_handler_and_connections(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(agent_id, role, method, params):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        await self.start_server(handler)
        call = asyncio.create_task(self.client().call("gateway.execute", {"source": "sleep 3600"}))
        await asyncio.wait_for(started.wait(), timeout=5)
        await self.server.close()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.socket_path.exists())
        with self.assertRaises(RpcError):
            await asyncio.wait_for(call, timeout=5)

    async def test_stale_non_socket_at_socket_path_is_not_unlinked(self):
        async def handler(agent_id, role, method, params):
            return {}

        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        regular = parent / "regular.sock"
        regular.write_text("not a socket")
        with self.assertRaises(ValueError):
            await RpcServer(regular, TOKENS, handler).start()
        self.assertEqual(regular.read_text(), "not a socket")
        self.assertTrue(stat.S_ISREG(regular.lstat().st_mode))

        directory = parent / "directory.sock"
        directory.mkdir()
        with self.assertRaises(ValueError):
            await RpcServer(directory, TOKENS, handler).start()
        self.assertTrue(directory.is_dir())

        target = parent / "target.txt"
        target.write_text("target")
        link = parent / "link.sock"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            await RpcServer(link, TOKENS, handler).start()
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "target")

    async def test_stale_socket_is_replaced(self):
        async def handler(agent_id, role, method, params):
            return {"pong": True}

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale.bind(str(self.socket_path))
        finally:
            stale.close()
        self.assertTrue(stat.S_ISSOCK(self.socket_path.lstat().st_mode))
        await self.start_server(handler)
        self.assertEqual(await self.client().call("ping"), {"pong": True})

    async def test_invalid_frame_closes_connection(self):
        async def handler(agent_id, role, method, params):
            return {}

        await self.start_server(handler)
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(encode_frame({"id": 0, "method": "hello",
                                       "params": {"token": "token-player1", "agent_id": "player1", "role": "player"}}))
            await writer.drain()
            self.assertTrue((await read_raw_frame(reader))["ok"])
            writer.write(struct.pack(">I", 8) + b"not-json")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(1), timeout=5), b"")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_oversize_incoming_frame_closes_connection(self):
        async def handler(agent_id, role, method, params):
            return {}

        await self.start_server(handler, max_frame_bytes=1024)
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(encode_frame({"id": 0, "method": "hello",
                                       "params": {"token": "token-player1", "agent_id": "player1", "role": "player"}}))
            await writer.drain()
            self.assertTrue((await read_raw_frame(reader))["ok"])
            writer.write(struct.pack(">I", 1025))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(1), timeout=5), b"")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_client_rejects_oversize_response(self):
        async def handler(agent_id, role, method, params):
            return {"blob": "x" * 2048}

        await self.start_server(handler)
        with self.assertRaises(RpcError) as raised:
            await self.client(max_frame_bytes=512).call("recorder.read_events")
        self.assertEqual(raised.exception.error_type, "ProtocolError")

    async def test_server_reports_oversized_result_as_error_frame(self):
        async def handler(agent_id, role, method, params):
            return {"blob": "x" * 2048}

        await self.start_server(handler, max_frame_bytes=256)
        with self.assertRaises(RpcError) as raised:
            await self.client().call("recorder.read_events")
        self.assertEqual(raised.exception.error_type, "ValueError")


if __name__ == "__main__":
    unittest.main()
