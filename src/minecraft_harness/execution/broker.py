"""Small player-scoped HTTP bridge into the trusted durable submission ledger.

This runs in the Supervisor process. Only distinct player credentials reach a
workspace; raw Node control and execution credentials never do.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from urllib.parse import parse_qs, urlsplit
import re
import os
import stat
from pathlib import Path


class SubmissionBroker:
    def __init__(self, gateway):
        self.gateway = gateway
        self.tokens = {agent: secrets.token_urlsafe(32) for agent in gateway.agent_ids}
        self.server = None
        self.tasks = set()
        self.writers = set()
        self.socket_path = None
        self.socket_inode = None

    async def start(self):
        configured_socket = self.gateway.config.get("cli_broker_socket")
        if configured_socket:
            path = Path(configured_socket)
            # Linux sun_path is 108 bytes including the trailing NUL; keep one
            # byte of headroom so bind() cannot fail late with
            # "AF_UNIX path too long" after the whole stack is already up.
            if len(os.fsencode(str(path))) > 107:
                raise ValueError(
                    f"broker Unix socket path is too long ({len(os.fsencode(str(path)))} bytes, max 107): "
                    f"choose a shorter run directory: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                existing = path.lstat()
            except FileNotFoundError:
                existing = None
            if existing:
                if not stat.S_ISSOCK(existing.st_mode):
                    raise ValueError("broker socket path is occupied by a non-socket")
                path.unlink()
            self.server = await asyncio.start_unix_server(self.handle, str(path), limit=16384)
            os.chmod(path, 0o666)  # Authenticated, mounted only into isolated workspaces.
            self.socket_path, self.socket_inode = path, path.stat().st_ino
            self.url = "http://localhost"
            return
        bind = self.gateway.config.get("cli_broker_bind", "127.0.0.1")
        self.server = await asyncio.start_server(self.handle, bind, 0, limit=16384)
        port = self.server.sockets[0].getsockname()[1]
        host = self.gateway.config.get("cli_broker_advertise_host", bind)
        if ":" in host:
            host = f"[{host}]"
        self.url = f"http://{host}:{port}"

    def environment(self, agent):
        result = {"MC_RUNTIME_URL": self.url, "MC_AGENT_ID": agent, "MC_AGENT_TOKEN": self.tokens[agent], "MC_SUBMISSION_AUTHORITY": "durable_gateway"}
        if self.socket_path:
            result["MC_RUNTIME_SOCKET"] = self.gateway.config.get("cli_broker_socket_advertise", str(self.socket_path))
        return result

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self.writers):
            writer.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.writers.clear()
        if self.socket_path:
            try:
                if self.socket_path.lstat().st_ino == self.socket_inode:
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            if len(self.tasks) > 32:
                await self.reply(writer, 429, {"error": "broker connection limit"})
                return
            async with asyncio.timeout(15):
                header = await reader.readuntil(b"\r\n\r\n")
                if len(header) > 16384:
                    raise ValueError("header too large")
                lines = header.decode("ascii").split("\r\n")
                method, target, version = lines[0].split(" ")
                if version != "HTTP/1.1":
                    raise ValueError("HTTP/1.1 required")
                headers = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    key, value = line.split(":", 1)
                    key = key.lower()
                    if key in headers:
                        raise ValueError("duplicate header")
                    headers[key] = value.strip()
                if "transfer-encoding" in headers:
                    raise ValueError("chunked request unsupported")
                length = int(headers.get("content-length", "0"))
                if not 0 <= length <= 1048576:
                    await self.reply(writer, 413, {"error": "body too large"})
                    return
                parsed = urlsplit(target)
                path = parsed.path.split("/")
                if len(path) < 4 or path[1:3] != ["v1", "sessions"] or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", path[3]):
                    await self.reply(writer, 404, {"error": "route not found"})
                    return
                agent = path[3]
                expected = self.tokens.get(agent)
                supplied = headers.get("authorization", "").removeprefix("Bearer ")
                if not expected or not secrets.compare_digest(supplied, expected):
                    await self.reply(writer, 403, {"error": "forbidden"})
                    return
                raw = await reader.readexactly(length)
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                session = self.gateway.sessions.get(agent)
                if not session or self.gateway.closing:
                    await self.reply(writer, 409, {"error": "session unavailable"})
                    return
                query = parse_qs(parsed.query)
                if query.get("session_id", [session["session_id"]]) != [session["session_id"]]:
                    await self.reply(writer, 409, {"error": "stale session"})
                    return
                route = path[4:]
                code = 200
                if method == "GET" and not route:
                    result = {**session, "submission_authority": "durable_gateway"}
                elif method == "POST" and route == ["programs"]:
                    if any(body.get(key) != session[key] for key in ("run_id", "world_epoch", "session_id")):
                        await self.reply(writer, 409, {"error": "stale identity"})
                        return
                    if not isinstance(body.get("request_id"), str) or not body["request_id"] or len(body["request_id"]) > 256:
                        raise ValueError("request_id required, max 256 characters")
                    if not self.gateway.admission.is_set() or agent in self.gateway.control_paused:
                        await self.reply(writer, 409, {"error": "session paused"})
                        return
                    result = await self.gateway.submit(agent, body.get("source"), body.get("timeout_ms", 60000), body["request_id"])
                    code = 202
                elif method == "GET" and len(route) == 2 and route[0] == "programs":
                    result = await self.gateway.status(agent, route[1])
                elif method == "POST" and len(route) == 3 and route[0] == "programs" and route[2] == "cancel":
                    result = await self.gateway.cancel(agent, route[1])
                elif method == "GET" and route == ["jobs"]:
                    result = await self.gateway.jobs(agent)
                elif method == "POST" and len(route) == 3 and route[0] == "jobs" and route[2] == "cancel":
                    result = await self.gateway.cancel_job(agent, route[1])
                else:
                    await self.reply(writer, 404, {"error": "route not found"})
                    return
                await self.reply(writer, code, result)
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await self.reply(writer, 400, {"error": "invalid request"})
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self.reply(writer, 504, {"error": "broker deadline; reconcile existing request, never replay"})
        except Exception as exc:
            await self.reply(writer, 503, {"error": f"broker operation failed: {type(exc).__name__}; reconcile request identity"})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.tasks.discard(task)
            self.writers.discard(writer)

    async def reply(self, writer, code, value):
        if writer.is_closing():
            return
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(payload) > self.gateway.config.get("response_max_bytes", 4194304):
            code, payload = 413, b'{"error":"broker response too large"}'
        writer.write(f"HTTP/1.1 {code} Result\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
