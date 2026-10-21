"""Read-only HTTP/browser acceptance against existing offline experiment ledgers.

The only accepted control requests target a temporary fixture, never a real run.
Chrome is driven through Node built-ins; neither Minecraft nor a model is started.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode

from minecraft_harness.cli import run_status
from minecraft_harness.dashboard import create_dashboard
from minecraft_harness.recorder import Recorder


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, default=Path('runs'))
    parser.add_argument('--run', default='paper-lifecycle-20261021-123443')
    parser.add_argument('--large-run', default='paper-hour-final')
    parser.add_argument('--output', type=Path, default=Path('validation/dashboard/acceptance.json'))
    parser.add_argument('--skip-browser', action='store_true')
    args = parser.parse_args()
    args.runs = args.runs.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    checks, requests, failures = {}, [], []
    controls = []
    originals = {}
    expected = {}
    servers = []
    project = Path(__file__).resolve().parents[1]
    source_paths = ['src/minecraft_harness/' + name for name in (
        'dashboard.py', 'dashboard_catalog.py', 'dashboard_data.py', 'cli.py', 'config.py', 'supervisor.py',
        'static/dashboard.html', 'static/dashboard.css', 'static/dashboard.js')]
    source_paths += ['scripts/' + name for name in ('dashboard_acceptance.py', 'dashboard_browser_check.mjs', 'browser_cdp.mjs')]
    source_hashes = {name: digest(project / name) for name in source_paths}

    def check(name, condition, detail=None):
        checks[name] = bool(condition)
        if not condition:
            failures.append({'check': name, 'detail': detail})

    def start(path, reader, writer):
        server = create_dashboard(path, 0, reader, writer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    def request(server, route, *, method='GET', body=None, headers=None, json_body=True):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        began = time.monotonic()
        try:
            connection.request(method, route, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024 + 1)
            elapsed = time.monotonic() - began
            requests.append({'method': method, 'route': route, 'status': response.status,
                             'bytes': len(raw), 'seconds': round(elapsed, 6)})
            return response.status, json.loads(raw) if json_body else raw, dict(response.getheaders())
        finally:
            connection.close()

    def get(server, route, run=None, **query):
        if run is not None:
            query['run'] = run
        code, value, _ = request(server, route + ('?' + urlencode(query) if query else ''))
        check('http_' + str(len(requests)), code == 200, {'route': route, 'status': code})
        return value

    def reject_control(path, command):
        raise RuntimeError('Acceptance is read-only for original experiment data')

    browser = None
    try:
        for name in (args.run, args.large_run):
            run = args.runs / name
            for relative in ['events.sqlite3', 'manifest.json', 'reports/report.json', 'reports/report.md']:
                path = run / relative
                originals[str(path)] = digest(path)
            with closing(sqlite3.connect((run / 'events.sqlite3').as_uri() + '?mode=ro', uri=True)) as connection:
                expected[name] = {'count': connection.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                    'kinds': dict(connection.execute('SELECT kind,COUNT(*) FROM events GROUP BY kind')),
                    'actors': dict(connection.execute('SELECT actor_id,COUNT(*) FROM events GROUP BY actor_id'))}
        server = start(args.runs, lambda path: run_status(path, dashboard=True), reject_control)
        catalogue = get(server, '/api/runs', limit=100)
        available = {run['id'] for run in catalogue['runs']}
        check('real_offline_runs_discoverable', {args.run, args.large_run} <= available, sorted(available))
        check('catalogue_read_only_bounded', catalogue.get('read_only') is True and len(catalogue['runs']) <= 100)
        for name in (args.run, args.large_run):
            overview = get(server, '/api/overview', name)
            check(name + '_exact_event_count', overview['counts']['events'] == expected[name]['count'])
            check(name + '_diagnostic_not_real_minecraft', overview['run'].get('diagnostic_only') is True)
            status = get(server, '/api/status', name)
            check(name + '_bounded_status', status.get('event_count') == expected[name]['count']
                  and not any(key.startswith('execution_finished:') for key in status))
            first = get(server, '/api/events', name, limit=7)
            second = get(server, '/api/events', name, before=first['next_cursor'], limit=7)
            seq1 = [event['event_seq'] for event in first['items']]
            seq2 = [event['event_seq'] for event in second['items']]
            check(name + '_descending_cursor_no_overlap', len(seq1) == 7 and len(seq2) == 7
                  and seq1 == sorted(seq1, reverse=True) and max(seq2) < min(seq1))
            forward = get(server, '/api/events', name, after=0, limit=7)
            check(name + '_ascending_initial_window', [event['event_seq'] for event in forward['items']] == list(range(1, 8)))
            scoped = get(server, '/api/events', name, actor='player1', category='model', limit=7)
            check(name + '_server_side_filter', bool(scoped['items']) and all(event['actor_id'] == 'player1'
                  and event['category'] == 'model' for event in scoped['items']))
            resources = get(server, '/api/resources', name, limit=7)
            history = sum(expected[name]['kinds'].get(kind, 0) for kind in ('resource_sample', 'resources_sampled', 'resource_guard_sample', 'storage_sample'))
            check(name + '_resource_history_truthful', bool(resources['completeness']['history_available']) == bool(history))

        observer = get(server, '/api/observer', args.run, limit=7)
        check('observer_real_records', len(observer['items']) == 7 and all(event['kind'] == 'observer_report' and event['advisory'] for event in observer['items']))
        jobs = get(server, '/api/jobs', args.run, limit=7)
        check('four_real_background_jobs', len(jobs['items']) == 4 and len({job['job_id'] for job in jobs['items']}) == 4)
        programs = get(server, '/api/programs', args.run, actor='player1', limit=7)
        check('programs_actor_scoped', bool(programs['items']) and all(item['actor_id'] == 'player1' for item in programs['items']))
        program = programs['items'][0]
        detail = get(server, '/api/programs/' + program['detail_id'], args.run, actor='player1')
        check('program_detail_correlates', detail.get('program', detail.get('item', {})).get('execution_id') == program['execution_id'], list(detail))
        if program.get('source_ref'):
            identity = program['source_ref'].get('sha256', program['source_ref'].get('hash'))
            artifact = get(server, '/api/artifact/' + identity, args.run, limit=37)
            source = (args.runs / args.run / 'artifacts' / identity).read_bytes()
            check('original_code_artifact_exact', artifact['content'] == source[:37].decode(errors='replace') and artifact['sha256'] == hashlib.sha256(source).hexdigest())
        event = get(server, '/api/event/' + str(observer['items'][0]['event_seq']), args.run, limit=37)
        check('event_payload_is_paged', len(event['payload_page']['content']) <= 37 and event['payload_page']['next_offset'] == 37)
        report = get(server, '/api/report', args.run, format='json', limit=37)
        original_report = (args.runs / args.run / 'reports/report.json').read_text()
        check('original_report_page_exact', report['content'] == original_report[:37])
        code, raw, headers = request(server, '/download/report.json?' + urlencode({'run': args.run}), json_body=False)
        check('original_report_download_exact', code == 200 and hashlib.sha256(raw).hexdigest() == originals[str(args.runs / args.run / 'reports/report.json')]
              and 'attachment' in headers.get('Content-Disposition', ''))
        get(server, '/api/chat', args.large_run, limit=7)
        for route in ['/api/events?limit=101', '/api/events?after=1&before=2', '/api/events?actor=a&actor=b',
                      '/api/events?category=not-a-category', '/api/overview?run=..', '/api/overview?run=../paper-hour-final',
                      '/api/artifact/' + 'a' * 63]:
            code, _, _ = request(server, route)
            check('invalid_' + str(len(requests)), code == 400, route)
        code, _, _ = request(server, '/', headers={'Host': 'foreign.example'})
        check('host_boundary', code == 403)
        code, html, _ = request(server, '/', json_body=False)
        token = re.search(rb'name="operator-token" content="([^"]+)"', html).group(1).decode()
        payload = json.dumps({'run': args.run, 'command': 'stop'})
        code, _, _ = request(server, '/api/control', method='POST', body=payload, headers={'Content-Type': 'application/json'})
        check('control_token_required', code == 403)
        code, _, _ = request(server, '/api/control', method='POST', body=payload,
                             headers={'Content-Type': 'application/json', 'X-Control-Token': token, 'Origin': 'https://foreign.example'})
        check('control_origin_required', code == 403)

        with tempfile.TemporaryDirectory(prefix='dashboard-acceptance-', dir='/tmp') as temporary:
            fixture_path = Path(temporary) / 'browser-control-fixture'
            with closing(Recorder(fixture_path)) as recorder:
                recorder.append('chat', {'message': '<img src=x onerror="window.unsafeExecuted=true">', 'username': '<script>bad</script>'}, actor_id='player1')
                # More than the browser's 600 point retention tests forward-moving history cursors.
                beginning = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)
                for index in range(735):
                    recorder.append('resource_sample', {'sampled_at': (beginning + timedelta(seconds=index)).isoformat(),
                        'python_rss_bytes': 100_000_000 + index * 1000}, actor_id='supervisor')
                latest = {'sampled_at': (beginning + timedelta(seconds=1000)).isoformat(), 'python_rss_bytes': 512 * 1024 ** 2}
                recorder.set_state('resources', latest)
                fixture_status = {'status': 'running', 'supervisor_active': True, 'event_count': recorder.latest_event_seq(),
                                  'resources': latest,
                                  'budget': {'calls': 0}, 'controls': []}
                fixture = start(fixture_path, lambda path: fixture_status,
                                lambda path, command: controls.append(command) or 'fixture-control-one')
                if not args.skip_browser:
                    browser_output = args.output.with_name(args.output.stem + '-browser.json')
                    command = ['node', str(Path(__file__).with_name('dashboard_browser_check.mjs')),
                               '--url', f'http://127.0.0.1:{server.server_port}',
                               '--fixture-url', f'http://127.0.0.1:{fixture.server_port}',
                               '--run', args.run, '--large-run', args.large_run,
                               '--output', str(browser_output)]
                    completed = subprocess.run(command, text=True, capture_output=True, timeout=180)
                    browser = json.loads(browser_output.read_text()) if browser_output.exists() else {'status': 'failed', 'stdout': completed.stdout[-4000:], 'stderr': completed.stderr[-4000:]}
                    check('real_chrome_browser_acceptance', completed.returncode == 0 and browser.get('status') == 'passed')
                    check('fixture_only_confirmed_control', controls == ['pause'], controls)
            # Stop listeners before their temporary SQLite ledger is removed.
            fixture.shutdown(); fixture.server_close()
            servers[-1][1].join(timeout=3)
            servers.pop()
    except BaseException as error:
        failures.append({'type': type(error).__name__, 'message': str(error)})
    finally:
        for server, thread in reversed(servers):
            server.shutdown(); server.server_close(); thread.join(timeout=3)
        check('original_ledgers_and_reports_unchanged', all(digest(path) == original for path, original in originals.items()))
        source_hashes_after = {name: digest(project / name) for name in source_paths}
        check('dashboard_source_unchanged_during_acceptance', source_hashes == source_hashes_after)
        evidence = {'at': datetime.now(timezone.utc).isoformat(), 'status': 'passed' if checks and all(checks.values()) and not failures else 'failed',
                    'checks': checks, 'failures': failures, 'http_requests': requests, 'expected_from_readonly_sqlite': expected,
                    'original_sha256': originals, 'browser': browser, 'fixture_controls': controls,
                    'source_sha256_before': source_hashes, 'source_sha256_after': source_hashes_after,
                    'actual_minecraft': False, 'paid_model': False, 'new_experiments_started': 0,
                    'scope': 'Existing offline real ledgers plus temporary UI/control fixture; no Vanilla/model capability claim'}
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'status': evidence['status'], 'checks': len(checks), 'failed': failures, 'output': str(args.output)}, ensure_ascii=False))
    return 0 if evidence['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
