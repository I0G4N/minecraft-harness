import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from minecraft_harness.recorder import Recorder, StorageLimitError, TransportGapError


class RecorderLongRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.recorder = Recorder(self.root, run_id="fixture-run")
        self.addCleanup(self.recorder.close)

    def event(self, cursor, kind="bot_chat", **changes):
        return {"cursor": cursor, "session_id": "session-a", "run_id": "fixture-run",
                "kind": kind, "payload": {"text": f"message-{cursor}"}, **changes}

    def reopen(self):
        self.recorder.close()
        self.recorder = Recorder(self.root, run_id="fixture-run")
        self.addCleanup(self.recorder.close)

    def test_transport_batch_is_durable_before_ack_and_replayed_batch_is_noop(self):
        batch = [self.event(1), self.event(2, "runtime_reset")]
        self.assertEqual(self.recorder.ingest_transport("alice", "session-a", batch), 2)
        events = list(self.recorder.all_events())
        players = self.recorder.player_events("alice")
        self.assertEqual([event["kind"] for event in players], ["chat", "runtime_reset"])
        self.assertEqual(len(events), 4)
        self.reopen()
        self.assertEqual(self.recorder.transport_cursor("alice", "session-a"), 2)
        self.assertEqual(self.recorder.ingest_transport("alice", "session-a", batch), 2)
        self.assertEqual(list(self.recorder.all_events()), events)
        self.assertEqual(self.recorder.player_events("bob"), [])

    def test_gap_rolls_back_entire_batch_including_execution_and_player_indexes(self):
        first = self.event(1, "execution_finished", payload={"record": {"execution_id": "exec-1", "status": "succeeded"}})
        with self.assertRaises(TransportGapError):
            self.recorder.ingest_transport("alice", "session-a", [first, self.event(3)])
        self.assertEqual(self.recorder.transport_cursor("alice", "session-a"), 0)
        self.assertEqual(list(self.recorder.all_events()), [])
        self.assertIsNone(self.recorder.execution("alice", "exec-1"))
        self.assertEqual(self.recorder.player_events("alice"), [])
        self.assertEqual(self.recorder.ingest_transport("alice", "session-a", [first, self.event(2)]), 2)

    def test_mixed_session_or_storage_failure_never_advances_ack_cursor(self):
        with self.assertRaises(TransportGapError):
            self.recorder.ingest_transport("alice", "session-a", [self.event(1), self.event(2, session_id="session-b")])
        self.assertEqual(self.recorder.latest_event_seq(), 0)
        self.recorder.limits["max_artifact_bytes"] = 1
        with self.assertRaises(StorageLimitError):
            self.recorder.ingest_transport("alice", "session-a", [self.event(1, payload={"source": "return 2"})])
        self.assertEqual(self.recorder.transport_cursor("alice", "session-a"), 0)
        self.assertEqual(self.recorder.latest_event_seq(), 0)

    def test_submission_sequence_and_identity_are_atomic_and_survive_restart(self):
        self.recorder.limits["max_event_bytes"] = 1
        with self.assertRaises(StorageLimitError):
            self.recorder.reserve_submission("alice", "request-a", "digest-a", {"status": "unknown"})
        self.assertIsNone(self.recorder.submission("alice", "request-a"))
        self.assertEqual(self.recorder.get_state("program_sequence:alice", 0), 0)
        self.recorder.limits.clear()
        record, fresh = self.recorder.reserve_submission("alice", "request-a", "digest-a", {"status": "unknown"})
        self.assertTrue(fresh)
        self.assertEqual(record["request_sequence"], 1)
        self.reopen()
        duplicate, fresh = self.recorder.reserve_submission("alice", "request-a", "digest-a", {"status": "would-reexecute"})
        self.assertFalse(fresh)
        self.assertEqual(duplicate, record)
        self.assertEqual(self.recorder.latest_event_seq(), 1)
        second, _ = self.recorder.reserve_submission("alice", "request-b", "digest-b", {"status": "unknown"})
        self.assertEqual(second["request_sequence"], 2)
        with self.assertRaises(ValueError):
            self.recorder.reserve_submission("alice", "request-a", "different", {})

    def test_concurrent_submission_sequences_are_unique_and_duplicate_is_one_admission(self):
        def submit(index):
            return self.recorder.reserve_submission("alice", str(index), str(index), {"status": "unknown"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, [index // 2 for index in range(80)]))
        self.assertEqual(sum(fresh for _, fresh in results), 40)
        self.assertEqual({record["request_sequence"] for record, _ in results}, set(range(1, 41)))
        self.assertEqual(self.recorder.latest_event_seq(), 40)

    def test_state_and_corresponding_audit_event_commit_or_rollback_together(self):
        self.recorder.set_state("budget", {"calls": 0})
        with patch.object(self.recorder, "_insert", side_effect=StorageLimitError("disk full")):
            with self.assertRaises(StorageLimitError):
                self.recorder.state_event("budget", {"calls": 1}, "model_admitted", {"reservation_id": "r"})
        self.assertEqual(self.recorder.get_state("budget"), {"calls": 0})
        self.assertEqual(self.recorder.latest_event_seq(), 0)
