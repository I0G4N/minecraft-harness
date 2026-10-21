"""Read-only, bounded operator views of the canonical experiment ledger.

These views never run an Evaluator or infer success from model prose. Event
filters run in SQLite before pagination; expensive whole-ledger aggregates are
cached briefly and carry their ledger high-water mark. No Recorder is opened.
"""
from __future__ import annotations

from collections import OrderedDict, Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

_LOCK = threading.RLock()
_CACHE = OrderedDict()
_INFLIGHT = {}
_CACHE_LIMIT = 48
_TTL = 5.0
_PAYLOAD_PREVIEW = 16000
_PAGE_BYTES = 110000  # events has a compatibility alias; two copies stay below HTTP's 512 KiB cap.
_TERMINAL = {'succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted', 'rejected', 'invalidated'}
_CATEGORY = """CASE
 WHEN kind LIKE 'job_%' THEN 'job'
 WHEN kind LIKE 'execution_%' OR kind LIKE 'program_%' THEN 'program'
 WHEN kind LIKE '%chat%' THEN 'chat'
 WHEN kind LIKE 'model_%' OR kind='context_compacted' THEN 'model'
 WHEN kind LIKE 'observer_%' THEN 'observer'
 WHEN kind LIKE 'control_%' OR kind LIKE 'session_%' THEN 'control'
 WHEN kind LIKE 'server_%' OR kind LIKE 'world_%' OR kind LIKE '%death%'
   OR kind LIKE '%advancement%' OR kind LIKE '%evidence%' OR kind LIKE '%success%' THEN 'evidence'
 ELSE 'system' END"""
_SEVERITY = """CASE
 WHEN kind LIKE '%failed%' OR kind LIKE '%error%' OR kind LIKE '%exhausted%'
   OR kind IN ('execution_timed_out','job_timed_out','agent_stalled') THEN 'error'
 WHEN kind LIKE '%warning%' OR kind LIKE '%gap%' OR kind LIKE '%truncated%'
   OR kind LIKE '%interrupted%' OR kind LIKE '%unknown%' OR kind LIKE '%degraded%'
   OR kind IN ('runtime_exit','runtime_recovery_started','server_recovery_started') THEN 'warning'
 ELSE 'info' END"""
_SELECT = "event_seq,kind,actor_id,source,received_at,world_epoch,runtime_epoch,execution_id,model_call_id,monotonic_elapsed_ms,length(payload_json) AS payload_length,substr(payload_json,1,16000) AS payload_json"
_RECORD_KEYS = ('execution_id', 'request_id', 'status', 'source_ref', 'source_hash', 'submitted_at',
                'accepted_at', 'started_at', 'finished_at', 'world_epoch', 'runtime_epoch', 'timeout_ms',
                'evidence_incomplete', 'output_truncated')
_RECORD_PROJECTION = 'json_object(' + ','.join("'" + key + "',json_extract(value_json,'$." + key + "')" for key in _RECORD_KEYS) + ", 'error',substr(json_extract(value_json,'$.error'),1,4000))"


def _integer(value, default=None, *, maximum=None):
    if value is None or value == '':
        return default
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ValueError('Expected a non-negative integer cursor')
    value = int(value)
    if maximum is not None and value > maximum:
        raise ValueError('Page limit exceeds the allowed maximum')
    return value


def _limit(value, maximum=100):
    value = _integer(value, maximum=maximum)
    if not value:
        raise ValueError('Page limit must be positive')
    return value


def _text(value, maximum=256):
    if value in (None, ''):
        return None
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValueError('Invalid filter')
    return value


def _json(raw, default=None):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


@contextmanager
def _connection(run_dir):
    database = Path(run_dir).resolve() / 'events.sqlite3'
    if database.is_symlink() or not database.is_file():
        raise ValueError('Run has no regular event ledger')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA temp_store=FILE')
    deadline = time.monotonic() + 4
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    try:
        yield connection
    except sqlite3.OperationalError as error:
        if 'interrupted' in str(error):
            raise ValueError('Query exceeded the read budget; narrow the time or cursor filters') from error
        raise
    finally:
        connection.close()


def _has_table(connection, name):
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _cached(run_dir, name, compute):
    key = (str(Path(run_dir).resolve()), name)
    with _LOCK:
        entry = _CACHE.get(key)
        if entry and time.monotonic() - entry[0] < _TTL:
            _CACHE.move_to_end(key)
            result = deepcopy(entry[1])
            result.setdefault('scope', {})['cache_age_seconds'] = round(time.monotonic() - entry[0], 3)
            return result
        event = _INFLIGHT.get(key)
        if event is None:
            event = _INFLIGHT[key] = threading.Event()
            leader = True
        else:
            leader = False
    if not leader:
        event.wait(30)
        with _LOCK:
            entry = _CACHE.get(key)
            if entry:
                result = deepcopy(entry[1])
                result.setdefault('scope', {})['cache_age_seconds'] = round(time.monotonic() - entry[0], 3)
                return result
        return compute()
    try:
        result = compute()
        with _LOCK:
            _CACHE[key] = (time.monotonic(), deepcopy(result))
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
            _INFLIGHT.pop(key, None)
            event.set()
        return result
    except BaseException:
        with _LOCK:
            _INFLIGHT.pop(key, None)
            event.set()
        raise


def _event(row):
    event = dict(row)
    raw = event.pop('payload_json', '{}')
    length = event.pop('payload_length', len(raw))
    event['payload'] = _json(raw, {'preview': raw, 'truncated': True}) if length <= _PAYLOAD_PREVIEW else {'preview': raw, 'truncated': True}
    event['payload_truncated'] = length > _PAYLOAD_PREVIEW
    event['detail_url'] = '/api/event/' + str(event['event_seq'])
    event['category'] = event.pop('_category', None) or _category(event['kind'])
    event['severity'] = event.pop('_severity', None) or _severity(event['kind'])
    return event


def _category(kind):
    if kind.startswith('job_'): return 'job'
    if kind.startswith(('execution_', 'program_')): return 'program'
    if 'chat' in kind: return 'chat'
    if kind.startswith('model_') or kind == 'context_compacted': return 'model'
    if kind.startswith('observer_'): return 'observer'
    if kind.startswith(('control_', 'session_')): return 'control'
    if kind.startswith(('server_', 'world_')) or any(part in kind for part in ('death', 'advancement', 'evidence', 'success')): return 'evidence'
    return 'system'


def _severity(kind):
    if any(part in kind for part in ('failed', 'error', 'exhausted')) or kind in ('execution_timed_out', 'job_timed_out', 'agent_stalled'): return 'error'
    if any(part in kind for part in ('warning', 'gap', 'truncated', 'interrupted', 'unknown', 'degraded')) or kind in ('runtime_exit', 'runtime_recovery_started', 'server_recovery_started'): return 'warning'
    return 'info'


def _scope(connection, **extra):
    high = connection.execute('SELECT COALESCE(MAX(event_seq),0) FROM events').fetchone()[0]
    return {'ledger_through_event_seq': high, 'read_only': True, **extra}


def events_page(run_dir, after=None, before=None, recent=True, limit=50, category='all', actor=None,
                kind=None, severity=None, q=None, start=None, end=None):
    """Filter first, then paginate. Descending by default; ``after`` is ascending."""
    limit = _limit(limit)
    after, before = _integer(after), _integer(before)
    if after is not None and before is not None:
        raise ValueError('Use either after or before')
    ascending = after is not None or not recent
    where, arguments = [], []
    if after is not None: where.append('event_seq>?'); arguments.append(after)
    if before is not None: where.append('event_seq<?'); arguments.append(before)
    if category not in (None, '', 'all'):
        if category not in {'program', 'job', 'chat', 'model', 'observer', 'control', 'evidence', 'system'}:
            raise ValueError('Unknown event category')
        where.append('(' + _CATEGORY + ')=?'); arguments.append(category)
    for column, value in [('actor_id', actor), ('kind', kind)]:
        if _text(value): where.append(column + '=?'); arguments.append(value)
    if severity not in (None, '', 'all'):
        if severity not in {'error', 'warning', 'info'}: raise ValueError('Unknown severity')
        where.append('(' + _SEVERITY + ')=?'); arguments.append(severity)
    if _text(q):
        where.append("instr(lower(kind||' '||actor_id||' '||payload_json),lower(?))>0"); arguments.append(q)
    for value, operator in [(start, '>='), (end, '<=')]:
        if value:
            try:
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if stamp.tzinfo is None: raise ValueError()
            except (ValueError, TypeError): raise ValueError('Time filters require ISO timestamps with a timezone')
            where.append('julianday(received_at)' + operator + 'julianday(?)'); arguments.append(value)
    sql = 'SELECT ' + _SELECT + ' FROM events' + (' WHERE ' + ' AND '.join(where) if where else '')
    sql += ' ORDER BY event_seq ' + ('ASC' if ascending else 'DESC') + ' LIMIT ?'
    with _connection(run_dir) as connection:
        rows = list(connection.execute(sql, [*arguments, limit + 1]))
        items, used = [], 0
        for row in rows[:limit]:
            event = _event(row)
            size = len(json.dumps(event, ensure_ascii=False).encode())
            if items and used + size > _PAGE_BYTES: break
            items.append(event); used += size
        has_more = len(rows) > len(items)
        scope = _scope(connection, selection='filtered_ledger', order='ascending' if ascending else 'descending')
    return {'events': items, 'items': items, 'next_cursor': items[-1]['event_seq'] if items else (after if ascending else before),
            'previous_cursor': items[0]['event_seq'] if items else None, 'has_more': has_more,
            'direction': 'after' if ascending else 'before', 'scope': scope,
            'completeness': {'page_complete': not any(item['payload_truncated'] for item in items),
                             'payload_previews': any(item['payload_truncated'] for item in items)}}


def event_detail(run_dir, event_seq, offset=0, limit=64000):
    event_seq, offset, limit = _integer(event_seq), _integer(offset, 0), _limit(limit, 64000)
    with _connection(run_dir) as connection:
        row = connection.execute('SELECT ' + _SELECT + ' FROM events WHERE event_seq=?', (event_seq,)).fetchone()
        if not row: raise ValueError('Event unavailable')
        raw = connection.execute('SELECT substr(payload_json,?,?),length(payload_json) FROM events WHERE event_seq=?',
                                 (offset + 1, limit, event_seq)).fetchone()
        # Return registered-looking references as references, never execute paths.
        refs = []
        for key, value in connection.execute("SELECT key,atom FROM events,json_tree(events.payload_json) WHERE events.event_seq=? AND json_tree.type='text' AND key IN ('sha256','hash','artifact_sha256','source_hash','raw_sha256') LIMIT 100", (event_seq,)):
            if isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) and value not in refs: refs.append(value)
    return {'event': _event(row), 'payload_page': {'content': raw[0], 'offset': offset,
            'next_offset': offset + len(raw[0]) if offset + len(raw[0]) < raw[1] else None,
            'total_characters': raw[1], 'offset_unit': 'unicode_characters'}, 'artifact_refs': refs}


def _small_file(run_dir, relative, maximum=1024 * 1024, *, verify_hash=None):
    root = Path(run_dir).resolve()
    path = root / relative
    if path.is_symlink() or not path.is_file() or any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
        raise ValueError('Read-only document unavailable')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum: raise ValueError('Document exceeds bounded read limit')
        raw = stream.read(maximum + 1)
    if len(raw) > maximum: raise ValueError('Document exceeds bounded read limit')
    if verify_hash and hashlib.sha256(raw).hexdigest() != verify_hash: raise ValueError('Artifact checksum mismatch')
    return raw


def report_page(run_dir, format='json', offset=0, limit=32000):
    if format not in {'json', 'markdown'}: raise ValueError('Unknown report format')
    offset, limit = _integer(offset, 0), _limit(limit, 64000)
    relative = 'reports/report.' + ('json' if format == 'json' else 'md')
    try:
        raw = _small_file(run_dir, relative, 8 * 1024 * 1024)
    except FileNotFoundError:
        raw = None
    except ValueError:
        if not (Path(run_dir) / relative).exists(): raw = None
        else: raise
    if raw is None:
        return {'available': False, 'format': format, 'content': '', 'offset': offset, 'next_offset': None, 'size_bytes': None}
    # Character cursors preserve UTF-8 across page boundaries.
    text = raw.decode('utf-8', errors='replace'); chunk = text[offset:offset + limit]
    return {'available': True, 'format': format, 'content': chunk, 'offset': offset,
            'next_offset': offset + len(chunk) if offset + len(chunk) < len(text) else None,
            'size_bytes': len(raw), 'offset_unit': 'unicode_characters',
            'scope': {'selection': 'existing_report', 'live': False, 'read_only': True}}


def _state(connection, keys):
    if not _has_table(connection, 'state'): return {}
    result = {}
    for row in connection.execute('SELECT key,value_json FROM state WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys):
        if len(row['value_json']) <= 256000: result[row['key']] = _json(row['value_json'])
    return result


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _usage_groups(connection):
    # model_usage is the durable settlement event; admission/response copies
    # are deliberately excluded so usage is never billed twice.
    fields = ['input_tokens', 'output_tokens', 'cached_input_tokens', 'noncached_input_tokens', 'reasoning_tokens']
    expressions = []
    for field in fields:
        path = "COALESCE(json_extract(payload_json,'$.normalized_usage." + field + "'),json_extract(payload_json,'$.usage." + field + "'))"
        type_path = "COALESCE(json_type(payload_json,'$.normalized_usage." + field + "'),json_type(payload_json,'$.usage." + field + "'))"
        expressions.extend([f'SUM(CASE WHEN {type_path} IN (\'integer\',\'real\') AND {path}>=0 THEN {path} ELSE 0 END) AS {field}_sum',
                            f'SUM(CASE WHEN {type_path} IN (\'integer\',\'real\') AND {path}>=0 THEN 0 ELSE 1 END) AS {field}_unknown'])
    cost = "COALESCE(json_extract(payload_json,'$.cost.cost_usd'),json_extract(payload_json,'$.usage.cost_usd'))"
    cost_type = "COALESCE(json_type(payload_json,'$.cost.cost_usd'),json_type(payload_json,'$.usage.cost_usd'))"
    query = "SELECT actor_id,COALESCE(json_extract(payload_json,'$.role'),CASE WHEN actor_id='observer' THEN 'observer' ELSE 'player' END) AS role,COALESCE(json_extract(payload_json,'$.purpose'),'unknown') AS purpose,COUNT(*) AS calls,"
    query += f"SUM(CASE WHEN {cost_type} IN ('integer','real') AND {cost}>=0 THEN {cost} ELSE 0 END) AS known_cost_usd,SUM(CASE WHEN {cost_type} IN ('integer','real') AND {cost}>=0 THEN 0 ELSE 1 END) AS unknown_cost_calls,"
    query += ','.join(expressions) + " FROM events WHERE kind='model_usage' AND source!='agent_claim' GROUP BY actor_id,role,purpose"
    groups = []
    for row in connection.execute(query):
        item = dict(row)
        for field in fields:
            item[field] = item.pop(field + '_sum') if item.pop(field + '_unknown') == 0 else None
        item['cost_usd'] = item['known_cost_usd'] if not item['unknown_cost_calls'] else None
        groups.append(item)
    unknown = sum(item['unknown_cost_calls'] for item in groups)
    known = sum(item['known_cost_usd'] for item in groups)
    return {'groups': groups, 'cost_usd': known if groups and not unknown else None,
            'known_cost_usd': known, 'unknown_cost_calls': unknown,
            'scope': 'durable_model_usage_settlements_only'}


def overview(run_dir):
    def compute():
        with _connection(run_dir) as connection:
            state = _state(connection, ['status', 'budget', 'resources', 'storage', 'evaluation', 'first_success',
                'first_verified_success', 'world_epoch', 'runtime_epoch', 'observer_outcome', 'observer_degraded', 'active_wall_seconds'])
            counts = dict(connection.execute('SELECT kind,COUNT(*) FROM events GROUP BY kind'))
            actors = [dict(row) for row in connection.execute('SELECT actor_id,COUNT(*) AS event_count,MAX(received_at) AS last_activity FROM events GROUP BY actor_id ORDER BY actor_id')]
            costs = _usage_groups(connection)
            recent = [_event(row) for row in connection.execute('SELECT ' + _SELECT + ' FROM events ORDER BY event_seq DESC LIMIT 8')]
            scope = _scope(connection, selection='all_ledger_aggregates', sampled_at=datetime.now(timezone.utc).isoformat())
            gaps = {key: value for key, value in counts.items() if any(word in key for word in ('evidence_gap', 'log_gap', 'truncated', 'log_unavailable', 'collection_failed'))}
            program_counts = {}
            program_total = None
            if _has_table(connection, 'submissions'):
                program_counts = dict(connection.execute("SELECT COALESCE(json_extract(value_json,'$.status'),'unknown'),COUNT(*) FROM submissions GROUP BY 1"))
                program_total = sum(program_counts.values())
            first = connection.execute("SELECT received_at,payload_json FROM events WHERE kind='run_started' ORDER BY event_seq LIMIT 1").fetchone()
            finished = connection.execute("SELECT json_extract(payload_json,'$.evaluation') FROM events WHERE kind='run_finished' AND source!='agent_claim' ORDER BY event_seq DESC LIMIT 1").fetchone()
            final_evaluation = _json(finished[0]) if finished and finished[0] and len(finished[0]) <= 64000 else None
        manifest = {}
        try: manifest = _json(_small_file(run_dir, 'manifest.json').decode(), {})
        except (ValueError, OSError): pass
        config = manifest.get('config', {}) if isinstance(manifest, dict) else {}
        experiment = config.get('experiment', {})
        name = Path(run_dir).name
        report = {}
        try: report = _json(_small_file(run_dir, 'reports/report.json', 1024 * 1024).decode(), {})
        except (ValueError, OSError): pass
        report_window = report.get('paper_metrics', {}).get('full_session', {}) if isinstance(report, dict) else {}
        evaluation = state.get('evaluation') or final_evaluation
        runtime_mode = config.get('runtime', {}).get('mode', manifest.get('runtime_mode'))
        model_kind = manifest.get('model_kind', config.get('model', {}).get('kind'))
        diagnostic = manifest.get('diagnostic_only')
        if runtime_mode == 'fake' or model_kind == 'fake': diagnostic = True
        elif type(diagnostic) is not bool: diagnostic = False if runtime_mode and model_kind else None
        return {'run': {'run_id': manifest.get('run_id', name), 'name': name,
                        'experiment_name': experiment.get('name'),
                        'profile': experiment.get('profile'), 'diagnostic_only': diagnostic,
                        'created_at': manifest.get('created_at') or (first['received_at'] if first else None),
                        'world_epoch': state.get('world_epoch'), 'runtime_epoch': state.get('runtime_epoch')},
                'status': state.get('status', 'unknown'), 'evaluation': evaluation,
                'first_success': state.get('first_verified_success', state.get('first_success')),
                'budget': state.get('budget'), 'costs': costs, 'resources': state.get('resources'),
                'budget_limits': {key: config.get('budgets', {}).get(key) for key in ('total_model_calls', 'max_cost_usd', 'wall_time_minutes')},
                'storage': state.get('storage'), 'active_wall_seconds': state.get('active_wall_seconds'),
                'counts': {'events': sum(counts.values()), 'by_kind': counts, 'program_statuses': program_counts,
                           'programs': program_total,
                           'observer_reports': counts.get('observer_report', 0),
                           'job_events': sum(value for key, value in counts.items() if key.startswith('job_')),
                           'model_settlements': sum(group['calls'] for group in costs['groups']),
                           'chat_deliveries': sum(value for key, value in counts.items() if 'chat' in key),
                           'client_death_reports': sum(value for key, value in counts.items() if key in {'bot_death', 'player_death'}),
                           'server_log_segments': counts.get('server_log_segment', 0)},
                'actors': actors, 'recent_activity': recent,
                'observer': {'outcome': state.get('observer_outcome'), 'degraded': state.get('observer_degraded'), 'advisory': True},
                'report_metrics': report.get('metrics') if report else None,
                'evidence': {'status': 'known_gaps' if gaps or report_window.get('incomplete') else 'not_assessed', 'gaps': gaps,
                             'report_incomplete': report_window.get('incomplete'), 'report_complete': report_window.get('complete'),
                             'authoritative_server_counts': report_window.get('authoritative_server_counts'),
                             'report_through_event_count': report.get('event_count'),
                             'claim': 'No gaps in event names is not proof that evidence is complete.'},
                'scope': scope, 'completeness': {'aggregate_scope': 'all_ledger', 'cost_complete': costs['cost_usd'] is not None,
                                                'success_source': 'persisted_deterministic_evaluation_only'}}
    return _cached(run_dir, 'overview', compute)


def _record_item(record, actor, rowid):
    identifier = record.get('execution_id')
    item = {key: record.get(key) for key in ['execution_id', 'request_id', 'status', 'source_ref', 'accepted_at',
            'started_at', 'finished_at', 'world_epoch', 'runtime_epoch', 'error', 'timeout_ms']}
    item.update(actor_id=actor, submitted_at=record.get('submitted_at', record.get('accepted_at')),
                event_seq=None, last_event_seq=None, cursor=rowid, duration_ms=None)
    item['status'] = item['status'] or 'unknown'
    if isinstance(item['error'], str) and len(item['error']) > 4000: item['error'] = item['error'][:4000] + ' [preview]'
    if isinstance(item['source_ref'], dict):
        item['source_ref'] = {key: value[:4096] for key, value in item['source_ref'].items()
                              if key in {'sha256', 'hash', 'path'} and isinstance(value, str)}
    if not item['source_ref'] and record.get('source_hash'): item['source_ref'] = {'sha256': record['source_hash']}
    if item['started_at'] and item['finished_at']:
        try:
            elapsed = (datetime.fromisoformat(item['finished_at'].replace('Z','+00:00')) - datetime.fromisoformat(item['started_at'].replace('Z','+00:00'))).total_seconds() * 1000
            item['duration_ms'] = elapsed if elapsed >= 0 else None
        except (ValueError, TypeError): pass
    item['detail_id'] = identifier or ('request:' + str(item['request_id']))
    item['evidence_incomplete'] = bool(record.get('evidence_incomplete') or record.get('output_truncated'))
    return item


def programs_page(run_dir, cursor=None, limit=50, actor=None, status=None, q=None):
    return _programs_page_cached(run_dir, cursor, limit, actor, status, q)


def _programs_page_cached(run_dir, cursor, limit, actor, status, q):
    key = 'programs:' + json.dumps([cursor, limit, actor, status, q], ensure_ascii=False, sort_keys=True)
    return _cached(run_dir, key, lambda: _programs_page(run_dir, cursor, limit, actor, status, q))


def _programs_page(run_dir, cursor=None, limit=50, actor=None, status=None, q=None):
    cursor, limit = _integer(cursor), _limit(limit)
    where, arguments = [], []
    if cursor is not None: where.append('rowid<?'); arguments.append(cursor)
    if _text(actor): where.append('agent_id=?'); arguments.append(actor)
    if _text(status) and status != 'all': where.append("COALESCE(json_extract(value_json,'$.status'),'unknown')=?"); arguments.append(status)
    if _text(q): where.append("instr(lower(value_json),lower(?))>0"); arguments.append(q)
    with _connection(run_dir) as connection:
        if not _has_table(connection, 'submissions'):
            return {'items': [], 'next_cursor': None, 'has_more': False, 'scope': _scope(connection), 'completeness': {'complete': False, 'reason': 'legacy_ledger_without_submission_table'}}
        sql = 'SELECT rowid,agent_id,' + _RECORD_PROJECTION + ' AS value_json FROM submissions' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY rowid DESC LIMIT ?'
        rows = list(connection.execute(sql, [*arguments, limit + 1]))
        items = []
        for row in rows[:limit]:
            record = _json(row['value_json'], {})
            item = _record_item(record, row['agent_id'], row['rowid'])
            items.append(item)
        ids = [item['execution_id'] for item in items if item['execution_id']]
        if ids:
            events = {}
            for row in connection.execute('SELECT actor_id,execution_id,MIN(event_seq),MAX(event_seq),MIN(received_at) FROM events WHERE source!=? AND execution_id IN (' + ','.join('?' for _ in ids) + ') GROUP BY actor_id,execution_id', ['agent_claim', *ids]):
                events[(row[0], row[1])] = row
            for item in items:
                row = events.get((item['actor_id'], item['execution_id']))
                if row: item.update(event_seq=row[2], last_event_seq=row[3], submitted_at=item['submitted_at'] or row[4])
        return {'items': items, 'next_cursor': items[-1]['cursor'] if items else cursor, 'has_more': len(rows) > limit,
                'scope': _scope(connection, selection='durable_submissions', order='admission_descending'),
                'completeness': {'complete': not any(item['evidence_incomplete'] for item in items), 'unknown_status_is_not_failure': True}}


def program_detail(run_dir, execution_id, actor=None):
    execution_id, actor = _text(execution_id, 512), _text(actor)
    if not execution_id: raise ValueError('Program identifier required')
    with _connection(run_dir) as connection:
        column, identifier = ('request_id', execution_id[8:]) if execution_id.startswith('request:') else ("json_extract(value_json,'$.execution_id')", execution_id)
        sql = 'SELECT rowid,agent_id,' + _RECORD_PROJECTION + ' AS value_json FROM submissions WHERE ' + column + '=?'
        arguments = [identifier]
        if actor: sql += ' AND agent_id=?'; arguments.append(actor)
        rows = list(connection.execute(sql + ' LIMIT 2', arguments))
        if len(rows) != 1: raise ValueError('Program unavailable or actor is required')
        row = rows[0]
        record = _json(row['value_json'], {}) if len(row['value_json']) <= 4 * 1024 * 1024 else {}
        item = _record_item(record, row['agent_id'], row['rowid'])
        refs = [item['source_ref']] if item.get('source_ref') else []
        events, used, capped = [], 0, False
        for event in connection.execute('SELECT ' + _SELECT + ' FROM events WHERE actor_id=? AND execution_id=? ORDER BY event_seq DESC LIMIT 101', (row['agent_id'], item['execution_id'])):
            event = _event(event); size = len(json.dumps(event, ensure_ascii=False).encode())
            if len(events) >= 100 or (events and used + size > 150000): capped = True; break
            events.append(event); used += size
        # Large receipts stay in event_detail's bounded original JSON pages.
        receipt_row = connection.execute("SELECT length(value_json),substr(value_json,1,32000) FROM submissions WHERE rowid=?", (row['rowid'],)).fetchone()
        full = _json(receipt_row[1]) if receipt_row[0] <= 32000 else None
        receipt = {key: full.get(key) for key in ('result', 'error', 'before', 'after', 'cleanup', 'evidence_incomplete')} if isinstance(full, dict) else {'preview': receipt_row[1], 'truncated': True}
        return {'program': item, 'receipt': receipt, 'events': events, 'source_refs': refs,
                'has_more_events': capped, 'scope': _scope(connection),
                'completeness': {'receipt_complete': not receipt.get('truncated', False), 'events_complete': not capped}}


def _inner(event):
    payload = event.get('payload')
    if not isinstance(payload, dict): return {}
    nested = payload.get('payload')
    return nested if isinstance(nested, dict) else payload


def jobs_page(run_dir, cursor=None, limit=50, actor=None, status=None, q=None):
    cursor, limit = _integer(cursor), _limit(limit)
    actor, status, q = _text(actor), _text(status), _text(q)
    def compute():
        with _connection(run_dir) as connection:
            rows = list(connection.execute('SELECT ' + _SELECT + " FROM events WHERE kind LIKE 'job_%' ORDER BY event_seq DESC LIMIT 2001"))
            jobs = {}
            for row in reversed(rows[:2000]):
                event = _event(row); payload = _inner(event)
                record = payload.get('job', payload)
                if not isinstance(record, dict) or not record.get('job_id'): continue
                key = (event['actor_id'], record['job_id'])
                item = jobs.setdefault(key, {'actor_id': key[0], 'job_id': key[1], 'status': 'unknown', 'started_at': None,
                    'finished_at': None, 'execution_id': event['execution_id'], 'ttl_ms': None, 'cancel_reason': None})
                for field in ('status', 'started_at', 'finished_at', 'execution_id', 'ttl_ms', 'cancel_reason'):
                    if record.get(field) is not None: item[field] = record[field]
                if not item['cancel_reason']:
                    item['cancel_reason'] = record.get('reset_reason', record.get('reason'))
                if event['kind'] == 'job_started':
                    item['status'] = record.get('status', 'running'); item['started_at'] = item['started_at'] or event['received_at']
                elif event['kind'] == 'job_finished':
                    item['status'] = record.get('status', 'unknown'); item['finished_at'] = item['finished_at'] or event['received_at']
                elif event['kind'][4:] in _TERMINAL:
                    item['status'] = event['kind'][4:]; item['finished_at'] = item['finished_at'] or event['received_at']
                item.update(last_event_seq=event['event_seq'],
                            world_epoch=record.get('world_epoch', item.get('world_epoch', event['world_epoch'])),
                            runtime_epoch=record.get('runtime_epoch', item.get('runtime_epoch', event['runtime_epoch'])),
                            last_event_runtime_epoch=event['runtime_epoch'])
            return {'items': sorted(jobs.values(), key=lambda item: item['last_event_seq'], reverse=True),
                    'scope': _scope(connection, selection='job_event_tail', scanned_events=min(len(rows), 2000)),
                    'completeness': {'history_complete': len(rows) <= 2000, 'live_state': False,
                                     'claim': 'Last recorded job state; running is not a current liveness guarantee.'}}
    result = _cached(run_dir, 'jobs', compute)
    items = [item for item in result['items'] if (cursor is None or item['last_event_seq'] < cursor)
        and (not actor or item['actor_id'] == actor) and (not status or status == 'all' or item['status'] == status)
        and (not q or q.lower() in json.dumps(item).lower())]
    result.update(items=items[:limit], has_more=len(items) > limit, next_cursor=items[min(limit, len(items)) - 1]['last_event_seq'] if items else cursor)
    return result


def observer_page(run_dir, cursor=None, limit=30):
    page = events_page(run_dir, before=cursor, limit=limit, kind='observer_report')
    selected, used = [], 0
    for item in page['items']:
        item['findings'] = _inner(item).get('findings')
        if item['payload_truncated']:
            # Large reports still expose a bounded structured preview. The
            # original remains available through event_detail pagination.
            with _connection(run_dir) as connection:
                rows = connection.execute("SELECT json_extract(value,'$.status'),substr(json_extract(value,'$.claim'),1,4000),json_extract(value,'$.evidence_refs') FROM events,json_each(events.payload_json,'$.findings') WHERE events.event_seq=? LIMIT 4", (item['event_seq'],))
                item['findings'] = [{'status': row[0], 'claim': row[1],
                    'evidence_refs': _json(row[2]) if row[2] and len(row[2]) < 2000 else [],
                    'preview': True} for row in rows]
        item['advisory'] = True
        size = len(json.dumps(item, ensure_ascii=False).encode())
        while size > _PAGE_BYTES and isinstance(item['findings'], list) and item['findings']:
            item['findings'].pop(); size = len(json.dumps(item, ensure_ascii=False).encode())
        if selected and used + size > _PAGE_BYTES: break
        selected.append(item); used += size
    page['has_more'] = page['has_more'] or len(selected) < len(page['items'])
    page['items'] = page['events'] = selected
    page['next_cursor'] = selected[-1]['event_seq'] if selected else _integer(cursor)
    return page


def resources_page(run_dir, cursor=None, limit=100):
    cursor, limit = _integer(cursor), _limit(limit, 200)
    with _connection(run_dir) as connection:
        where = "kind IN ('resource_sample','resources_sampled','resource_guard_sample','storage_sample')"
        arguments = []
        if cursor is not None: where += ' AND event_seq<?'; arguments.append(cursor)
        rows = list(connection.execute('SELECT ' + _SELECT + ' FROM events WHERE ' + where + ' ORDER BY event_seq DESC LIMIT ?', [*arguments, limit + 1]))
        items = [{**_inner(event), 'event_seq': event['event_seq'], 'sampled_at': _inner(event).get('sampled_at', event['received_at'])} for event in map(_event, rows[:limit])]
        state = _state(connection, ['resources'])
        return {'items': items, 'latest': state.get('resources'), 'next_cursor': items[-1]['event_seq'] if items else cursor,
                'has_more': len(rows) > limit, 'scope': _scope(connection, selection='persisted_resource_samples'),
                'completeness': {'history_available': bool(rows), 'claim': 'State-only samples have no persisted time series.'}}


def _chat_cursor(value):
    if value is None or value == '': return None
    if not re.fullmatch(r'\d+:\d+', str(value)): raise ValueError('Invalid chat cursor')
    return tuple(map(int, str(value).split(':')))


def chat_page(run_dir, cursor=None, limit=50, actor=None, q=None):
    """A bounded server-first view. Deduplication is explicit and window-local.

    Source positions, not message text alone, identify server messages. Client
    deliveries are collapsed across recipients only within the same epoch and
    second; repeated identical messages remain uncertain, rather than counted
    as independently verified messages.
    """
    cursor, limit, actor, q = _chat_cursor(cursor), _limit(limit), _text(actor), _text(q)
    def compute():
        with _connection(run_dir) as connection:
            rows = list(connection.execute('SELECT ' + _SELECT + " FROM events WHERE kind IN ('server_log_segment','server_chat','game_chat','chat','chat_received','bot_chat') ORDER BY event_seq DESC LIMIT 1001"))
            scope = _scope(connection, selection='recent_chat_and_log_events', scanned_events=min(len(rows), 1000))
        server, clients, gaps = [], [], Counter()
        budget, segments = 16 * 1024 * 1024, 0
        for row in rows[:1000]:
            event = _event(row); payload = _inner(event)
            if event['kind'] == 'server_log_segment' and event['source'] == 'server':
                digest = payload.get('artifact_sha256')
                if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest): gaps['invalid_segment_reference'] += 1; continue
                if segments >= 32 or budget <= 0: gaps['segment_read_window_capped'] += 1; continue
                try:
                    raw = _small_file(run_dir, 'artifacts/' + digest, min(budget, 8 * 1024 * 1024), verify_hash=digest)
                    budget -= len(raw); segments += 1
                    document = _json(raw, {})
                    if not document.get('complete'): gaps['incomplete_log_segment'] += 1
                    for index, message in enumerate(document.get('events', [])):
                        if not isinstance(message, dict) or message.get('kind') != 'chat': continue
                        if len(server) >= 4000: gaps['message_window_capped'] += 1; break
                        server.append({**message, 'event_seq': event['event_seq'], 'item_index': index,
                            'source_location': message.get('source'),
                            'verification': 'server_normalized', 'evidence_ref': {'event_seq': event['event_seq'], 'artifact_sha256': digest},
                            'duplicate_deliveries': 0, 'source': 'server', 'world_epoch': event['world_epoch']})
                except (ValueError, OSError, TypeError): gaps['unreadable_log_segment'] += 1
                continue
            message, author = payload.get('message'), payload.get('actor_id', payload.get('username', event['actor_id']))
            outer = event['payload']; inner = outer.get('payload') if isinstance(outer, dict) else None
            if isinstance(inner, list) and len(inner) >= 2: author, message = inner[0], inner[1]
            if not isinstance(message, str): continue
            item = {'actor_id': author, 'message': message, 'source': 'server' if event['source'] == 'server' else 'client',
                    'verification': 'server_event' if event['source'] == 'server' else 'client_delivery',
                    'event_seq': event['event_seq'], 'item_index': 0, 'received_at': event['received_at'],
                    'source_time': payload.get('source_time'), 'world_epoch': event['world_epoch'], 'duplicate_deliveries': 0}
            (server if event['source'] == 'server' else clients).append(item)
        seen, items = {}, []
        for item in sorted(server, key=lambda value: (value['event_seq'], value['item_index']), reverse=True):
            location = item.get('source_location') or {'event_seq': item['event_seq'], 'item_index': item['item_index']}
            key = (item['world_epoch'], json.dumps(location, sort_keys=True), item.get('source_time'), item['actor_id'], item['message'])
            if key in seen: seen[key]['duplicate_deliveries'] += 1
            else: seen[key] = item; items.append(item)
        matches = {(item['world_epoch'], item['actor_id'], item['message']): item for item in items}
        client_seen = {}
        for item in clients:
            key = (item['world_epoch'], item['actor_id'], item['message'])
            # Prefer a nearby server source. A distant identical phrase is not
            # proof that a newer client delivery describes the same message.
            candidate = matches.get(key)
            if candidate and _near(candidate.get('source_time') or candidate.get('received_at'), item['received_at'], 3):
                candidate['duplicate_deliveries'] += 1; continue
            delivery_key = (*key, item['received_at'][:19])
            if delivery_key in client_seen: client_seen[delivery_key]['duplicate_deliveries'] += 1
            else:
                item['deduplication'] = 'recipient_deliveries_same_second_heuristic'
                client_seen[delivery_key] = item; items.append(item)
        return {'items': sorted(items, key=lambda item: (item['event_seq'], item['item_index']), reverse=True), 'scope': scope,
                'completeness': {'history_complete': len(rows) <= 1000 and not gaps, 'gaps': dict(gaps),
                                 'deduplication_scope': 'bounded_recent_window', 'server_priority': True}}
    result = _cached(run_dir, 'chat', compute)
    items = [item for item in result['items'] if (cursor is None or (item['event_seq'], item['item_index']) < cursor)
             and (not actor or item.get('actor_id') == actor) and (not q or q.lower() in item.get('message', '').lower())]
    selected, used = [], 0
    for item in items[:limit]:
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if selected and used + size > _PAGE_BYTES: break
        selected.append(item); used += size
    result.update(items=selected, has_more=len(items) > len(selected),
                  next_cursor=f"{selected[-1]['event_seq']}:{selected[-1]['item_index']}" if selected else None)
    return result


def _near(first, second, seconds):
    try:
        return abs((datetime.fromisoformat(first.replace('Z', '+00:00')) - datetime.fromisoformat(second.replace('Z', '+00:00'))).total_seconds()) <= seconds
    except (ValueError, TypeError, AttributeError): return False
