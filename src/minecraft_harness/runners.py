"""Control-plane broker for same-host independent agent runner processes.

The supervisor keeps the durable ledger, budget, memory, execution gateway,
workspace broker, evaluator and server control. Each player and the Observer run
in a separate OS process and reach those services only through an authenticated
local Unix-socket RPC. Runner processes receive no bot-runtime credentials.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .execution.gateway import RuntimeReliabilityError, RuntimeUnavailable
from .pi_model_proxy import (ENV_MODEL_FORWARDER_TOKEN,
                             MODEL_FORWARDER_CONTAINER_DIRECTORY,
                             MODEL_FORWARDER_CONTAINER_SOCKET,
                             MODEL_FORWARDER_SOCKET_NAME)
from .recorder import StorageLimitError, atomic_json
from .runner_rpc import RpcError, RpcServer


_ARTIFACT_DIRECTORY = "artifacts"
# Linux sockaddr_un.sun_path is 108 bytes including the NUL; keep a margin for
# Docker's bind-mount/launcher layer and use one threshold for both the
# host-shared directory selection and the final socket creation check.
_MAX_RPC_SOCKET_BYTES = 100
# Runner processes must load exactly the harness source recorded for this run.
# The first RunnerManager.start() therefore freezes the live package tree into
# run_dir/source-snapshot/src and every later start/spawn verifies that copy.
_SOURCE_SNAPSHOT_DIRECTORY = "source-snapshot"
_SOURCE_SNAPSHOT_MANIFEST = "source-snapshot.json"
_SOURCE_SNAPSHOT_SCHEMA_VERSION = 1
_SOURCE_SNAPSHOT_IGNORED_DIRECTORIES = frozenset({"__pycache__"})
_SOURCE_SNAPSHOT_IGNORED_SUFFIXES = frozenset({".pyc"})
_SOURCE_TREE_HASH_POLICY = (
    "sha256 per file: relative POSIX path -> sha256(file bytes); tree digest = "
    "sha256 of a canonical JSON object with sorted keys and compact separators; "
    "__pycache__ directories and *.pyc files are excluded; symlinks are hashed "
    "by their relative target string and may not escape the frozen tree")
# Pi runtime integration. The Python AgentCore runner remains the default; when
# runtime.runner_runtime == "pi" player runners launch minecraft_harness.pi_runner
# against the pinned Pi coding-agent bundle (Node). The observer always stays on
# the Python runner for this iteration.
_DEFAULT_PI_CLI_RELATIVE = (
    "pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js")
_DEFAULT_PI_CONTEXT_WINDOW = 64000
_PYTHON_RUNNER_MODULE = "minecraft_harness.runner"
_PI_RUNNER_MODULE = "minecraft_harness.pi_runner"
_PI_OBSERVER_RUNNER_MODULE = "minecraft_harness.pi_observer_runner"
_PI_RUNNER_MODULES = (_PYTHON_RUNNER_MODULE, _PI_RUNNER_MODULE,
                      _PI_OBSERVER_RUNNER_MODULE)
_PI_EXTENSION_FILES = {"player": "minecraft-tools.js",
                       "observer": "observer-tools.js"}
# Runner-container egress policy (AUDIT_2026-10-23_POST_FIXES.md P1-3). The
# legacy behavior keeps the container's bridge network. "model_only" gives Pi
# runner containers no network at all and reaches the model provider exclusively
# through a host-side forwarder over a bind-mounted Unix domain socket. Python
# runners (and inprocess mode) always keep the legacy behavior.
_RUNNER_EGRESS_BRIDGE = "bridge"
_RUNNER_EGRESS_MODEL_ONLY = "model_only"
_RUNNER_EGRESS_MODES = frozenset({_RUNNER_EGRESS_BRIDGE, _RUNNER_EGRESS_MODEL_ONLY})


def resolve_runner_egress(config):
    """Effective egress mode for runner containers.

    ``runtime.runner_egress`` may be set explicitly ("bridge" preserves the
    historical unrestricted behavior, "model_only" enables the host model
    forwarder). When unset, the Pi container runtime defaults to "model_only"
    (the paper runner) while python/inprocess configurations keep "bridge".
    """
    runtime = config.get("runtime", {}) if isinstance(config, dict) else {}
    if not isinstance(runtime, dict):
        runtime = {}
    explicit = runtime.get("runner_egress")
    if explicit is not None:
        if not isinstance(explicit, str) or explicit not in _RUNNER_EGRESS_MODES:
            raise RunnerControlError(
                "runtime.runner_egress must be bridge or model_only")
        return explicit
    if (runtime.get("runner_mode") == "container"
            and runtime.get("runner_runtime", "python") == "pi"):
        return _RUNNER_EGRESS_MODEL_ONLY
    return _RUNNER_EGRESS_BRIDGE
_PI_CLI_ENVIRONMENT_KEYS = ("MC_RUNTIME_URL", "MC_AGENT_ID", "MC_AGENT_TOKEN",
                            "MC_SUBMISSION_AUTHORITY", "MC_RUNTIME_SOCKET")
# Observer RPC (Pi observer runner). Mirrors ObserverAgent review semantics: a
# bounded, redacting ledger page whose embedded artifact digests are granted to
# the observer, typed evidence reads, and advisory-only report publication.
_OBSERVER_AGENT_ID = "observer"
_OBSERVER_PAGE_BYTES = 32000
# Trusted Pi artifact-budget accounting. The SQLite authority lives under the
# run directory (never bind-mounted into a runner container) so a model-writable
# /state marker cannot reset restart-safe usage; both caps are enforced here.
_PI_BUDGET_DEFAULT_ARTIFACT_BYTES = 4_000_000_000
_PI_BUDGET_PRESSURE_THRESHOLD = 0.9
_PI_BUDGET_DEFAULT_GLOBAL_BYTES = 24 * 1024 ** 3
_PI_BUDGET_STATE_FILE = "pi-budget.sqlite3"
_OBSERVER_EVENT_JSON_LIMIT = 8000
_OBSERVER_FINDING_STATUSES = frozenset({
    "verified", "player_claim", "insufficient_evidence", "contradicted"})
_ARTIFACT_DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}")


class RunnerControlError(RuntimeError):
    """A runner control-plane invariant failed."""


def _append(recorder, kind, payload):
    """Runner lifecycle records must never mask the actual failure."""
    try:
        recorder.append(kind, payload)
    except Exception:
        pass


def _source_tree_hashes(source_root):
    """Deterministic sha256 per file for a source tree, keyed by relative POSIX path.

    ``__pycache__`` directories and ``*.pyc`` files are excluded so bytecode
    caches created by runner processes cannot change the recorded source hash.
    Symlinks are recorded by their relative target string (they are never
    followed) and may not point outside the tree.
    """
    root = Path(source_root)
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise RunnerControlError(f"runner source tree is unreadable: {source_root}: {exc}") from exc
    hashes = {}
    for base, directories, files in os.walk(root, followlinks=False):
        directory = Path(base)
        kept, symlinked = [], []
        for name in sorted(directories):
            candidate = directory / name
            if name in _SOURCE_SNAPSHOT_IGNORED_DIRECTORIES:
                continue
            (symlinked if candidate.is_symlink() else kept).append(name)
        directories[:] = kept
        for name in sorted(files) + symlinked:
            path = directory / name
            if not path.is_symlink() and path.suffix in _SOURCE_SNAPSHOT_IGNORED_SUFFIXES:
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise RunnerControlError(
                        "runner source snapshot cannot contain an absolute symlink: " + str(path))
                if not (path.parent / target).resolve().is_relative_to(root):
                    raise RunnerControlError(
                        "runner source snapshot symlink escapes the frozen tree: " + str(path))
                hashes[relative] = hashlib.sha256(
                    ("symlink:" + target).encode("utf-8")).hexdigest()
            else:
                if not path.is_file():
                    raise RunnerControlError(
                        "runner source snapshot cannot contain a non-regular file: " + str(path))
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    raise RunnerControlError(
                        f"runner source file is unreadable: {path}: {exc}") from exc
                hashes[relative] = hashlib.sha256(data).hexdigest()
    return dict(sorted(hashes.items()))


def _source_tree_sha256(hashes):
    """Collapse per-file hashes into the deterministic frozen-tree digest."""
    canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ArtifactVault:
    """Central per-agent artifact read ACL; runner processes never open files.

    The old in-process path kept one SQLite ACL per agent state directory. With
    runners the trusted control plane owns every grant and every read so a
    runner cannot read another agent's artifacts by path.
    """

    def __init__(self, recorder, run_dir):
        self.recorder = recorder
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "artifact-access.sqlite3"
        self.lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS grants ("
                               "agent_id TEXT NOT NULL, hash TEXT NOT NULL, "
                               "PRIMARY KEY(agent_id, hash))")
        self._migrate_per_agent_acls()

    def _migrate_per_agent_acls(self):
        """Import in-process per-agent ACLs so an upgraded resume keeps reads.

        New runs grant directly into this central vault; old checkpoints may
        reference artifacts granted only in ``agents/<id>/artifact-access.sqlite3``.
        """
        root = self.run_dir / "agents"
        if not root.is_dir():
            return
        migrated = 0
        for agent_dir in sorted(root.iterdir()):
            if not agent_dir.is_dir() or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", agent_dir.name):
                continue
            legacy = agent_dir / "artifact-access.sqlite3"
            if not legacy.is_file():
                continue
            try:
                source = sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True, timeout=5)
                try:
                    hashes = [row[0] for row in source.execute(
                        "SELECT hash FROM owned_artifacts")]
                finally:
                    source.close()
            except sqlite3.Error:
                continue
            hashes = [digest for digest in hashes
                      if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)]
            if not hashes:
                continue
            with self.lock, self._connect() as connection:
                before = connection.total_changes
                connection.executemany("INSERT OR IGNORE INTO grants VALUES(?,?)",
                                       [(agent_dir.name, digest) for digest in hashes])
                migrated += connection.total_changes - before
        if migrated and self.recorder is not None:
            try:
                self.recorder.append("artifact_acl_migrated", {"grants": migrated})
            except Exception:
                pass

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _agent(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", value):
            raise ValueError("Invalid agent identity")
        return value

    @staticmethod
    def _digest(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("Artifact hash must be SHA256")
        return value

    def grant(self, agent_id, digest):
        agent_id, digest = self._agent(agent_id), self._digest(digest)
        with self.lock, self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO grants VALUES(?,?)", (agent_id, digest))

    def owned(self, agent_id, digest):
        agent_id, digest = self._agent(agent_id), self._digest(digest)
        with self.lock, self._connect() as connection:
            return connection.execute("SELECT 1 FROM grants WHERE agent_id=? AND hash=?",
                                      (agent_id, digest)).fetchone() is not None

    def read(self, agent_id, digest, offset=0, limit=12000):
        agent_id, digest = self._agent(agent_id), self._digest(digest)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32000:
            raise ValueError("Artifact offset must be nonnegative and limit between 1 and 32000")
        if not self.owned(agent_id, digest):
            raise PermissionError("Only artifacts already delivered to this agent are readable")
        artifact_dir = Path(getattr(self.recorder, "artifact_dir", self.run_dir / _ARTIFACT_DIRECTORY))
        descriptor = os.open(artifact_dir / digest, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise PermissionError("Artifact is not a regular file")
            calculated = hashlib.sha256()
            while chunk := handle.read(128000):
                calculated.update(chunk)
            if calculated.hexdigest() != digest:
                raise ValueError("Artifact integrity violation")
            handle.seek(offset)
            content = handle.read(limit + 1)
        return {"hash": digest, "content": content[:limit].decode("utf-8", errors="replace"),
                "offset": offset, "next_offset": offset + limit if len(content) > limit else None,
                "offset_unit": "utf8_bytes"}


class ServiceHub:
    """Dispatch runner RPCs onto the trusted control-plane services."""

    RECORDER_METHODS = ("recorder.append", "recorder.put_artifact",
                        "recorder.read_events", "recorder.latest_event_seq")
    BUDGET_METHODS = ("budget.validate_request", "budget.reserve", "budget.settle",
                      "budget.admit_web", "budget.stop_requested", "budget.wait_admission")
    MEMORY_METHODS = ("memory.read", "memory.write", "memory.list", "memory.search")
    WEB_METHODS = ("web.search", "web.fetch")
    OBSERVER_METHODS = ("observer.records_read", "observer.evidence_read",
                        "observer.report")
    PI_BUDGET_METHODS = ("pi.budget.take", "pi.budget.charge_metadata",
                         "pi.budget.snapshot")
    WORKSPACE_METHODS = ("workspace.read", "workspace.write", "workspace.remove",
                         "workspace.list", "workspace.shell")
    ARTIFACT_METHODS = ("artifact.grant", "artifact.owned", "artifact.read")
    GATEWAY_METHODS = ("gateway.execute", "gateway.submit", "gateway.status",
                       "gateway.cancel", "gateway.wait", "gateway.jobs",
                       "gateway.cancel_job", "gateway.read_events")
    SUPERVISOR_METHODS = ("supervisor.observer_infra_issue",)

    def __init__(self, *, recorder, budget, memory, gateway, workspaces, artifacts,
                 config, observer_callback: Callable | None = None):
        self.recorder, self.budget, self.memory = recorder, budget, memory
        self.gateway, self.workspaces, self.artifacts = gateway, workspaces, artifacts
        self.config = config
        self.observer_callback = observer_callback
        self.manager = None
        self._max_artifact_bytes = min(
            int(config.get("storage", {}).get("max_event_bytes", 4 * 1024 ** 2)) * 64,
            480 * 1024 * 1024)
        # Reference results are stored as bounded artifacts; the raw web config
        # and Budget are shared, while WebTools instances are cached per agent so
        # the bound identity is fixed at construction.
        self._max_reference_artifact_bytes = max(
            1024, int(config.get("storage", {}).get("max_event_bytes", 4 * 1024 ** 2)))
        self._web_tools: dict[str, Any] = {}
        # Observer evidence integrity: the ledger events the observer actually
        # received are persisted (small SQLite under the run directory) so a report
        # can only cite viewed records, including across a restart.
        self._observer_seen_lock = threading.RLock()
        self._observer_seen_memory: set[int] = set()
        self._observer_seen_path = self._observer_seen_database_path()
        self._observer_seen_initialize()
        # Trusted Pi artifact budget, owned by the control plane only.
        self._pi_budget_lock = threading.RLock()
        self._pi_budget_path = self._pi_budget_database_path()
        self._pi_budget_memory: dict[str, dict[str, int]] = {}
        self._pi_budget_initialize()
        self._handlers: dict[str, Callable[[str, str, dict], Awaitable[Any]]] = {}
        for name in (self.RECORDER_METHODS + self.BUDGET_METHODS + self.MEMORY_METHODS +
                     self.WEB_METHODS + self.OBSERVER_METHODS +
                     self.PI_BUDGET_METHODS + self.WORKSPACE_METHODS +
                     self.ARTIFACT_METHODS + self.GATEWAY_METHODS +
                     self.SUPERVISOR_METHODS):
            self._handlers[name] = getattr(self, "_handle_" + name.replace(".", "_"))

    async def handle(self, agent_id, role, method, params):
        if not isinstance(method, str):
            raise ValueError("RPC method must be a string")
        if method.startswith("runner."):
            if self.manager is None:
                raise RunnerControlError("runner control plane is not ready")
            return await self.manager.handle_runner(agent_id, role, method, params)
        handler = self._handlers.get(method)
        if handler is None:
            raise ValueError("unknown RPC method")
        if not isinstance(params, dict):
            raise ValueError("RPC params must be an object")
        return await handler(agent_id, role, params)

    # ---- recorder -----------------------------------------------------
    async def _handle_recorder_append(self, agent_id, role, params):
        kind = params.get("kind")
        payload = params.get("payload")
        if not isinstance(kind, str) or not 1 <= len(kind) <= 200:
            raise ValueError("Invalid ledger event kind")
        if not isinstance(payload, dict):
            raise ValueError("Ledger payload must be an object")
        kwargs = params.get("kwargs") or {}
        if not isinstance(kwargs, dict):
            raise ValueError("Ledger kwargs must be an object")
        allowed = {"source", "world_epoch", "runtime_epoch", "execution_id", "model_call_id"}
        extra = {}
        for key, value in kwargs.items():
            if key in allowed:
                extra[key] = value
        for key in ("world_epoch", "runtime_epoch"):
            if key in extra and (type(extra[key]) is not int or extra[key] < 0):
                raise ValueError(f"Invalid {key}")
        if "source" in extra and (not isinstance(extra["source"], str) or len(extra["source"]) > 64):
            raise ValueError("Invalid event source")
        # The bound connection identity always wins over model-shaped fields.
        return await asyncio.to_thread(self.recorder.append, kind, payload,
                                       actor_id=agent_id, **extra)

    async def _handle_recorder_put_artifact(self, agent_id, role, params):
        text = params.get("text")
        encoded = params.get("base64")
        if (text is None) == (encoded is None):
            raise ValueError("Artifact payload requires exactly one of text or base64")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("Artifact text must be a string")
            data = text.encode("utf-8")
        else:
            if not isinstance(encoded, str):
                raise ValueError("Artifact base64 must be a string")
            try:
                data = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (ValueError, UnicodeError):
                raise ValueError("Artifact base64 is invalid") from None
        if len(data) > self._max_artifact_bytes:
            raise StorageLimitError("artifact exceeds the runner RPC frame limit")
        return await asyncio.to_thread(self.recorder.put_artifact, data)

    async def _handle_recorder_read_events(self, agent_id, role, params):
        if role != "observer":
            raise PermissionError("Only the Observer may read the full ledger through RPC")
        after, limit = params.get("after", 0), params.get("limit", 1000)
        if type(after) is not int or after < 0:
            raise ValueError("Recorder cursor must be nonnegative")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Recorder limit must be between 1 and 1000")
        return await asyncio.to_thread(self.recorder.read_events, after=after, limit=limit)

    async def _handle_recorder_latest_event_seq(self, agent_id, role, params):
        if role != "observer":
            raise PermissionError("Only the Observer may read the full ledger through RPC")
        return await asyncio.to_thread(self.recorder.latest_event_seq)

    # ---- budget -------------------------------------------------------
    async def _handle_budget_validate_request(self, agent_id, role, params):
        request_bytes = params.get("request_bytes")
        if type(request_bytes) is not int or request_bytes < 1:
            raise ValueError("request_bytes must be a positive integer")
        result = self.budget.validate_request(request_bytes)
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            result = await result
        return result

    async def _handle_budget_reserve(self, agent_id, role, params):
        purpose = params.get("purpose", "agent_decision")
        claimed_role = params.get("role", role)
        if claimed_role != role:
            raise PermissionError("Runner cannot reserve budget for another role")
        return await self.budget.reserve(agent_id, purpose=purpose, role=role)

    async def _handle_budget_settle(self, agent_id, role, params):
        reservation_id = params.get("reservation_id")
        usage = params.get("usage")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("reservation_id is required")
        if not isinstance(usage, dict):
            raise ValueError("usage must be an object")
        pending = self.budget.state.get("pending", {}).get(reservation_id)
        if pending is not None and pending.get("agent_id") != agent_id:
            raise PermissionError("budget reservation belongs to another agent")
        if pending is not None and pending.get("role") not in {None, role}:
            raise PermissionError("budget reservation belongs to another role")
        return await self.budget.settle(reservation_id, usage)

    async def _handle_budget_admit_web(self, agent_id, role, params):
        """Durably meter one web search/fetch call for the bound runner agent."""
        tool = params.get("tool", "web")
        if not isinstance(tool, str) or not 1 <= len(tool) <= 64:
            raise ValueError("web tool name must be a short string")
        claimed_agent = params.get("agent_id")
        if claimed_agent is not None and claimed_agent != agent_id:
            raise PermissionError("Runner cannot meter web calls for another agent")
        admitted = await asyncio.to_thread(self.budget.admit_web, agent_id, tool)
        return {"admitted": bool(admitted), "agent_id": agent_id, "tool": tool}

    async def _handle_budget_stop_requested(self, agent_id, role, params):
        return bool(self.budget.stop_requested())

    async def _handle_budget_wait_admission(self, agent_id, role, params):
        # Long-poll until the operator resumes, or report a terminal stop so the
        # runner can exit instead of waiting on a permanently cleared gate.
        while not self.budget.admission.is_set():
            if self.budget.stop_requested():
                return {"admitted": False, "stop": True}
            try:
                await asyncio.wait_for(self.budget.admission.wait(), 0.5)
            except TimeoutError:
                continue
        return {"admitted": True, "stop": bool(self.budget.stop_requested())}

    # ---- web references ------------------------------------------------
    # Pi players call these through the runner-process tool bridge. The bound
    # connection identity is authoritative: no caller-supplied agent id is
    # accepted, and the WebTools budget admission uses the same Budget as the
    # Python AgentLoop so both runtimes share one durable web-call cap.
    def _web_tools_for(self, agent_id):
        tools = self._web_tools.get(agent_id)
        if tools is None:
            from .agents.web import WebTools

            web_config = self.config.get("web", {}) if isinstance(self.config, dict) else {}
            tools = WebTools(web_config if isinstance(web_config, dict) else {},
                             budget=self.budget, agent_id=agent_id)
            self._web_tools[agent_id] = tools
        return tools

    @staticmethod
    def _reject_claimed_agent(params, agent_id):
        claimed = params.get("agent_id")
        if claimed is not None and claimed != agent_id:
            raise PermissionError("Runner cannot use web tools for another agent")

    async def _handle_web_search(self, agent_id, role, params):
        self._reject_claimed_agent(params, agent_id)
        query = params.get("query")
        if not isinstance(query, str) or not 1 <= len(query) <= 1000:
            raise ValueError("Search query must contain 1 to 1000 characters")
        result = await self._web_tools_for(agent_id).search(query)
        await self._record_reference_access(agent_id, "web_search", {"query": query}, result)
        return result

    async def _handle_web_fetch(self, agent_id, role, params):
        self._reject_claimed_agent(params, agent_id)
        url = params.get("url")
        if not isinstance(url, str) or not 1 <= len(url) <= 4096:
            raise ValueError("Reference URL must contain 1 to 4096 characters")
        result = await self._web_tools_for(agent_id).fetch(url)
        await self._record_reference_access(agent_id, "web_fetch", {"url": url}, result)
        return result

    async def _record_reference_access(self, agent_id, tool, query, result):
        """Mirror AgentLoop: durable event plus a bounded result artifact."""
        reference = await asyncio.to_thread(self._reference_artifact, result)
        world_epoch = getattr(self.gateway, "world_epoch", 0)
        if type(world_epoch) is not int or world_epoch < 0:
            world_epoch = 0
        await asyncio.to_thread(
            self.recorder.append, "reference_access",
            {"tool": tool, "query": query, "result_ref": reference},
            actor_id=agent_id, world_epoch=world_epoch)
        if self.artifacts is not None and isinstance(reference, dict) and reference.get("hash"):
            try:
                await asyncio.to_thread(self.artifacts.grant, agent_id, reference["hash"])
            except Exception:
                # The event already carries the hash; an ACL hiccup must not
                # convert a completed reference lookup into a runner failure.
                pass

    def _reference_artifact(self, result):
        encoded = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) > self._max_reference_artifact_bytes:
            # Keep ledger evidence bounded without altering the tool result the
            # runner receives.
            encoded = json.dumps({
                "status": result.get("status") if isinstance(result, dict) else None,
                "truncated": True,
                "reason": "reference result exceeded the bounded ledger artifact",
                "bytes": len(encoded),
            }, ensure_ascii=False).encode("utf-8")
        return self.recorder.put_artifact(encoded)

    # ---- observer evidence / report -----------------------------------
    # These methods back the Pi observer runner. The bound RPC identity must be
    # the Observer (role=="observer", agent=="observer"). Records are bounded and
    # redacted exactly like ObserverAgent; artifact digests are only delivered to
    # the observer when the event actor already owns them (or they are trusted
    # control-plane artifacts, or the observer already owns them); viewed event
    # sequence numbers are persisted so report references can be checked against
    # what the observer actually read; published reports are rebuilt from known
    # fields only and stay advisory (they can never set task success).
    @staticmethod
    def _require_observer(agent_id, role):
        if role != "observer" or agent_id != _OBSERVER_AGENT_ID:
            raise PermissionError("Only the bound Observer runner may use observer RPC methods")

    def _observer_artifacts(self):
        if self.artifacts is None:
            raise RunnerControlError("observer artifact vault is unavailable")
        return self.artifacts

    # -- viewed-event persistence ---------------------------------------
    def _runner_agent_ids(self):
        """Identities whose ledger events are authored by independent runners."""
        ids = {_OBSERVER_AGENT_ID}
        agents = self.config.get("agents", {}) if isinstance(self.config, dict) else {}
        configured = agents.get("ids") if isinstance(agents, dict) else None
        if isinstance(configured, list):
            ids.update(item for item in configured
                       if isinstance(item, str) and item)
        return ids

    def _observer_seen_database_path(self):
        run_dir = getattr(self.recorder, "run_dir", None)
        if run_dir is None:
            return None
        return Path(run_dir) / "observer-seen.sqlite3"

    def _observer_seen_initialize(self):
        path = self._observer_seen_path
        if path is None:
            return
        try:
            with self._observer_seen_lock, sqlite3.connect(path, timeout=10) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS seen_events (event_seq INTEGER PRIMARY KEY)")
                connection.commit()
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except sqlite3.Error as exc:
            raise RunnerControlError(
                "could not initialize the observer seen-event index: " + str(exc)) from exc

    def _observer_mark_seen(self, sequences):
        values = sorted({int(sequence) for sequence in sequences
                         if type(sequence) is int and sequence >= 0})
        if not values:
            return
        path = self._observer_seen_path
        if path is None:
            self._observer_seen_memory.update(values)
            return
        try:
            with self._observer_seen_lock, sqlite3.connect(path, timeout=10) as connection:
                connection.executemany(
                    "INSERT OR IGNORE INTO seen_events(event_seq) VALUES(?)",
                    [(sequence,) for sequence in values])
                connection.commit()
        except sqlite3.Error as exc:
            raise RunnerControlError(
                "could not persist the observer seen-event index: " + str(exc)) from exc

    def _observer_seen_many(self, sequences):
        wanted = sorted({int(sequence) for sequence in sequences
                         if type(sequence) is int and sequence >= 0})
        if not wanted:
            return set()
        path = self._observer_seen_path
        if path is None:
            return {sequence for sequence in wanted if sequence in self._observer_seen_memory}
        seen: set[int] = set()
        try:
            with self._observer_seen_lock, sqlite3.connect(path, timeout=10) as connection:
                for offset in range(0, len(wanted), 500):
                    chunk = wanted[offset:offset + 500]
                    placeholders = ",".join("?" * len(chunk))
                    seen.update(int(row[0]) for row in connection.execute(
                        f"SELECT event_seq FROM seen_events WHERE event_seq IN ({placeholders})",
                        chunk))
        except sqlite3.Error as exc:
            raise RunnerControlError(
                "could not read the observer seen-event index: " + str(exc)) from exc
        return seen

    def _observer_seen_max(self):
        path = self._observer_seen_path
        if path is None:
            return max(self._observer_seen_memory, default=0)
        try:
            with self._observer_seen_lock, sqlite3.connect(path, timeout=10) as connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(event_seq), 0) FROM seen_events").fetchone()
        except sqlite3.Error as exc:
            raise RunnerControlError(
                "could not read the observer seen-event index: " + str(exc)) from exc
        return int(row[0] or 0)

    # -- records / evidence ---------------------------------------------
    async def _handle_observer_records_read(self, agent_id, role, params):
        self._require_observer(agent_id, role)
        after = params.get("after", 0)
        limit = params.get("limit", 50)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Records cursor must be nonnegative, limit between 1 and 100")
        return await asyncio.to_thread(self._observer_records_page, after, limit)

    def _observer_records_page(self, after, limit):
        artifacts = self._observer_artifacts()
        latest = int(self.recorder.latest_event_seq())
        events = self.recorder.read_events(after=after, limit=limit)
        selected, size, cursor = [], 0, int(after)
        truncated = len(events) >= limit
        for event in events:
            if not isinstance(event, dict):
                continue
            sequence = event.get("event_seq")
            if type(sequence) is not int:
                continue
            # The Observer's own model bookkeeping is not game evidence; it is
            # skipped without being returned or granted.
            if event.get("actor_id") == _OBSERVER_AGENT_ID:
                cursor = sequence
                continue
            payload = event.get("payload", {})
            original = event
            if (event.get("kind") == "effective_environment_verified"
                    or (isinstance(payload, dict) and payload.get("operator_only") is True)):
                # Operator-only records keep their cursor visible but leak no
                # payload and grant no embedded artifacts.
                event = {key: value for key, value in event.items() if key != "payload"}
                event["payload"] = {"operator_only": True, "redacted": True}
                original = None
            encoded = json.dumps(event, ensure_ascii=False, default=str)
            if isinstance(payload, dict) and len(encoded) > _OBSERVER_EVENT_JSON_LIMIT:
                reference = self.recorder.put_artifact(
                    json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
                # This artifact is created by the trusted ServiceHub itself.
                artifacts.grant(_OBSERVER_AGENT_ID, reference["hash"])
                event = {key: value for key, value in event.items() if key != "payload"}
                event["payload"] = {"bounded": True, "original_ref": reference}
                encoded = json.dumps(event, ensure_ascii=False, default=str)
            if selected and size + len(encoded) > _OBSERVER_PAGE_BYTES:
                truncated = True
                break
            selected.append(event)
            if original is not None:
                self._grant_observer_artifacts(original.get("actor_id"),
                                               original.get("payload"), artifacts)
            cursor = sequence
            size += len(encoded)
        # Persist exactly what the model was shown so reports can prove "viewed".
        self._observer_mark_seen(event.get("event_seq") for event in selected)
        return {"events": selected, "next_cursor": cursor,
                "latest_event_seq": latest, "truncated": truncated}

    def _grant_observer_artifacts(self, actor_id, value, artifacts):
        """Deliver only event-actor-owned or control-plane artifact digests.

        Player-authored payloads are model-controlled. A player must not be able
        to widen the Observer ACL to another agent's artifacts by planting a
        digest inside its own ledger event, so a runner-actor event can only
        deliver artifacts that actor already owns (or was granted) and never a
        digest owned by a different runner. Trusted control-plane events (actor
        ids outside the runner set) are produced by harness code and may hand the
        Observer the harness-created evidence artifacts they reference.
        """
        digests = set()

        def walk(item):
            if isinstance(item, dict):
                digest = item.get("hash", item.get("sha256"))
                if isinstance(digest, str) and _ARTIFACT_DIGEST_PATTERN.fullmatch(digest):
                    digests.add(digest)
                for child in item.values():
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)

        walk(value)
        if not digests:
            return
        runner_ids = self._runner_agent_ids()
        actor_is_runner = isinstance(actor_id, str) and actor_id in runner_ids
        for digest in sorted(digests):
            if artifacts.owned(_OBSERVER_AGENT_ID, digest):
                continue
            if actor_is_runner:
                if artifacts.owned(actor_id, digest):
                    artifacts.grant(_OBSERVER_AGENT_ID, digest)
                    continue
                if any(artifacts.owned(other, digest) for other in runner_ids
                       if other not in {_OBSERVER_AGENT_ID, actor_id}):
                    # Another runner's artifact planted in this event: do not
                    # widen the Observer ACL.
                    continue
                # Unowned artifact referenced by the runner (e.g. a gateway
                # execution source/result artifact created by the control plane).
                artifacts.grant(_OBSERVER_AGENT_ID, digest)
                continue
            if isinstance(actor_id, str):
                # Trusted control-plane authored record.
                artifacts.grant(_OBSERVER_AGENT_ID, digest)

    async def _handle_observer_evidence_read(self, agent_id, role, params):
        self._require_observer(agent_id, role)
        digest = params.get("hash")
        if not isinstance(digest, str) or not _ARTIFACT_DIGEST_PATTERN.fullmatch(digest):
            raise ValueError("Evidence hash must be a SHA256 digest")
        offset = params.get("offset", 0)
        limit = params.get("limit", 12000)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32000:
            raise ValueError("Evidence offset must be nonnegative and limit between 1 and 32000")
        artifacts = self._observer_artifacts()
        # ArtifactVault.read enforces the observer's grant (PermissionError when
        # the digest was never delivered) and the same offset/limit bounds.
        return await asyncio.to_thread(artifacts.read, _OBSERVER_AGENT_ID, digest, offset, limit)

    # -- report ---------------------------------------------------------
    async def _handle_observer_report(self, agent_id, role, params):
        self._require_observer(agent_id, role)
        report = await asyncio.to_thread(self._validated_observer_report, params)
        event = await asyncio.to_thread(self._append_observer_report, report)
        return {"status": "recorded", "event_seq": event["event_seq"],
                "advisory": True,
                "progress_summary_present": bool(report["progress_summary"])}

    def _validated_observer_report(self, params):
        seen_max = self._observer_seen_max()
        through_cursor = params.get("through_cursor")
        if through_cursor is None:
            through_cursor = seen_max
        elif (type(through_cursor) is not int or through_cursor < 0
              or through_cursor > seen_max):
            raise ValueError(
                "through_cursor must not exceed the last records page this observer read")
        findings = params.get("findings")
        if not isinstance(findings, list) or len(findings) > 100:
            raise ValueError("Report findings must be an array of at most 100 entries")
        clean_findings = []
        for finding in findings:
            if (not isinstance(finding, dict)
                    or finding.get("status") not in _OBSERVER_FINDING_STATUSES
                    or not isinstance(finding.get("claim"), str)
                    or len(finding["claim"]) > 4000):
                raise ValueError("Invalid observer finding")
            clean_findings.append({
                "status": finding["status"],
                "claim": finding["claim"],
                "evidence_refs": self._validate_observer_evidence_refs(
                    finding.get("evidence_refs")),
            })
        progress = params.get("progress_summary")
        if progress is not None and (not isinstance(progress, str) or len(progress) > 4000):
            raise ValueError("Observer progress summary must be a string of at most 4000 characters")
        verified = params.get("verified_events", [])
        if not isinstance(verified, list) or len(verified) > 100:
            raise ValueError("verified_events must be an array of at most 100 entries")
        clean_verified = []
        for item in verified:
            if (not isinstance(item, dict) or not isinstance(item.get("claim"), str)
                    or len(item["claim"]) > 4000):
                raise ValueError("Invalid verified event")
            clean_verified.append({
                "claim": item["claim"],
                "evidence_refs": self._validate_observer_evidence_refs(
                    item.get("evidence_refs")),
            })
        infrastructure = params.get("infrastructure_findings", [])
        if (not isinstance(infrastructure, list) or len(infrastructure) > 20
                or any(not isinstance(item, str) or len(item) > 2000
                       for item in infrastructure)):
            raise ValueError("infrastructure_findings must be at most 20 short strings")
        # Rebuild from known fields only: a direct RPC caller cannot smuggle
        # success/reward-like keys into the durable observer_report event.
        return {"findings": clean_findings, "progress_summary": progress or "",
                "verified_events": clean_verified,
                "infrastructure_findings": infrastructure,
                "advisory": True, "through_cursor": through_cursor}

    def _validate_observer_evidence_refs(self, refs):
        if not isinstance(refs, list) or not 1 <= len(refs) <= 20:
            raise ValueError("Each finding/request requires 1 to 20 viewed evidence references")
        artifacts = self._observer_artifacts()
        parsed, sequences = [], []
        for reference in refs:
            if type(reference) is int:
                parsed.append(("event", reference))
                sequences.append(reference)
            elif isinstance(reference, dict):
                sequence = reference.get("event_seq")
                digest = reference.get("hash", reference.get("sha256"))
                if sequence is not None:
                    if type(sequence) is not int:
                        raise ValueError("Evidence event reference must be an integer")
                    parsed.append(("event", sequence))
                    sequences.append(sequence)
                elif isinstance(digest, str) and _ARTIFACT_DIGEST_PATTERN.fullmatch(digest):
                    parsed.append(("artifact", digest))
                else:
                    raise ValueError("Evidence reference must be a viewed event sequence "
                                     "or an owned artifact hash")
            elif isinstance(reference, str) and _ARTIFACT_DIGEST_PATTERN.fullmatch(reference):
                parsed.append(("artifact", reference))
            else:
                raise ValueError("Evidence reference must be a viewed event sequence or an "
                                 "owned artifact hash")
        seen = self._observer_seen_many(sequences) if sequences else set()
        normalized = []
        for kind, value in parsed:
            if kind == "event":
                if value not in seen:
                    raise PermissionError("Evidence event was not read by this observer")
                normalized.append(value)
            else:
                if not artifacts.owned(_OBSERVER_AGENT_ID, value):
                    raise PermissionError(
                        "Evidence artifact was not delivered to this observer")
                normalized.append({"hash": value})
        return normalized

    def _append_observer_report(self, report):
        world_epoch = getattr(self.gateway, "world_epoch", 0)
        if type(world_epoch) is not int or world_epoch < 0:
            world_epoch = 0
        return self.recorder.append("observer_report", report,
                                    actor_id=_OBSERVER_AGENT_ID,
                                    world_epoch=world_epoch)

    # ---- trusted Pi artifact budget (control plane) -------------------
    # ``ArtifactBudget`` in the runner is a model-writable cache only; the
    # authoritative used/metadata counters live in this SQLite file under the run
    # directory, which is never mounted into a runner container.
    def _pi_budget_database_path(self):
        run_dir = getattr(self.recorder, "run_dir", None)
        if run_dir is None:
            return None
        return Path(run_dir) / _PI_BUDGET_STATE_FILE

    def _pi_budget_limits(self) -> tuple[int, int]:
        storage = self.config.get("storage", {}) if isinstance(self.config, dict) else {}
        if not isinstance(storage, dict):
            storage = {}
        try:
            per_agent = int(storage.get("pi_artifact_max_bytes",
                                        _PI_BUDGET_DEFAULT_ARTIFACT_BYTES))
        except (TypeError, ValueError):
            per_agent = _PI_BUDGET_DEFAULT_ARTIFACT_BYTES
        global_cap = storage.get("pi_artifact_global_max_bytes")
        if global_cap is None:
            global_cap = storage.get("max_run_bytes", _PI_BUDGET_DEFAULT_GLOBAL_BYTES)
        try:
            global_bytes = int(global_cap)
        except (TypeError, ValueError):
            global_bytes = _PI_BUDGET_DEFAULT_GLOBAL_BYTES
        return max(0, per_agent), max(0, global_bytes)

    def _pi_budget_initialize(self) -> None:
        path = self._pi_budget_path
        if path is None:
            return
        try:
            with self._pi_budget_lock, sqlite3.connect(path, timeout=15) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS pi_budget ("
                    "agent_id TEXT PRIMARY KEY,"
                    "used_bytes INTEGER NOT NULL DEFAULT 0,"
                    "metadata_bytes INTEGER NOT NULL DEFAULT 0,"
                    "retained_count INTEGER NOT NULL DEFAULT 0,"
                    "retention_recorded INTEGER NOT NULL DEFAULT 0,"
                    "pressure_recorded INTEGER NOT NULL DEFAULT 0)")
                connection.commit()
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except sqlite3.Error as exc:
            raise RunnerControlError(
                "could not initialize the trusted Pi artifact budget: " + str(exc)) from exc

    @staticmethod
    def _pi_budget_row(connection, agent_id: str) -> dict[str, int]:
        row = connection.execute(
            "SELECT used_bytes, metadata_bytes, retained_count, retention_recorded,"
            " pressure_recorded FROM pi_budget WHERE agent_id = ?",
            (agent_id,)).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO pi_budget(agent_id) VALUES(?)", (agent_id,))
            return {"used_bytes": 0, "metadata_bytes": 0, "retained_count": 0,
                    "retention_recorded": 0, "pressure_recorded": 0}
        return {"used_bytes": int(row[0]), "metadata_bytes": int(row[1]),
                "retained_count": int(row[2]), "retention_recorded": int(row[3]),
                "pressure_recorded": int(row[4])}

    def _pi_budget_guard(self, agent_id, role) -> None:
        if not isinstance(agent_id, str) or not agent_id:
            raise PermissionError("Pi budget needs a bound agent identity")
        if role == "observer":
            if agent_id != _OBSERVER_AGENT_ID:
                raise PermissionError("Only the bound Observer may use the observer budget")
            return
        if role == "player":
            players = self._runner_agent_ids() - {_OBSERVER_AGENT_ID}
            if agent_id not in players:
                raise PermissionError("Pi budget is bound to a configured player agent")
            return
        raise PermissionError("Only runner identities may use the Pi artifact budget")

    @staticmethod
    def _pi_budget_validate_size(value) -> int:
        if type(value) is not int or value < 0 or value > 2 ** 31:
            raise ValueError("Pi artifact budget bytes must be a nonnegative integer")
        return value

    def _pi_budget_response(self, row: dict[str, int], per_agent: int,
                            global_used: int, global_limit: int) -> dict[str, Any]:
        return {"used_bytes": row["used_bytes"], "limit_bytes": per_agent,
                "metadata_bytes": row["metadata_bytes"],
                "retained_count": row["retained_count"],
                "retention_recorded": bool(row["retention_recorded"]),
                "pressure_recorded": bool(row["pressure_recorded"]),
                "global_used_bytes": global_used, "global_limit_bytes": global_limit}

    async def _handle_pi_budget_take(self, agent_id, role, params):
        self._pi_budget_guard(agent_id, role)
        size = self._pi_budget_validate_size(params.get("bytes"))
        return await asyncio.to_thread(self._pi_budget_take_sync, agent_id, size)

    async def _handle_pi_budget_charge_metadata(self, agent_id, role, params):
        self._pi_budget_guard(agent_id, role)
        size = self._pi_budget_validate_size(params.get("bytes"))
        return await asyncio.to_thread(self._pi_budget_charge_metadata_sync, agent_id, size)

    async def _handle_pi_budget_snapshot(self, agent_id, role, params):
        self._pi_budget_guard(agent_id, role)
        return await asyncio.to_thread(self._pi_budget_snapshot_sync, agent_id)

    def _pi_budget_take_sync(self, agent_id, size: int) -> dict[str, Any]:
        per_agent, global_limit = self._pi_budget_limits()
        with self._pi_budget_lock:
            if self._pi_budget_path is None:
                return self._pi_budget_memory_take(agent_id, size, per_agent, global_limit)
            connection = sqlite3.connect(self._pi_budget_path, timeout=15,
                                         isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._pi_budget_row(connection, agent_id)
                global_used = int(connection.execute(
                    "SELECT COALESCE(SUM(used_bytes), 0) FROM pi_budget").fetchone()[0])
                allowed = (row["used_bytes"] + size <= per_agent
                           and global_used + size <= global_limit)
                response = {"allowed": allowed}
                if allowed:
                    response["used_bytes"] = row["used_bytes"] + size
                    if (not row["pressure_recorded"] and per_agent > 0
                            and response["used_bytes"] >= per_agent
                            * _PI_BUDGET_PRESSURE_THRESHOLD):
                        response["pressure_recorded"] = True
                        response["pressure"] = {
                            "event": "pi_retention_pressure", "kind": "artifact",
                            "reason": "pi_artifact_max_bytes_pressure",
                            "used_bytes": response["used_bytes"],
                            "limit_bytes": per_agent,
                            "percent": round(100.0 * response["used_bytes"] / per_agent, 2),
                            "threshold": _PI_BUDGET_PRESSURE_THRESHOLD}
                        connection.execute(
                            "UPDATE pi_budget SET used_bytes = ?, metadata_bytes = ?,"
                            " retained_count = ?, retention_recorded = ?,"
                            " pressure_recorded = 1 WHERE agent_id = ?",
                            (response["used_bytes"], row["metadata_bytes"],
                             row["retained_count"], row["retention_recorded"], agent_id))
                    else:
                        connection.execute(
                            "UPDATE pi_budget SET used_bytes = ? WHERE agent_id = ?",
                            (response["used_bytes"], agent_id))
                    global_used += size
                else:
                    response["retained_count"] = row["retained_count"] + 1
                    reason = ("pi_artifact_global_max_bytes"
                              if global_used + size > global_limit
                              else "pi_artifact_max_bytes")
                    if not row["retention_recorded"]:
                        response["retention_recorded"] = True
                        response["retention"] = {
                            "event": "pi_artifact_retention", "reason": reason,
                            "limit_bytes": (global_limit if reason
                                            == "pi_artifact_global_max_bytes"
                                            else per_agent),
                            "used_bytes": row["used_bytes"],
                            "retained_count": response["retained_count"]}
                        connection.execute(
                            "UPDATE pi_budget SET retained_count = ?,"
                            " retention_recorded = 1 WHERE agent_id = ?",
                            (response["retained_count"], agent_id))
                    else:
                        connection.execute(
                            "UPDATE pi_budget SET retained_count = ? WHERE agent_id = ?",
                            (response["retained_count"], agent_id))
                connection.commit()
                fresh = {"used_bytes": response.get("used_bytes", row["used_bytes"]),
                         "metadata_bytes": row["metadata_bytes"],
                         "retained_count": response.get("retained_count",
                                                        row["retained_count"]),
                         "retention_recorded": int(response.get(
                             "retention_recorded", row["retention_recorded"])),
                         "pressure_recorded": int(response.get(
                             "pressure_recorded", row["pressure_recorded"]))}
                response.update(self._pi_budget_response(fresh, per_agent,
                                                         global_used, global_limit))
                return response
            except sqlite3.Error as exc:
                raise RunnerControlError(
                    "trusted Pi artifact budget write failed: " + str(exc)) from exc
            finally:
                connection.close()

    def _pi_budget_charge_metadata_sync(self, agent_id, size: int) -> dict[str, Any]:
        per_agent, global_limit = self._pi_budget_limits()
        with self._pi_budget_lock:
            if self._pi_budget_path is None:
                return self._pi_budget_memory_charge(agent_id, size, per_agent, global_limit)
            connection = sqlite3.connect(self._pi_budget_path, timeout=15,
                                         isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._pi_budget_row(connection, agent_id)
                used = row["used_bytes"] + size
                response: dict[str, Any] = {"used_bytes": used,
                                            "metadata_bytes": row["metadata_bytes"] + size,
                                            "retained_count": row["retained_count"]}
                pressure = False
                if (not row["pressure_recorded"] and per_agent > 0
                        and used >= per_agent * _PI_BUDGET_PRESSURE_THRESHOLD):
                    pressure = True
                    response["pressure_recorded"] = True
                    response["pressure"] = {
                        "event": "pi_retention_pressure", "kind": "artifact",
                        "reason": "pi_artifact_max_bytes_pressure",
                        "used_bytes": used, "limit_bytes": per_agent,
                        "percent": round(100.0 * used / per_agent, 2),
                        "threshold": _PI_BUDGET_PRESSURE_THRESHOLD}
                connection.execute(
                    "UPDATE pi_budget SET used_bytes = ?, metadata_bytes = ?,"
                    " pressure_recorded = ? WHERE agent_id = ?",
                    (used, response["metadata_bytes"],
                     1 if pressure else row["pressure_recorded"], agent_id))
                connection.commit()
                global_used = int(connection.execute(
                    "SELECT COALESCE(SUM(used_bytes), 0) FROM pi_budget").fetchone()[0])
                fresh = {"used_bytes": used, "metadata_bytes": response["metadata_bytes"],
                         "retained_count": row["retained_count"],
                         "retention_recorded": row["retention_recorded"],
                         "pressure_recorded": (1 if pressure
                                               else row["pressure_recorded"])}
                response.update(self._pi_budget_response(fresh, per_agent,
                                                         global_used, global_limit))
                return response
            except sqlite3.Error as exc:
                raise RunnerControlError(
                    "trusted Pi artifact budget write failed: " + str(exc)) from exc
            finally:
                connection.close()

    def _pi_budget_snapshot_sync(self, agent_id: str) -> dict[str, Any]:
        per_agent, global_limit = self._pi_budget_limits()
        with self._pi_budget_lock:
            if self._pi_budget_path is None:
                row = dict(self._pi_budget_memory.get(agent_id) or {})
                row.setdefault("used_bytes", 0)
                row.setdefault("metadata_bytes", 0)
                row.setdefault("retained_count", 0)
                row.setdefault("retention_recorded", 0)
                row.setdefault("pressure_recorded", 0)
                global_used = sum(int(item.get("used_bytes", 0))
                                  for item in self._pi_budget_memory.values())
                agents = [{"agent_id": key, **value}
                          for key, value in self._pi_budget_memory.items()]
                return self._pi_budget_snapshot_response(
                    row, agents, per_agent, global_used, global_limit)
            connection = sqlite3.connect(self._pi_budget_path, timeout=15)
            try:
                row = self._pi_budget_row(connection, agent_id)
                rows = connection.execute(
                    "SELECT agent_id, used_bytes, metadata_bytes, retained_count,"
                    " retention_recorded, pressure_recorded FROM pi_budget"
                    " ORDER BY agent_id").fetchall()
                agents = [{"agent_id": item[0], "used_bytes": int(item[1]),
                           "metadata_bytes": int(item[2]),
                           "retained_count": int(item[3]),
                           "retention_recorded": bool(item[4]),
                           "pressure_recorded": bool(item[5])} for item in rows]
                global_used = sum(item["used_bytes"] for item in agents)
                return self._pi_budget_snapshot_response(
                    row, agents, per_agent, global_used, global_limit)
            except sqlite3.Error as exc:
                raise RunnerControlError(
                    "trusted Pi artifact budget read failed: " + str(exc)) from exc
            finally:
                connection.close()

    def _pi_budget_snapshot_response(self, row, agents, per_agent, global_used,
                                     global_limit) -> dict[str, Any]:
        return {"self": self._pi_budget_response(row, per_agent, global_used,
                                                 global_limit),
                "agents": agents,
                "limits": {"per_agent_bytes": per_agent,
                           "global_bytes": global_limit},
                "global": {"used_bytes": global_used, "limit_bytes": global_limit}}

    def _pi_budget_memory_take(self, agent_id, size, per_agent, global_limit):
        row = self._pi_budget_memory.setdefault(
            agent_id, {"used_bytes": 0, "metadata_bytes": 0, "retained_count": 0,
                       "retention_recorded": 0, "pressure_recorded": 0})
        global_used = sum(int(item["used_bytes"])
                          for item in self._pi_budget_memory.values())
        allowed = (row["used_bytes"] + size <= per_agent
                   and global_used + size <= global_limit)
        response: dict[str, Any] = {"allowed": allowed}
        if allowed:
            row["used_bytes"] += size
            if (not row["pressure_recorded"] and per_agent > 0
                    and row["used_bytes"] >= per_agent * _PI_BUDGET_PRESSURE_THRESHOLD):
                row["pressure_recorded"] = 1
                response["pressure"] = {
                    "event": "pi_retention_pressure", "kind": "artifact",
                    "reason": "pi_artifact_max_bytes_pressure",
                    "used_bytes": row["used_bytes"], "limit_bytes": per_agent,
                    "percent": round(100.0 * row["used_bytes"] / per_agent, 2),
                    "threshold": _PI_BUDGET_PRESSURE_THRESHOLD}
            global_used += size
        else:
            row["retained_count"] += 1
            if not row["retention_recorded"]:
                row["retention_recorded"] = 1
                reason = ("pi_artifact_global_max_bytes"
                          if global_used + size > global_limit
                          else "pi_artifact_max_bytes")
                response["retention"] = {
                    "event": "pi_artifact_retention", "reason": reason,
                    "limit_bytes": (global_limit if reason
                                    == "pi_artifact_global_max_bytes" else per_agent),
                    "used_bytes": row["used_bytes"],
                    "retained_count": row["retained_count"]}
        response.update(self._pi_budget_response(row, per_agent, global_used,
                                                 global_limit))
        return response

    def _pi_budget_memory_charge(self, agent_id, size, per_agent, global_limit):
        row = self._pi_budget_memory.setdefault(
            agent_id, {"used_bytes": 0, "metadata_bytes": 0, "retained_count": 0,
                       "retention_recorded": 0, "pressure_recorded": 0})
        row["used_bytes"] += size
        row["metadata_bytes"] += size
        response: dict[str, Any] = {}
        if (not row["pressure_recorded"] and per_agent > 0
                and row["used_bytes"] >= per_agent * _PI_BUDGET_PRESSURE_THRESHOLD):
            row["pressure_recorded"] = 1
            response["pressure"] = {
                "event": "pi_retention_pressure", "kind": "artifact",
                "reason": "pi_artifact_max_bytes_pressure",
                "used_bytes": row["used_bytes"], "limit_bytes": per_agent,
                "percent": round(100.0 * row["used_bytes"] / per_agent, 2),
                "threshold": _PI_BUDGET_PRESSURE_THRESHOLD}
        global_used = sum(int(item["used_bytes"])
                          for item in self._pi_budget_memory.values())
        response.update(self._pi_budget_response(row, per_agent, global_used,
                                                 global_limit))
        return response

    # ---- memory -------------------------------------------------------
    async def _handle_memory_read(self, agent_id, role, params):
        kwargs = {key: value for key, value in params.items() if key != "actor_id"}
        return await asyncio.to_thread(self.memory.read, agent_id, **kwargs)

    async def _handle_memory_write(self, agent_id, role, params):
        kwargs = {key: value for key, value in params.items() if key != "actor_id"}
        return await asyncio.to_thread(self.memory.write, agent_id, **kwargs)

    async def _handle_memory_list(self, agent_id, role, params):
        kwargs = {key: value for key, value in params.items() if key != "actor_id"}
        return await asyncio.to_thread(self.memory.list, agent_id, **kwargs)

    async def _handle_memory_search(self, agent_id, role, params):
        kwargs = {key: value for key, value in params.items() if key != "actor_id"}
        return await asyncio.to_thread(self.memory.search, agent_id, **kwargs)

    # ---- workspace ----------------------------------------------------
    def _workspace(self, agent_id):
        workspace = self.workspaces.get(agent_id)
        if workspace is None:
            raise PermissionError("No workspace is provisioned for this agent")
        return workspace

    async def _handle_workspace_read(self, agent_id, role, params):
        return await asyncio.to_thread(self._workspace(agent_id).read, **params)

    async def _handle_workspace_write(self, agent_id, role, params):
        return await asyncio.to_thread(self._workspace(agent_id).write, **params)

    async def _handle_workspace_remove(self, agent_id, role, params):
        return await asyncio.to_thread(self._workspace(agent_id).remove, **params)

    async def _handle_workspace_list(self, agent_id, role, params):
        return await asyncio.to_thread(self._workspace(agent_id).list, **params)

    async def _handle_workspace_shell(self, agent_id, role, params):
        return await self._workspace(agent_id).shell(**params)

    # ---- artifacts ----------------------------------------------------
    async def _handle_artifact_grant(self, agent_id, role, params):
        digest = params.get("hash")
        return await asyncio.to_thread(self.artifacts.grant, agent_id, digest)

    async def _handle_artifact_owned(self, agent_id, role, params):
        digest = params.get("hash")
        return await asyncio.to_thread(self.artifacts.owned, agent_id, digest)

    async def _handle_artifact_read(self, agent_id, role, params):
        digest = params.get("hash")
        offset = params.get("offset", 0)
        limit = params.get("limit", 12000)
        return await asyncio.to_thread(self.artifacts.read, agent_id, digest, offset, limit)

    # ---- execution gateway -------------------------------------------
    async def _handle_gateway_execute(self, agent_id, role, params):
        return await self.gateway.execute(
            agent_id, params.get("source"), timeout_ms=params.get("timeout_ms", 60000),
            request_id=params.get("request_id"))

    async def _handle_gateway_submit(self, agent_id, role, params):
        return await self.gateway.submit(
            agent_id, params.get("source"), timeout_ms=params.get("timeout_ms", 60000),
            request_id=params.get("request_id"))

    async def _handle_gateway_status(self, agent_id, role, params):
        return await self.gateway.status(agent_id, params.get("execution_id"))

    async def _handle_gateway_cancel(self, agent_id, role, params):
        return await self.gateway.cancel(agent_id, params.get("execution_id"))

    async def _handle_gateway_wait(self, agent_id, role, params):
        return await self.gateway.wait(agent_id, params.get("execution_id"),
                                       wait_ms=params.get("wait_ms", 1000))

    async def _handle_gateway_jobs(self, agent_id, role, params):
        return await self.gateway.jobs(agent_id)

    async def _handle_gateway_cancel_job(self, agent_id, role, params):
        return await self.gateway.cancel_job(agent_id, params.get("job_id"))

    async def _handle_gateway_read_events(self, agent_id, role, params):
        return await self.gateway.read_events(
            agent_id, after_cursor=params.get("after_cursor", 0),
            limit=params.get("limit", 100))

    # ---- supervisor ---------------------------------------------------
    async def _handle_supervisor_observer_infra_issue(self, agent_id, role, params):
        if role != "observer":
            raise PermissionError("Only the Observer may submit infrastructure issues")
        if self.observer_callback is None:
            raise RunnerControlError("observer infrastructure callback is unavailable")
        issue = params.get("issue") if isinstance(params.get("issue"), dict) else params
        return await self.observer_callback(issue)


def _identity(agent_id):
    if not isinstance(agent_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", agent_id):
        raise ValueError("Invalid runner agent identity")
    return agent_id


class RunnerManager:
    """Spawn, supervise, restart and stop one independent process per agent."""

    def __init__(self, *, run_dir, config, specs, token_map, service_handler,
                 runner_env, recorder=None, budget=None, python=None,
                 runner_module=None,
                 model_forwarder_socket=None, model_forwarder_token=None,
                 on_runner_lost: Callable[[str], Awaitable[None]] | None = None,
                 log_max_bytes: int = 64 * 1024 ** 2,
                 docker_bin=None, container_source_root=None,
                 container_python=None):
        self.run_dir = Path(run_dir).resolve()
        self.config = config
        self.specs = dict(specs)
        self.agents = list(self.specs)
        self.roles = {identity["agent_id"]: identity["role"] for identity in token_map.values()}
        if set(self.roles) != set(self.agents):
            raise ValueError("runner token identities must match runner specs")
        if len({token for token in token_map}) != len(token_map):
            raise ValueError("runner tokens must be unique")
        self.token_by_agent = {}
        for token, identity in token_map.items():
            self.token_by_agent[_identity(identity["agent_id"])] = token
        self.service_handler = service_handler
        self.runner_env = dict(runner_env)
        self.recorder, self.budget = recorder, budget
        self.python = python or sys.executable
        self.runner_module = runner_module
        self.on_runner_lost = on_runner_lost
        self.log_max_bytes = max(1024 ** 2, int(log_max_bytes))
        self.runners_dir = self.run_dir / "runners"
        self.config_supervision = config.get("supervision", {}) if isinstance(config, dict) else {}
        runtime = config.get("runtime", {}) if isinstance(config, dict) else {}
        self.runner_mode = runtime.get("runner_mode", "process")
        if self.runner_mode == "inprocess":
            # Direct RunnerManager users/tests may pass a default strict config;
            # the manager itself only knows the process and container launchers.
            self.runner_mode = "process"
        if self.runner_mode not in {"process", "container"}:
            raise RunnerControlError("RunnerManager requires process or container runner_mode")
        self.run_id = str(next(iter(self.specs.values())).get("run_id", self.run_dir.name))
        self._run_slug = hashlib.sha256(self.run_id.encode("utf-8")).hexdigest()[:10]
        self.container_image = runtime.get(
            "runner_container_image", "minecraft-harness-workspace:local")
        self.container_network = runtime.get("runner_container_network", "bridge")
        self.runner_egress = resolve_runner_egress(config)
        self.model_forwarder_socket = (
            Path(model_forwarder_socket).resolve()
            if isinstance(model_forwarder_socket, (str, os.PathLike)) and
            str(model_forwarder_socket).strip() else None)
        self.model_forwarder_token = model_forwarder_token
        self.container_memory_mb = int(runtime.get("runner_container_memory_mb", 1024))
        self.container_cpus = float(runtime.get("runner_container_cpus", 1.0))
        self.container_read_only = bool(runtime.get("runner_container_read_only", True))
        configured_user = runtime.get("runner_container_user")
        if configured_user is None:
            try:
                stat_result = self.run_dir.stat()
                configured_user = f"{stat_result.st_uid}:{stat_result.st_gid}"
            except OSError:
                configured_user = None
        self.container_user = configured_user
        self.docker_bin = docker_bin or shutil.which("docker")
        self.container_source_root = Path(
            container_source_root or Path(__file__).resolve().parents[2]).resolve()
        self.container_python = str(
            container_python or runtime.get("runner_container_python", "/usr/bin/python3.11"))
        site = list(self.container_source_root.glob(".venv/lib/python3.*/site-packages"))
        self.container_site_packages = site[0] if site else None
        self.container_run_id = self.run_id
        self.runner_runtime = str(runtime.get("runner_runtime", "python"))
        if self.runner_runtime not in {"python", "pi"}:
            raise RunnerControlError(
                "runtime.runner_runtime must be python or pi, not " +
                repr(self.runner_runtime))
        pi_cli = runtime.get("pi_cli")
        if not isinstance(pi_cli, str) or not pi_cli.strip():
            pi_cli = _DEFAULT_PI_CLI_RELATIVE
        pi_cli_path = Path(pi_cli.strip()).expanduser()
        if not pi_cli_path.is_absolute():
            pi_cli_path = self.container_source_root / pi_cli_path
        self.pi_cli = pi_cli_path.resolve()
        try:
            self.pi_context_window = int(runtime.get("pi_context_window",
                                                      _DEFAULT_PI_CONTEXT_WINDOW))
        except (TypeError, ValueError):
            raise RunnerControlError("runtime.pi_context_window must be an integer") from None
        if not 1024 <= self.pi_context_window <= 4_000_000:
            raise RunnerControlError(
                "runtime.pi_context_window must be between 1024 and 4000000")
        if self.runner_egress == _RUNNER_EGRESS_MODEL_ONLY:
            if self.runner_mode != "container":
                raise RunnerControlError(
                    "runtime.runner_egress=model_only requires container runner mode")
            if self.runner_runtime != "pi":
                raise RunnerControlError(
                    "runtime.runner_egress=model_only requires runner_runtime=pi")
            if self.model_forwarder_socket is None:
                raise RunnerControlError(
                    "runtime.runner_egress=model_only requires the Supervisor to "
                    "provide a model forwarder socket")
            if (not isinstance(self.model_forwarder_token, str)
                    or len(self.model_forwarder_token) < 16):
                raise RunnerControlError(
                    "runtime.runner_egress=model_only requires a model forwarder token")
        # Runner processes import from a frozen copy of ``src`` (never the live
        # tree) so a restart or resume cannot load operator-edited harness code.
        self.live_source_src = self.container_source_root / "src"
        self.source_snapshot_root = self.run_dir / _SOURCE_SNAPSHOT_DIRECTORY
        self.source_snapshot_src = self.source_snapshot_root / "src"
        self.source_snapshot_manifest = self.run_dir / _SOURCE_SNAPSHOT_MANIFEST
        self.source_sha256 = None
        self.source_hashes = None
        self.source_snapshot_files = 0
        self.source_snapshot_created = False
        self.source_snapshot_live_sha256 = None
        self.source_snapshot_live_drift = False
        self.pi_runtime_sha256: str | None = None
        self.states: dict[str, dict[str, Any]] = {}
        if self.runner_mode == "container":
            self.rpc_dir = self._container_shared_dir() / (
                ".mc-runner-rpc-" + hashlib.sha256(
                    (self.run_id + ":rpc").encode("utf-8")).hexdigest()[:12])
        else:
            # AF_UNIX paths are limited to ~108 bytes. Run directories under deep
            # experiment layouts exceed that, so the process control socket lives
            # in a private short-lived directory instead of under run_dir.
            self.rpc_dir = Path(tempfile.mkdtemp(prefix="mc-runner-rpc-"))
        self.rpc_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path = self.rpc_dir / "control.sock"
        if len(str(self.socket_path).encode()) >= _MAX_RPC_SOCKET_BYTES:
            raise RunnerControlError(
                "runner RPC socket path is too long; set runtime.runner_container_shared_dir")
        self.server = RpcServer(self.socket_path, token_map, self._dispatch)
        self.restart_limit = int(runtime.get("runner_restart_limit", 5))
        self.startup_timeout = float(runtime.get("runner_startup_timeout_s", 90))
        self.heartbeat_timeout = float(runtime.get("runner_heartbeat_timeout_s", 180))
        self.recovery_base_s = float(self.config_supervision.get("recovery_base_s", 1))
        self.recovery_max_s = float(self.config_supervision.get("recovery_max_s", 30))
        self.poll_interval = min(5.0, max(0.2, self.heartbeat_timeout / 6))
        for agent in self.agents:
            self.states[agent] = {
                "agent_id": agent, "role": self.roles[agent], "process": None,
                "ready_event": asyncio.Event(), "ready_info": None,
                "finished_event": asyncio.Event(), "result": None,
                "monitor": None, "heartbeat_task": None, "log": None,
                "log_offset": 0, "attempts": 0, "started_at": None,
                "last_heartbeat": 0.0, "pid": None, "container": None,
            }
        self.closing = False
        self._started = False
        self._stopped = False
        self._lock = asyncio.Lock()

    def _container_shared_dir(self):
        """Pick a short host-shared directory for the Unix socket in container mode.

        The socket is bind-mounted into each runner container, so its host path
        must be visible to the Docker daemon at the same absolute path. The
        deployment wrapper mounts the run directory's parent at the same path;
        operators can override this with runtime.runner_container_shared_dir.
        """
        runtime = self.config.get("runtime", {}) if isinstance(self.config, dict) else {}
        configured = runtime.get("runner_container_shared_dir")
        candidates = []
        if configured:
            candidates = [Path(configured)]
        else:
            ancestors = [self.run_dir, self.run_dir.parent]
            if len(self.run_dir.parents) > 1:
                ancestors.append(self.run_dir.parents[1])
            candidates = sorted({path for path in ancestors if path}, key=lambda p: len(str(p)))
        suffix = len("/.mc-runner-rpc-xxxxxxxxxxxx/control.sock") + 2
        for candidate in candidates:
            if len(str(candidate)) + suffix >= _MAX_RPC_SOCKET_BYTES:
                continue
            try:
                candidate.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            if os.access(candidate, os.W_OK):
                return candidate.resolve()
        raise RunnerControlError(
            "container runners need a short host-shared directory for the RPC socket; "
            "set runtime.runner_container_shared_dir to a path mounted at the same "
            "absolute path inside the supervisor and visible to the Docker daemon")

    async def _docker(self, *arguments, timeout=60):
        if not self.docker_bin:
            raise RunnerControlError("Docker CLI is required for container runners")
        environment = {"PATH": self.runner_env.get("PATH", os.environ.get("PATH", ""))}
        if os.environ.get("DOCKER_HOST"):
            environment["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
        process = await asyncio.create_subprocess_exec(
            self.docker_bin, *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL, env=environment)
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate()
        except BaseException:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        return (process.returncode,
                stdout.decode("utf-8", "replace")[-4000:],
                stderr.decode("utf-8", "replace")[-4000:])

    def _agent_egress(self, agent):
        """Per-agent egress mode (restricted only for the Pi runtime)."""
        if self.runner_egress == _RUNNER_EGRESS_MODEL_ONLY and self._agent_runtime(agent) == "pi":
            return _RUNNER_EGRESS_MODEL_ONLY
        return _RUNNER_EGRESS_BRIDGE

    def _container_network_for(self, agent):
        return "none" if self._agent_egress(agent) == _RUNNER_EGRESS_MODEL_ONLY else self.container_network

    def _validate_model_forwarder(self):
        """Fail closed before spawn when the restricted-egress forwarder is absent."""
        if self.runner_egress != _RUNNER_EGRESS_MODEL_ONLY:
            return
        socket_path = self.model_forwarder_socket
        if socket_path is None:
            raise RunnerControlError(
                "restricted runner egress requires the host model forwarder socket: "
                "unset")
        try:
            socket_stat = socket_path.lstat()
        except OSError as exc:
            raise RunnerControlError(
                "restricted runner egress requires the host model forwarder socket: " +
                str(socket_path) + " (" + (exc.strerror or type(exc).__name__) + ")") from None
        if not stat.S_ISSOCK(socket_stat.st_mode):
            raise RunnerControlError(
                "restricted runner egress requires the host model forwarder socket: " +
                str(socket_path) + " is not a Unix socket")
        if not (isinstance(self.model_forwarder_token, str)
                and len(self.model_forwarder_token) >= 16):
            raise RunnerControlError(
                "restricted runner egress requires a model forwarder token")

    async def _validate_container_runtime(self):
        if not self.docker_bin:
            raise RunnerControlError("Docker CLI is required for container runner_mode")
        code, output, error = await self._docker("version", "--format", "{{.Server.Version}}")
        if code:
            raise RunnerControlError("Docker daemon is unavailable for container runners: " +
                                     (error or output or "unknown error"))
        code, output, error = await self._docker(
            "image", "inspect", "--format", "{{.Id}}", self.container_image)
        if code:
            raise RunnerControlError(
                f"runner container image is unavailable: {self.container_image}: "
                + (error or output or "docker image inspect failed"))
        if self.runner_egress != _RUNNER_EGRESS_MODEL_ONLY and self.container_network != "none":
            code, output, error = await self._docker(
                "network", "inspect", "--format", "{{.Name}}", self.container_network)
            if code:
                raise RunnerControlError(
                    f"runner container network is unavailable: {self.container_network}: "
                    + (error or output or "docker network inspect failed"))
        frozen_source = self._frozen_source_path()
        if not frozen_source.is_dir():
            raise RunnerControlError(
                "frozen container runner source tree is missing: " + str(frozen_source))
        probe = ("import sys, httpx, yaml; "
                 "print(sys.version.split()[0], httpx.__version__)")
        code, output, error = await self._docker(
            "run", "--rm", "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--mount", f"type=bind,src={frozen_source},"
                       f"dst={self.container_source_root / 'src'},readonly",
            *(["--mount", f"type=bind,src={self.container_site_packages},"
                         f"dst={self.container_site_packages},readonly"]
              if self.container_site_packages is not None else []),
            "-e", "PYTHONPATH=" + os.pathsep.join(
                [str(self.container_source_root / "src")] +
                ([str(self.container_site_packages)] if self.container_site_packages else [])),
            "--entrypoint", self.container_python, self.container_image, "-c", probe,
            timeout=120)
        if code:
            raise RunnerControlError(
                "container runner image/python cannot import the harness dependencies: " +
                (error or output or "probe failed"))


    # ---- RPC routing --------------------------------------------------
    async def _dispatch(self, agent_id, role, method, params):
        if method in {"runner.ready", "runner.heartbeat", "runner.finished"}:
            return await self.handle_runner(agent_id, role, method, params)
        return await self.service_handler(agent_id, role, method, params)

    async def handle_runner(self, agent_id, role, method, params):
        state = self.states.get(agent_id)
        if state is None or role != state["role"]:
            raise PermissionError("Runner identity does not match a configured runner")
        if not isinstance(params, dict):
            raise ValueError("Runner RPC params must be an object")
        if method == "runner.ready":
            hashes = (params.get("prompt_sha256"), params.get("tools_sha256"))
            if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes):
                raise ValueError("Runner ready hashes must be SHA256")
            if state["ready_info"] is None:
                state["ready_info"] = {
                    "agent_id": agent_id, "pid": params.get("pid"),
                    "prompt_sha256": hashes[0], "tools_sha256": hashes[1],
                }
                state["last_heartbeat"] = time.monotonic()
                self._record("runner_ready", {"agent_id": agent_id, "pid": params.get("pid")})
            state["ready_event"].set()
            return {"accepted": True}
        if method == "runner.heartbeat":
            state["last_heartbeat"] = time.monotonic()
            if type(params.get("turns")) is int:
                state["turns"] = params["turns"]
            return {"accepted": True}
        if method == "runner.finished":
            result = params.get("result")
            if not isinstance(result, dict):
                raise ValueError("Runner result must be an object")
            if state["result"] is None:
                state["result"] = result
                self._record("runner_reported", {
                    "agent_id": agent_id, "status": result.get("status"),
                    "turns": result.get("turns")})
            state["finished_event"].set()
            return {"accepted": True}
        raise ValueError("unknown runner method")

    def _record(self, kind, payload):
        if self.recorder is None:
            return
        _append(self.recorder, kind, payload)

    # ---- frozen runner source -----------------------------------------
    def freeze_source_snapshot(self):
        """Freeze the live harness source once, or verify the frozen copy on reuse.

        The first call copies ``container_source_root/src`` into
        ``run_dir/source-snapshot/src`` (excluding ``__pycache__``/``*.pyc``),
        hashes the tree deterministically into ``run_dir/source-snapshot.json``
        and records a ``source_snapshot_frozen`` ledger event. Later starts
        (supervisor resume or operator reuse) verify the persisted snapshot
        against that manifest and raise :class:`RunnerControlError` when it no
        longer matches, so a runner restart can never silently load changed
        harness code.
        """
        if self.source_sha256 is not None:
            return self._verify_source_snapshot()
        if self.source_snapshot_root.exists() or self.source_snapshot_manifest.exists():
            info = self._verify_source_snapshot()
        else:
            info = self._create_source_snapshot()
        self._record("source_snapshot_frozen", {
            "source_sha256": info["source_sha256"],
            "source_files": info["source_files"],
            "source_root": str(self.live_source_src),
            "snapshot": str(self.source_snapshot_src),
            "created": info["created"],
            "live_source_sha256": info.get("live_source_sha256"),
            "live_source_drift": bool(info.get("live_source_drift")),
        })
        if info.get("live_source_drift"):
            self._record("source_snapshot_drift", {
                "source_sha256": info["source_sha256"],
                "live_source_sha256": info.get("live_source_sha256"),
                "snapshot": str(self.source_snapshot_src),
            })
        return info

    def _pi_runtime_hashes(self) -> dict[str, Any]:
        """Hash the complete pinned Pi runtime tree.

        The Python source snapshot cannot cover the Node/Pi tree. Hashing every
        regular file (not just the CLI shim/package lock/extensions) makes a
        restart fail closed if any bundle chunk, provider module or extension
        was changed after the manifest was frozen.
        """
        if self.runner_runtime != "pi":
            return {}
        if not self.pi_cli.is_file():
            raise RunnerControlError(
                "runtime.pi_cli does not exist: " + str(self.pi_cli) +
                " (install the pinned pi_runtime tree or set runtime.pi_cli)")
        tree = self._pi_runtime_tree()
        files: dict[str, str] = {}
        for path in sorted(tree.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(tree).as_posix()
            if any(part in {"__pycache__", ".cache"} for part in path.parts):
                continue
            try:
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise RunnerControlError(
                    "could not hash the pinned Pi runtime file " + relative + ": " +
                    (exc.strerror or type(exc).__name__)) from None
        if str(self.pi_cli.resolve()) not in {
                str((tree / relative).resolve()) for relative in files}:
            raise RunnerControlError(
                "the pinned Pi CLI is not part of the hashed pi_runtime tree: " +
                str(self.pi_cli))
        digest = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {"root": str(tree), "file_count": len(files),
                "files": files, "sha256": digest}

    def _create_source_snapshot(self):
        live = self.live_source_src
        if not live.is_dir():
            raise RunnerControlError(
                "harness source tree is missing; cannot freeze the runner source: " + str(live))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        staging = self.run_dir / (".source-snapshot-" + uuid.uuid4().hex)
        try:
            staging.mkdir()
            live_hashes = _source_tree_hashes(live)
            shutil.copytree(live, staging / "src", symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            # Runner containers run with cap_drop=ALL (no CAP_DAC_OVERRIDE), so
            # root inside the container can only read world-readable files.
            # Normalize the frozen tree: an agent-created 0600 source file would
            # otherwise make every container runner fail at import.
            tree = staging / "src"
            for base, dirs, files in os.walk(tree):
                os.chmod(base, 0o755)
                for name in dirs:
                    path = Path(base) / name
                    if not path.is_symlink():
                        os.chmod(path, 0o755)
                for name in files:
                    path = Path(base) / name
                    if not path.is_symlink():
                        os.chmod(path, 0o644)
            frozen_hashes = _source_tree_hashes(tree)
            if frozen_hashes != live_hashes:
                raise RunnerControlError(
                    "live harness source changed while the runner source snapshot was frozen")
            if _source_tree_hashes(live) != live_hashes:
                raise RunnerControlError(
                    "live harness source changed while the runner source snapshot was frozen")
            source_sha256 = _source_tree_sha256(frozen_hashes)
            manifest = {
                "schema_version": _SOURCE_SNAPSHOT_SCHEMA_VERSION,
                "run_id": self.run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_root": str(live),
                "snapshot_root": str(self.source_snapshot_root),
                "snapshot_src": str(self.source_snapshot_src),
                "source_sha256": source_sha256,
                "source_files": len(frozen_hashes),
                "source_hashes": frozen_hashes,
                "hash_policy": _SOURCE_TREE_HASH_POLICY,
                "ignored": ["__pycache__/", "*.pyc"],
                "pi_runtime": self._pi_runtime_hashes(),
            }
            try:
                os.replace(staging, self.source_snapshot_root)
            except OSError as exc:
                raise RunnerControlError(
                    "could not publish the runner source snapshot: " + str(exc)) from exc
            try:
                atomic_json(self.source_snapshot_manifest, manifest)
            except (OSError, TypeError, ValueError) as exc:
                raise RunnerControlError(
                    "could not write the runner source snapshot manifest: " + str(exc)) from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return self._apply_source_snapshot(manifest, frozen_hashes, created=True)

    def _verify_source_snapshot(self):
        if not self.source_snapshot_src.is_dir():
            raise RunnerControlError(
                "runner source snapshot is incomplete (missing " + str(self.source_snapshot_src) +
                "); delete " + str(self.source_snapshot_root) + " and " +
                str(self.source_snapshot_manifest) + " to freeze the current source again")
        try:
            manifest = json.loads(self.source_snapshot_manifest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RunnerControlError(
                "runner source snapshot manifest is missing: " + str(self.source_snapshot_manifest) +
                "; a partially frozen run must not start runners") from None
        except (OSError, UnicodeError, ValueError) as exc:
            raise RunnerControlError(
                "runner source snapshot manifest is unreadable: " +
                str(self.source_snapshot_manifest) + ": " + str(exc)) from exc
        if (not isinstance(manifest, dict) or
                manifest.get("schema_version") != _SOURCE_SNAPSHOT_SCHEMA_VERSION):
            raise RunnerControlError(
                "runner source snapshot manifest has an unsupported schema: " +
                str(self.source_snapshot_manifest))
        if manifest.get("run_id") != self.run_id:
            raise RunnerControlError(
                "runner source snapshot belongs to run " + repr(manifest.get("run_id")) +
                ", not " + repr(self.run_id))
        expected_hashes = manifest.get("source_hashes")
        expected_sha256 = manifest.get("source_sha256")
        if (not isinstance(expected_hashes, dict) or
                any(not isinstance(key, str) or not isinstance(value, str)
                    for key, value in expected_hashes.items()) or
                not isinstance(expected_sha256, str) or
                not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise RunnerControlError(
                "runner source snapshot manifest is malformed: " +
                str(self.source_snapshot_manifest))
        frozen_hashes = _source_tree_hashes(self.source_snapshot_src)
        frozen_sha256 = _source_tree_sha256(frozen_hashes)
        if frozen_hashes != expected_hashes or frozen_sha256 != expected_sha256:
            raise RunnerControlError(
                "frozen runner source snapshot no longer matches its manifest: expected "
                "sha256=" + expected_sha256 + " files=" + str(len(expected_hashes)) +
                ", found sha256=" + frozen_sha256 + " files=" + str(len(frozen_hashes)) +
                "; refusing to load changed runner code")
        live_sha256 = None
        if self.live_source_src.is_dir():
            try:
                live_sha256 = _source_tree_sha256(_source_tree_hashes(self.live_source_src))
            except (OSError, RunnerControlError):
                live_sha256 = None
        drift = live_sha256 is not None and live_sha256 != frozen_sha256
        expected_pi = manifest.get("pi_runtime")
        if self.runner_runtime == "pi":
            actual_pi = self._pi_runtime_hashes()
            if actual_pi != expected_pi:
                raise RunnerControlError(
                    "Pinned Pi runtime changed since the run started; refusing to load "
                    "a changed Pi CLI/extension. Restore the pinned pi_runtime tree or "
                    "start a new run with a fresh source snapshot.")
        return self._apply_source_snapshot(manifest, frozen_hashes, created=False,
                                           live_source_sha256=live_sha256,
                                           live_source_drift=drift)

    def _apply_source_snapshot(self, manifest, hashes, *, created,
                               live_source_sha256=None, live_source_drift=False):
        self.source_sha256 = manifest["source_sha256"]
        self.source_hashes = dict(hashes)
        self.source_snapshot_files = len(hashes)
        self.source_snapshot_created = bool(created)
        self.source_snapshot_live_sha256 = live_source_sha256
        self.source_snapshot_live_drift = bool(live_source_drift)
        pi_runtime = manifest.get("pi_runtime") if isinstance(manifest, dict) else None
        self.pi_runtime_sha256 = (pi_runtime or {}).get("sha256") if isinstance(pi_runtime, dict) else None
        return {"created": bool(created), "source_sha256": self.source_sha256,
                "pi_runtime_sha256": self.pi_runtime_sha256,
                "source_files": self.source_snapshot_files,
                "live_source_sha256": live_source_sha256,
                "live_source_drift": bool(live_source_drift)}

    def _frozen_source_path(self):
        """Return the frozen source path, freezing/verifying it on first use."""
        if self.source_sha256 is None:
            self.freeze_source_snapshot()
        return self.source_snapshot_src

    def _ensure_frozen_source(self):
        """Verify the frozen snapshot before spawning, so tampering fails closed."""
        if self.source_sha256 is None:
            return self.freeze_source_snapshot()
        return self._verify_source_snapshot()

    def _runner_pythonpath(self, existing):
        """Point process runners at the frozen source, keeping support paths.

        The live source entry is dropped from the inherited ``PYTHONPATH`` while
        entries such as the installed ``site-packages`` or ``bot_runtime`` stay
        unchanged (the frozen snapshot is only the harness package source).
        """
        frozen = str(self._frozen_source_path())
        live = self.live_source_src
        entries, seen = [], set()
        for entry in [frozen] + (existing or "").split(os.pathsep):
            if not entry:
                continue
            try:
                key = str(Path(entry).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                key = entry
            if key in seen or key == str(live):
                continue
            seen.add(key)
            entries.append(entry)
        return os.pathsep.join(entries)

    def _process_environment(self, agent, spec_path):
        """Environment for a process-mode runner; source resolution is frozen."""
        state = self.states[agent]
        environment = dict(self.runner_env)
        environment["PYTHONPATH"] = self._runner_pythonpath(environment.get("PYTHONPATH"))
        environment.update({
            "HARNESS_RUNNER_SOCKET": str(self.socket_path),
            "HARNESS_RUNNER_TOKEN": self.token_by_agent[agent],
            "HARNESS_RUNNER_AGENT": agent,
            "HARNESS_RUNNER_ROLE": state["role"],
            "HARNESS_RUNNER_SPEC": str(spec_path),
            "HARNESS_RUNNER_RUN_ID": self.run_id,
            "PYTHONUNBUFFERED": "1",
            # Keep bytecode caches out of the frozen snapshot subtree.
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if self._agent_runtime(agent) == "pi":
            spec = self._pi_spec(agent)
            environment["HARNESS_PI_CLI"] = spec["pi_cli"]
            environment["HARNESS_PI_CONTEXT_WINDOW"] = str(spec["pi_context_window"])
            if self.roles.get(agent) != "observer":
                # Only player runtimes have the game CLI bridge; the Observer has
                # no game client and must never receive MC_* credentials.
                environment.update(spec.get("cli_environment") or {})
        return environment

    # ---- Pi runtime selection -----------------------------------------
    def _agent_runtime(self, agent):
        """Effective runner implementation for one agent.

        ``runtime.runner_runtime`` switches both player agents and the Observer to
        the Pi runtimes (``pi_runner`` / ``pi_observer_runner``); the python
        runtime remains the default for every agent.
        """
        if self.runner_runtime != "pi":
            return "python"
        return "pi"

    def _agent_module(self, agent):
        if self.runner_module is not None:
            return self.runner_module
        if self._agent_runtime(agent) != "pi":
            return _PYTHON_RUNNER_MODULE
        return (_PI_OBSERVER_RUNNER_MODULE if self.roles.get(agent) == "observer"
                else _PI_RUNNER_MODULE)

    def _pi_runtime_tree(self):
        cli = self.pi_cli
        for parent in cli.parents:
            if parent.name == "pi_runtime":
                return parent
        candidate = self.container_source_root / "pi_runtime"
        if candidate.is_dir():
            return candidate
        raise RunnerControlError(
            "the pinned pi_runtime tree is missing next to runtime.pi_cli: " + str(cli))

    def _pi_host_workspace(self, agent):
        return self.run_dir / "workspaces" / agent

    def _pi_host_reference(self):
        return self.run_dir / "reference-docs"

    def _pi_host_team_memory(self):
        return self.run_dir / "memory" / "team"

    def _pi_host_broker_dir(self):
        return self.run_dir / "broker"

    def _pi_host_broker_socket(self):
        return self._pi_host_broker_dir() / "socket"

    def _ensure_pi_roots(self, agent):
        """Create the writable Pi runtime roots before they are bind-mounted."""
        paths = [self.run_dir / "agents" / agent,
                 self._pi_host_workspace(agent),
                 self._pi_host_reference()]
        if self.roles.get(agent) != "observer":
            # The Observer has no game client: it never gets the shared team
            # memory or submission-broker mounts/credentials.
            paths += [self._pi_host_team_memory(), self._pi_host_broker_dir()]
        for path in paths:
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise RunnerControlError(
                    "could not create Pi runner runtime directory " + str(path) + ": " +
                    str(exc)) from exc

    def _pi_model_config(self, agent):
        model = self.specs[agent].get("pi_model")
        if not isinstance(model, dict) or not model:
            configured = self.config.get("model", {}) if isinstance(self.config, dict) else {}
            model = dict(configured) if isinstance(configured, dict) else {}
        try:
            copied = json.loads(json.dumps(model))
        except (TypeError, ValueError) as exc:
            raise RunnerControlError(
                "Pi runner model configuration is not JSON-serializable: " + str(exc)) from exc
        if not isinstance(copied, dict) or not copied:
            raise RunnerControlError("Pi runner requires a non-empty model configuration")
        if "api_key" in copied:
            raise RunnerControlError(
                "Pi runner model configuration must not contain an inline api_key")
        return copied

    def _pi_cli_environment(self, agent):
        """MC_* environment for the Pi bash tool and the ``mc`` CLI wrapper."""
        container = self.runner_mode == "container"
        supplied = self.specs[agent].get("cli_environment")
        environment = {}
        if isinstance(supplied, dict):
            for key in _PI_CLI_ENVIRONMENT_KEYS:
                value = supplied.get(key)
                if isinstance(value, str) and value:
                    environment[key] = value
        if not environment:
            return environment
        environment["MC_RUNTIME_SOCKET"] = (
            "/run/mc-broker/socket" if container else str(self._pi_host_broker_socket()))
        return environment

    def _pi_retention_limits(self):
        storage = self.config.get("storage", {}) if isinstance(self.config, dict) else {}
        if not isinstance(storage, dict):
            storage = {}
        return {
            "pi_session_max_bytes": int(
                storage.get("pi_session_max_bytes", 1_000_000_000)),
            "pi_artifact_max_bytes": int(
                storage.get("pi_artifact_max_bytes", 4_000_000_000)),
        }

    def _pi_observer_config(self, agent):
        """Observer tuning passed to ``pi_observer_runner`` (interval/reviews)."""
        supplied = self.specs[agent].get("observer")
        if not isinstance(supplied, dict):
            configured = self.config.get("observer", {}) if isinstance(self.config, dict) else {}
            supplied = configured if isinstance(configured, dict) else {}
        return dict(supplied)

    def _pi_model_with_forwarder(self, agent):
        """Pi model config plus the restricted-egress forwarder socket, if any."""
        model = self._pi_model_config(agent)
        if self._agent_egress(agent) == _RUNNER_EGRESS_MODEL_ONLY:
            model = dict(model)
            model["model_forwarder_socket"] = MODEL_FORWARDER_CONTAINER_SOCKET
        return model

    def _pi_spec(self, agent):
        """Mode-specific spec keys consumed by the matching Pi runner module."""
        if self.roles.get(agent) == "observer":
            return self._pi_observer_spec(agent)
        container = self.runner_mode == "container"
        cli_environment = self._pi_cli_environment(agent)
        spec = {
            "runner_runtime": "pi",
            "pi_cli": str(self.pi_cli),
            "pi_context_window": self.pi_context_window,
            "workspace_root": "/workspace" if container else str(self._pi_host_workspace(agent)),
            "reference_root": "/reference" if container else str(self._pi_host_reference()),
            "team_memory_root": "/memory/team" if container else str(self._pi_host_team_memory()),
            "broker_socket": cli_environment.get("MC_RUNTIME_SOCKET"),
            "cli_environment": cli_environment,
            "pi_model": self._pi_model_with_forwarder(agent),
        }
        spec.update(self._pi_retention_limits())
        return spec

    def _pi_observer_spec(self, agent):
        """Spec for ``minecraft_harness.pi_observer_runner`` (role=observer)."""
        container = self.runner_mode == "container"
        spec = {
            "schema_version": 1,
            "runner_runtime": "pi",
            "run_id": self.run_id,
            "agent_id": _OBSERVER_AGENT_ID,
            "role": "observer",
            "state_root": "/state" if container else str(self.run_dir / "agents" / agent),
            "pi_cli": str(self.pi_cli),
            "pi_context_window": self.pi_context_window,
            "workspace_root": "/workspace" if container else str(self._pi_host_workspace(agent)),
            "reference_root": "/reference" if container else str(self._pi_host_reference()),
            "pi_model": self._pi_model_with_forwarder(agent),
            "observer": self._pi_observer_config(agent),
        }
        spec.update(self._pi_retention_limits())
        observer_config = spec["observer"]
        if isinstance(observer_config.get("interval_s"), (int, float)) and not isinstance(
                observer_config.get("interval_s"), bool):
            spec["interval_s"] = observer_config["interval_s"]
        for key in ("max_reviews", "max_turns"):
            if type(observer_config.get(key)) is int:
                spec[key] = observer_config[key]
        return spec

    def _runner_spec(self, agent):
        """Full runner spec: existing keys plus the effective runtime contract."""
        spec = dict(self.specs[agent])
        runtime = self._agent_runtime(agent)
        spec["runner_runtime"] = runtime
        if self.runner_mode == "container":
            spec["state_root"] = "/state"
        if runtime == "pi":
            spec.update(self._pi_spec(agent))
            if self.roles.get(agent) == "observer":
                # The Observer runs read-only and has no game client, shell,
                # shared-memory mount or submission-broker credentials.
                spec["agent_id"] = _OBSERVER_AGENT_ID
                spec["role"] = "observer"
                spec["shell_enabled"] = False
                spec["cli_environment"] = {}
                spec["broker_socket"] = None
                spec.pop("team_memory_root", None)
        return spec

    def _process_cwd(self, agent):
        """Process-mode cwd: the Pi workspace, or run_dir for the Python runner."""
        if self._agent_runtime(agent) == "pi":
            return self._pi_host_workspace(agent)
        return self.run_dir

    def _validate_pi_runtime(self):
        """Fail closed before spawning Pi runners when the pinned runtime is absent."""
        pi_agents = [agent for agent in self.agents
                     if self._agent_runtime(agent) == "pi"]
        if not pi_agents:
            return
        if not self.pi_cli.is_file():
            raise RunnerControlError(
                "runtime.pi_cli does not exist: " + str(self.pi_cli) +
                " (install the pinned pi_runtime tree or set runtime.pi_cli)")
        tree = self._pi_runtime_tree()
        if not tree.is_dir():
            raise RunnerControlError(
                "the pinned pi_runtime tree is missing: " + str(tree))
        if self.runner_mode == "container":
            try:
                self.pi_cli.relative_to(tree)
            except ValueError:
                raise RunnerControlError(
                    "runtime.pi_cli must live under the mounted pi_runtime tree " +
                    str(tree) + " for container runners") from None
        # The Pi tool extensions are hash-pinned in the run manifest; fail closed
        # before launch when a runtime an agent needs is missing.
        for role in ("player", "observer"):
            if not any(self.roles.get(agent) == role for agent in self.agents):
                continue
            name = _PI_EXTENSION_FILES[role]
            extension = tree / "extensions" / name
            if not extension.is_file():
                raise RunnerControlError(
                    "the pinned Pi " + role + " extension is missing: " + str(extension))
        if sys.platform.startswith("linux"):
            guard = tree / "prctl_guard.so"
            if not guard.is_file():
                raise RunnerControlError(
                    "the pinned Pi prctl guard is missing: " + str(guard) +
                    " (build pi_runtime/prctl_guard.so)")

    # ---- lifecycle ----------------------------------------------------
    async def start(self):
        if self._started:
            raise RunnerControlError("runner manager already started")
        # Freeze before validation/spawn: from here on every runner (including
        # restarts) imports the same frozen source recorded for this run.
        await asyncio.to_thread(self.freeze_source_snapshot)
        await asyncio.to_thread(self._validate_pi_runtime)
        await asyncio.to_thread(self._validate_model_forwarder)
        if self.runner_mode == "container":
            await self._validate_container_runtime()
        self.runners_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.runners_dir, 0o700)
        await self.server.start()
        self._started = True
        starters = [asyncio.create_task(self._start_one(agent),
                                        name=f"runner-start-{agent}") for agent in self.agents]
        try:
            await self._fence_stale()
            await asyncio.gather(*starters)
            self._write_process_state()
            return {agent: self.states[agent]["ready_info"] for agent in self.agents}
        except BaseException:
            for task in starters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*starters, return_exceptions=True)
            await self.stop(grace=5)
            raise

    async def _fence_stale(self):
        if self.recorder is None:
            return
        saved = await asyncio.to_thread(self.recorder.get_state, "runner_processes", {})
        if not isinstance(saved, dict):
            return
        for agent, info in saved.items():
            if agent not in self.states or not isinstance(info, dict):
                continue
            container = info.get("container")
            if isinstance(container, str) and container:
                if await self._remove_owned_container(container):
                    self._record("stale_runner_container_fenced",
                                 {"agent_id": agent, "container": container})
                continue
            pid = info.get("pid")
            if type(pid) is not int or pid <= 1:
                continue
            if not self._pid_is_runner(pid, agent):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except OSError:
                continue
            self._record("stale_runner_fenced", {"agent_id": agent, "pid": pid})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self._pid_alive(pid):
                await asyncio.sleep(0.05)

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _pid_is_runner(pid, agent):
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
            if not any(module.encode() in command for module in _PI_RUNNER_MODULES):
                return False
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            return f"HARNESS_RUNNER_AGENT={agent}".encode() in environment
        except (OSError, ValueError):
            return False

    async def _start_one(self, agent):
        state = self.states[agent]
        while True:
            if self.closing:
                raise RunnerControlError("runner manager is closing")
            process = await self._spawn(agent)
            deadline = time.monotonic() + self.startup_timeout
            exit_task = asyncio.create_task(process.wait(), name=f"runner-exit-start-{agent}")
            ready_task = asyncio.create_task(state["ready_event"].wait(), name=f"runner-ready-{agent}")
            try:
                done, _ = await asyncio.wait({ready_task, exit_task}, timeout=max(0.1, deadline - time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                for task in (exit_task, ready_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(exit_task, ready_task, return_exceptions=True)
                await self._kill(agent, process)
                raise
            if ready_task in done and ready_task.result():
                exit_task.cancel()
                await asyncio.gather(exit_task, return_exceptions=True)
                state["last_heartbeat"] = time.monotonic()
                state["attempts"] = 0
                state["monitor"] = asyncio.create_task(self._monitor(agent), name=f"runner-monitor-{agent}")
                state["heartbeat_task"] = asyncio.create_task(
                    self._heartbeat_watch(agent), name=f"runner-heartbeat-{agent}")
                return
            # Exited or timed out before ready.
            for task in (exit_task, ready_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(exit_task, ready_task, return_exceptions=True)
            await self._kill(agent, process)
            state["attempts"] += 1
            reason = "timeout" if not exit_task.done() else f"exit={process.returncode}"
            self._record("runner_start_failed", {"agent_id": agent, "attempt": state["attempts"],
                                                 "reason": reason})
            if state["attempts"] > self.restart_limit:
                raise RunnerControlError(f"runner {agent} failed to start after {state['attempts']} attempts")
            await self._backoff(state["attempts"])

    def _container_name(self, agent):
        return f"mc-runner-{self._run_slug}-{agent}"

    def _container_environment(self, agent):
        runtime = self.config.get("runtime", {}) if isinstance(self.config, dict) else {}
        restricted = self._agent_egress(agent) == _RUNNER_EGRESS_MODEL_ONLY
        allowed = {"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                   "http_proxy", "https_proxy", "no_proxy",
                   "HARNESS_RUNNER_RPC_TIMEOUT_S"}
        if not restricted:
            # Restricted-egress runners never receive provider/web credentials:
            # the host-side forwarder injects the provider key and the web tools
            # run in the Supervisor, so the container environment carries neither.
            for group in (self.config.get("model", {}), self.config.get("web", {})):
                for key in ("api_key_env", "search_key_env", "fetch_key_env"):
                    name = group.get(key) if isinstance(group, dict) else None
                    if isinstance(name, str):
                        allowed.add(name)
        environment = {key: value for key, value in self.runner_env.items()
                       if key in allowed}
        environment["PATH"] = environment.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        # The frozen source snapshot is bind-mounted over this same absolute path,
        # so the container destination and PYTHONPATH stay unchanged while live
        # tree edits are ignored by runners.
        pythonpath = [str(self.container_source_root / "src")]
        if self.container_site_packages is not None:
            pythonpath.append(str(self.container_site_packages))
        environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
        environment["HOME"] = "/state/home"
        environment["TMPDIR"] = "/state/tmp"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.update({
            "HARNESS_RUNNER_SOCKET": "/run/mc-runner-rpc/control.sock",
            "HARNESS_RUNNER_TOKEN": self.token_by_agent[agent],
            "HARNESS_RUNNER_AGENT": agent,
            "HARNESS_RUNNER_ROLE": self.states[agent]["role"],
            "HARNESS_RUNNER_SPEC": "/run/mc-runner/spec.json",
            "HARNESS_RUNNER_RUN_ID": self.run_id,
            # Container runners get the same per-call RPC bound as process
            # runners instead of silently using the transport default.
            "HARNESS_RUNNER_RPC_TIMEOUT_S": str(
                runtime.get("runner_rpc_timeout_s", 120)),
        })
        if restricted:
            # Capability for the host model forwarder only. pi_runner does not
            # forward this key into the Pi child/bash environment, and the
            # hardened PID 1 env is not readable through /proc by the same uid.
            environment[ENV_MODEL_FORWARDER_TOKEN] = self.model_forwarder_token
        if self._agent_runtime(agent) == "pi":
            spec = self._pi_spec(agent)
            environment["HARNESS_PI_CLI"] = spec["pi_cli"]
            environment["HARNESS_PI_CONTEXT_WINDOW"] = str(spec["pi_context_window"])
            if self.roles.get(agent) != "observer":
                # Only the players' Pi runtime has the game CLI: put the wrapper
                # dir on PATH and forward the bound MC_* broker credentials. The
                # Observer has no shell/game client and gets none of these.
                environment["PATH"] = "/state/bin" + os.pathsep + environment["PATH"]
                environment.update(spec.get("cli_environment") or {})
        return environment

    def _container_args(self, agent, spec_path, state_dir):
        state = self.states[agent]
        name = self._container_name(agent)
        args = [
            # SECURITY: no `--init`. The hardened Python runner (PR_SET_DUMPABLE=0
            # plus signal handlers) is PID 1 so container env secrets are not
            # inherited by an unhardened tini PID 1 that model-directed
            # `/proc/1/environ` reads could disclose.
            "run", "--rm", "--name", name,
            "--label", "minecraft-harness.runner=1",
            "--label", f"minecraft-harness.run={self.run_id}",
            "--label", f"minecraft-harness.agent={agent}",
            "--network", self._container_network_for(agent),
            "--pids-limit", "256",
            "--memory", f"{self.container_memory_mb}m",
            "--cpus", str(self.container_cpus),
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            *(["--user", self.container_user] if self.container_user else []),
            "--workdir", "/state",
            "--mount", f"type=bind,src={self._frozen_source_path()},"
                       f"dst={self.container_source_root / 'src'},readonly",
            "--mount", f"type=bind,src={spec_path},dst=/run/mc-runner/spec.json,readonly",
            "--mount", f"type=bind,src={state_dir},dst=/state",
        ]
        if self.runner_egress == _RUNNER_EGRESS_MODEL_ONLY:
            forwarder_directory = self.model_forwarder_socket.parent
            args += [
                "--mount", f"type=bind,src={forwarder_directory},"
                           f"dst={MODEL_FORWARDER_CONTAINER_DIRECTORY},readonly",
            ]
        if self._agent_runtime(agent) == "pi":
            self._ensure_pi_roots(agent)
            pi = self._pi_spec(agent)
            pi_runtime_tree = self._pi_runtime_tree()
            args += [
                "--mount", f"type=bind,src={self._pi_host_workspace(agent)},"
                           f"dst={pi['workspace_root']}",
                "--mount", f"type=bind,src={self._pi_host_reference()},"
                           f"dst={pi['reference_root']},readonly",
                "--mount", f"type=bind,src={pi_runtime_tree},"
                           f"dst={pi_runtime_tree},readonly",
            ]
            if self.roles.get(agent) != "observer":
                args += ["--mount", f"type=bind,src={self._pi_host_team_memory()},"
                                   f"dst={pi['team_memory_root']}"]
                if pi.get("broker_socket"):
                    args += ["--mount", f"type=bind,src={self._pi_host_broker_dir()},"
                                       "dst=/run/mc-broker,readonly"]
        if self.container_site_packages is not None:
            args += ["--mount", f"type=bind,src={self.container_site_packages},"
                               f"dst={self.container_site_packages},readonly"]
        args += ["--mount", f"type=bind,src={self.socket_path},"
                           "dst=/run/mc-runner-rpc/control.sock"]
        if self.container_read_only:
            args += ["--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m"]
        # runc rejects a symlinked venv python as an OCI entrypoint on this
        # host; /bin/sh exec keeps the venv semantics and has no extra process.
        args += ["--entrypoint", "/bin/sh"]
        for key, value in self._container_environment(agent).items():
            args += ["-e", f"{key}={value}"]
        args += [self.container_image, "-c", 'exec "$@"', "sh",
                 self.container_python, "-m", self._agent_module(agent)]
        return args

    async def _spawn_container(self, agent, spec_path, log):
        state = self.states[agent]
        name = self._container_name(agent)
        await self._remove_owned_container(name)
        state_dir = self.run_dir / "agents" / agent
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (state_dir / "home").mkdir(exist_ok=True, mode=0o700)
        (state_dir / "tmp").mkdir(exist_ok=True, mode=0o700)
        arguments = self._container_args(agent, spec_path, state_dir)
        environment = {"PATH": self.runner_env.get("PATH", os.environ.get("PATH", ""))}
        if os.environ.get("DOCKER_HOST"):
            environment["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
        state["container"] = name
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker_bin, *arguments,
                cwd=str(self.run_dir), env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
        except BaseException:
            state["container"] = None
            raise
        return process

    async def _spawn(self, agent):
        if self.closing:
            raise RunnerControlError("runner manager is closing")
        # Re-verify before every launch/restart: a tampered snapshot must fail
        # closed instead of feeding a restarted runner changed source.
        await asyncio.to_thread(self._ensure_frozen_source)
        state = self.states[agent]
        spec_path = self.runners_dir / f"{agent}.json"
        if self._agent_runtime(agent) == "pi":
            await asyncio.to_thread(self._ensure_pi_roots, agent)
        spec = self._runner_spec(agent)
        await asyncio.to_thread(atomic_json, spec_path, spec)
        log_path = self.run_dir / "agents" / agent / "runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log_path.stat().st_size > self.log_max_bytes:
                rotated = log_path.with_name("runner.log.1")
                try:
                    rotated.unlink()
                except FileNotFoundError:
                    pass
                os.replace(log_path, rotated)
        except FileNotFoundError:
            pass
        log = log_path.open("ab")
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
        state["log_offset"] = log.tell()
        try:
            if self.runner_mode == "container":
                process = await self._spawn_container(agent, spec_path, log)
            else:
                environment = self._process_environment(agent, spec_path)
                process = await asyncio.create_subprocess_exec(
                    self.python, "-m", self._agent_module(agent),
                    cwd=str(self._process_cwd(agent)), env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
        except BaseException:
            log.close()
            state["container"] = None
            raise
        state.update(process=process, log=log, pid=process.pid, ready_event=asyncio.Event(),
                     finished_event=asyncio.Event(), result=None,
                     started_at=time.time(), last_heartbeat=time.monotonic())
        state["ready_info"] = None
        self._write_process_state()
        self._record("runner_process_started", {"agent_id": agent, "pid": process.pid,
                                                "container": state.get("container"),
                                                "attempt": state["attempts"] + 1})
        return process

    async def _container_label(self, name):
        code, output, _error = await self._docker(
            "inspect", "--format",
            '{{index .Config.Labels "minecraft-harness.run"}}', name, timeout=20)
        return output.strip() if code == 0 else None

    async def _remove_owned_container(self, name):
        if not name:
            return False
        owner = await self._container_label(name)
        if owner != self.run_id:
            return False
        await self._docker("rm", "-f", name, timeout=30)
        return True

    async def _stop_container(self, name, grace):
        if not name:
            return
        await self._docker("kill", "--signal=SIGTERM", name, timeout=15)
        deadline = time.monotonic() + max(0.0, float(grace))
        while time.monotonic() < deadline:
            code, _output, _error = await self._docker(
                "inspect", "--format", "{{.State.Running}}", name, timeout=10)
            if code != 0 or _output.strip() != "true":
                break
            await asyncio.sleep(0.1)
        await self._remove_owned_container(name)

    async def _kill(self, agent, process, *, graceful=False, grace=5):
        state = self.states[agent]
        container = state.get("container")
        if self.runner_mode == "container":
            if container:
                if graceful:
                    await self._stop_container(container, grace)
                else:
                    await self._remove_owned_container(container)
            if process is not None:
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        pass
        else:
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        pass
        if state.get("log") is not None:
            try:
                state["log"].flush()
                state["log"].close()
            except OSError:
                pass
            state["log"] = None
        state["process"] = None
        state["container"] = None
        self._write_process_state()

    def _write_process_state(self):
        if self.recorder is None:
            return
        value = {}
        for agent, state in self.states.items():
            if state.get("process") is not None:
                value[agent] = {"pid": state["process"].pid, "role": state["role"],
                                "container": state.get("container"),
                                "started_at": state.get("started_at"),
                                "attempts": state.get("attempts", 0)}
        try:
            self.recorder.set_state("runner_processes", value)
        except Exception:
            pass

    async def _backoff(self, attempt):
        delay = min(self.recovery_max_s, self.recovery_base_s * 2 ** min(max(attempt - 1, 0), 10))
        await asyncio.sleep(delay)

    async def _monitor(self, agent):
        state = self.states[agent]
        while True:
            process = state["process"]
            if process is None:
                return {"agent_id": agent, "status": "infra_failed",
                        "error": "runner process disappeared"}
            await process.wait()
            returncode = process.returncode
            if self.runner_mode == "container":
                await self._remove_owned_container(state.get("container"))
                state["container"] = None
            outcome = state["result"]
            if outcome is None:
                fallback = self._stdout_fallback(agent)
                if fallback is not None:
                    state["result"] = outcome = fallback
                    self._record("runner_result_recovered_from_stdout", {"agent_id": agent})
            if outcome is None and returncode == 0:
                try:
                    await asyncio.wait_for(state["finished_event"].wait(), 5)
                except TimeoutError:
                    pass
                outcome = state["result"]
            self._close_log(agent)
            self._record("runner_exited", {"agent_id": agent, "pid": process.pid,
                                           "returncode": returncode,
                                           "reported": bool(outcome)})
            if outcome is not None:
                return {**outcome, "agent_id": outcome.get("agent_id", agent)}
            if self.closing:
                return {"agent_id": agent, "status": "stopped", "returncode": returncode}
            state["attempts"] += 1
            self._record("runner_crashed", {"agent_id": agent, "pid": process.pid,
                                           "returncode": returncode,
                                           "attempt": state["attempts"]})
            if state["attempts"] > self.restart_limit:
                self._record("runner_restart_exhausted", {"agent_id": agent,
                                                          "attempts": state["attempts"]})
                return {"agent_id": agent, "status": "infra_failed",
                        "error": f"runner exceeded {self.restart_limit} restarts"}
            state["process"] = None
            self._write_process_state()
            try:
                cancelled = await self.server.cancel_agent(agent)
                if cancelled:
                    self._record("runner_rpc_handlers_cancelled", {"agent_id": agent,
                                                                   "count": cancelled})
            except Exception:
                pass
            if self.on_runner_lost is not None:
                try:
                    await self.on_runner_lost(agent)
                except Exception:
                    self._record("runner_budget_recovery_failed", {"agent_id": agent})
            await self._backoff(state["attempts"] - 1)
            if self.closing:
                return {"agent_id": agent, "status": "stopped"}
            while True:
                if self.closing:
                    return {"agent_id": agent, "status": "stopped"}
                try:
                    await self._restart(agent)
                    break
                except Exception as exc:
                    if self.closing:
                        return {"agent_id": agent, "status": "stopped"}
                    state["attempts"] += 1
                    self._record("runner_restart_failed", {"agent_id": agent,
                                                           "attempt": state["attempts"],
                                                           "error": type(exc).__name__})
                    if state["attempts"] > self.restart_limit:
                        return {"agent_id": agent, "status": "infra_failed",
                                "error": f"runner restart failed: {type(exc).__name__}"}
                    await self._backoff(state["attempts"] - 1)
            self._record("runner_restarted", {"agent_id": agent,
                                              "pid": state["process"].pid if state["process"] else None,
                                              "attempt": state["attempts"]})

    async def _restart(self, agent):
        state = self.states[agent]
        state["finished_event"] = asyncio.Event()
        state["result"] = None
        process = await self._spawn(agent)
        deadline = time.monotonic() + self.startup_timeout
        exit_task = asyncio.create_task(process.wait(), name=f"runner-exit-restart-{agent}")
        ready_task = asyncio.create_task(state["ready_event"].wait(), name=f"runner-ready-restart-{agent}")
        try:
            done, _ = await asyncio.wait({ready_task, exit_task}, timeout=max(0.1, deadline - time.monotonic()),
                                         return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            for task in (exit_task, ready_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(exit_task, ready_task, return_exceptions=True)
            await self._kill(agent, process)
            raise
        if ready_task in done and ready_task.result():
            exit_task.cancel()
            await asyncio.gather(exit_task, return_exceptions=True)
            # The configured restart limit is a consecutive-failure bound, not a
            # lifetime crash quota for a 54-hour run.
            state["attempts"] = 0
            return
        for task in (exit_task, ready_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(exit_task, ready_task, return_exceptions=True)
        await self._kill(agent, process)
        raise RunnerControlError("restarted runner did not become ready")

    def _stdout_fallback(self, agent):
        """Read the runner's one documented stdout fallback line, if any.

        Only the current process attempt is considered: the log is append-only
        across restarts and a stale fallback from an earlier attempt must never
        be attributed to the current one.
        """
        state = self.states[agent]
        path = self.run_dir / "agents" / agent / "runner.log"
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                start = max(int(state.get("log_offset", 0)), size - 256 * 1024)
                stream.seek(start)
                data = stream.read()
        except OSError:
            return None
        for line in reversed(data.splitlines()):
            if b"__runner_result__" not in line:
                continue
            try:
                document = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError):
                continue
            result = document.get("__runner_result__") if isinstance(document, dict) else None
            if isinstance(result, dict) and isinstance(result.get("status"), str):
                claimed = result.get("agent_id")
                if claimed is not None and claimed != agent:
                    continue
                return result
        return None

    def _close_log(self, agent):
        log = self.states[agent].get("log")
        if log is not None:
            try:
                log.flush()
                log.close()
            except OSError:
                pass
            self.states[agent]["log"] = None

    async def _heartbeat_watch(self, agent):
        state = self.states[agent]
        try:
            while not self.closing:
                await asyncio.sleep(self.poll_interval)
                if state.get("finished_event").is_set() or state.get("result") is not None:
                    return
                process = state.get("process")
                if process is None:
                    continue
                last = state.get("last_heartbeat", 0.0)
                if time.monotonic() - last <= self.heartbeat_timeout:
                    continue
                self._record("runner_heartbeat_timeout", {
                    "agent_id": agent, "pid": process.pid,
                    "timeout_s": self.heartbeat_timeout})
                if self.runner_mode == "container":
                    await self._remove_owned_container(state.get("container"))
                else:
                    try:
                        os.kill(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                # The monitor task handles the process exit/restart. Do not
                # emit a timeout event every poll interval for the same PID.
                return
        except asyncio.CancelledError:
            raise

    async def wait(self, agent):
        state = self.states.get(agent)
        if state is None:
            raise ValueError("unknown runner agent")
        monitor = state.get("monitor")
        if monitor is None:
            return {"agent_id": agent, "status": "infra_failed", "error": "runner never started"}
        return await monitor

    def snapshot(self):
        processes = {agent: {"pid": state.get("process").pid if state.get("process") else None,
                             "container": state.get("container"),
                             "role": state["role"], "attempts": state["attempts"],
                             "runtime": self._agent_runtime(agent),
                             "egress": self._agent_egress(agent),
                             "ready": state.get("ready_info") is not None}
                     for agent, state in self.states.items()}
        # Record which frozen source/runtime this process actually launched
        # runners from; supervisor manifests expose these under
        # manifest["runner_processes"].
        processes["source_sha256"] = self.source_sha256
        processes["pi_runtime_sha256"] = self.pi_runtime_sha256
        processes["runner_runtime"] = self.runner_runtime
        processes["runner_egress"] = self.runner_egress
        if self.runner_egress == _RUNNER_EGRESS_MODEL_ONLY:
            processes["model_forwarder"] = {
                "socket": str(self.model_forwarder_socket),
                "container_socket": MODEL_FORWARDER_CONTAINER_SOCKET,
            }
        return processes

    async def stop(self, *, grace=30.0):
        if self._stopped:
            return self._outcomes()
        self._stopped = True
        self.closing = True
        for state in self.states.values():
            task = state.get("heartbeat_task")
            if task is not None and not task.done():
                task.cancel()
        for agent in self.agents:
            state = self.states[agent]
            process = state.get("process")
            if process is None or process.returncode is not None:
                continue
            if self.runner_mode == "container":
                name = state.get("container")
                if name:
                    await self._docker("kill", "--signal=SIGTERM", name, timeout=15)
            else:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + max(0.0, float(grace))
        for agent in self.agents:
            state = self.states[agent]
            process = state.get("process")
            if process is None or process.returncode is not None:
                continue
            try:
                await asyncio.wait_for(process.wait(), max(0.05, deadline - time.monotonic()))
            except TimeoutError:
                if self.runner_mode == "container":
                    await self._remove_owned_container(state.get("container"))
                else:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    pass
        for agent in self.agents:
            monitor = self.states[agent].get("monitor")
            if monitor is not None and not monitor.done():
                try:
                    await asyncio.wait_for(asyncio.shield(monitor), 10)
                except TimeoutError:
                    monitor.cancel()
                except asyncio.CancelledError:
                    pass
        for agent in self.agents:
            self._close_log(agent)
            heartbeat = self.states[agent].get("heartbeat_task")
            if heartbeat is not None and not heartbeat.done():
                heartbeat.cancel()
            state = self.states[agent]
            if self.runner_mode == "container" and state.get("container"):
                await self._remove_owned_container(state["container"])
                state["container"] = None
            state["process"] = None
        try:
            await self.server.close()
        except Exception:
            pass
        shutil.rmtree(self.rpc_dir, ignore_errors=True)
        self._write_process_state()
        return self._outcomes()

    def _outcomes(self):
        return {agent: state.get("result") or {"agent_id": agent, "status": "stopped"}
                for agent, state in self.states.items()}
