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
    stderr_tails = []
    for row in con.execute("SELECT kind, payload_json FROM events"):
        kinds[row["kind"]] += 1
        if row["kind"] == "runtime_exit":
            try:
                payload = json.loads(row["payload_json"]).get("payload") or {}
            except ValueError:
                continue
            if payload.get("signal"):
                signals[str(payload["signal"])] += 1
            if payload.get("stderr_tail"):
                stderr_tails.append({"signal": payload.get("signal"), "tail": str(payload["stderr_tail"])[:2000]})
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
        "runtime_exit_stderr_tails": stderr_tails[:20],
        "budget": budget.get("budget"),
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
            "last_storage": storage[-1] if storage else None,
        },
        "server_log": server,
        "flags": {
            "bot_kicked": kinds["bot_kicked"],
            "sigabrt": signals.get("SIGABRT", 0),
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
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(out), "status": result["status"], "flags": result["flags"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
