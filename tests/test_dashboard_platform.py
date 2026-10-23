"""Operator-facing run selection, static delivery and scoped control boundaries."""
import hashlib
import http.client
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from minecraft_harness.dashboard import artifact_page, create_dashboard, world_image
from minecraft_harness.dashboard_catalog import RunCatalog
from minecraft_harness.cli import run_status
from minecraft_harness.recorder import Recorder, atomic_json


def make_run(root, identity, created='2026-10-21T00:00:00+00:00'):
    path = root / identity
    recorder = Recorder(path)
    recorder.set_state('status', 'stopped')
    recorder.append('run_started', {'mode': 'fake'})
    recorder.close()
    atomic_json(path / 'manifest.json', {'created_at': created, 'diagnostic_only': True,
        'config': {'experiment': {'name': '<script>unsafe name</script>', 'profile': 'paper_aligned',
                                  'goal': 'Read-only diagnostic experiment'},
                   'model': {'kind': 'fake'}, 'agents': {'ids': ['player1', 'player2']}}})
    return path


class RunCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.first = make_run(self.root, 'one')
        self.second = make_run(self.root, 'two', '2026-10-22T00:00:00+00:00')

    def test_collection_only_discovers_real_direct_runs_and_pages_them(self):
        (self.root / 'frozen-source').mkdir()
        (self.root / 'alias').symlink_to(self.second, target_is_directory=True)
        catalog = RunCatalog(self.root)
        page = catalog.list(limit=1)
        self.assertEqual([run['id'] for run in page['runs']], ['two'])
        self.assertTrue(page['has_more'])
        self.assertTrue(page['runs'][0]['diagnostic_only'])
        self.assertEqual(catalog.list(cursor=page['next_cursor'], limit=1)['runs'][0]['id'], 'one')
        self.assertEqual(catalog.resolve(), self.second)
        self.assertEqual(page['total'], 2)

    def test_untrusted_paths_cannot_expand_selected_scope(self):
        catalog = RunCatalog(self.root)
        for identity in ['..', '../two', '/two', 'two/../one', 'two\\other', 'two\x00']:
            with self.assertRaises(ValueError):
                catalog.resolve(identity)
        (self.root / 'alias').symlink_to(self.second, target_is_directory=True)
        with self.assertRaises(ValueError):
            catalog.resolve('alias')
        single = RunCatalog(self.first)
        self.assertEqual(single.resolve('one'), self.first)
        with self.assertRaises(ValueError):
            single.resolve('two')

    def test_catalog_never_changes_ledger_or_requires_mutable_lock(self):
        before = hashlib.sha256((self.first / 'events.sqlite3').read_bytes()).hexdigest()
        (self.first / '.supervisor.lock').touch(mode=0o400)
        self.assertFalse(RunCatalog(self.first).list()['runs'][0]['active'])
        self.assertEqual(before, hashlib.sha256((self.first / 'events.sqlite3').read_bytes()).hexdigest())

    def test_dashboard_status_projects_summary_instead_of_execution_receipt_state(self):
        with sqlite3.connect(self.first / 'events.sqlite3') as connection:
            connection.executemany('INSERT INTO state(key,value_json) VALUES(?,?)',
                [(f'execution_finished:{index}', json.dumps({'receipt': 'x' * 1000})) for index in range(2000)])
        result = run_status(self.first, dashboard=True)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['event_count'], 1)
        self.assertLess(len(json.dumps(result)), 10000)
        self.assertFalse(any(key.startswith('execution_finished:') for key in result))

    def test_artifact_pages_preserve_chinese_and_emoji_at_byte_boundaries(self):
        original = '中文源码🙂abc🧪' * 10
        recorder = Recorder(self.first)
        ref = recorder.put_artifact(original.encode())
        recorder.close()
        for limit in (1, 2, 3, 7, 17):
            offset, parts = 0, []
            for _ in range(1000):
                page = artifact_page(self.first, ref['hash'], offset, limit)
                parts.append(page['content'])
                if page['next_offset'] is None:
                    break
                self.assertGreater(page['next_offset'], offset)
                offset = page['next_offset']
            self.assertEqual(''.join(parts), original)


class DashboardPlatformHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.one = make_run(self.root, 'one')
        self.two = make_run(self.root, 'two')
        self.controls = []
        self.server = create_dashboard(self.root, 0,
            lambda path: {'status': 'stopped', 'supervisor_active': False, 'label': path.name, 'budget': {'cost_usd': None}},
            lambda path, command: self.controls.append((path.name, command)) or 'queued-one')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, route, *, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, route, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_local_assets_are_separate_and_do_not_need_inline_javascript(self):
        code, headers, page = self.request('/')
        self.assertEqual(code, 200)
        self.assertIn('operator-token', page.decode())
        self.assertIn("script-src 'self';", headers['Content-Security-Policy'])
        self.assertNotIn("script-src 'self' 'unsafe-inline'", headers['Content-Security-Policy'])
        for route, content_type in [('/dashboard.css', 'text/css'), ('/dashboard.js', 'text/javascript'), ('/favicon.svg', 'image/svg+xml')]:
            code, headers, body = self.request(route)
            self.assertEqual(code, 200)
            self.assertTrue(headers['Content-Type'].startswith(content_type))
            self.assertGreater(len(body), 20)
        self.assertEqual(self.request('/../dashboard.py')[0], 404)

    def test_switching_runs_changes_reads_and_never_writes_a_control(self):
        self.assertEqual(self.request('/api/runs')[0], 200)
        for identity in ['one', 'two']:
            code, _, body = self.request('/api/status?' + urlencode({'run': identity}))
            self.assertEqual(code, 200)
            data = json.loads(body)
            self.assertEqual(data['label'], identity)
            self.assertIsNone(data['budget']['cost_usd'])
        self.assertEqual(self.controls, [])
        self.assertEqual(self.request('/api/status?run=one&run=two')[0], 400)
        self.assertEqual(self.request('/api/status?' + urlencode({'run': '../one'}))[0], 400)

    def test_program_request_identifiers_are_decoded_then_validated(self):
        with sqlite3.connect(self.one / 'events.sqlite3') as connection:
            connection.execute('INSERT INTO submissions VALUES(?,?,?,?)', ('player1', 'pending-one', 'digest',
                json.dumps({'request_id': 'pending-one', 'status': 'unknown'})))
        code, _, body = self.request('/api/programs/request%3Apending-one?run=one&actor=player1')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['program']['request_id'], 'pending-one')
        self.assertEqual(self.request('/api/programs/request%3A..%2Fone?run=one')[0], 400)

    def test_control_is_explicit_and_scoped_to_selected_run(self):
        _, _, body = self.request('/')
        token = re.search(r'<meta name="operator-token" content="([^"]+)">', body.decode()).group(1)
        headers = {'Content-Type': 'application/json', 'X-Control-Token': token}
        payload = json.dumps({'run': 'two', 'command': 'stop'})
        code, _, response = self.request('/api/control', method='POST', body=payload, headers=headers)
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(response)['state'], 'queued')
        self.assertEqual(self.controls, [('two', 'stop')])
        self.assertEqual(self.request('/api/control', method='POST', body=json.dumps({'run': '../one', 'command': 'stop'}), headers=headers)[0], 400)
        self.assertEqual(self.controls, [('two', 'stop')])

    def test_report_download_preserves_original_bytes_and_refuses_symlinks(self):
        reports = self.one / 'reports'
        reports.mkdir()
        raw = b'{"unknown_cost":null,"diagnostic_only":true}\n'
        (reports / 'report.json').write_bytes(raw)
        code, headers, body = self.request('/download/report.json?run=one')
        self.assertEqual(code, 200)
        self.assertEqual(body, raw)
        self.assertIn('attachment', headers['Content-Disposition'])
        (reports / 'report.json').unlink()
        (reports / 'report.json').symlink_to(self.two / 'manifest.json')
        self.assertEqual(self.request('/download/report.json?run=one')[0], 400)


class WorldImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_world_image_cache_never_writes_into_reports(self):
        # The dashboard runs as a different UID than the Supervisor. Its world
        # render cache must not live in run_dir/reports, or final report
        # creation can fail with EACCES.
        run = make_run(self.root, 'image')
        world = run / 'world'
        world.mkdir()
        manifest = json.loads((run / 'manifest.json').read_text())
        manifest['config']['server'] = {'world_dir': str(world)}
        (run / 'manifest.json').write_text(json.dumps(manifest))
        with patch('minecraft_harness.dashboard.world_fingerprint',
                   return_value={'sha256': 'fingerprint'}), \
             patch('minecraft_harness.dashboard.render_world',
                   side_effect=lambda path, png, **kwargs: (png.write_bytes(b'PNG'), {})[1]):
            png, metadata = world_image(run)
        self.assertEqual(png, b'PNG')
        self.assertTrue((run / 'renders' / 'world_render_mesh_160.png').is_file())
        self.assertTrue((run / 'renders' / 'world_render_mesh_160.json').is_file())
        self.assertFalse((run / 'reports').exists())
