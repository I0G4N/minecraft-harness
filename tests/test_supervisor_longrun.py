"""Exercise operator signals through the real CLI and owned fake Node runtime."""
import asyncio
import json
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from minecraft_harness.supervisor import is_active


def child_pids(pid):
    result, stack = set(), [pid]
    while stack:
        parent = stack.pop()
        try:
            children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
        except FileNotFoundError:
            continue
        for child in map(int, children):
            if child not in result:
                result.add(child)
                stack.append(child)
    return result


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


@unittest.skipUnless(shutil.which("node") and Path("/proc").is_dir(), "requires Linux and Node")
class SupervisorLongRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_sigkill_then_resume_cleans_old_runtime_without_replaying_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, run_dir = root / "fixture.json", root / "run"
            config = {"agents": {"count": 1, "max_turns": 100, "max_no_progress_turns": 100},
                      "model": {"kind": "fake", "script": [{"tool_calls": [{"id": "long", "type": "function", "function": {
                          "name": "mc_execute", "arguments": {"source": "console.log('kill fixture'); await bot.fake.wait(30000); return 1;", "timeout_ms": 10000}}}]}]},
                      "runtime": {"mode": "fake", "heartbeat_interval_s": .2, "lease_timeout_s": 8,
                                  "program_timeout_default_s": 10, "program_timeout_max_s": 10},
                      "budgets": {"wall_time_minutes": 1}, "supervision": {"poll_interval_s": .05}}
            config_path.write_text(json.dumps(config))
            first = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "minecraft_harness", "run", "--config", str(config_path), "--run-dir", str(run_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            resumed, tracked = None, set()

            def state():
                with closing(sqlite3.connect(run_dir / "events.sqlite3")) as connection:
                    return {key: json.loads(value) for key, value in connection.execute("SELECT key,value_json FROM state")}

            try:
                for _ in range(300):
                    tracked.update(child_pids(first.pid))
                    try:
                        with closing(sqlite3.connect(run_dir / "events.sqlite3")) as connection:
                            running = connection.execute("SELECT COUNT(*) FROM events WHERE kind='execution_running'").fetchone()[0] > 0
                    except sqlite3.OperationalError:
                        running = False
                    if running:
                        break
                    await asyncio.sleep(.02)
                self.assertTrue(running)
                before = state()
                self.assertTrue(before["owned_runtime"]["instance_id"])
                self.assertEqual(before["budget"]["calls"], 1)
                tracked.update(child_pids(first.pid))
                first.kill()
                await asyncio.wait_for(first.communicate(), 5)
                self.assertFalse(is_active(run_dir))
                resumed = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "minecraft_harness", "resume", str(run_dir),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                ready = False
                for _ in range(700):
                    tracked.update(child_pids(resumed.pid))
                    current = state()
                    if current.get("status") == "running" and current.get("world_epoch", 0) >= 1 and current["budget"]["calls"] >= 2:
                        ready = True
                        break
                    if resumed.returncode is not None:
                        break
                    await asyncio.sleep(.02)
                if not ready:
                    output, errors = await asyncio.wait_for(resumed.communicate(), 5)
                    self.fail(f"resume failed: {output.decode()} {errors.decode()}")
                self.assertGreaterEqual(current["budget"]["calls"], before["budget"]["calls"])
                self.assertGreater(current["world_epoch"], before.get("world_epoch", 0))
                self.assertFalse(alive(before["owned_runtime"]["pid"]), "old Node parent survived marker-based recovery")
                resumed.send_signal(signal.SIGTERM)
                _, errors = await asyncio.wait_for(resumed.communicate(), 20)
                self.assertEqual(resumed.returncode, 128 + signal.SIGTERM, errors.decode())
                for _ in range(100):
                    if not any(alive(pid) for pid in tracked):
                        break
                    await asyncio.sleep(.02)
                self.assertFalse([pid for pid in tracked if alive(pid)])
                with closing(sqlite3.connect(run_dir / "events.sqlite3")) as connection:
                    duplicates = list(connection.execute("SELECT execution_id,COUNT(*) FROM events WHERE kind='execution_running' GROUP BY execution_id HAVING COUNT(*)>1"))
                    self.assertEqual(duplicates, [])
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM submissions WHERE agent_id='player1' AND request_id='player1:1:long'").fetchone()[0], 1)
                    self.assertGreaterEqual(connection.execute("SELECT COUNT(*) FROM events WHERE kind='owned_runtime_processes_fenced'").fetchone()[0], 1)
                self.assertFalse(state()["budget"]["pending"])
                self.assertFalse(is_active(run_dir))
            finally:
                for process in (first, resumed):
                    if process and process.returncode is None:
                        process.kill()
                        await process.wait()
                for pid in tracked:
                    if alive(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    async def test_sigterm_cli_fences_inflight_action_and_releases_every_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, run_dir = root / "fixture.json", root / "run"
            config = {"agents": {"count": 1, "max_turns": 100, "max_no_progress_turns": 100},
                      "model": {"kind": "fake", "script": [{"tool_calls": [{"id": "long", "type": "function", "function": {
                          "name": "mc_execute", "arguments": {"source": "console.log('signal fixture'); await bot.fake.wait(30000); return 1;", "timeout_ms": 10000}}}]}]},
                      "runtime": {"mode": "fake", "heartbeat_interval_s": .1, "lease_timeout_s": 3,
                                  "program_timeout_default_s": 10, "program_timeout_max_s": 10},
                      "budgets": {"wall_time_minutes": 1}, "supervision": {"poll_interval_s": .05}}
            config_path.write_text(json.dumps(config))
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "minecraft_harness", "run", "--config", str(config_path), "--run-dir", str(run_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            tracked = set()
            try:
                running = False
                for _ in range(300):
                    tracked.update(child_pids(process.pid))
                    database = run_dir / "events.sqlite3"
                    if database.exists():
                        try:
                            with closing(sqlite3.connect(database)) as connection:
                                running = connection.execute("SELECT COUNT(*) FROM events WHERE kind='execution_running'").fetchone()[0] > 0
                        except sqlite3.OperationalError:
                            pass
                    if running or process.returncode is not None:
                        break
                    await asyncio.sleep(.02)
                self.assertTrue(running, "fixture action never reached the real fake Node worker")
                tracked.update(child_pids(process.pid))
                process.send_signal(signal.SIGTERM)
                output, errors = await asyncio.wait_for(process.communicate(), 20)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, errors.decode() + output.decode())
                for _ in range(100):
                    if not any(alive(pid) for pid in tracked):
                        break
                    await asyncio.sleep(.02)
                self.assertFalse([pid for pid in tracked if alive(pid)], "owned Node worker/guardian survived CLI shutdown")
                self.assertFalse(is_active(run_dir))
                with closing(sqlite3.connect(run_dir / "events.sqlite3")) as connection:
                    state = {key: json.loads(value) for key, value in connection.execute("SELECT key,value_json FROM state")}
                    self.assertEqual(state["status"], "stopped")
                    self.assertFalse(state["budget"]["pending"])
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE kind='run_finished'").fetchone()[0], 1)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                # Only the fixture's recorded descendants are eligible for test
                # cleanup, including when the assertion intentionally fails.
                for pid in tracked:
                    if alive(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
