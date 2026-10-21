"""Real Node process integration; fake game is explicit, never capability evidence."""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import httpx

from minecraft_harness.config import load_config, validate_config
from minecraft_harness.execution import ExecutionGateway
from minecraft_harness.recorder import Recorder
from minecraft_harness.ownership import RuntimeOwnership
from minecraft_harness.supervisor import Supervisor
from minecraft_harness.budget import Budget
from minecraft_harness.evaluation import ServerEvidenceCollector


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class GatewayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.recorder = Recorder(self.path)
        config = validate_config({})["runtime"]
        self.gateway = ExecutionGateway(config, self.recorder, ["alice", "bob"], self.path)
        await self.gateway.start()

    async def asyncTearDown(self):
        await self.gateway.close()
        self.recorder.close()
        self.temp.cleanup()

    async def test_durable_idempotency_timeout_and_chat_delivery(self):
        first = await self.gateway.execute("alice", 'globals.n = (globals.n || 0) + 1; bot.chat("hello"); return globals.n;', request_id="same")
        again = await self.gateway.execute("alice", 'globals.n = (globals.n || 0) + 1; bot.chat("hello"); return globals.n;', request_id="same")
        self.assertEqual(first["execution_id"], again["execution_id"])
        self.assertEqual(first["result"], 1)
        with self.assertRaises(ValueError):
            await self.gateway.execute("alice", "return 99", request_id="same")
        result = await self.gateway.read_events("bob")
        self.assertTrue(any(event["kind"] == "chat" for event in result["events"]))
        timeout = await self.gateway.execute("alice", "while (true) {}", timeout_ms=40)
        self.assertEqual(timeout["status"], "timed_out")
        recovered = await self.gateway.execute("alice", "return typeof globals.n;")
        self.assertEqual(recovered["result"], "undefined")
        self.assertGreater(recovered["runtime_epoch"], first["runtime_epoch"])

    async def test_player_cursor_survives_world_session_reset(self):
        await self.gateway.execute("alice", 'bot.chat("before");')
        before = await self.gateway.read_events("bob")
        cursor = before["next_cursor"]
        await self.gateway.reset_world()
        await self.gateway.execute("alice", 'bot.chat("after");')
        after = await self.gateway.read_events("bob", cursor)
        self.assertGreater(after["next_cursor"], cursor)
        self.assertTrue(any(event["kind"] == "world_restored" for event in after["events"]))
        self.assertTrue(any(event["kind"] == "chat" and "after" in json.dumps(event) for event in after["events"]))

    async def test_queued_old_epoch_program_is_invalidated(self):
        self.gateway.admission.clear()
        old = asyncio.create_task(self.gateway.execute("alice", "return 'stale'"))
        await asyncio.sleep(0)
        await self.gateway.reset_world()
        self.gateway.admission.set()
        result = await old
        self.assertEqual(result["status"], "invalidated")
        self.assertFalse(any(event["kind"] == "execution_submitted" for event in self.recorder.all_events()))

    async def test_operator_checkpoint_restore_fences_and_recreates_sessions(self):
        data = self.path / "server-data"
        world = data / "world"
        world.mkdir(parents=True)
        (world / "level.dat").write_bytes(b"initial-world-fixture")
        config = validate_config({"agents": {"count": 2, "ids": ["alice", "bob"]}, "server": {"world_dir": str(world), "managed_container": "fixture-server"}})
        operator = Supervisor(config, self.path / "run")
        operator.recorder = self.recorder
        operator.gateway = self.gateway
        operator.budget = Budget(config["budgets"], self.recorder)
        class StoppedServerFixture:
            container = "fixture-server"
            stopped = False
            async def save(self):
                pass
            async def stop(self):
                self.stopped = True
            async def start(self):
                self.stopped = False
        server = StoppedServerFixture()
        collector = ServerEvidenceCollector(self.path, self.recorder)
        await operator._control("checkpoint", server, collector)
        checkpoint = self.recorder.get_state("latest_world_checkpoint")
        (world / "level.dat").write_bytes(b"later-world-fixture")
        await operator._control("restore", server, collector, checkpoint_id=checkpoint["checkpoint_id"])
        self.assertEqual((world / "level.dat").read_bytes(), b"initial-world-fixture")
        self.assertEqual(self.gateway.world_epoch, 2)
        self.assertFalse(server.stopped)
        self.assertTrue(operator.budget.admission.is_set())
        events = await self.gateway.read_events("alice")
        self.assertEqual(len([event for event in events["events"] if event["kind"] == "world_restored"]), 2)

    async def test_fence_preserves_external_cli_source_and_terminal_events(self):
        source = "await new Promise(r => setTimeout(r, 500)); return 'external';"
        submitted = await self.gateway._request("POST", "/v1/sessions/alice/programs", {"source": source, "timeout_ms": 2000, "request_id": "direct-cli", "run_id": self.recorder.run_id, "world_epoch": 0}, "alice")
        await self.gateway.fence()
        events = list(self.recorder.all_events())
        source_events = [event for event in events if event["kind"] == "execution_accepted" and event["execution_id"] == submitted["execution_id"]]
        self.assertEqual(len(source_events), 1)
        artifact = source_events[0]["payload"]["payload"]["source_ref"]
        self.assertEqual((self.path / artifact["path"]).read_text(), source)
        self.assertTrue(any(event["kind"] in {"execution_cancelled", "execution_interrupted", "execution_succeeded"} and event["execution_id"] == submitted["execution_id"] for event in events))

    async def test_unconfirmed_shutdown_is_an_infrastructure_error(self):
        config = validate_config({})["runtime"]
        config["url"] = "http://127.0.0.1:9"
        other = ExecutionGateway(config, self.recorder, ["alice"], self.path)
        async def disconnected(*args, **kwargs):
            raise httpx.ConnectError("test connection lost")
        other._request = disconnected
        with self.assertRaises(RuntimeError):
            await other.close()
        self.assertTrue(any(event["kind"] == "runtime_shutdown_unconfirmed" for event in self.recorder.all_events()))


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class SupervisorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_runtime_cannot_be_claimed_by_two_runs(self):
        import uuid
        runtime = {"url": "http://127.0.0.1/ownership-test-" + uuid.uuid4().hex}
        first, second = RuntimeOwnership(runtime), RuntimeOwnership(runtime)
        first.acquire()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire()
        finally:
            first.close()
        second.acquire()
        second.close()

    async def test_four_agents_generate_auditable_diagnostic_without_capability_claim(self):
        config = load_config(Path(__file__).resolve().parents[1] / "configs" / "offline-smoke.yaml")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = await Supervisor(config, path).run()
            self.assertEqual(result["status"], "diagnostic_pass")
            self.assertIsNone(result["evaluation"]["capability_success"])
            recorder = Recorder(path)
            events = list(recorder.all_events())
            recorder.close()
            completed = [event for event in events if event["kind"] == "execution_finished"]
            self.assertEqual({event["actor_id"] for event in completed}, {"player1", "player2", "player3", "player4"})
            self.assertEqual(len(completed), 4)
            self.assertTrue(Path(result["reports"]["markdown"]).is_file())
            manifest = json.loads((path / "manifest.json").read_text())
            self.assertTrue(manifest["source_hashes"])
            self.assertTrue(manifest["diagnostic_only"])

    async def test_total_call_budget_is_classified(self):
        config = validate_config({"agents": {"count": 1, "max_turns": 10}, "budgets": {"total_model_calls": 1}})
        with tempfile.TemporaryDirectory() as directory:
            result = await Supervisor(config, directory).run()
            self.assertEqual(result["status"], "budget_exhausted")
