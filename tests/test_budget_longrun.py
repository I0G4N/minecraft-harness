import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minecraft_harness.budget import Budget, BudgetExhausted
from minecraft_harness.recorder import Recorder, StorageLimitError


class BudgetLongRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.recorder = Recorder(self.root)
        self.addCleanup(self.recorder.close)
        self.config = {"wall_time_minutes": 1, "total_model_calls": 100, "max_cost_usd": 1,
                       "max_input_tokens": 1000, "prices_per_million": {"input": 1, "output": 2}}

    def reopen(self):
        self.recorder.close()
        self.recorder = Recorder(self.root)
        self.addCleanup(self.recorder.close)

    async def test_admitted_state_and_usage_are_atomic_and_settlement_is_idempotent(self):
        budget = Budget(self.config, self.recorder, max_output_tokens=100)
        reservation = await budget.reserve("alice")
        self.assertIn(reservation, self.recorder.get_state("budget")["pending"])
        admitted = list(self.recorder.all_events())
        self.assertEqual(admitted[-1]["kind"], "model_admitted")
        await budget.settle(reservation, {"prompt_tokens": 10, "completion_tokens": 20})
        before = self.recorder.get_state("budget")
        await budget.settle(reservation, {"prompt_tokens": 100000, "completion_tokens": 100000})
        self.assertEqual(self.recorder.get_state("budget"), before)
        usage = [event for event in self.recorder.all_events() if event["kind"] == "model_usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["payload"]["budget"], before)

    async def test_pending_request_becomes_unknown_once_and_charges_conservative_reservation(self):
        budget = Budget(self.config, self.recorder)
        await budget.reserve("alice")
        self.reopen()
        recovered = Budget(self.config, self.recorder)
        self.assertEqual(recovered.state["unknown_usage_calls"], 1)
        self.assertEqual(recovered.state["calls"], 1)
        # Admission already reserved the worst case; unknown usage is charged at
        # that bound instead of freezing every later paid request.
        self.assertAlmostEqual(recovered.state["cost_usd"], 1000 / 1e6 * 1 + 4096 / 1e6 * 2)
        identity = await recovered.reserve("bob")
        await recovered.settle(identity, {"prompt_tokens": 10, "completion_tokens": 5})
        self.assertFalse(recovered.state["pending"])
        self.reopen()
        again = Budget(self.config, self.recorder)
        self.assertEqual(again.state["unknown_usage_calls"], 1)

    async def test_wall_deadline_includes_downtime_and_cannot_reset_on_resume(self):
        with patch("minecraft_harness.budget.time") as clock:
            clock.time.return_value, clock.monotonic.return_value = 1000, 500
            first = Budget(self.config, self.recorder)
            self.assertEqual(first.deadline, 560)
            self.assertEqual(self.recorder.get_state("budget_deadline_unix"), 1060)
            self.reopen()
            clock.time.return_value, clock.monotonic.return_value = 1010, 900
            resumed = Budget(self.config, self.recorder)
            self.assertEqual(resumed.deadline, 950)
            clock.time.return_value, clock.monotonic.return_value = 1061, 900
            expired = Budget(self.config, self.recorder)
            self.assertTrue(expired.stop_requested())
            with self.assertRaises(BudgetExhausted):
                await expired.reserve("alice")

    async def test_bad_usage_is_charged_conservatively_and_does_not_freeze_paid_calls(self):
        reserved = 1000 / 1e6 * 1 + 4096 / 1e6 * 2
        for index, usage in enumerate(([1], "invalid", {"prompt_tokens": True, "completion_tokens": 1})):
            with self.subTest(usage=usage):
                recorder = Recorder(self.root / f"usage-{index}")
                self.addCleanup(recorder.close)
                budget = Budget(self.config, recorder)
                reservation = await budget.reserve("alice")
                await budget.settle(reservation, usage)
                self.assertEqual(budget.state["unknown_usage_calls"], 1)
                self.assertEqual(budget.state["unknown_cost_calls"], 1)
                self.assertAlmostEqual(budget.state["cost_usd"], reserved)
                await budget.reserve("bob")

    async def test_output_reservation_violation_stops_further_calls(self):
        budget = Budget(self.config, self.recorder, max_output_tokens=100)
        reservation = await budget.reserve("alice")
        await budget.settle(reservation, {"prompt_tokens": 1, "completion_tokens": 101})
        self.assertTrue(budget.stop_requested())
        with self.assertRaises(BudgetExhausted):
            await budget.reserve("bob")

    async def test_failed_admission_transaction_cannot_be_followed_by_provider_request(self):
        budget = Budget(self.config, self.recorder)
        with patch.object(self.recorder, "_insert", side_effect=StorageLimitError("disk full")):
            with self.assertRaises(StorageLimitError):
                await budget.reserve("alice")
        persisted = self.recorder.get_state("budget", {})
        self.assertEqual(persisted.get("calls", 0), 0)
        self.assertEqual(self.recorder.latest_event_seq(), 0)
