"""Local OpenAI-compatible model proxy used by the Pi agent runtime.

The Pi coding-agent CLI is pointed at this proxy through a generated
``models.json`` custom provider.  For every chat-completion attempt the proxy:

1. asks the control plane for budget admission (``budget.wait_admission``);
2. validates and reserves one model call (``budget.validate_request`` /
   ``budget.reserve`` with ``purpose``/``role``);
3. records a durable ``model_request`` ledger event plus a bounded request
   artifact;
4. forwards the request to the real upstream ``/chat/completions`` endpoint and
   relays the SSE/JSON bytes to Pi unchanged;
5. settles the reservation with the upstream usage (conservative unknown usage
   when the stream failed or reported none) and records ``model_response`` plus
   a bounded response artifact.

All control-plane traffic uses the runner's synchronous RPC client from the
HTTP server's worker threads.  The proxy binds only to a randomized
``127.0.0.1`` port, never logs request/response bodies or credentials, and never
forwards its own upstream credential to Pi.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import select
import socket
import socketserver
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

# Conservative unknown-usage marker: the budget charges the admission-time
# reservation when token usage cannot be established.
UNKNOWN_USAGE = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
# A provider that returns an HTTP error before producing a completion has not
# generated billable output. Settle zero (rather than the admission-time
# worst-case reservation) so a retry loop cannot drain the budget as "unknown".
ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
# The pause gate can stay closed for hours. A proxy handler that waits forever
# keeps a disconnected Pi request alive and can spend a model call after resume;
# bound the wait and abort the attempt instead.
ADMISSION_WAIT_TIMEOUT_S = 30.0

MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
MAX_REQUEST_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_CAPTURE_CHARS = 512 * 1024
DEFAULT_TIMEOUT_S = 90.0
DEFAULT_ARTIFACT_MAX_BYTES = 4_000_000_000
RETAINED_METADATA_CHARS = 512
RETENTION_PRESSURE_THRESHOLD = 0.9
BUDGET_STATE_SCHEMA_VERSION = 1
BUDGET_STATE_SAVE_INTERVAL_S = 0.5

# Host-side egress allowlist for restricted Pi runner containers (design A from
# AUDIT_2026-10-23_POST_FIXES.md P1-3). The Supervisor runs this forwarder over a
# bind-mounted Unix domain socket; the runner container has no network at all and
# only the runner-owned PiModelProxy (which meters budget/ledger) can reach the
# provider. The provider credential lives only in the Supervisor environment.
ENV_MODEL_FORWARDER_TOKEN = "HARNESS_MODEL_FORWARDER_TOKEN"
FORWARDER_TOKEN_HEADER = "x-harness-model-forwarder-token"
MODEL_FORWARDER_SOCKET_NAME = "socket"
MODEL_FORWARDER_CONTAINER_DIRECTORY = "/run/mc-model"
MODEL_FORWARDER_CONTAINER_SOCKET = (
    MODEL_FORWARDER_CONTAINER_DIRECTORY + "/" + MODEL_FORWARDER_SOCKET_NAME)
MODEL_FORWARDER_ALLOWED_PATH = "/v1/chat/completions"
MODEL_FORWARDER_ENDPOINT = "http://model-forwarder" + MODEL_FORWARDER_ALLOWED_PATH
MODEL_FORWARDER_MAX_REQUEST_BYTES = 4_000_000
MODEL_FORWARDER_MAX_RESPONSE_BYTES = 2_000_000


class ProxyInfraError(RuntimeError):
    """A control-plane dependency failed before the upstream could be called."""


def _bounded(value: Any, limit: int) -> tuple[str, bool]:
    """Serialize bounded UTF-8 text with an explicit truncation marker."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    marker = "\n[model proxy: payload truncated at the durable event limit]"
    keep = max(0, limit - len(marker.encode("utf-8")))
    truncated = encoded[:keep].decode("utf-8", errors="ignore")
    return truncated + marker, True


def _is_loopback_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


class ArtifactBudget:
    """Run-wide (restart-safe) byte budget for the Pi runner's artifacts.

    Request, response, tool-result and compaction artifacts all draw from the
    same cap. Once the cap would be exceeded, call sites store a small
    metadata-only projection instead of the full content. Metadata projections
    are charged too, so the ledger cannot grow past the cap through fallbacks.

    Usage is persisted atomically in the runner state directory so a runner
    restart never resets the cap (missing marker = fresh run; a corrupt marker is
    treated conservatively as a full budget, i.e. metadata-only from then on).
    A one-time ``pi_retention_pressure`` event fires at >=90% of the cap.
    """

    def __init__(self, max_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES, *,
                 event: Any = None,
                 state_path: Path | str | None = None,
                 save_interval_s: float = BUDGET_STATE_SAVE_INTERVAL_S,
                 rpc_client: Any = None,
                 agent_id: str | None = None) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self._event = event
        self._state_path = Path(state_path) if state_path is not None else None
        self._save_interval_s = max(0.0, float(save_interval_s))
        # When a control-plane RPC client is supplied the authoritative
        # accounting lives in ServiceHub (pi-budget.sqlite3, not mounted into the
        # runner); the local JSON marker is then never read or trusted, so a
        # model-writable marker cannot reset usage after a restart.
        self._rpc_client = rpc_client
        self._agent_id = agent_id
        self._rpc_errors = 0
        self._central_global_used = 0
        self._central_global_limit = 0
        self._lock = threading.Lock()
        self._used = 0
        self._retained = 0
        self._metadata_bytes = 0
        self._retention_recorded = False
        self._pressure_recorded = False
        self._dirty = False
        self._last_flush = 0.0
        self.state_status = "fresh"
        self._load()

    # -- control-plane (trusted) accounting -----------------------------
    @property
    def central(self) -> bool:
        return self._rpc_client is not None

    def _central_call(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        client = self._rpc_client
        if client is None:
            return None
        try:
            try:
                response = client.call_sync(method, params, timeout_s=30)
            except TypeError:
                response = client.call_sync(method, params)
        except Exception:
            self._central_failure()
            return None
        if not isinstance(response, dict):
            self._central_failure()
            return None
        return response

    def _central_failure(self) -> None:
        # Fail closed: without trusted accounting we cannot know what was already
        # stored, so behave like a full budget (metadata-only) from now on.
        with self._lock:
            self._rpc_errors += 1
            self._used = self.max_bytes
            self._pressure_recorded = True
            self.state_status = "conservative_central_unavailable"

    def _sync_from_central(self, response: dict[str, Any]) -> None:
        with self._lock:
            try:
                self._used = max(0, int(response.get("used_bytes", self._used)))
                self._metadata_bytes = max(0, int(response.get("metadata_bytes",
                                                               self._metadata_bytes)))
                self._retained = max(0, int(response.get("retained_count",
                                                         self._retained)))
                limit = response.get("limit_bytes")
                if type(limit) is int and limit >= 0:
                    # The control plane's configured cap is authoritative if it
                    # changed across a restart.
                    self.max_bytes = limit
            except (TypeError, ValueError):
                self._central_failure()
                return
            self._pressure_recorded = bool(
                response.get("pressure_recorded", self._pressure_recorded))
            retention_recorded = response.get("retention_recorded")
            if isinstance(retention_recorded, bool):
                self._retention_recorded = retention_recorded
            self._central_global_used = max(
                0, int(response.get("global_used_bytes") or 0))
            self._central_global_limit = max(
                0, int(response.get("global_limit_bytes") or 0))
            self.state_status = "central"

    def _load_central(self) -> None:
        response = self._central_call("pi.budget.snapshot", {})
        if response is None:
            return
        row = response.get("self")
        limits = response.get("limits")
        global_usage = response.get("global")
        merged: dict[str, Any] = dict(row) if isinstance(row, dict) else {}
        if isinstance(limits, dict):
            merged.setdefault("limit_bytes", limits.get("per_agent_bytes"))
        if isinstance(global_usage, dict):
            merged.setdefault("global_used_bytes", global_usage.get("used_bytes"))
            merged.setdefault("global_limit_bytes", global_usage.get("limit_bytes"))
        self._sync_from_central(merged)

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        if self._rpc_client is not None:
            self._load_central()
            return
        path = self._state_path
        if path is None:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.state_status = "fresh"
            return
        except OSError:
            self._conservative_state("unreadable")
            return
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            self._conservative_state("corrupt")
            return
        if (not isinstance(document, dict)
                or document.get("schema_version") != BUDGET_STATE_SCHEMA_VERSION
                or type(document.get("used_bytes")) is not int
                or document["used_bytes"] < 0):
            self._conservative_state("corrupt")
            return
        limit = document.get("limit_bytes")
        used = document["used_bytes"]
        if type(limit) is int and limit != self.max_bytes:
            # A cap change across restarts must not credit the old usage away.
            self.state_status = "limit_changed"
        # Metadata projections are charged and may legitimately push usage
        # past the full-artifact cap; preserve the exact persisted total.
        self._used = used
        self._retained = max(0, int(document.get("retained_count") or 0))
        self._metadata_bytes = max(0, int(document.get("metadata_bytes") or 0))
        self._retention_recorded = bool(document.get("retention_recorded"))
        self._pressure_recorded = bool(document.get("pressure_recorded"))
        if self._pressure_recorded and self._used < self.max_bytes:
            pass
        self.state_status = "restored"

    def _conservative_state(self, reason: str) -> None:
        # Fail-safe: a damaged marker means we cannot know how much was already
        # stored, so stop storing full artifacts for this process.
        self._used = self.max_bytes
        self._retention_recorded = True
        self._pressure_recorded = True
        self.state_status = "conservative_" + reason

    def _state_document(self) -> dict[str, Any]:
        return {"schema_version": BUDGET_STATE_SCHEMA_VERSION,
                "limit_bytes": self.max_bytes,
                "used_bytes": self._used,
                "retained_count": self._retained,
                "metadata_bytes": self._metadata_bytes,
                "retention_recorded": self._retention_recorded,
                "pressure_recorded": self._pressure_recorded}

    def _save_locked(self) -> None:
        path = self._state_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            import tempfile

            descriptor, temporary = tempfile.mkstemp(
                prefix=".pi-artifact-budget-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(self._state_document(), handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self._dirty = False
            self._last_flush = time.monotonic()
        except OSError:
            # The state marker is a best-effort durability aid; accounting in
            # memory must keep working even if the disk is unavailable.
            return

    def _maybe_flush_locked(self) -> None:
        if not self._dirty or self._state_path is None:
            return
        now = time.monotonic()
        if self._last_flush == 0.0 or now - self._last_flush >= self._save_interval_s:
            self._save_locked()

    def flush(self) -> None:
        """Force-persist accounting (called from heartbeat/finished/shutdown).

        In control-plane mode the SQLite authority is already durable, so the
        model-writable local marker is neither written nor consulted.
        """
        if self._rpc_client is not None:
            return
        with self._lock:
            if self._dirty:
                self._save_locked()

    # -- accounting -----------------------------------------------------
    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used

    @property
    def metadata_bytes(self) -> int:
        with self._lock:
            return self._metadata_bytes

    @property
    def retained_count(self) -> int:
        with self._lock:
            return self._retained

    def snapshot(self) -> dict[str, Any]:
        if self._rpc_client is not None:
            response = self._central_call("pi.budget.snapshot", {})
            if response is not None:
                row = response.get("self")
                limits = response.get("limits")
                global_usage = response.get("global")
                merged: dict[str, Any] = dict(row) if isinstance(row, dict) else {}
                if isinstance(limits, dict):
                    merged.setdefault("limit_bytes", limits.get("per_agent_bytes"))
                if isinstance(global_usage, dict):
                    merged.setdefault("global_used_bytes", global_usage.get("used_bytes"))
                    merged.setdefault("global_limit_bytes", global_usage.get("limit_bytes"))
                self._sync_from_central(merged)
        with self._lock:
            result = {"limit_bytes": self.max_bytes, "used_bytes": self._used,
                      "retained_count": self._retained,
                      "metadata_bytes": self._metadata_bytes,
                      "pressure_recorded": self._pressure_recorded,
                      "retention": "metadata_only" if self._retained else "full",
                      "state": self.state_status}
            if self._rpc_client is None:
                self._maybe_flush_locked()
            else:
                result["global_used_bytes"] = self._central_global_used
                result["global_limit_bytes"] = self._central_global_limit
            return result

    def _maybe_pressure_locked(self) -> dict[str, Any] | None:
        if (not self._pressure_recorded and self.max_bytes > 0
                and self._used >= self.max_bytes * RETENTION_PRESSURE_THRESHOLD):
            self._pressure_recorded = True
            percent = round(100.0 * self._used / self.max_bytes, 2)
            return {"event": "pi_retention_pressure", "kind": "artifact",
                    "reason": "pi_artifact_max_bytes_pressure",
                    "used_bytes": self._used, "limit_bytes": self.max_bytes,
                    "percent": percent,
                    "threshold": RETENTION_PRESSURE_THRESHOLD}
        return None

    def _emit(self, payload: dict[str, Any] | None) -> None:
        if payload is not None and self._event is not None:
            try:
                self._event(payload)
            except Exception:
                pass

    def take(self, size: int) -> bool:
        """Account a full artifact of ``size`` bytes when the cap allows it."""
        size = max(0, int(size))
        if self._rpc_client is not None:
            response = self._central_call("pi.budget.take", {"bytes": size})
            if response is None:
                return False
            self._sync_from_central(response)
            self._emit(response.get("pressure") or response.get("retention"))
            return bool(response.get("allowed"))
        payload = None
        with self._lock:
            if self._used + size <= self.max_bytes:
                self._used += size
                self._dirty = True
                payload = self._maybe_pressure_locked()
                self._maybe_flush_locked()
                return_ok = True
            else:
                self._retained += 1
                self._dirty = True
                if not self._retention_recorded:
                    self._retention_recorded = True
                    payload = {"event": "pi_artifact_retention",
                               "reason": "pi_artifact_max_bytes",
                               "limit_bytes": self.max_bytes,
                               "used_bytes": self._used,
                               "retained_count": 1}
                self._maybe_flush_locked()
                return_ok = False
        self._emit(payload)
        return return_ok

    def charge_metadata(self, size: int) -> None:
        """Charge stored metadata-projection bytes against the same budget."""
        size = max(0, int(size))
        if self._rpc_client is not None:
            response = self._central_call("pi.budget.charge_metadata", {"bytes": size})
            if response is not None:
                self._sync_from_central(response)
                self._emit(response.get("pressure") or response.get("retention"))
            return
        payload = None
        with self._lock:
            self._used += size
            self._metadata_bytes += size
            self._dirty = True
            payload = self._maybe_pressure_locked()
            self._maybe_flush_locked()
        self._emit(payload)

    @staticmethod
    def metadata_text(kind: str, text: str) -> str:
        encoded = text.encode("utf-8", errors="replace")
        head = text[:RETAINED_METADATA_CHARS]
        tail = text[-RETAINED_METADATA_CHARS:]
        projection = {
            "retention": "metadata_only",
            "reason": "pi_artifact_max_bytes",
            "kind": str(kind)[:100],
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
            "full_text_omitted": True,
            "head": head,
            "tail": tail,
        }
        return json.dumps(projection, ensure_ascii=False)


def budgeted_artifact_text(budget: ArtifactBudget, kind: str, text: str) -> str:
    """Return full artifact text while the shared budget allows, else metadata.

    Metadata-only fallbacks are themselves charged so repeated fallbacks cannot
    grow storage without bound.
    """
    if budget.take(len(text.encode("utf-8", errors="replace"))):
        return text
    metadata = budget.metadata_text(kind, text)
    budget.charge_metadata(len(metadata.encode("utf-8", errors="replace")))
    return metadata


def _upstream_chat_url(base_url: str) -> str:
    parsed = urlsplit(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProxyInfraError("model base_url must be an http(s) endpoint")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _usage_from_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
        return dict(payload["usage"])
    return None


class _SSECapture:
    """Incrementally parse OpenAI SSE chunks while the raw bytes are relayed.

    Only bounded text/tool-call metadata are retained for the response artifact;
    raw bytes go to Pi unchanged.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self.usage: dict[str, Any] | None = None
        self.model: str | None = None
        self.finish_reason: str | None = None
        self.chunks = 0
        self.truncated = False
        self.done = False
        self._content: list[str] = []
        self._content_chars = 0
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._tool_call_chars = 0

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            line, self._buffer = self._buffer[:index], self._buffer[index + 1:]
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == b"[DONE]":
                self.done = True
                continue
            try:
                record = json.loads(data)
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            self.chunks += 1
            if isinstance(record.get("usage"), dict):
                self.usage = dict(record["usage"])
            model = record.get("model")
            if isinstance(model, str) and model:
                self.model = model
            choices = record.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                reason = choice.get("finish_reason")
                if isinstance(reason, str):
                    self.finish_reason = reason
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str) and content and not self.truncated:
                    self._content.append(content)
                    self._content_chars += len(content)
                    if self._content_chars > MAX_CAPTURE_CHARS:
                        self.truncated = True
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for call in tool_calls:
                        if not isinstance(call, dict):
                            continue
                        index = call.get("index")
                        if not isinstance(index, int):
                            index = len(self._tool_calls)
                        target = self._tool_calls.setdefault(
                            index, {"id": None, "type": "function",
                                    "function": {"name": None, "arguments": ""}})
                        if isinstance(call.get("id"), str):
                            target["id"] = call["id"]
                        function = call.get("function")
                        if isinstance(function, dict):
                            if isinstance(function.get("name"), str):
                                target["function"]["name"] = function["name"]
                            arguments = function.get("arguments")
                            if isinstance(arguments, str):
                                if self._tool_call_chars <= MAX_CAPTURE_CHARS:
                                    target["function"]["arguments"] += arguments
                                    self._tool_call_chars += len(arguments)
                                else:
                                    self.truncated = True

    def document(self) -> dict[str, Any]:
        content = "".join(self._content)
        if len(content) > MAX_CAPTURE_CHARS:
            content = content[:MAX_CAPTURE_CHARS]
            self.truncated = True
        return {
            "content": content,
            "tool_calls": [self._tool_calls[index] for index in sorted(self._tool_calls)],
            "finish_reason": self.finish_reason,
            "model": self.model,
            "chunks": self.chunks,
            "truncated": self.truncated,
        }


class PiModelProxy:
    """The loopback OpenAI-compatible proxy for one Pi runner process."""

    def __init__(self, model: dict[str, Any], rpc_client: Any, *,
                 agent_id: str, role: str = "player",
                 purpose: str = "agent_decision",
                 host: str = "127.0.0.1",
                 transport: httpx.BaseTransport | None = None,
                 artifact_budget: ArtifactBudget | None = None) -> None:
        if not isinstance(model, dict):
            raise ValueError("Pi model config must be an object")
        self.model = model
        self.model_id = str(model.get("model") or "")
        if not self.model_id:
            raise ValueError("Pi model config needs a model id")
        forwarder_socket = model.get("model_forwarder_socket")
        if forwarder_socket is not None:
            if not isinstance(forwarder_socket, str) or not forwarder_socket:
                raise ValueError("Pi model forwarder socket must be a path")
            token = os.environ.get(ENV_MODEL_FORWARDER_TOKEN)
            if not isinstance(token, str) or not token:
                raise ProxyInfraError(
                    "model forwarder token is missing from the runner environment")
            self.forwarder_socket = forwarder_socket
            self.forwarder_token = token
            self.upstream_url = MODEL_FORWARDER_ENDPOINT
            self._transport = (transport if transport is not None
                               else httpx.HTTPTransport(uds=forwarder_socket))
        else:
            self.forwarder_socket = None
            self.forwarder_token = None
            self.upstream_url = _upstream_chat_url(str(model.get("base_url") or ""))
        self.rpc = rpc_client
        self.agent_id = agent_id
        self.role = role if role in {"player", "observer"} else "player"
        self.purpose = purpose if purpose in {"agent_decision", "observer_review",
                                              "context_compaction"} else "agent_decision"
        self.host = host
        if self.forwarder_socket is None:
            self._transport = transport
        timeout = model.get("timeout_s", DEFAULT_TIMEOUT_S)
        try:
            self.timeout_s = max(1.0, float(timeout))
        except (TypeError, ValueError):
            self.timeout_s = DEFAULT_TIMEOUT_S
        self.api_key_env = model.get("api_key_env")
        self.artifact_budget = (artifact_budget if artifact_budget is not None
                                else ArtifactBudget())
        self._lock = threading.Lock()
        self._request_count = 0
        self.denials = 0
        self.upstream_errors = 0
        self.consecutive_upstream_errors = 0
        self.fatal_upstream_error = False
        self.last_upstream_status: int | None = None
        self.ledger_errors = 0
        self.closed = False
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    # -- lifecycle ------------------------------------------------------
    def start(self) -> str:
        """Bind on 127.0.0.1 with an OS-assigned port and serve in a thread."""
        if self._server is not None:
            return self.base_url
        handler_class = type("BoundProxyHandler", (_ProxyHandler,), {"proxy": self})
        server = ThreadingHTTPServer((self.host, 0), handler_class)
        server.daemon_threads = True
        port = int(server.server_address[1])
        self._server = server
        self.base_url = f"http://{self.host}:{port}/v1"
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.1},
                                  name="pi-model-proxy", daemon=True)
        self._thread = thread
        thread.start()
        return self.base_url

    def stop(self) -> None:
        self.closed = True
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def __enter__(self) -> "PiModelProxy":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.stop()
        return False

    # -- durable metering ----------------------------------------------
    def _call(self, method: str, params: dict[str, Any] | None = None,
              *, timeout_s: float | None = None) -> Any:
        if timeout_s is None:
            return self.rpc.call_sync(method, params if params is not None else {})
        return self.rpc.call_sync(method, params if params is not None else {},
                                  timeout_s=timeout_s)

    def _append(self, kind: str, payload: dict[str, Any]) -> None:
        self._call("recorder.append", {"kind": kind, "payload": payload,
                                       "kwargs": {"actor_id": self.agent_id}})

    def _put_artifact(self, value: Any, limit: int,
                      kind: str = "artifact") -> dict[str, Any] | None:
        text, _truncated = _bounded(value, limit)
        text = budgeted_artifact_text(self.artifact_budget, kind, text)
        result = self._call("recorder.put_artifact", {"text": text})
        return result if isinstance(result, dict) else None

    def _next_turn(self) -> int:
        with self._lock:
            self._request_count += 1
            return self._request_count

    def _note_denial(self) -> None:
        with self._lock:
            self.denials += 1

    def _note_upstream_error(self, status: int | None = None) -> None:
        with self._lock:
            self.upstream_errors += 1
            self.consecutive_upstream_errors += 1
            if status is not None:
                self.last_upstream_status = status
                # Authentication/authorization and malformed-request errors
                # cannot be fixed by Pi retrying or continuing the session.
                if 400 <= status < 500 and status not in {408, 409, 425, 429}:
                    self.fatal_upstream_error = True

    def _note_upstream_success(self) -> None:
        with self._lock:
            self.consecutive_upstream_errors = 0
            self.last_upstream_status = None

    @staticmethod
    def _client_alive(handler: "_ProxyHandler") -> bool:
        """Best-effort detection of a Pi client that disconnected while paused."""
        connection = getattr(handler, "connection", None)
        if connection is None:
            return False
        try:
            readable, _, _ = select.select([connection], [], [], 0)
            if not readable:
                return True
            return bool(connection.recv(1, socket.MSG_PEEK))
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            return False

    def _note_ledger_error(self) -> None:
        with self._lock:
            self.ledger_errors += 1

    # -- HTTP handling --------------------------------------------------
    def _handle_get(self, handler: "_ProxyHandler") -> None:
        path = urlsplit(handler.path).path.rstrip("/")
        if path == "/v1/models":
            handler._send_json(200, {"object": "list", "data": [{
                "id": self.model_id, "object": "model", "owned_by": "mcproxy",
                "created": int(time.time())}]})
            return
        handler._send_json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def _handle_post(self, handler: "_ProxyHandler") -> None:
        path = urlsplit(handler.path).path.rstrip("/")
        if path != "/v1/chat/completions":
            handler._send_json(404, {"error": {"message": "not found",
                                               "type": "invalid_request_error"}})
            return
        try:
            length = int(handler.headers.get("content-length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > MAX_REQUEST_BODY_BYTES:
            handler._send_json(413, {"error": {"message": "request body size is invalid",
                                               "type": "invalid_request_error"}})
            return
        raw = handler.rfile.read(length)
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            handler._send_json(400, {"error": {"message": "request body is not valid JSON",
                                               "type": "invalid_request_error"}})
            return
        if not isinstance(body, dict):
            handler._send_json(400, {"error": {"message": "request body must be an object",
                                               "type": "invalid_request_error"}})
            return
        try:
            self._serve_chat(handler, body, raw)
        except (BrokenPipeError, ConnectionResetError):
            # Pi went away mid-stream; the reservation is settled by the inner
            # finally block and no response can be delivered.
            return

    # -- one model attempt ----------------------------------------------
    def _serve_chat(self, handler: "_ProxyHandler", body: dict[str, Any],
                    raw: bytes) -> None:
        # 1. Admission gate: paused/stopped runs and exhausted budgets never
        #    reach the upstream model. The wait is bounded and the client is
        #    re-checked afterwards so a disconnected request that was queued
        #    behind a long pause cannot spend a model call after resume.
        if not self._client_alive(handler):
            return
        try:
            admission = self._call("budget.wait_admission",
                                   timeout_s=ADMISSION_WAIT_TIMEOUT_S)
        except Exception as exc:
            if getattr(exc, "error_type", "") == "TimeoutError":
                handler._send_json(503, {"error": {
                    "message": "run is paused; model admission timed out",
                    "type": "service_unavailable"}})
            else:
                handler._send_json(503, {"error": {"message": "budget admission unavailable",
                                                   "type": "service_unavailable"}})
            return
        if not self._client_alive(handler):
            return
        if isinstance(admission, dict) and (admission.get("admitted") is False
                                            or admission.get("stop") is True):
            self._note_denial()
            message = ("run is paused or stopped" if admission.get("stop")
                       else "budget admission is closed")
            handler._send_json(429, {"error": {"message": message,
                                               "type": "budget_exhausted"}})
            return
        try:
            self._call("budget.validate_request", {"request_bytes": len(raw)})
        except Exception as exc:
            if getattr(exc, "error_type", "") in {"BudgetExhausted"}:
                self._note_denial()
                handler._send_json(429, {"error": {
                    "message": "model request exceeds the budget bound",
                    "type": "budget_exhausted"}})
            else:
                handler._send_json(503, {"error": {
                    "message": "budget validation unavailable",
                    "type": "service_unavailable"}})
            return
        if not self._client_alive(handler):
            return

        # 2. Reserve exactly one model attempt.
        try:
            reservation = self._call("budget.reserve",
                                     {"purpose": self.purpose, "role": self.role})
        except Exception as exc:
            if getattr(exc, "error_type", "") in {"BudgetExhausted"}:
                self._note_denial()
                handler._send_json(429, {"error": {
                    "message": "team model budget exhausted",
                    "type": "budget_exhausted"}})
            else:
                handler._send_json(503, {"error": {
                    "message": "model admission unavailable",
                    "type": "service_unavailable"}})
            return
        if not isinstance(reservation, str) or not reservation:
            handler._send_json(503, {"error": {"message": "model admission returned no reservation",
                                              "type": "service_unavailable"}})
            return

        turn = self._next_turn()
        started = time.monotonic()
        usage: dict[str, Any] = dict(UNKNOWN_USAGE)
        response_document: dict[str, Any] | None = None
        sent = False
        client_gone = False
        response_started = False
        stream_error: BaseException | None = None
        try:
            # 3. Durable request evidence before invoking the provider. A ledger
            #    failure is fail-closed: never spend budget without the record.
            try:
                request_ref = self._put_artifact(body, MAX_REQUEST_ARTIFACT_BYTES,
                                                 kind="model_request")
                self._append("model_request", {
                    "turn": turn, "request_ref": request_ref, "purpose": self.purpose,
                    "role": self.role, "reservation_id": reservation, "runtime": "pi"})
            except Exception:
                self._note_ledger_error()
                handler._send_json(503, {"error": {
                    "message": "model request could not be recorded",
                    "type": "service_unavailable"}})
                return

            # 4. Upstream call; relay bytes and capture bounded evidence.
            upstream_body = dict(body)
            upstream_body["model"] = self.model_id
            encoded = json.dumps(upstream_body, ensure_ascii=False, default=str).encode("utf-8")
            headers = {"content-type": "application/json", "accept": "text/event-stream, application/json"}
            if self.forwarder_socket is not None:
                # The provider credential is added by the host-side forwarder and
                # never enters the runner container environment.
                headers[FORWARDER_TOKEN_HEADER] = self.forwarder_token
            else:
                api_key = os.environ.get(str(self.api_key_env)) if self.api_key_env else None
                if api_key:
                    headers["authorization"] = "Bearer " + api_key
            try:
                with httpx.Client(
                        timeout=self.timeout_s, transport=self._transport,
                        # A Unix-socket forwarder is not a network URL: never let
                        # ambient proxy env vars (or NO_PROXY parsing) affect it.
                        trust_env=(self.forwarder_socket is None
                                   and not _is_loopback_url(self.upstream_url))) as client:
                    with client.stream("POST", self.upstream_url, content=encoded,
                                       headers=headers) as response:
                        sent = True
                        content_type = response.headers.get("content-type", "")
                        if response.status_code >= 400:
                            self._note_upstream_error(response.status_code)
                            # No completion was produced; do not charge the
                            # worst-case reservation for the failed attempt.
                            usage = dict(ZERO_USAGE)
                            payload = b""
                            for chunk in response.iter_bytes():
                                if len(payload) < 256 * 1024:
                                    payload += chunk[:256 * 1024 - len(payload)]
                            response_document = {"upstream_status": response.status_code,
                                                 "body_excerpt":
                                                     payload.decode("utf-8", errors="replace")[:4000]}
                            handler._send_raw(response.status_code,
                                              {"content-type": "application/json",
                                               "connection": "close"},
                                              payload or json.dumps({
                                                  "error": {"message": "upstream model error",
                                                            "type": "upstream_error"}}).encode())
                            return
                        if "text/event-stream" in content_type:
                            capture = _SSECapture()
                            handler._send_stream_headers()
                            response_started = True
                            try:
                                for chunk in response.iter_bytes():
                                    capture.feed(chunk)
                                    handler._write_stream_chunk(chunk)
                            except (BrokenPipeError, ConnectionResetError):
                                client_gone = True
                                raise
                            except httpx.HTTPError as exc:
                                # Headers/chunks are already on the wire: a second
                                # HTTP response would corrupt Pi's SSE parser.
                                stream_error = exc
                                self._note_upstream_error(None)
                            if stream_error is None and not capture.done:
                                stream_error = RuntimeError(
                                    "upstream SSE stream ended before the [DONE] terminator")
                                self._note_upstream_error(None)
                            if stream_error is not None:
                                handler.close_connection = True
                                usage = dict(UNKNOWN_USAGE)
                                response_document = None
                            else:
                                handler._end_stream()
                                usage = capture.usage or dict(UNKNOWN_USAGE)
                                response_document = capture.document()
                                self._note_upstream_success()
                        else:
                            payload = bytearray()
                            for chunk in response.iter_bytes():
                                if len(payload) < MAX_RESPONSE_ARTIFACT_BYTES:
                                    payload.extend(chunk[:MAX_RESPONSE_ARTIFACT_BYTES - len(payload)])
                            parsed = None
                            try:
                                parsed = json.loads(bytes(payload))
                            except (UnicodeDecodeError, ValueError):
                                parsed = None
                            usage = _usage_from_payload(parsed) or dict(UNKNOWN_USAGE)
                            response_document = {"json": parsed if parsed is not None
                                                 else bytes(payload).decode("utf-8", errors="replace")[:MAX_CAPTURE_CHARS]}
                            self._note_upstream_success()
                            handler._send_raw(200, {"content-type": content_type or "application/json",
                                                    "content-length": str(len(payload))},
                                              bytes(payload))
            except ProxyInfraError:
                raise
            except (BrokenPipeError, ConnectionResetError):
                client_gone = True
                raise
            except httpx.HTTPError as exc:
                self._note_upstream_error(None)
                stream_error = exc
                if not response_started:
                    handler._send_json(502, {
                        "error": {"message": "upstream model request failed",
                                  "type": "upstream_error"}})
                else:
                    # Never append a second HTTP response to a started stream.
                    handler.close_connection = True
                return
        finally:
            latency_ms = (time.monotonic() - started) * 1000
            # 5. Settle exactly once. Unknown usage is conservative (the budget
            #    charges the admission-time reservation).
            settle_usage = dict(usage) if isinstance(usage, dict) else dict(UNKNOWN_USAGE)
            if client_gone:
                settle_usage = dict(UNKNOWN_USAGE)
            try:
                self._call("budget.settle", {"reservation_id": reservation,
                                             "usage": settle_usage})
            except Exception:
                self._note_ledger_error()
            if sent and not client_gone and response_document is not None \
                    and stream_error is None:
                try:
                    response_ref = self._put_artifact(response_document,
                                                      MAX_RESPONSE_ARTIFACT_BYTES,
                                                      kind="model_response")
                    self._append("model_response", {
                        "turn": turn, "response_ref": response_ref, "usage": settle_usage,
                        "latency_ms": round(latency_ms, 3), "purpose": self.purpose,
                        "role": self.role, "reservation_id": reservation, "runtime": "pi"})
                except Exception:
                    self._note_ledger_error()
            elif sent and not client_gone:
                try:
                    error = stream_error
                    self._append("model_error", {
                        "turn": turn,
                        "runtime": "pi",
                        "phase": "upstream_stream",
                        "error": type(error).__name__ if error is not None else "IncompleteUpstreamStream",
                        "message": (str(error)[:300] if error is not None else
                                    "upstream stream ended before completion"),
                        "purpose": self.purpose,
                        "role": self.role,
                        "reservation_id": reservation,
                    })
                except Exception:
                    self._note_ledger_error()


class _ProxyHandler(BaseHTTPRequestHandler):
    """One HTTP handler bound to a :class:`PiModelProxy` instance."""

    protocol_version = "HTTP/1.1"
    proxy: "PiModelProxy"

    def log_message(self, *_args: Any) -> None:
        # Never log request/response bodies or headers: they can contain
        # prompts, tool output and (for upstream calls) credentials.
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.proxy._handle_get(self)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.proxy._handle_post(self)

    def _send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_raw(status, {"content-type": "application/json",
                                "content-length": str(len(payload))}, payload)

    def _send_raw(self, status: int, headers: dict[str, str], payload: bytes) -> None:
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, str(value))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_stream_headers(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

    def _write_stream_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_stream(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


# ---------------------------------------------------------------------------
# Host-side model forwarder (restricted-egress runner containers)
# ---------------------------------------------------------------------------

class _ForwarderHandler(BaseHTTPRequestHandler):
    """One HTTP request relayed to the configured provider by the host."""

    protocol_version = "HTTP/1.1"
    forwarder: "PiModelForwarder"

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send_json(405, {"error": {"message": "method not allowed",
                                        "type": "invalid_request_error"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.forwarder._handle_post(self)

    def _send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_raw(status, {"content-type": "application/json",
                                "content-length": str(len(payload))}, payload)

    def _send_raw(self, status: int, headers: dict[str, str], payload: bytes) -> None:
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, str(value))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_stream_headers(self, content_type: str) -> None:
        self.send_response(200)
        self.send_header("content-type", content_type or "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

    def _write_stream_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_stream(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


class _ThreadingUnixHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server bound to a Unix domain socket."""

    daemon_threads = True
    address_family = socket.AF_UNIX

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "model-forwarder"
        self.server_port = 0


class PiModelForwarder:
    """Loopback-free egress allowlist for Pi runner containers.

    The Supervisor process owns this server: it binds a Unix domain socket inside
    the run directory (bind-mounted into the network-less runner container),
    authenticates a per-run token that only the runner-owned ``PiModelProxy``
    learns, and relays exactly ``POST /v1/chat/completions`` to the configured
    model endpoint with the Supervisor's provider credential. No other host or
    path is reachable, so a model-directed shell in the runner container cannot
    exfiltrate data or spend provider quota through arbitrary egress.
    """

    def __init__(self, model: dict[str, Any], *, socket_path: str | os.PathLike[str],
                 token: str, allowed_path: str = MODEL_FORWARDER_ALLOWED_PATH,
                 owner_uid: int | None = None, owner_gid: int | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        if not isinstance(model, dict):
            raise ValueError("Model forwarder needs a model configuration object")
        if not isinstance(token, str) or len(token) < 16 or "\x00" in token:
            raise ValueError("Model forwarder token must be a non-trivial string")
        if not isinstance(allowed_path, str) or not allowed_path.startswith("/"):
            raise ValueError("Model forwarder path must be absolute")
        self.upstream_url = _upstream_chat_url(str(model.get("base_url") or ""))
        self.api_key_env = model.get("api_key_env")
        timeout = model.get("timeout_s", DEFAULT_TIMEOUT_S)
        try:
            self.timeout_s = max(1.0, min(float(timeout), 3600.0))
        except (TypeError, ValueError):
            self.timeout_s = DEFAULT_TIMEOUT_S
        try:
            self.max_request_bytes = max(
                1024, min(int(model.get("max_request_bytes") or
                             MODEL_FORWARDER_MAX_REQUEST_BYTES),
                          MAX_REQUEST_BODY_BYTES))
        except (TypeError, ValueError):
            self.max_request_bytes = MODEL_FORWARDER_MAX_REQUEST_BYTES
        try:
            self.max_response_bytes = max(
                1024, min(int(model.get("max_response_bytes") or
                             MODEL_FORWARDER_MAX_RESPONSE_BYTES),
                          MAX_RESPONSE_ARTIFACT_BYTES))
        except (TypeError, ValueError):
            self.max_response_bytes = MODEL_FORWARDER_MAX_RESPONSE_BYTES
        self.socket_path = Path(socket_path)
        self.allowed_path = allowed_path
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self.token = token
        self._transport = transport
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._socket_stat: os.stat_result | None = None

    # -- lifecycle ------------------------------------------------------
    def start(self) -> str:
        if self._server is not None:
            return str(self.socket_path)
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        try:
            # The runner container often uses the run directory owner's uid
            # (Supervisor itself may be root). Let that uid traverse the mount
            # and read the socket; the per-run capability token still gates use.
            os.chmod(path.parent, 0o750)
            if self.owner_uid is not None and self.owner_gid is not None:
                os.chown(path.parent, self.owner_uid, self.owner_gid)
        except OSError:
            pass
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise ValueError("model forwarder socket path is occupied by a non-socket")
            path.unlink()
        handler_class = type("BoundForwarderHandler", (_ForwarderHandler,),
                             {"forwarder": self})
        server = _ThreadingUnixHTTPServer(str(path), handler_class)
        os.chmod(path, 0o600)
        if self.owner_uid is not None and self.owner_gid is not None:
            try:
                os.chown(path, self.owner_uid, self.owner_gid)
            except OSError:
                pass
        self._socket_stat = path.lstat()
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="pi-model-forwarder", daemon=True)
        self._thread.start()
        return str(path)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        saved, self._socket_stat = self._socket_stat, None
        if saved is None:
            return
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        try:
            if current.st_dev == saved.st_dev and current.st_ino == saved.st_ino:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "PiModelForwarder":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.stop()
        return False

    # -- relay ----------------------------------------------------------
    def _authorized(self, handler: _ForwarderHandler) -> bool:
        supplied = handler.headers.get(FORWARDER_TOKEN_HEADER, "")
        if not isinstance(supplied, str) or not supplied:
            return False
        return hmac.compare_digest(supplied, self.token)

    def _handle_post(self, handler: _ForwarderHandler) -> None:
        path = handler.path.split("?", 1)[0]
        if path != self.allowed_path:
            handler._send_json(404, {"error": {"message": "not found",
                                               "type": "invalid_request_error"}})
            return
        if not self._authorized(handler):
            handler._send_json(401, {"error": {"message": "unauthorized",
                                               "type": "authentication_error"}})
            return
        try:
            length = int(handler.headers.get("content-length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > self.max_request_bytes:
            handler._send_json(413, {"error": {"message": "request body size is invalid",
                                               "type": "invalid_request_error"}})
            return
        raw = handler.rfile.read(length)
        headers = {"content-type": "application/json",
                   "accept": handler.headers.get("accept", "text/event-stream, application/json")}
        api_key = os.environ.get(str(self.api_key_env)) if self.api_key_env else None
        if api_key:
            headers["authorization"] = "Bearer " + api_key
        response_started = False
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self._transport,
                              trust_env=False) as client:
                with client.stream("POST", self.upstream_url, content=raw,
                                   headers=headers) as response:
                    response_started = True
                    content_type = response.headers.get("content-type", "")
                    if response.status_code >= 400:
                        payload = bytearray()
                        for chunk in response.iter_bytes():
                            if len(payload) < self.max_response_bytes:
                                payload.extend(
                                    chunk[:self.max_response_bytes - len(payload)])
                        handler._send_raw(
                            response.status_code,
                            {"content-type": content_type or "application/json",
                             "content-length": str(len(payload))}, bytes(payload))
                        return
                    if "text/event-stream" in content_type.lower():
                        handler._send_stream_headers(content_type)
                        try:
                            for chunk in response.iter_bytes():
                                handler._write_stream_chunk(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            handler.close_connection = True
                            return
                        handler._end_stream()
                    else:
                        payload = bytearray()
                        for chunk in response.iter_bytes():
                            if len(payload) < self.max_response_bytes:
                                payload.extend(
                                    chunk[:self.max_response_bytes - len(payload)])
                            if len(payload) >= self.max_response_bytes:
                                break
                        handler._send_raw(200, {
                            "content-type": content_type or "application/json",
                            "content-length": str(len(payload))}, bytes(payload))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            handler.close_connection = True
        except httpx.HTTPError:
            if response_started:
                # Never append a second HTTP response to a started stream.
                handler.close_connection = True
            else:
                handler._send_json(502, {
                    "error": {"message": "upstream model request failed",
                              "type": "upstream_error"}})
        except OSError:
            handler.close_connection = True
