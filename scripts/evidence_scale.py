"""Reproducible ledger/report scale check; no Minecraft, models or network calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import tempfile
import time
import tracemalloc
from datetime import datetime, timezone

from minecraft_harness.evaluation import IncrementalEvaluator
from minecraft_harness.observer import build_report
from minecraft_harness.recorder import Recorder, atomic_json


def rss_bytes():
    try:
        line = next(line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:'))
        return int(line.split()[1]) * 1024
    except (OSError, StopIteration):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events',type=int,default=100000)
    parser.add_argument('--payload-bytes',type=int,default=1024)
    parser.add_argument('--actor',default='player1')
    parser.add_argument('--output',type=Path,default=Path('validation/evidence-scale.json'))
    parser.add_argument('--temp-parent',type=Path)
    args=parser.parse_args()
    if args.events < 1 or not 0 <= args.payload_bytes <= 65536:
        parser.error('positive events and payload-bytes in [0,65536] required')
    root=Path(__file__).resolve().parents[1]
    sources=[root/'src/minecraft_harness'/relative for relative in ('recorder.py','budget.py','usage.py','evaluation/scoring.py','evaluation/collector.py','evaluation/logs.py','observer/report.py','observer/analysis.py')]
    result={'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),'diagnostic_only':True,
            'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,
            'events':args.events,'payload_padding_bytes':args.payload_bytes,'actor_id':args.actor,
            'source_sha256':{str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}}
    with tempfile.TemporaryDirectory(prefix='minecraft-evidence-scale-',dir=args.temp_parent) as temporary:
        run=Path(temporary)
        recorder=Recorder(run)
        payload=json.dumps({'status':'succeeded','diagnostic_detail':'x'*args.payload_bytes})
        started=time.monotonic()
        try:
            with recorder.db:
                recorder.db.executemany('INSERT INTO events(kind,actor_id,source,received_at,world_epoch,runtime_epoch,execution_id,payload_json,monotonic_elapsed_ms) VALUES(?,?,?,?,?,?,?,?,?)',
                    (('execution_finished',args.actor,'harness','2026-10-21T00:00:00+00:00',0,0,f'job-{index}',payload,index) for index in range(args.events)))
        finally:
            recorder.close()
        result['write_seconds']=round(time.monotonic()-started,6)
        result['ledger_bytes']=(run/'events.sqlite3').stat().st_size
        tracemalloc.start()
        before=rss_bytes()
        started=time.monotonic()
        report=build_report(run)
        result['report']={'elapsed_seconds':round(time.monotonic()-started,6),
                          'python_peak_bytes':tracemalloc.get_traced_memory()[1],
                          'rss_before_bytes':before,'rss_after_bytes':rss_bytes(),
                          'execution_count':report['metrics']['execution_count'],
                          'execution_terminal_counts':report['metrics']['execution_terminal_counts']}
        tracemalloc.reset_peak()
        before=rss_bytes()
        scorer=IncrementalEvaluator(run,'observe_only')
        started=time.monotonic()
        score=scorer.evaluate()
        result['resume_score']={'elapsed_seconds':round(time.monotonic()-started,6),
                                'python_peak_bytes':tracemalloc.get_traced_memory()[1],
                                'rss_before_bytes':before,'rss_after_bytes':rss_bytes(),
                                'status':score['status'],'retained_snapshot_refs':len(scorer.rows)}
        tracemalloc.stop()
        result['passed']=(report['event_count']==args.events and report['metrics']['execution_count']==args.events
                          and report['metrics']['execution_terminal_counts']=={'succeeded':args.events}
                          and score['status']=='diagnostic_pass')
    atomic_json(args.output,result)
    print(json.dumps({'output':str(args.output.resolve()),**result},ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
