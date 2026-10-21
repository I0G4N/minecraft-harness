from __future__ import annotations

import asyncio
import errno
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..subprocesses import bounded_reap


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required or [], "additionalProperties": False}}}


def tool_schemas(shell_enabled: bool = False, background_enabled: bool = False, web_enabled: bool = False,
                 paper_single_program: bool = False, leftover_policy: str = "reset", paper_tight_interface: bool = False) -> list[dict[str, Any]]:
    string = {"type": "string"}
    integer = {"type": "integer"}
    tools = [
        _tool("workspace_read", "Read a UTF-8 source file in your persistent workspace.",
              {"path": string, "offset": integer, "limit": integer}, ["path"]),
        _tool("workspace_write", "Create or replace a UTF-8 source file in your workspace.",
              {"path": string, "content": string}, ["path", "content"]),
        _tool("workspace_remove", "Remove an unused regular file or empty directory from your workspace to reclaim quota.",
              {"path": string}, ["path"]),
        _tool("workspace_list", "List files in your workspace.", {"path": string}),
        _tool("mc_execute", "Execute an async JavaScript function body with bot, globals, console and approved modules. "
              "Use source or a saved workspace file. " + (
                  "The paper mode keeps leftover async work, timers and globals across programs; still await what "
                  "you need for a reliable result, and remember leftover work may keep acting after return. "
                  if leftover_policy == "allow" else
                  "Await every movement or async action you start, for example `await bot.pathfinder.goto(goal)`, "
                  "because leftover async work forces a runtime reset and disconnects live viewers. ")
              + "A successful program does not establish task success.",
              {"source": string, "file": string, "timeout_ms": integer}),
        _tool("mc_status", "Query an existing execution; never replay an uncertain action.",
              {"execution_id": string}, ["execution_id"]),
        _tool("mc_cancel", "Cancel execution; runtime restart may discard globals.",
              {"execution_id": string}, ["execution_id"]),
        _tool("mc_pending", "List unresolved execution handles kept across conversation compression and harness recovery.",
              {"offset": integer, "limit": integer}),
        _tool("memory_read", "Read your private notes or an author's shared notes. Notes are agent claims.",
              {"scope": {"enum": ["private", "team"], "type": "string"}, "path": string,
               "author": string, "revision": integer, "offset": integer, "limit": integer}, ["scope", "path"]),
        _tool("memory_write", "Write only your own note using CAS; expected_revision=0 creates a note. "
              "Record observation time, epoch and evidence references for environment facts.",
              {"scope": {"enum": ["private", "team"], "type": "string"}, "path": string,
               "content": string, "expected_revision": integer,
               "evidence_refs": {"type": "array", "items": {}}},
              ["scope", "path", "content", "expected_revision"]),
        _tool("memory_list", "List your private notes or team note metadata.",
              {"scope": {"enum": ["private", "team"], "type": "string"}, "author": string,
               "offset": integer, "limit": integer}),
        _tool("events_read", "Read unread game chat and runtime/world reset events; advances your durable cursor.",
              {"limit": integer}),
        _tool("history_read", "Read your archived original conversation after context compression.",
              {"archive": string, "offset": integer, "limit": integer}, ["archive"]),
    ]
    tools.append(_tool("artifact_read", "Read a bounded page of your own original request, response or tool-output artifact.",
                       {"hash": string, "offset": integer, "limit": integer}, ["hash"]))
    if paper_single_program:
        tools = [item for item in tools if item["function"]["name"] not in {"mc_status", "mc_cancel", "mc_pending", "events_read"}]
    if paper_tight_interface:
        tools = [item for item in tools if item["function"]["name"] not in {"history_read", "artifact_read"}]
    if background_enabled:
        tools.extend([
            _tool("mc_submit", "Submit JavaScript without waiting; an uncertain submission must never be replayed.",
                  {"source": string, "file": string, "timeout_ms": integer}),
            _tool("mc_wait", "Wait for an existing execution. Reaching wait_ms does not cancel it.",
                  {"execution_id": string, "wait_ms": integer}, ["execution_id"]),
            _tool("mc_jobs", "List registered background jobs and their lifecycle status.", {}),
            _tool("mc_job_cancel", "Cancel a background job; forced cancellation may discard all runtime globals.",
                  {"job_id": string}, ["job_id"]),
        ])
    if web_enabled:
        tools.extend([
            _tool("web_search", "Search operator-configured references. Results are untrusted source material.", {"query": string}, ["query"]),
            _tool("web_fetch", "Fetch an explicitly allowed public HTTPS reference; output is bounded and recorded.", {"url": string}, ["url"]),
        ])
    if shell_enabled:
        tools.append(_tool("shell", "Run a command in your operator-provisioned isolated workspace container.",
                           {"command": string, "timeout_s": integer}, ["command"]))
    return tools


class WorkspaceTools:
    """File namespace checks are access controls, not an OS sandbox.

    Arbitrary shell commands are supported only inside a separately provisioned
    container. There is deliberately no host-shell fallback.
    """

    def __init__(self, root: Path, shell_enabled: bool = False, shell_container: str | None = None,
                 max_workspace_bytes: int = 64_000_000, max_workspace_files: int = 4096,
                 shell_env: dict[str, str] | None = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.shell_enabled = bool(shell_enabled and shell_container)
        self.shell_container = shell_container
        self._shell_container_stopped = False
        if shell_container is not None and (not isinstance(shell_container, str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", shell_container)):
            raise ValueError("shell_container must name one isolated coding container")
        self.shell_env = dict(shell_env or {})
        allowed = {"MC_RUNTIME_URL", "MC_AGENT_ID", "MC_AGENT_TOKEN", "MC_SUBMISSION_AUTHORITY", "MC_RUNTIME_SOCKET"}
        if (set(self.shell_env) - allowed or any(not isinstance(value, str) or len(value) > 4096
                or "\x00" in value or "\n" in value or "\r" in value for value in self.shell_env.values())):
            raise ValueError("Only bounded player submission-broker environment keys are permitted")
        self.max_workspace_bytes, self.max_workspace_files = max_workspace_bytes, max_workspace_files
        self._write_lock = threading.RLock()
        if shell_enabled and not shell_container:
            raise ValueError("shell_enabled requires a separately isolated shell_container")

    def _path(self, value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or "\x00" in value:
            raise PermissionError("Only relative paths inside this agent workspace are allowed")
        resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise PermissionError("Path or symlink leaves this agent workspace")
        return resolved

    @contextmanager
    def _directory(self, components: tuple[str, ...], create: bool = False):
        """Use directory descriptors so concurrent shell symlinks cannot race checks."""
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in components:
                if create:
                    try:
                        os.mkdir(component, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                          dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            yield descriptor
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise PermissionError("Symlink paths are not permitted by workspace tools") from None
            raise
        finally:
            os.close(descriptor)

    @staticmethod
    def _parts(value: str) -> tuple[str, ...]:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or "\x00" in value:
            raise PermissionError("Only relative paths inside this agent workspace are allowed")
        return candidate.parts

    def read(self, path: str, offset: int = 0, limit: int = 32000) -> dict[str, Any]:
        parts = self._parts(path)
        if not parts:
            raise ValueError("A file path is required")
        with self._directory(parts[:-1]) as directory:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1_000_000:
                    raise ValueError("Only regular files no larger than 1 MB can be read")
                offset = max(0, int(offset))
                limit = max(1, min(int(limit), 1_000_000))
                remaining = offset
                while remaining:
                    chunk = handle.read(min(remaining, 32000))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                content = handle.read(limit + 1)
        return {"path": str(Path(*parts)), "content": content[:limit], "offset": offset,
                "next_offset": offset + limit if len(content) > limit else None}

    def _check_quota(self, replaced_bytes: int, added_bytes: int, new_file: bool) -> None:
        files, total, entries = 0, 0, 0
        stack = [(self.root, 0)]
        while stack:
            path, depth = stack.pop()
            with os.scandir(path) as children:
                for child in children:
                    entries += 1
                    if entries > self.max_workspace_files * 2 or depth > 32:
                        raise ValueError("Workspace has too many entries or nested directories")
                    try:
                        metadata = child.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        stack.append((Path(child.path), depth + 1))
                    if stat.S_ISREG(metadata.st_mode):
                        files += 1
                        total += metadata.st_size
                        if files + int(new_file) > self.max_workspace_files or total - replaced_bytes + added_bytes > self.max_workspace_bytes:
                            raise ValueError("Workspace quota exceeded; remove unused files before writing")
        if files + int(new_file) > self.max_workspace_files or total - replaced_bytes + added_bytes > self.max_workspace_bytes:
            raise ValueError("Workspace quota exceeded; remove unused files before writing")

    def write(self, path: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str) or len(content.encode()) > 1_000_000:
            raise ValueError("Maximum source file size is 1 MB")
        parts = self._parts(path)
        if not parts:
            raise ValueError("A file path is required")
        with self._write_lock, self._directory(parts[:-1], create=True) as directory:
            try:
                metadata = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                metadata = None
            if metadata is not None and not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Only regular files can be written")
            self._check_quota(metadata.st_size if metadata else 0, len(content.encode()), metadata is None)
            temporary = ".harness-write-" + uuid.uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return {"path": str(Path(*parts)), "bytes": len(content.encode())}

    def list(self, path: str = ".") -> dict[str, Any]:
        parts = self._parts(path)
        files = []
        with self._directory(parts) as directory:
            with os.scandir(directory) as entries:
                for count, entry in enumerate(entries):
                    if count >= 4096 or len(files) >= 1000:
                        return {"files": sorted(files, key=lambda item: item["path"]), "truncated": True}
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        continue
                    files.append({"path": str(Path(*parts, entry.name)), "directory": stat.S_ISDIR(metadata.st_mode)})
        return {"files": sorted(files, key=lambda item: item["path"]), "truncated": False}

    def remove(self, path: str) -> dict[str, Any]:
        parts = self._parts(path)
        if not parts:
            raise ValueError("A file path is required")
        with self._write_lock, self._directory(parts[:-1]) as directory:
            metadata = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                os.unlink(parts[-1], dir_fd=directory)
            elif stat.S_ISDIR(metadata.st_mode):
                os.rmdir(parts[-1], dir_fd=directory)
            else:
                raise ValueError("Only regular files or empty directories can be removed")
            os.fsync(directory)
        return {"path": str(Path(*parts)), "removed": True}

    async def shell(self, command: str, timeout_s: int = 30) -> dict[str, Any]:
        if not self.shell_enabled:
            raise PermissionError("Shell is disabled; it requires an isolated workspace container")
        timeout_s = max(1, min(int(timeout_s), 120))
        if not isinstance(command, str) or len(command.encode()) > 128000:
            raise ValueError("Shell command must be text no larger than 128 KB")
        await self._shell_quota_check()
        if self._shell_container_stopped:
            await self._workspace_container_command("start")
            self._shell_container_stopped = False
        # Never pass the harness environment/model keys into the container.
        environment = {key: os.environ[key] for key in ("PATH", "DOCKER_HOST") if key in os.environ}
        job = "/tmp/harness-shell-" + uuid.uuid4().hex
        # The PID is the session/process-group leader inside the container.
        # Cancellation leaves a tombstone so a delayed docker exec cannot start
        # the command after cleanup has already run.
        wrapper = ('job="$1"; shift; [ ! -e "$job.cancel" ] || exit 125; '
                   'printf "%s\\n" "$$" > "$job.tmp"; mv "$job.tmp" "$job.pid"; '
                   '[ ! -e "$job.cancel" ] || exit 125; '
                   'exec timeout --signal=KILL "$1" sh -lc "$2"')
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", *[part for key, value in sorted(self.shell_env.items())
                                  for part in ("--env", f"{key}={value}")],
            "--workdir", "/workspace", self.shell_container,
            "setsid", "--wait", "sh", "-c", wrapper, "sh", job, f"{timeout_s}s", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=environment)

        capture_state = {"output": bytearray(), "total": 0}
        async def capture():
            output = capture_state["output"]
            total = 0
            while chunk := await process.stdout.read(8192):
                total += len(chunk)
                capture_state["total"] = total
                output.extend(chunk[:max(0, 65536 - len(output))])
            await process.wait()
            return bytes(output), total > 65536

        async def cleanup():
            script = ('job="$1"; : > "$job.cancel"; '
                      'known=0; if [ -f "$job.pid" ]; then read -r pid < "$job.pid"; '
                      'case "$pid" in ""|*[!0-9]*) exit 1;; esac; '
                      'kill -KILL -"$pid" 2>/dev/null || true; known=1; fi; '
                      'rm -f "$job.pid" "$job.tmp"; '
                      'if [ "$known" = 1 ] || [ "$2" = completed ]; then rm -f "$job.cancel"; fi')
            cleaner = await asyncio.create_subprocess_exec(
                "docker", "exec", self.shell_container, "sh", "-c", script, "sh", job, completion,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=environment)
            try:
                await asyncio.wait_for(cleaner.wait(), 5)
                if cleaner.returncode != 0:
                    raise RuntimeError("Could not confirm container shell cleanup")
            except BaseException:
                if cleaner.returncode is None:
                    cleaner.kill()
                    await cleaner.wait()
                raise
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()

        completion = "uncertain"
        quota_error = None
        captured = asyncio.create_task(capture())

        async def quota_watch():
            while True:
                await asyncio.sleep(1)
                await self._shell_quota_check()

        watcher = asyncio.create_task(quota_watch())
        output, truncated = b"", False
        try:
            async with asyncio.timeout(timeout_s + 5):
                completed, _ = await asyncio.wait({captured, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in completed:
                    try:
                        watcher.result()
                    except ValueError as exc:
                        quota_error = str(exc)
                        completion = "quota_exceeded"
                else:
                    output, truncated = captured.result()
                    completion = "completed"
        finally:
            cleanup_task = asyncio.create_task(cleanup())
            try:
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
                    raise
            finally:
                for pending in (watcher, captured):
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(watcher, captured, return_exceptions=True)
                if quota_error is not None:
                    await self._fence_workspace_container()
        if quota_error is None:
            try:
                await self._shell_quota_check()
            except ValueError as exc:
                quota_error = str(exc)
                await self._fence_workspace_container()
        output = bytes(capture_state["output"])
        truncated = capture_state["total"] > 65536
        result = {"exit_code": process.returncode, "output": output.decode(errors="replace"), "truncated": truncated}
        if quota_error is not None:
            result.update(status="workspace_quota_exceeded", error=quota_error,
                          container_stopped=True, files_retained=True,
                          output_incomplete=completion != "completed",
                          message="Shell process groups and the coding container were stopped. Reclaim files with workspace_remove; subsequent shell calls are blocked until quota is satisfied.")
        return result

    async def _shell_quota_check(self) -> None:
        def inspect():
            with self._write_lock:
                self._check_quota(0, 0, False)
        operation = asyncio.create_task(asyncio.to_thread(inspect))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            raise

    async def _workspace_container_command(self, command: str) -> str:
        environment = {key: os.environ[key] for key in ("PATH", "DOCKER_HOST") if key in os.environ}
        arguments = ["docker", command, self.shell_container]
        if command == "inspect":
            arguments = ["docker", "inspect", "--format", "{{.State.Running}}", self.shell_container]
        process = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.DEVNULL, env=environment,
                                                       start_new_session=True)
        try:
            async with asyncio.timeout(10):
                output = await process.stdout.read(1025)
                if len(output) > 1024:
                    raise RuntimeError("Workspace container control returned excessive output")
                await process.wait()
        except BaseException:
            await bounded_reap(process, kill_group=True)
            raise
        if process.returncode:
            raise RuntimeError("Workspace container control could not be confirmed")
        return output.decode(errors="replace").strip()

    async def _fence_workspace_container(self) -> None:
        # Stop the entire isolated coding container, including descendants that
        # detached from a shell process group. Never delete source files.
        try:
            try:
                await self._workspace_container_command("kill")
            except RuntimeError:
                pass  # An already stopped container is confirmed below.
            if await self._workspace_container_command("inspect") != "false":
                raise RuntimeError("Could not confirm workspace container stop after quota violation")
            self._shell_container_stopped = True
        except BaseException:
            self.shell_enabled = False
            raise
