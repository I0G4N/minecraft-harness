import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from minecraft_harness.program_cli import invoke


class ProgramCliTests(unittest.IsolatedAsyncioTestCase):
    async def _invoke(self, handler, wait_ms=1000):
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "action.js"
            source.write_text("return bot.health")
            args = argparse.Namespace(url="http://runtime.test", agent="alice", token_env="TEST_PLAYER_TOKEN", command="exec", file=source, request_id="stable-request", timeout_ms=60000, wait_ms=wait_ms)
            with patch.dict(os.environ, {"TEST_PLAYER_TOKEN": "test-player-token"}), patch("minecraft_harness.program_cli.httpx.AsyncClient", client):
                return await invoke(args)

    async def test_lost_status_connection_preserves_execution_identity(self):
        async def handle(request):
            if request.url.path.endswith("/sessions/alice"):
                return httpx.Response(200, json={"run_id": "run-current", "world_epoch": 7})
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual((body["run_id"], body["world_epoch"]), ("run-current", 7))
                return httpx.Response(202, json={"status": "running", "execution_id": "known-execution", "request_id": "stable-request"})
            raise httpx.ReadError("connection lost", request=request)
        result = await self._invoke(handle)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["execution_id"], "known-execution")
        self.assertEqual(result["request_id"], "stable-request")

    async def test_wait_deadline_bounds_status_request(self):
        async def handle(request):
            if request.url.path.endswith("/sessions/alice"):
                return httpx.Response(200, json={"run_id": "run", "world_epoch": 0})
            if request.method == "POST":
                return httpx.Response(202, json={"status": "running", "execution_id": "known"})
            await asyncio.sleep(1)
            return httpx.Response(200, json={"status": "succeeded", "execution_id": "known"})
        started = time.monotonic()
        result = await self._invoke(handle, 150)
        self.assertTrue(result["wait_expired"])
        self.assertEqual(result["execution_id"], "known")
        self.assertLess(time.monotonic() - started, 0.6)
