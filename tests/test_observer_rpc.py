"""``ServiceHub`` observer RPC contract (Pi observer runner): records/evidence/report.

Uses fake recorder/artifact-vault/gateway so the tests stay fast and never touch a
real ledger or network. Semantics intentionally mirror ``ObserverAgent`` plus the
security hardening from AUDIT_2026-10-23_OBSERVER_SECURITY.md:
* F2: a runner-authored event may not widen the Observer ACL to another agent's
  artifacts;
* F3: report evidence event sequences must have been read and are persisted;
* F4: injected unknown report keys are stripped before the durable report event.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = REPO / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from minecraft_harness.runner_rpc import RpcClient, RpcError, RpcServer
from minecraft_harness.runners import RunnerControlError, ServiceHub


class FakeRecorder:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.events = []
        self.artifacts = {}
        self.appended = []

    def add(self, event_seq, kind, payload, actor_id="player1", **extra):
        event = {"event_seq": event_seq, "kind": kind, "actor_id": actor_id,
                 "payload": payload, **extra}
        self.events.append(event)
        return event

    def read_events(self, after=0, limit=1000):
        ordered = sorted(self.events, key=lambda event: event["event_seq"])
        return [event for event in ordered if event["event_seq"] > after][:limit]

    def latest_event_seq(self):
        return max((event["event_seq"] for event in self.events), default=0)

    def append(self, kind, payload, **kwargs):
        sequence = self.latest_event_seq() + 1
        event = {"event_seq": sequence, "kind": kind, "payload": payload, **kwargs}
        self.events.append(event)
        self.appended.append(event)
        return event

    def put_artifact(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        self.artifacts[digest] = data
        return {"hash": digest, "sha256": digest, "path": "artifacts/" + digest}


class FakeArtifacts:
    def __init__(self, recorder):
        self.recorder = recorder
        self.grants = set()

    def grant(self, agent_id, digest):
        self.grants.add((agent_id, digest))

    def owned(self, agent_id, digest):
        return (agent_id, digest) in self.grants

    def read(self, agent_id, digest, offset=0, limit=12000):
        if not self.owned(agent_id, digest):
            raise PermissionError("Only artifacts already delivered to this agent are readable")
        data = self.recorder.artifacts.get(digest)
        if data is None:
            raise FileNotFoundError("missing artifact fixture")
        content = data[offset:offset + limit]
        return {"hash": digest, "content": content.decode("utf-8", errors="replace"),
                "offset": offset,
                "next_offset": offset + limit if len(data) > offset + limit else None,
                "offset_unit": "utf8_bytes"}


class FakeGateway:
    world_epoch = 7


class FakeBudget:
    def admit_web(self, agent_id, tool="web"):
        return True


class ObserverRpcTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.recorder = FakeRecorder(self.run_dir)
        self.gateway = FakeGateway()
        self.artifacts = FakeArtifacts(self.recorder)
        self.hub = self.make_hub()

    def make_hub(self, *, observer_callback=None):
        return ServiceHub(recorder=self.recorder, budget=FakeBudget(), memory=None,
                          gateway=self.gateway, workspaces={},
                          artifacts=self.artifacts, config={
                              "storage": {},
                              "agents": {"ids": ["player1", "player2"]}},
                          observer_callback=observer_callback)

    def artifact(self, text):
        return self.recorder.put_artifact(text.encode("utf-8"))["hash"]

    def grant(self, agent, text):
        digest = self.artifact(text)
        self.artifacts.grant(agent, digest)
        return digest

    async def read_records(self, hub=None, *, after=0, limit=100):
        return await (hub or self.hub).handle(
            "observer", "observer", "observer.records_read",
            {"after": after, "limit": limit})

    def report_params(self, **overrides):
        params = {"progress_summary": "working on the dragon",
                  "verified_events": [{"claim": "obsidian found",
                                       "evidence_refs": [1]}],
                  "infrastructure_findings": ["no issues"],
                  "findings": [{"status": "verified",
                                "claim": "player mined obsidian",
                                "evidence_refs": [1]}]}
        params.update(overrides)
        return params

    # -- registration / identity ---------------------------------------
    async def test_methods_registered_and_identity_gated(self):
        for method in ("observer.records_read", "observer.evidence_read", "observer.report"):
            self.assertIn(method, self.hub._handlers)
        with self.assertRaises(PermissionError):
            await self.hub.handle("player1", "player", "observer.records_read", {})
        with self.assertRaises(PermissionError):
            await self.hub.handle("observer", "player", "observer.records_read", {})
        with self.assertRaises(PermissionError):
            await self.hub.handle("player1", "observer", "observer.records_read", {})
        with self.assertRaises(PermissionError):
            await self.hub.handle("player1", "player", "observer.evidence_read",
                                  {"hash": "a" * 64})
        with self.assertRaises(PermissionError):
            await self.hub.handle("player1", "player", "observer.report", self.report_params())

    # -- records_read ---------------------------------------------------
    async def test_records_read_paginates_with_cursor(self):
        for sequence in range(1, 6):
            self.recorder.add(sequence, "bot_chat", {"text": f"message {sequence}"})
        first = await self.read_records(limit=2)
        self.assertEqual([event["event_seq"] for event in first["events"]], [1, 2])
        self.assertEqual(first["next_cursor"], 2)
        self.assertEqual(first["latest_event_seq"], 5)
        self.assertTrue(first["truncated"])
        second = await self.read_records(after=first["next_cursor"], limit=2)
        self.assertEqual([event["event_seq"] for event in second["events"]], [3, 4])
        self.assertTrue(second["truncated"])
        third = await self.read_records(after=second["next_cursor"], limit=2)
        self.assertEqual([event["event_seq"] for event in third["events"]], [5])
        self.assertFalse(third["truncated"])
        self.assertEqual(third["next_cursor"], 5)

    async def test_records_read_defaults_and_validation(self):
        self.recorder.add(1, "bot_chat", {"text": "x"})
        page = await self.read_records()
        self.assertEqual([event["event_seq"] for event in page["events"]], [1])
        for params in ({"after": -1}, {"after": "x"}, {"limit": 0}, {"limit": 101},
                       {"limit": True}):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    await self.hub.handle("observer", "observer",
                                          "observer.records_read", params)

    async def test_records_read_skips_observer_authored_events(self):
        self.recorder.add(1, "bot_chat", {"text": "player"}, actor_id="player1")
        self.recorder.add(2, "observer_report", {"findings": []}, actor_id="observer")
        self.recorder.add(3, "bot_chat", {"text": "again"}, actor_id="player2")
        page = await self.read_records(after=0, limit=10)
        self.assertEqual([event["event_seq"] for event in page["events"]], [1, 3])
        self.assertEqual(page["next_cursor"], 3)
        # The skipped observer event is not "viewed" for report evidence.
        with self.assertRaises(PermissionError):
            await self.hub.handle("observer", "observer", "observer.report",
                                  self.report_params(
                                      findings=[{"status": "verified", "claim": "x",
                                                 "evidence_refs": [2]}]))

    async def test_records_read_redacts_operator_only_records(self):
        digest = self.artifact("operator secret reference")
        self.artifacts.grant("supervisor", digest)
        self.recorder.add(1, "effective_environment_verified",
                          {"seed": 42, "operator_only": True,
                           "result_ref": {"hash": digest, "sha256": digest}},
                          actor_id="supervisor")
        page = await self.read_records(after=0, limit=10)
        event = page["events"][0]
        self.assertEqual(event["payload"], {"operator_only": True, "redacted": True})
        self.assertNotIn(("observer", digest), self.artifacts.grants)
        self.assertNotIn("seed", json.dumps(event))
        # Redacted records are still viewed records (their cursor can be cited).
        report = await self.hub.handle("observer", "observer", "observer.report",
                                       self.report_params(
                                           verified_events=[],
                                           findings=[{"status": "verified",
                                                      "claim": "operator record seen",
                                                      "evidence_refs": [1]}]))
        self.assertEqual(report["status"], "recorded")

    async def test_records_read_bounds_large_payload_and_grants_original_ref(self):
        embedded = self.grant("player1", "embedded payload reference")
        self.recorder.add(1, "bot_event", {
            "blob": "x" * 9000,
            "result_ref": {"hash": embedded, "sha256": embedded}})
        page = await self.read_records(after=0, limit=10)
        payload = page["events"][0]["payload"]
        self.assertTrue(payload["bounded"])
        reference = payload["original_ref"]
        self.assertNotIn("x" * 9000, json.dumps(page))
        self.assertIn(("observer", reference["hash"]), self.artifacts.grants)
        self.assertIn(("observer", embedded), self.artifacts.grants)
        stored = await self.hub.handle("observer", "observer", "observer.evidence_read",
                                       {"hash": reference["hash"]})
        self.assertIn("x" * 100, stored["content"])
        self.assertIn(embedded, stored["content"])

    async def test_records_read_grants_actor_owned_embedded_artifacts(self):
        digest = self.grant("player1", "evidence body")
        self.recorder.add(1, "execution_finished",
                          {"result_ref": {"hash": digest, "sha256": digest,
                                          "path": "artifacts/" + digest}})
        await self.read_records(after=0, limit=10)
        self.assertIn(("observer", digest), self.artifacts.grants)
        evidence = await self.hub.handle("observer", "observer", "observer.evidence_read",
                                         {"hash": digest})
        self.assertEqual(evidence["content"], "evidence body")
        self.assertEqual(evidence["hash"], digest)

    # -- F2: artifact grant ownership -----------------------------------
    async def test_records_read_does_not_grant_another_agents_artifact(self):
        foreign = self.grant("player2", "player2 secret artifact")
        self.recorder.add(1, "bot_event",
                          {"result_ref": {"hash": foreign, "sha256": foreign}},
                          actor_id="player1")
        await self.read_records(after=0, limit=10)
        self.assertNotIn(("observer", foreign), self.artifacts.grants)
        with self.assertRaises(PermissionError):
            await self.hub.handle("observer", "observer", "observer.evidence_read",
                                  {"hash": foreign})
        with self.assertRaises(PermissionError):
            await self.hub.handle("observer", "observer", "observer.report",
                                  self.report_params(
                                      verified_events=[],
                                      findings=[{"status": "verified", "claim": "x",
                                                 "evidence_refs": [{"hash": foreign}]}]))

    async def test_records_read_grants_control_plane_artifacts(self):
        trusted = self.artifact("gateway produced artifact")  # owned by no agent
        self.recorder.add(1, "execution_accepted",
                          {"source_ref": {"hash": trusted, "sha256": trusted}},
                          actor_id="supervisor")
        await self.read_records(after=0, limit=10)
        self.assertIn(("observer", trusted), self.artifacts.grants)
        evidence = await self.hub.handle("observer", "observer", "observer.evidence_read",
                                         {"hash": trusted})
        self.assertEqual(evidence["content"], "gateway produced artifact")

    # -- evidence_read --------------------------------------------------
    async def test_evidence_read_requires_grant_and_validates_bounds(self):
        digest = self.artifact("0123456789abcdef")
        with self.assertRaises(PermissionError):
            await self.hub.handle("observer", "observer", "observer.evidence_read",
                                  {"hash": digest})
        self.artifacts.grant("observer", digest)
        page = await self.hub.handle("observer", "observer", "observer.evidence_read",
                                     {"hash": digest, "offset": 0, "limit": 5})
        self.assertEqual(page["content"], "01234")
        self.assertEqual(page["next_offset"], 5)
        self.assertEqual(page["offset_unit"], "utf8_bytes")
        for params in ({"hash": "nothex"}, {"hash": "A" * 64}, {"hash": digest, "offset": -1},
                       {"hash": digest, "limit": 0}, {"hash": digest, "limit": 32001}):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    await self.hub.handle("observer", "observer",
                                          "observer.evidence_read", params)

    # -- F3 / report ----------------------------------------------------
    async def test_report_requires_viewed_event_references(self):
        for sequence in range(1, 4):
            self.recorder.add(sequence, "bot_chat", {"text": f"message {sequence}"})
        with self.assertRaises(PermissionError):
            await self.hub.handle("observer", "observer", "observer.report",
                                  self.report_params(
                                      verified_events=[],
                                      findings=[{"status": "verified", "claim": "x",
                                                 "evidence_refs": [1]}]))
        page = await self.read_records(limit=2)
        self.assertEqual([event["event_seq"] for event in page["events"]], [1, 2])
        accepted = await self.hub.handle("observer", "observer", "observer.report",
                                         self.report_params(verified_events=[]))
        self.assertEqual(accepted["status"], "recorded")
        self.assertEqual(self.recorder.appended[-1]["payload"]["through_cursor"], 2)
        with self.assertRaises(PermissionError):
            await self.hub.handle("observer", "observer", "observer.report",
                                  self.report_params(
                                      verified_events=[],
                                      findings=[{"status": "verified", "claim": "x",
                                                 "evidence_refs": [3]}]))
        # An explicit through_cursor may not exceed the last viewed page.
        for cursor in (-1, 3):
            with self.subTest(through_cursor=cursor):
                with self.assertRaises(ValueError):
                    await self.hub.handle("observer", "observer", "observer.report",
                                          self.report_params(through_cursor=cursor))
        recorded = await self.hub.handle("observer", "observer", "observer.report",
                                         self.report_params(through_cursor=2))
        self.assertEqual(recorded["status"], "recorded")
        self.assertEqual(self.recorder.appended[-1]["payload"]["through_cursor"], 2)

    async def test_viewed_event_index_survives_servicehub_restart(self):
        self.recorder.add(1, "bot_chat", {"text": "one"})
        await self.read_records()
        self.assertTrue((self.run_dir / "observer-seen.sqlite3").is_file())
        restarted = self.make_hub()  # new ServiceHub instance on the same run dir
        report = await restarted.handle("observer", "observer", "observer.report",
                                        self.report_params(verified_events=[]))
        self.assertEqual(report["status"], "recorded")
        self.assertEqual(restarted._observer_seen_many([1]), {1})
        self.assertEqual(restarted._observer_seen_max(), 1)

    async def test_report_records_advisory_event_with_clean_fields(self):
        digest = self.grant("observer", "verified evidence")
        self.recorder.add(1, "bot_chat", {"text": "evidence record"})
        await self.read_records()
        params = self.report_params(
            verified_events=[{"claim": "obsidian found",
                              "evidence_refs": [{"hash": digest}]}],
            findings=[{"status": "verified", "claim": "player mined obsidian",
                       "evidence_refs": [{"sha256": digest}, 1]}])
        result = await self.hub.handle("observer", "observer", "observer.report", params)
        self.assertEqual(result["status"], "recorded")
        self.assertTrue(result["advisory"])
        self.assertTrue(result["progress_summary_present"])
        self.assertIsInstance(result["event_seq"], int)
        event = self.recorder.appended[-1]
        self.assertEqual(event["kind"], "observer_report")
        self.assertEqual(event["actor_id"], "observer")
        self.assertEqual(event["world_epoch"], 7)
        self.assertEqual(event["event_seq"], result["event_seq"])
        payload = event["payload"]
        self.assertTrue(payload["advisory"])
        self.assertEqual(payload["through_cursor"], 1)
        self.assertEqual(payload["progress_summary"], "working on the dragon")
        self.assertEqual(payload["findings"], [
            {"status": "verified", "claim": "player mined obsidian",
             "evidence_refs": [{"hash": digest}, 1]}])
        self.assertEqual(payload["verified_events"], [
            {"claim": "obsidian found", "evidence_refs": [{"hash": digest}]}])
        self.assertEqual(payload["infrastructure_findings"], ["no issues"])
        self.assertNotIn("status", payload)

    async def test_report_strips_unknown_finding_and_reference_keys(self):
        self.recorder.add(1, "bot_chat", {"text": "evidence"})
        await self.read_records()
        params = self.report_params(
            findings=[{"status": "verified", "claim": "x",
                       "evidence_refs": [{"event_seq": 1, "note": "smuggle"}],
                       "success": True, "reward": 1.5}],
            verified_events=[{"claim": "y", "evidence_refs": [1], "reward": 2.0}])
        result = await self.hub.handle("observer", "observer", "observer.report", params)
        self.assertEqual(result["status"], "recorded")
        payload = self.recorder.appended[-1]["payload"]
        self.assertEqual(payload["findings"], [
            {"status": "verified", "claim": "x", "evidence_refs": [1]}])
        self.assertEqual(payload["verified_events"], [
            {"claim": "y", "evidence_refs": [1]}])
        serialized = json.dumps(payload)
        for injected in ("success", "reward", "smuggle"):
            self.assertNotIn(injected, serialized)

    async def test_report_rejects_invalid_schema(self):
        self.recorder.add(1, "bot_chat", {"text": "one"})
        self.recorder.add(2, "bot_chat", {"text": "two"})
        await self.read_records()
        digest = self.grant("observer", "owned evidence")
        invalid_value = [
            {"findings": "not-a-list"},
            {"findings": [{"status": "made_up", "claim": "x", "evidence_refs": [1]}]},
            {"findings": [{"status": "verified", "claim": "x" * 4001, "evidence_refs": [1]}]},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": []}]},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": [1] * 21}]},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": [1]}],
             "progress_summary": "x" * 4001},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": [1]}],
             "verified_events": [{"claim": "x" * 4001, "evidence_refs": [1]}]},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": [1]}],
             "infrastructure_findings": ["x"] * 21},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": [1]}],
             "infrastructure_findings": ["x" * 2001]},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": [1]}],
             "through_cursor": -1},
            {"findings": [{"status": "verified", "claim": "x", "evidence_refs": [1]}],
             "through_cursor": 999},
            {"findings": [{"status": "verified", "claim": "x",
                           "evidence_refs": [{"event_seq": "one"}]}]},
            {"findings": [{"status": "verified", "claim": "x",
                           "evidence_refs": [{"hash": "not-a-hash"}]}]},
        ]
        for params in invalid_value:
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    await self.hub.handle("observer", "observer",
                                          "observer.report", params)
        for params in (
                {"findings": [{"status": "verified", "claim": "x",
                               "evidence_refs": [{"hash": "b" * 64}]}]},
                {"findings": [{"status": "verified", "claim": "x",
                               "evidence_refs": [3]}]},
        ):
            with self.subTest(params=params):
                with self.assertRaises(PermissionError):
                    await self.hub.handle("observer", "observer",
                                          "observer.report", params)
        # A valid report with an owned digest ref still works after all rejections.
        recorded = await self.hub.handle("observer", "observer", "observer.report",
                                         self.report_params(
                                             verified_events=[],
                                             findings=[{"status": "verified",
                                                        "claim": "x",
                                                        "evidence_refs": [
                                                            {"hash": digest}]}]))
        self.assertEqual(recorded["status"], "recorded")

    # -- infra issue passthrough ----------------------------------------
    async def test_infra_issue_callback_remains_observer_only(self):
        delivered = []

        async def callback(issue):
            delivered.append(issue)
            return {"status": "accepted", "issue": issue}

        hub = self.make_hub(observer_callback=callback)
        issue = {"kind": "reconnect", "reason": "bot reconnected twice",
                 "evidence_refs": [1]}
        result = await hub.handle("observer", "observer",
                                  "supervisor.observer_infra_issue", {"issue": issue})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(delivered, [issue])
        with self.assertRaises(PermissionError):
            await hub.handle("player1", "player", "supervisor.observer_infra_issue",
                             {"issue": issue})
        with self.assertRaises(RunnerControlError):
            await self.make_hub().handle("observer", "observer",
                                         "supervisor.observer_infra_issue", {"issue": issue})

    # -- RPC identity binding -------------------------------------------
    async def test_observer_identity_is_bound_by_the_rpc_server(self):
        digest = self.artifact("evidence body")
        self.artifacts.grant("observer", digest)
        self.recorder.add(1, "bot_chat", {"text": "hello"})
        tokens = {"token-observer": {"agent_id": "observer", "role": "observer"},
                  "token-player": {"agent_id": "player1", "role": "player"}}
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "control.sock"
            server = RpcServer(socket_path, tokens, self.hub.handle)
            await server.start()
            self.addAsyncCleanup(server.close)
            # The client's claimed hello identity is ignored; the token binds observer.
            liar = RpcClient(socket_path, "token-observer", "player1", "player")
            page = await liar.call("observer.records_read", {"after": 0, "limit": 10})
            self.assertEqual([event["event_seq"] for event in page["events"]], [1])
            report = await liar.call("observer.report", self.report_params(
                findings=[{"status": "verified", "claim": "x",
                           "evidence_refs": [{"hash": digest}, 1]}]))
            self.assertEqual(report["status"], "recorded")
            self.assertEqual(self.recorder.appended[-1]["actor_id"], "observer")
            player = RpcClient(socket_path, "token-player", "player1", "player")
            with self.assertRaises(RpcError) as raised:
                await player.call("observer.records_read", {"after": 0, "limit": 10})
            self.assertEqual(raised.exception.error_type, "PermissionError")


if __name__ == "__main__":
    unittest.main()
