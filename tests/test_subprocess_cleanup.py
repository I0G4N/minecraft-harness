"""Real pipe-pressure regressions for bounded Docker CLI cleanup, without Docker."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from minecraft_harness.agents import WorkspaceTools
from minecraft_harness.workspaces import WorkspaceProvisioner


class CapturedSubprocessCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.provisioner = WorkspaceProvisioner({'network': 'none'}, self.root, run_id='fixture')
        self.workspace = WorkspaceTools(self.root / 'workspace', True, 'fixture-container')
        self.real_spawn = asyncio.create_subprocess_exec
        self.children = []

    async def asyncTearDown(self):
        # Failure cleanup prevents the regression fixture itself leaking a child.
        for child in self.children:
            if child.returncode is None:
                try:
                    os.killpg(child.pid, 9)
                except ProcessLookupError:
                    pass
            for stream in (child.stdout, child.stderr):
                transport = getattr(stream, '_transport', None)
                if transport is not None:
                    transport.close()
            await asyncio.wait_for(child.wait(), 2)

    def fixture_spawn(self, script, started=None):
        async def spawn(*arguments, **kwargs):
            child = await self.real_spawn(sys.executable, '-c', script, **kwargs)
            self.children.append(child)
            if started is not None:
                started.set()
            return child
        return spawn

    def assert_reaped(self, child):
        self.assertIsNotNone(child.returncode)
        self.assertEqual(child.returncode, -9)
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                self.assertTrue(stream._transport.is_closing())

    async def test_overflow_kills_reaps_and_closes_full_pipes_for_both_callers(self):
        # Both writes exceed the asyncio pipe high-water mark. A killed process
        # alone is insufficient: wait() used to hang on unread pipe transports.
        script = 'import os,time; os.write(1,b"x"*4_000_000); time.sleep(30)'
        for operation in (lambda: self.provisioner._docker('inspect', 'fixture', timeout=.5),
                          lambda: self.workspace._workspace_container_command('inspect')):
            with self.subTest(operation=operation):
                start = time.monotonic()
                with patch('asyncio.create_subprocess_exec', self.fixture_spawn(script)):
                    async with asyncio.timeout(3):
                        with self.assertRaisesRegex(RuntimeError, 'bounded inspection|excessive output'):
                            await operation()
                self.assertLess(time.monotonic() - start, 2.5)
                self.assert_reaped(self.children[-1])

    async def test_inspection_timeout_reaps_child_without_late_pipe_writes(self):
        # stderr is intentionally drained/discarded rather than an overflow
        # error, so this exercises the actual operation timeout path.
        script = 'import os,time; os.write(2,b"x"*4_000_000); time.sleep(30)'
        with patch('asyncio.create_subprocess_exec', self.fixture_spawn(script)):
            async with asyncio.timeout(3):
                with self.assertRaises(TimeoutError):
                    await self.provisioner._docker('inspect', 'fixture', timeout=.1)
        self.assert_reaped(self.children[-1])

    async def test_cancellation_reaps_both_control_and_inspection_children(self):
        for operation in (lambda: self.provisioner._docker('inspect', 'fixture'),
                          lambda: self.workspace._workspace_container_command('inspect')):
            with self.subTest(operation=operation):
                started = asyncio.Event()
                with patch('asyncio.create_subprocess_exec', self.fixture_spawn('import time; time.sleep(30)', started)):
                    task = asyncio.create_task(operation())
                    await asyncio.wait_for(started.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 3)
                self.assert_reaped(self.children[-1])

    async def test_already_exited_parent_with_inherited_child_pipe_is_fenced(self):
        pid_file = self.root / 'descendant.pid'
        script = ('import subprocess,sys; '
                  'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
                  f'open({str(pid_file)!r},"w").write(str(child.pid))')
        with patch('asyncio.create_subprocess_exec', self.fixture_spawn(script)):
            async with asyncio.timeout(3):
                with self.assertRaises(TimeoutError):
                    await self.provisioner._docker('inspect', 'fixture', timeout=.2)
        child = self.children[-1]
        self.assertEqual(child.returncode, 0)  # The pipe holder outlived its parent.
        descendant = int(pid_file.read_text())
        status = Path(f'/proc/{descendant}/stat')
        for _ in range(100):
            if not status.exists() or status.read_text().split()[2] == 'Z':
                break
            await asyncio.sleep(.005)
        self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
        for stream in (child.stdout, child.stderr):
            self.assertTrue(stream._transport.is_closing())
