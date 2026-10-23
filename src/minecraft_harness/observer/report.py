"""Deterministic reporting without model calls, bot access or memory writes."""

from __future__ import annotations

from collections import Counter
from contextlib import closing, ExitStack
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import re
import os
import tempfile
import uuid
from typing import Iterable

from ..evaluation import evaluate
from ..evaluation.collector import SERVER_LINE, artifact_bytes, iter_registered_evidence
from .analysis import Analysis


def read_events(run_dir: str | Path) -> list[dict]:
    return list(iter_events(run_dir))


def iter_events(run_dir: str | Path):
    """Read the canonical ledger in SQLite read-only mode, without a Recorder."""
    database = Path(run_dir).resolve() / "events.sqlite3"
    if not database.is_file():
        return
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        for row in connection.execute("SELECT * FROM events ORDER BY event_seq"):
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            yield event
    finally:
        connection.close()


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _timestamp(event):
    try:
        return datetime.fromisoformat(event["received_at"].replace("Z", "+00:00")).timestamp()
    except (ValueError, KeyError, TypeError):
        return None


def _sum_complete(values):
    return sum(values) if values and all(value is not None for value in values) else None


def _execution_status(event):
    kind, payload = event["kind"], event.get("payload", {})
    aliases = {"execution_succeeded": "succeeded", "program_succeeded": "succeeded",
               "execution_failed": "failed", "program_failed": "failed",
               "execution_timed_out": "timed_out", "program_timed_out": "timed_out",
               "execution_cancelled": "cancelled", "program_cancelled": "cancelled",
               "execution_interrupted": "interrupted", "program_interrupted": "interrupted",
               "execution_rejected": "rejected", "program_rejected": "rejected",
               "execution_invalidated": "invalidated", "program_invalidated": "invalidated"}
    if kind in aliases:
        return aliases[kind]
    if kind in ("execution_finished", "program_finished", "execution_completed", "program_completed"):
        return payload.get("status", "succeeded" if kind == "execution_completed" else None)
    return None


def _server_chat_count(run_dir):
    # Log snapshots grow within an epoch. Only its latest snapshot counts; a
    # per-player bot delivery is not a distinct in-world message.
    latest = {}
    segment_epochs = set()
    count = 0
    seen = False
    for row in iter_registered_evidence(run_dir):
        if row["kind"] == "server_log_snapshot":
            latest[row["world_epoch"]] = row
        elif row["kind"] == "server_log_segment":
            seen = True
            segment_epochs.add(row["world_epoch"])
            try:
                document = json.loads(artifact_bytes(run_dir, row["artifact_sha256"]))
                raw = artifact_bytes(run_dir, document["raw_sha256"])
                if document.get("kind") != "server_log_segment" or document["offset_end"] - document["offset_start"] != len(raw):
                    return None
            except (OSError, ValueError, KeyError, TypeError):
                return None
            lines = raw.splitlines(keepends=True)
            if document.get("fragment_start") and lines:
                lines = lines[1:]
            for line in lines:
                if not line.endswith(b"\n"):
                    continue
                match = SERVER_LINE.fullmatch(line.decode("utf-8", errors="replace").rstrip("\r\n"))
                if match and re.match(r"^(?:\[Not Secure\] )?<\w{1,16}> ", match.group("message")):
                    count += 1
    if not latest and not seen:
        return None
    for row in latest.values():
        if row["world_epoch"] in segment_epochs:
            continue
        try:
            document = json.loads(artifact_bytes(run_dir, row["artifact_sha256"]))
            raw = artifact_bytes(run_dir, document["raw_sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        for line in raw.decode("utf-8", errors="replace").splitlines():
            match = SERVER_LINE.fullmatch(line)
            if match and re.match(r"^(?:\[Not Secure\] )?<\w{1,16}> ", match.group("message")):
                count += 1
    return count


def _sql_execution_counts(run_dir):
    database = run_dir / "events.sqlite3"
    if not database.is_file():
        return 0, Counter()
    # SQLite spills DISTINCT/GROUP BY to its own temporary storage. Reporting
    # never retains every historical execution or code payload in Python.
    query = """
        WITH execution_events AS (
          SELECT event_seq,kind,payload_json,
                 COALESCE(execution_id,json_extract(payload_json,'$.execution_id')) AS id
          FROM events WHERE source!='agent_claim' AND kind IN (
            'execution_accepted','program_accepted','execution_submitted','program_submitted',
            'execution_succeeded','program_succeeded','execution_failed','program_failed',
            'execution_timed_out','program_timed_out','execution_cancelled','program_cancelled',
            'execution_interrupted','program_interrupted','execution_rejected','program_rejected',
            'execution_invalidated','program_invalidated',
            'execution_finished','program_finished','execution_completed','program_completed')),
        classified AS (
          SELECT *, CASE
            WHEN kind IN ('execution_succeeded','program_succeeded') THEN 'succeeded'
            WHEN kind IN ('execution_failed','program_failed') THEN 'failed'
            WHEN kind IN ('execution_timed_out','program_timed_out') THEN 'timed_out'
            WHEN kind IN ('execution_cancelled','program_cancelled') THEN 'cancelled'
            WHEN kind IN ('execution_interrupted','program_interrupted') THEN 'interrupted'
            WHEN kind IN ('execution_rejected','program_rejected') THEN 'rejected'
            WHEN kind IN ('execution_invalidated','program_invalidated') THEN 'invalidated'
            WHEN kind='execution_completed' THEN COALESCE(json_extract(payload_json,'$.status'),'succeeded')
            WHEN kind IN ('execution_finished','program_finished','program_completed') THEN json_extract(payload_json,'$.status')
            ELSE NULL END AS status FROM execution_events WHERE id IS NOT NULL),
        terminals AS (SELECT id,MAX(event_seq) AS seq FROM classified WHERE status IS NOT NULL GROUP BY id)
        SELECT '__accepted__' AS status,COUNT(DISTINCT id) AS amount FROM classified
          WHERE status IS NOT NULL OR kind IN ('execution_accepted','program_accepted','execution_submitted','program_submitted')
        UNION ALL
        SELECT c.status,COUNT(*) FROM classified c JOIN terminals t ON c.event_seq=t.seq GROUP BY c.status
    """
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA temp_store=FILE")
        counts = dict(connection.execute(query))
    return counts.pop("__accepted__", 0), Counter(counts)


def _sql_submission_counts(run_dir):
    database = run_dir / "events.sqlite3"
    if not database.is_file():
        return 0, {}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='submissions'").fetchone():
            return None, None
        counts = dict(connection.execute("SELECT COALESCE(json_extract(value_json,'$.status'),'unknown'),COUNT(*) FROM submissions GROUP BY COALESCE(json_extract(value_json,'$.status'),'unknown')"))
    return sum(counts.values()), dict(sorted(counts.items()))


class _Usage:
    def __init__(self):
        self.count = 0
        self.sums = dict(input_tokens=0, output_tokens=0, cost_usd=0, latency_s=0)
        self.complete = dict.fromkeys(self.sums, True)

    def add(self, event):
        self.count += 1
        payload = event.get("payload", {})
        usage = payload.get("usage", {})
        milliseconds = _number(payload.get("latency_ms"))
        values = dict(input_tokens=_number(usage.get("input_tokens", usage.get("prompt_tokens"))),
                      output_tokens=_number(usage.get("output_tokens", usage.get("completion_tokens"))),
                      cost_usd=_number(usage.get("cost_usd")),
                      latency_s=milliseconds / 1000 if milliseconds is not None else _number(payload.get("latency_s", payload.get("duration_s"))))
        for key,value in values.items():
            if value is None:
                self.complete[key] = False
            else:
                self.sums[key] += value

    def value(self, key):
        return self.sums[key] if self.count and self.complete[key] else None


def _in_game_days(run_dir):
    """Latest server save's level.dat clock, expressed as a Minecraft day count."""
    latest = None
    for row in iter_registered_evidence(run_dir):
        if row["kind"] == "server_world_snapshot":
            latest = row
    if not latest:
        return {"value": None, "method": "no registered world snapshot; level.dat not collected"}
    try:
        document = json.loads(artifact_bytes(run_dir, latest["artifact_sha256"]))
    except (OSError, ValueError, UnicodeError):
        return {"value": None, "method": "latest world snapshot is unreadable"}
    level = document.get("level") if isinstance(document, dict) else None
    if not isinstance(level, dict) or level.get("error"):
        return {"value": None, "method": "level.dat unavailable in latest server save"}
    ticks = level.get("time")
    if not isinstance(ticks, int):
        ticks = level.get("daytime")
    day = level.get("day")
    if isinstance(day, int) and day >= 0:
        value = day
    elif isinstance(ticks, int) and ticks >= 0:
        value = ticks // 24000 + 1
    else:
        return {"value": None, "method": "level.dat has no usable Time/Day"}
    return {"value": value, "method": "world/level.dat Time (or Day) at latest server save",
            "source": "server save", "ticks": ticks, "snapshot_event_seq": latest.get("event_seq")}


def _server_log_success(run_dir):
    """Find the authoritative [Free the End] advancement line in archived server log segments."""
    for row in iter_registered_evidence(run_dir):
        if row["kind"] != "server_log_segment":
            continue
        try:
            document = json.loads(artifact_bytes(run_dir, row["artifact_sha256"]))
        except (OSError, ValueError, UnicodeError):
            continue
        for event in document.get("events", []) if isinstance(document, dict) else []:
            message = event.get("raw_message") if isinstance(event, dict) else None
            if isinstance(message, str) and "free the end" in message.lower():
                return {"available": True, "source_time": event.get("source_time"),
                        "raw_message": message[:1000], "evidence_id": row.get("evidence_id"),
                        "snapshot_event_seq": row.get("event_seq"),
                        "method": "server log advancement line (not the evaluator's snapshot upper bound)"}
    return {"available": False, "source_time": None, "raw_message": None,
            "method": "no [Free the End] line found in registered server log segments"}


def _server_command_audit(run_dir):
    """Count server-command lines in authoritative logs for the paper's no-console-control claim."""
    count, examples = 0, []
    for row in iter_registered_evidence(run_dir):
        if row["kind"] != "server_log_segment":
            continue
        try:
            document = json.loads(artifact_bytes(run_dir, row["artifact_sha256"]))
        except (OSError, ValueError, UnicodeError):
            continue
        for event in document.get("events", []) if isinstance(document, dict) else []:
            message = event.get("raw_message") if isinstance(event, dict) else None
            if isinstance(message, str) and ("issued server command" in message.lower() or "ran command" in message.lower()):
                count += 1
                if len(examples) < 20:
                    examples.append({"source_time": event.get("source_time"), "raw_message": message[:500],
                                     "evidence_id": row.get("evidence_id")})
    return {"command_lines": count, "examples": examples,
            "method": "registered server log lines containing issued/ran command; absence is evidence, not proof"}


def _build_report(
    run_dir: str | Path,
    evaluation_result: dict | None = None,
    events: Iterable[dict] | None = None,
    task_config: dict | str | None = None,
    _resources=None,
) -> dict:
    """Stream the ledger without retaining historical program/model payloads.

    Explicit ``events`` must be in event_seq order. The normal SQLite path uses
    disk-backed aggregation for exact execution deduplication; failure details
    are capped at 1000 with an explicit omitted count.
    """
    run_dir = Path(run_dir).resolve()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    config = manifest.get("config", manifest.get("configuration", {}))
    task = task_config or config.get("experiment", {}).get("task", "observe_only")
    provided = events is not None
    # Fresh evaluation independently rehashes immutable evidence; live scorer
    # caches are never used to certify the final report.
    if provided:
        event_source = list(events)
        event_source.sort(key=lambda event: event["event_seq"])
        evaluation_result = evaluation_result or evaluate(run_dir, task, event_source)
    else:
        event_source = iter_events(run_dir)
        evaluation_result = evaluation_result or evaluate(run_dir, task)
    diagnostic_only = (manifest.get("diagnostic_only") is True
                       or config.get("runtime", {}).get("mode") == "fake"
                       or config.get("model", {}).get("kind") == "fake")
    if diagnostic_only and evaluation_result.get("capability_success"):
        evaluation_result = {**evaluation_result, "status": "diagnostic_pass", "capability_success": None, "diagnostic_only": True}
    kinds = Counter()
    terminals, accepted = {}, set()
    usage, responses, fallback = _Usage(), _Usage(), _Usage()
    event_count = 0
    start = end = success_time = None
    run_status = "unknown"
    failures, failure_count = [], 0
    observer_reports, observer_report_count, observer_issue_count = [], 0, 0
    last_start = last_stop = None
    active_sum = stopped_sum = 0
    active_count = stopped_count = 0
    failure_kinds = {"infra_failed", "infrastructure_error", "evidence_collection_failed", "cleanup_failed", "control_failed", "runtime_failed", "server_failed", "model_failed", "budget_exhausted", "stalled"}
    analysis = _resources.enter_context(Analysis(run_dir, config, evaluation_result))
    for event in event_source:
        analysis.event(event)
        stamp = _timestamp(event)
        if event_count == 0:
            start = stamp
        end = stamp
        event_count += 1
        if event["event_seq"] == evaluation_result.get("first_success_event_seq"):
            success_time = stamp
        if event.get("source") == "agent_claim":
            continue
        kind, payload = event["kind"], event.get("payload", {})
        kinds[kind] += 1
        if kind == "observer_report" and isinstance(payload, dict):
            observer_report_count += 1
            observer_reports.append({
                "event_seq": event.get("event_seq"), "received_at": event.get("received_at"),
                "progress_summary": payload.get("progress_summary", ""),
                "verified_events": payload.get("verified_events", []),
                "infrastructure_findings": payload.get("infrastructure_findings", []),
                "findings": payload.get("findings", []),
            })
            if len(observer_reports) > 100:
                observer_reports = observer_reports[-100:]
        elif kind == "observer_infra_issue":
            observer_issue_count += 1
        if provided:
            execution_id = event.get("execution_id") or payload.get("execution_id")
            status = _execution_status(event)
            if execution_id and (kind in {"execution_accepted", "program_accepted", "execution_submitted", "program_submitted"} or status):
                accepted.add(execution_id)
            if execution_id and status:
                terminals[execution_id] = status
        if kind == "model_usage":
            usage.add(event)
        elif kind in {"model_completed", "model_response"}:
            fallback.add(event)
        if kind == "model_response":
            responses.add(event)
        if kind in {"run_finished", "run_stopped", "run_status"} and payload.get("status"):
            run_status = payload["status"]
        if kind in failure_kinds:
            failure_count += 1
            if len(failures) < 1000:
                encoded = json.dumps(payload, ensure_ascii=False)
                detail = payload if len(encoded) <= 8192 else {"detail_preview": encoded[:8192], "truncated": True}
                failures.append({"event_seq": event["event_seq"], "kind": kind, "payload": detail})
        if stamp is None:
            continue
        if kind == "server_started":
            if last_stop is not None:
                stopped_sum += max(0, stamp - last_stop)
                stopped_count += 1
            # Repeated readiness checks are not new lifecycle starts.
            if last_start is None:
                last_start = stamp
            last_stop = None
        elif kind in {"server_stopped", "world_stopped"} and last_start is not None:
            active_sum += max(0, stamp - last_start)
            active_count += 1
            last_start, last_stop = None, stamp
    model = usage if usage.count else fallback
    latency = responses if responses.count else model
    latency_sum = latency.value("latency_s") if latency.count == model.count else None
    input_sum, output_sum = model.value("input_tokens"), model.value("output_tokens")
    estimated_cost = model.value("cost_usd")
    rates = config.get("budgets", {}).get("prices_per_million", {})
    if estimated_cost is None and input_sum is not None and output_sum is not None and all(_number(rates.get(key)) is not None for key in ("input", "output")):
        estimated_cost = (input_sum * rates["input"] + output_sum * rates["output"]) / 1_000_000
    if provided:
        execution_count, execution_counts = len(accepted), Counter(terminals.values())
        submission_count = submission_statuses = None
    else:
        execution_count, execution_counts = _sql_execution_counts(run_dir)
        submission_count, submission_statuses = _sql_submission_counts(run_dir)
    completed = sum(execution_counts.values())
    if last_start is not None and end is not None:
        active_sum += max(0, end - last_start)
        active_count += 1
    if last_stop is not None and end is not None:
        stopped_sum += max(0, end - last_stop)
        stopped_count += 1
    metrics = {
        "wall_time_s": max(0, end - start) if start is not None and end is not None else None,
        "first_success_elapsed_s": max(0, success_time - start) if success_time is not None and start is not None else None,
        "execution_count": execution_count, "execution_terminal_counts": dict(sorted(execution_counts.items())),
        "submission_count": submission_count, "submission_status_counts": submission_statuses,
        "program_error_rate": execution_counts["failed"] / completed if completed else None,
        "program_timeout_rate": execution_counts["timed_out"] / completed if completed else None,
        "death_count": sum(kinds[kind] for kind in ("player_death", "bot_death", "server_player_death")),
        "chat_message_count": _server_chat_count(run_dir),
        "chat_delivery_count": sum(kinds[kind] for kind in ("game_chat", "chat", "server_chat", "chat_received", "bot_chat")),
        "recovery_count": sum(kinds[kind] for kind in ("world_restored", "world_recovered", "runtime_reset")),
        "model_call_count": model.count, "input_tokens": input_sum,
        "output_tokens": output_sum, "estimated_cost_usd": estimated_cost,
        "agent_model_wait_sum_s": latency_sum,
        "game_running_time_s": active_sum if active_count else None,
        "server_stopped_time_s": stopped_sum if stopped_count else None,
    }
    paper_metrics = analysis.finish()
    metrics['ledger_wall_time_s'] = metrics['wall_time_s']
    metrics['wall_time_s'] = paper_metrics['full_session']['duration_s']
    before = paper_metrics.get('pre_first_success')
    metrics['first_success_elapsed_s'] = before['duration_s'] if before else None
    server_counts = paper_metrics["full_session"]["authoritative_server_counts"]
    metrics["death_count"] = server_counts.get("death", 0) if server_counts is not None and paper_metrics["full_session"]["server_evidence_complete"] else None
    metrics['chat_message_count'] = server_counts.get('chat',0) if server_counts is not None else None
    metrics['server_fact_counts_complete'] = paper_metrics['full_session']['server_evidence_complete']
    metrics['death_count_observed'] = server_counts.get('death',0) if server_counts is not None else None
    cost_groups = paper_metrics["full_session"]["costs"]["groups"]
    if cost_groups:
        for field in ("input_tokens", "output_tokens", "cached_input_tokens", "noncached_input_tokens", "reasoning_tokens", "visible_output_tokens", "estimated_cost_usd"):
            values = [group[field] for group in cost_groups]
            metrics[field] = sum(values) if all(value is not None for value in values) else None
        metrics["model_call_count"] = sum(group["calls"] for group in cost_groups)
        if not paper_metrics['full_session']['costs']['legacy_unlinked_usage']:
            waits=[group['model_wait_s'] for group in cost_groups]
            metrics['agent_model_wait_sum_s']=sum(waits) if all(value is not None for value in waits) else None
    return {
        "schema_version": 1, "observer": "read_only_rules_v1", "run_id": manifest.get("run_id", run_dir.name),
        "task": task, "run_status": run_status,
        "evaluation": evaluation_result, "metrics": metrics, "failures": failures,
        "paper_metrics": paper_metrics,
        "failure_count": failure_count, "failures_omitted": max(0, failure_count - len(failures)),
        "event_count": event_count, "events_by_kind": dict(sorted(kinds.items())),
        "observer_review": {"report_count": observer_report_count, "infra_issue_count": observer_issue_count,
                            "latest_reports": observer_reports},
        "in_game_days": _in_game_days(run_dir),
        "success_log": _server_log_success(run_dir),
        "server_command_audit": _server_command_audit(run_dir),
        "limitations": ["Unknown usage, costs and lifecycle durations are null, never inferred as zero.",
                        "First success time is the first verified server snapshot, an upper bound on achievement time.",
                        "Model wait is summed over agents and may overlap game time.",
                        "Chat messages are counted from registered server logs; bot deliveries can contain one copy per recipient.",
                        "Log normalization recognizes English Vanilla Java grammar; unfamiliar messages remain in raw artifacts without inferred deaths or advancements.",
                        "Agent claims and unregistered server-source strings cannot establish capability success.",
                        "Submission counts use the durable request ledger and include requests rejected/interrupted before an execution ID exists.",
                        "Failure details are limited to the first 1000; failure_count and failures_omitted retain exact counts."],
    }


def build_report(run_dir, evaluation_result=None, events=None, task_config=None):
    """Read-only streaming report; temporary SQLite aggregation always closes."""
    with ExitStack() as resources:
        return _build_report(run_dir,evaluation_result,events,task_config,_resources=resources)


def _markdown(report: dict) -> str:
    evaluation = report["evaluation"]
    lines = [f"# Minecraft experiment {report['run_id']}", "", f"Run status: **{report['run_status']}**.",
             f"Task evaluation: **{evaluation['status']}** ({evaluation.get('task')}).", "",
             f"Criterion: `{evaluation.get('criterion_version', 'unknown')}`. World epoch: {evaluation.get('world_epoch', 'unknown')}.", "",
             "| Metric | Value |", "| --- | --- |"]
    for name, value in report["metrics"].items():
        rendered = "unknown" if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)
        lines.append(f"| {name} | {rendered} |")
    lines.extend(["", "## Independently verified evidence", ""])
    for reference in evaluation.get("evidence_refs", []):
        lines.append(f"- Event {reference['event_seq']}, epoch {reference['world_epoch']}, snapshot `artifacts/{reference['artifact_sha256']}`.")
        for file in reference.get("files", []):
            lines.append(f"  - `{file['relative_path']}`: `artifacts/{file['artifact_sha256']}`.")
    if not evaluation.get("evidence_refs"):
        lines.append("No independently verified capability evidence.")
    if report["failures"]:
        lines.extend(["", "## Recorded failures", ""])
        for failure in report["failures"]:
            lines.append(f"- Event {failure['event_seq']}: `{failure['kind']}`.")
    paper=report.get('paper_metrics',{})
    def cell(value):
        return ('unknown' if value is None else json.dumps(value,ensure_ascii=False,sort_keys=True)).replace('|','\\|').replace('\n',' ')
    if paper:
        lines.extend(['','## Paper measurement windows','',
            'Time starts at the first authoritative program start. First success is the first verified snapshot observation; it is an upper bound on achievement time.',
            'Server facts use registered raw log bytes. Missing log intervals and truncated output remain explicit; client copies are excluded.',''])
        for key,label in (('full_session','Entire session'),('pre_first_success','Before first verified success')):
            window=paper.get(key)
            lines.extend([f'### {label}',''])
            if window is None:
                lines.extend(['Unknown: no verified first-success observation.','']);continue
            lines.extend([f"Duration: {cell(window['duration_s'])} s. Server byte coverage complete: {cell(window['server_evidence_complete'])}.",'',
                '| Player | Program outcomes | Mean / p50 / p95 duration (s) | Program activity fraction | Programs + jobs activity | Death causes | Memory tools |',
                '| --- | --- | --- | --- | --- | --- | --- |'])
            for player in window['players']:
                duration=player['program_duration_s']
                values=(player['actor_id'],player['execution_status_counts'],[duration['mean'],duration['p50'],duration['p95']],
                    player['active_fraction'],player['combined_active_fraction'],player['death_causes'],player['memory_tool_calls'])
                lines.append('| '+' | '.join(cell(value) for value in values)+' |')
            lines.extend(['','| Actor / role / purpose | Calls | Input / cached / noncached | Output / reasoning / visible | Cost USD | Model wait (s) |',
                '| --- | --- | --- | --- | --- | --- |'])
            for group in window['costs']['groups']:
                values=('/'.join((group['actor_id'],group['role'],group['purpose'])),group['calls'],
                    [group['input_tokens'],group['cached_input_tokens'],group['noncached_input_tokens']],
                    [group['output_tokens'],group['reasoning_tokens'],group['visible_output_tokens']],group['estimated_cost_usd'],group['model_wait_s'])
                lines.append('| '+' | '.join(cell(value) for value in values)+' |')
            lines.extend(['',f"Incomplete evidence: {cell(window['incomplete'])}.",f"Exact lexical reuse: {cell(window['reuse'])}.",''])
        lines.extend(['### Explicit stage observations','',
            'Only registered server observations are listed. An absent stage is unknown. Snapshot criterion times are conservative upper bounds; log display titles are not canonical advancement IDs.',''])
        for stage in paper.get('stage_timeline',[]):
            lines.append(f"- {cell(stage['actor_id'])}: {cell(stage['stage'])}, source time {cell(stage['source_time'])}, evidence event {stage['event_seq']}.")
        lines.extend(['',f"Omitted stage observations: {paper.get('stage_timeline_omitted',0)}.",
            f"Chat labels: {cell(paper['chat_classification'])}. Lexical labels make no claim about intent or causal benefit."])
    observer_review = report.get("observer_review", {})
    lines.extend(["", "## Observer review", "",
                  f"Reports: {observer_review.get('report_count', 0)}; infrastructure issues: {observer_review.get('infra_issue_count', 0)}."])
    for item in (observer_review.get("latest_reports") or [])[-10:]:
        lines.append(f"- Event {item.get('event_seq')}: {(item.get('progress_summary') or '').strip()[:500] or '(no progress summary)'}")
    days = report.get("in_game_days", {})
    success_log = report.get("success_log", {})
    lines.extend(["", "## Server time and success corroboration", "",
                  f"In-game days: {days.get('value')} ({days.get('method')}).",
                  f"Server log [Free the End]: {success_log.get('available')} ({success_log.get('method')}).",
                  f"Server command lines observed: {report.get('server_command_audit', {}).get('command_lines')}."])
    lines.extend(["", "## Interpretation", ""])
    lines.extend(f"- {text}" for text in report["limitations"])
    return "\n".join(lines) + "\n"


def write_report(
    run_dir: str | Path,
    output_dir: str | Path | None = None,
    evaluation_result: dict | None = None,
    events: Iterable[dict] | None = None,
    task_config: dict | str | None = None,
) -> dict:
    """Write only inside the run's reports directory; reject other destinations."""
    run_dir = Path(run_dir).resolve()
    reports = run_dir / "reports"
    if reports.is_symlink():
        raise ValueError("Report directory must not be a symlink")
    output = Path(output_dir).resolve() if output_dir is not None else reports
    if not output.is_relative_to(reports):
        raise ValueError("Observer can write only inside run_dir/reports")
    report = build_report(run_dir, evaluation_result, events, task_config)

    def write(destination: Path) -> dict:
        destination.mkdir(parents=True, exist_ok=True)
        written = {}
        for name, data in (("json", json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"), ("markdown", _markdown(report))):
            path = destination / ("report.json" if name == "json" else "report.md")
            if path.is_symlink():
                raise ValueError("Report destination must not be a symlink")
            descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + "-", dir=destination)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(path)
                directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
            written[name] = str(path)
        return written

    try:
        return write(output)
    except PermissionError:
        # The dashboard may have created run_dir/reports first under a
        # different UID (the 2026-10-23 live test wrote its world-render cache
        # there as nobody:nogroup).  The Supervisor owns the run directory, so
        # preserve the foreign cache under a hidden name, recreate the report
        # directory, and write the final report.  A completed experiment must
        # not be turned into infra_failed by a cache-ownership collision.
        backup = run_dir / (".reports-foreign-" + uuid.uuid4().hex[:12])
        try:
            if reports.exists():
                reports.rename(backup)
            reports.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise
        relative = output.relative_to(reports)
        return write(reports / relative)
