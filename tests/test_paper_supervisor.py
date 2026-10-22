"""Full offline Supervisor/Gateway/Node/AgentCore/Observer integration."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paper_lifecycle_soak.py"
_spec = importlib.util.spec_from_file_location("paper_lifecycle_fixture", _SCRIPT)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


@unittest.skipUnless(shutil.which("node"), "requires Node fake runtime")
class PaperSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_aligned_jobs_compaction_observer_and_durable_budget_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            result = await _fixture.run_soak(3, run_dir)
            self.assertEqual(result["status"], "passed", result)
            self.assertTrue(result["diagnostic_only"])
            self.assertFalse(result["actual_minecraft"])
            self.assertFalse(result["paid_model"])

    async def _observer_failure_run(self, profile="paper_aligned", model_calls=1000, stop_when_degraded=False,
                                    observer_failure_policy=None):
        import asyncio
        from contextlib import closing
        import json
        import sqlite3
        import time
        from unittest.mock import patch
        from minecraft_harness.models import ModelError, ModelResponse
        from minecraft_harness.budget import Budget
        from minecraft_harness.supervisor import Supervisor, request_control

        class PlayerModel:
            calls = 0
            async def complete(self, messages, schemas):
                self.calls += 1
                await asyncio.sleep(.03)
                return ModelResponse(tool_calls=[{"id": f"write-{self.calls}", "type": "function", "function": {
                    "name": "workspace_write", "arguments": json.dumps({"path": "progress", "content": str(self.calls)})}}],
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        class BrokenObserver:
            async def complete(self, messages, schemas):
                raise ModelError("private-provider-message")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run"
            supervision = {"poll_interval_s": .01}
            if observer_failure_policy is not None:
                supervision["observer_failure_policy"] = observer_failure_policy
            supervisor = Supervisor({"experiment": {"profile": profile, "task": "observe_only"},
                "runtime": {"lifecycle": "paper_aligned", "mode": "fake", "heartbeat_interval_s": .2},
                "agents": {"count": 4, "max_turns": 10000, "max_no_progress_turns": 1000},
                "observer": {"mode": "llm", "interval_s": .01}, "budgets": {"total_model_calls": model_calls},
                "supervision": supervision}, path)
            created = []
            def build(config):
                model = PlayerModel() if len(created) < 4 else BrokenObserver()
                created.append(model)
                return model
            original_reserve = Budget.reserve
            async def reserve(instance, actor_id, *, purpose="agent_decision", role="player"):
                if role == "observer" and model_calls == 4:
                    while instance.state["calls"] < model_calls:
                        await asyncio.sleep(.001)
                return await original_reserve(instance, actor_id, purpose=purpose, role=role)
            with patch("minecraft_harness.supervisor.build_model", side_effect=build), patch.object(Budget, "reserve", reserve):
                task = asyncio.create_task(supervisor.run())
                try:
                    if stop_when_degraded:
                        deadline = time.monotonic() + 10
                        while not task.done() and time.monotonic() < deadline:
                            if supervisor.recorder and supervisor.recorder.get_state("observer_degraded"):
                                self.assertEqual(supervisor.status, "running")
                                request_control(path, "stop")
                                break
                            await asyncio.sleep(.02)
                        else:
                            self.fail("Optional Observer failure was not reported as degraded")
                    result = await asyncio.wait_for(task, 30)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            with closing(sqlite3.connect(path / "events.sqlite3")) as connection:
                state = {key: json.loads(value) for key, value in connection.execute("SELECT key,value_json FROM state")}
                raw = " ".join(row[0] for row in connection.execute("SELECT payload_json FROM events"))
            self.assertNotIn("private-provider-message", raw)
            return result, state

    async def test_required_observer_failure_stops_under_fail_run_policy(self):
        result, state = await self._observer_failure_run(observer_failure_policy="fail_run")
        self.assertEqual(result["status"], "model_failed")
        self.assertEqual(state["observer_outcome"]["status"], "model_failed")
        self.assertTrue(state["observer_outcome"]["required"])
        self.assertEqual(state["evaluation"]["status"], "insufficient_evidence")

    async def test_required_observer_failure_degrades_by_default_while_players_continue(self):
        result, state = await self._observer_failure_run(stop_when_degraded=True)
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(state["observer_degraded"])
        self.assertTrue(state["observer_outcome"]["required"])
        self.assertEqual(state["observer_outcome"]["status"], "model_failed")
        self.assertEqual(state["evaluation"]["status"], "insufficient_evidence")

    async def test_optional_observer_failure_is_explicitly_degraded_while_players_continue(self):
        result, state = await self._observer_failure_run(profile="strict", stop_when_degraded=True)
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(state["observer_degraded"])
        self.assertFalse(state["observer_outcome"]["required"])

    async def test_observer_budget_exhaustion_stops_the_team_as_budget_exhausted(self):
        result, state = await self._observer_failure_run(model_calls=4)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(state["observer_outcome"]["status"], "budget_exhausted")
        self.assertFalse(state["budget"]["pending"])
