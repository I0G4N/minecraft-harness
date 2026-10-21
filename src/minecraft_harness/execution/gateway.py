"""Durable, leased transport to isolated Node runtimes; uncertain actions never replay."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx

from ..recorder import TransportGapError
from .broker import SubmissionBroker

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted", "rejected", "invalidated"}


class RuntimeUnavailable(RuntimeError):
    retryable = True


class RuntimeReliabilityError(RuntimeError):
    retryable = False


def _tagged_processes(instance_id):
    """Find live local children without relying on a possibly stale worker PID."""
    marker = b"RUNTIME_INSTANCE_ID=" + instance_id.encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            if stat[stat.rfind(")") + 2:].split()[0] == "Z":
                continue
            if marker in (entry / "environ").read_bytes().split(b"\0"):
                found.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return found


class ExecutionGateway:
    def __init__(self, config, recorder, agent_ids, run_dir):
        self.config, self.recorder, self.agent_ids = config, recorder, agent_ids
        self.run_dir = Path(run_dir)
        self.world_epoch = recorder.get_state("world_epoch", 0)
        self.run_deadline = time.monotonic() + config.get("remaining_run_ms", config.get("max_background_ttl_ms", 129600000)) / 1000
        self.process = self.process_log = None
        self.runtime_instance_id = None
        self.saved_owned_runtime = recorder.get_state('owned_runtime')
        self.owned_state_lock = asyncio.Lock()
        self.auto_spawn = not config.get("url") and not config.get("urls")
        self.url = config.get("url")
        self.token = os.environ.get(config.get("token_env", "MC_RUNTIME_TOKEN"))
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(config.get("request_timeout_s", 10), connect=5), trust_env=False)
        self.locks = {agent: asyncio.Lock() for agent in agent_ids}
        self.event_locks = {agent: asyncio.Lock() for agent in agent_ids}
        self.node_cursors = {agent: 0 for agent in agent_ids}
        self.sessions = {}
        self.fencing = set()
        self.control_paused = set()
        self.admission = asyncio.Event()
        self.admission.set()
        self.maintenance = None
        self.maintenance_stopping = False
        self.heartbeat_tasks = {}
        self.journal_tasks = {}
        self.fault = None
        self.closing = False
        self.recovery_lock = asyncio.Lock()
        self.cli_broker = None
        self.completion_lock = asyncio.Lock()

    async def _record(self, method, *args, **kwargs):
        # fsync and SQLite FULL commits must not block lease renewal.
        operation = asyncio.create_task(asyncio.to_thread(getattr(self.recorder, method), *args, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # A cancelled coroutine must still wait for its durable transaction
            # before shutdown closes SQLite or another task sends an ACK.
            await operation
            raise

    def _url(self, agent_id=None):
        return self.config.get("urls", {}).get(agent_id, self.url)

    def _token(self, agent_id=None):
        env = self.config.get("token_envs", {}).get(agent_id)
        return os.environ.get(env) if env else self.token

    async def _request(self, method, path, body=None, agent_id=None, *, timeout=None):
        url, token = self._url(agent_id), self._token(agent_id)
        if not url or not token:
            raise RuntimeReliabilityError("runtime endpoint and supervisor token required")
        try:
            async with asyncio.timeout(timeout or self.config.get("request_timeout_s", 10)):
                async with self.client.stream(method, url.rstrip("/") + path, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(timeout or self.config.get("request_timeout_s", 10), connect=min(5, timeout or self.config.get("request_timeout_s", 10)))) as response:
                    if method == "DELETE" and response.status_code == 404:
                        return {"deleted": True, "already_absent": True}
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.config.get("response_max_bytes", 4194304):
                            raise RuntimeReliabilityError("runtime response exceeds configured byte limit")
                        chunks.append(chunk)
                    try:
                        result = json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError) as exc:
                        raise RuntimeReliabilityError("runtime returned malformed JSON") from exc
                    if not isinstance(result, dict):
                        raise RuntimeReliabilityError("runtime returned a non-object response")
                    return result
        except (httpx.TransportError, TimeoutError, OSError) as exc:
            raise RuntimeUnavailable(f"runtime transport: {type(exc).__name__}") from exc

    @staticmethod
    def _failure(exc):
        if isinstance(exc, (RuntimeUnavailable, RuntimeReliabilityError)):
            return exc
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in {401, 403, 410}:
                return RuntimeReliabilityError(f"runtime protocol/authority failure (HTTP {code})")
            if code in {404, 408, 409, 425, 429} or code >= 500:
                return RuntimeUnavailable(f"runtime temporarily unavailable or fenced (HTTP {code})")
            return RuntimeReliabilityError(f"invalid runtime operation (HTTP {code})")
        if isinstance(exc, (httpx.HTTPError, TimeoutError, OSError)):
            return RuntimeUnavailable(f"runtime transport: {type(exc).__name__}")
        return RuntimeReliabilityError(f"runtime recorder/protocol failure: {type(exc).__name__}: {exc}")

    def _set_fault(self, exc):
        fault = self._failure(exc)
        # A transient error must never erase a known gap or storage failure.
        if not isinstance(self.fault, RuntimeReliabilityError):
            self.fault = fault
        self.admission.clear()
        return self.fault

    async def _persist_owned(self):
        if not self.auto_spawn or not self.runtime_instance_id:
            return
        async with self.owned_state_lock:
            value = {"instance_id": self.runtime_instance_id, "pid": self.process.pid if self.process else None,
                     "sessions": [{key: session.get(key) for key in ("agent_id", "run_id", "world_epoch", "session_id", "worker_pid")} for session in self.sessions.values()]}
            await self._record("set_state", "owned_runtime", value)
            self.saved_owned_runtime = value

    async def _recover_previous_owned(self):
        saved = self.saved_owned_runtime
        if not saved or not isinstance(saved, dict) or not saved.get("instance_id"):
            return
        self.runtime_instance_id = saved["instance_id"]
        old_sessions = {session["agent_id"]: session for session in saved.get("sessions", []) if session.get("agent_id") in self.agent_ids}
        self.sessions.update(old_sessions)
        await self._fence_owned_processes()
        self.runtime_instance_id = None

    async def _spawn(self):
        if self.config["mode"] != "fake":
            raise RuntimeReliabilityError("real generated code requires an isolated external runtime")
        node = shutil.which("node")
        if not node:
            raise RuntimeUnavailable("Node.js is required")
        runtime_dir = Path(__file__).resolve().parents[3] / "bot_runtime"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.url, self.token = f"http://127.0.0.1:{port}", secrets.token_urlsafe(32)
        self.runtime_instance_id = uuid.uuid4().hex
        await self._persist_owned()
        env = {"PATH": os.environ.get("PATH", ""), "MODE": "fake", "BIND": "127.0.0.1", "PORT": str(port), "TOKEN": self.token,
               "RUNTIME_INSTANCE_ID": self.runtime_instance_id,
               "LIFECYCLE": self.config.get("lifecycle", "strict"), "MAX_BACKGROUND_JOBS": str(self.config.get("max_background_jobs", 8)),
               "MAX_BACKGROUND_TTL_MS": str(self.config.get("max_background_ttl_ms", 129600000)),
               "MAX_TIMEOUT_MS": str(int(self.config["program_timeout_max_s"] * 1000)),
               "SUPERVISOR_LEASE_MS": str(int(self.config.get("lease_timeout_s", 30) * 1000)), "EXIT_ON_LEASE_EXPIRY": "true"}
        option_env = {"maxEvents": "MAX_BUFFERED_EVENTS", "maxEventBytes": "MAX_EVENT_BYTES", "maxRetainedExecutions": "MAX_RETAINED_EXECUTIONS",
                      "maxQueue": "MAX_QUEUE", "maxLogs": "MAX_PROGRAM_LOGS", "maxLogBytes": "MAX_PROGRAM_LOG_BYTES",
                      "maxResultBytes": "MAX_RESULT_BYTES", "maxResponseBytes": "MAX_RESPONSE_BYTES", "maxWorkerRssBytes": "MAX_WORKER_RSS_BYTES",
                      "parentWatchdogMs": "PARENT_WATCHDOG_MS", "maxSessions": "MAX_SESSIONS"}
        for option, variable in option_env.items():
            if option in self.config.get("options", {}):
                env[variable] = str(self.config["options"][option])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.process_log = (self.run_dir / "runtime.log").open("ab")
        self.process = await asyncio.create_subprocess_exec(node, str(runtime_dir / "src/server.js"), cwd=runtime_dir, env=env,
                                                           stdout=self.process_log, stderr=self.process_log, start_new_session=True)
        await self._persist_owned()
        deadline = time.monotonic() + self.config.get("startup_timeout_s", 60)
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise RuntimeUnavailable("bot runtime exited; see runtime.log")
            try:
                await self._request("GET", "/health", timeout=min(1, self.config.get("request_timeout_s", 10)))
                return
            except RuntimeUnavailable:
                await asyncio.sleep(.05)
        raise RuntimeUnavailable("bot runtime startup deadline exceeded")

    def _identity(self, session):
        return {key: session[key] for key in ("run_id", "world_epoch", "session_id")}

    def _register(self, agent, session):
        if not session.get("session_id") or session.get("run_id") != self.recorder.run_id or session.get("world_epoch") != self.world_epoch:
            raise RuntimeReliabilityError("runtime protocol v2 session identity mismatch")
        self._check(session)
        if session.get("closed"):
            raise RuntimeUnavailable("runtime session is already closed")
        if self.config.get("lifecycle") == "paper_aligned" and self.config.get("mode") == "minecraft":
            if session.get("worker_transport") != "remote" or not (session.get("native_sandbox") or {}).get("native_filter") or not (session.get("execution_boundary") or {}).get("identity_bound"):
                raise RuntimeReliabilityError("paper_aligned real runtime did not confirm separate native-filtered identity-bound worker")
        self.sessions[agent] = session
        if session.get('paused'):
            self.control_paused.add(agent)
        else:
            self.control_paused.discard(agent)
        # The manager creates independent heartbeat and journal tasks per agent.
        self._ensure_tasks(agent, session)
        if self.maintenance is None or self.maintenance.done():
            self.maintenance = asyncio.create_task(self._maintain(), name="runtime-maintenance")

    def _ensure_tasks(self, agent, session):
        if self.maintenance_stopping:
            return
        for tasks, loop, label in ((self.heartbeat_tasks, self._heartbeat, "heartbeat"), (self.journal_tasks, self._journal, "journal")):
            previous = tasks.get(agent)
            if previous and getattr(previous, "session_id", None) == session["session_id"] and not previous.done():
                continue
            if previous:
                previous.cancel()
            task = asyncio.create_task(loop(agent, session), name=f"runtime-{label}-{agent}")
            task.session_id = session["session_id"]
            tasks[agent] = task

    async def start(self, paused=False):
        self.closing = False
        self.maintenance_stopping = False
        if isinstance(self.fault, RuntimeReliabilityError):
            raise self.fault
        self.fault = None
        if self.auto_spawn and self.process is None and self.runtime_instance_id is None:
            await self._recover_previous_owned()
        if self.auto_spawn and (not self.process or self.process.returncode is not None):
            if self.process_log:
                self.process_log.close()
            await self._spawn()
        health = {}
        for agent in self.agent_ids:
            health[agent] = await self._request("GET", "/health", agent_id=agent)
            required_protocol = 3 if self.config.get("lifecycle") == "paper_aligned" else 2
            if health[agent].get("protocol_version", 0) < required_protocol:
                raise RuntimeReliabilityError(f"runtime protocol v{required_protocol} with leases and durable ACK required")
            if required_protocol == 3 and not health[agent].get("capabilities", {}).get("background_jobs"):
                raise RuntimeReliabilityError("paper_aligned runtime requires supervised background job capability")
            observed_mode = health[agent].get("mode", health[agent].get("runtime", {}).get("mode"))
            if observed_mode and observed_mode != self.config["mode"]:
                raise RuntimeReliabilityError("runtime mode does not match configuration")
            session = await self._request("POST", "/v1/sessions", {"agent_id": agent, "username": agent, "world_epoch": self.world_epoch,
                                          "run_id": self.recorder.run_id, "paused": paused, **({"lifecycle": self.config.get("lifecycle", "strict"), "max_background_jobs": self.config.get("max_background_jobs", 8), "remaining_run_ms": max(1, min(int((self.run_deadline - time.monotonic()) * 1000), self.config.get("max_background_ttl_ms", 129600000)))} if health[agent].get("protocol_version", 0) >= 3 else {})}, agent, timeout=self.config.get("startup_timeout_s", 60))
            self.node_cursors[agent] = await self._record("transport_cursor", agent, session["session_id"])
            self._register(agent, session)
            await self._persist_owned()
        if self.cli_broker is None:
            self.cli_broker = SubmissionBroker(self)
            await self.cli_broker.start()
        await self._record("append", "runtime_started", {"mode": self.config["mode"], "health": health}, world_epoch=self.world_epoch)
        if self.fault:
            raise self.fault

    def cli_environment(self, agent_id):
        if self.cli_broker is None or agent_id not in self.locks:
            raise RuntimeError("player CLI broker is not running")
        return self.cli_broker.environment(agent_id)

    def _check(self, result, session=None, allow_expired=False):
        if session and result.get("session_id") != session["session_id"]:
            raise RuntimeReliabilityError("runtime response belongs to a different session")
        if result.get("reliability_fault") or result.get("dropped_events"):
            raise RuntimeReliabilityError("runtime journal overflow or reliability fault; evidence incomplete")
        if result.get("lease_expired") and not allow_expired:
            raise RuntimeUnavailable("runtime supervisor lease expired")

    async def _maintain(self):
        try:
            while not self.closing and not self.maintenance_stopping:
                for agent, session in list(self.sessions.items()):
                    if agent not in self.fencing and not session.get('closed'):
                        self._ensure_tasks(agent, session)
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            pass

    def _observe_runtime_epoch(self, agent, session_id, epoch):
        current = self.sessions.get(agent)
        if not current or current.get("session_id") != session_id or epoch is None:
            return
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise RuntimeReliabilityError("invalid runtime epoch metadata")
        current["runtime_epoch"] = max(current.get("runtime_epoch") or 0, epoch)

    def _current(self, agent, session):
        return not self.closing and self.sessions.get(agent, {}).get("session_id") == session["session_id"]

    async def _heartbeat(self, agent, session):
        lease = session.get("lease_timeout_ms", self.config.get("lease_timeout_s", 30) * 1000) / 1000
        interval = min(self.config.get("heartbeat_interval_s", 5), lease / 3)
        timeout = max(.01, min(self.config.get("request_timeout_s", 10), lease / 4))
        try:
            while self._current(agent, session) and not self.maintenance_stopping:
                begun = time.monotonic()
                if not self.sessions.get(agent, {}).get('closed'):
                    try:
                        result = await self._request("POST", f"/v1/sessions/{agent}/heartbeat", self._identity(session), agent, timeout=timeout)
                        self._check(result, session)
                        if not self._current(agent, session) or self.sessions.get(agent, {}).get('closed'):
                            return
                        old_worker = self.sessions.get(agent, {}).get('worker_pid')
                        old_epoch = self.sessions.get(agent, {}).get('runtime_epoch') or 0
                        self.sessions[agent] = {**session, **result}
                        self._observe_runtime_epoch(agent, session['session_id'], old_epoch)
                        if result.get('worker_pid') != old_worker:
                            await self._persist_owned()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if self._current(agent, session) and not self.sessions.get(agent, {}).get('closed'):
                            self._set_fault(exc)
                if self.maintenance_stopping:
                    return
                await asyncio.sleep(max(.01, interval - (time.monotonic() - begun)))
        except asyncio.CancelledError:
            pass

    async def _journal(self, agent, session):
        try:
            while self._current(agent, session) and not self.maintenance_stopping:
                if agent not in self.fencing:
                    try:
                        async with self.event_locks[agent]:
                            await self._drain(agent, session, max_pages=1)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._set_fault(exc)
                if self.maintenance_stopping:
                    return
                await asyncio.sleep(min(.25, self.config.get("heartbeat_interval_s", 5)))
        except asyncio.CancelledError:
            pass

    async def _drain(self, agent, session, max_pages=4):
        for _ in range(max_pages):
            cursor = await self._record("transport_cursor", agent, session["session_id"])
            result = await self._request("GET", f"/v1/sessions/{agent}/events?" + urlencode({"after_cursor": cursor, "limit": 500, "session_id": session["session_id"]}), agent_id=agent)
            await self._commit_ack(agent, session, result)
            if not result.get("has_more"):
                return

    async def _commit_ack(self, agent, session, result, closing=False):
        events = result.get("events")
        if not isinstance(events, list) or result.get("session_id") != session["session_id"]:
            raise RuntimeReliabilityError("invalid/mixed runtime event page")
        try:
            cursor = await self._record("ingest_transport", agent, session["session_id"], events)
        except TransportGapError as exc:
            raise RuntimeReliabilityError(str(exc)) from exc
        if result.get("next_cursor") != cursor:
            raise RuntimeReliabilityError("runtime event page cursor differs from committed journal")
        self.node_cursors[agent] = cursor
        for event in events:
            self._observe_runtime_epoch(agent, event.get('session_id'), event.get('runtime_epoch'))
        self._check(result, session, allow_expired=closing)
        ack = await self._request("POST", f"/v1/sessions/{agent}/events/ack", {**self._identity(session), "cursor": cursor}, agent)
        self._check(ack, session, allow_expired=closing)
        if ack.get("acked_cursor", -1) < cursor:
            raise RuntimeReliabilityError("runtime failed to acknowledge committed cursor")

    async def submit(self, agent_id, source, timeout_ms=60000, request_id=None):
        if agent_id not in self.locks:
            raise PermissionError("unknown agent")
        expected_epoch = self.world_epoch
        await self.admission.wait()
        if not isinstance(source, str) or len(source.encode()) > 524288:
            raise ValueError("program must be a string of at most 512 KiB")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 10 <= timeout_ms <= self.config["program_timeout_max_s"] * 1000:
            raise ValueError("program timeout must be an integer from 10 ms through the configured maximum")
        request_id = request_id or uuid.uuid4().hex
        digest = hashlib.sha256(json.dumps([source, timeout_ms], ensure_ascii=False).encode()).hexdigest()
        async with self.locks[agent_id]:
            await self.admission.wait()
            if self.world_epoch != expected_epoch:
                return {"status": "invalidated", "error": "world changed; observe and replan", "world_epoch": self.world_epoch}
            previous = await self._record("submission", agent_id, request_id, digest=digest)
            if previous:
                return previous
            session = self.sessions[agent_id]
            pending = {"agent_id": agent_id, "request_id": request_id, "status": "unknown", "source_ref": await self._record("put_artifact", source),
                       **self._identity(session), "timeout_ms": timeout_ms}
            pending, created = await self._record("reserve_submission", agent_id, request_id, digest, pending)
            if not created:
                return pending
            try:
                record = await self._request("POST", f"/v1/sessions/{agent_id}/programs", {"request_id": request_id, "request_sequence": pending["request_sequence"],
                                              "source": source, "timeout_ms": timeout_ms, **self._identity(session)}, agent_id)
                self._check(record, session)
                await self._record("update_submission", agent_id, request_id, record)
                await self._record("append", "execution_accepted", record, actor_id=agent_id, execution_id=record.get("execution_id"), world_epoch=expected_epoch)
                return record
            except asyncio.CancelledError:
                known = await self._record("submission", agent_id, request_id) or pending
                if known.get("execution_id"):
                    try:
                        await asyncio.shield(self.cancel(agent_id, known["execution_id"]))
                    except Exception:
                        pass
                raise
            except Exception as exc:
                known = await self._record("submission", agent_id, request_id) or pending
                if known.get("status") in TERMINAL:
                    return known
                fault = self._failure(exc)
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {400, 403, 409, 410, 413, 429}:
                    record = {**known, "status": "rejected", "error": str(fault)}
                else:
                    record = {**known, "status": "unknown", "error": str(fault)}
                await self._record("update_submission", agent_id, request_id, record)
                await self._record("append", "execution_" + record["status"], record, actor_id=agent_id, world_epoch=expected_epoch)
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                ordinary_rejection = status in {400, 413, 422, 429} or (status == 409 and (agent_id in self.control_paused or self.sessions.get(agent_id, {}).get('paused')))
                if self._current(agent_id, session) and not ordinary_rejection:
                    self._set_fault(fault)
                if isinstance(fault, RuntimeReliabilityError) and not ordinary_rejection:
                    raise fault from exc
                return record

    async def _observe_finished(self, agent_id, record):
        if record.get("status") not in TERMINAL or not record.get("execution_id"):
            return record
        key = "execution_finished:" + record["execution_id"]
        async with self.completion_lock:
            if not await self._record("get_state", key, False):
                await self._record("state_event", key, True, "execution_finished", record,
                                   actor_id=agent_id, execution_id=record["execution_id"],
                                   world_epoch=record.get("world_epoch", self.world_epoch), runtime_epoch=record.get("runtime_epoch") or 0)
        return record

    async def execute(self, agent_id, source, timeout_ms=60000, request_id=None):
        """Compatibility synchronous helper: admission is durable before waiting."""
        record = await self.submit(agent_id, source, timeout_ms, request_id)
        if record.get("status") in TERMINAL | {"unknown"} or not record.get("execution_id"):
            return await self._observe_finished(agent_id, record)
        try:
            record = await self.wait(agent_id, record["execution_id"], timeout_ms + 30000)
            if record.get("wait_expired") and record.get("status") not in TERMINAL | {"unknown"}:
                record = await self.cancel(agent_id, record["execution_id"])
            return await self._observe_finished(agent_id, record)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.cancel(agent_id, record["execution_id"]))
            except Exception:
                pass
            raise

    async def wait(self, agent_id, execution_id, wait_ms=1000):
        """Wait for an existing handle; expiry never cancels or resubmits it."""
        if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= 86400000:
            raise ValueError("wait_ms must be an integer between zero and 86400000")
        deadline = time.monotonic() + wait_ms / 1000
        record = await self._record("execution", agent_id, execution_id) or {"execution_id": execution_id, "status": "unknown"}
        first = True
        while first or record.get("status") not in TERMINAL | {"unknown"}:
            first = False
            if record.get("status") in TERMINAL:
                return record
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**record, "wait_expired": True}
            try:
                async with asyncio.timeout(remaining):
                    record = await self.status(agent_id, execution_id)
            except TimeoutError:
                return {**record, "wait_expired": True}
            except RuntimeUnavailable as exc:
                self._set_fault(exc)
                return {**record, "status": "unknown", "error": "status unavailable; reconcile existing execution, never replay"}
            if record.get("status") in TERMINAL | {"unknown"}:
                return record
            await asyncio.sleep(min(.05, max(0, deadline - time.monotonic())))
        return record

    async def jobs(self, agent_id):
        if agent_id not in self.locks:
            raise PermissionError("unknown agent")
        session = self.sessions[agent_id]
        if session.get("protocol_version", 2) < 3:
            return {"jobs": [], "supported": False, **self._identity(session)}
        result = await self._request("GET", f"/v1/sessions/{agent_id}/jobs?" + urlencode({"session_id": session["session_id"]}), agent_id=agent_id)
        self._check(result, session)
        return result

    async def cancel_job(self, agent_id, job_id):
        if agent_id not in self.locks:
            raise PermissionError("unknown agent")
        uuid.UUID(job_id)
        session = self.sessions[agent_id]
        result = await self._request("POST", f"/v1/sessions/{agent_id}/jobs/{job_id}/cancel?" + urlencode({"session_id": session["session_id"]}), {}, agent_id)
        self._check(result, session)
        await self._record("append", "job_cancel_result", result, actor_id=agent_id, world_epoch=self.world_epoch)
        return result

    async def status(self, agent_id, execution_id):
        if agent_id not in self.locks:
            raise PermissionError("unknown agent")
        uuid.UUID(execution_id)
        cached = await self._record("execution", agent_id, execution_id)
        if cached and cached.get("status") in TERMINAL:
            self._observe_runtime_epoch(agent_id, cached.get('session_id'), cached.get('runtime_epoch'))
            return await self._observe_finished(agent_id, cached)
        session = self.sessions.get(agent_id)
        query = "?" + urlencode({"session_id": (cached or {}).get("session_id") or session["session_id"]}) if session else ""
        try:
            record = await self._request("GET", f"/v1/sessions/{agent_id}/programs/{execution_id}{query}", agent_id=agent_id)
            if record.get("execution_id") != execution_id:
                raise RuntimeReliabilityError("runtime execution identity mismatch")
            if record.get("request_id"):
                await self._record("update_submission", agent_id, record["request_id"], record)
            self._observe_runtime_epoch(agent_id, record.get('session_id'), record.get('runtime_epoch'))
            return await self._observe_finished(agent_id, record)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404, 409, 410):
                raise
            return cached if cached and cached.get("status") in TERMINAL else {**(cached or {}), "execution_id": execution_id, "status": "unknown", "error": "not retained; reconcile durable journal"}

    async def cancel(self, agent_id, execution_id):
        if agent_id not in self.locks:
            raise PermissionError("unknown agent")
        uuid.UUID(execution_id)
        cached = await self._record("execution", agent_id, execution_id)
        if cached and cached.get("status") in TERMINAL:
            self._observe_runtime_epoch(agent_id, cached.get('session_id'), cached.get('runtime_epoch'))
            return await self._observe_finished(agent_id, cached)
        session = self.sessions.get(agent_id)
        query = "?" + urlencode({"session_id": (cached or {}).get("session_id") or session["session_id"]}) if session else ""
        result = await self._request("POST", f"/v1/sessions/{agent_id}/programs/{execution_id}/cancel{query}", {}, agent_id)
        if result.get("request_id"):
            await self._record("update_submission", agent_id, result["request_id"], result)
        return result

    async def read_events(self, agent_id, after_cursor=0, limit=100):
        if isinstance(after_cursor, bool) or not isinstance(after_cursor, int) or not 0 <= after_cursor < 2 ** 63:
            raise ValueError('event cursor must be a nonnegative signed-64-bit integer')
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('event limit must be a positive integer')
        if agent_id not in self.event_locks:
            raise PermissionError("unknown agent")
        if self.fault:
            raise self._failure(self.fault)
        try:
            async with self.event_locks[agent_id]:
                if agent_id in self.sessions and agent_id not in self.fencing:
                    await self._drain(agent_id, self.sessions[agent_id])
                visible = await self._record("player_events", agent_id, after_cursor, min(limit, 500))
                session = self.sessions.get(agent_id, {})
                return {"events": visible, "next_cursor": visible[-1]["cursor"] if visible else after_cursor,
                        "world_epoch": self.world_epoch, "runtime_epoch": session.get('runtime_epoch') or 0, "session_id": session.get('session_id')}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self._set_fault(exc) from exc

    def _capture_events(self, agent_id, events):
        session_id = events[0].get("session_id") if events else self.sessions.get(agent_id, {}).get("session_id")
        if session_id:
            self.node_cursors[agent_id] = self.recorder.ingest_transport(agent_id, session_id, events)

    async def pause_sessions(self):
        self.control_paused.update(self.agent_ids)
        results = await asyncio.gather(*(self._request("POST", f"/v1/sessions/{agent}/pause?" + urlencode({"session_id": self.sessions[agent]["session_id"]}), {}, agent) for agent in self.agent_ids))
        for agent, result in zip(self.agent_ids, results):
            self._check(result, self.sessions[agent])
            self.sessions[agent] = result

    async def resume_sessions(self):
        results = await asyncio.gather(*(self._request("POST", f"/v1/sessions/{agent}/resume?" + urlencode({"session_id": self.sessions[agent]["session_id"]}), {}, agent) for agent in self.agent_ids))
        for agent, result in zip(self.agent_ids, results):
            self._check(result, self.sessions[agent])
            self.sessions[agent] = result
        self.control_paused.clear()

    def announce_restored(self, rollback=False, **lineage):
        event = {"kind": "world_restored", "world_epoch": self.world_epoch, "runtime_epoch": 0,
                 "payload": {"globals_lost": True, "requires_reobservation": True, "rollback": rollback, **lineage}}
        for agent in self.agent_ids:
            self.recorder.append("player_event", event, actor_id=agent, world_epoch=self.world_epoch)

    async def _settle(self, agent, session, reason):
        if hasattr(self.recorder, "settle_session"):
            await self._record("settle_session", agent, session["session_id"], reason)

    async def fence(self):
        async def delete(agent):
            self.fencing.add(agent)
            try:
                async with self.event_locks[agent]:
                    session = self.sessions.get(agent)
                    if not session:
                        try:
                            session = await self._request("GET", f"/v1/sessions/{agent}", agent_id=agent)
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code == 404:
                                return
                            raise
                    for _ in range(128):
                        cursor = await self._record("transport_cursor", agent, session["session_id"])
                        query = urlencode({"retain": "true", "after_cursor": cursor, "session_id": session["session_id"]})
                        result = await self._request("DELETE", f"/v1/sessions/{agent}?{query}", agent_id=agent)
                        if result.get("already_absent"):
                            raise RuntimeReliabilityError("registered session disappeared before its final audit tail was recorded")
                        if not result.get("confirmed_old_process_exit"):
                            raise RuntimeReliabilityError("worker process exit was not confirmed")
                        if agent in self.sessions:
                            self.sessions[agent]['closed'] = True
                        await self._commit_ack(agent, session, result, closing=True)
                        if not result.get("has_more"):
                            cursor = self.node_cursors[agent]
                            final = await self._request("DELETE", f"/v1/sessions/{agent}?" + urlencode({"after_cursor": cursor, "session_id": session["session_id"]}), agent_id=agent)
                            if not final.get("deleted"):
                                continue
                            if final.get("events"):
                                raise RuntimeReliabilityError("closed runtime audit tail changed after final ACK")
                            break
                    else:
                        raise RuntimeReliabilityError("session close journal did not converge")
                    await self._settle(agent, session, "old worker process exit confirmed; action outcomes without terminal evidence remain unobserved")
                    self.sessions.pop(agent, None)
                    await self._persist_owned()
            finally:
                self.fencing.discard(agent)
        results = await asyncio.gather(*(delete(agent) for agent in self.agent_ids), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            normalized = [self._failure(exc) for exc in errors]
            critical = next((exc for exc in normalized if isinstance(exc, RuntimeReliabilityError)), None)
            raise critical or RuntimeUnavailable("could not confirm all old bot sessions stopped: " + str(normalized[0]))

    async def _stop_maintenance(self):
        # Transport cleanup may consume an asyncio cancellation while unwinding
        # a shielded AnyIO operation. A cancellation request alone is therefore
        # insufficient to stop/refrain from recreating maintenance loops.
        self.maintenance_stopping = True
        tasks = [task for task in [self.maintenance, *self.heartbeat_tasks.values(), *self.journal_tasks.values()] if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.maintenance = None
        self.heartbeat_tasks.clear()
        self.journal_tasks.clear()

    async def _fence_owned_processes(self):
        if not self.auto_spawn or not self.runtime_instance_id or sys.platform != "linux":
            raise RuntimeUnavailable("cannot independently certify owned runtime process cleanup on this platform")
        if self.process and self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(self.process.wait(), 5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            pids = await asyncio.to_thread(_tagged_processes, self.runtime_instance_id)
            if not pids:
                for agent, session in list(self.sessions.items()):
                    await self._settle(agent, session, "owned runtime instance process groups confirmed absent; action effects not replayed")
                await self._record("append", "owned_runtime_processes_fenced", {"runtime_instance_id": self.runtime_instance_id, "sessions": list(self.sessions.values()), "unobserved_outcomes": "interrupted"}, world_epoch=self.world_epoch)
                self.sessions.clear()
                await self._record('set_state', 'owned_runtime', None)
                self.saved_owned_runtime = None
                return
            # Also clean a guardian that failed after its worker died. The marker
            # comes from our private process creation, never from a remote URL.
            for pid in pids:
                try:
                    group = os.getpgid(pid)
                    if group != os.getpgrp():
                        os.killpg(group, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await asyncio.sleep(.05)
        raise RuntimeUnavailable("owned runtime descendants did not stop within cleanup deadline")

    async def recover(self, paused=True):
        async with self.recovery_lock:
            if isinstance(self.fault, RuntimeReliabilityError):
                # A runtime restart can lose the session registry. Long-run
                # profiles may opt into an explicit, auditable session-loss
                # reset; the default remains fatal so unknown action tails are
                # never silently discarded.
                if not self.config.get("recover_session_loss"):
                    raise self.fault
            self.admission.clear()
            await self._stop_maintenance()
            try:
                await self.fence()
            except RuntimeReliabilityError as exc:
                if not self.config.get("recover_session_loss"):
                    raise
                self.sessions.clear()
                await self._record("append", "runtime_session_lost", {"error": type(exc).__name__,
                    "message": str(exc)[:2000], "globals_lost": True, "requires_reobservation": True})
            except RuntimeUnavailable:
                if not self.auto_spawn:
                    raise
                await self._fence_owned_processes()
            await self.reset_world(already_fenced=True, paused=paused)
            if not paused:
                self.admission.set()

    async def reset_world(self, already_fenced=False, paused=False, rollback=False, **lineage):
        was_open = self.admission.is_set()
        self.admission.clear()
        if not already_fenced:
            try:
                await self.fence()
            except RuntimeUnavailable:
                if not self.auto_spawn:
                    raise
                await self._stop_maintenance()
                await self._fence_owned_processes()
        source_epoch = self.world_epoch
        self.world_epoch += 1
        await self._record("set_state", "world_epoch", self.world_epoch)
        await self.start(paused=paused)
        lineage.setdefault("source_world_epoch", source_epoch)
        await self._record("append", "world_restored", {"runtime_reset": True, "globals_lost": True, "requires_reobservation": True, "rollback": rollback, **lineage}, world_epoch=self.world_epoch)
        await asyncio.to_thread(self.announce_restored, rollback, **lineage)
        if was_open and not paused:
            self.admission.set()

    async def close(self, fence=True):
        shutdown_error = None
        self.closing = True
        self.admission.clear()
        if self.cli_broker:
            await self.cli_broker.close()
            self.cli_broker = None
        await self._stop_maintenance()
        try:
            if fence and (self.url or self.config.get("urls")):
                try:
                    await self.fence()
                except Exception as exc:
                    shutdown_error = exc
                    if self.auto_spawn:
                        try:
                            await self._fence_owned_processes()
                            if isinstance(exc, RuntimeUnavailable):
                                shutdown_error = None
                        except Exception:
                            pass
                    if shutdown_error:
                        try:
                            await self._record("append", "runtime_shutdown_unconfirmed", {"error": str(exc), "outcomes": "unknown"}, world_epoch=self.world_epoch)
                        except Exception:
                            pass
        finally:
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            if not shutdown_error and self.auto_spawn:
                await self._record('set_state', 'owned_runtime', None)
                self.saved_owned_runtime = None
            if self.process_log:
                self.process_log.close()
            await self.client.aclose()
        if shutdown_error:
            raise RuntimeError("could not confirm runtime shutdown; lease/watchdog will fence; inspect ledger") from shutdown_error
