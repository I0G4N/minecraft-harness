"""Operator data views: filtering precedes paging and evidence stays read-only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from minecraft_harness import dashboard_data as data
from minecraft_harness.recorder import Recorder


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.recorder = Recorder(self.path)
        (self.path / 'manifest.json').write_text(json.dumps({'run_id': 'test-run', 'config': {
            'experiment': {'profile': 'strict'}, 'runtime': {'mode': 'fake'},
            'budgets': {'total_model_calls': 200}}, 'created_at': '2026-10-21T00:00:00Z'}))

    def tearDown(self):
        self.recorder.close()
        self.temp.cleanup()

    def append(self, kind, payload=None, actor='alice', **kwargs):
        return self.recorder.append(kind, payload or {}, actor_id=actor, **kwargs)

    def submission(self, request, execution=None, status='unknown', actor='alice', **fields):
        record = {'request_id': request, 'execution_id': execution, 'status': status,
                  'agent_id': actor, **fields}
        self.recorder.submission(actor, request, digest='digest:' + request, value=record)
        return record

    def test_filters_are_applied_before_limit_and_cursors_are_filtered(self):
        first = self.append('execution_failed', {'message': 'needle'}, 'alice')
        for _ in range(150): self.append('bot_chat', {'message': 'noise'}, 'bob')
        second = self.append('execution_failed', {'message': 'needle2'}, 'alice')
        third = self.append('execution_failed', {'message': 'needle3'}, 'alice')
        self.append('execution_failed', {'message': 'other'}, 'bob')
        page = data.events_page(self.path, after=0, limit=2, category='program', actor='alice', severity='error', q='needle')
        self.assertEqual([item['event_seq'] for item in page['items']], [first['event_seq'], second['event_seq']])
        self.assertTrue(page['has_more'])
        tail = data.events_page(self.path, after=page['next_cursor'], category='program', actor='alice', severity='error', q='needle')
        self.assertEqual([item['event_seq'] for item in tail['items']], [third['event_seq']])
        self.assertFalse(tail['has_more'])

    def test_recent_before_and_time_range(self):
        for n in range(4): self.append('program_log', {'n': n})
        page = data.events_page(self.path, limit=2)
        self.assertEqual([item['event_seq'] for item in page['items']], [4, 3])
        self.assertEqual(data.events_page(self.path, before=page['next_cursor'])['items'][0]['event_seq'], 2)
        self.assertEqual(data.events_page(self.path, start='2030-01-01T00:00:00Z')['items'], [])
        for kwargs in [{'after': 0, 'before': 3}, {'limit': 101}, {'severity': 'made_up'}, {'start': 'yesterday'}, {'category': "all';DROP TABLE events;--"}]:
            with self.assertRaises(ValueError): data.events_page(self.path, **kwargs)
        self.assertEqual(len(data.events_page(self.path, q="';DROP TABLE events;--")['items']), 0)

    def test_large_payload_has_complete_original_unicode_pages(self):
        digest = 'a' * 64
        payload = {'text': '实验数据🙂' * 10000, 'artifact_sha256': digest}
        event = self.append('program_log', payload)
        page = data.events_page(self.path)
        self.assertTrue(page['items'][0]['payload_truncated'])
        detail = data.event_detail(self.path, event['event_seq'], limit=1000)
        self.assertEqual(detail['artifact_refs'], [digest])
        raw, offset = detail['payload_page']['content'], detail['payload_page']['next_offset']
        while offset is not None:
            chunk = data.event_detail(self.path, event['event_seq'], offset, 64000)['payload_page']
            raw += chunk['content']; offset = chunk['next_offset']
        self.assertEqual(json.loads(raw), payload)
        self.assertLess(len(json.dumps(detail, ensure_ascii=False).encode()), 512000)

    def test_byte_limited_event_pages_keep_filtered_cursor_correct(self):
        for n in range(12): self.append('program_log', {'text': '中' * 15000, 'n': n})
        page = data.events_page(self.path, after=0, limit=12)
        self.assertLess(len(page['items']), 12)
        self.assertTrue(page['has_more'])
        self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 512000)
        next_page = data.events_page(self.path, after=page['next_cursor'], limit=12)
        self.assertEqual(next_page['items'][0]['event_seq'], page['items'][-1]['event_seq'] + 1)

    def test_large_observer_report_retains_structured_preview_and_original(self):
        findings = [{'status': 'insufficient_evidence', 'claim': '中' * 3000, 'evidence_refs': [{'event_seq': 1}]} for _ in range(10)]
        event = self.append('observer_report', {'findings': findings}, 'observer')
        page = data.observer_page(self.path)
        self.assertEqual(len(page['items'][0]['findings']), 4)
        self.assertTrue(page['items'][0]['findings'][0]['preview'])
        self.assertTrue(page['items'][0]['payload_truncated'])
        self.assertEqual(data.event_detail(self.path, event['event_seq'])['payload_page']['total_characters'], len(json.dumps({'findings': findings}, ensure_ascii=False)))

    def test_budget_unknown_cost_remains_null_and_roles_split(self):
        self.recorder.set_state('budget', {'calls': 2, 'cost_usd': .3, 'pending': {}})
        self.append('model_usage', {'role': 'player', 'purpose': 'agent_decision', 'normalized_usage': {'input_tokens': 12, 'output_tokens': 3, 'cached_input_tokens': 5}, 'cost': {'cost_usd': .3}})
        self.append('model_usage', {'role': 'observer', 'purpose': 'context_compaction', 'normalized_usage': {'input_tokens': 5, 'output_tokens': 1}}, 'observer')
        self.append('model_response', {'usage': {'cost_usd': 1000}})
        value = data.overview(self.path)
        self.assertIsNone(value['costs']['cost_usd'])
        self.assertEqual(value['costs']['known_cost_usd'], .3)
        self.assertEqual(value['costs']['unknown_cost_calls'], 1)
        self.assertEqual(len(value['costs']['groups']), 2)
        self.assertEqual(sum(item['calls'] for item in value['costs']['groups']), 2)
        self.assertEqual(value['run']['run_id'], 'test-run')
        self.assertTrue(value['run']['diagnostic_only'])

    def test_boolean_usage_is_unknown_and_zero_cost_is_known(self):
        self.append('model_usage', {'normalized_usage': {'input_tokens': True, 'output_tokens': 0}, 'cost': {'cost_usd': 0}})
        value = data.overview(self.path)
        self.assertIsNone(value['costs']['groups'][0]['input_tokens'])
        self.assertEqual(value['costs']['cost_usd'], 0)
        self.assertEqual(value['costs']['groups'][0]['output_tokens'], 0)

    def test_fake_model_with_real_runtime_is_diagnostic(self):
        manifest = json.loads((self.path / 'manifest.json').read_text())
        manifest['config']['runtime']['mode'] = 'minecraft'
        manifest['model_kind'] = 'fake'
        manifest['diagnostic_only'] = False
        (self.path / 'manifest.json').write_text(json.dumps(manifest))
        self.assertTrue(data.overview(self.path)['run']['diagnostic_only'])

    def test_player_claim_and_observer_cannot_establish_success(self):
        self.append('observer_report', {'findings': [{'status': 'verified', 'claim': 'won'}]}, 'observer')
        self.append('program_log', {'text': 'game complete'})
        value = data.overview(self.path)
        self.assertIsNone(value['evaluation'])
        self.assertIsNone(value['first_success'])
        self.assertTrue(data.observer_page(self.path)['items'][0]['advisory'])
        self.append('run_finished', {'evaluation': {'status': 'diagnostic_pass', 'capability_success': None}}, 'supervisor')
        with data._LOCK: data._CACHE.clear()
        self.assertEqual(data.overview(self.path)['evaluation']['status'], 'diagnostic_pass')

    def test_program_rows_merge_submission_and_terminal_and_keep_unknown(self):
        self.submission('r1', 'e1', 'succeeded', started_at='2026-10-21T00:00:00Z', finished_at='2026-10-21T00:00:02Z', source_ref={'sha256': 'a' * 64})
        self.append('execution_accepted', {'status': 'accepted'}, execution_id='e1')
        self.append('execution_finished', {'status': 'succeeded'}, execution_id='e1')
        self.submission('r2')
        page = data.programs_page(self.path, limit=1)
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['items'][0]['status'], 'unknown')
        self.assertTrue(page['has_more'])
        next_page = data.programs_page(self.path, cursor=page['next_cursor'])
        self.assertEqual(len(next_page['items']), 1)
        self.assertEqual(next_page['items'][0]['execution_id'], 'e1')
        self.assertEqual(next_page['items'][0]['duration_ms'], 2000)
        self.assertEqual(next_page['items'][0]['last_event_seq'], 2)
        detail = data.program_detail(self.path, 'e1', actor='alice')
        self.assertEqual(len(detail['events']), 2)
        self.assertEqual(detail['source_refs'], [{'sha256': 'a' * 64}])
        self.assertEqual(data.program_detail(self.path, 'request:r2')['program']['status'], 'unknown')

    def test_program_big_receipt_does_not_hide_terminal_status(self):
        self.submission('r', 'big', 'succeeded', result={'text': 'x' * 300000})
        page = data.programs_page(self.path)
        self.assertEqual(page['items'][0]['status'], 'succeeded')
        detail = data.program_detail(self.path, 'big')
        self.assertTrue(detail['receipt']['truncated'])
        self.assertLess(len(json.dumps(detail).encode()), 512000)

    def test_job_lifecycle_merged_and_is_not_claimed_as_live(self):
        self.append('job_started', {'payload': {'job': {'job_id': 'j', 'status': 'running', 'ttl_ms': 5000}}}, execution_id='e')
        self.append('job_log', {'payload': {'job_id': 'j', 'message': 'work'}}, execution_id='e')
        self.append('job_finished', {'payload': {'job': {'job_id': 'j', 'status': 'cancelled', 'cancel_reason': 'pause'}}}, execution_id='e')
        page = data.jobs_page(self.path)
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['items'][0]['status'], 'cancelled')
        self.assertEqual(page['items'][0]['ttl_ms'], 5000)
        self.assertFalse(page['completeness']['live_state'])

    def test_interrupted_job_keeps_creation_epoch_and_reset_reason(self):
        self.append('job_started', {'payload': {'job': {'job_id': 'j', 'status': 'running', 'runtime_epoch': 1}}}, runtime_epoch=1)
        self.append('job_interrupted', {'payload': {'job': {'job_id': 'j', 'status': 'interrupted', 'runtime_epoch': 1, 'reset_reason': 'worker_exit'}}}, runtime_epoch=2)
        job = data.jobs_page(self.path)['items'][0]
        self.assertEqual(job['runtime_epoch'], 1)
        self.assertEqual(job['last_event_runtime_epoch'], 2)
        self.assertEqual(job['cancel_reason'], 'worker_exit')

    def test_server_chat_dedup_preserves_repeated_messages_at_distinct_locations(self):
        stamp = '2026-10-21T01:02:03Z'
        messages = [{'kind': 'chat', 'actor_id': 'alice', 'message': 'hello', 'source_time': stamp,
                     'source': {'generation': 'g', 'offset_start': n, 'offset_end': n + 10}} for n in (0, 10)]
        document = {'complete': True, 'normalizer_version': 'test', 'events': messages}
        raw = json.dumps(document).encode(); digest = hashlib.sha256(raw).hexdigest()
        (self.path / 'artifacts' / digest).write_bytes(raw)
        server = self.recorder.append_server_event('server_log_segment', {'artifact_sha256': digest}, actor_id='server_collector')
        client = self.append('bot_chat', {'payload': ['alice', 'hello']}, 'bob')
        self.recorder.db.execute('UPDATE events SET received_at=? WHERE event_seq=?', (stamp, client['event_seq'])); self.recorder.db.commit()
        page = data.chat_page(self.path, limit=1)
        self.assertEqual(page['items'][0]['verification'], 'server_normalized')
        self.assertTrue(page['has_more'])
        next_page = data.chat_page(self.path, cursor=page['next_cursor'])
        self.assertEqual(len(next_page['items']), 1)
        self.assertFalse(next_page['has_more'])
        self.assertEqual(page['items'][0]['event_seq'], server['event_seq'])
        self.assertNotEqual(page['items'][0]['item_index'], next_page['items'][0]['item_index'])

    def test_client_delivery_dedup_is_explicitly_heuristic(self):
        for actor in ['alice', 'bob', 'carol']:
            event = self.append('bot_chat', {'payload': ['alice', 'hi']}, actor)
            self.recorder.db.execute('UPDATE events SET received_at=? WHERE event_seq=?', ('2026-10-21T01:02:03Z', event['event_seq']))
        self.recorder.db.commit()
        page = data.chat_page(self.path)
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['items'][0]['duplicate_deliveries'], 2)
        self.assertIn('heuristic', page['items'][0]['deduplication'])
        self.assertEqual(page['items'][0]['verification'], 'client_delivery')

    def test_readonly_report_paging_and_symlinks_rejected(self):
        reports = self.path / 'reports'; reports.mkdir()
        content = json.dumps({'text': '中文🙂' * 300}, ensure_ascii=False)
        (reports / 'report.json').write_text(content)
        page = data.report_page(self.path, limit=100)
        self.assertEqual(page['content'], content[:100])
        self.assertEqual(data.report_page(self.path, offset=page['next_offset'], limit=100)['content'], content[100:200])
        (reports / 'report.md').symlink_to(reports / 'report.json')
        with self.assertRaises(ValueError): data.report_page(self.path, format='markdown')
        with self.assertRaises(ValueError): data.report_page(self.path, format='../../manifest')

    def test_missing_report_and_state_only_resources_are_explicit(self):
        self.recorder.set_state('resources', {'python_rss_bytes': 1000, 'sampled_at': '2026-10-21T00:00:00Z'})
        self.assertFalse(data.report_page(self.path)['available'])
        page = data.resources_page(self.path)
        self.assertEqual(page['items'], [])
        self.assertFalse(page['completeness']['history_available'])
        self.assertEqual(page['latest']['python_rss_bytes'], 1000)

    def test_all_views_do_not_mutate_ledger(self):
        self.append('run_started')
        self.submission('r', 'e', 'succeeded')
        self.recorder.close()
        database = self.path / 'events.sqlite3'
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        for function in [data.events_page, data.overview, data.programs_page, data.jobs_page, data.chat_page, data.observer_page, data.resources_page, data.report_page]:
            function(self.path)
        data.event_detail(self.path, 1)
        data.program_detail(self.path, 'e')
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
