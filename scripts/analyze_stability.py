#!/usr/bin/env python3
"""Read-only stability analysis for one run directory.

Emits a JSON summary used after each long-run round.  It never mutates the
ledger or invents unknown usage: missing values stay null.
"""
from __future__ import annotations
import argparse, json, os, sqlite3
from collections import Counter
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument("--server-log", type=Path)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    run = a.run_dir.resolve()
    db = run / "events.sqlite3"
    if not db.is_file():
        raise SystemExit(f"no ledger: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    kinds = Counter()
    signals = Counter()
    reset_reasons = Counter()
    stderr_tails = []
    actor_events = Counter()
    execution_status = Counter()
    latencies = []
    session_samples = []
    for row in con.execute("SELECT kind, payload_json FROM events"):
        kinds[row["kind"]] += 1
        try:
            raw = json.loads(row["payload_json"])
        except ValueError:
            raw = {}
        body = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        actor = raw.get("actor_id") or body.get("agent_id") or body.get("actor_id")
        if actor:
            actor_events[str(actor)] += 1
        if row["kind"] == "execution_finished":
            status = body.get("status")
            if status:
                execution_status[str(status)] += 1
        elif row["kind"] == "model_response":
            latency = body.get("latency_ms")
            if isinstance(latency, (int, float)):
                latencies.append(float(latency))
        elif row["kind"] == "pi_session_sample":
            session_samples.append(body)
        if row["kind"] == "runtime_exit":
            payload = body
            if payload.get("signal"):
                signals[str(payload["signal"])] += 1
            if payload.get("stderr_tail"):
                stderr_tails.append({"signal": payload.get("signal"), "tail": str(payload["stderr_tail"])[:2000]})
        elif row["kind"] == "runtime_reset":
            reason = body.get("reason")
            reset_reasons[str(reason)] += 1
    def state(key):
        row = con.execute("SELECT value_json FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    resources = []
    for row in con.execute("SELECT payload_json FROM events WHERE kind='resource_sample'"):
        try:
            resources.append(json.loads(row[0]))
        except ValueError:
            pass
    rss = [r["python_rss_bytes"] for r in resources if isinstance(r.get("python_rss_bytes"), int)]
    worker_rss = [v for r in resources if isinstance(r.get("worker_rss_bytes"), dict) for v in r["worker_rss_bytes"].values() if isinstance(v, int)]
    storage = [r.get("storage") for r in resources if isinstance(r.get("storage"), dict)]
    budget = next((json.loads(row[0]) for row in con.execute("SELECT payload_json FROM events WHERE kind='run_finished' LIMIT 1")), {}) if kinds["run_finished"] else {}
    report = {}
    if (run / "reports/report.json").is_file():
        try:
            report = json.loads((run / "reports/report.json").read_text())
        except ValueError:
            pass
    server = {"available": False}
    if a.server_log and a.server_log.is_file():
        text = a.server_log.read_text(errors="replace")
        server = {"available": True,
                  "invalid_move": text.count("Invalid move player packet"),
                  "decoder_exception": text.count("DecoderException"),
                  "moved_too_quickly": text.count("moved too quickly"),
                  "bytes": len(text)}
    result = {
        "run_dir": str(run),
        "status": state("status"),
        "active_wall_seconds": state("active_wall_seconds"),
        "event_count": sum(kinds.values()),
        "events_by_kind": dict(sorted(kinds.items())),
        "runtime_exit_signals": dict(sorted(signals.items())),
        "runtime_reset_reasons": dict(sorted(reset_reasons.items())),
        "runtime_exit_stderr_tails": stderr_tails[:20],
        "budget": budget.get("budget"),
        "model_latency_ms": {
            "count": len(latencies),
            "p50": sorted(latencies)[len(latencies)//2] if latencies else None,
            "p95": sorted(latencies)[max(0, int(len(latencies)*0.95)-1)] if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "execution_status_counts": dict(sorted(execution_status.items())),
        "actor_events": dict(sorted(actor_events.items())),
        "pi_session_samples": session_samples[-3:],
        "report": {
            "run_status": report.get("run_status"),
            "failures": report.get("failures"),
            "metrics": {k: (report.get("metrics") or {}).get(k) for k in
                        ("wall_time_s", "ledger_wall_time_s", "model_call_count", "input_tokens", "output_tokens",
                         "estimated_cost_usd", "execution_count", "submission_status_counts", "execution_terminal_counts",
                         "death_count", "chat_message_count", "program_error_rate", "recovery_count")},
        },
        "resource_samples": {
            "count": len(resources),
            "supervisor_rss_bytes_min": min(rss) if rss else None,
            "supervisor_rss_bytes_max": max(rss) if rss else None,
            "worker_rss_bytes_max": max(worker_rss) if worker_rss else None,
            "first_storage": storage[0] if storage else None,
            "last_storage": storage[-1] if storage else None,
            "storage_growth_bytes": ((storage[-1].get("measured_run_bytes") or 0) - (storage[0].get("measured_run_bytes") or 0)) if len(storage) >= 2 else None,
        },
        "server_log": server,
        "flags": {
            "bot_kicked": kinds["bot_kicked"],
            "sigabrt": signals.get("SIGABRT", 0),
            "worker_fatal": reset_reasons.get("worker_fatal", 0),
            "bot_death": kinds["bot_death"],
            "model_request": kinds["model_request"],
            "model_response": kinds["model_response"],
            "runtime_ready": kinds["runtime_ready"],
            "execution_succeeded": kinds["execution_succeeded"],
            "execution_failed": kinds["execution_failed"],
            "cleanup_failed": kinds["cleanup_failed"],
            "finalization_failed": kinds["finalization_failed"],
            "runtime_shutdown_retry": kinds["runtime_shutdown_retry"],
            "runtime_shutdown_unconfirmed": kinds["runtime_shutdown_unconfirmed"],
            "agent_degraded": kinds["agent_degraded"],
            "observer_degraded": kinds["observer_degraded"],
            "storage_alert": kinds["storage_alert"],
            "model_failed": kinds["model_failed"],
        },
    }
    out = a.output or (run / "reports" / "stability-analysis.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(out), "status": result["status"], "flags": result["flags"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
