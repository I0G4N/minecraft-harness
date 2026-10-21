"""Failures in logging or maintenance must still release process/world ownership."""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from minecraft_harness.config import validate_config
from minecraft_harness.recorder import Recorder, StorageLimitError
from minecraft_harness.supervisor import Supervisor, is_active


class SupervisorSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_ownership_never_saves_or_stops_another_runs_world(self):
        with tempfile.TemporaryDirectory() as directory:
            config = validate_config({'agents': {'count': 1}})
            operator = Supervisor(config, directory)
            with patch('minecraft_harness.supervisor.RuntimeOwnership.acquire', side_effect=RuntimeError('owned by someone else')), patch('minecraft_harness.supervisor.ServerControl.save') as save, patch('minecraft_harness.supervisor.ServerControl.stop') as stop:
                result = await operator.run()
            self.assertEqual(result['status'], 'infra_failed')
            save.assert_not_called()
            stop.assert_not_called()
            self.assertFalse(is_active(directory))
            self.assertTrue(operator.recorder.closed)

    async def test_failed_report_export_still_releases_locks_and_children(self):
        with tempfile.TemporaryDirectory() as directory:
            operator = Supervisor(validate_config({'agents': {'count': 1}}), directory)
            with patch.object(Recorder, 'export_jsonl', side_effect=OSError('disk full fixture')):
                result = await operator.run()
            self.assertEqual(result['status'], 'infra_failed')
            self.assertFalse(is_active(directory))
            self.assertTrue(operator.recorder.closed)
            self.assertIsNotNone(operator.gateway.process.returncode)

    async def test_copy_cancellation_waits_for_worker_before_world_lifecycle_continues(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def copying():
            started.set()
            release.wait(2)
            finished.set()
        operator = Supervisor(validate_config({}), '/tmp/unused-copy-fixture')
        task = asyncio.create_task(operator._copy_safely(copying))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_storage_exhaustion_fails_without_capability_claim_and_frees_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            operator = Supervisor(validate_config({'agents': {'count': 1}}), directory)
            with patch.object(Recorder, 'storage_status', return_value={'limit_exceeded': True}):
                result = await operator.run()
            self.assertEqual(result['status'], 'resource_exhausted')
            self.assertFalse(is_active(directory))
            self.assertTrue(operator.recorder.closed)
            self.assertIsNotNone(operator.gateway.process.returncode)

    async def test_restore_lineage_and_epoch_commit_together_and_receipt_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory)
            payload = {'rollback': True, 'source_world_epoch': 0, 'restore_point_event_seq': 1}
            epoch = recorder.apply_restore_transition('receipt-1', payload, ['alice', 'bob'])
            self.assertEqual(epoch, 1)
            self.assertEqual(recorder.apply_restore_transition('receipt-1', payload, ['alice', 'bob']), 1)
            self.assertEqual(len([x for x in recorder.all_events() if x['kind'] == 'world_restored']), 1)
            with patch.object(recorder, '_insert', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    recorder.apply_restore_transition('receipt-2', payload, ['alice'])
            self.assertEqual(recorder.get_state('world_epoch'), 1)
            self.assertEqual(recorder.get_state('last_applied_restore_receipt'), 'receipt-1')
            recorder.close()

    async def test_known_terminal_execution_survives_late_unknown_and_session_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory)
            pending, _ = recorder.reserve_submission('alice', 'req-1', 'digest', {'session_id': 'old', 'status': 'unknown'})
            recorder.update_submission('alice', 'req-1', {'execution_id': 'exec-1', 'status': 'succeeded', 'result': 42})
            recorder.update_submission('alice', 'req-1', {'execution_id': 'exec-1', 'status': 'unknown'})
            self.assertEqual(recorder.execution('alice', 'exec-1')['result'], 42)
            self.assertEqual(recorder.settle_session('alice', 'old'), 0)
            recorder.reserve_submission('alice', 'req-2', 'digest', {'session_id': 'old', 'status': 'unknown'})
            self.assertEqual(recorder.settle_session('alice', 'old'), 1)
            self.assertTrue(recorder.submission('alice', 'req-2')['result_unknown'])
            self.assertEqual(recorder.execution('alice', 'exec-1')['status'], 'succeeded')
            recorder.close()

    async def test_legacy_manifest_defaults_migrate_without_renewing_expired_wall_budget(self):
        import json
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            created = datetime.now(timezone.utc) - timedelta(hours=2)
            old_config = {'agents': {'count': 1}}
            (path / 'manifest.json').write_text(json.dumps({'run_id': 'legacy-run', 'created_at': created.isoformat(), 'config': old_config}))
            operator = Supervisor(validate_config(old_config), path, resume=True)
            with patch('minecraft_harness.supervisor.ExecutionGateway.start') as start, patch('minecraft_harness.supervisor.ServerControl.save') as save:
                result = await operator.run()
            self.assertEqual(result['status'], 'budget_exhausted')
            start.assert_not_called()
            save.assert_not_called()
            recorder = Recorder(path)
            self.assertAlmostEqual(recorder.get_state('budget_deadline_unix'), created.timestamp()+3600, places=2)
            self.assertEqual(len([event for event in recorder.all_events() if event['kind']=='config_defaults_migrated']),1)
            recorder.close()

    async def test_large_event_pages_are_byte_bounded_and_export_streams_without_batch_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory)
            body = 'x' * (1536*1024)
            for index in range(7):
                recorder.append('large-fixture', {'index':index,'body':body})
            first = recorder.read_events(limit=1000)
            self.assertLess(len(first),7)
            second = recorder.read_events(first[-1]['event_seq'])
            self.assertEqual(len(first)+len(second),7)
            with patch.object(recorder,'read_events',side_effect=AssertionError('batch loading forbidden in export')):
                exported = recorder.export_jsonl()
            with exported.open() as stream:
                self.assertEqual(sum(1 for _ in stream),7)
            recorder.close()
