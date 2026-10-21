#!/usr/bin/env python3
"""A bounded real-API / synthetic-game smoke run, never a game capability test."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from minecraft_harness.cli import _run_with_signals, doctor, run_status
from minecraft_harness.config import load_config
from minecraft_harness.recorder import atomic_json
from minecraft_harness.supervisor import Supervisor

ROOT = Path(__file__).resolve().parents[1]


def load_credential(variable, dotenv=None, prompt=False):
    """Read only the requested credential; dotenv is data and is never sourced."""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', variable):
        raise ValueError('invalid credential variable name')
    value = os.environ.get(variable)
    if not value and dotenv:
        path = Path(dotenv)
        if path.stat().st_size > 65536:
            raise ValueError('credential file exceeds limit')
        for line in path.read_text(encoding='utf-8').splitlines():
            match = re.fullmatch(r'\s*(?:export\s+)?' + re.escape(variable) + r'\s*=\s*(.*?)\s*', line)
            if match:
                value = match.group(1)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                else:
                    value = value.split(' #', 1)[0].rstrip()
                break
    if not value and prompt:
        value = getpass.getpass('DeepSeek API key (not saved): ')
    if not value or not value.strip():
        return False
    if any(char.isspace() for char in value) or len(value) > 4096:
        raise ValueError('invalid credential value')
    os.environ[variable] = value
    return True


def summarize(run_dir):
    """Read actual provider, tool and native runtime evidence independently."""
    run_dir = Path(run_dir).resolve()
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    config = manifest['config']
    with closing(sqlite3.connect((run_dir / 'events.sqlite3').as_uri() + '?mode=ro', uri=True)) as connection:
        counts = dict(connection.execute('SELECT kind,COUNT(*) FROM events GROUP BY kind'))
        responses = list(connection.execute("SELECT actor_id,model_call_id,payload_json FROM events WHERE kind='model_response' AND source!='agent_claim'"))
        native = list(connection.execute("SELECT DISTINCT actor_id,execution_id FROM events WHERE kind='execution_succeeded' AND source='bot_runtime'"))
        settlements = list(connection.execute("SELECT payload_json FROM events WHERE kind='model_usage'"))
        model_errors = sum(value for name, value in counts.items() if name in {'model_error', 'observer_model_error'})
        admitted = [json.loads(row[0]).get('reservation_id') for row in connection.execute("SELECT payload_json FROM events WHERE kind='model_admitted'")]
        settled = [json.loads(row[0]).get('reservation_id') for row in settlements]
        event_count = connection.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    status = run_status(run_dir, dashboard=True)
    actual_backend = (config['model']['kind'] == 'chat_completions'
        and config['model']['base_url'].rstrip('/') == 'https://api.deepseek.com'
        and config['model']['model'] == 'deepseek-flash')
    actors = Counter(row[0] for row in responses)
    successful_native = {actor for actor, identity in native if identity}
    expected = set(config['agents']['ids'])
    checks = {
        'real_deepseek_responses': actual_backend and bool(responses),
        'all_players_received_responses': expected.issubset(actors),
        'no_model_errors': model_errors == 0,
        'all_players_executed_native_programs': expected.issubset(successful_native),
        'observer_api_response': actors.get('observer', 0) > 0,
        'observer_persisted_report': counts.get('observer_report', 0) > 0,
        'all_admissions_uniquely_settled': bool(admitted) and len(set(admitted)) == len(admitted)
            and len(set(settled)) == len(settled) and Counter(admitted) == Counter(settled),
        'no_pending_requests': status.get('budget', {}).get('pending') == {},
        'call_limit_respected': status.get('budget', {}).get('calls', 17) <= config['budgets']['total_model_calls'],
        'reported_usage_known': bool(settlements) and status.get('budget', {}).get('unknown_usage_calls', 1) == 0,
        'estimated_cost_known_and_bounded': status.get('budget', {}).get('unknown_cost_calls', 0) == 0
            and isinstance(status.get('budget', {}).get('cost_usd'), (float, int))
            and status['budget']['cost_usd'] <= config['budgets']['max_cost_usd'],
        'reports_saved': all((run_dir / 'reports' / name).is_file() for name in ['report.json', 'report.md']),
        'supervisor_released': status.get('supervisor_active') is False,
        'no_infrastructure_failure': status.get('status') not in {'infra_failed', 'model_failed', 'reliability_failed', 'resource_exhausted'},
    }
    return {'at': datetime.now(timezone.utc).isoformat(), 'status': 'passed' if all(checks.values()) else 'failed',
        'checks': checks, 'run_dir': str(run_dir), 'model': config['model']['model'], 'endpoint': config['model']['base_url'],
        'game_diagnostic_only': True, 'actual_minecraft': False,
        'paid_model_attempted': bool(admitted), 'api_success': checks['real_deepseek_responses'] and checks['no_model_errors'],
        'native_tool_closed_loop': checks['all_players_executed_native_programs'],
        'observer_api_success': checks['observer_api_response'] and checks['observer_persisted_report'],
        'model_responses_by_actor': dict(actors), 'native_successful_players': sorted(successful_native),
        'event_count': event_count, 'event_counts': counts, 'budget': status.get('budget'),
        'experiment_status': status.get('status'), 'evaluation': status.get('evaluation'),
        'cost_basis': 'Frozen official peak USD token rates; estimate, not an account billing receipt.',
        'dashboard_url': 'http://127.0.0.1:8899/?run=' + run_dir.name}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/deepseek-api-smoke.yaml')
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'validation/deepseek-api-smoke.json')
    parser.add_argument('--dotenv', type=Path)
    parser.add_argument('--prompt-key', action='store_true')
    parser.add_argument('--summarize', type=Path, help='Read-only verification of a completed run; no API request.')
    args = parser.parse_args(argv)
    if args.summarize:
        result = summarize(args.summarize)
    else:
        config = load_config(args.config)
        variable = config['model'].get('api_key_env', 'DEEPSEEK_API_KEY')
        if not load_credential(variable, args.dotenv, args.prompt_key):
            print(json.dumps({'status': 'blocked', 'reason': 'model_credential_missing', 'variable': variable,
                              'model_calls': 0, 'actual_minecraft': False}))
            return 2
        # Restrict this launcher to its advertised small paid diagnostic.
        if (config['model']['base_url'].rstrip('/') != 'https://api.deepseek.com'
                or config['model']['model'] != 'deepseek-flash' or config['runtime']['mode'] != 'fake'
                or config['budgets']['total_model_calls'] > 16 or config['budgets']['wall_time_minutes'] > 3
                or config['budgets'].get('max_cost_usd', 1) > .15):
            raise ValueError('configuration exceeds the advertised diagnostic scope')
        preflight = doctor(config)
        if not preflight['ok']:
            print(json.dumps({'status': 'blocked', 'reason': 'preflight_failed', 'checks': preflight['checks']}))
            return 2
        run_dir = args.run_dir or ROOT / 'runs' / ('deepseek-smoke-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6])
        print(json.dumps({'status': 'starting', 'run_dir': str(run_dir), 'model': config['model']['model'],
            'call_limit': config['budgets']['total_model_calls'], 'cost_estimate_limit_usd': .15,
            'actual_minecraft': False}), flush=True)
        asyncio.run(_run_with_signals(Supervisor(config, run_dir)))
        result = summarize(run_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Configuration/credential file contents and HTTP response bodies must
        # not enter terminal logs through an exception traceback.
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}))
        raise SystemExit(1)
