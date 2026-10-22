"""Standalone per-agent runner process for the same-host multiprocess harness.

The supervisor (control plane) keeps the ledger, budget, shared memory, the
execution gateway, the workspace broker and the server/world control.  This
module is the *only* code that runs inside a runner process::

    python -m minecraft_harness.runner

One runner owns exactly one :class:`~minecraft_harness.agents.loop.AgentLoop`
(player) or :class:`~minecraft_harness.observer.agent.ObserverAgent` (observer)
together with its model client, its conversation and its local checkpoint under
``state_root``.  It reaches every central service through an authenticated
Unix-socket RPC using :class:`minecraft_harness.runner_rpc.RpcClient`; it never
imports ``Supervisor``, ``Recorder``, ``MemoryStore`` or ``workspaces`` and it
never receives bot-runtime tokens, ledger/memory paths, a workspace host path or
Docker access.

Contract summary (see ``RUNNER_ARCH_CONTRACT.md``):

* Environment: ``HARNESS_RUNNER_SOCKET``, ``HARNESS_RUNNER_TOKEN``,
  ``HARNESS_RUNNER_AGENT``, ``HARNESS_RUNNER_ROLE``, ``HARNESS_RUNNER_SPEC`` (a
  mode-0600 JSON spec) and the optional ``HARNESS_RUNNER_RUN_ID``.
* One :class:`RpcClient` is shared by all proxies; sync proxies are used by the
  agent's synchronous tool code, async proxies by the agent's coroutines.
* Startup order: proxies -> model -> agent -> ``runner.ready`` (with prompt and
  tool-schema SHA256) -> heartbeat task -> agent task.
* Shutdown: ``runner.finished`` is always attempted; if delivery fails for good,
  the one allowed stdout line ``{"__runner_result__": ...}`` is written instead.
* Any startup failure prints one short JSON object to stderr and exits 2.  No
  other stdout output is ever produced.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import re
import signal
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .agents.loop import AgentLoop
from .models.client import build_model
from .pi_security import CredentialHygieneError, harden_process_credentials
from .runner_rpc import RpcClient, RpcError


# ---------------------------------------------------------------------------
# Public contract constants
# ---------------------------------------------------------------------------

ENV_RUNNER_SOCKET = "HARNESS_RUNNER_SOCKET"
ENV_RUNNER_TOKEN = "HARNESS_RUNNER_TOKEN"
ENV_RUNNER_AGENT = "HARNESS_RUNNER_AGENT"
ENV_RUNNER_ROLE = "HARNESS_RUNNER_ROLE"
ENV_RUNNER_SPEC = "HARNESS_RUNNER_SPEC"
ENV_RUNNER_RUN_ID = "HARNESS_RUNNER_RUN_ID"

SPEC_SCHEMA_VERSION = 1
MAX_SPEC_BYTES = 8 * 1024 * 1024
MAX_PATH_CHARS = 4096
MAX_RUN_ID_CHARS = 128
MAX_TOKEN_CHARS = 8192

DEFAULT_HEARTBEAT_SECONDS = 10.0
MIN_HEARTBEAT_SECONDS = 0.05
MAX_HEARTBEAT_SECONDS = 3600.0

READY_TIMEOUT_SECONDS = 30.0
FINISHED_TIMEOUT_SECONDS = 10.0
FINISHED_RETRY_DELAY_SECONDS = 0.25
# The supervisor's shutdown grace is 15-30 s; leave most of it for the final
# checkpoint and runner.finished report instead of waiting forever on an agent
# that does not observe cancellation promptly.
AGENT_CANCEL_GRACE_SECONDS = 5.0
HEARTBEAT_CALL_MIN_TIMEOUT_SECONDS = 5.0
HEARTBEAT_CALL_MAX_TIMEOUT_SECONDS = 60.0

_AGENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}")

# Keys that must never reach the agent implementation from the spec: tokens,
# ledger/memory/workspace host paths, Docker handles and shell credentials are
# control-plane-only.  The runner supplies a RemoteWorkspace with an empty
# ``shell_env`` instead.
_FORBIDDEN_AGENT_CONFIG_KEYS = frozenset({
    "shell_container",
    "shell_env",
    "workspace",
    "workspace_root",
    "workspace_path",
    "workspace_host_path",
    "state_root",
    "run_dir",
    "ledger",
    "ledger_path",
    "memory_path",
    "memory_root",
    "bot_token",
    "runtime_token",
    "runner_token",
    "token",
    "api_key",
    "docker",
    "docker_socket",
    "supervisor",
})
_FORBIDDEN_WEB_CONFIG_KEYS = frozenset({
    "api_key",
    "search_key",
    "fetch_key",
    "authorization",
    "headers",
    "token",
})
_FORBIDDEN_MODEL_SPEC_KEYS = frozenset({
    "api_key",
    "token",
    "bot_token",
    "runtime_token",
    "ledger_path",
    "memory_path",
    "workspace_root",
})


class RunnerStartupError(RuntimeError):
    """The runner could not be configured or started; the process exits 2."""


# ---------------------------------------------------------------------------
# Runner environment and spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunnerEnvironment:
    """The five required environment values that identify this runner."""

    socket_path: Path
    token: str
    agent_id: str
    role: str
    spec_path: Path
    run_id: str | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> RunnerEnvironment:
        raw: dict[str, str] = {}
        for name in (ENV_RUNNER_SOCKET, ENV_RUNNER_TOKEN, ENV_RUNNER_AGENT,
                     ENV_RUNNER_ROLE, ENV_RUNNER_SPEC):
            value = environment.get(name)
            if not isinstance(value, str) or not value:
                raise RunnerStartupError(f"Missing required runner environment variable {name}")
            raw[name] = value
        socket_path = _absolute_path(raw[ENV_RUNNER_SOCKET], ENV_RUNNER_SOCKET)
        spec_path = _absolute_path(raw[ENV_RUNNER_SPEC], ENV_RUNNER_SPEC)
        token = raw[ENV_RUNNER_TOKEN]
        if len(token) > MAX_TOKEN_CHARS or "\x00" in token:
            raise RunnerStartupError("HARNESS_RUNNER_TOKEN is not a usable token")
        agent_id = raw[ENV_RUNNER_AGENT]
        if not _AGENT_ID_PATTERN.fullmatch(agent_id) or agent_id in {".", ".."}:
            raise RunnerStartupError("HARNESS_RUNNER_AGENT is not a valid agent id")
        role = raw[ENV_RUNNER_ROLE]
        if role not in {"player", "observer"}:
            raise RunnerStartupError("HARNESS_RUNNER_ROLE must be player or observer")
        run_id = environment.get(ENV_RUNNER_RUN_ID)
        if run_id is not None and not isinstance(run_id, str):
            raise RunnerStartupError("HARNESS_RUNNER_RUN_ID must be a string")
        return cls(socket_path=socket_path, token=token, agent_id=agent_id,
                   role=role, spec_path=spec_path, run_id=run_id or None)


@dataclass(frozen=True)
class RunnerSpec:
    """A validated, scrubbed view of the control-plane spec document."""

    run_id: str
    agent_id: str
    role: str
    state_root: Path
    shell_enabled: bool
    heartbeat_s: float
    agent_config: dict[str, Any]
    model: dict[str, Any]


def _absolute_path(value: str, label: str) -> Path:
    if "\x00" in value or len(value) > MAX_PATH_CHARS:
        raise RunnerStartupError(f"{label} is not a usable path")
    try:
        path = Path(value)
    except (TypeError, ValueError):
        raise RunnerStartupError(f"{label} is not a usable path") from None
    if not path.is_absolute():
        raise RunnerStartupError(f"{label} must be an absolute path")
    return path


def load_spec(environment: RunnerEnvironment) -> RunnerSpec:
    """Read and validate the JSON spec without ever following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(environment.spec_path, flags)
    except OSError as exc:
        raise RunnerStartupError(
            f"Runner spec is not readable: {exc.strerror or type(exc).__name__}") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RunnerStartupError("Runner spec must be a regular file")
        if metadata.st_size > MAX_SPEC_BYTES:
            raise RunnerStartupError("Runner spec exceeds the size limit")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1  # ownership transferred to the file object
        with handle:
            raw = handle.read(MAX_SPEC_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_SPEC_BYTES:
        raise RunnerStartupError("Runner spec exceeds the size limit")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RunnerStartupError("Runner spec is not valid UTF-8 JSON") from None
    return _validate_spec(document, environment)


def _validate_spec(document: Any, environment: RunnerEnvironment) -> RunnerSpec:
    if not isinstance(document, dict):
        raise RunnerStartupError("Runner spec must be a JSON object")
    version = document.get("schema_version")
    if type(version) is not int or version != SPEC_SCHEMA_VERSION:
        raise RunnerStartupError("Unsupported runner spec schema version")
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= MAX_RUN_ID_CHARS:
        raise RunnerStartupError("Runner spec needs a bounded run_id")
    if environment.run_id is not None and environment.run_id != run_id:
        raise RunnerStartupError(
            "Runner spec run_id does not match HARNESS_RUNNER_RUN_ID")
    if document.get("agent_id") != environment.agent_id:
        raise RunnerStartupError(
            "Runner spec agent_id does not match HARNESS_RUNNER_AGENT")
    role = document.get("role")
    if role != environment.role:
        raise RunnerStartupError("Runner spec role does not match HARNESS_RUNNER_ROLE")
    if role not in {"player", "observer"}:
        raise RunnerStartupError("Runner spec role must be player or observer")
    state_root = _absolute_path(document.get("state_root", ""), "Runner spec state_root") \
        if isinstance(document.get("state_root"), str) else None
    if state_root is None:
        raise RunnerStartupError("Runner spec state_root must be an absolute path")
    shell_enabled = document.get("shell_enabled")
    if type(shell_enabled) is not bool:
        raise RunnerStartupError("Runner spec shell_enabled must be a boolean")
    heartbeat_s = document.get("heartbeat_s", DEFAULT_HEARTBEAT_SECONDS)
    if isinstance(heartbeat_s, bool) or not isinstance(heartbeat_s, (int, float)):
        raise RunnerStartupError("Runner spec heartbeat_s must be a number")
    heartbeat_s = float(heartbeat_s)
    if not math.isfinite(heartbeat_s) or not MIN_HEARTBEAT_SECONDS <= heartbeat_s <= MAX_HEARTBEAT_SECONDS:
        raise RunnerStartupError("Runner spec heartbeat_s is out of range")
    raw_config = document.get("agent_config")
    if not isinstance(raw_config, dict):
        raise RunnerStartupError("Runner spec agent_config must be an object")
    raw_model = document.get("model")
    if not isinstance(raw_model, dict):
        raise RunnerStartupError("Runner spec model must be an object")
    return RunnerSpec(
        run_id=run_id,
        agent_id=environment.agent_id,
        role=role,
        state_root=state_root,
        shell_enabled=shell_enabled,
        heartbeat_s=heartbeat_s,
        agent_config=_scrub_agent_config(raw_config, agent_id=environment.agent_id,
                                         shell_enabled=shell_enabled),
        model=_scrub_model_spec(raw_model),
    )


def _scrub_agent_config(config: dict[str, Any], *, agent_id: str,
                        shell_enabled: bool) -> dict[str, Any]:
    """Return the agent-visible config with every control-plane secret removed.

    The spec is written by the trusted supervisor, but a runner must not pass
    tokens, container names, host workspace paths or ledger/memory paths into
    the agent implementation even if a future spec accidentally contains them.
    """
    clean = copy.deepcopy(config)
    for key in _FORBIDDEN_AGENT_CONFIG_KEYS:
        clean.pop(key, None)
    web = clean.get("web")
    clean["web"] = _scrub_web_config(web if isinstance(web, dict) else {})
    # The control plane, not the spec body, decides the effective identity and
    # whether a remote shell is available for this runner.
    clean["agent_id"] = agent_id
    clean["shell_enabled"] = bool(shell_enabled)
    return clean


def _scrub_web_config(config: dict[str, Any]) -> dict[str, Any]:
    clean = copy.deepcopy(config)
    for key in _FORBIDDEN_WEB_CONFIG_KEYS:
        clean.pop(key, None)
    return clean


def _scrub_model_spec(config: dict[str, Any]) -> dict[str, Any]:
    """Keep only model settings the runner needs; inline credentials never pass."""
    clean = copy.deepcopy(config)
    for key in _FORBIDDEN_MODEL_SPEC_KEYS:
        clean.pop(key, None)
    return clean


# ---------------------------------------------------------------------------
# RPC error mapping
# ---------------------------------------------------------------------------

_KNOWN_EXCEPTION_TYPES: dict[str, type[BaseException]] | None = None


def _known_exception_types() -> dict[str, type[BaseException]]:
    """Import the real local exception classes lazily, on an error path only.

    The happy path must not import the privileged control-plane modules
    (``recorder``, ``memory.store``, ``execution.gateway``) at all.
    """
    global _KNOWN_EXCEPTION_TYPES
    if _KNOWN_EXCEPTION_TYPES is None:
        from .agents.core import ModelAdmissionError
        from .budget import BudgetExhausted
        from .execution.gateway import RuntimeReliabilityError, RuntimeUnavailable
        from .memory.store import MemoryConflict, MemoryPermissionError
        from .recorder import StorageLimitError

        _KNOWN_EXCEPTION_TYPES = {
            "BudgetExhausted": BudgetExhausted,
            "ModelAdmissionError": ModelAdmissionError,
            "StorageLimitError": StorageLimitError,
            "RuntimeUnavailable": RuntimeUnavailable,
            "RuntimeReliabilityError": RuntimeReliabilityError,
            "MemoryConflict": MemoryConflict,
            "MemoryPermissionError": MemoryPermissionError,
            "PermissionError": PermissionError,
            "FileNotFoundError": FileNotFoundError,
            "ValueError": ValueError,
            "TimeoutError": TimeoutError,
        }
    return _KNOWN_EXCEPTION_TYPES


def _mapped_exception(error: BaseException, *, gateway: bool = False) -> BaseException:
    """Map an :class:`RpcError` back to the real local exception class.

    ``retryable`` errors fall back to ``ConnectionError`` so the agent retry
    logic sees ``retryable``; gateway calls prefer the gateway's own
    ``RuntimeUnavailable``/``RuntimeReliabilityError`` classes.
    """
    error_type = getattr(error, "error_type", "")
    if not isinstance(error_type, str):
        error_type = ""
    retryable = bool(getattr(error, "retryable", False))
    message = str(error) or error_type or "runner RPC call failed"
    known = _known_exception_types()
    exception_class = known.get(error_type)
    if exception_class is None:
        if gateway:
            exception_class = (known["RuntimeUnavailable"] if retryable
                               else known["RuntimeReliabilityError"])
        elif retryable:
            exception_class = ConnectionError
        else:
            exception_class = RuntimeError
    mapped = exception_class(message)  # type: ignore[call-arg]
    try:
        mapped.retryable = retryable  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass
    return mapped


class _RemoteService:
    """Shared sync/async RPC plumbing for the remote service proxies."""

    _gateway_errors = False

    def __init__(self, client: RpcClient) -> None:
        self._client = client

    def _call(self, method: str, params: dict[str, Any] | None = None, *,
              timeout_s: float | None = None) -> Any:
        try:
            return self._client.call_sync(method, params if params is not None else {},
                                          timeout_s=timeout_s)
        except RpcError as exc:
            raise _mapped_exception(exc, gateway=self._gateway_errors) from None

    async def _acall(self, method: str, params: dict[str, Any] | None = None, *,
                     timeout_s: float | None = None) -> Any:
        try:
            return await self._client.call(method, params if params is not None else {},
                                           timeout_s=timeout_s)
        except RpcError as exc:
            raise _mapped_exception(exc, gateway=self._gateway_errors) from None


# ---------------------------------------------------------------------------
# Remote service proxies (all built from one RpcClient)
# ---------------------------------------------------------------------------

class RemoteRecorder(_RemoteService):
    """Ledger append / artifact upload / (Observer) ledger reads."""

    def __init__(self, client: RpcClient, *, agent_id: str) -> None:
        super().__init__(client)
        self.agent_id = agent_id

    def append(self, kind: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        if not isinstance(kind, str) or not isinstance(payload, dict):
            raise ValueError("Ledger append requires a string kind and object payload")
        return self._call("recorder.append",
                          {"kind": kind, "payload": payload, "kwargs": dict(kwargs)})

    def put_artifact(self, data: Any) -> dict[str, Any]:
        if isinstance(data, str):
            encoded = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray, memoryview)):
            encoded = bytes(data)
        else:
            raise ValueError("Artifact payload must be text or bytes")
        try:
            params = {"text": encoded.decode("utf-8")}
        except UnicodeDecodeError:
            params = {"base64": base64.b64encode(encoded).decode("ascii")}
        return self._call("recorder.put_artifact", params)

    def read_events(self, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        if type(after) is not int or after < 0:
            raise ValueError("Recorder cursor must be nonnegative")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Recorder limit must be between 1 and 1000")
        return self._call("recorder.read_events", {"after": after, "limit": limit})

    def latest_event_seq(self) -> int:
        return int(self._call("recorder.latest_event_seq") or 0)


class _RemoteAdmission:
    """Pause gate proxy: ``await budget.admission.wait()``."""

    def __init__(self, budget: "RemoteBudget") -> None:
        self._budget = budget

    async def wait(self) -> Any:
        # The control plane long-polls until the operator resumes the run and
        # returns {"admitted": bool, "stop": bool}; AgentCore checks
        # budget.stop_requested() again right after admission, so the value
        # itself is advisory here.
        return await self._budget._acall("budget.wait_admission")


class RemoteBudget(_RemoteService):
    """Team-wide model admission/settlement; the control plane owns the state."""

    def __init__(self, client: RpcClient, *, agent_id: str, role: str) -> None:
        super().__init__(client)
        self.agent_id = agent_id
        self.role = role
        self.admission = _RemoteAdmission(self)

    async def validate_request(self, request_bytes: int) -> Any:
        if isinstance(request_bytes, bool) or not isinstance(request_bytes, int) or request_bytes < 1:
            raise ValueError("request_bytes must be a positive integer")
        return await self._acall("budget.validate_request", {"request_bytes": request_bytes})

    async def reserve(self, agent_id: str, *,
                      purpose: str = "agent_decision", role: str = "player") -> Any:
        # AgentCore introspects this signature for the keyword-only "purpose"
        # parameter and treats the returned value as the reservation id.
        if not isinstance(purpose, str) or not isinstance(role, str):
            raise ValueError("Budget reservation requires purpose and role names")
        return await self._acall("budget.reserve", {"purpose": purpose, "role": role})

    async def settle(self, reservation_id: str, usage: dict[str, Any]) -> Any:
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("reservation_id is required")
        if not isinstance(usage, dict):
            raise ValueError("usage must be an object")
        return await self._acall("budget.settle",
                                 {"reservation_id": reservation_id, "usage": usage})

    async def stop_requested(self) -> bool:
        # Must be ``async def``: AgentCore awaits awaitables but also tolerates
        # a plain bool for the in-process budget.
        return bool(await self._acall("budget.stop_requested"))

    def admit_web(self, agent_id: str, tool: str = "web") -> bool:
        """Meter one web call before touching the network.

        Synchronous because :meth:`agents.web.WebTools._admit` is synchronous;
        the control plane meters durably and always uses the bound runner
        identity, so a runner cannot spend another agent's web budget.
        """
        if not isinstance(tool, str) or not 1 <= len(tool) <= 64:
            raise ValueError("web tool name must be a short string")
        result = self._call("budget.admit_web", {"agent_id": self.agent_id, "tool": tool})
        if isinstance(result, dict):
            return bool(result.get("admitted"))
        return bool(result)


class RemoteMemory(_RemoteService):
    """Shared-memory service; the server forces the bound agent identity."""

    def __init__(self, client: RpcClient, *, agent_id: str) -> None:
        super().__init__(client)
        self.agent_id = agent_id

    def _params(self, args: tuple[Any, ...], kwargs: dict[str, Any],
                method: str) -> dict[str, Any]:
        if args:
            if len(args) != 1 or args[0] != self.agent_id:
                raise ValueError(f"memory.{method} only serves the bound agent")
        forwarded = {key: value for key, value in kwargs.items()
                     if key not in {"actor_id", "agent_id"}}
        return {"actor_id": self.agent_id, **forwarded}

    def read(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("memory.read", self._params(args, kwargs, "read"))

    def write(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("memory.write", self._params(args, kwargs, "write"))

    def list(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("memory.list", self._params(args, kwargs, "list"))

    def search(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("memory.search", self._params(args, kwargs, "search"))


class RemoteWorkspace(_RemoteService):
    """Remote proxy presenting the WorkspaceTools surface to the agent.

    ``shell_container``/``shell_env`` are placeholders: the control plane keeps
    the real container and submission-broker environment, so no Docker handle or
    bot credential ever reaches the runner.
    """

    def __init__(self, client: RpcClient, *, shell_enabled: bool) -> None:
        super().__init__(client)
        self.shell_enabled = bool(shell_enabled)
        self.shell_container = "rpc-broker" if self.shell_enabled else None
        self.shell_env: dict[str, str] = {}

    def read(self, path: str, offset: int = 0, limit: int = 32000) -> dict[str, Any]:
        return self._call("workspace.read",
                          {"path": path, "offset": offset, "limit": limit})

    def write(self, path: str, content: str) -> dict[str, Any]:
        return self._call("workspace.write", {"path": path, "content": content})

    def remove(self, path: str) -> dict[str, Any]:
        return self._call("workspace.remove", {"path": path})

    def list(self, path: str = ".") -> dict[str, Any]:
        return self._call("workspace.list", {"path": path})

    async def shell(self, command: str, timeout_s: int | None = None) -> dict[str, Any]:
        if not self.shell_enabled:
            raise PermissionError("Shell is disabled for this runner workspace")
        return await self._acall("workspace.shell",
                                 {"command": command, "timeout_s": timeout_s})


class RemoteExecutor(_RemoteService):
    """Remote ExecutionGateway proxy; every call carries the bound agent id."""

    _gateway_errors = True

    def __init__(self, client: RpcClient, *, agent_id: str) -> None:
        super().__init__(client)
        self.agent_id = agent_id

    def _identity(self, agent_id: str) -> str:
        if agent_id != self.agent_id:
            raise ValueError("Execution gateway only serves the bound agent")
        return self.agent_id

    async def execute(self, agent_id: str, source: str, *, timeout_ms: int = 60000,
                      request_id: str | None = None) -> dict[str, Any]:
        return await self._acall("gateway.execute", {
            "agent_id": self._identity(agent_id), "source": source,
            "timeout_ms": timeout_ms, "request_id": request_id})

    async def submit(self, agent_id: str, source: str, *, timeout_ms: int = 60000,
                     request_id: str | None = None) -> dict[str, Any]:
        return await self._acall("gateway.submit", {
            "agent_id": self._identity(agent_id), "source": source,
            "timeout_ms": timeout_ms, "request_id": request_id})

    async def status(self, agent_id: str, execution_id: str) -> dict[str, Any]:
        return await self._acall("gateway.status", {
            "agent_id": self._identity(agent_id), "execution_id": execution_id})

    async def cancel(self, agent_id: str, execution_id: str) -> dict[str, Any]:
        return await self._acall("gateway.cancel", {
            "agent_id": self._identity(agent_id), "execution_id": execution_id})

    async def wait(self, agent_id: str, execution_id: str,
                   wait_ms: int = 1000) -> dict[str, Any]:
        return await self._acall("gateway.wait", {
            "agent_id": self._identity(agent_id), "execution_id": execution_id,
            "wait_ms": wait_ms})

    async def jobs(self, agent_id: str) -> dict[str, Any]:
        return await self._acall("gateway.jobs", {"agent_id": self._identity(agent_id)})

    async def cancel_job(self, agent_id: str, job_id: str) -> dict[str, Any]:
        return await self._acall("gateway.cancel_job", {
            "agent_id": self._identity(agent_id), "job_id": job_id})

    async def read_events(self, agent_id: str, after_cursor: int = 0,
                          limit: int = 100) -> dict[str, Any]:
        return await self._acall("gateway.read_events", {
            "agent_id": self._identity(agent_id), "after_cursor": after_cursor,
            "limit": limit})


class RemoteArtifactAPI(_RemoteService):
    """Central artifact ACL/read service; runner processes never open files."""

    def __init__(self, client: RpcClient, *, agent_id: str) -> None:
        super().__init__(client)
        self.agent_id = agent_id
        # Owned digests are cached locally so repeated ACL checks avoid RPCs.
        self._granted: set[str] = set()

    def _identity(self, agent_id: str) -> str:
        if agent_id != self.agent_id:
            raise ValueError("Artifact service only serves the bound agent")
        return self.agent_id

    @staticmethod
    def _digest(digest: Any) -> str:
        if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
            raise ValueError("Artifact hash must be SHA256")
        return digest

    def grant(self, agent_id: str, digest: str) -> Any:
        digest = self._digest(digest)
        self._identity(agent_id)
        result = self._call("artifact.grant", {"hash": digest})
        self._granted.add(digest)
        return result

    def owned(self, agent_id: str, digest: str) -> bool:
        digest = self._digest(digest)
        self._identity(agent_id)
        if digest in self._granted:
            return True
        result = self._call("artifact.owned", {"hash": digest})
        granted = _as_bool(result)
        if granted:
            self._granted.add(digest)
        return granted

    def read(self, agent_id: str, hash: str, offset: int = 0,
             limit: int = 12000) -> dict[str, Any]:
        digest = self._digest(hash)
        self._identity(agent_id)
        if type(offset) is not int or offset < 0:
            raise ValueError("Artifact offset must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 32000:
            raise ValueError("Artifact limit must be between 1 and 32000")
        return self._call("artifact.read",
                          {"hash": digest, "offset": offset, "limit": limit})


class RemoteInfraCallback(_RemoteService):
    """Observer -> supervisor infrastructure-issue callback."""

    def __init__(self, client: RpcClient, *, agent_id: str) -> None:
        super().__init__(client)
        self.agent_id = agent_id

    async def __call__(self, issue: Any) -> Any:
        if not isinstance(issue, dict):
            raise ValueError("Observer infrastructure issue must be an object")
        return await self._acall("supervisor.observer_infra_issue", {"issue": issue})


def _as_bool(value: Any) -> bool:
    """Coerce an artifact-ACL RPC result to a real bool.

    The control-plane handler returns a JSON boolean; accept an object form too
    so a defensive handler cannot accidentally invert the meaning.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        for key in ("owned", "granted", "allowed"):
            if key in value:
                return bool(value[key])
        return False
    return bool(value)


# ---------------------------------------------------------------------------
# Building the runner
# ---------------------------------------------------------------------------

@dataclass
class PreparedRunner:
    """Everything needed once the event loop starts."""

    environment: RunnerEnvironment
    spec: RunnerSpec
    client: RpcClient
    agent: Any
    heartbeat_s: float
    ready_params: dict[str, Any]


def _agent_interface_hashes(agent: Any) -> tuple[str, str]:
    messages = getattr(agent, "messages", None)
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        raise RunnerStartupError("Agent did not expose its system prompt")
    prompt = messages[0].get("content")
    if not isinstance(prompt, str):
        raise RunnerStartupError("Agent system prompt is not text")
    schemas = getattr(agent, "schemas", None)
    if not isinstance(schemas, list):
        raise RunnerStartupError("Agent did not expose its tool schemas")
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    # Must match the in-process supervisor's manifest computation exactly.
    tools_sha256 = hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()
    return prompt_sha256, tools_sha256


def prepare_runner(environment: RunnerEnvironment, spec: RunnerSpec) -> PreparedRunner:
    """Build the RPC client, remote proxies, model and agent (no I/O waits yet)."""
    try:
        spec.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise RunnerStartupError(
            f"Could not create the runner state directory: {exc.strerror or type(exc).__name__}") from None
    if not spec.state_root.is_dir():
        raise RunnerStartupError("Runner state_root is not a directory")

    client = RpcClient(str(environment.socket_path), environment.token,
                       environment.agent_id, environment.role)
    recorder = RemoteRecorder(client, agent_id=environment.agent_id)
    budget = RemoteBudget(client, agent_id=environment.agent_id, role=environment.role)
    memory = RemoteMemory(client, agent_id=environment.agent_id)
    executor = RemoteExecutor(client, agent_id=environment.agent_id)
    workspace = RemoteWorkspace(client, shell_enabled=spec.shell_enabled)
    artifacts = RemoteArtifactAPI(client, agent_id=environment.agent_id)

    try:
        model = build_model(spec.model)
    except Exception as exc:
        raise RunnerStartupError(
            f"Could not construct the model client: {type(exc).__name__}: {exc}") from None

    try:
        if spec.role == "player":
            agent = AgentLoop(spec.agent_config, spec.state_root, recorder, executor,
                              model, budget, memory, state_root=spec.state_root,
                              workspace=workspace, artifact_api=artifacts)
        else:
            # Imported lazily so player runners never load the Observer's
            # deterministic report/evaluation modules.
            from .observer.agent import ObserverAgent

            agent = ObserverAgent(spec.agent_config, spec.state_root, recorder, model,
                                  budget,
                                  infra_callback=RemoteInfraCallback(
                                      client, agent_id=environment.agent_id),
                                  state_root=spec.state_root, workspace=workspace,
                                  artifact_api=artifacts)
    except RunnerStartupError:
        raise
    except Exception as exc:
        raise RunnerStartupError(
            f"Could not construct the {spec.role} agent: {type(exc).__name__}: {exc}") from None

    prompt_sha256, tools_sha256 = _agent_interface_hashes(agent)
    ready_params = {
        "agent_id": environment.agent_id,
        "role": environment.role,
        "pid": os.getpid(),
        "prompt_sha256": prompt_sha256,
        "tools_sha256": tools_sha256,
    }
    return PreparedRunner(environment=environment, spec=spec, client=client,
                          agent=agent, heartbeat_s=spec.heartbeat_s,
                          ready_params=ready_params)


# ---------------------------------------------------------------------------
# Signals, heartbeat and final reporting
# ---------------------------------------------------------------------------

def _stop_signals() -> tuple[int, ...]:
    return tuple(signum for signum in (getattr(signal, "SIGTERM", None),
                                       getattr(signal, "SIGINT", None))
                 if signum is not None)


class _EarlySignalGuard:
    """Record SIGTERM/SIGINT during synchronous startup.

    The asyncio-level handlers are installed once the event loop is running; a
    signal that arrives while the model/agent are constructed must not kill the
    process with the default action.
    """

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> "_EarlySignalGuard":
        for signum in _stop_signals():
            try:
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle)
            except (ValueError, OSError, TypeError):
                continue
        return self

    def _handle(self, _signum: int, _frame: Any) -> None:
        self.stop_event.set()

    def __exit__(self, *_exc: Any) -> bool:
        for signum, previous in self._previous.items():
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError, TypeError):
                continue
        self._previous.clear()
        return False


def _install_stop_signal_handlers(stop_event: asyncio.Event) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - only when called outside a loop
        return
    for signum in _stop_signals():
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(signum, lambda _signum, _frame: stop_event.set())
            except (ValueError, OSError, TypeError):
                continue


def _turns(agent: Any) -> int:
    turns = getattr(agent, "turns", 0)
    return turns if type(turns) is int and turns >= 0 else 0


def _process_rss_bytes() -> int:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return max(0, resident_pages) * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError):
        try:
            import resource

            return max(0, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)) * 1024
        except (ImportError, OSError, ValueError):
            return 0


async def _heartbeat_loop(client: RpcClient, agent: Any, interval_s: float,
                          state: dict[str, str]) -> None:
    """Periodically report ``{turns, status, rss_bytes}``; never fail the agent."""
    timeout_s = max(HEARTBEAT_CALL_MIN_TIMEOUT_SECONDS,
                    min(HEARTBEAT_CALL_MAX_TIMEOUT_SECONDS, interval_s * 2))
    while True:
        await asyncio.sleep(interval_s)
        params = {
            "turns": _turns(agent),
            "status": state.get("status", "running"),
            "rss_bytes": _process_rss_bytes(),
        }
        try:
            await client.call("runner.heartbeat", params, timeout_s=timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transport errors are expected during control-plane shutdown or a
            # transient pause; the runner must keep working.
            continue


def _stopped_result(spec: RunnerSpec, agent: Any) -> dict[str, Any]:
    return {"agent_id": spec.agent_id, "status": "stopped", "turns": _turns(agent)}


async def _deliver_finished(client: RpcClient, result: dict[str, Any],
                            real_stdout: Any) -> bool:
    """Send ``runner.finished``; fall back to the one allowed stdout line."""
    params = {"result": result}
    for attempt in range(2):
        try:
            await client.call("runner.finished", params,
                              timeout_s=FINISHED_TIMEOUT_SECONDS)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            if attempt == 0:
                try:
                    await asyncio.sleep(FINISHED_RETRY_DELAY_SECONDS)
                except asyncio.CancelledError:
                    raise
            continue
    _write_stdout_result(real_stdout, result)
    return False


def _write_stdout_result(real_stdout: Any, result: dict[str, Any]) -> None:
    if real_stdout is None:
        return
    try:
        line = json.dumps({"__runner_result__": result}, ensure_ascii=False, default=str)
        real_stdout.write(line + "\n")
        real_stdout.flush()
    except (OSError, ValueError, TypeError):
        pass


def _write_startup_error(error: BaseException) -> None:
    message = str(error)
    if len(message) > 500:
        message = message[:500]
    document = {"__runner_error__": {"error": type(error).__name__, "message": message}}
    try:
        sys.stderr.write(json.dumps(document, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except (OSError, ValueError, TypeError):
        pass


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------

async def _run_prepared(prepared: PreparedRunner, early_stop: threading.Event,
                        real_stdout: Any) -> int:
    """Announce readiness, run the agent, then report a single terminal result."""
    stop_event = asyncio.Event()
    _install_stop_signal_handlers(stop_event)
    if early_stop.is_set():
        return 0
    try:
        await prepared.client.call("runner.ready", prepared.ready_params,
                                   timeout_s=READY_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if stop_event.is_set() or early_stop.is_set():
            return 0  # control plane is shutting down; nothing was started
        raise RunnerStartupError(
            f"runner.ready failed: {type(exc).__name__}") from None
    if stop_event.is_set():
        # A signal arrived while readiness was in flight: report a clean stop
        # without starting the agent at all.
        await _deliver_finished(prepared.client,
                                _stopped_result(prepared.spec, prepared.agent),
                                real_stdout)
        return 0

    state = {"status": "running"}
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(prepared.client, prepared.agent, prepared.heartbeat_s, state),
        name=f"runner-heartbeat-{prepared.spec.agent_id}")
    agent_task = asyncio.create_task(
        prepared.agent.run(), name=f"agent-{prepared.spec.agent_id}")
    stop_task = asyncio.create_task(stop_event.wait(),
                                    name=f"runner-stop-{prepared.spec.agent_id}")
    runtime_failure = False
    try:
        try:
            done, _pending = await asyncio.wait(
                {agent_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if agent_task in done:
                try:
                    result = agent_task.result()
                    if not isinstance(result, dict):
                        raise RunnerStartupError("Agent returned a non-object result")
                except asyncio.CancelledError:
                    result = _stopped_result(prepared.spec, prepared.agent)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:
                    runtime_failure = True
                    state["status"] = "failed"
                    result = {"agent_id": prepared.spec.agent_id,
                              "status": "infra_failed", "turns": _turns(prepared.agent),
                              "error": type(exc).__name__}
            else:
                # SIGTERM/SIGINT: cancel the agent and let its cancellation
                # handler write the final checkpoint and stop record. Await it
                # through asyncio.wait (not asyncio.wait_for) so a task whose
                # cancellation is slow cannot block shutdown forever.
                state["status"] = "stopping"
                agent_task.cancel()
                await asyncio.wait({agent_task},
                                   timeout=AGENT_CANCEL_GRACE_SECONDS)
                result = _stopped_result(prepared.spec, prepared.agent)
        except (KeyboardInterrupt, SystemExit):
            state["status"] = "stopping"
            raise
    finally:
        for task in (heartbeat_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(heartbeat_task, stop_task, return_exceptions=True)
        if agent_task.done():
            await asyncio.gather(agent_task, return_exceptions=True)
    await _deliver_finished(prepared.client, result, real_stdout)
    return 2 if runtime_failure else 0


def main(argv: list[str] | None = None) -> int:
    """Process entry point: ``python -m minecraft_harness.runner``."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    real_stdout = sys.__stdout__ if sys.__stdout__ is not None else sys.stdout
    # No dependency may ever corrupt the supervisor's stdout protocol: the only
    # permitted stdout write is the final JSON fallback below.
    sys.stdout = sys.stderr
    try:
        harden_process_credentials()
    except CredentialHygieneError as exc:
        _write_startup_error(exc)
        return 2
    if arguments:
        _write_startup_error(RunnerStartupError("The runner entry point takes no arguments"))
        return 2
    with _EarlySignalGuard() as guard:
        try:
            environment = RunnerEnvironment.from_environment(os.environ)
            spec = load_spec(environment)
            prepared = prepare_runner(environment, spec)
        except Exception as exc:
            _write_startup_error(exc)
            return 2
        if guard.stop_event.is_set():
            return 0  # stopped during startup; nothing was announced or started
        try:
            return asyncio.run(_run_prepared(prepared, guard.stop_event, real_stdout))
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            _write_startup_error(exc)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
