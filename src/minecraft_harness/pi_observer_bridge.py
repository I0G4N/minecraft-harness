"""Loopback tool bridge for the Pi Observer runtime.

The Pi Observer's extension (``pi_runtime/extensions/observer-tools.js``) has no
access to the control plane.  It may only POST one observer tool call at a time
to this loopback server, authenticated with a per-process random token.  The
bridge validates the tool name and arguments, forwards the call over the
runner's RPC client (bound to the Observer identity), and returns a bounded,
safe ``content``/``details`` payload to the extension.

The Observer tool surface is deliberately small and paper-aligned:

* ``records_read``   - paged durable experiment records;
* ``evidence_read``  - one artifact referenced by a viewed record;
* ``observer_report``- the Observer's advisory report;
* ``infra_issue``    - a request to repair an environment failure.

There is no shell, game client, memory or web tool here.  The server binds only
``127.0.0.1`` on an OS-assigned port, never logs request bodies or tokens, and
turns every failure into a generic JSON error without leaking harness internals.

The random bridge token is passed to the Pi child environment (the extension
needs it to reach this loopback server).  It authorizes only the four observer
tools above; it is not a control-plane, shell or game credential.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 64 * 1024
MAX_FINDINGS = 100
MAX_VERIFIED_EVENTS = 100
MAX_INFRASTRUCTURE_FINDINGS = 20
MAX_EVIDENCE_REFS = 20
MAX_SUMMARY_CHARS = 4000
MAX_CLAIM_CHARS = 4000
MAX_INFRA_FINDING_CHARS = 2000
MAX_REASON_CHARS = 4000
MAX_QUERY_CHARS = 1000
MAX_EVIDENCE_LIMIT = 32000
MAX_RECORDS_LIMIT = 100

OBSERVER_TOOL_NAMES = frozenset({
    "records_read", "evidence_read", "observer_report", "infra_issue",
})
# Alias matching the player-side PiToolBridge naming.
KNOWN_TOOLS = OBSERVER_TOOL_NAMES
REPORT_STATUSES = frozenset({
    "verified", "player_claim", "insufficient_evidence", "contradicted",
})
# Must match ObserverAgent.ISSUE_KINDS (the paper/observer contract): the
# Observer may request repair for a broken tool or a quota ceiling as well as
# runtime/server connectivity.
INFRA_ISSUE_KINDS = frozenset({
    "reconnect", "runtime_recovery", "server_recovery",
    "broken_tool", "quota_ceiling",
})
_RPC_METHODS = {
    "records_read": "observer.records_read",
    "evidence_read": "observer.evidence_read",
    "observer_report": "observer.report",
    "infra_issue": "supervisor.observer_infra_issue",
}
# Only explicit user-validation errors may surface, and only when their text is
# bounded and free of paths/secrets.  Everything else (RunnerControlError,
# FileNotFoundError, transport/timeout, internal runtime failures) becomes one
# generic message so the model cannot learn control-plane paths or state.
_SAFE_ERROR_TYPES = frozenset({"ValueError", "PermissionError"})
_MAX_SAFE_ERROR_CHARS = 300
_UNSAFE_ERROR_TEXT = re.compile(
    r"(?:^|[\s'\"(])/(?:[^\s'\")]+)"          # absolute POSIX path
    r"|(?:^|[\s'\"(])[A-Za-z]:\\"                # Windows absolute path
    r"|[A-Za-z0-9_.-]*(?:artifacts?|ledger|state|memory)[/\\][^\s]*"  # internal dirs
    r"|(?i:\b(?:api[_-]?key|bearer|token|secret|password|sk-[A-Za-z0-9])\b)"
    r"|(?i:\bharness_[a-z_]+\b)"
    r"|(?i:\b(?:runnercontrol|traceback|sqlite|proc/))"
)
_HEX64 = re.compile(r"[a-f0-9]{64}")


class ObserverBridgeError(ValueError):
    """A model-visible validation or tool error."""


class PiObserverBridge:
    """One authenticated loopback HTTP server per Pi Observer process."""

    def __init__(self, rpc_client: Any, *, agent_id: str = "observer",
                 expected_agent_id: str = "observer", host: str = "127.0.0.1",
                 max_body_bytes: int = MAX_BODY_BYTES,
                 max_text_chars: int = MAX_TEXT_CHARS) -> None:
        role = getattr(rpc_client, "role", None)
        if role is not None and role != "observer":
            raise ValueError("the observer tool bridge requires an observer RPC identity")
        bound_agent = getattr(rpc_client, "agent_id", None)
        if bound_agent is not None and bound_agent != expected_agent_id:
            raise ValueError("the observer tool bridge is not bound to the Observer identity")
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
                # Never log headers or bodies: they contain records and tokens.
                return

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                bridge._handle_post(self)

        server = ThreadingHTTPServer((self.host, 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.base_url = f"http://{self.host}:{int(server.server_address[1])}"
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="pi-observer-bridge", daemon=True)
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

    def __enter__(self) -> "PiObserverBridge":
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
        if not isinstance(tool, str) or tool not in OBSERVER_TOOL_NAMES:
            return 404, {"ok": False, "error": "unknown tool"}
        if not isinstance(arguments, dict):
            return 400, {"ok": False, "error": "tool arguments must be an object"}
        try:
            method, params = self._validate(tool, arguments)
        except ObserverBridgeError as exc:
            return 400, {"ok": False, "error": str(exc)}
        try:
            result = self.rpc.call_sync(method, params)
        except Exception as exc:  # RpcError or transport failure
            return 200, {"ok": False, "error": _safe_error_message(exc)}
        return 200, self._success(tool, result,
                                  forwarded=params if tool == "infra_issue" else None)

    def _success(self, tool: str, result: Any,
                 *, forwarded: dict[str, Any] | None = None) -> dict[str, Any]:
        details: dict[str, Any] = {"tool": tool}
        if isinstance(forwarded, dict) and forwarded.get("issue_id"):
            details["issue_id"] = forwarded["issue_id"]
        if isinstance(result, dict):
            if tool == "records_read":
                details.update({
                    "next_cursor": result.get("next_cursor"),
                    "latest_event_seq": result.get("latest_event_seq"),
                    "truncated": bool(result.get("truncated")),
                    "count": len(result.get("events") or [])
                    if isinstance(result.get("events"), list) else None,
                })
            elif tool == "evidence_read":
                details.update({
                    "hash": result.get("hash"),
                    "offset": result.get("offset"),
                    "next_offset": result.get("next_offset"),
                })
            elif tool == "observer_report":
                details.update({
                    "status": result.get("status"),
                    "event_seq": result.get("event_seq"),
                    "advisory": True,
                    "progress_summary_present": bool(result.get("progress_summary_present")),
                })
            elif tool == "infra_issue":
                details.update({"status": result.get("status")})
        text = json.dumps(result, ensure_ascii=False, default=str)
        truncated = len(text) > self.max_text_chars
        if truncated:
            text = text[:self.max_text_chars] + (
                "\n[tool output truncated at the model-visible limit]")
        return {"ok": True, "content": text,
                "details": {**details, "truncated": truncated}}

    # -- validation -----------------------------------------------------
    def _validate(self, tool: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if tool == "records_read":
            self._require_keys(arguments, {"after", "limit"})
            after = self._optional_int(arguments, "after", default=0, minimum=0)
            limit = self._optional_int(arguments, "limit", default=50,
                                       minimum=1, maximum=MAX_RECORDS_LIMIT)
            return _RPC_METHODS[tool], {"after": after, "limit": limit}
        if tool == "evidence_read":
            self._require_keys(arguments, {"hash", "offset", "limit"})
            digest = arguments.get("hash")
            if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
                raise ObserverBridgeError("hash must be a lowercase SHA256 hex digest")
            offset = self._optional_int(arguments, "offset", default=0, minimum=0)
            limit = self._optional_int(arguments, "limit", default=12000,
                                       minimum=1, maximum=MAX_EVIDENCE_LIMIT)
            return _RPC_METHODS[tool], {"hash": digest, "offset": offset, "limit": limit}
        if tool == "observer_report":
            self._require_keys(arguments, {"progress_summary", "verified_events",
                                           "infrastructure_findings", "findings"})
            params: dict[str, Any] = {}
            if "progress_summary" in arguments:
                params["progress_summary"] = self._text(
                    arguments.get("progress_summary"), "progress_summary",
                    MAX_SUMMARY_CHARS)
            if "verified_events" in arguments:
                params["verified_events"] = self._verified_events(
                    arguments.get("verified_events"))
            if "infrastructure_findings" in arguments:
                params["infrastructure_findings"] = self._infrastructure_findings(
                    arguments.get("infrastructure_findings"))
            if "findings" not in arguments:
                raise ObserverBridgeError("findings is required")
            params["findings"] = self._findings(arguments.get("findings"))
            return _RPC_METHODS[tool], params
        # infra_issue
        self._require_keys(arguments, {"kind", "reason", "evidence_refs"})
        kind = arguments.get("kind")
        if not isinstance(kind, str) or kind not in INFRA_ISSUE_KINDS:
            raise ObserverBridgeError(
                "kind must be one of: " + ", ".join(sorted(INFRA_ISSUE_KINDS)))
        reason = self._text(arguments.get("reason"), "reason", MAX_REASON_CHARS)
        refs = self._evidence_refs(arguments.get("evidence_refs"), label="evidence_refs")
        # The Supervisor dedupes on issue_id.  Every bridge call is a new model
        # request, so a fresh UUID keeps distinct repairs from being collapsed
        # into the first request (the pre-fix bug).  The model cannot set this
        # field: it is not in the allowed argument keys.
        issue_id = uuid.uuid4().hex
        return _RPC_METHODS[tool], {"issue_id": issue_id, "kind": kind,
                                    "reason": reason, "evidence_refs": refs}

    def _findings(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > MAX_FINDINGS:
            raise ObserverBridgeError(
                f"findings must be an array of at most {MAX_FINDINGS} entries")
        findings: list[dict[str, Any]] = []
        for entry in value:
            if not isinstance(entry, dict):
                raise ObserverBridgeError("each finding must be an object")
            self._require_keys(entry, {"status", "claim", "evidence_refs"})
            status = entry.get("status")
            if not isinstance(status, str) or status not in REPORT_STATUSES:
                raise ObserverBridgeError(
                    "finding status must be one of: " + ", ".join(sorted(REPORT_STATUSES)))
            claim = self._text(entry.get("claim"), "claim", MAX_CLAIM_CHARS)
            refs = self._evidence_refs(entry.get("evidence_refs"), label="evidence_refs")
            findings.append({"status": status, "claim": claim, "evidence_refs": refs})
        return findings

    def _verified_events(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > MAX_VERIFIED_EVENTS:
            raise ObserverBridgeError(
                f"verified_events must be an array of at most {MAX_VERIFIED_EVENTS} entries")
        events: list[dict[str, Any]] = []
        for entry in value:
            if not isinstance(entry, dict):
                raise ObserverBridgeError("each verified event must be an object")
            self._require_keys(entry, {"claim", "evidence_refs"})
            claim = self._text(entry.get("claim"), "claim", MAX_CLAIM_CHARS)
            refs = self._evidence_refs(entry.get("evidence_refs"), label="evidence_refs")
            events.append({"claim": claim, "evidence_refs": refs})
        return events

    def _infrastructure_findings(self, value: Any) -> list[str]:
        if not isinstance(value, list) or len(value) > MAX_INFRASTRUCTURE_FINDINGS:
            raise ObserverBridgeError(
                "infrastructure_findings must be an array of at most "
                f"{MAX_INFRASTRUCTURE_FINDINGS} strings")
        return [self._text(item, "infrastructure finding", MAX_INFRA_FINDING_CHARS)
                for item in value]

    @staticmethod
    def _evidence_refs(value: Any, *, label: str = "evidence_refs") -> list[Any]:
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_EVIDENCE_REFS:
            raise ObserverBridgeError(
                f"{label} must be an array of 1 to {MAX_EVIDENCE_REFS} references")
        for reference in value:
            if isinstance(reference, bool):
                raise ObserverBridgeError(f"{label} entries are invalid")
            if isinstance(reference, int):
                if reference < 0:
                    raise ObserverBridgeError(f"{label} event_seq must be nonnegative")
                continue
            if isinstance(reference, str):
                if not _HEX64.fullmatch(reference):
                    raise ObserverBridgeError(
                        f"{label} string references must be lowercase SHA256 hex")
                continue
            if isinstance(reference, dict) and reference:
                keys = set(reference)
                if not keys <= {"hash", "sha256"}:
                    raise ObserverBridgeError(f"{label} object references are invalid")
                for key in keys:
                    if not isinstance(reference.get(key), str) or \
                            not _HEX64.fullmatch(reference[key]):
                        raise ObserverBridgeError(
                            f"{label} object references must contain SHA256 hex")
                continue
            raise ObserverBridgeError(f"{label} entries are invalid")
        return list(value)

    @staticmethod
    def _require_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
        unexpected = set(arguments) - allowed
        if unexpected:
            raise ObserverBridgeError(
                "unexpected tool argument(s): " +
                ", ".join(sorted(map(str, unexpected))[:8]))

    @staticmethod
    def _text(value: Any, name: str, maximum: int) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
            raise ObserverBridgeError(
                f"{name} must be non-empty text no longer than {maximum} characters")
        return value

    @staticmethod
    def _optional_int(arguments: dict[str, Any], name: str, *,
                      default: int, minimum: int, maximum: int | None = None) -> int:
        value = arguments.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ObserverBridgeError(f"{name} must be an integer >= {minimum}")
        if maximum is not None and value > maximum:
            raise ObserverBridgeError(f"{name} must be <= {maximum}")
        return value


def _safe_error_message(error: BaseException) -> str:
    """Return a bounded user-validation message or one generic failure.

    ``FileNotFoundError``/``RunnerControlError``/transport errors are internal
    infrastructure failures: their messages must never reach the model because
    they can contain artifact paths, ledger locations or control-plane state.
    """
    error_type = getattr(error, "error_type", type(error).__name__)
    try:
        message = str(error)
    except Exception:
        message = ""
    message = " ".join(message.split())
    if (error_type in _SAFE_ERROR_TYPES and message
            and len(message) <= _MAX_SAFE_ERROR_CHARS
            and not _UNSAFE_ERROR_TEXT.search(message)):
        return message
    return "observer tool call failed"
