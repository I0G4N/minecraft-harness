#!/usr/bin/env python3
"""Low-overhead end-to-end performance sampler for a running harness run.

It samples the Supervisor/runner/container chain (host load, supervisor RSS,
Docker CPU/memory, bot health RTT), and reads durable ledger summaries
(model/tool latency, event counts, disk growth) without writing to the run.
The output is a JSON list of samples plus a summary; it never prints secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen


def _run(command, timeout=15):
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _ledger_snapshot(run_dir: Path):
    path = run_dir / "events.sqlite3"
    kinds = Counter()
    model_latencies = []
    tool_latencies = []
    latest = 0
    pending = {}
    if not path.is_file():
        return {"kinds": {}, "model_latency_ms": [], "tool_latency_ms": [],
                "latest_seq": 0, "bytes": 0}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as db:
            for seq, kind, payload, received_at in db.execute(
                    "SELECT event_seq, kind, payload_json, received_at FROM events ORDER BY event_seq"):
                latest = max(latest, int(seq))
                kinds[kind] += 1
                try:
                    body = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if kind == "model_request":
                    reservation = body.get("reservation_id")
                    if isinstance(reservation, str):
                        pending[reservation] = _parse_time(received_at)
                elif kind == "model_response":
                    reservation = body.get("reservation_id")
                    if reservation in pending:
                        start = pending.pop(reservation)
                        end = _parse_time(received_at)
                        if start is not None and end is not None:
                            model_latencies.append(max(0.0, (end - start) * 1000))
                elif kind == "tool_result":
                    duration = body.get("duration_ms")
                    if isinstance(duration, (int, float)) and duration >= 0:
                        tool_latencies.append(float(duration))
    except sqlite3.Error:
        pass
    return {"kinds": dict(kinds), "model_latency_ms": model_latencies,
            "tool_latency_ms": tool_latencies, "latest_seq": latest,
            "bytes": path.stat().st_size if path.is_file() else 0}


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _artifacts_bytes(run_dir: Path):
    total = 0
    root = run_dir / "artifacts"
    if root.is_dir():
        for path in root.iterdir():
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                pass
    return total


def _dir_bytes(run_dir: Path):
    total = 0
    for base, _dirs, files in os.walk(run_dir):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                pass
    return total


def _container_stats(project: str, server_container: str):
    names = [server_container]
    listing = _run(["docker", "ps", "--format", "{{.Names}}",
                    "--filter", "label=com.docker.compose.project=" + project])
    names.extend(name for name in listing.split() if name not in names)
    names.extend(name for name in _run(
        ["docker", "ps", "--format", "{{.Names}}",
         "--filter", "label=minecraft-harness.runner=1"]).split()
        if name not in names)
    result = {}
    for name in names:
        raw = _run(["docker", "stats", "--no-stream", "--format",
                    "{{.CPUPerc}}|{{.MemUsage}}|{{.BlockIO}}", name], timeout=10).strip()
        if not raw:
            continue
        parts = raw.split("|")
        result[name] = {
            "cpu": parts[0] if parts else "",
            "mem": parts[1] if len(parts) > 1 else "",
            "block": parts[2] if len(parts) > 2 else "",
        }
    return result


def _supervisor_rss(run_dir: Path):
    marker = str(run_dir)
    for line in _run(["ps", "-eo", "pid,rss,args"]).splitlines()[1:]:
        if marker in line and "minecraft_harness.cli run" in line:
            parts = line.split(None, 2)
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def _health_rtt(urls):
    values = []
    for url in urls:
        started = time.monotonic()
        try:
            with urlopen(url.rstrip("/") + "/health", timeout=0.6) as response:
                response.read(256)
            values.append((time.monotonic() - started) * 1000)
        except Exception:
            values.append(-1.0)
    return values


def _summary(samples, run_dir: Path):
    # Each sample recomputes the full ledger history; use the final snapshot
    # (plus health across samples) to avoid duplicating latency observations.
    model = list((samples[-1].get("model_latency_ms") if samples else None) or [])
    tools = list((samples[-1].get("tool_latency_ms") if samples else None) or [])
    health = []
    kinds = Counter()
    for sample in samples:
        ledger = sample.get("ledger") or {}
        for kind, count in (ledger.get("kinds") or {}).items():
            kinds[kind] = max(kinds[kind], int(count))
        health.extend(value for value in sample.get("health_ms") or [] if value >= 0)
    def stats(values):
        if not values:
            return {"count": 0}
        ordered = sorted(values)
        return {"count": len(values), "p50_ms": round(statistics.median(ordered), 3),
                "p95_ms": round(ordered[max(0, int(len(ordered) * .95) - 1)], 3),
                "max_ms": round(ordered[-1], 3), "mean_ms": round(statistics.fmean(ordered), 3)}
    first = samples[0] if samples else {}
    last = samples[-1] if samples else {}
    container_peak = {"cpu_percent": 0.0, "memory_mib": 0.0}
    for sample in samples:
        for item in (sample.get("containers") or {}).values():
            try:
                cpu = float(str(item.get("cpu", "0%")).rstrip("%"))
            except (TypeError, ValueError):
                cpu = 0.0
            try:
                mem_text = str(item.get("mem", "0B / 0B")).split("/")[0].strip()
                if mem_text.endswith("GiB"):
                    memory = float(mem_text[:-3].strip()) * 1024.0
                elif mem_text.endswith("MiB"):
                    memory = float(mem_text[:-3].strip())
                elif mem_text.endswith("KiB"):
                    memory = float(mem_text[:-3].strip()) / 1024.0
                else:
                    memory = float(mem_text.rstrip("B").strip() or 0) / (1024.0 ** 2)
            except (TypeError, ValueError):
                memory = 0.0
            container_peak["cpu_percent"] = max(container_peak["cpu_percent"], cpu)
            container_peak["memory_mib"] = max(container_peak["memory_mib"], memory)
    return {
        "samples": len(samples),
        "observed_seconds": round((samples[-1]["at"] - samples[0]["at"]), 3) if samples else 0,
        "run_dir": str(run_dir),
        "container_peak": container_peak,
        "model_latency": stats(model),
        "tool_duration": stats(tools),
        "bot_health_rtt": stats(health),
        "max_supervisor_rss_bytes": max((s.get("supervisor_rss_bytes") or 0 for s in samples), default=0),
        "ledger_bytes_growth": (last.get("ledger_bytes", 0) - first.get("ledger_bytes", 0)),
        "run_bytes_growth": (last.get("run_bytes", 0) - first.get("run_bytes", 0)),
        "event_counts_seen": dict(kinds),
        "container_samples": [s.get("containers") for s in samples[-3:]],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--server-container", required=True)
    parser.add_argument("--duration", type=float, default=360.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    config = manifest.get("config") or {}
    urls = list((config.get("runtime", {}).get("urls") or {}).values())
    samples = []
    deadline = time.time() + max(1.0, args.duration)
    while time.time() < deadline:
        ledger = _ledger_snapshot(run_dir)
        sample = {
            "at": time.time(),
            "loadavg": list(os.getloadavg()),
            "cpu_count": os.cpu_count(),
            "supervisor_rss_bytes": _supervisor_rss(run_dir),
            "health_ms": _health_rtt(urls),
            "ledger": {key: ledger[key] for key in ("kinds", "latest_seq", "bytes")},
            "model_latency_ms": ledger["model_latency_ms"],
            "tool_latency_ms": ledger["tool_latency_ms"],
            "ledger_bytes": ledger["bytes"],
            "artifacts_bytes": _artifacts_bytes(run_dir),
            "run_bytes": _dir_bytes(run_dir),
            "containers": _container_stats(args.project, args.server_container),
        }
        samples.append(sample)
        time.sleep(max(0.0, args.interval))
    summary = _summary(samples, run_dir)
    args.out.write_text(json.dumps({"summary": summary, "samples": samples},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
