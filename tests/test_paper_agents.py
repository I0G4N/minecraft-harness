import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minecraft_harness.agents import AgentCore, AgentLoop, WorkspaceTools
from minecraft_harness.agents.web import WebTools
from minecraft_harness.models import FakeModel, ModelError, ModelResponse
from minecraft_harness.observer.agent import ObserverAgent
from minecraft_harness.recorder import Recorder


def call(identifier, name, **args):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Budget:
    def __init__(self):
        self.admission = asyncio.Event()
        self.admission.set()
        self.admissions = []
        self.settlements = []

    def stop_requested(self):
        return False

    async def reserve(self, agent_id, *, purpose="agent_decision", role="player"):
        self.admissions.append((agent_id, purpose, role))
        return len(self.admissions)

    async def settle(self, reservation, usage):
        self.settlements.append((reservation, usage))


class Executor:
    def __init__(self):
        self.submitted = []
        self.waited = []
        self.job_records = []

    async def submit(self, agent, source, **options):
        self.submitted.append((agent, source, options))
        return {"execution_id": "handle", "status": "running"}

    async def execute(self, agent, source, **options):
        return await self.submit(agent, source, **options)

    async def wait(self, agent, execution_id, wait_ms=1000):
        self.waited.append((execution_id, wait_ms))
        return {"execution_id": execution_id, "status": "running", "wait_expired": True}

    async def status(self, agent, execution_id):
        return {"execution_id": execution_id, "status": "running"}

    async def cancel(self, agent, execution_id):
        return {"execution_id": execution_id, "status": "cancelled"}

    async def jobs(self, agent):
        return {"jobs": self.job_records}

    async def cancel_job(self, agent, job_id):
        return {"job_id": job_id, "status": "cancelled"}

    async def read_events(self, agent, after_cursor=0, limit=100):
        return {"events": [], "next_cursor": after_cursor}


class PaperAgentsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.recorder = Recorder(self.root)
        self.addCleanup(self.recorder.close)
        self.budget = Budget()
        self.executor = Executor()

    def player(self, model=None, agent_id="a", **options):
        return AgentLoop({"agent_id": agent_id, "lifecycle": "paper_aligned", "context_chars": 12000,
                          "goal": "Observe", "max_turns": 1, **options}, self.root, self.recorder,
                         self.executor, model or FakeModel(), self.budget, None)

    async def test_shared_core_and_purpose_accounting(self):
        player = self.player()
        observer = ObserverAgent({}, self.root, self.recorder, FakeModel(), self.budget)
        self.assertIsInstance(player, AgentCore)
        self.assertIsInstance(observer, AgentCore)
        await player._model_call(player.messages, player.schemas)
        await observer._model_call(observer.messages, observer.schemas, purpose="observer_review")
        self.assertEqual([item[1:] for item in self.budget.admissions],
                         [("agent_decision", "player"), ("observer_review", "observer")])
        self.assertEqual(len(self.budget.settlements), 2)

    async def test_paper_single_program_hides_async_execution_tools(self):
        player = self.player(paper_single_program=True, leftover_policy='allow')
        names = {item['function']['name'] for item in player.schemas}
        self.assertIn('mc_execute', names)
        for hidden in ('mc_submit', 'mc_wait', 'mc_jobs', 'mc_job_cancel', 'mc_status', 'mc_cancel', 'mc_pending'):
            self.assertNotIn(hidden, names)
        self.assertIn('leftover', player.messages[0]['content'])
        legacy = self.player(paper_single_program=True)
        self.assertNotIn('leftover', legacy.messages[0]['content'])

    async def test_observer_report_carries_progress_and_verified_events(self):
        observer = ObserverAgent({}, self.root, self.recorder, FakeModel(), self.budget)
        self.recorder.append("execution_finished", {"status": "succeeded"}, execution_id="evidence-1")
        observer._grant_record({"event_seq": 1, "payload": {}})
        result = await observer._dispatch("observer_report", {
            "progress_summary": "Team gathered wood and made stone tools.",
            "verified_events": [{"claim": "stone tools advanced", "evidence_refs": [1]}],
            "infrastructure_findings": ["no broken tool observed"],
            "findings": [{"status": "verified", "claim": "stone tools advanced", "evidence_refs": [1]}],
        }, "report-1")
        self.assertEqual(result["status"], "recorded")
        self.assertTrue(result["progress_summary_present"])
        event = [item for item in self.recorder.read_events() if item["kind"] == "observer_report"][-1]
        self.assertEqual(event["payload"]["progress_summary"], "Team gathered wood and made stone tools.")
        self.assertEqual(event["payload"]["verified_events"][0]["claim"], "stone tools advanced")
        self.assertEqual(event["payload"]["infrastructure_findings"], ["no broken tool observed"])

    async def test_paper_tight_interface_hides_archives_and_snapshots(self):
        player = self.player(paper_single_program=True, paper_tight_interface=True)
        names = {item['function']['name'] for item in player.schemas}
        for hidden in ('history_read', 'artifact_read', 'events_read', 'mc_submit', 'mc_wait'):
            self.assertNotIn(hidden, names)
        class TerminalExecutor(Executor):
            async def execute(self, agent, source, **options):
                return {'execution_id': 'e1', 'status': 'succeeded', 'result': 'ok',
                        'before': {'health': 20}, 'after': {'health': 19}}
        tight = AgentLoop({"agent_id": "a", "lifecycle": "paper_aligned", "paper_single_program": True,
                           "paper_tight_interface": True, "context_chars": 12000, "goal": "Observe",
                           "max_turns": 1}, self.root, self.recorder, TerminalExecutor(),
                          FakeModel(), self.budget, None)
        result = await tight._dispatch("mc_execute", {"source": "return 1"}, "call")
        self.assertNotIn("before", result)
        self.assertNotIn("after", result)
        self.assertEqual(result["result"], "ok")

    async def test_observer_retries_transient_model_failure(self):
        observer = ObserverAgent({"retry_base_s": 0, "max_model_failures": 3},
                                 self.root, self.recorder, FakeModel(), self.budget)
        calls = 0

        async def flaky(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelError("temporary provider failure", retryable=True)
            return ModelResponse(text="observed")

        with patch.object(observer, "_model_call", side_effect=flaky):
            result = await observer._review_model_call(
                observer.messages, observer.schemas, purpose="observer_review")
        self.assertEqual(result.text, "observed")
        self.assertEqual(calls, 2)
        kinds = [event["kind"] for event in self.recorder.read_events()]
        self.assertIn("observer_model_error", kinds)

    async def test_submit_and_wait_preserve_handle_without_replay(self):
        player = self.player()
        result = await player._dispatch("mc_submit", {"source": "return 1"}, "one")
        self.assertEqual(result["execution_id"], "handle")
        await player._dispatch("mc_wait", {"execution_id": "handle", "wait_ms": 15}, "two")
        self.assertIn("handle", player.pending_executions)
        self.assertEqual(len(self.executor.submitted), 1)
        self.assertEqual(self.executor.waited, [("handle", 15)])
        player._checkpoint()
        resumed = self.player()
        self.assertIn("handle", resumed.pending_executions)
        self.assertEqual(len(self.executor.submitted), 1)

    async def test_registered_jobs_survive_handoff_and_avoid_false_stall(self):
        self.executor.job_records = [{"job_id": "watch", "status": "running"}]
        player = self.player()
        await player._dispatch("mc_jobs", {}, "jobs")
        player.no_progress_turns = 5
        self.assertTrue(await player._has_live_activity())
        self.assertEqual(player.no_progress_turns, 0)
        player._checkpoint()
        resumed = self.player()
        self.assertIn("watch", resumed.pending_jobs)
        await resumed._dispatch("mc_job_cancel", {"job_id": "watch"}, "cancel")
        self.assertNotIn("watch", resumed.pending_jobs)

    async def test_handoff_notes_then_structured_summary_uses_budget(self):
        handoff = {"objective": "Keep observing", "plans": ["Read state before acting"],
                   "verified_facts": ["epoch 0 event 1 at t=0: observed only; now stale"],
                   "failed_attempts": ["Do not retry unknown handle"], "source_paths": ["watch.js"],
                   "unresolved_tasks": ["handle remains unknown"]}
        model = FakeModel([{"tool_calls": [call("note", "workspace_write", path="handoff.md", content="pending handle")]},
                           {"tool_calls": [call("save", "context_handoff", **handoff)]}])
        player = self.player(model)
        player.pending_executions["handle"] = "previous"
        player.pending_jobs["watch"] = {"status": "running", "job_id": "watch"}
        player.messages.append({"role": "user", "content": "x" * 18000})
        await player._prepare_compaction()
        player._compress()
        self.assertTrue(player.handoff["complete"])
        self.assertEqual(player.workspace.read("handoff.md")["content"], "pending handle")
        self.assertIn("handoff.md", json.dumps(model.calls[1]["messages"]))
        self.assertIn("handle", player.handoff["pending_executions"])
        self.assertIn("watch", player.handoff["pending_jobs"])
        self.assertEqual([row[1] for row in self.budget.admissions], ["context_compaction"] * 2)
        self.assertLessEqual(len(json.dumps(player.messages, ensure_ascii=False)), 12000)
        self.assertEqual(self.player().handoff["source_paths"], ["watch.js"])

    async def test_failed_handoff_is_explicitly_incomplete_and_keeps_original(self):
        class FailedModel:
            async def complete(self, messages, schemas):
                raise ConnectionError("secret-url")
        player = self.player(FailedModel())
        player.pending_executions["unknown"] = "old"
        player.messages.append({"role": "user", "content": "ORIGINAL" * 3000})
        await player._prepare_compaction()
        player._compress()
        self.assertFalse(player.handoff["complete"])
        self.assertIn("unknown", player.pending_executions)
        self.assertIn("ORIGINAL", (player.state_dir / player.archives[-1]).read_text())
        self.assertNotIn("secret-url", json.dumps(self.recorder.read_events()))
        self.assertIsNone(self.budget.settlements[0][1]["total_tokens"])

    async def test_artifact_read_is_own_only_durable_and_paginated(self):
        a, b = self.player(), self.player(agent_id="b")
        ref = a._artifact({"payload": "abcdefghij" * 5000})
        page = await a._dispatch("artifact_read", {"hash": ref["hash"], "offset": 3, "limit": 17}, "read")
        raw = (self.recorder.artifact_dir / ref["hash"]).read_bytes()
        self.assertEqual(page["content"], raw[3:20].decode())
        self.assertEqual(page["next_offset"], 20)
        with self.assertRaises(PermissionError):
            await b._dispatch("artifact_read", {"hash": ref["hash"]}, "read")
        self.assertEqual(self.player()._artifact_read(ref["hash"], 3, 17)["content"], page["content"])
        (self.recorder.artifact_dir / ref["hash"]).write_text("tampered")
        with self.assertRaisesRegex(ValueError, "integrity"):
            a._artifact_read(ref["hash"])

    async def test_observer_has_no_game_or_player_memory_tools(self):
        observer = ObserverAgent({}, self.root, self.recorder, FakeModel(), self.budget)
        names = {tool["function"]["name"] for tool in observer.schemas}
        self.assertFalse(names & {"mc_execute", "mc_submit", "mc_cancel", "memory_write", "memory_read"})
        for name in ("mc_execute", "memory_write", "set_budget"):
            with self.assertRaises(PermissionError):
                await observer._dispatch(name, {}, "forbidden")

    async def test_observer_references_and_infra_requests_are_validated_deduplicated(self):
        event = self.recorder.append("runtime_fault", {"reason": "worker gone"})
        requests = []
        observer = ObserverAgent({}, self.root, self.recorder, FakeModel(), self.budget, requests.append)
        await observer._dispatch("records_read", {}, "read")
        issue = {"kind": "runtime_recovery", "reason": "Worker exited", "evidence_refs": [event["event_seq"]]}
        first = await observer._dispatch("infra_issue", issue, "one")
        duplicate = await observer._dispatch("infra_issue", issue, "two")
        self.assertEqual(first["status"], "submitted")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(len(requests), 1)
        resumed = ObserverAgent({}, self.root, self.recorder, FakeModel(), self.budget, requests.append)
        self.assertEqual((await resumed._dispatch("infra_issue", issue, "three"))["status"], "duplicate")
        with self.assertRaises(PermissionError):
            await observer._dispatch("infra_issue", {**issue, "evidence_refs": [99999]}, "bad")
        with self.assertRaises(ValueError):
            await observer._dispatch("infra_issue", {**issue, "kind": "increase_budget"}, "bad")
        with self.assertRaises(PermissionError):
            await observer._dispatch("observer_report", {"findings": [{"status": "verified", "claim": "success", "evidence_refs": [99999]}]}, "bad")

    async def test_observer_evidence_access_requires_viewed_reference(self):
        ref = self.recorder.put_artifact(b"evidence")
        self.recorder.append("server_evidence", {"evidence_ref": ref})
        observer = ObserverAgent({}, self.root, self.recorder, FakeModel(), self.budget)
        with self.assertRaises(PermissionError):
            await observer._dispatch("evidence_read", {"hash": ref["hash"]}, "early")
        await observer._dispatch("records_read", {}, "read")
        self.assertEqual((await observer._dispatch("evidence_read", {"hash": ref["hash"]}, "ok"))["content"], "evidence")

    async def test_observer_wakeups_never_overlap_model_requests(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowModel:
            calls = active = peak = 0
            observer = None
            async def complete(self, messages, schemas):
                self.calls += 1
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.calls == 1:
                    entered.set()
                    await release.wait()
                else:
                    self.observer.stop()
                self.active -= 1
                return ModelResponse(text="No new verified findings", usage={"prompt_tokens": 0, "completion_tokens": 0})
        model = SlowModel()
        observer = ObserverAgent({"interval_s": 60}, self.root, self.recorder, model, self.budget)
        model.observer = observer
        task = asyncio.create_task(observer.run())
        await entered.wait()
        for _ in range(30):
            observer.wakeup({"kind": "runtime_fault"})
        release.set()
        result = await asyncio.wait_for(task, 2)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(model.calls, 2)
        self.assertEqual(model.peak, 1)
        self.assertTrue(all(row[1:] == ("observer_review", "observer") for row in self.budget.admissions))

    async def test_search_unconfigured_and_private_fetch_are_explicit(self):
        self.assertEqual((await WebTools({"enabled": True}).search("query"))["status"], "unavailable")
        web = WebTools({"enabled": True, "allowed_hosts": ["localhost", "example.com"]})
        with self.assertRaises(PermissionError):
            await web.fetch("https://localhost/")
        with self.assertRaises(PermissionError):
            await web.fetch("https://user:secret@example.com/")
        with self.assertRaises(PermissionError):
            await web.fetch("https://unapproved.example/")

    async def test_fetch_pins_public_address_and_bounds_transport(self):
        web = WebTools({"enabled": True, "allowed_hosts": ["example.com"]})
        async def resolved(*args, **kwargs):
            return [(2, 1, 6, "", ("93.184.216.34", 443))]
        async def read(method, url, **kwargs):
            self.assertEqual(url, "https://93.184.216.34/path")
            self.assertEqual(kwargs["headers"]["host"], "example.com")
            self.assertEqual(kwargs["extensions"]["sni_hostname"], "example.com")
            return {"status": "ok", "content": "document"}
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolved), patch.object(web, "_read", read):
            self.assertEqual((await web.fetch("https://example.com/path"))["content"], "document")

    async def test_workspace_env_rejects_model_and_admin_credentials(self):
        for key in ("OPENAI_API_KEY", "MC_RUNTIME_TOKEN", "HOME", "NODE_OPTIONS", "DOCKER_HOST"):
            with self.assertRaises(ValueError):
                WorkspaceTools(self.root / "sandbox", shell_env={key: "secret"})
        with self.assertRaises(ValueError):
            WorkspaceTools(self.root / "sandbox", shell_env={"MC_AGENT_TOKEN": "token\ninvalid"})
        with self.assertRaises(ValueError):
            self.player(shell_env={"MC_AGENT_ID": "another", "MC_AGENT_TOKEN": "scoped"})
        with self.assertRaises(ValueError):
            ObserverAgent({"shell_env": {"MC_AGENT_ID": "observer", "MC_AGENT_TOKEN": "scoped"}},
                          self.root, self.recorder, FakeModel(), self.budget)
        workspace = WorkspaceTools(self.root / "safe", shell_env={"MC_AGENT_ID": "a", "MC_AGENT_TOKEN": "scoped"})
        self.assertEqual(workspace.shell_env["MC_AGENT_ID"], "a")

    async def test_model_settlement_finishes_before_cancellation_returns(self):
        started, release = asyncio.Event(), asyncio.Event()
        player = self.player()
        async def slow_settle(reservation, usage):
            started.set()
            await release.wait()
            self.budget.settlements.append((reservation, usage))
        self.budget.settle = slow_settle
        task = asyncio.create_task(player._model_call(player.messages, player.schemas))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.budget.settlements), 1)

    async def test_failure_to_record_response_is_infrastructure_failure(self):
        player = self.player()
        original = self.recorder.append
        def failed(kind, payload, **kwargs):
            if kind == "model_response":
                raise OSError("disk full")
            return original(kind, payload, **kwargs)
        with patch.object(self.recorder, "append", failed):
            result = await player.run()
        self.assertEqual(result["status"], "infra_failed")
        self.assertEqual(len(self.budget.admissions), 1)
        self.assertEqual(len(self.budget.settlements), 1)

    async def test_observer_important_event_interrupts_sixty_second_tick(self):
        observer = ObserverAgent({}, self.root, self.recorder, FakeModel(), self.budget)
        self.recorder.append("runtime_recovery_started", {"attempt": 1})
        await asyncio.wait_for(observer._wait_for_review(60), 2)
        self.assertEqual(observer.event_cursor, 0)  # watch never consumes review evidence

    async def test_failed_jobs_query_does_not_erase_known_execution(self):
        async def failed(agent):
            raise ConnectionError("private endpoint")
        self.executor.jobs = failed
        player = self.player()
        result = await player._dispatch("mc_submit", {"source": "return 1"}, "known")
        self.assertEqual(result["execution_id"], "handle")
        self.assertEqual(result["status"], "running")
        self.assertIn("handle", player.pending_executions)
        self.assertNotIn("private endpoint", json.dumps(result))

    async def test_new_runtime_generation_invalidates_local_job_cache(self):
        player = self.player()
        player.pending_jobs["old-job"] = {"status": "running"}
        player._update_epochs({"world_epoch": 1, "runtime_epoch": 0})
        self.assertEqual(player.pending_jobs, {})

    async def test_near_limit_handoff_sees_complete_earlier_context(self):
        handoff = {"objective": "Carry earlier evidence", "plans": [], "verified_facts": ["Earlier observation is stale"],
                   "failed_attempts": [], "source_paths": [], "unresolved_tasks": []}
        model = FakeModel([{"tool_calls": [call("save", "context_handoff", **handoff)]}])
        player = self.player(model)
        player.messages.append({"role": "user", "content": "EARLIER_EVIDENCE_REFERENCE " + "f" * 4000})
        player.messages.append({"role": "user", "content": "r" * 4800})
        original_size = len(json.dumps(player.messages, ensure_ascii=False))
        self.assertGreater(original_size, int(12000 * .85))
        self.assertLess(original_size, 12000)
        await player._prepare_compaction()
        self.assertIn("EARLIER_EVIDENCE_REFERENCE", json.dumps(model.calls[0]["messages"]))
        self.assertIn('"objective": "Observe"', json.dumps(model.calls[0]["messages"], ensure_ascii=False).replace('\\"', '"'))
        player._compress()
        self.assertTrue(player.archives)
        self.assertLess(len(json.dumps(player.messages)), original_size)

    async def test_legacy_artifact_acl_recovers_only_trusted_own_refs(self):
        a, b = self.player(), self.player(agent_id="b")
        request = call("old", "workspace_list")
        a._tool_result(request, {"old": "receipt"})
        event = next(item for item in self.recorder.read_events() if item["kind"] == "tool_result")
        digest = event["payload"]["result_ref"]["hash"]
        a.artifact_index.unlink()
        resumed = self.player()
        self.assertIn("receipt", resumed._artifact_read(digest)["content"])
        with self.assertRaises(PermissionError):
            b._artifact_read(digest)

    async def test_stopped_run_does_not_start_paid_handoff(self):
        player = self.player()
        player.messages.append({"role": "user", "content": "x" * 15000})
        self.budget.stop_requested = lambda: True
        await player._prepare_compaction()
        self.assertEqual(self.budget.admissions, [])
        self.assertFalse(player._compaction_prepared)
