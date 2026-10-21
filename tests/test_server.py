"""Operator control failure paths using process fixtures, never Docker/Java."""
import asyncio
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock

from minecraft_harness.server import ServerControl


class FakeRecorder:
    def __init__(self):
        self.events = []
    def append(self, kind, payload):
        self.events.append((kind, payload))


class ServerControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_helpers_have_deadlines_and_stop_verifies_container_state(self):
        recorder = FakeRecorder()
        control = ServerControl({'managed_container': 'research-server'}, recorder)
        calls = []
        async def docker(*args, **kwargs):
            calls.append((args, kwargs))
            return 'false\n' if args[0] == 'inspect' else ''
        with patch.object(control, '_docker', side_effect=docker):
            await control.stop(timeout_s=4)
        self.assertIn('timeout', calls[0][0])
        self.assertIn('--kill-after=2', calls[0][0])
        self.assertEqual(calls[-1][0][0], 'inspect')
        self.assertEqual(recorder.events[-1][0], 'world_stopped')
        with patch.object(control, '_docker', AsyncMock(return_value='true\n')):
            with self.assertRaisesRegex(RuntimeError, 'still running'):
                await control.stop(timeout_s=4)

    async def test_startup_is_bounded_by_one_deadline(self):
        control = ServerControl({'managed_container': 'research-server'}, FakeRecorder())
        async def docker(*args, **kwargs):
            if args[0] == 'exec':
                raise RuntimeError('not ready')
            return ''
        start = time.monotonic()
        with patch.object(control, '_docker', side_effect=docker):
            with self.assertRaisesRegex(RuntimeError, 'startup deadline'):
                await control.start(timeout_s=0.05)
        self.assertLess(time.monotonic() - start, 0.5)

    async def test_timeout_and_cancellation_kill_local_docker_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'docker'
            pid_file = root / 'child.pid'
            executable.write_text('#!/bin/sh\nsleep 60 &\nprintf "%s" "$!" > "$HARNESS_CHILD_PID"\nwait\n')
            executable.chmod(0o755)
            env = {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'HARNESS_CHILD_PID': str(pid_file)}
            control = ServerControl({}, FakeRecorder())
            with patch.dict(os.environ, env):
                with self.assertRaisesRegex(RuntimeError, 'remote operation may already have executed'):
                    await control._docker('exec', timeout_s=0.1)
                child = int(pid_file.read_text())
                self.assertTrue(_dead_or_zombie(child))
                pid_file.unlink()
                task = asyncio.create_task(control._docker('exec', timeout_s=10))
                deadline = time.monotonic() + 2
                while not pid_file.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(pid_file.exists())
                child = int(pid_file.read_text())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(_dead_or_zombie(child))

    async def test_pending_restore_blocks_server_start(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'server-data'
            data.mkdir()
            (data.parent / '.server-data.harness-restore.json').write_text('{}')
            control = ServerControl({'managed_container': 'research-server', 'world_dir': str(data / 'world')}, FakeRecorder())
            with patch.object(control, '_docker', AsyncMock()) as docker:
                with self.assertRaisesRegex(RuntimeError, 'Interrupted world restore'):
                    await control.start()
                docker.assert_not_called()


def _dead_or_zombie(pid):
    try:
        os.kill(pid, 0)
        return Path(f'/proc/{pid}/stat').read_text().split()[2] == 'Z'
    except ProcessLookupError:
        return True
