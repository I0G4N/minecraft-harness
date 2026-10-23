#!/usr/bin/env python3
"""Summarize the read-only Docker container metrics CSV for one run."""
from __future__ import annotations
import argparse, csv, json, re
from collections import defaultdict
from pathlib import Path

MEM = re.compile(r"([0-9.]+)\s*([KMGT]iB)\s*/\s*([0-9.]+)\s*([KMGT]iB)")
SCALE = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "TiB": 1024.0 ** 2}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("csv", type=Path)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    rows = list(csv.DictReader(a.csv.open()))
    samples = sorted({r["timestamp"] for r in rows})
    per = defaultdict(lambda: {"samples": 0, "mem_mib_max": 0.0, "mem_pct_max": 0.0, "cpu_max": 0.0, "pids_max": 0})
    for r in rows:
        name = r["container"]
        item = per[name]
        item["samples"] += 1
        m = MEM.match((r.get("mem_usage") or "").replace("\t", " "))
        if m:
            value = float(m.group(1)) * SCALE[m.group(2)]
            limit = float(m.group(3)) * SCALE[m.group(4)]
            item["mem_mib_max"] = round(max(item["mem_mib_max"], value), 2)
            item["mem_limit_mib"] = round(limit, 2)
        try:
            item["mem_pct_max"] = round(max(item["mem_pct_max"], float((r.get("mem_percent") or "0").rstrip("%"))), 2)
        except ValueError:
            pass
        try:
            item["cpu_max"] = round(max(item["cpu_max"], float((r.get("cpu_percent") or "0").rstrip("%"))), 2)
        except ValueError:
            pass
        try:
            item["pids_max"] = max(item["pids_max"], int(r.get("pids") or 0))
        except ValueError:
            pass
    worker_mem = [v["mem_mib_max"] for n, v in per.items() if n.startswith("mc-worker-")]
    result = {
        "csv": str(a.csv),
        "samples": len(samples),
        "first_sample": samples[0] if samples else None,
        "last_sample": samples[-1] if samples else None,
        "containers": dict(sorted(per.items())),
        "worker_mem_mib_max": max(worker_mem) if worker_mem else None,
        "worker_mem_mib_min_max": min(worker_mem) if worker_mem else None,
        "server_mem_mib_max": per.get("paper8r1-server", per.get("minecraft-paper-server", {})).get("mem_mib_max"),
    }
    out = a.output or (a.csv.parent / "summary.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(out), "samples": len(samples), "workers": len(worker_mem),
                      "worker_max_mib": result["worker_mem_mib_max"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
