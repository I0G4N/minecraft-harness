#!/usr/bin/env python3
"""Aggregate per-round stability-analysis.json files into the final report."""
from __future__ import annotations
import argparse, json
from pathlib import Path


def fmt(value, suffix=""):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return f"{value:,}{suffix}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dirs", nargs="+", type=Path)
    p.add_argument("--output", type=Path, default=Path("mc-runs/p8-4round-stability-report.md"))
    p.add_argument("--json-output", type=Path)
    a = p.parse_args()
    rows = []
    for index, run in enumerate(a.run_dirs, 1):
        data_path = run / "reports/stability-analysis.json"
        if not data_path.is_file():
            raise SystemExit(f"missing {data_path}; run analyze_stability.py first")
        data = json.loads(data_path.read_text())
        report = data.get("report") or {}
        metrics = report.get("metrics") or {}
        flags = data.get("flags") or {}
        resources = data.get("resource_samples") or {}
        server = data.get("server_log") or {}
        budget = data.get("budget") or {}
        cost = metrics.get("estimated_cost_usd")
        if cost is None:
            cost = budget.get("cost_usd")
        rows.append({"round": index, "run": str(run), "data": data,
                     "status": data.get("status"), "wall": data.get("active_wall_seconds"),
                     "calls": metrics.get("model_call_count") or flags.get("model_request"),
                     "cost": cost,
                     "kicks": flags.get("bot_kicked"), "sigabrt": flags.get("sigabrt"),
                     "worker_fatal": flags.get("worker_fatal"),
                     "failures": report.get("failures"),
                     "cleanup": flags.get("cleanup_failed"), "finalization": flags.get("finalization_failed"),
                     "storage": (resources.get("last_storage") or {}).get("measured_run_bytes"),
                     "rss": resources.get("supervisor_rss_bytes_max"),
                     "server": server})
    lines = ["# 8-agent paper-aligned 4-round stability report", ""]
    lines.append("| round | status | wall s | calls | cost USD | kicks | SIGABRT | worker_fatal | exec failed | p95 latency ms | report failures | cleanup | storage MiB | supervisor RSS MiB |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |")
    for r in rows:
        storage = r["storage"] / 2**20 if isinstance(r["storage"], int) else None
        rss = r["rss"] / 2**20 if isinstance(r["rss"], int) else None
        flags = r["data"].get("flags") or {}
        latency = (r["data"].get("model_latency_ms") or {}).get("p95")
        execs = r["data"].get("execution_status_counts") or {}
        lines.append(f"| {r['round']} | {r['status']} | {fmt(r['wall'])} | {fmt(r['calls'])} | {fmt(r['cost'])} | "
                     f"{fmt(r['kicks'])} | {fmt(r['sigabrt'])} | {fmt(r['worker_fatal'])} | {fmt(execs.get('failed'))} | {fmt(latency)} | {r['failures']} | "
                     f"{fmt(r['cleanup'])} | {fmt(storage)} | {fmt(rss)} |")
    lines += ["", "## Per-round evidence", ""]
    for r in rows:
        s = r["server"] or {}
        data = r["data"]
        lines += [f"### Round {r['round']}: `{r['run']}`", "",
                  f"- status: `{r['status']}`; wall: {fmt(r['wall'])} s; calls: {fmt(r['calls'])}; cost (budget subtotal if usage was unknown): {fmt(r['cost'])} USD",
                  f"- kicks: {fmt(r['kicks'])}; SIGABRT: {fmt(r['sigabrt'])}; worker_fatal: {fmt(r['worker_fatal'])}; report failures: `{r['failures']}`",
                  f"- cleanup_failed: {fmt(r['cleanup'])}; finalization_failed: {fmt(r['finalization'])}; storage: {fmt(r['storage'])} bytes; supervisor RSS max: {fmt(r['rss'])} bytes",
                  f"- runtime exit signals: `{data.get('runtime_exit_signals')}`; reset reasons: `{data.get('runtime_reset_reasons')}`; execution: `{data.get('execution_status_counts')}`; model latency p95: `{(data.get('model_latency_ms') or {}).get('p95')}` ms",
                  f"- server log: invalid_move={s.get('invalid_move')}, decoder_exception={s.get('decoder_exception')}, moved_too_quickly={s.get('moved_too_quickly')}",
                  ""]
        tails = data.get("runtime_exit_stderr_tails") or []
        if tails:
            lines.append("- worker stderr tails:")
            lines += [f"  - `{t.get('signal')}`: `{str(t.get('tail'))[:300]}`" for t in tails[:3]]
            lines.append("")
    md = "\n".join(lines).rstrip() + "\n"
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(md)
    if a.json_output:
        a.json_output.write_text(json.dumps([{"round": r["round"], "run": r["run"], "status": r["status"],
                                              "data": r["data"]} for r in rows], ensure_ascii=False, indent=2) + "\n")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
