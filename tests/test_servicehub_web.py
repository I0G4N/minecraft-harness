"""ServiceHub ``web.search``/``web.fetch`` RPC contract for Pi players.

Uses fake WebTools/Budget for the happy paths and the real ``agents/web`` WebTools
with a fake budget for admission-denial paths, so no external network is used.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = REPO / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from minecraft_harness.agents.web import WebTools
from minecraft_harness.recorder import Recorder
from minecraft_harness.runner_rpc import RpcClient, RpcServer
from minecraft_harness.runners import ServiceHub

SEARCH_RESULT = {"status": "ok", "backend": "fixture-search", "query": "minecraft dragon",
                 "source": "search.example.test",
                 "results": [{"title": "Dragon", "url": "https://example.test/dragon",
                              "snippet": "The Ender Dragon lives in the End."}]}
FETCH_RESULT = {"status": "ok", "content": "Reference body", "content_type": "text/plain",
                "truncated": False, "source": "https://example.test/dragon"}


class FakeBudget:
    def __init__(self, admitted=True):
        self.admitted = admitted
        self.calls = []

    def admit_web(self, agent_id, tool="web"):
        self.calls.append((agent_id, tool))
        return self.admitted


class FakeArtifacts:
    def __init__(self):
        self.grants = []

    def grant(self, agent_id, digest):
        self.grants.append((agent_id, digest))


class FakeWebTools:
    def __init__(self, search_result=None, fetch_result=None):
        self.search_result = dict(search_result if search_result is not None else SEARCH_RESULT)
        self.fetch_result = dict(fetch_result if fetch_result is not None else FETCH_RESULT)
        self.calls = []

    async def search(self, query):
        self.calls.append(("search", query))
        return dict(self.search_result)

    async def fetch(self, url):
        self.calls.append(("fetch", url))
        return dict(self.fetch_result)


def _web_config(**overrides):
    config = {"enabled": True, "provider": "deepseek",
              "search_url": "https://api.deepseek.com/v1/tools/search",
              "fetch_url": "https://api.deepseek.com/v1/tools/fetch",
              "api_key_env": "FAKE_WEB_KEY", "fetch_key_env": "FAKE_WEB_KEY",
              "count": 3, "need_content": False, "allowed_hosts": ["example.test"],
              "timeout_s": 5, "max_response_bytes": 32768}
    config.update(overrides)
    return config


class ServiceHubWebTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = Path(self.temporary.name)
        self.recorder = Recorder(self.run_dir)
        self.addCleanup(self.recorder.close)
        self.budget = FakeBudget()
        self.artifacts = FakeArtifacts()
        self.hub = self.make_hub()

    def make_hub(self, *, web_config=None, budget=None, storage=None, artifacts=None):
        return ServiceHub(
            recorder=self.recorder, budget=budget or self.budget, memory=None,
            gateway=None, workspaces={},
            artifacts=artifacts if artifacts is not None else self.artifacts,
            config={"web": web_config if web_config is not None else _web_config(),
                    "storage": storage if storage is not None else {}})

    def reference_events(self):
        return [event for event in self.recorder.read_events(0, 1000)
                if event["kind"] == "reference_access"]

    def artifact_bytes(self, reference):
        return (self.run_dir / reference["path"]).read_bytes()

    async def test_methods_are_registered_and_dispatch(self):
        self.assertIn("web.search", self.hub._handlers)
        self.assertIn("web.fetch", self.hub._handlers)
        fake = FakeWebTools()
        self.hub._web_tools["player1"] = fake
        result = await self.hub.handle("player1", "player", "web.search",
                                       {"query": "minecraft dragon"})
        self.assertEqual(result, SEARCH_RESULT)
        self.assertEqual(fake.calls, [("search", "minecraft dragon")])

    async def test_search_success_records_bounded_reference_access(self):
        fake = FakeWebTools()
        self.hub._web_tools["player1"] = fake
        result = await self.hub.handle("player1", "player", "web.search",
                                       {"query": "minecraft dragon"})
        self.assertEqual(result, SEARCH_RESULT)
        events = self.reference_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["actor_id"], "player1")
        self.assertEqual(event["payload"]["tool"], "web_search")
        self.assertEqual(event["payload"]["query"], {"query": "minecraft dragon"})
        reference = event["payload"]["result_ref"]
        self.assertEqual(len(reference["hash"]), 64)
        self.assertEqual(reference["sha256"], reference["hash"])
        self.assertEqual(json.loads(self.artifact_bytes(reference).decode("utf-8")), SEARCH_RESULT)
        self.assertEqual(self.hub.artifacts.grants, [("player1", reference["hash"])])

    async def test_fetch_success_records_reference_access(self):
        fake = FakeWebTools()
        self.hub._web_tools["player1"] = fake
        result = await self.hub.handle("player1", "player", "web.fetch",
                                       {"url": "https://example.test/dragon"})
        self.assertEqual(result, FETCH_RESULT)
        self.assertEqual(fake.calls, [("fetch", "https://example.test/dragon")])
        events = self.reference_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["tool"], "web_fetch")
        self.assertEqual(events[0]["payload"]["query"], {"url": "https://example.test/dragon"})
        self.assertEqual(events[0]["actor_id"], "player1")

    async def test_unavailable_results_are_returned_and_recorded(self):
        fake = FakeWebTools(search_result={"status": "unavailable",
                                           "capability": "web_search",
                                           "reason": "backend down"})
        self.hub._web_tools["player1"] = fake
        result = await self.hub.handle("player1", "player", "web.search", {"query": "x"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "backend down")
        self.assertEqual(len(self.reference_events()), 1)

    async def test_budget_denial_is_structured_not_an_exception(self):
        budget = FakeBudget(admitted=False)
        hub = self.make_hub(budget=budget)
        with mock.patch.dict(os.environ, {"FAKE_WEB_KEY": "super-secret-web-key"}):
            result = await hub.handle("player1", "player", "web.search", {"query": "dragon"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "web call budget exhausted")
        self.assertEqual(budget.calls, [("player1", "web_search")])
        events = self.reference_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor_id"], "player1")
        stored = self.artifact_bytes(events[0]["payload"]["result_ref"]).decode("utf-8")
        self.assertIn("budget exhausted", stored)
        serialized = json.dumps([events[0]["payload"], stored])
        self.assertNotIn("super-secret-web-key", serialized)

    async def test_fetch_budget_denial_is_structured(self):
        budget = FakeBudget(admitted=False)
        hub = self.make_hub(budget=budget)
        with mock.patch.dict(os.environ, {"FAKE_WEB_KEY": "super-secret-web-key"}):
            result = await hub.handle("player1", "player", "web.fetch",
                                      {"url": "https://example.test/dragon"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "web call budget exhausted")
        self.assertEqual(budget.calls, [("player1", "web_fetch")])

    async def test_unconfigured_web_service_is_structured(self):
        hub = self.make_hub(web_config={"enabled": False})
        result = await hub.handle("player1", "player", "web.search", {"query": "dragon"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["capability"], "web_search")
        self.assertEqual(self.budget.calls, [])
        self.assertEqual(len(self.reference_events()), 1)

    async def test_invalid_arguments_raise_without_calling_or_recording(self):
        fake = FakeWebTools()
        self.hub._web_tools["player1"] = fake
        cases = [
            ("web.search", {}),
            ("web.search", {"query": ""}),
            ("web.search", {"query": "x" * 1001}),
            ("web.search", {"query": 7}),
            ("web.search", {"query": None}),
            ("web.fetch", {}),
            ("web.fetch", {"url": ""}),
            ("web.fetch", {"url": "https://example.test/" + "x" * 4096}),
            ("web.fetch", {"url": 7}),
        ]
        for method, params in cases:
            with self.subTest(method=method, params=params):
                with self.assertRaises(ValueError):
                    await self.hub.handle("player1", "player", method, params)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.reference_events(), [])

    async def test_cross_agent_claims_are_rejected(self):
        fake = FakeWebTools()
        self.hub._web_tools["player1"] = fake
        with self.assertRaises(PermissionError):
            await self.hub.handle("player1", "player", "web.search",
                                  {"query": "x", "agent_id": "player2"})
        with self.assertRaises(PermissionError):
            await self.hub.handle("player1", "player", "web.fetch",
                                  {"url": "https://example.test/x", "agent_id": "player2"})
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.reference_events(), [])

    async def test_web_tools_are_cached_and_bound_to_the_agent(self):
        tools = self.hub._web_tools_for("player1")
        self.assertIsInstance(tools, WebTools)
        self.assertEqual(tools.agent_id, "player1")
        self.assertIs(self.hub._web_tools_for("player1"), tools)
        other = self.hub._web_tools_for("player2")
        self.assertIsNot(other, tools)
        self.assertEqual(other.agent_id, "player2")

    async def test_oversized_reference_result_artifact_is_bounded(self):
        hub = self.make_hub(storage={"max_event_bytes": 1024})
        payload = {"status": "ok", "content": "x" * 5000}
        fake = FakeWebTools(search_result=payload)
        hub._web_tools["player1"] = fake
        result = await hub.handle("player1", "player", "web.search", {"query": "big"})
        self.assertEqual(result, payload)  # the runner still gets the full result
        events = [event for event in self.recorder.read_events(0, 1000)
                  if event["kind"] == "reference_access"]
        self.assertEqual(len(events), 1)
        stored = json.loads(self.artifact_bytes(events[0]["payload"]["result_ref"]))
        self.assertTrue(stored["truncated"])
        self.assertEqual(stored["status"], "ok")
        self.assertNotIn("x" * 5000, json.dumps(stored))

    async def test_identity_is_bound_by_the_rpc_server(self):
        # A forged hello identity must not change which agent spends web budget
        # or owns the reference_access event.
        budget = FakeBudget(admitted=False)
        hub = self.make_hub(budget=budget)
        tokens = {"token-player1": {"agent_id": "player1", "role": "player"}}
        socket_path = self.run_dir / "control.sock"
        server = RpcServer(socket_path, tokens, hub.handle)
        await server.start()
        self.addAsyncCleanup(server.close)
        liar = RpcClient(socket_path, "token-player1", "player2", "player")
        with mock.patch.dict(os.environ, {"FAKE_WEB_KEY": "super-secret-web-key"}):
            result = await liar.call("web.search", {"query": "dragon"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "web call budget exhausted")
        self.assertEqual(budget.calls, [("player1", "web_search")])
        events = [event for event in self.recorder.read_events(0, 1000)
                  if event["kind"] == "reference_access"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor_id"], "player1")
        with self.assertRaisesRegex(Exception, "another agent"):
            await liar.call("web.search", {"query": "dragon", "agent_id": "player2"})


if __name__ == "__main__":
    unittest.main()
