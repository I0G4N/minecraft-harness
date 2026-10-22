"""Authenticated Unix-socket RPC between the control plane and independent runners.

Wire format: every frame is ``4-byte unsigned big-endian length | UTF-8 JSON
object``. A connection always begins with a ``hello`` handshake that
authenticates a per-run token; the server binds the connection to that token's
identity and never trusts client-supplied identity values. While a handler runs
an EOF watcher keeps reading the connection so a disconnected client cancels its
in-flight work instead of leaving a shell/gateway call running for hours.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import socket
import stat
import struct
from pathlib import Path

__all__ = ["RpcClient", "RpcError", "RpcServer"]

MAX_FRAME_BYTES = 512 * 1024 * 1024
# Exception type names the runner maps back to real local classes.
KNOWN_ERROR_TYPES = frozenset({
    "BudgetExhausted",
    "ModelAdmissionError",
    "StorageLimitError",
    "RuntimeUnavailable",
    "RuntimeReliabilityError",
    "MemoryConflict",
    "MemoryPermissionError",
    "PermissionError",
    "FileNotFoundError",
    "ValueError",
    "TimeoutError",
})

_HEADER = struct.Struct(">I")
_HEADER_BYTES = _HEADER.size
_ROLES = frozenset({"player", "observer"})
_INVALID_HANDSHAKE = object()
_TIMEOUT_MESSAGE = "RPC call timed out"
# Every call without an explicit timeout is bounded so a dead or wedged peer
# cannot block a runner process forever. The Supervisor sets
# HARNESS_RUNNER_RPC_TIMEOUT_S from runtime.runner_rpc_timeout_s for process
# runners; container runners currently use the default until the runner spawner
# forwards the same variable.
try:
    _ENV_CALL_TIMEOUT_S = float(os.environ.get("HARNESS_RUNNER_RPC_TIMEOUT_S", "120"))
except (TypeError, ValueError):
    _ENV_CALL_TIMEOUT_S = 120.0
if not 0 < _ENV_CALL_TIMEOUT_S <= 3600:
    _ENV_CALL_TIMEOUT_S = 120.0
DEFAULT_CALL_TIMEOUT_S = _ENV_CALL_TIMEOUT_S
# Game-facing calls have their own authoritative tool timeouts (program/shell
# timeout, bounded wait); applying the RPC default here would kill legitimate
# long-running programs. All other (control-plane bookkeeping) calls are bounded.
UNBOUNDED_METHODS = frozenset({
    "gateway.execute", "gateway.submit", "gateway.wait", "workspace.shell",
    # Pause gate long-poll: must survive an arbitrarily long operator pause.
    "budget.wait_admission",
})


class RpcError(RuntimeError):
    """A remote call failed; ``error_type`` and ``retryable`` mirror the server frame."""

    error_type: str
    retryable: bool

    def __init__(self, error_type, message="", *, retryable=False):
        super().__init__(message)
        self.error_type = str(error_type)
        self.retryable = bool(retryable)


class _FramingError(RuntimeError):
    """A peer violated the framing/JSON contract; the connection must close."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = bool(retryable)


class _TransportClosed(ConnectionError):
    """The peer closed the connection before a complete response arrived."""


def _encode_frame(value, max_frame_bytes):
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _FramingError(f"RPC frame is not JSON-serializable: {exc}") from exc
    if len(payload) > max_frame_bytes:
        raise _FramingError(f"RPC frame of {len(payload)} bytes exceeds the {max_frame_bytes}-byte limit")
    return _HEADER.pack(len(payload)) + payload


def _decode_frame(payload):
    try:
        frame = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _FramingError(f"RPC peer sent an invalid JSON frame: {exc}") from exc
    if not isinstance(frame, dict):
        raise _FramingError("RPC frame must be a JSON object")
    return frame


def _error_from_exception(exc):
    """Convert any handler exception into the wire error object, preserving its type."""
    try:
        message = str(exc)
    except Exception:
        message = ""
    try:
        retryable = bool(getattr(exc, "retryable", False))
    except Exception:
        retryable = False
    return {"type": type(exc).__name__, "message": message, "retryable": retryable}


def _timeout_message(exc):
    return str(exc) or _TIMEOUT_MESSAGE


class RpcServer:
    """Concurrent, token-authenticated RPC server for runner/control-plane traffic."""

    def __init__(self, socket_path, tokens, handler, *, max_frame_bytes=MAX_FRAME_BYTES, backlog=64):
        self.socket_path = Path(socket_path)
        self.tokens = dict(tokens)
        self.handler = handler
        self.max_frame_bytes = int(max_frame_bytes)
        self.backlog = int(backlog)
        self.server = None
        self._started = False
        self._closed = False
        self._closing = False
        self._connections = set()
        self._writers = set()
        self._handlers = {}
        self._agent_handlers = {}
        self._socket_stat = None

    async def start(self):
        if self._started:
            raise RuntimeError("RPC server has already been started")
        path = self.socket_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise ValueError(f"RPC socket path is occupied by a non-socket: {path}")
            path.unlink()
        self.server = await asyncio.start_unix_server(
            self._handle_connection, str(path), backlog=self.backlog, limit=self.max_frame_bytes + _HEADER_BYTES)
        os.chmod(path, 0o600)
        self._socket_stat = path.lstat()
        self._started = True

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._closing = True
        server, self.server = self.server, None
        if server is not None:
            # Stop accepting new connections first, but do not wait for active
            # transports yet: Python 3.12 makes wait_closed() wait for every
            # accepted connection, so a long in-flight handler would wedge
            # Supervisor shutdown. Cancel tracked connections/handlers below,
            # then wait.
            server.close()
        connections = list(self._connections)
        handlers = list(self._handlers)
        self._connections.clear()
        for task in handlers:
            task.cancel()
        for task in connections:
            task.cancel()
        if connections or handlers:
            await asyncio.gather(*connections, *handlers, return_exceptions=True)
        self._handlers.clear()
        self._agent_handlers.clear()
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        if server is not None:
            try:
                # Bounded: shutdown must never depend on a peer transport or a
                # future asyncio semantic change.
                await asyncio.wait_for(server.wait_closed(), 5)
            except Exception:
                pass
        self._unlink_socket()

    def _unlink_socket(self):
        path, saved = self.socket_path, self._socket_stat
        self._socket_stat = None
        if saved is None:
            return
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        try:
            if current.st_dev == saved.st_dev and current.st_ino == saved.st_ino:
                path.unlink()
        except FileNotFoundError:
            pass

    async def cancel_agent(self, agent_id):
        """Cancel every in-flight handler bound to ``agent_id``; return the count cancelled."""
        tasks = [task for task in self._agent_handlers.get(agent_id, ()) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    # -- connection lifecycle -------------------------------------------------

    async def _handle_connection(self, reader, writer):
        task = asyncio.current_task()
        self._connections.add(task)
        self._writers.add(writer)
        try:
            try:
                hello = await self._read_frame(reader)
            except (_FramingError, asyncio.IncompleteReadError, ConnectionError):
                return
            identity = self._authenticate(hello)
            if identity is _INVALID_HANDSHAKE:
                return
            if identity is None:
                await self._try_send_error(writer, 0, "PermissionError", "authentication failed")
                return
            agent_id, role = identity
            await self._send(writer, {"id": 0, "ok": True, "result": {"agent_id": agent_id, "role": role}})
            try:
                frame = await self._read_frame(reader)
                request_id, method, params = self._validate_request(frame)
            except (_FramingError, asyncio.IncompleteReadError, ConnectionError):
                return
            await self._serve_request(reader, writer, agent_id, role, request_id, method, params)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Peer-specific failures must never take down the accept loop or other runners.
            return
        finally:
            self._connections.discard(task)
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _authenticate(self, hello):
        if hello.get("id") != 0 or hello.get("method") != "hello":
            return _INVALID_HANDSHAKE
        params = hello.get("params")
        if not isinstance(params, dict):
            return _INVALID_HANDSHAKE
        token = params.get("token")
        if not isinstance(token, str):
            return None
        binding = self.tokens.get(token)
        if not isinstance(binding, dict):
            return None
        agent_id, role = binding.get("agent_id"), binding.get("role")
        if not isinstance(agent_id, str) or role not in _ROLES:
            return None
        return agent_id, role

    @staticmethod
    def _validate_request(frame):
        request_id = frame.get("id")
        if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id < 1:
            raise _FramingError("request frame id must be an integer >= 1")
        method = frame.get("method")
        if not isinstance(method, str) or not method:
            raise _FramingError("request frame method must be a non-empty string")
        params = frame.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise _FramingError("request frame params must be a JSON object")
        return request_id, method, params

    async def _serve_request(self, reader, writer, agent_id, role, request_id, method, params):
        handler_task = asyncio.create_task(self.handler(agent_id, role, method, params),
                                           name=f"rpc-handler:{agent_id}:{method}")
        self._handlers[handler_task] = (agent_id, writer, request_id)
        self._agent_handlers.setdefault(agent_id, set()).add(handler_task)
        watcher = asyncio.create_task(self._watch_client(reader), name=f"rpc-watch:{agent_id}")
        try:
            try:
                await asyncio.wait((handler_task, watcher), return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                handler_task.cancel()
                watcher.cancel()
                await asyncio.gather(handler_task, watcher, return_exceptions=True)
                raise
            if watcher.done():
                # EOF means the client went away; any byte means a pipelined frame (protocol
                # error). Either way the in-flight handler must not keep running.
                handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
                return
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if handler_task.cancelled():
                if not self._closing:
                    await self._try_send_error(writer, request_id, "CancelledError", "RPC handler was cancelled", retryable=True)
                return
            error = handler_task.exception()
            if error is not None:
                payload = _error_from_exception(error)
                await self._try_send_error(writer, request_id, payload["type"], payload["message"], payload["retryable"])
                return
            await self._send_result(writer, request_id, handler_task.result())
        finally:
            self._handlers.pop(handler_task, None)
            scoped = self._agent_handlers.get(agent_id)
            if scoped is not None:
                scoped.discard(handler_task)
                if not scoped:
                    self._agent_handlers.pop(agent_id, None)

    @staticmethod
    async def _watch_client(reader):
        try:
            return await reader.read(1)
        except (ConnectionError, OSError):
            return b""

    async def _read_frame(self, reader):
        header = await reader.readexactly(_HEADER_BYTES)
        (length,) = _HEADER.unpack(header)
        if length > self.max_frame_bytes:
            raise _FramingError(f"RPC frame of {length} bytes exceeds the {self.max_frame_bytes}-byte limit")
        payload = await reader.readexactly(length)
        return _decode_frame(payload)

    async def _send(self, writer, frame):
        writer.write(_encode_frame(frame, self.max_frame_bytes))
        await writer.drain()

    async def _send_result(self, writer, request_id, result):
        try:
            await self._send(writer, {"id": request_id, "ok": True, "result": result})
        except _FramingError as exc:
            await self._try_send_error(writer, request_id, "ValueError", str(exc))

    async def _try_send_error(self, writer, request_id, error_type, message, retryable=False):
        if writer.is_closing():
            return
        frame = {"id": request_id, "ok": False,
                 "error": {"type": error_type, "message": message, "retryable": bool(retryable)}}
        try:
            await self._send(writer, frame)
        except (ConnectionError, OSError, _FramingError):
            pass


class RpcClient:
    """Per-process RPC client; every call uses a fresh authenticated connection."""

    def __init__(self, socket_path, token, agent_id, role, *, max_frame_bytes=MAX_FRAME_BYTES,
                 connect_timeout_s=10.0, default_call_timeout_s=None):
        self.socket_path = Path(socket_path)
        self.token = token
        self.agent_id = agent_id
        self.role = role
        self.max_frame_bytes = int(max_frame_bytes)
        self.connect_timeout_s = float(connect_timeout_s)
        if self.connect_timeout_s <= 0:
            raise ValueError("connect_timeout_s must be positive")
        if default_call_timeout_s is None:
            default_call_timeout_s = DEFAULT_CALL_TIMEOUT_S
        if (isinstance(default_call_timeout_s, bool)
                or not isinstance(default_call_timeout_s, (int, float))
                or not 0 < float(default_call_timeout_s) <= 3600):
            raise ValueError("default_call_timeout_s must be a positive number <= 3600")
        self.default_call_timeout_s = float(default_call_timeout_s)
        self._request_ids = itertools.count(1)
        self._read_limit = self.max_frame_bytes + _HEADER_BYTES
        self._hello = {"id": 0, "method": "hello",
                       "params": {"token": token, "agent_id": agent_id, "role": role}}

    def _call_timeout(self, method, timeout_s):
        """Explicit timeout wins; long-running game calls are exempt from the default."""
        if timeout_s is not None:
            return timeout_s
        if method in UNBOUNDED_METHODS:
            return None
        return self.default_call_timeout_s

    # -- synchronous client ---------------------------------------------------

    def call_sync(self, method, params=None, *, timeout_s=None):
        request_id = next(self._request_ids)
        request = {"id": request_id, "method": method, "params": {} if params is None else params}
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            raise RpcError(type(exc).__name__, str(exc), retryable=isinstance(exc, ConnectionError)) from exc
        try:
            try:
                sock.settimeout(self.connect_timeout_s)
                sock.connect(str(self.socket_path))
                self._sync_send(sock, self._hello)
                self._check_handshake(self._sync_receive(sock))
                sock.settimeout(self._call_timeout(method, timeout_s))
                self._sync_send(sock, request)
                return self._unwrap(self._sync_receive(sock), request_id)
            except RpcError:
                raise
            except _TransportClosed as exc:
                raise RpcError("ConnectionError", str(exc), retryable=True) from exc
            except _FramingError as exc:
                raise RpcError("ProtocolError", str(exc), retryable=exc.retryable) from exc
            except TimeoutError as exc:
                raise RpcError("TimeoutError", _timeout_message(exc), retryable=True) from exc
            except OSError as exc:
                raise RpcError(type(exc).__name__, str(exc), retryable=isinstance(exc, ConnectionError)) from exc
        finally:
            sock.close()

    def _sync_send(self, sock, frame):
        sock.sendall(_encode_frame(frame, self.max_frame_bytes))

    def _sync_receive(self, sock):
        header = self._sync_read(sock, _HEADER_BYTES)
        (length,) = _HEADER.unpack(header)
        if length > self.max_frame_bytes:
            raise _FramingError(f"RPC frame of {length} bytes exceeds the {self.max_frame_bytes}-byte limit")
        return _decode_frame(self._sync_read(sock, length))

    @staticmethod
    def _sync_read(sock, size):
        chunks = []
        while size:
            # Bound each recv buffer; the socket layer allocates the full requested size.
            chunk = sock.recv(min(size, 1024 * 1024))
            if not chunk:
                raise _TransportClosed("control-plane closed the RPC connection before the response was complete")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    # -- asynchronous client --------------------------------------------------

    async def call(self, method, params=None, *, timeout_s=None):
        request_id = next(self._request_ids)
        request = {"id": request_id, "method": method, "params": {} if params is None else params}
        writer = None
        try:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(self.socket_path), limit=self._read_limit),
                    timeout=self.connect_timeout_s)
            except TimeoutError as exc:
                raise RpcError("TimeoutError", f"timed out connecting to RPC socket {self.socket_path}", retryable=True) from exc
            async with asyncio.timeout(self.connect_timeout_s):
                await self._async_send(writer, self._hello)
                self._check_handshake(await self._async_receive(reader))
            effective_timeout = self._call_timeout(method, timeout_s)
            if effective_timeout is None:
                await self._async_send(writer, request)
                response = await self._async_receive(reader)
            else:
                async with asyncio.timeout(effective_timeout):
                    await self._async_send(writer, request)
                    response = await self._async_receive(reader)
            return self._unwrap(response, request_id)
        except RpcError:
            raise
        except _TransportClosed as exc:
            raise RpcError("ConnectionError", str(exc), retryable=True) from exc
        except _FramingError as exc:
            raise RpcError("ProtocolError", str(exc), retryable=exc.retryable) from exc
        except TimeoutError as exc:
            raise RpcError("TimeoutError", _timeout_message(exc), retryable=True) from exc
        except OSError as exc:
            raise RpcError(type(exc).__name__, str(exc), retryable=isinstance(exc, ConnectionError)) from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def _async_send(self, writer, frame):
        writer.write(_encode_frame(frame, self.max_frame_bytes))
        await writer.drain()

    async def _async_receive(self, reader):
        try:
            header = await reader.readexactly(_HEADER_BYTES)
            (length,) = _HEADER.unpack(header)
            if length > self.max_frame_bytes:
                raise _FramingError(f"RPC frame of {length} bytes exceeds the {self.max_frame_bytes}-byte limit")
            payload = await reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise _TransportClosed("control-plane closed the RPC connection before the response was complete") from exc
        return _decode_frame(payload)

    # -- response validation --------------------------------------------------

    def _check_handshake(self, response):
        if response.get("id") != 0:
            raise _FramingError("handshake response id must be 0")
        if response.get("ok") is not True:
            if response.get("ok") is False:
                raise self._error_from_frame(response)
            raise _FramingError("handshake response is missing a boolean ok field")
        result = response.get("result")
        if not isinstance(result, dict):
            raise _FramingError("handshake response is missing the bound identity")
        return result

    def _unwrap(self, response, request_id):
        if response.get("id") != request_id:
            raise _FramingError(f"response id {response.get('id')!r} does not match request id {request_id!r}")
        if response.get("ok") is True:
            return response.get("result")
        if response.get("ok") is False:
            raise self._error_from_frame(response)
        raise _FramingError("response is missing a boolean ok field")

    @staticmethod
    def _error_from_frame(response):
        error = response.get("error")
        if not isinstance(error, dict):
            raise _FramingError("error response is missing its error object")
        error_type = error.get("type")
        message = error.get("message")
        return RpcError(error_type if isinstance(error_type, str) and error_type else "RuntimeError",
                        "" if message is None else str(message),
                        retryable=bool(error.get("retryable", False)))
