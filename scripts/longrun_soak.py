"""Bounded offline Supervisor soak with real Node processes and fault injection.

Run from the harness directory: uv run python scripts/longrun_soak.py
This exercises infrastructure only. It never starts Minecraft or calls a model.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import re
import signal
import sqlite3
import sys
import time
import tracemalloc
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from minecraft_harness.models import ModelResponse
from minecraft_harness.recorder import atomic_json
from minecraft_harness.supervisor import Supervisor, request_control


def process_status(pid):
    try:
        values = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())
        return {"pid": pid, "running": not values["State"].lstrip().startswith("Z"),
                "rss_bytes": int(values.get("VmRSS", "0 kB").split()[0]) * 1024}
    except (FileNotFoundError, ProcessLookupError):
        return {"pid": pid, "running": False, "rss_bytes": 0}


def descendants(pid):
    result, stack = set(), [pid]
    while stack:
        parent = stack.pop()
        try:
            children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        for child in map(int, children):
            if child not in result:
                result.add(child)
                stack.append(child)
    return result


class PacedFixtureModel:
    """A mechanism fixture, deliberately containing no Minecraft strategy."""

    def __init__(self, pace_s):
        self.pace_s, self.total_calls = pace_s, 0

    async def complete(self, messages, tools):
        await asyncio.sleep(self.pace_s)
        self.total_calls += 1
        match = re.search(r"(?:Agent: |independent coding agent )([A-Za-z0-9_]+)", messages[0]["content"])
        if not match:
            raise ValueError("Offline fixture could not identify its configured player")
        agent = match.group(1)
        marker = f"offline-soak:{agent}:{self.total_calls}"
        source = (f"const marker = {json.dumps(marker)}; globals.soakTicks = (globals.soakTicks || 0) + 1; "
                  "console.log('offline_soak_tick', marker); "
                  + ("bot.chat(marker); " if self.total_calls % 20 == 0 else "")
                  + "return {marker, tick: globals.soakTicks, username: bot.username, position: bot.entity.position};")
        return ModelResponse(text="Offline infrastructure fixture; observe and record a unique tick. " * 8,
                             tool_calls=[{"id": f"tick-{self.total_calls}", "type": "function", "function": {
                                 "name": "mc_execute", "arguments": json.dumps({"source": source, "timeout_ms": 3000})}}],
                             usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


async def wait_until(predicate, task, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if task.done():
            raise RuntimeError(f"Supervisor ended early: {await task}")
        await asyncio.sleep(.05)
    raise TimeoutError("offline soak condition exceeded its bounded wait")


async def apply_control(run_dir, command, task, timeout=20):
    identifier = request_control(run_dir, command)

    def completed():
        with closing(sqlite3.connect(run_dir / "events.sqlite3")) as connection:
            row = connection.execute("SELECT state FROM controls WHERE control_id=?", (identifier,)).fetchone()
            if row and row[0] in {"failed", "interrupted"}:
                raise RuntimeError(f"Control {command} {row[0]}")
            return bool(row and row[0] == "applied")

    await wait_until(completed, task, timeout)
    return identifier


def ledger_summary(run_dir):
    with closing(sqlite3.connect(run_dir / "events.sqlite3")) as connection:
        kinds = dict(connection.execute("SELECT kind,COUNT(*) FROM events GROUP BY kind"))
        state = {key: json.loads(value) for key, value in connection.execute("SELECT key,value_json FROM state")}
        duplicates = list(connection.execute("""SELECT actor_id,execution_id,COUNT(*) FROM events
            WHERE kind='execution_running' AND source='bot_runtime' GROUP BY actor_id,execution_id HAVING COUNT(*)>1"""))
        sequences = {}
        for agent, encoded in connection.execute("SELECT agent_id,value_json FROM submissions"):
            sequences.setdefault(agent, []).append(json.loads(encoded)["request_sequence"])
        sequence_checks = {agent: {"count": len(values), "maximum": max(values),
                                   "unique": len(set(values)) == len(values),
                                   "contiguous": sorted(values) == list(range(1, len(values) + 1))}
                           for agent, values in sequences.items()}
        cursors = [{"agent": row[0], "session_id": row[1], "cursor": row[2]}
                   for row in connection.execute("SELECT * FROM transport_cursors ORDER BY agent_id,session_id")]
        controls = [dict(zip(("command", "state", "count"), row)) for row in connection.execute(
            "SELECT command,state,COUNT(*) FROM controls GROUP BY command,state")]
    return {"event_counts": kinds, "state": state, "duplicate_running_executions": duplicates,
            "submission_sequences": sequence_checks, "transport_cursors": cursors, "controls": controls}


async def run(args):
    args.run_dir.mkdir(parents=True, exist_ok=False)
    if args.trace_memory:
        tracemalloc.start(1)
    config = {"experiment": {"name": "offline-longrun-soak", "task": "observe_only", "goal": "Offline infrastructure soak; observe a diagnostic fake bot."},
              "agents": {"count": 4, "max_turns": 1_000_000, "context_chars": 12000, "max_no_progress_turns": 100,
                         "max_event_failures": 30, "retry_base_s": .05, "retry_max_s": 1},
              "model": {"kind": "fake", "timeout_s": 5},
              "runtime": {"mode": "fake", "program_timeout_default_s": 3, "program_timeout_max_s": 5,
                          "request_timeout_s": 2, "heartbeat_interval_s": .5, "lease_timeout_s": 8},
              "budgets": {"wall_time_minutes": (args.duration_s + 120) / 60, "total_model_calls": 1000000},
              "server": {"save_interval_s": 10},
              "supervision": {"poll_interval_s": .05, "evaluation_interval_s": 1, "resource_check_interval_s": 2,
                              "recovery_attempts": 5, "recovery_base_s": .1, "recovery_max_s": 1, "shutdown_timeout_s": 20}}
    supervisor = Supervisor(config, args.run_dir)
    samples, injections, pause_checks, tracked, models = [], [], [], set(), []
    started_at = datetime.now(timezone.utc).isoformat()
    warm_snapshot, trace, runtime_limits = None, {}, {}

    def build(_):
        model = PacedFixtureModel(args.pace_s)
        models.append(model)
        return model

    task = None
    result, failure, final_metrics = None, None, []
    with patch("minecraft_harness.supervisor.build_model", side_effect=build):
        task = asyncio.create_task(supervisor.run(), name="offline-soak-supervisor")
        try:
            await wait_until(lambda: supervisor.status == "running", task)
            start = time.monotonic()
            next_sample, next_progress, crash_index, pause_index = 0.0, 0.0, 0, 0
            crash_times = [args.duration_s * fraction for fraction in (.25, .5, .75)]
            pause_times = [args.duration_s * fraction for fraction in (.1, .85)]
            while time.monotonic() - start < args.duration_s:
                elapsed = time.monotonic() - start
                if args.trace_memory and warm_snapshot is None and elapsed >= min(30, args.duration_s / 5):
                    gc.collect()
                    warm_snapshot = tracemalloc.take_snapshot()
                    trace["warm_seconds"] = round(elapsed, 3)
                    trace["warm_current_bytes"], trace["warm_peak_bytes"] = tracemalloc.get_traced_memory()
                    trace["warm_rss_bytes"] = process_status(os.getpid())["rss_bytes"]
                if task.done():
                    raise RuntimeError(f"Supervisor ended before the soak duration: {await task}")
                gateway = supervisor.gateway
                if gateway.process:
                    tracked.add(gateway.process.pid)
                    tracked.update(descendants(gateway.process.pid))
                if elapsed >= next_sample:
                    sample = {"seconds": round(elapsed, 3), "python_rss_bytes": process_status(os.getpid())["rss_bytes"],
                              "budget_calls": supervisor.budget.state["calls"], "world_epoch": gateway.world_epoch,
                              "status": supervisor.status, "runtime_pid": gateway.process.pid if gateway.process else None}
                    if supervisor.status == "running" and not gateway.fault:
                        try:
                            health = await gateway._request("GET", "/health")
                            metrics = health["session_metrics"]
                            runtime_limits = health["limits"]
                            sample.update(node_rss_bytes=health["memory"]["rss"],
                                          node_buffered_events=sum(value["buffers"]["events"] for value in metrics),
                                          node_buffered_event_bytes=sum(value["buffers"]["event_bytes"] for value in metrics),
                                          node_retained_executions=sum(value["buffers"]["executions"] for value in metrics),
                                          max_worker_rss_bytes=max((value["worker_rss_bytes"] or 0 for value in metrics), default=0))
                            tracked.update(item["worker_pid"] for item in metrics if item.get("worker_pid"))
                        except Exception as exc:
                            sample["health_error"] = type(exc).__name__
                    samples.append(sample)
                    next_sample = elapsed + args.sample_interval_s
                if pause_index < len(pause_times) and elapsed >= pause_times[pause_index] and supervisor.status == "running":
                    await apply_control(args.run_dir, "pause", task)
                    before = supervisor.budget.state["calls"]
                    await asyncio.sleep(.3)
                    after = supervisor.budget.state["calls"]
                    pause_checks.append({"seconds": round(elapsed, 3), "calls_before": before, "calls_after": after, "stable": before == after})
                    await apply_control(args.run_dir, "resume", task)
                    pause_index += 1
                if crash_index < len(crash_times) and elapsed >= crash_times[crash_index] and supervisor.status == "running":
                    old_pid = gateway.process.pid
                    before = supervisor.budget.state["calls"]
                    tracked.update(descendants(old_pid))
                    os.kill(old_pid, signal.SIGKILL)
                    injection = {"seconds": round(elapsed, 3), "killed_pid": old_pid, "calls_before": before}
                    injections.append(injection)
                    await wait_until(lambda: (supervisor.status == "running" and gateway.process and gateway.process.pid != old_pid and not gateway.fault), task, 30)
                    injection.update(new_pid=gateway.process.pid, calls_after=supervisor.budget.state["calls"],
                                     recovered_seconds=round(time.monotonic() - start, 3))
                    crash_index += 1
                if elapsed >= next_progress:
                    print(json.dumps({"progress_seconds": round(elapsed, 1), "calls": supervisor.budget.state["calls"],
                                      "recoveries": len([value for value in injections if "new_pid" in value]),
                                      "python_rss_mib": round(process_status(os.getpid())["rss_bytes"] / 1024 ** 2, 1)}), flush=True)
                    next_progress = elapsed + 10
                await asyncio.sleep(.05)
            await apply_control(args.run_dir, "pause", task)
            await asyncio.sleep(.3)
            for agent in supervisor.config["agents"]["ids"]:
                await supervisor.gateway.read_events(agent)
            health = await supervisor.gateway._request("GET", "/health")
            final_metrics = health["session_metrics"]
            if args.trace_memory and warm_snapshot:
                gc.collect()
                ending_snapshot = tracemalloc.take_snapshot()
                trace["ending_current_bytes"], trace["ending_peak_bytes"] = tracemalloc.get_traced_memory()
                trace["ending_rss_bytes"] = process_status(os.getpid())["rss_bytes"]
                changes = ending_snapshot.compare_to(warm_snapshot, "lineno")
                trace["net_live_growth_bytes"] = sum(value.size_diff for value in changes)
                trace["top_live_growth"] = [{"bytes_diff": value.size_diff, "count_diff": value.count_diff,
                                              "location": str(value.traceback)} for value in changes[:25]]
            if args.trace_memory:
                # The measurement window ends above. Instrumenting the large
                # final JSONL export/report can multiply cleanup time and turn
                # the fixture's unchanged 45-second wait into a false failure.
                tracemalloc.stop()
                trace['stopped_before_finalization'] = True
            tracked.update(descendants(supervisor.gateway.process.pid))
            request_control(args.run_dir, "stop")
            result = await asyncio.wait_for(task, 45)
        except BaseException as exc:
            if args.trace_memory:
                tracemalloc.stop()
                trace['stopped_before_cleanup'] = True
            failure = {"type": type(exc).__name__, "message": str(exc)}
            if task and not task.done():
                task.cancel()
                try:
                    result = await asyncio.wait_for(task, 45)
                except BaseException as cleanup:
                    failure["cleanup"] = type(cleanup).__name__ + ": " + str(cleanup)
        finally:
            if task and task.done() and result is None:
                try:
                    result = task.result()
                except BaseException as exc:
                    failure = failure or {"type": type(exc).__name__, "message": str(exc)}
    await asyncio.sleep(1)
    remaining = [process_status(pid) for pid in sorted(tracked) if process_status(pid)["running"]]
    ledger = ledger_summary(args.run_dir)
    budget = ledger["state"].get("budget", {})
    warm_samples = [sample for sample in samples if sample["seconds"] >= min(30, args.duration_s / 5)]
    memory = {"python_peak_rss_bytes": max((sample["python_rss_bytes"] for sample in samples), default=0),
              "node_peak_rss_bytes": max((sample.get("node_rss_bytes", 0) for sample in samples), default=0),
              "python_after_warmup_growth_bytes": (warm_samples[-1]["python_rss_bytes"] - warm_samples[0]["python_rss_bytes"]) if warm_samples else 0}
    tail_samples = [sample["python_rss_bytes"] for sample in samples if sample["seconds"] >= args.duration_s - min(60, args.duration_s / 3)]
    memory["python_final_window_rss_range_bytes"] = max(tail_samples, default=0) - min(tail_samples, default=0)
    checks = {"duration_completed": failure is None,
              "three_runtime_crashes_recovered": len([value for value in injections if "new_pid" in value]) == 3,
              "recovery_events_durable": ledger["event_counts"].get("runtime_recovery_completed", 0) >= 3,
              "pause_admission_stable": len(pause_checks) == 2 and all(value["stable"] for value in pause_checks),
              "budget_never_reset": all(value["calls_before"] <= value.get("calls_after", -1) for value in injections),
              "all_admissions_recorded": budget.get("calls") == ledger["event_counts"].get("model_admitted", 0),
              "all_admissions_settled": budget.get("calls") == ledger["event_counts"].get("model_usage", 0) and not budget.get("pending"),
              "no_duplicate_execution_dispatch": not ledger["duplicate_running_executions"],
              "request_sequences_durable": len(ledger["submission_sequences"]) == 4 and all(
                  value["unique"] and value["contiguous"] for value in ledger["submission_sequences"].values()),
              "event_buffers_fully_acknowledged_before_stop": len(final_metrics) == 4 and all(
                  value["event_cursor"] == value["acked_cursor"] and value["buffers"]["events"] == 0 for value in final_metrics),
              "no_live_child_processes_after_stop": not remaining,
              "rss_within_observed_limits": memory["python_peak_rss_bytes"] < 512 * 1024 ** 2 and memory["node_peak_rss_bytes"] < 768 * 1024 ** 2,
              "python_growth_after_warmup_under_64mib": memory["python_after_warmup_growth_bytes"] < 64 * 1024 ** 2,
              "final_memory_window_range_under_16mib": memory["python_final_window_rss_range_bytes"] < 16 * 1024 ** 2,
              "stopped_cleanly": bool(result and result["status"] == "stopped")}
    if args.trace_memory:
        checks["traced_live_growth_after_warmup_under_8mib"] = bool(trace) and trace.get("net_live_growth_bytes", 16 * 1024 ** 2) < 8 * 1024 ** 2
    summary = {"kind": "observed_supervisor_offline_soak", "started_at_utc": started_at,
               "duration_requested_seconds": args.duration_s, "pace_seconds": args.pace_s,
               "minecraft_server_started": False, "external_model_called": False,
               "status": "passed" if all(checks.values()) else "failed", "checks": checks,
               "failure": failure, "result": result, "fault_injections": injections,
               "pause_checks": pause_checks, "model_completed_calls": sum(model.total_calls for model in models),
               "memory": memory,
               "tracemalloc": trace, "runtime_limits": runtime_limits,
               "final_runtime_metrics_before_stop": final_metrics, "remaining_processes": remaining,
               "tracked_processes": sorted(tracked), "ledger": ledger, "samples": samples,
               "scope": "Offline 4-agent orchestration, bounded state, crash recovery, durable budget and process lifecycle; no Minecraft capability or day-scale claim."}
    atomic_json(args.output, summary)
    print(json.dumps({"status": summary["status"], "checks": checks, "failure": failure,
                      "calls": budget.get("calls"), "output": str(args.output)}, indent=2), flush=True)
    return 0 if summary["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-s", type=float, default=300)
    parser.add_argument("--pace-s", type=float, default=.05)
    parser.add_argument("--sample-interval-s", type=float, default=1)
    parser.add_argument("--trace-memory", action="store_true", help="Compare GC-clean warm and ending live Python allocations; adds tracing overhead")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parser.add_argument("--run-dir", type=Path, default=Path("runs") / ("longrun-soak-" + stamp))
    parser.add_argument("--output", type=Path, default=Path("validation") / ("supervisor-soak-" + stamp + ".json"))
    args = parser.parse_args()
    if args.duration_s < 10 or args.pace_s <= 0 or args.sample_interval_s <= 0:
        parser.error("duration must be >=10 and pace/sample intervals must be positive")
    args.run_dir, args.output = args.run_dir.resolve(), args.output.resolve()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
