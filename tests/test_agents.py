import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from minecraft_harness.agents import AgentLoop, WorkspaceTools
from minecraft_harness.memory import MemoryStore
from minecraft_harness.models import FakeModel, ModelError


def call(identifier, name, **arguments):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


class Recorder:
    def __init__(self):
        self.events = []

    def append(self, kind, payload, **kwargs):
        event = {"event_seq": len(self.events) + 1, "kind": kind, "payload": payload, **kwargs}
        self.events.append(event)
        return event

    def put_artifact(self, value):
        return {"hash": "fixture", "path": "fixture"}


class Budget:
    def __init__(self):
        self.calls = 0
        self.settled = []
        self.admission = asyncio.Event()
        self.admission.set()

    def stop_requested(self):
        return False

    async def reserve(self, agent):
        self.calls += 1
        return str(self.calls)

    async def settle(self, reservation, usage):
        self.settled.append((reservation, usage))


class Executor:
    def __init__(self):
        self.executions = []
        self.event_reads = 0

    async def execute(self, agent, source, **kwargs):
        self.executions.append((agent, source, kwargs))
        return {"status": "succeeded", "execution_id": "execution-1", "result": 42}

    async def status(self, agent, execution_id):
        return {"status": "succeeded", "execution_id": execution_id}

    async def cancel(self, agent, execution_id):
        return {"status": "cancelled", "execution_id": execution_id}

    async def read_events(self, agent_id, after_cursor=0, limit=100):
        self.event_reads += 1
        return {"events": [], "next_cursor": after_cursor}


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.recorder = Recorder()
        self.executor = Executor()
        self.budget = Budget()

    def agent(self, model, **config):
        return AgentLoop({"agent_id": "alice", "goal": "fixture", "max_no_progress_turns": 2, **config},
                         self.root, self.recorder, self.executor, model, self.budget,
                         MemoryStore(self.root / "memory"))

    async def test_ordered_file_game_tools_and_tool_result_pairing(self):
        model = FakeModel([{"tool_calls": [
            call("write-1", "workspace_write", path="action.js", content="return 42"),
            call("execute-1", "mc_execute", file="action.js")]}])
        agent = self.agent(model)
        result = await agent.run()
        self.assertEqual(result["status"], "stalled")
        self.assertEqual(self.executor.executions[0][1], "return 42")
        self.assertEqual(self.executor.executions[0][2]["request_id"], "alice:1:execute-1")
        conversation = model.calls[1]["messages"]
        assistant = next(item for item in conversation if item.get("tool_calls"))
        results = [item["tool_call_id"] for item in conversation if item["role"] == "tool"]
        self.assertEqual([item["id"] for item in assistant["tool_calls"]], results)
        self.assertEqual(len(self.budget.settled), self.budget.calls)

    async def test_recovery_never_replays_uncertain_execution(self):
        agent = self.agent(FakeModel(), max_turns=1)
        pending = call("uncertain", "mc_execute", source="bot.dig(target)")
        agent.messages.append({"role": "assistant", "content": None, "tool_calls": [pending]})
        agent.pending_tools = [pending]
        agent._checkpoint()
        recovered = self.agent(FakeModel(), max_turns=1)
        await recovered.run()
        self.assertFalse(self.executor.executions)
        tool = next(item for item in recovered.messages if item.get("tool_call_id") == "uncertain")
        self.assertEqual(json.loads(tool["content"])["status"], "unknown")
        self.assertFalse(recovered.pending_tools)

    async def test_reused_tool_ids_do_not_reuse_execution_requests(self):
        model = FakeModel([{"tool_calls": [call("reused", "mc_execute", source="return 42")]},
                           {"tool_calls": [call("reused", "mc_execute", source="return 43")]}])
        await self.agent(model, max_turns=2).run()
        identifiers = [item[2]["request_id"] for item in self.executor.executions]
        self.assertEqual(len(set(identifiers)), 2)

    async def test_repeated_unchanged_tool_responses_stall(self):
        script = [{"tool_calls": [call(str(index), "workspace_list")]} for index in range(10)]
        result = await self.agent(FakeModel(script), max_turns=10).run()
        self.assertEqual(result["status"], "stalled")
        self.assertEqual(result["turns"], 3)

    async def test_pause_blocks_event_reads_and_model_admission(self):
        self.budget.admission.clear()
        agent = self.agent(FakeModel(), max_turns=1)
        task = asyncio.create_task(agent.run())
        try:
            await asyncio.sleep(0)
            self.assertEqual(self.executor.event_reads, 0)
            self.assertEqual(self.budget.calls, 0)
        finally:
            self.budget.admission.set()
            await task
        self.assertEqual(self.budget.calls, 1)

    async def test_reset_during_model_request_invalidates_old_tools(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        class SlowModel(FakeModel):
            async def complete(self, messages, tools):
                entered.set()
                await finish.wait()
                return await super().complete(messages, tools)

        class ResetExecutor(Executor):
            reset_pending = False

            async def read_events(self, agent_id, after_cursor=0, limit=100):
                self.event_reads += 1
                if self.reset_pending:
                    self.reset_pending = False
                    return {"events": [{"kind": "world_restored", "world_epoch": 2,
                                        "runtime_epoch": 3, "payload": {"globals_lost": True}}],
                            "next_cursor": after_cursor + 1}
                return {"events": [], "next_cursor": after_cursor}

        self.executor = ResetExecutor()
        model = SlowModel([{"tool_calls": [call("old", "mc_execute", source="return 'old-plan'")]},
                           {"tool_calls": [call("fresh", "mc_execute", source="return 'observed-again'")]}])
        agent = self.agent(model, max_turns=2)
        task = asyncio.create_task(agent.run())
        await entered.wait()
        self.budget.admission.clear()
        finish.set()
        await asyncio.sleep(0)
        self.assertFalse(self.executor.executions)
        self.assertEqual(self.executor.event_reads, 1)
        self.executor.reset_pending = True
        self.budget.admission.set()
        await task
        self.assertEqual(len(self.executor.executions), 1)
        self.assertEqual(self.executor.executions[0][1], "return 'observed-again'")
        self.assertEqual(agent.world_epoch, 2)
        invalidated = next(item for item in agent.messages if item.get("tool_call_id") == "old")
        self.assertEqual(json.loads(invalidated["content"])["status"], "invalidated")
        # No user event is interleaved between an assistant tool call and reply.
        second_request = model.calls[1]["messages"]
        old_index = next(index for index, item in enumerate(second_request) if item.get("tool_calls"))
        self.assertEqual(second_request[old_index + 1]["role"], "tool")
        self.assertEqual(second_request[old_index + 2]["role"], "user")

    async def test_workspace_namespace_and_shell_default(self):
        workspace = WorkspaceTools(self.root / "workspace")
        outside = self.root / "outside"
        outside.write_text("secret")
        (workspace.root / "link").symlink_to(outside)
        for path in ("../outside", str(outside), "link"):
            with self.assertRaises(PermissionError):
                workspace.read(path)
        with self.assertRaises(PermissionError):
            await workspace.shell("echo should-not-run")

    async def test_compaction_keeps_pairing_and_archives_original(self):
        agent = self.agent(FakeModel(), context_chars=4000)
        for index in range(5):
            item = call(str(index), "workspace_read", path="large")
            agent.messages.append({"role": "assistant", "content": "intention", "tool_calls": [item]})
            agent.messages.append({"role": "tool", "tool_call_id": str(index), "content": "x" * 3000})
        agent._compress()
        self.assertLess(len(json.dumps(agent.messages)), 4000)
        self.assertTrue(agent.archives)
        self.assertTrue((agent.state_dir / agent.archives[-1]).exists())
        ids = {call["id"] for item in agent.messages for call in item.get("tool_calls", [])}
        self.assertTrue(all(item["tool_call_id"] in ids for item in agent.messages if item["role"] == "tool"))

    async def test_failed_model_billing_unknown_and_stops(self):
        class Failing:
            async def complete(self, messages, tools):
                raise RuntimeError("secret-api-key")
        result = await self.agent(Failing()).run()
        self.assertEqual(result["status"], "model_failed")
        self.assertIsNone(self.budget.settled[0][1]["total_tokens"])
        self.assertNotIn("secret-api-key", json.dumps(self.recorder.events))

    async def test_transient_model_failures_reserve_and_settle_every_attempt(self):
        class Flaky(FakeModel):
            attempts = 0

            async def complete(self, messages, tools):
                self.attempts += 1
                if self.attempts <= 2:
                    raise ModelError("provider temporarily unavailable", retryable=True)
                return await super().complete(messages, tools)

        model = Flaky([{"text": "observed"}])
        result = await self.agent(model, max_turns=1, retry_base_s=0).run()
        self.assertEqual(result["turns"], 1)
        self.assertEqual(self.budget.calls, 3)
        self.assertEqual(len(self.budget.settled), 3)
        self.assertIsNone(self.budget.settled[0][1]["total_tokens"])

    async def test_unresponsive_custom_model_has_finite_total_deadline_and_retry_count(self):
        class Hung:
            async def complete(self, messages, tools):
                await asyncio.Event().wait()

        result = await asyncio.wait_for(self.agent(Hung(), max_model_failures=2, retry_base_s=0,
                                                   model_request_timeout_s=0.01).run(), 0.5)
        self.assertEqual(result["status"], "model_failed")
        self.assertEqual(self.budget.calls, 2)
        self.assertEqual(len(self.budget.settled), 2)

    async def test_event_transport_blip_does_not_end_agent(self):
        class FlakyEvents(Executor):
            async def read_events(self, agent_id, after_cursor=0, limit=100):
                self.event_reads += 1
                if self.event_reads <= 2:
                    raise ConnectionError("private endpoint")
                return {"events": [], "next_cursor": after_cursor}

        self.executor = FlakyEvents()
        result = await self.agent(FakeModel(), max_turns=1, retry_base_s=0).run()
        self.assertEqual(result["turns"], 1)
        self.assertEqual(self.executor.event_reads, 3)
        self.assertNotIn("private endpoint", json.dumps(self.recorder.events))

    async def test_uncertain_submit_is_never_implicitly_retried(self):
        class LostAck(Executor):
            async def execute(self, agent, source, **kwargs):
                self.executions.append(source)
                raise ConnectionError("ack lost after dispatch")

        self.executor = LostAck()
        agent = self.agent(FakeModel([{"tool_calls": [call("once", "mc_execute", source="irreversible_action()")]}]), max_turns=1)
        await agent.run()
        result = json.loads(next(item["content"] for item in agent.messages if item.get("tool_call_id") == "once"))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(self.executor.executions), 1)
        await self.agent(FakeModel(), max_turns=1).run()
        self.assertEqual(len(self.executor.executions), 1)

    async def test_many_large_turns_keep_requests_bounded_and_old_archives_pageable(self):
        script = [{"text": "intent" * 2000, "tool_calls": [
            call(str(index), "workspace_write", path=f"notes/{index}", content=str(index))]} for index in range(80)]
        model = FakeModel(script)
        agent = self.agent(model, max_turns=80, context_chars=4000)
        await agent.run()
        for request in model.calls:
            messages = request["messages"]
            self.assertLessEqual(len(json.dumps(messages, ensure_ascii=False)), 4000)
            pending = set()
            for message in messages:
                if message["role"] == "assistant":
                    self.assertFalse(pending)
                    pending = {item["id"] for item in message.get("tool_calls", [])}
                elif message["role"] == "tool":
                    pending.remove(message["tool_call_id"])
                else:
                    self.assertFalse(pending)
            self.assertFalse(pending)
        self.assertEqual(len(agent.archives), 64)
        old_archive = sorted(agent.state_dir.glob("history-*.json"))[0]
        page = await agent._dispatch("history_read", {"archive": old_archive.name, "offset": 7, "limit": 100}, "read")
        self.assertEqual(page["content"], old_archive.read_text()[7:107])
        self.assertEqual(page["next_offset"], 107)

    async def test_single_oversized_event_is_archived_before_model_request(self):
        agent = self.agent(FakeModel(), context_chars=4000)
        agent.messages.append({"role": "user", "content": "chat" * 20000})
        agent._compress()
        self.assertLessEqual(len(json.dumps(agent.messages, ensure_ascii=False)), 4000)
        self.assertTrue(agent.archives)

    async def test_unknown_execution_status_survives_recovery_and_compaction(self):
        class UnknownStatus(Executor):
            async def status(self, agent, execution_id):
                return {"status": "unknown", "execution_id": execution_id}

        self.executor = UnknownStatus()
        agent = self.agent(FakeModel(), context_chars=4000)
        agent.pending_executions["unresolved"] = "old-tool"
        agent._checkpoint()
        resumed = self.agent(FakeModel(), max_turns=1, context_chars=4000)
        await resumed.run()
        self.assertIn("unresolved", resumed.pending_executions)
        await resumed._dispatch("mc_status", {"execution_id": "unresolved"}, "query")
        self.assertIn("unresolved", resumed.pending_executions)
        resumed.messages.append({"role": "user", "content": "large" * 10000})
        resumed._compress()
        self.assertIn("unresolved", resumed.pending_executions)
        self.assertIn("unresolved", resumed.messages[1]["content"])
        pending = await resumed._dispatch("mc_pending", {}, "pending")
        self.assertEqual(pending["executions"], [{"execution_id": "unresolved", "tool_call_id": "old-tool"}])

    async def test_truncated_tool_output_preserves_execution_and_pagination_handles(self):
        agent = self.agent(FakeModel(), context_chars=4000)
        request = call("read", "workspace_read", path="large")
        agent._tool_result(request, {"content": "\\\"" * 10000, "execution_id": "execution", "next_offset": 123})
        encoded = agent.messages[-1]["content"]
        self.assertLessEqual(len(encoded), 1000)
        self.assertEqual(json.loads(encoded)["next_offset"], 123)
        self.assertEqual(json.loads(encoded)["execution_id"], "execution")

    async def test_event_gap_after_model_decision_invalidates_tool_bundle(self):
        class GapEvents(Executor):
            async def read_events(self, agent_id, after_cursor=0, limit=100):
                self.event_reads += 1
                return {"events": [{"kind": "events_gap"}] if self.event_reads == 2 else [],
                        "next_cursor": self.event_reads}

        self.executor = GapEvents()
        agent = self.agent(FakeModel([{"tool_calls": [call("stale", "mc_execute", source="act()")]}]), max_turns=1)
        await agent.run()
        self.assertFalse(self.executor.executions)
        response = next(message for message in agent.messages if message.get("tool_call_id") == "stale")
        self.assertEqual(json.loads(response["content"])["status"], "invalidated")

    async def test_reliability_gap_exception_fails_closed_without_advancing_cursor(self):
        class ReliabilityFailure(RuntimeError):
            retryable = False

        class BrokenEvents(Executor):
            async def read_events(self, agent_id, after_cursor=0, limit=100):
                raise ReliabilityFailure("unrecoverable evidence gap")

        self.executor = BrokenEvents()
        agent = self.agent(FakeModel())
        result = await agent.run()
        self.assertEqual(result["status"], "infra_failed")
        self.assertEqual(agent.event_cursor, 0)
        self.assertEqual(self.budget.calls, 0)

    async def test_maintenance_gate_stops_event_retries_consuming_their_window(self):
        failed = asyncio.Event()

        class MaintenanceEvents(Executor):
            async def read_events(inner, agent_id, after_cursor=0, limit=100):
                inner.event_reads += 1
                if inner.event_reads == 1:
                    self.budget.admission.clear()
                    failed.set()
                    raise ConnectionError("maintenance begins")
                return {"events": [], "next_cursor": after_cursor}

        self.executor = MaintenanceEvents()
        agent = self.agent(FakeModel(), max_turns=1, max_event_failures=2, retry_base_s=.001)
        task = asyncio.create_task(agent.run())
        await failed.wait()
        await asyncio.sleep(.04)  # Much longer than this test's retry backoff.
        self.assertEqual(self.executor.event_reads, 1)
        self.assertFalse(task.done())
        self.budget.admission.set()
        result = await task
        self.assertEqual(result["turns"], 1)
        self.assertEqual(self.executor.event_reads, 2)

    async def test_budget_storage_errors_fail_as_infrastructure(self):
        from unittest.mock import patch
        with patch.object(self.budget, "reserve", side_effect=OSError("disk failed")):
            result = await self.agent(FakeModel()).run()
        self.assertEqual(result["status"], "infra_failed")
        self.assertFalse(any(event["kind"] == "agent_budget_exhausted" for event in self.recorder.events))

    async def test_queued_or_unknown_submission_null_epoch_does_not_poison_ledger(self):
        class NullEpoch(Executor):
            async def execute(self, agent, source, **kwargs):
                return {"status": "unknown", "execution_id": "uncertain", "runtime_epoch": None, "world_epoch": None}

        self.executor = NullEpoch()
        agent = self.agent(FakeModel([{"tool_calls": [call("null", "mc_execute", source="act()")]}]), max_turns=1)
        agent.world_epoch, agent.runtime_epoch = 2, 3
        await agent.run()
        self.assertEqual((agent.world_epoch, agent.runtime_epoch), (2, 3))
        self.assertTrue(all(event["runtime_epoch"] == 3 for event in self.recorder.events))

    async def test_compaction_leaves_headroom_to_avoid_archiving_every_turn(self):
        script = [{"text": "fixture intention " * 30, "tool_calls": [
            call(str(index), "workspace_write", path=f"note-{index}", content="value")]} for index in range(100)]
        agent = self.agent(FakeModel(script), max_turns=100, context_chars=12000)
        await agent.run()
        compactions = [event for event in self.recorder.events if event["kind"] == "context_compacted"]
        self.assertGreater(len(compactions), 0)
        self.assertLess(len(compactions), 35)

    async def test_authoritative_reset_bypasses_chat_backlog_and_invalidates_old_decision(self):
        class BackloggedEvents(Executor):
            runtime_epoch = 1

            async def read_events(self, agent_id, after_cursor=0, limit=100):
                self.event_reads += 1
                # The reset notification is event 251; the old tool decision
                # reads only pages 1 and 2 before dispatch would otherwise run.
                if self.event_reads == 2:
                    self.runtime_epoch = 2
                end = min(251, after_cursor + limit)
                events = [{"kind": "chat", "world_epoch": 0, "runtime_epoch": 1,
                           "payload": {"text": str(index)}} if index < 251 else
                          {"kind": "runtime_reset", "world_epoch": 0, "runtime_epoch": 2}
                          for index in range(after_cursor + 1, end + 1)]
                return {"events": events, "next_cursor": end, "world_epoch": 0,
                        "runtime_epoch": self.runtime_epoch, "session_id": "current"}

        self.executor = BackloggedEvents()
        model = FakeModel([{"tool_calls": [call("stale", "mc_execute", source="old_globals_action()") ]},
                           {"tool_calls": [call("fresh", "mc_execute", source="return 'observed-new-generation'")]}])
        agent = self.agent(model, max_turns=2)
        await agent.run()
        self.assertEqual([item[1] for item in self.executor.executions], ["return 'observed-new-generation'"])
        stale = next(item for item in agent.messages if item.get("tool_call_id") == "stale")
        self.assertEqual(json.loads(stale["content"])["status"], "invalidated")
        self.assertEqual((agent.world_epoch, agent.runtime_epoch), (0, 2))

    async def test_tool_result_generation_change_invalidates_remaining_tools_without_reset_event(self):
        class ResetResult(Executor):
            async def execute(self, agent, source, **kwargs):
                self.executions.append(source)
                return {"status": "timed_out", "execution_id": "resetting", "world_epoch": 0, "runtime_epoch": 1}

        self.executor = ResetResult()
        agent = self.agent(FakeModel([{"tool_calls": [call("first", "mc_execute", source="timeout_and_reset()"),
                                                     call("stale", "mc_execute", source="use_old_globals()")]}]), max_turns=1)
        await agent.run()
        self.assertEqual(self.executor.executions, ["timeout_and_reset()"])
        stale = next(item for item in agent.messages if item.get("tool_call_id") == "stale")
        self.assertEqual(json.loads(stale["content"])["status"], "invalidated")

    async def test_old_chat_cannot_regress_world_and_runtime_generation_pair(self):
        class OldChat(Executor):
            async def read_events(self, agent_id, after_cursor=0, limit=100):
                return {"events": [{"kind": "chat", "world_epoch": 4, "runtime_epoch": 99}],
                        "world_epoch": 5, "runtime_epoch": 1, "next_cursor": 1}

        self.executor = OldChat()
        agent = self.agent(FakeModel())
        agent.world_epoch, agent.runtime_epoch = 4, 100
        result = await agent._read_events()
        self.assertEqual((agent.world_epoch, agent.runtime_epoch), (5, 1))
        self.assertTrue(any(event["kind"] == "world_restored" for event in result["events"]))
        agent._update_epochs({"world_epoch": 4, "runtime_epoch": 1000})
        self.assertEqual((agent.world_epoch, agent.runtime_epoch), (5, 1))

    async def test_workspace_write_failure_preserves_previous_source_and_quota_can_be_reclaimed(self):
        from unittest.mock import patch
        workspace = WorkspaceTools(self.root / "atomic", max_workspace_bytes=100, max_workspace_files=1)
        workspace.write("source", "old source")
        with patch("minecraft_harness.agents.tools.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                workspace.write("source", "new source")
        self.assertEqual(workspace.read("source")["content"], "old source")
        self.assertEqual(len(list(workspace.root.iterdir())), 1)
        with self.assertRaisesRegex(ValueError, "quota"):
            workspace.write("another", "content")
        workspace.remove("source")
        workspace.write("another", "content")
        self.assertEqual(workspace.read("another", offset=2, limit=2)["content"], "nt")
