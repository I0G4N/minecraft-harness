#!/bin/bash
# Wait for a round's Supervisor to exit, archive server log, run analyses.
set -u
D=/ssd_data/caizihao/minecraft/minecraft-harness
PROJECT=${1:?project}; RUN=${2:?run dir}; SERVER_LOG=${3:?server log}
for i in $(seq 1 260); do
  state=$(docker inspect -f '{{.State.Status}}' "${PROJECT}-supervisor" 2>/dev/null || echo missing)
  [ "$state" != running ] && { echo "SUPERVISOR_STATE=$state at $(date -Is)"; break; }
  sleep 20
done
mkdir -p "$RUN/diagnostics"
[ -f "$SERVER_LOG" ] && cp "$SERVER_LOG" "$RUN/diagnostics/server-latest.log"
python3 "$D/scripts/analyze_stability.py" "$RUN" --server-log "$RUN/diagnostics/server-latest.log" || true
[ -f "$RUN/metrics/containers.csv" ] && python3 "$D/scripts/analyze_container_metrics.py" "$RUN/metrics/containers.csv" || true
python3 - "$RUN/events.sqlite3" <<'PY'
import json,sqlite3,sys
con=sqlite3.connect(sys.argv[1])
row=con.execute("select value_json from state where key='status'").fetchone()
print('FINAL_STATUS',row[0] if row else None)
for kind in ('run_finished','cleanup_failed','finalization_failed','runtime_shutdown_retry'):
 print(kind,con.execute('select count(*) from events where kind=?',(kind,)).fetchone()[0])
PY
