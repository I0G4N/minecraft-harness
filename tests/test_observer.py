import json
from pathlib import Path
import tempfile
import unittest

from minecraft_harness.observer import build_report, read_events, write_report
from minecraft_harness.evaluation import ServerEvidenceCollector
from minecraft_harness.recorder import Recorder


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.recorder = Recorder(self.run)
        self.addCleanup(self.recorder.close)
        (self.run / "manifest.json").write_text(json.dumps({"run_id": "test", "config": {"experiment": {"task": "observe_only"}}}))

    def test_report_is_repeatable_and_observer_cannot_write_memory(self):
        self.recorder.append("execution_accepted", {}, execution_id="job1")
        self.recorder.append("execution_finished", {"status": "succeeded"}, execution_id="job1")
        memory = self.run / "memory" / "team"
        memory.mkdir(parents=True)
        note = memory / "note.txt"
        note.write_text("unchanged")
        before = read_events(self.run)
        result = write_report(self.run)
        first = {name: Path(path).read_bytes() for name, path in result.items()}
        write_report(self.run)
        self.assertEqual(first, {name: Path(path).read_bytes() for name, path in result.items()})
        self.assertEqual(before, read_events(self.run))
        self.assertEqual(note.read_text(), "unchanged")
        with self.assertRaises(ValueError):
            write_report(self.run, output_dir=memory)

    def test_unknown_usage_is_null_and_terminal_status_deduplicated(self):
        self.recorder.append("execution_accepted", {}, execution_id="job1")
        self.recorder.append("execution_finished", {"status": "timed_out"}, execution_id="job1")
        self.recorder.append("execution_finished", {"status": "timed_out"}, execution_id="job1")
        self.recorder.append("model_usage", {"usage": {"input_tokens": None, "output_tokens": 12}})
        report = build_report(self.run)
        self.assertEqual(report["metrics"]["execution_count"], 1)
        self.assertEqual(report["metrics"]["program_timeout_rate"], 1)
        self.assertIsNone(report["metrics"]["input_tokens"])
        self.assertIsNone(report["metrics"]["estimated_cost_usd"])
        self.assertEqual(report["metrics"]["output_tokens"], 12)
        self.assertIsNone(report["metrics"]["game_running_time_s"])

    def test_report_includes_observer_review_and_server_clock_placeholders(self):
        self.recorder.append("observer_report", {"progress_summary": "reviewed 3 events",
                                                  "verified_events": [], "infrastructure_findings": [], "findings": []})
        report = build_report(self.run)
        self.assertEqual(report["observer_review"]["report_count"], 1)
        self.assertEqual(report["observer_review"]["latest_reports"][0]["progress_summary"], "reviewed 3 events")
        self.assertIn("in_game_days", report)
        self.assertIn("success_log", report)
        self.assertFalse(report["success_log"]["available"])

    def test_agent_claim_cannot_create_execution_success(self):
        self.recorder.append("execution_succeeded", {"status": "succeeded"}, source="agent_claim", execution_id="fake")
        report = build_report(self.run)
        self.assertEqual(report["metrics"]["execution_count"], 0)
        self.assertNotEqual(report["evaluation"]["status"], "diagnostic_pass")

    def test_latency_comes_from_response_without_double_counting_usage(self):
        self.recorder.append("model_usage", {"usage": {"input_tokens": 10, "output_tokens": 4}})
        self.recorder.append("model_response", {"usage": {"input_tokens": 10, "output_tokens": 4}, "latency_ms": 1250})
        report = build_report(self.run)
        self.assertEqual(report["metrics"]["model_call_count"], 1)
        self.assertEqual(report["metrics"]["input_tokens"], 10)
        self.assertEqual(report["metrics"]["agent_model_wait_sum_s"], 1.25)
        self.recorder.append("model_usage", {"usage": {"input_tokens": None, "output_tokens": None}})
        self.assertIsNone(build_report(self.run)["metrics"]["agent_model_wait_sum_s"])

    def test_cost_estimate_uses_frozen_rates_only_with_complete_usage(self):
        (self.run / "manifest.json").write_text(json.dumps({"config": {"budgets": {"prices_per_million": {"input": 1, "output": 2}}}}))
        self.recorder.append("model_usage", {"usage": {"input_tokens": 1000000, "output_tokens": 500000}})
        self.assertEqual(build_report(self.run)["metrics"]["estimated_cost_usd"], 2)
        self.recorder.append("model_usage", {"usage": {"input_tokens": None, "output_tokens": None}})
        self.assertIsNone(build_report(self.run)["metrics"]["estimated_cost_usd"])

    def test_rescoring_fake_model_run_cannot_promote_capability_success(self):
        (self.run / "manifest.json").write_text(json.dumps({"diagnostic_only": True}))
        report = build_report(self.run, evaluation_result={"status": "succeeded", "capability_success": True})
        self.assertEqual(report["evaluation"]["status"], "diagnostic_pass")
        self.assertIsNone(report["evaluation"]["capability_success"])

    def test_server_log_counts_messages_without_counting_recipient_copies(self):
        collector = ServerEvidenceCollector(self.run, self.recorder)
        log = self.root / "latest.log"
        log.write_text('[12:00:00] [Server thread/INFO]: <player1> hello\n')
        collector.collect_log(log)
        log.write_text('[12:00:00] [Server thread/INFO]: <player1> hello\n'
                       '[12:00:01] [Server thread/INFO]: [Not Secure] <player2> hi\n')
        collector.collect_log(log)
        for _ in range(4):
            self.recorder.append("bot_chat", {"message": "hello"}, source="bot_runtime")
        report = build_report(self.run)
        self.assertEqual(report["metrics"]["chat_message_count"], 2)
        self.assertEqual(report["metrics"]["chat_delivery_count"], 4)

    def test_symlink_destinations_cannot_escape_reports(self):
        target = self.root / "target"
        target.mkdir()
        (self.run / "reports").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            write_report(self.run)
        self.assertFalse(list(target.iterdir()))

    def test_build_report_does_not_create_files(self):
        missing = self.root / "missing"
        result = build_report(missing)
        self.assertEqual(result["event_count"], 0)
        self.assertFalse(missing.exists())




class StreamingObserverTests(unittest.TestCase):
    setUp = ObserverTests.setUp

    def test_all_recovery_terminal_states_are_included_in_both_report_paths(self):
        for index,status in enumerate(('succeeded','interrupted','rejected','invalidated')):
            identity=f'job-{index}'
            self.recorder.append('execution_submitted',{},execution_id=identity)
            self.recorder.append('execution_'+status,{},execution_id=identity)
        expected={'succeeded':1,'interrupted':1,'rejected':1,'invalidated':1}
        for events in (None,read_events(self.run)):
            with self.subTest(streaming=events is None):
                report=build_report(self.run,events=events)
                self.assertEqual(report['metrics']['execution_count'],4)
                self.assertEqual(report['metrics']['execution_terminal_counts'],expected)

    def test_submission_outcomes_without_execution_ids_are_reported(self):
        for index,status in enumerate(('rejected','interrupted','unknown')):
            self.recorder.submission('alice',f'request-{index}',digest=f'digest-{index}',value={'request_id':f'request-{index}','status':status})
        report=build_report(self.run)
        self.assertEqual(report['metrics']['execution_count'],0)
        self.assertEqual(report['metrics']['submission_count'],3)
        self.assertEqual(report['metrics']['submission_status_counts'],{'interrupted':1,'rejected':1,'unknown':1})

    def test_default_report_streams_without_read_events_materialization(self):
        from unittest.mock import patch
        for index in range(100):
            self.recorder.append('execution_accepted', {'code': 'x' * 4096}, execution_id=f'job-{index}')
            self.recorder.append('execution_finished', {'status': 'succeeded'}, execution_id=f'job-{index}')
        with patch('minecraft_harness.observer.report.read_events', side_effect=AssertionError('full ledger read')):
            report = build_report(self.run)
        self.assertEqual(report['event_count'], 200)
        self.assertEqual(report['metrics']['execution_count'], 100)
        self.assertEqual(report['metrics']['execution_terminal_counts'], {'succeeded': 100})

    def test_failure_detail_is_bounded_with_exact_omitted_count(self):
        for index in range(1005):
            self.recorder.append('infrastructure_error', {'detail': str(index)})
        report = build_report(self.run)
        self.assertEqual(len(report['failures']), 1000)
        self.assertEqual(report['failure_count'], 1005)
        self.assertEqual(report['failures_omitted'], 5)


if __name__ == "__main__":
    unittest.main()
