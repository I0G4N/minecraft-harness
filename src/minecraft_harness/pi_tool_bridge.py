"""Loopback tool bridge for the Pi memory/web extension.

The Pi extension (``pi_runtime/extensions/minecraft-tools.js``) is sandboxed from
the control plane: it may only POST one tool call at a time to this loopback
server, authenticated with a per-runner random token.  The bridge validates the
tool name and arguments, forwards the call over the runner RPC client
(``memory.*`` and ``web.search``/``web.fetch``) and returns a bounded
``content``/``details`` payload to the extension.

It binds only ``127.0.0.1`` on an OS-assigned port, never logs request bodies or
tokens, and turns every failure into a generic JSON error without leaking
harness internals.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_MEMORY_CONTENT_CHARS = 1_000_000
MAX_TEXT_CHARS = 64 * 1024

KNOWN_TOOLS = frozenset({
    "memory_read", "memory_write", "memory_list", "memory_search",
    "web_search", "web_fetch",
})
_READ_ONLY_TOOLS = frozenset({
    "memory_read", "memory_list", "memory_search", "web_search", "web_fetch",
})
_SAFE_ERROR_TYPES = frozenset({
    "ValueError", "PermissionError", "FileNotFoundError", "MemoryConflict",
    "MemoryPermissionError",
})
_RPC_METHODS = {
    "memory_read": "memory.read",
    "memory_write": "memory.write",
    "memory_list": "memory.list",
    "memory_search": "memory.search",
    "web_search": "web.search",
    "web_fetch": "web.fetch",
}


class ToolBridgeError(ValueError):
    """A model-visible validation or tool error."""


class PiToolBridge:
    """One authenticated loopback HTTP server per Pi runner process."""

    def __init__(self, rpc_client: Any, *, agent_id: str, host: str = "127.0.0.1",
                 max_body_bytes: int = MAX_BODY_BYTES,
                 max_text_chars: int = MAX_TEXT_CHARS) -> None:
        self.rpc = rpc_client
        self.agent_id = agent_id
        self.host = host
        self.token = secrets.token_urlsafe(32)
        self.max_body_bytes = int(max_body_bytes)
        self.max_text_chars = int(max_text_chars)
        self.base_url = ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------
    def start(self) -> str:
        if self._server is not None:
            return self.base_url
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                # Never log headers/body: they contain memory content and token.
                return

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                bridge._handle_post(self)

        server = ThreadingHTTPServer((self.host, 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.base_url = f"http://{self.host}:{int(server.server_address[1])}"
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="pi-tool-bridge", daemon=True)
        self._thread.start()
        return self.base_url

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

    def __enter__(self) -> "PiToolBridge":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.stop()
        return False

    # -- HTTP handling --------------------------------------------------
    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        header = handler.headers.get("authorization", "")
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:], self.token)

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        path = handler.path.split("?", 1)[0].rstrip("/")
        if path != "/tool":
            self._send(handler, 404, {"ok": False, "error": "not found"})
            return
        if not self._authorized(handler):
            self._send(handler, 401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(handler.headers.get("content-length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > self.max_body_bytes:
            self._send(handler, 413, {"ok": False, "error": "tool call size is invalid"})
            return
        raw = handler.rfile.read(length)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            self._send(handler, 400, {"ok": False, "error": "tool call is not valid JSON"})
            return
        if not isinstance(payload, dict):
            self._send(handler, 400, {"ok": False, "error": "tool call must be an object"})
            return
        tool = payload.get("tool")
        arguments = payload.get("arguments", {})
        status, result = self.handle_tool(tool, arguments)
        self._send(handler, status, result)

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(body)))
            handler.send_header("connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        handler.close_connection = True

    # -- tool dispatch --------------------------------------------------
    def handle_tool(self, tool: Any, arguments: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(tool, str) or tool not in KNOWN_TOOLS:
            return 404, {"ok": False, "error": "unknown tool"}
        if not isinstance(arguments, dict):
            return 400, {"ok": False, "error": "tool arguments must be an object"}
        try:
            params = self._validate(tool, arguments)
        except ToolBridgeError as exc:
            return 400, {"ok": False, "error": str(exc)}
        try:
            result = self.rpc.call_sync(_RPC_METHODS[tool], params)
        except Exception as exc:  # RpcError or transport failure
            return 200, {"ok": False, "error": _safe_error_message(exc)}
        text = json.dumps(result, ensure_ascii=False, default=str)
        truncated = len(text) > self.max_text_chars
        if truncated:
            text = text[:self.max_text_chars] + (
                "\n[tool output truncated at the model-visible limit]")
        return 200, {"ok": True, "content": text,
                     "details": {"tool": tool, "truncated": truncated}}

    def _validate(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # memory.* carry the bound actor for the ServiceHub filter; web.* are
        # identity-free because the RPC connection already binds the identity.
        params: dict[str, Any] = ({} if tool in {"web_search", "web_fetch"}
                                  else {"actor_id": self.agent_id})
        if tool == "memory_read":
            self._require_keys(arguments, {"scope", "path", "author", "revision",
                                           "offset", "limit"})
            scope = self._scope(arguments, required=True, allow_all=False)
            params["scope"] = scope
            params["path"] = self._text(arguments.get("path"), "path", 512)
            self._optional_author(arguments, params)
            self._optional_int(arguments, params, "revision", minimum=0)
            self._optional_int(arguments, params, "offset", minimum=0)
            self._optional_int(arguments, params, "limit", minimum=1, maximum=32000)
        elif tool == "memory_write":
            self._require_keys(arguments, {"scope", "path", "content", "expected_revision"})
            params["scope"] = self._scope(arguments, required=True, allow_all=False)
            params["path"] = self._text(arguments.get("path"), "path", 512)
            content = arguments.get("content")
            if not isinstance(content, str) or len(content.encode()) > MAX_MEMORY_CONTENT_CHARS:
                raise ToolBridgeError("note content must be text no larger than 1 MB")
            params["content"] = content
            expected = arguments.get("expected_revision")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ToolBridgeError("expected_revision must be a nonnegative integer")
            params["expected_revision"] = expected
            self._optional_author(arguments, params)
        elif tool == "memory_list":
            self._require_keys(arguments, {"scope", "author", "offset", "limit"})
            if "scope" in arguments:
                params["scope"] = self._scope(arguments, required=True, allow_all=False)
            self._optional_author(arguments, params)
            self._optional_int(arguments, params, "offset", minimum=0)
            self._optional_int(arguments, params, "limit", minimum=1, maximum=1000)
        elif tool == "memory_search":
            self._require_keys(arguments, {"query", "scope", "author", "limit"})
            params["query"] = self._text(arguments.get("query"), "query", 1000)
            if "scope" in arguments:
                params["scope"] = self._scope(arguments, required=True, allow_all=True)
            self._optional_author(arguments, params)
            self._optional_int(arguments, params, "limit", minimum=1, maximum=100)
        elif tool == "web_search":
            self._require_keys(arguments, {"query"})
            params["query"] = self._text(arguments.get("query"), "query", 1000)
        else:  # web_fetch
            self._require_keys(arguments, {"url"})
            params["url"] = self._text(arguments.get("url"), "url", 4096)
        return params

    @staticmethod
    def _require_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
        unexpected = set(arguments) - allowed
        if unexpected:
            raise ToolBridgeError(
                "unexpected tool argument(s): " + ", ".join(sorted(map(str, unexpected))[:8]))

    @staticmethod
    def _text(value: Any, name: str, maximum: int) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
            raise ToolBridgeError(f"{name} must be non-empty text no longer than {maximum} characters")
        return value

    @staticmethod
    def _scope(arguments: dict[str, Any], *, required: bool, allow_all: bool) -> str:
        value = arguments.get("scope")
        allowed = {"private", "team"} | ({"all"} if allow_all else set())
        if value is None and not required:
            return "private"
        if not isinstance(value, str) or value not in allowed:
            raise ToolBridgeError("scope must be one of: " + ", ".join(sorted(allowed)))
        return value

    @staticmethod
    def _optional_author(arguments: dict[str, Any], params: dict[str, Any]) -> None:
        if "author" not in arguments:
            return
        author = arguments.get("author")
        if not isinstance(author, str) or len(author) > 100:
            raise ToolBridgeError("author must be a short agent name")
        params["author"] = author

    @staticmethod
    def _optional_int(arguments: dict[str, Any], params: dict[str, Any], name: str, *,
                      minimum: int, maximum: int | None = None) -> None:
        if name not in arguments:
            return
        value = arguments.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ToolBridgeError(f"{name} must be an integer >= {minimum}")
        if maximum is not None and value > maximum:
            raise ToolBridgeError(f"{name} must be <= {maximum}")
        params[name] = value


def _safe_error_message(error: BaseException) -> str:
    """Only whitelisted, already user-facing tool errors surface verbatim."""
    error_type = getattr(error, "error_type", type(error).__name__)
    message = str(error)
    if error_type in _SAFE_ERROR_TYPES and message:
        return message[:1000]
    return "memory/web tool call failed"
