"""Local operator CLI. No player model is allowed to execute this control surface."""
from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.metadata
import json
import os
import shutil
import signal
import sqlite3
import sys
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import load_config
from .observer import write_report
from .recorder import atomic_json
from .supervisor import Supervisor, is_active, request_control

FAILED_STATUSES = {"infra_failed", "model_failed", "resource_exhausted", "reliability_failed"}
DOCTOR_TIMEOUT_S = 5
DOCTOR_RESPONSE_BYTES = 65536


def _viewer_ports(path):
    """Optional live first-person viewer ports frozen into the run manifest."""
    manifest = Path(path) / "manifest.json"
    try:
        config = json.loads(manifest.read_text()).get("config", {})
    except (OSError, ValueError):
        return {}
    ports = config.get("runtime", {}).get("viewer_ports", {})
    if not isinstance(ports, dict):
        return {}
    return {agent: port for agent, port in ports.items()
            if isinstance(agent, str) and type(port) is int and 0 < port < 65536}


def run_status(run_dir, *, dashboard=False):
    path = Path(run_dir).resolve()
    database = path / "events.sqlite3"
    if not database.exists():
        raise ValueError("run directory has no event ledger")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        state_query = "SELECT key,value_json FROM state"
        state_args = ()
        if dashboard:
            keys = ('status', 'supervisor_pid', 'active_wall_seconds', 'world_epoch', 'world_frozen',
                    'budget', 'evaluation', 'first_success', 'resources', 'storage',
                    'observer_outcome', 'observer_degraded', 'latest_world_checkpoint')
            state_query += ' WHERE key IN (' + ','.join('?' for _ in keys) + ')'
            state_args = keys
        state = {key: json.loads(value) for key, value in connection.execute(state_query, state_args)}
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        controls, control_counts = [], {}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='controls'").fetchone():
            control_counts = dict(connection.execute("SELECT state,COUNT(*) FROM controls GROUP BY state"))
            connection.row_factory = sqlite3.Row
            for row in connection.execute("SELECT * FROM controls ORDER BY rowid DESC LIMIT 100"):
                control = dict(row)
                control["arguments"] = json.loads(control.pop("arguments_json"))
                encoded = control.pop("result_json")
                control["result"] = (json.loads(encoded) if len(encoded) <= 8192 else {"preview": encoded[:8192], "truncated": True}) if encoded else None
                controls.append(control)
    finally:
        connection.close()
    return {"run_dir": str(path), "supervisor_active": is_active(path), "event_count": count,
            **state, "viewers": _viewer_ports(path), "controls": controls, "control_counts": control_counts}


class _OperatorInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


async def _run_with_signals(supervisor):
    """Deliver OS shutdown as cancellation so supervisor cleanup can finish."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(supervisor.run(), name="minecraft-supervisor")
    received = []
    previous = {}
    def interrupted(signum):
        # A second signal must not interrupt the cancellation cleanup itself.
        if not received:
            received.append(signum)
            task.cancel()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, interrupted, signum)
        except (NotImplementedError, RuntimeError, ValueError):
            previous.pop(signum, None)
    try:
        try:
            result = await task
        except asyncio.CancelledError:
            if received:
                raise _OperatorInterrupted(received[0]) from None
            raise
        if received:
            raise _OperatorInterrupted(received[0])
        return result
    finally:
        for signum, handler in previous.items():
            loop.remove_signal_handler(signum)
            signal.signal(signum, handler)


async def _runtime_doctor(config):
    """Read-only credential/protocol checks under one shared network deadline."""
    runtime = config["runtime"]
    pending = []
    async with httpx.AsyncClient(timeout=DOCTOR_TIMEOUT_S, follow_redirects=False) as client:
        async def probe(agent, endpoint, path, token, health):
            name = f"runtime {'health' if health else 'admin credential'} for {agent}"
            parsed = urlparse(endpoint)
            result = {"check": name, "endpoint": f"{parsed.scheme}://{parsed.netloc}", "ok": False}
            try:
                headers = {} if health else {"Authorization": "Bearer " + token}
                raw = bytearray()
                async with client.stream("GET", endpoint.rstrip("/") + path, headers=headers) as response:
                    if response.status_code != 200:
                        return {**result, "reason": f"RPC returned HTTP {response.status_code}"}
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > DOCTOR_RESPONSE_BYTES:
                            return {**result, "reason": "RPC preflight response exceeded 64 KiB"}
                        raw.extend(chunk)
                document = json.loads(raw)
                if health:
                    valid = (isinstance(document, dict) and document.get("status") == "ok"
                             and document.get("protocol_version") in ({3} if runtime.get('lifecycle') == 'paper_aligned' else {2, 3})
                             and document.get("mode") == runtime["mode"])
                    return {**result, "ok": valid, **({} if valid else {"reason": "RPC health must report compatible protocol, expected mode and ready status"})}
                valid = isinstance(document, dict) and isinstance(document.get("sessions"), list)
                return {**result, "ok": valid, **({} if valid else {"reason": "RPC admin session listing has invalid schema"})}
            except httpx.TimeoutException:
                return {**result, "reason": "RPC network deadline exceeded"}
            except httpx.HTTPError:
                return {**result, "reason": "RPC network is unreachable"}
            except (ValueError, TypeError):
                return {**result, "reason": "RPC returned invalid JSON"}
        for agent in config["agents"]["ids"]:
            endpoint = runtime.get("urls", {}).get(agent) or runtime.get("url")
            if not endpoint:
                continue
            environment = runtime.get("token_envs", {}).get(agent, runtime["token_env"])
            token = os.environ.get(environment)
            pending.append((agent, endpoint, True, asyncio.create_task(probe(agent, endpoint, "/health", "", True))))
            if token:
                pending.append((agent, endpoint, False, asyncio.create_task(probe(agent, endpoint, "/v1/sessions", token, False))))
        try:
            async with asyncio.timeout(DOCTOR_TIMEOUT_S):
                await asyncio.gather(*(task for _, _, _, task in pending))
        except TimeoutError:
            for _, _, _, task in pending:
                task.cancel()
            await asyncio.gather(*(task for _, _, _, task in pending), return_exceptions=True)
        results = []
        for agent, endpoint, health, task in pending:
            if task.done() and not task.cancelled():
                results.append(task.result())
            else:
                parsed = urlparse(endpoint)
                results.append({"check": f"runtime {'health' if health else 'admin credential'} for {agent}",
                                "endpoint": f"{parsed.scheme}://{parsed.netloc}", "ok": False,
                                "reason": "RPC network preflight exceeded its total deadline"})
        return results


def doctor(config):
    result = {"python": sys.version.split()[0], "node": shutil.which("node"), "docker": shutil.which("docker"), "runtime_mode": config["runtime"]["mode"], "model_kind": config["model"]["kind"], "dependencies": {name: importlib.metadata.version(name) for name in ("httpx", "PyYAML")}, "checks": []}
    checks = result["checks"]
    external = bool(config["runtime"].get("url") or config["runtime"].get("urls"))
    if config["runtime"]["mode"] == "fake" and not external:
        checks.append({"check": "node_installed", "ok": bool(result["node"])})
    else:
        rt = config["runtime"]
        for agent in config["agents"]["ids"]:
            env = rt.get("token_envs", {}).get(agent, rt["token_env"])
            checks.append({"check": f"runtime credential for {agent}", "variable": env, "ok": bool(os.environ.get(env))})
        task = config["experiment"]["task"]
        task_name = task if isinstance(task, str) else task.get("name", task.get("id", task.get("task")))
        if task_name != "observe_only":
            world = config["server"].get("world_dir")
            checks.append({"check": "server world evidence configured", "ok": bool(world and Path(world).is_dir())})
        if external:
            checks.extend(asyncio.run(_runtime_doctor(config)))
    key_env = config["model"].get("api_key_env")
    if key_env:
        checks.append({"check": "model credential present", "variable": key_env, "ok": bool(os.environ.get(key_env))})
    result["ok"] = all(item["ok"] for item in checks)
    return result


def serve(run_dir, port):
    from .dashboard import serve_dashboard
    serve_dashboard(run_dir, port, lambda path: run_status(path, dashboard=True), request_control)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent multi coding-agent Minecraft experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "doctor", "batch", "prepare-trials"):
        p = commands.add_parser(name)
        p.add_argument("--config", required=True, type=Path)
        if name in {"run", "batch", "prepare-trials"}:
            p.add_argument("--run-dir", type=Path)
        if name in {"batch", "prepare-trials"}:
            p.add_argument("--seeds", default="random" if name == 'prepare-trials' else '42,43,44')
            p.add_argument("--repetitions", type=int, default=1)
    for name in ("status", "report", "pause", "resume", "stop", "serve", "checkpoint", "restore"):
        p = commands.add_parser(name)
        p.add_argument("run_dir", type=Path)
        if name == "pause":
            p.add_argument("--freeze-world", action="store_true")
        if name == "serve":
            p.add_argument("--port", type=int, default=8899)
            p.description = "Serve one run or a runs folder as the local experiment dashboard"
        if name == "restore":
            p.add_argument("--checkpoint-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"doctor", "run", "batch", "prepare-trials"}:
            config = load_config(args.config)
            directory = getattr(args, 'run_dir', None) or Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
            if args.command == 'prepare-trials' or (args.command == 'batch' and config['runtime']['mode'] == 'minecraft'):
                from .trials import prepare_trials, execute_trials
                seeds = [None if value.strip() == 'random' else int(value) for value in args.seeds.split(',')]
                manifest = prepare_trials(config, directory, seeds, args.repetitions)
                if args.command == 'batch':
                    async def run_trial(trial_config, run_dir):
                        check = await asyncio.to_thread(doctor, trial_config)
                        if not check['ok']:
                            raise ValueError('Trial preflight failed')
                        return await _run_with_signals(Supervisor(trial_config, run_dir))
                    manifest = asyncio.run(execute_trials(manifest, directory, run_trial))
                print(json.dumps(manifest, ensure_ascii=False, indent=2))
                return 1 if any(item['status'] in FAILED_STATUSES for item in manifest['trials']) else 0
            check = doctor(config)
            if args.command == "doctor":
                print(json.dumps(check, ensure_ascii=False, indent=2))
                return 0 if check["ok"] else 2
            if not check["ok"]:
                raise ValueError("preflight failed: " + json.dumps(check["checks"]))
            directory = args.run_dir or Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
            if args.command == "run":
                result = asyncio.run(_run_with_signals(Supervisor(config, directory)))
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1 if result["status"] in FAILED_STATUSES else 0
            if args.repetitions < 1:
                raise ValueError("repetitions must be positive")
            seeds = [int(value) for value in args.seeds.split(",")]
            if config["runtime"]["mode"] == "minecraft":
                raise ValueError("real-world batch needs a fresh world per trial; provision worlds first and run each config separately")
            directory.mkdir(parents=True, exist_ok=False)
            results = []
            for seed in seeds:
                for repeat in range(args.repetitions):
                    trial = copy.deepcopy(config)
                    trial["experiment"]["seed"] = seed
                    result = asyncio.run(_run_with_signals(Supervisor(trial, directory / f"seed-{seed}-repeat-{repeat + 1}")))
                    results.append(result)
                    atomic_json(directory / "batch.json", results)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return 1 if any(result["status"] in FAILED_STATUSES for result in results) else 0
        elif args.command == "status":
            print(json.dumps(run_status(args.run_dir), ensure_ascii=False, indent=2))
        elif args.command == "report":
            manifest = json.loads((args.run_dir / "manifest.json").read_text())
            print(json.dumps(write_report(args.run_dir, task_config=manifest["config"]["experiment"]["task"]), ensure_ascii=False, indent=2))
        elif args.command == "serve":
            serve(args.run_dir, args.port)
        elif args.command == "resume" and not is_active(args.run_dir):
            manifest = json.loads((args.run_dir / "manifest.json").read_text())
            result = asyncio.run(_run_with_signals(Supervisor(manifest["config"], args.run_dir, resume=True)))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result["status"] in FAILED_STATUSES else 0
        else:
            command = "freeze" if args.command == "pause" and args.freeze_world else args.command
            control_id = request_control(args.run_dir, command, **({"checkpoint_id": args.checkpoint_id} if command == "restore" else {}))
            print(json.dumps({"command": command, "status": "queued", "control_id": control_id}))
        return 0
    except _OperatorInterrupted as exc:
        print(f"Experiment cleanup finished after signal {exc.signum}", file=sys.stderr)
        return 128 + exc.signum
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError, KeyError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
