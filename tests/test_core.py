import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path

from minecraft_harness.budget import Budget, BudgetExhausted
from minecraft_harness.config import validate_config
from minecraft_harness.recorder import Recorder


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def recorder(self):
        value = Recorder(self.root)
        self.addCleanup(value.close)
        return value

    async def test_concurrent_agents_share_one_durable_call_limit(self):
        recorder = self.recorder()
        config = validate_config({"budgets": {"total_model_calls": 7}})["budgets"]
        budget = Budget(config, recorder)

        async def request(index):
            try:
                return await budget.reserve(f"player{index % 4}")
            except BudgetExhausted:
                return None

        results = await asyncio.gather(*(request(index) for index in range(40)))
        admitted = [reservation for reservation in results if reservation is not None]
        self.assertEqual(len(admitted), 7)
        self.assertEqual(len(set(admitted)), 7)
        await asyncio.gather(*(budget.settle(reservation, {"prompt_tokens": 3, "completion_tokens": 2})
                               for reservation in admitted))
        # Duplicate network completion delivery must not bill a reservation twice.
        await budget.settle(admitted[0], {"prompt_tokens": 999, "completion_tokens": 999})
        recorder.close()
        reopened = self.recorder()
        restored_budget = Budget(config, reopened)
        self.assertEqual(restored_budget.state["calls"], 7)
        self.assertEqual(restored_budget.state["input_tokens"], 21)
        self.assertEqual(restored_budget.state["output_tokens"], 14)
        with self.assertRaises(BudgetExhausted):
            await restored_budget.reserve("player1")

    @staticmethod
    def cost_config():
        return validate_config({"budgets": {"max_cost_usd": 100,
            "max_input_tokens": 1000, "prices_per_million": {"input": 1, "output": 2}}})["budgets"]

    async def test_unknown_usage_is_charged_conservatively_and_keeps_paid_admission_open(self):
        budget = Budget(self.cost_config(), self.recorder())
        reservation = await budget.reserve("player1")
        await budget.settle(reservation, {"prompt_tokens": None, "completion_tokens": None})
        self.assertAlmostEqual(budget.state["cost_usd"], 1000 / 1e6 * 1 + 4096 / 1e6 * 2)
        await budget.reserve("player2")
        self.assertEqual(budget.state["calls"], 2)
        self.assertEqual(budget.state["unknown_usage_calls"], 1)

    async def test_crash_with_unsettled_request_is_unknown_after_reopen(self):
        recorder = self.recorder()
        config = self.cost_config()
        budget = Budget(config, recorder)
        await budget.reserve("player1")
        recorder.close()
        recovered = Budget(config, self.recorder())
        self.assertEqual(recovered.state["unknown_usage_calls"], 1)
        self.assertFalse(recovered.state["pending"])
        self.assertAlmostEqual(recovered.state["cost_usd"], 1000 / 1e6 * 1 + 4096 / 1e6 * 2)
        await recovered.reserve("player2")

    async def test_concurrent_cost_reservations_cannot_oversubscribe(self):
        config = self.cost_config()
        config.update(max_cost_usd=0.02, max_input_tokens=1000,
                      prices_per_million={"input": 10, "output": 10})
        budget = Budget(config, self.recorder(), max_output_tokens=1000)
        outcomes = await asyncio.gather(*(budget.reserve(f"player{index}") for index in range(4)),
                                        return_exceptions=True)
        self.assertEqual(sum(isinstance(item, str) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, BudgetExhausted) for item in outcomes), 3)

    def test_nonfinite_cost_time_and_price_configuration_is_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(field="wall_time_minutes", value=value):
                with self.assertRaises(ValueError):
                    validate_config({"budgets": {"wall_time_minutes": value}})
            with self.subTest(field="max_cost_usd", value=value):
                with self.assertRaises(ValueError):
                    validate_config({"budgets": {**self.cost_config(), "max_cost_usd": value}})
            with self.subTest(field="prices_per_million", value=value):
                with self.assertRaises(ValueError):
                    validate_config({"budgets": {**self.cost_config(),
                        "prices_per_million": {"input": value, "output": 1}}})

    def test_cost_reservation_bound_must_be_a_positive_integer(self):
        for value in (-1, 0, 1.5, "1000", True, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_config({"budgets": {**self.cost_config(), "max_input_tokens": value}})

    def test_submission_idempotency_survives_database_reopen(self):
        recorder = self.recorder()
        digest = hashlib.sha256(b"return 1; timeout=60000").hexdigest()
        accepted = {"status": "accepted", "execution_id": "execution-a", "source_ref": "hash-a"}
        recorder.submission("alice", "request-1", digest=digest, value=accepted)
        finished = {**accepted, "status": "succeeded", "result": 1}
        recorder.update_submission("alice", "request-1", finished)
        recorder.close()
        reopened = self.recorder()
        self.assertEqual(reopened.submission("alice", "request-1", digest=digest), finished)
        # Even a second create supplying a new value must retain the original execution.
        self.assertEqual(reopened.submission("alice", "request-1", digest=digest,
                                              value={"execution_id": "would-duplicate"}), finished)
        with self.assertRaises(ValueError):
            reopened.submission("alice", "request-1", digest=hashlib.sha256(b"return 2").hexdigest())
        self.assertIsNone(reopened.submission("bob", "request-1"))

    def test_player_events_isolate_actors_and_exclude_privileged_records(self):
        recorder = self.recorder()
        recorder.append("model_request", {"private": "alice-model-history"}, actor_id="alice")
        first = recorder.append("player_event", {"kind": "chat", "payload": {"text": "for-alice"}}, actor_id="alice")
        bob = recorder.append("player_event", {"kind": "chat", "payload": {"text": "for-bob"}}, actor_id="bob")
        recorder.append_server_event("server_stats", {"operator_only": "world-map"}, actor_id="alice")
        alice_events = recorder.player_events("alice")
        self.assertEqual(len(alice_events), 1)
        self.assertEqual(alice_events[0]["cursor"], first["event_seq"])
        self.assertEqual(alice_events[0]["payload"]["text"], "for-alice")
        self.assertEqual(recorder.player_events("bob")[0]["cursor"], bob["event_seq"])
        self.assertFalse(recorder.player_events("charlie"))

    def test_player_event_cursor_is_global_and_monotonic_across_session_recreation(self):
        recorder = self.recorder()
        first = recorder.append("player_event", {"kind": "chat", "cursor": 1}, actor_id="alice")
        recorder.append("player_event", {"kind": "chat", "cursor": 2}, actor_id="bob")
        recorder.append("runtime_reset", {"operator_diagnostic": True})
        reset = recorder.append("player_event", {"kind": "world_restored", "cursor": 0,
                                                  "world_epoch": 1}, actor_id="alice", world_epoch=1)
        page = recorder.player_events("alice", limit=1)
        self.assertEqual(page[0]["cursor"], first["event_seq"])
        second_page = recorder.player_events("alice", after=page[0]["cursor"], limit=1)
        self.assertEqual(second_page[0]["cursor"], reset["event_seq"])
        self.assertGreater(second_page[0]["cursor"], page[0]["cursor"])
        last_cursor = second_page[0]["cursor"]
        recorder.close()
        reopened = self.recorder()
        newest = reopened.append("player_event", {"kind": "chat", "cursor": 1,
                                                   "world_epoch": 1}, actor_id="alice", world_epoch=1)
        recovered = reopened.player_events("alice", after=last_cursor)
        self.assertEqual([item["cursor"] for item in recovered], [newest["event_seq"]])

    def test_artifacts_are_content_addressed_and_existing_corruption_is_rejected(self):
        recorder = self.recorder()
        first = recorder.put_artifact(b"immutable raw program")
        self.assertEqual(recorder.put_artifact(b"immutable raw program"), first)
        target = self.root / first["path"]
        target.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            recorder.put_artifact(b"immutable raw program")
