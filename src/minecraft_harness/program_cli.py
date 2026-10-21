"""Player-scoped Mineflayer CLI; run only with that player's runtime token."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse
import uuid

import httpx


async def invoke(args):
    url = args.url or os.environ.get("MC_RUNTIME_URL")
    agent = args.agent or os.environ.get("MC_AGENT_ID")
    token = os.environ.get(args.token_env)
    if not url or not agent or not token:
        raise ValueError("runtime URL, player ID and player-token environment variable are required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid runtime URL")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", agent):
        raise ValueError("invalid Minecraft player ID")
    endpoint = url.rstrip("/") + f"/v1/sessions/{agent}/programs"
    client_options = {"timeout": 60, "trust_env": False, "headers": {"Authorization": f"Bearer {token}"}}
    if os.environ.get("MC_RUNTIME_SOCKET"):
        client_options["transport"] = httpx.AsyncHTTPTransport(uds=os.environ["MC_RUNTIME_SOCKET"])
    async with httpx.AsyncClient(**client_options) as client:
        if args.command in {"exec", "submit"}:
            if args.wait_ms < 0 or args.timeout_ms < 10:
                raise ValueError("wait must be nonnegative and execution timeout at least 10ms")
            with args.file.open("rb") as stream:
                raw_source = stream.read(524289)
            if len(raw_source) > 524288:
                raise ValueError("program exceeds 512 KiB")
            source = raw_source.decode("utf-8")
            request_id = args.request_id or uuid.uuid4().hex
            metadata = await client.get(url.rstrip("/") + f"/v1/sessions/{agent}")
            metadata.raise_for_status()
            session = metadata.json()
            if session.get("lifecycle") == "paper_aligned" and session.get("submission_authority") != "durable_gateway":
                raise ValueError("paper_aligned CLI requires the durable Supervisor submission broker")
            try:
                response = await client.post(endpoint, json={"request_id": request_id, "source": source, "timeout_ms": args.timeout_ms, "world_epoch": session["world_epoch"], "run_id": session["run_id"], "session_id": session.get("session_id")})
                if response.status_code == 409:
                    return {"status": "rejected", "request_id": request_id, "reason": "session paused or run/epoch changed; observe before deciding again"}
                response.raise_for_status()
            except httpx.TransportError:
                return {"status": "unknown", "request_id": request_id, "reason": "submission may have reached the world; do not automatically resubmit"}
            result = response.json()
            if args.command == "submit":
                return result
            wait_until = time.monotonic() + args.wait_ms / 1000
            while result["status"] in {"accepted", "running"} and time.monotonic() < wait_until:
                await asyncio.sleep(min(0.1, max(0, wait_until - time.monotonic())))
                remaining = wait_until - time.monotonic()
                if remaining <= 0:
                    return {**result, "wait_expired": True}
                try:
                    async with asyncio.timeout(remaining):
                        response = await client.get(endpoint + "/" + result["execution_id"])
                        response.raise_for_status()
                        result = response.json()
                except asyncio.TimeoutError:
                    return {**result, "wait_expired": True}
                except httpx.HTTPError:
                    return {**result, "status": "unknown", "request_id": request_id, "reason": "lost status connection; query the same execution_id, do not resubmit"}
            return result
        if args.command == "jobs":
            response = await client.get(url.rstrip("/") + f"/v1/sessions/{agent}/jobs")
            response.raise_for_status()
            return response.json()
        if args.command == "job-cancel":
            job_id = str(uuid.UUID(args.job_id))
            response = await client.post(url.rstrip("/") + f"/v1/sessions/{agent}/jobs/{job_id}/cancel", json={})
            response.raise_for_status()
            return response.json()
        execution_id = str(uuid.UUID(args.execution_id))
        response = await (client.post(endpoint + "/" + execution_id + "/cancel", json={}) if args.command == "cancel" else client.get(endpoint + "/" + execution_id))
        if response.status_code in {404, 410}:
            return {"status": "unknown", "execution_id": execution_id, "reason": "runtime retired this record; inspect the durable Supervisor ledger and never resubmit an uncertain action"}
        response.raise_for_status()
        return response.json()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Submit agent-written JS to your Mineflayer bot")
    parser.add_argument("--url")
    parser.add_argument("--agent")
    parser.add_argument("--token-env", default="MC_AGENT_TOKEN")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("exec", "submit"):
        execute = commands.add_parser(command)
        execute.add_argument("--file", required=True, type=Path)
        execute.add_argument("--timeout-ms", type=int, default=60000)
        execute.add_argument("--wait-ms", type=int, default=65000 if command == "exec" else 0)
        execute.add_argument("--request-id")
    commands.add_parser("jobs")
    job_cancel = commands.add_parser("job-cancel")
    job_cancel.add_argument("--job-id", required=True)
    for name in ("status", "cancel"):
        p = commands.add_parser(name)
        p.add_argument("--execution-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(invoke(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in {"failed", "timed_out", "unknown", "interrupted", "rejected"} else 0
    except (ValueError, OSError, httpx.HTTPError) as exc:
        print(f"mc error: {type(exc).__name__}", file=sys.stderr)
        return 2
