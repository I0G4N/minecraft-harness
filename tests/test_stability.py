"""Long-run stability: agent/observer isolation, pause clock, web cap, RPC bound, compose restart."""
import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import yaml

from minecraft_harness.budget import Budget
from minecraft_harness.deployment_generation import render_compose
from minecraft_harness.models import ModelError, ModelResponse
from minecraft_harness.recorder import Recorder
from minecraft_harness.runner_rpc import RpcClient, RpcError, RpcServer
from minecraft_harness.supervisor import Supervisor
from minecraft_harness.agents.web import WebTools


ROOT = Path(__file__).resolve().parents[1]


def _supervisor_config(**overrides):
    config = {
        "experiment": {"profile": "strict", "task": "observe_only"},
        "runtime": {"mode": "fake", "lifecycle": "strict", "heartbeat_interval_s": .2},
        "agents": {"count": 2, "max_turns": 8, "max_no_progress_turns": 1000},
        "observer": {"mode": "rules"},
        "budgets": {"total_model_calls": 500},
        "supervision": {"poll_interval_s": .01},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = {**config[key], **value}
        else:
            config[key] = value
    return config


class _BudgetRecorder:
    """Small helper: a durable Recorder in a temporary directory."""

    def __init__(self, case):
        self.temporary = tempfile.TemporaryDirectory()
        case.addCleanup(self.temporary.cleanup)
        self.recorder = Recorder(Path(self.temporary.name))
        case.addCleanup(self.recorder.close)


class BudgetPauseClockTests(unittest.TestCase):
    def setUp(self):
        self.holder = _BudgetRecorder(self)
        self.recorder = self.holder.recorder
        self.config = {"wall_time_minutes": 1, "total_model_calls": 100,
                       "max_cost_usd": None}

    def test_pause_freezes_wall_deadline_and_persists_paused_seconds(self):
        with patch("minecraft_harness.budget.time") as clock:
            clock.time.return_value, clock.monotonic.return_value = 1000.0, 500.0
            budget = Budget(self.config, self.recorder)
            self.assertEqual(budget.deadline, 560.0)
            budget.pause()
            clock.monotonic.return_value = 530.0
            # Wall time alone would have expired at 560; paused time is free.
            self.assertFalse(budget.stop_requested())
            budget.resume()
            self.assertFalse(budget.paused)
        state = self.recorder.get_state("budget")
        self.assertAlmostEqual(state["paused_seconds"], 30.0)
        self.assertAlmostEqual(self.recorder.get_state("budget_deadline_unix"), 1090.0)
        kinds = [event["kind"] for event in self.recorder.all_events()]
        self.assertIn("budget_paused", kinds)
        self.assertIn("budget_resumed", kinds)

    def test_crash_during_pause_extends_durable_deadline_on_restart(self):
        with patch("minecraft_harness.budget.time") as clock:
            clock.time.return_value, clock.monotonic.return_value = 1000.0, 500.0
            first = Budget(self.config, self.recorder)
            first.pause()
            # Supervisor dies while paused; 40 s later a new Budget is built.
            clock.time.return_value, clock.monotonic.return_value = 1040.0, 800.0
            restarted = Budget(self.config, self.recorder)
            self.assertEqual(restarted.deadline, 860.0)
        self.assertIsNone(self.recorder.get_state("budget_paused_at_unix"))
        state = self.recorder.get_state("budget")
        self.assertAlmostEqual(state["paused_seconds"], 40.0)
        self.assertAlmostEqual(self.recorder.get_state("budget_deadline_unix"), 1100.0)

    def test_supervisor_active_wall_excludes_paused_window(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = Supervisor(_supervisor_config(), Path(directory) / "run")
        supervisor._paused_started = 120.0
        supervisor._paused_total = 5.0
        # t=160, start=100: 60 s elapsed, 5 s finished pause + 40 s current pause.
        self.assertEqual(supervisor._active_wall_seconds(0.0, 100.0, 160.0), 15.0)


class WebBudgetTests(unittest.TestCase):
    def setUp(self):
        self.holder = _BudgetRecorder(self)
        self.recorder = self.holder.recorder

    def _budget(self, cap):
        return Budget({"wall_time_minutes": 60, "total_model_calls": 10,
                       "max_web_calls": cap}, self.recorder)

    def test_admit_web_caps_durably_and_records_usage_events(self):
        budget = self._budget(2)
        self.assertTrue(budget.admit_web("alice", "web_search"))
        self.assertTrue(budget.admit_web("alice", "web_fetch"))
        self.assertFalse(budget.admit_web("alice", "web_search"))
        self.assertFalse(budget.stopped)
        self.assertEqual(budget.state["web_calls"], 2)
        events = [event for event in self.recorder.all_events() if event["kind"] == "web_usage"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["payload"]["tool"], "web_fetch")
        self.assertEqual(events[-1]["payload"]["max_web_calls"], 2)
        persisted = self.recorder.get_state("budget")
        self.assertEqual(persisted["web_calls"], 2)

    def test_webtools_deny_without_network_when_cap_exhausted(self):
        budget = self._budget(0)
        web = WebTools({"enabled": True, "search_url": "http://127.0.0.1:9/search",
                        "provider": "custom", "allowed_hosts": ["github.com"]},
                       budget=budget, agent_id="alice")

        async def scenario():
            search = await web.search("minecraft")
            self.assertEqual(search["status"], "unavailable")
            self.assertEqual(search["reason"], "web call budget exhausted")
            fetch = await web.fetch("https://github.com/example")
            self.assertEqual(fetch["status"], "unavailable")
            self.assertEqual(fetch["reason"], "web call budget exhausted")

        asyncio.run(scenario())
        self.assertEqual(budget.state["web_calls"], 0)
        self.assertFalse(budget.stopped)

    def test_concurrent_admission_never_exceeds_cap(self):
        import concurrent.futures
        budget = self._budget(50)

        def admit(_index):
            return budget.admit_web("alice", "web_search")

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(admit, range(200)))
        self.assertEqual(sum(1 for result in results if result), 50)
        self.assertEqual(self.recorder.get_state("budget")["web_calls"], 50)
        events = [event for event in self.recorder.all_events()
                  if event["kind"] == "web_usage"]
        self.assertEqual(len(events), 50)
        self.assertEqual(sorted(event["payload"]["web_calls"] for event in events),
                         list(range(1, 51)))

    def test_unavailable_web_backend_does_not_consume_cap(self):
        budget = self._budget(2)
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MISSING_WEB_KEY", None)
            web = WebTools({"enabled": True, "provider": "deepseek",
                            "search_url": "https://api.deepseek.com/v1/tools/search",
                            "api_key_env": "MISSING_WEB_KEY"},
                           budget=budget, agent_id="alice")

            async def scenario():
                result = await web.search("minecraft")
                self.assertEqual(result["reason"], "API key is unset")

            asyncio.run(scenario())
        self.assertEqual(budget.state["web_calls"], 0)
        self.assertEqual((self.recorder.get_state("budget") or {}).get("web_calls", 0), 0)

    def test_supervisor_web_config_carries_budget_to_webtools(self):
        budget = self._budget(0)
        with tempfile.TemporaryDirectory() as directory:
            supervisor = Supervisor(_supervisor_config(
                web={"enabled": True, "search_url": "http://127.0.0.1:9/search"}),
                Path(directory) / "run")
        supervisor.budget = budget
        config = supervisor._web_config("player1")
        web = WebTools(config)
        self.assertIs(web.budget, budget)
        self.assertEqual(web.agent_id, "player1")

        async def scenario():
            denied = await web.search("minecraft")
            self.assertEqual(denied["reason"], "web call budget exhausted")

        asyncio.run(scenario())


class RpcTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.socket_path = Path(self.temporary.name) / "control.sock"

        async def handler(agent_id, role, method, params):
            if method in {"echo", "gateway.wait", "budget.wait_admission"}:
                await asyncio.sleep(.5)
                return {"method": method, "params": params}
            return {"method": method}

        self.server = RpcServer(self.socket_path,
                                {"token-1": {"agent_id": "alice", "role": "player"}}, handler)
        await self.server.start()
        self.addAsyncCleanup(self.server.close)

    async def test_default_timeout_bounds_async_and_sync_paths(self):
        client = RpcClient(self.socket_path, "token-1", "alice", "player",
                           default_call_timeout_s=0.1)
        with self.assertRaises(RpcError) as raised:
            await client.call("echo", {"value": 1})
        self.assertEqual(raised.exception.error_type, "TimeoutError")
        self.assertTrue(raised.exception.retryable)
        with self.assertRaises(RpcError) as raised:
            await asyncio.to_thread(client.call_sync, "echo", {"value": 1})
        self.assertEqual(raised.exception.error_type, "TimeoutError")
        self.assertTrue(raised.exception.retryable)

    async def test_long_running_game_calls_are_exempt_from_default_timeout(self):
        client = RpcClient(self.socket_path, "token-1", "alice", "player",
                           default_call_timeout_s=0.1)
        result = await client.call("gateway.wait", {"execution_id": "x"})
        self.assertEqual(result["method"], "gateway.wait")
        # A long operator pause keeps the admission long-poll open past the
        # default RPC budget; it must not be killed by the generic timeout.
        admitted = await client.call("budget.wait_admission", {})
        self.assertEqual(admitted["method"], "budget.wait_admission")

    async def test_explicit_timeout_still_wins_over_default(self):
        client = RpcClient(self.socket_path, "token-1", "alice", "player",
                           default_call_timeout_s=0.05)
        result = await client.call("echo", {"value": 2}, timeout_s=2.0)
        self.assertEqual(result["params"], {"value": 2})
        with self.assertRaises(ValueError):
            RpcClient(self.socket_path, "token-1", "alice", "player",
                      default_call_timeout_s=0)


class RpcCloseRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Python 3.12 asyncio.Server.wait_closed() waits for live connections."""

    async def test_close_cancels_inflight_handler_and_returns_promptly(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "control.sock"
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def handler(agent_id, role, method, params):
                started.set()
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return {}

            server = RpcServer(socket_path,
                               {"token-1": {"agent_id": "alice", "role": "player"}},
                               handler)
            await server.start()
            client = RpcClient(socket_path, "token-1", "alice", "player",
                               default_call_timeout_s=120)
            call = asyncio.create_task(client.call("slow", {}))
            await asyncio.wait_for(started.wait(), 5)
            # Without the cancellation-before-wait_closed ordering this hangs
            # under Python 3.12 and would hit the outer wait_for.
            await asyncio.wait_for(server.close(), 5)
            self.assertTrue(cancelled.is_set())
            with self.assertRaises((RpcError, ConnectionError)):
                await asyncio.wait_for(call, 5)
            self.assertFalse(socket_path.exists())


class _FakeTask:
    def __init__(self, name, *, result=None, exception=None, cancelled=False):
        self._name = name
        self._result = result
        self._exception = exception
        self._cancelled = cancelled

    def get_name(self):
        return self._name

    def done(self):
        return True

    def cancelled(self):
        return self._cancelled

    def exception(self):
        return self._exception

    def result(self):
        if self._exception is not None:
            raise self._exception
        return self._result


class AgentTaskReapingTests(unittest.TestCase):
    def setUp(self):
        self.holder = _BudgetRecorder(self)

    def _supervisor(self, policy, directory):
        supervisor = Supervisor(_supervisor_config(
            supervision={"agent_failure_policy": policy}), Path(directory) / "run")
        supervisor.recorder = self.holder.recorder
        supervisor.budget = Budget({"wall_time_minutes": 60, "total_model_calls": 10},
                                   self.holder.recorder)
        return supervisor

    def test_crashed_and_cancelled_tasks_are_recorded_and_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self._supervisor("isolate", directory)
            crashed = _FakeTask("player1", exception=ValueError("boom"))
            cancelled = _FakeTask("player2", cancelled=True)
            self.assertFalse(supervisor._reap_agent_tasks([crashed, cancelled]))
            self.assertEqual(supervisor._agent_outcomes["player1"]["status"], "infra_failed")
            self.assertEqual(supervisor._agent_outcomes["player2"]["status"], "infra_failed")
        kinds = [event["kind"] for event in self.holder.recorder.all_events()]
        self.assertIn("agent_task_crashed", kinds)
        self.assertIn("agent_task_cancelled", kinds)
        self.assertEqual(kinds.count("agent_degraded"), 2)

    def test_fail_run_policy_breaks_on_crashed_task(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self._supervisor("fail_run", directory)
            crashed = _FakeTask("player1", exception=ValueError("boom"))
            self.assertTrue(supervisor._reap_agent_tasks([crashed]))
            self.assertTrue(supervisor._fail_run_triggered)

    def test_reaping_is_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self._supervisor("isolate", directory)
            failed = _FakeTask("player1", result={"status": "model_failed", "agent_id": "player1"})
            self.assertFalse(supervisor._reap_agent_tasks([failed]))
            self.assertFalse(supervisor._reap_agent_tasks([failed]))
        kinds = [event["kind"] for event in self.holder.recorder.all_events()]
        self.assertEqual(kinds.count("agent_degraded"), 1)

    def test_isolated_final_status_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self._supervisor("isolate", directory)
        result = {"status": "insufficient_evidence"}
        self.assertEqual(supervisor._isolated_final_status(
            [{"agent_id": "player1", "status": "turns_exhausted"},
             {"agent_id": "player2", "status": "stalled"}], result), "completed")
        self.assertEqual(supervisor._isolated_final_status(
            [{"agent_id": "player1", "status": "model_failed"},
             {"agent_id": "player2", "status": "model_failed"}], result), "model_failed")
        self.assertEqual(supervisor._isolated_final_status(
            [{"agent_id": "player1", "status": "model_failed"},
             {"agent_id": "player2", "status": "infra_failed"}], result), "infra_failed")
        self.assertEqual(supervisor._isolated_final_status(
            [{"agent_id": "player1", "status": "model_failed"},
             {"agent_id": "player2", "status": "turns_exhausted"}], result), "model_failed")


@unittest.skipUnless(shutil.which("node"), "requires the Node fake runtime")
class AgentFailurePolicyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    class GoodModel:
        def __init__(self, state):
            self.state = state

        async def complete(self, messages, schemas):
            self.state["calls"] = self.state.get("calls", 0) + 1
            await asyncio.sleep(.01)
            return ModelResponse(tool_calls=[{"id": f"write-{self.state['calls']}",
                                              "type": "function",
                                              "function": {"name": "workspace_write",
                                                           "arguments": json.dumps({
                                                               "path": f"progress-{self.state['calls']}",
                                                               "content": "tick"})}}],
                                 usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    class BrokenModel:
        async def complete(self, messages, schemas):
            raise ModelError("private-provider-message")

    class TextOnlyModel:
        async def complete(self, messages, schemas):
            await asyncio.sleep(.005)
            return ModelResponse(text="still thinking",
                                 usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    async def _run(self, policy, max_turns=8, text_only=False):
        good = {}
        created = []

        def build(_config):
            if text_only:
                model = self.TextOnlyModel()
            elif not created:
                model = self.GoodModel(good)
            else:
                model = self.BrokenModel()
            created.append(model)
            return model

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run"
            supervisor = Supervisor(_supervisor_config(
                agents={"count": 2, "max_turns": max_turns, "max_no_progress_turns": 2},
                supervision={"agent_failure_policy": policy,
                             "poll_interval_s": .01}), path)
            with patch("minecraft_harness.supervisor.build_model", side_effect=build):
                task = asyncio.create_task(supervisor.run())
                try:
                    result = await asyncio.wait_for(task, 30)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            with closing(sqlite3.connect(path / "events.sqlite3")) as connection:
                state = {key: json.loads(value) for key, value
                         in connection.execute("SELECT key,value_json FROM state")}
            return result, state, good

    async def test_isolate_keeps_other_players_running_after_model_failure(self):
        result, state, good = await self._run("isolate", max_turns=8)
        self.assertGreaterEqual(good.get("calls", 0), 7)
        self.assertTrue(state.get("agent_degraded:player2"))
        self.assertEqual(state["agent_degraded:player2"]["status"], "model_failed")
        # All players ended degraded: one model failure => aggregate model_failed.
        self.assertEqual(result["status"], "model_failed")

    async def test_fail_run_stops_immediately_on_model_failure(self):
        result, _state, good = await self._run("fail_run", max_turns=8)
        self.assertLess(good.get("calls", 0), 7)
        self.assertEqual(result["status"], "model_failed")

    async def test_mixed_bounded_endings_report_completed(self):
        result, state, _good = await self._run("isolate", max_turns=8, text_only=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(state.get("agent_degraded:player1"))
        self.assertEqual(state["agent_degraded:player1"]["status"], "stalled")


class ObserverWakeTests(unittest.TestCase):
    def test_runner_mode_observer_treats_degraded_agents_as_important(self):
        # A runner-mode Observer has no in-process wakeup() handle: it polls the
        # ledger, so degraded events must be in IMPORTANT_KINDS for the next
        # review to start promptly instead of waiting a full interval.
        from minecraft_harness.observer.agent import ObserverAgent
        self.assertIn("agent_degraded", ObserverAgent.IMPORTANT_KINDS)
        self.assertIn("agent_task_crashed", ObserverAgent.IMPORTANT_KINDS)
        self.assertIn("agent_task_cancelled", ObserverAgent.IMPORTANT_KINDS)


class ComposeRestartPolicyTests(unittest.TestCase):
    def test_paper_template_player_services_are_unless_stopped(self):
        document = yaml.safe_load((ROOT / "infra" / "compose.paper.yaml").read_text(encoding="utf-8"))
        services = document["services"]
        self.assertEqual(services["server"]["restart"], "no")
        for name, service in services.items():
            if name.startswith(("identity", "launcher", "bot")):
                self.assertEqual(service["restart"], "unless-stopped", name)

    def test_legacy_template_bot_services_are_unless_stopped(self):
        document = yaml.safe_load((ROOT / "infra" / "compose.yaml").read_text(encoding="utf-8"))
        services = document["services"]
        self.assertEqual(services["server"]["restart"], "no")
        for name in ("bot1", "bot2", "bot3", "bot4"):
            self.assertEqual(services[name]["restart"], "unless-stopped", name)

    def test_generated_exact_n_inherits_restart_policies(self):
        rendered = render_compose(ROOT / "infra" / "compose.paper.yaml", 4, "mc-stability-test")
        services = rendered["services"]
        self.assertEqual(services["server"]["restart"], "no")
        for name, service in services.items():
            if name.startswith(("identity", "launcher", "bot")):
                self.assertEqual(service["restart"], "unless-stopped", name)
        self.assertEqual(services["server"]["environment"]["MAX_PLAYERS"], "4")
