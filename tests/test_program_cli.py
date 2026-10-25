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


class RenderProgramTextTests(unittest.TestCase):
    """The paper text output keeps a bounded source location for errors.

    Agents edit generated programs by hand; printing only ``Error: <message>``
    for a SyntaxError loses the line/caret and makes them repeat the same bad
    edit. Keep the added context compact.
    """

    def test_success_output_prints_logs_then_returned(self):
        from minecraft_harness.program_cli import render_program_text
        text = render_program_text({"status": "succeeded", "logs": [{"message": "hello"}], "result": "ok"})
        self.assertEqual(text, "hello\nReturned: ok")

    def test_syntax_error_renders_line_code_and_caret(self):
        from minecraft_harness.program_cli import render_program_text
        result = {
            "status": "failed",
            "error": {
                "name": "SyntaxError",
                "message": "Unexpected token ';'",
                "stack": (
                    "execution-abc.js:11\n"
                    "for (let dz = -10;__mcWindowCheck(); dz <= 10; dz++) {\n"
                    "                                                         ^\n"
                    "\nSyntaxError: Unexpected token ';'"
                ),
            },
        }
        lines = render_program_text(result).splitlines()
        self.assertEqual(lines[0], "Error: Unexpected token ';'")
        self.assertEqual(lines[1], "  at execution-abc.js:11")
        self.assertIn("__mcWindowCheck", lines[2])
        self.assertEqual(lines[3].strip(), "^")

    def test_runtime_error_renders_first_execution_frame(self):
        from minecraft_harness.program_cli import render_program_text
        result = {
            "status": "failed",
            "error": {
                "name": "TypeError",
                "message": "x is not a function",
                "stack": "TypeError: x is not a function\n    at execution-def.js:3:7\n    at foo",
            },
        }
        self.assertEqual(
            render_program_text(result).splitlines(),
            ["Error: x is not a function", "  at execution-def.js:3:7", "Status: failed"],
        )

    def test_error_without_stack_stays_compact(self):
        from minecraft_harness.program_cli import render_program_text
        result = {"status": "failed", "error": {"name": "Error", "message": "boom"}}
        self.assertEqual(render_program_text(result), "Error: boom\nStatus: failed")

    def test_non_dict_error_and_status_line(self):
        from minecraft_harness.program_cli import render_program_text
        result = {"status": "timed_out", "error": "timeout", "result": None}
        self.assertEqual(render_program_text(result), "Error: timeout\nStatus: timed_out")

    def test_location_is_bounded(self):
        from minecraft_harness.program_cli import render_program_text
        long_code = "x" * 1000
        result = {
            "status": "failed",
            "error": {
                "name": "SyntaxError", "message": "bad",
                "stack": f"execution-abc.js:1\n{long_code}\n{' ' * 10}^\n",
            },
        }
        lines = render_program_text(result).splitlines()
        self.assertTrue(all(len(line) <= 250 for line in lines))
