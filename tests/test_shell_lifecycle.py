"""Exercise the container exec protocol with a local, unprivileged docker shim.

No Docker daemon, Minecraft server or external model is used. The shim preserves
real process groups so cancellation checks actual child-process termination.
"""
import asyncio
import os
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from minecraft_harness.agents import WorkspaceTools


@unittest.skipUnless(shutil.which("setsid") and shutil.which("timeout"), "requires Linux session and timeout utilities")
class ShellLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        shim = self.bin / "docker"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "assert args.pop(0) == 'exec'\n"
            "if args[0] == '--workdir': del args[:2]\n"
            "args.pop(0)\n"
            f"args = [str(Path({str(self.jobs)!r}) / Path(x).name) if x.startswith('/tmp/harness-shell-') else x for x in args]\n"
            "os.setpgid(0, 0)  # Docker exec may already be a process-group leader.\n"
            "os.execvp(args[0], args)\n"
        )
        shim.chmod(0o700)
        self.path_patch = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.workspace = WorkspaceTools(self.root / "workspace", True, "fixture-container")

    @staticmethod
    def running(pid):
        path = Path(f"/proc/{pid}/stat")
        return path.exists() and path.read_text().split()[2] != "Z"

    async def test_stdout_is_drained_with_bounded_capture(self):
        result = await self.workspace.shell("python3 -c 'import sys; sys.stdout.write(\"x\" * 2000000)'", timeout_s=5)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["output"]), 65536)
        self.assertTrue(result["truncated"])

    async def test_cancellation_kills_container_command_and_its_child(self):
        pid_file = self.root / "child.pid"
        task = asyncio.create_task(self.workspace.shell(f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}; wait", timeout_s=30))
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        self.assertTrue(self.running(pid))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertFalse(self.running(pid))
        self.assertFalse(list(self.jobs.glob("*.pid")))

    async def test_successful_command_does_not_leave_background_children(self):
        pid_file = self.root / "background.pid"
        result = await self.workspace.shell(f"sleep 30 >/dev/null 2>&1 & echo $! > {shlex.quote(str(pid_file))}")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(self.running(int(pid_file.read_text())))

    async def test_container_timeout_kills_sleeping_children(self):
        pid_file = self.root / "timeout.pid"
        result = await self.workspace.shell(f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}; wait", timeout_s=1)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertFalse(self.running(int(pid_file.read_text())))

    async def test_sets_id_fork_preserves_command_failure_status(self):
        result = await self.workspace.shell("printf 'failure-fixture'; exit 17")
        self.assertEqual(result["exit_code"], 17)
        self.assertEqual(result["output"], "failure-fixture")

    async def test_existing_aggregate_overquota_prevents_any_shell_spawn(self):
        workspace = WorkspaceTools(self.root / "limited", True, "fixture-container", max_workspace_bytes=1024)
        (workspace.root / "large").write_bytes(b"x" * 2048)
        with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaisesRegex(ValueError, "quota"):
                await workspace.shell("touch should-not-start")
        spawn.assert_not_called()

    async def test_post_command_quota_fences_container_retains_files_and_can_recover(self):
        workspace = WorkspaceTools(self.root / "limited", True, "fixture-container", max_workspace_bytes=1024)
        path = workspace.root / "large"
        fenced = []
        async def fence():
            fenced.append(True)
            workspace._shell_container_stopped = True
        with patch.object(workspace, "_fence_workspace_container", fence):
            result = await workspace.shell(f"head -c 2048 /dev/zero > {shlex.quote(str(path))}")
        self.assertEqual(result["status"], "workspace_quota_exceeded")
        self.assertTrue(result["files_retained"])
        self.assertEqual(path.stat().st_size, 2048)
        self.assertEqual(len(fenced), 1)
        with self.assertRaises(ValueError):
            await workspace.shell("printf blocked")
        workspace.remove("large")
        with patch.object(workspace, "_workspace_container_command", AsyncMock(return_value="fixture")) as command:
            result = await workspace.shell("printf recovered")
        command.assert_awaited_once_with("start")
        self.assertEqual(result["output"], "recovered")
        self.assertEqual(result["exit_code"], 0)

    async def test_quota_watcher_interrupts_long_command_and_child(self):
        workspace = WorkspaceTools(self.root / "limited-watch", True, "fixture-container", max_workspace_bytes=1024)
        path = workspace.root / "large"
        pid_file = self.root / "watched-child.pid"
        command = (f"head -c 2048 /dev/zero > {shlex.quote(str(path))}; "
                   f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}; wait")
        fence = AsyncMock()
        with patch.object(workspace, "_fence_workspace_container", fence):
            result = await asyncio.wait_for(workspace.shell(command, timeout_s=30), 4)
        self.assertEqual(result["status"], "workspace_quota_exceeded")
        self.assertFalse(self.running(int(pid_file.read_text())))
        self.assertTrue(path.exists())
        fence.assert_awaited_once()

    async def test_empty_directories_can_be_reclaimed_without_recursive_deletion(self):
        empty = self.workspace.root / "empty"
        empty.mkdir()
        self.workspace.remove("empty")
        self.assertFalse(empty.exists())
        retained = self.workspace.root / "retained"
        retained.mkdir()
        (retained / "source.js").write_text("source")
        with self.assertRaises(OSError):
            self.workspace.remove("retained")
        self.assertTrue((retained / "source.js").exists())
