"""Tests for the deterministic Pi-style context projection (``agents/context.py``).

Covers the paper-facing requirements:
* tool file effects (workspace_* and the documented ``mc exec --file`` form);
* compaction summary fields (objective/WIP/next steps/decisions/verified/failed/
  files/blockers/pending/epochs);
* append-only JSONL journal for compaction + recovery projections;
* one structured branch/recovery summary on restart;
* durable ``tool_intent`` journal with unknown-never-replay semantics;
* profile-driven paper normalization (single program/tight/shell/CLI);
* ``memory_search`` dispatch and web-budget wiring.

Run in the disposable test env::

    PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
        .test-venv/bin/python -m pytest -q tests/test_context_engine.py
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from minecraft_harness.agents import AgentLoop
from minecraft_harness.agents.context import ContextEngine
from minecraft_harness.agents.tools import PAPER_TOOL_NAMES, WorkspaceTools
from minecraft_harness.memory import MemoryStore
from minecraft_harness.models import FakeModel


def call(identifier, name, **arguments):
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


class Recorder:
    def __init__(self):
        self.events = []

    def append(self, kind, payload, **kwargs):
        event = {"event_seq": len(self.events) + 1, "kind": kind,
                 "payload": payload, **kwargs}
        self.events.append(event)
        return event

    def put_artifact(self, value):
        return {"hash": "fixture", "sha256": "fixture", "path": "fixture"}

    def kinds(self):
        return [event["kind"] for event in self.events]


class Budget:
    def __init__(self):
        self.calls = 0
        self.settled = []
        self.admission = asyncio.Event()
        self.admission.set()

    def stop_requested(self):
        return False

    def validate_request(self, request_bytes):
        return None

    async def reserve(self, agent_id, *, purpose="agent_decision", role="player"):
        self.calls += 1
        return str(self.calls)

    async def settle(self, reservation, usage):
        self.settled.append((reservation, usage))


class Executor:
    def __init__(self):
        self.executions = []

    async def execute(self, agent, source, **kwargs):
        self.executions.append((agent, source, kwargs))
        return {"status": "succeeded", "execution_id": "execution-1", "result": 42}

    async def submit(self, agent, source, **kwargs):
        return await self.execute(agent, source, **kwargs)

    async def status(self, agent, execution_id):
        return {"status": "succeeded", "execution_id": execution_id}

    async def cancel(self, agent, execution_id):
        return {"status": "cancelled", "execution_id": execution_id}

    async def read_events(self, agent_id, after_cursor=0, limit=100):
        return {"events": [], "next_cursor": after_cursor}


class ContextEngineUnitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.engine = ContextEngine(self.state_dir, agent_id="alice", goal="Observe")

    def journal_entries(self):
        journal = self.state_dir / "context-journal.jsonl"
        return [json.loads(line) for line in journal.read_text().splitlines() if line]

    # -- tool file effects ---------------------------------------------
    def test_tracks_workspace_and_program_file_effects(self):
        self.engine.track_tool("workspace_read", {"path": "notes/plan.md"}, {"content": "x"})
        self.engine.track_tool("workspace_write", {"path": "action.js", "content": "x"}, {"bytes": 1})
        self.engine.track_tool("workspace_remove", {"path": "old.js"}, {"removed": True})
        self.engine.track_tool("mc_execute", {"file": "programs/farm.js"}, {"status": "succeeded"})
        self.engine.track_tool("shell", {"command": "mc exec --file scripts/scan.js --timeout-ms 5000"},
                               {"exit_code": 0, "output": "ok"})
        self.assertEqual(self.engine.files_read,
                         ["notes/plan.md", "programs/farm.js", "scripts/scan.js"])
        self.assertEqual(self.engine.files_modified, ["action.js", "old.js"])
        # Arbitrary shell text and failed tools are not parsed/recorded.
        self.engine.track_tool("shell", {"command": "cat /etc/passwd"}, {"exit_code": 0})
        self.engine.track_tool("workspace_read", {"path": "secret.md"},
                               {"error": "No such file"})
        self.assertNotIn("/etc/passwd", self.engine.files_read)
        self.assertNotIn("secret.md", self.engine.files_read)

    def test_file_tracking_is_deduplicated_and_bounded(self):
        for index in range(80):
            self.engine.track_tool("workspace_write", {"path": f"note-{index}.js"}, {})
        self.engine.track_tool("workspace_write", {"path": "note-79.js"}, {})
        self.assertLessEqual(len(self.engine.files_modified), 64)
        self.assertEqual(self.engine.files_modified[-1], "note-79.js")
        self.assertEqual(len(set(self.engine.files_modified)), len(self.engine.files_modified))

    # -- summary projection --------------------------------------------
    def test_summary_fields_and_legacy_handoff_compatibility(self):
        self.engine.track_tool("workspace_read", {"path": "a.js"}, {"content": "x"})
        handoff = {
            "objective": "Build a stone pickaxe",
            "work_in_progress": ["mining stone"],
            "next_steps": ["craft the pickaxe"],
            "decisions": ["use the west quarry"],
            "verified_facts": ["three cobblestone gathered"],
            "failed_attempts": ["east quarry is blocked"],
            "files_read": ["notes/plan.md"],
            "files_modified": ["craft.js"],
            "blockers": ["missing wooden pickaxe"],
            # Legacy fields still flow into the projection when new ones are absent.
            "plans": ["legacy plan ignored when next_steps exists"],
            "unresolved_tasks": ["old unresolved task"],
        }
        summary = self.engine.build_summary(
            handoff=handoff, pending_executions={"exec-1": "call-1"},
            pending_jobs={"watch": {"status": "running"}},
            world_epoch=2, runtime_epoch=3)
        self.assertEqual(summary["objective"], "Build a stone pickaxe")
        self.assertEqual(summary["work_in_progress"], ["mining stone"])
        self.assertEqual(summary["next_steps"], ["craft the pickaxe"])
        self.assertEqual(summary["decisions"], ["use the west quarry"])
        self.assertEqual(summary["verified_facts"], ["three cobblestone gathered"])
        self.assertEqual(summary["failed_attempts"], ["east quarry is blocked"])
        self.assertEqual(summary["blockers"], ["missing wooden pickaxe"])
        self.assertEqual(summary["files_read"], ["a.js", "notes/plan.md"])
        self.assertEqual(summary["files_modified"], ["craft.js"])
        self.assertEqual(summary["pending_executions"], {"exec-1": "call-1"})
        self.assertEqual(summary["pending_jobs"], ["watch"])
        self.assertEqual((summary["world_epoch"], summary["runtime_epoch"]), (2, 3))

    def test_summary_uses_legacy_plans_and_defaults(self):
        summary = self.engine.build_summary(
            handoff={"plans": ["legacy plan"], "unresolved_tasks": ["legacy blocker"]},
            goal="Observe", pending_executions=None, pending_jobs=None)
        self.assertEqual(summary["objective"], "Observe")
        self.assertEqual(summary["next_steps"], ["legacy plan"])
        self.assertEqual(summary["blockers"], ["legacy blocker"])
        self.assertEqual(summary["pending_executions"], {})
        self.assertEqual(summary["pending_jobs"], [])

    # -- journal / recovery --------------------------------------------
    def test_compaction_and_recovery_journal_are_append_only_and_replayable(self):
        summary = self.engine.build_summary(
            handoff={"objective": "obj", "decisions": ["decision-1"],
                     "files_read": ["a.js"], "files_modified": ["b.js"]},
            pending_executions={"exec-1": "call"}, pending_jobs=["job"], world_epoch=1)
        self.engine.record_compaction("history-deadbeef.json", summary, [3, 10])
        self.engine.record_recovery("harness_restart", {"reason": "killed"})
        entries = self.journal_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["compaction", "recovery"])
        self.assertEqual(entries[0]["archive"], "history-deadbeef.json")
        self.assertEqual(entries[0]["retained_range"], [3, 10])
        self.assertEqual(entries[0]["summary"]["files_read"], ["a.js"])
        self.assertEqual(entries[1]["reason"], "harness_restart")
        # A fresh engine rebuilds its projection from the journal alone.
        restored = ContextEngine(self.state_dir, agent_id="alice", goal="Observe")
        self.assertEqual(restored.compactions, 1)
        self.assertEqual(restored.recoveries, 1)
        self.assertEqual(restored.decisions, ["decision-1"])
        self.assertEqual(restored.files_read, ["a.js"])
        self.assertEqual(restored.files_modified, ["b.js"])
        self.assertEqual(restored.last_summary["objective"], "obj")

    def test_state_roundtrip_and_invalid_state_tolerance(self):
        self.engine.track_tool("workspace_write", {"path": "a.js"}, {})
        self.engine.decisions = ["keep the server up"]
        state = json.loads(json.dumps(self.engine.state()))
        restored = ContextEngine(self.state_dir, agent_id="alice")
        restored.load_state(state)
        self.assertEqual(restored.files_modified, ["a.js"])
        self.assertEqual(restored.decisions, ["keep the server up"])
        restored.load_state(None)
        restored.load_state({"schema_version": 99, "files_read": ["ignored"]})
        self.assertEqual(restored.files_read, [])

    def test_recovery_message_is_structured_and_bounded(self):
        self.engine.files_read = ["notes/plan.md"]
        self.engine.files_modified = ["action.js"]
        message = self.engine.recovery_message(
            "harness_restart",
            previous_epoch=(0, 1), world_epoch=1, runtime_epoch=0,
            pending_executions={"exec-1": "call-1"},
            pending_jobs=["watch"], next_steps=["re-read the plan"],
            details={"recovered_executions": [{"status": "succeeded"}]})
        self.assertEqual(message["kind"], "branch_summary")
        self.assertEqual(message["reason"], "harness_restart")
        self.assertTrue(message["epoch_changed"])
        self.assertEqual(message["previous_epoch"], [0, 1])
        self.assertEqual((message["world_epoch"], message["runtime_epoch"]), (1, 0))
        self.assertTrue(message["globals_may_be_lost"])
        self.assertTrue(message["requires_reobservation"])
        self.assertEqual(message["pending_executions"], {"exec-1": "call-1"})
        self.assertEqual(message["pending_jobs"], ["watch"])
        self.assertEqual(message["next_steps"][0], "re-read the plan")
        self.assertIn("do_not_replay", message)
        self.assertLessEqual(len(json.dumps(message)), 6000)

    def test_recovery_message_defaults_to_last_summary_next_steps(self):
        summary = self.engine.build_summary(
            handoff={"objective": "obj", "next_steps": ["continue mining"]})
        self.engine.record_compaction("history-x.json", summary, [0, 1])
        message = self.engine.recovery_message("world_reset", world_epoch=2)
        self.assertEqual(message["next_steps"], ["continue mining"])
        self.assertFalse(message["epoch_changed"])


class AgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Core/loop integration: compaction projection, tool journal, recovery."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.recorder = Recorder()
        self.executor = Executor()
        self.budget = Budget()
        self.memory = MemoryStore(self.root / "memory")

    def agent(self, model=None, **config):
        return AgentLoop({"agent_id": "alice", "goal": "Observe", "max_no_progress_turns": 2,
                          **config}, self.root, self.recorder, self.executor,
                         model or FakeModel(), self.budget, self.memory)

    # -- compaction ----------------------------------------------------
    async def test_compaction_summary_uses_context_projection_and_keeps_pairing(self):
        agent = self.agent(context_chars=12000)
        agent.context.track_tool("workspace_read", {"path": "notes/plan.md"}, {"content": "x"})
        agent.handoff = {
            "objective": "Build a pickaxe", "complete": True, "source": "agent_handoff",
            "plans": ["mine stone"], "next_steps": ["craft pickaxe"],
            "decisions": ["use the west quarry"],
            "verified_facts": ["three stone mined"], "failed_attempts": ["east blocked"],
            "files_read": ["notes/plan.md"], "files_modified": ["craft.js"],
            "blockers": ["no wood"],
            "work_in_progress": ["mining stone"],
            "evidence": "legacy",
        }
        agent.pending_executions["exec-1"] = "call-1"
        agent.pending_jobs["watch"] = {"status": "running"}
        agent.messages.append({"role": "assistant", "content": None,
                               "tool_calls": [call("call-1", "workspace_read", path="notes/plan.md")]})
        agent.messages.append({"role": "tool", "tool_call_id": "call-1", "content": "x" * 8000})
        agent.messages.append({"role": "user", "content": "y" * 8000})
        agent._compress()
        self.assertTrue(agent.archives)
        compacted = [event for event in self.recorder.events
                     if event["kind"] == "context_compacted"][-1]
        summary = compacted["payload"]["summary"]
        self.assertEqual(summary["objective"], "Build a pickaxe")
        self.assertEqual(summary["work_in_progress"], ["mining stone"])
        self.assertEqual(summary["next_steps"], ["craft pickaxe"])
        self.assertEqual(summary["decisions"], ["use the west quarry"])
        self.assertEqual(summary["verified_facts"], ["three stone mined"])
        self.assertEqual(summary["failed_attempts"], ["east blocked"])
        self.assertIn("notes/plan.md", summary["files_read"])
        self.assertIn("craft.js", summary["files_modified"])
        self.assertEqual(summary["blockers"], ["no wood"])
        self.assertEqual(summary["pending_executions"], {"exec-1": "call-1"})
        self.assertEqual(summary["pending_jobs"], ["watch"])
        self.assertIn("retained_range", compacted["payload"])
        # The journal got the same compaction projection.
        journal = [json.loads(line) for line in
                   (agent.state_dir / "context-journal.jsonl").read_text().splitlines()]
        self.assertEqual(journal[-1]["kind"], "compaction")
        self.assertEqual(journal[-1]["summary"]["work_in_progress"], ["mining stone"])
        # Archived original messages remain pageable through history_read.
        archive = agent.archives[0]
        self.assertIn("notes/plan.md", (agent.state_dir / archive).read_text())
        page = await agent._dispatch("history_read", {"archive": archive, "offset": 0,
                                                      "limit": 100}, "page")
        self.assertTrue(page["content"])
        # Retained context keeps assistant/tool pairing valid.
        pending = set()
        for message in agent.messages:
            if message["role"] == "assistant":
                self.assertFalse(pending)
                pending = {item["id"] for item in message.get("tool_calls", [])}
            elif message["role"] == "tool":
                pending.discard(message["tool_call_id"])
            else:
                self.assertFalse(pending)
        self.assertFalse(pending)

    def test_context_projection_is_persisted_in_the_checkpoint(self):
        agent = self.agent()
        agent.context.track_tool("workspace_write", {"path": "plan.md"}, {})
        agent.context.decisions = ["stay on task"]
        agent._checkpoint()
        resumed = self.agent()
        self.assertEqual(resumed.context.files_modified, ["plan.md"])
        self.assertEqual(resumed.context.decisions, ["stay on task"])

    # -- tool intent journal ------------------------------------------
    async def test_tool_intent_is_durable_before_dispatch_without_raw_args(self):
        secret = "do-not-log-this-value"
        model = FakeModel([{"tool_calls": [
            call("write-1", "workspace_write", path="action.js", content=secret)]}])
        agent = self.agent(model, max_turns=1)
        await agent.run()
        kinds = self.recorder.kinds()
        self.assertIn("tool_intent", kinds)
        self.assertLess(kinds.index("tool_intent"), kinds.index("tool_result"))
        intent = next(event for event in self.recorder.events
                      if event["kind"] == "tool_intent")["payload"]
        self.assertEqual(intent["tool"], "workspace_write")
        self.assertEqual(intent["call_id"], "write-1")
        self.assertEqual(len(intent["args_sha256"]), 64)
        self.assertEqual(intent["idempotency"], "side_effect")
        self.assertNotIn(secret, json.dumps(intent))
        # Successful writes feed the context projection.
        self.assertIn("action.js", agent.context.files_modified)

    async def test_read_only_intent_is_classified_and_never_replayed(self):
        model = FakeModel([{"tool_calls": [call("read-1", "workspace_read", path="notes.md")]}])
        agent = self.agent(model, max_turns=1)
        await agent.run()
        intent = next(event for event in self.recorder.events
                      if event["kind"] == "tool_intent")["payload"]
        self.assertEqual(intent["idempotency"], "read_only")

    # -- recovery ------------------------------------------------------
    async def test_restart_records_unknown_effect_and_structured_recovery(self):
        pending_call = call("uncertain", "mc_execute", file="danger.js")
        agent = self.agent()
        agent.context.track_tool("workspace_write", {"path": "notes/plan.md"}, {})
        agent.messages.append({"role": "assistant", "content": None,
                               "tool_calls": [pending_call]})
        agent.pending_tools = [pending_call]
        agent._checkpoint()
        resumed = self.agent(FakeModel(), max_turns=1)
        await resumed.run()
        self.assertEqual(self.executor.executions, [])  # never replayed
        unknown = next(event for event in self.recorder.events
                       if event["kind"] == "tool_effect_unknown")
        self.assertEqual(unknown["payload"]["tool"], "mc_execute")
        self.assertEqual(unknown["payload"]["status"], "unknown")
        self.assertEqual(unknown["payload"]["idempotency"], "side_effect")
        self.assertFalse(unknown["payload"]["replayed"])
        tool_message = next(item for item in resumed.messages
                            if item.get("tool_call_id") == "uncertain")
        self.assertEqual(json.loads(tool_message["content"])["status"], "unknown")
        branch = next(event for event in self.recorder.events
                      if event["kind"] == "context_branch_summary")
        self.assertEqual(branch["payload"]["reason"], "harness_restart")
        recovery_message = next(item for item in resumed.messages
                                if item["role"] == "user" and "branch_summary" in str(item["content"]))
        message = json.loads(recovery_message["content"])
        self.assertIn("do_not_replay", message)
        self.assertTrue(message["requires_reobservation"])
        self.assertTrue(message["globals_may_be_lost"])
        self.assertIn("next_steps", message)
        journal = [json.loads(line) for line in
                   (resumed.state_dir / "context-journal.jsonl").read_text().splitlines()]
        self.assertTrue(any(entry["kind"] == "recovery" for entry in journal))

    # -- memory search -------------------------------------------------
    async def test_memory_search_dispatch_returns_ranked_snippets(self):
        self.memory.write("alice", "team", "team.md", "dragon portal coordinates", 0)
        self.memory.write("alice", "private", "private.md", "dragon private plan", 0)
        self.memory.write("bob", "private", "bob.md", "dragon hidden from alice", 0)
        agent = self.agent()
        result = await agent._dispatch("memory_search", {"query": "dragon", "scope": "all"}, "search")
        paths = {item["path"] for item in result["results"]}
        self.assertEqual(paths, {"team.md", "private.md"})
        self.assertEqual(result["count"], len(result["results"]))
        for item in result["results"]:
            self.assertIn("snippet", item)
            self.assertIn("rank", item)
            self.assertNotIn("content", item)
        # The search itself is an intent-journaled read-only tool.
        intents = [event for event in self.recorder.events if event["kind"] == "tool_intent"]
        self.assertEqual(intents[-1]["payload"]["idempotency"], "read_only")


class PaperNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.recorder = Recorder()
        self.budget = Budget()

    def player(self, workspace=None, **config):
        return AgentLoop({"agent_id": "alice", "goal": "Observe", "max_turns": 1, **config},
                         self.root, self.recorder, Executor(), FakeModel(), self.budget,
                         None, workspace=workspace)

    def shell_workspace(self):
        return WorkspaceTools(self.root / "sandbox", shell_enabled=True,
                              shell_container="mc-workspace-test")

    def test_paper_profile_with_shell_forces_the_minimal_surface(self):
        player = self.player(workspace=self.shell_workspace(),
                             profile="paper_aligned", lifecycle="paper_aligned",
                             context_chars=12000)
        self.assertTrue(player.paper_profile)
        self.assertTrue(player.paper_single_program)
        self.assertTrue(player.paper_tight_interface)
        self.assertTrue(player.paper_shell_cli)
        self.assertTrue(player.paper_cli_output)
        names = {tool["function"]["name"] for tool in player.schemas}
        expected = set(PAPER_TOOL_NAMES) - {"web_search", "web_fetch"}
        self.assertEqual(names, expected)
        self.assertNotIn("mc_execute", names)
        self.assertIn("shell", names)
        self.assertIn("memory_search", names)

    def test_non_paper_profile_keeps_the_legacy_tool_surface(self):
        player = self.player(workspace=self.shell_workspace(), lifecycle="strict",
                             context_chars=12000)
        self.assertFalse(player.paper_profile)
        self.assertFalse(player.paper_single_program)
        names = {tool["function"]["name"] for tool in player.schemas}
        self.assertIn("mc_execute", names)
        self.assertNotIn("memory_search", names)

    def test_web_tools_receive_the_team_budget_and_identity(self):
        player = self.player(web={"enabled": True, "allowed_hosts": ["example.com"]})
        self.assertIs(player.web.budget, self.budget)
        self.assertEqual(player.web.agent_id, "alice")


if __name__ == "__main__":
    unittest.main()
