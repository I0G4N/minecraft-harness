#!/usr/bin/env python3
"""Offline end-to-end aligned lifecycle/Observer fixture; contains no game strategy."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch

from minecraft_harness.models import ModelResponse
from minecraft_harness.recorder import atomic_json
from minecraft_harness.supervisor import Supervisor, request_control


def tool(identifier, name, **args):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class AlignedFixture:
    """Choose behavior from advertised tool capabilities, never model/role names."""

    def __init__(self, pace_s=.03):
        self.pace_s = pace_s
        self.calls = self.player_decisions = self.active = self.peak_active = 0
        self.observer = False

    async def complete(self, messages, schemas):
        self.calls += 1
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(self.pace_s)
            names = {item["function"]["name"] for item in schemas}
            if "context_handoff" in names:
                calls = [tool(f"handoff-{self.calls}", "context_handoff", objective="Record synthetic lifecycle checks",
                              plans=["Inspect pending handles and registered task status before further actions"],
                              verified_facts=["Diagnostic fake-game observations require fresh verification after epoch change"],
                              failed_attempts=[], source_paths=["fixture.js"], unresolved_tasks=["Inspect mc_pending and mc_jobs"])]
                text = ""
            elif "observer_report" in names:
                self.observer = True
                refs = []
                for message in reversed(messages):
                    content = message.get("content") or ""
                    if content.startswith("Incremental read-only evidence: "):
                        page = json.loads(content.removeprefix("Incremental read-only evidence: "))
                        refs = [page["events"][0]["event_seq"]] if page["events"] else []
                        break
                findings = ([{"status": "insufficient_evidence", "claim": "Synthetic runtime evidence cannot establish Minecraft capability success.",
                              "evidence_refs": refs}] if refs else [])
                calls = [tool(f"invalid-{self.calls}", "infra_issue", kind="runtime_recovery", reason="Negative reference probe",
                              evidence_refs=[999999999]), tool(f"review-{self.calls}", "observer_report", findings=findings)]
                text = ""
            else:
                self.player_decisions += 1
                index = self.player_decisions
                source = ("if(!globals.fixtureJob){globals.fixtureJob=jobs.start(async()=>{"
                          "while(!isCancelled()){globals.fixturePulse=(globals.fixturePulse||0)+1;"
                          "await new Promise(r=>setTimeout(r,25));}},{ttl_ms:60000});}"
                          "globals.fixtureTicks=(globals.fixtureTicks||0)+1;"
                          "return {tick:globals.fixtureTicks,pulse:globals.fixturePulse||0,job:globals.fixtureJob};")
                calls = [tool(f"tick-{self.calls}", "mc_execute", source=source, timeout_ms=2000),
                         tool(f"notes-{self.calls}", "workspace_write", path="fixture.js", content=f"// Synthetic generation {index}\n" + source),
                         tool(f"jobs-{self.calls}", "mc_jobs")]
                text = "Synthetic lifecycle fixture; preserve unknown handles and reobserve after reset. " * 40
            return ModelResponse(text=text, tool_calls=calls,
                                 usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        finally:
            self.active -= 1


async def run_soak(duration_s: float, run_dir: Path, output: Path | None = None):
    config = {"experiment": {"profile": "paper_aligned", "task": "observe_only", "seed": None,
                              "goal": "Synthetic lifecycle and read-only Observer validation"},
              "agents": {"count": 4, "max_turns": 100000, "context_chars": 12000,
                         "max_no_progress_turns": 100, "retry_base_s": .01},
              "runtime": {"mode": "fake", "lifecycle": "paper_aligned", "request_timeout_s": 2,
                          "heartbeat_interval_s": .3, "lease_timeout_s": 8,
                          "program_timeout_default_s": 2, "program_timeout_max_s": 5},
              "model": {"kind": "fake", "timeout_s": 5},
              "observer": {"mode": "llm", "interval_s": .15}, "server": {"save_interval_s": 300},
              "budgets": {"wall_time_minutes": (duration_s + 120) / 60, "total_model_calls": 100000},
              "supervision": {"poll_interval_s": .03, "evaluation_interval_s": .5, "shutdown_timeout_s": 20}}
    supervisor = Supervisor(config, run_dir)
    models = []
    def build(_):
        model = AlignedFixture()
        models.append(model)
        return model
    started = time.monotonic()
    failure = None
    result = None
    with patch("minecraft_harness.supervisor.build_model", side_effect=build):
        task = asyncio.create_task(supervisor.run())
        try:
            deadline = time.monotonic() + 20
            while supervisor.status != "running":
                if task.done():
                    raise RuntimeError(f"Supervisor ended before running: {await task}")
                if time.monotonic() > deadline:
                    raise TimeoutError("Supervisor startup exceeded 20 seconds")
                await asyncio.sleep(.02)
            run_started = time.monotonic()
            while time.monotonic() - run_started < duration_s:
                if task.done():
                    raise RuntimeError(f"Supervisor ended during diagnostic soak: {await task}")
                await asyncio.sleep(.05)
            request_control(run_dir, "stop")
            result = await asyncio.wait_for(task, 25)
        except BaseException as exc:
            failure = type(exc).__name__
            if not task.done():
                task.cancel()
            try:
                result = await task
            except asyncio.CancelledError:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
    with closing(sqlite3.connect(run_dir / "events.sqlite3")) as connection:
        events = [(seq, kind, json.loads(payload)) for seq, kind, payload in connection.execute(
            "SELECT event_seq,kind,payload_json FROM events ORDER BY event_seq")]
        state = {key: json.loads(value) for key, value in connection.execute("SELECT key,value_json FROM state")}
        duplicate = list(connection.execute("SELECT execution_id,COUNT(*) FROM events WHERE kind='execution_running' GROUP BY execution_id HAVING COUNT(*)>1"))
    counts = Counter(kind for _, kind, _ in events)
    roles = Counter((payload.get("role"), payload.get("purpose")) for _, kind, payload in events if kind == "model_admitted")
    completions = [payload for _, kind, payload in events if kind == "context_handoff"]
    tool_results = [payload for _, kind, payload in events if kind == "tool_result"]
    invalid_issue_errors = 0
    running_job_observations = 0
    observed_background_pulse = 0
    for payload in tool_results:
        if payload.get("tool") == "infra_issue":
            path = run_dir / payload["result_ref"]["path"]
            data = json.loads(path.read_text())
            invalid_issue_errors += data.get("status") == "tool_error"
        elif payload.get("tool") in {"mc_jobs", "mc_execute"}:
            path = run_dir / payload["result_ref"]["path"]
            data = json.loads(path.read_text())
            if payload["tool"] == "mc_jobs":
                running_job_observations += sum(job.get("status") == "running" for job in data.get("jobs", []))
            elif isinstance(data.get("result"), dict):
                observed_background_pulse = max(observed_background_pulse, data["result"].get("pulse", 0))
    checks = {"completed_duration": failure is None,
              "four_players_and_observer": len(models) == 5,
              "no_overlapping_requests_per_agent": all(model.peak_active == 1 for model in models),
              "registered_jobs_were_executed": counts["job_started"] == 4,
              "jobs_survived_foreground_and_kept_running": running_job_observations > 4 and observed_background_pulse > 0,
              "structured_handoffs_complete_or_explicit_shutdown_fallback": bool(completions) and any(item["complete"] for item in completions)
                  and all(item["complete"] or item.get("failure_reason") == "BudgetExhausted" and item.get("stop_requested") is True for item in completions),
              "player_and_observer_model_usage_separate": roles[("player", "agent_decision")] > 0 and roles[("observer", "observer_review")] > 0,
              "compaction_calls_counted": roles[("player", "context_compaction")] > 0,
              "observer_reports_recorded": counts["observer_report"] > 0,
              "invalid_infra_refs_rejected": invalid_issue_errors > 0 and counts["observer_infra_issue"] == 0,
              "unknown_actions_not_replayed": not duplicate,
              "all_admissions_settled": not state["budget"]["pending"] and counts["model_admitted"] == counts["model_usage"],
              "stopped_cleanly": state["status"] == "stopped"}
    summary = {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
               "failure": failure, "diagnostic_only": True, "actual_minecraft": False,
               "paid_model": False, "duration_s": duration_s, "elapsed_s": round(time.monotonic() - started, 3),
               "run_dir": str(run_dir), "created_at": datetime.now(timezone.utc).isoformat(),
               "event_counts": dict(counts), "model_admissions_by_role_purpose": {f"{role}:{purpose}": count for (role, purpose), count in roles.items()},
               "invalid_infra_ref_errors": invalid_issue_errors, "running_job_observations": running_job_observations,
               "maximum_background_pulse": observed_background_pulse,
               "completed_handoffs": sum(item["complete"] for item in completions),
               "shutdown_fallback_handoffs": sum(not item["complete"] and item.get("failure_reason") == "BudgetExhausted" and item.get("stop_requested") is True for item in completions),
               "result": result}
    if output:
        atomic_json(output, summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-s", type=float, default=20)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("validation/paper-lifecycle-soak.json"))
    args = parser.parse_args()
    if args.duration_s < 1:
        parser.error("duration must be at least one second")
    run_dir = (args.run_dir or Path("runs") / ("paper-lifecycle-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    summary = asyncio.run(run_soak(args.duration_s, run_dir, args.output.resolve()))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(0 if summary["status"] == "passed" else 1)
