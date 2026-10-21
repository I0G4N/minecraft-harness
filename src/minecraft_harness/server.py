"""Operator-only lifecycle with bounded, cancellation-safe Docker control."""
from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from pathlib import Path

from .world_checkpoints import has_interrupted_restore


class ServerControl:
    def __init__(self, config, recorder):
        self.config, self.recorder = config, recorder
        self.container = config.get("managed_container")
        if self.container and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.container):
            raise ValueError("invalid managed_container name")
        self.timeout_s = float(config.get("control_timeout_s", 90))
        self.startup_timeout_s = float(config.get("startup_timeout_s", 120))

    async def _docker(self, *arguments, timeout_s=None):
        process = await asyncio.create_subprocess_exec("docker", *arguments, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout_s or self.timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            # Killing docker exec's client does not cancel its process inside a
            # container. All exec helpers also have a remote timeout, below.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.shield(process.wait())
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeError("Docker control deadline exceeded; remote operation may already have executed; inspect server state before retrying") from None
        if process.returncode:
            raise RuntimeError("Docker server control failed: " + err.decode(errors="replace")[-1000:])
        return out.decode(errors="replace")

    async def save(self, *, timeout_s=None):
        if self.container:
            duration = min(timeout_s or self.timeout_s, self.timeout_s)
            # The inner timeout survives disconnection of the Docker client.
            # A killed helper releases save.lock via its TERM trap.
            remote_duration = max(1, int(duration - 2))
            await self._docker("exec", self.container, "timeout", "--signal=TERM", "--kill-after=2",
                               str(remote_duration), "harness-control", "save", timeout_s=duration)
            self.recorder.append("world_saved", {"method": "save-all flush", "container": self.container})

    async def stop(self, *, timeout_s=None):
        if not self.container:
            raise RuntimeError("world freeze requires server.managed_container; reasoning-only pause is available")
        deadline = time.monotonic() + (timeout_s or self.timeout_s)
        await self.save(timeout_s=max(0.001, deadline - time.monotonic()))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Server stop deadline exceeded after saving")
        await self._docker("stop", "--time", str(max(1, min(45, int(remaining - 2)))), self.container, timeout_s=remaining)
        # A successful docker stop is the prerequisite for host filesystem
        # checkpoint work; an uncertain stop must not certify a frozen world.
        state = await self._docker("inspect", "--format", "{{.State.Running}}", self.container,
                                   timeout_s=max(0.001, deadline - time.monotonic()))
        if state.strip() != "false":
            raise RuntimeError("Minecraft container is still running after stop")
        self.recorder.append("world_stopped", {"container": self.container})

    async def ready(self, *, timeout_s=None):
        if not self.container:
            raise RuntimeError("no managed world container configured")
        await self._docker("exec", self.container, "timeout", "--signal=TERM", "--kill-after=1", "3",
                           "harness-control", "ready", timeout_s=timeout_s or min(5, self.timeout_s))
        return True

    async def start(self, *, timeout_s=None):
        if not self.container:
            raise RuntimeError("no managed world container configured")
        if self.config.get("world_dir") and has_interrupted_restore(Path(self.config["world_dir"]).parent):
            raise RuntimeError("Interrupted world restore detected; recover_interrupted_restore with the server stopped before starting Minecraft")
        deadline = time.monotonic() + (timeout_s or self.startup_timeout_s)
        await self._docker("start", self.container, timeout_s=min(self.timeout_s, max(0.001, deadline - time.monotonic())))
        last_error = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                await self.ready(timeout_s=min(5, remaining))
                self.recorder.append("server_started", {"container": self.container})
                return
            except RuntimeError as error:
                last_error = error
            await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
        raise RuntimeError(f"Minecraft server did not become ready within startup deadline: {last_error}")
