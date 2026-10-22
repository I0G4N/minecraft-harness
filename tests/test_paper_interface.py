"""Paper-visible prompt and tool surface alignment (paper-framework-v4).

These tests pin the model-visible interface to the categories the paper publishes
(§2.1 tools/environment, §2.2 instructions, §2.3 Observer role).  They are
deliberately independent of the internal harness: no recovery, artifact,
evidence or scheduling terminology may leak into the paper profile.
"""
from __future__ import annotations

import json
import re
import unittest

from minecraft_harness.agents.prompts import (
    PROMPT_VERSION,
    STRICT_PROMPT_VERSION,
    observer_prompt,
    paper_observer_prompt,
    paper_player_prompt,
    player_prompt,
)
from minecraft_harness.agents.tools import PAPER_TOOL_NAMES, tool_schemas

GOAL = (
    "The objective is to defeat the Ender Dragon, starting from the spawn of the session's "
    "shared Minecraft survival world. This is the only criterion. Any means of meeting it "
    "within the rules of survival mode is permitted, and the players decide how to share the work."
)
PAPER_BASE_TOOLS = {
    "workspace_read", "workspace_write", "workspace_list", "workspace_remove",
    "memory_read", "memory_write", "memory_list", "memory_search",
}
FORBIDDEN_TOOLS = {
    "mc_execute", "mc_submit", "mc_wait", "mc_status", "mc_cancel", "mc_pending",
    "mc_jobs", "mc_job_cancel", "events_read", "history_read", "artifact_read",
}
# Framework/harness vocabulary that must not reach the paper-visible surface.
HARNESS_TERMS = (
    "artifact", "evidence", "events_read", "history_read", "artifact_read",
    "mc_execute", "mc_submit", "mc_wait", "mc_status", "mc_cancel", "mc_pending",
    "mc_jobs", "mc_job_cancel", "ledger", "epoch", "harness", "supervisor",
    "leftover", "runtime reset", "deterministic evaluator", "context_compacted",
    "runtime_epoch", "world_epoch",
)


def _names(schemas):
    return {schema["function"]["name"] for schema in schemas}


def _paper_tools(**options):
    return tool_schemas(paper_profile=True, **options)


class PaperToolSurfaceTests(unittest.TestCase):
    def test_exact_paper_tool_name_set(self):
        names = _names(_paper_tools(shell_enabled=True, web_enabled=True))
        self.assertEqual(names, set(PAPER_TOOL_NAMES))
        self.assertEqual(names, PAPER_BASE_TOOLS | {"shell", "web_search", "web_fetch"})

    def test_conditional_paper_tools_follow_feature_flags(self):
        cases = {
            (False, False): PAPER_BASE_TOOLS,
            (True, False): PAPER_BASE_TOOLS | {"shell"},
            (False, True): PAPER_BASE_TOOLS | {"web_search", "web_fetch"},
            (True, True): PAPER_BASE_TOOLS | {"shell", "web_search", "web_fetch"},
        }
        for (shell_enabled, web_enabled), expected in cases.items():
            with self.subTest(shell=shell_enabled, web=web_enabled):
                self.assertEqual(_names(_paper_tools(
                    shell_enabled=shell_enabled, web_enabled=web_enabled)), expected)

    def test_no_game_or_framework_tools_in_paper_profile(self):
        for shell_enabled in (False, True):
            for web_enabled in (False, True):
                names = _names(_paper_tools(
                    shell_enabled=shell_enabled, web_enabled=web_enabled))
                self.assertFalse(names & FORBIDDEN_TOOLS)

    def test_paper_profile_ignores_legacy_flags(self):
        # Even if legacy paper_* flags and background/web/shell flags are passed,
        # paper_profile is the single source of truth for the model-visible set.
        names = _names(tool_schemas(
            shell_enabled=True, background_enabled=True, web_enabled=True,
            paper_single_program=True, leftover_policy="allow",
            paper_tight_interface=True, paper_shell_cli=True, paper_profile=True))
        self.assertEqual(names, set(PAPER_TOOL_NAMES))

    def test_memory_write_schema_keeps_cas_and_hides_harness_fields(self):
        tool = next(schema for schema in _paper_tools() if schema["function"]["name"] == "memory_write")
        properties = tool["function"]["parameters"]["properties"]
        self.assertEqual(set(properties), {"scope", "path", "content", "expected_revision"})
        self.assertIn("expected_revision", tool["function"]["parameters"]["required"])
        self.assertEqual(properties["scope"]["enum"], ["private", "team"])
        encoded = json.dumps(tool)
        self.assertNotIn("evidence_refs", encoded)
        self.assertNotIn("world_epoch", encoded)

    def test_memory_search_schema(self):
        tool = next(schema for schema in _paper_tools() if schema["function"]["name"] == "memory_search")
        parameters = tool["function"]["parameters"]
        self.assertEqual(set(parameters["properties"]), {"query", "scope", "author", "limit"})
        self.assertEqual(parameters["required"], ["query"])
        self.assertEqual(parameters["properties"]["scope"]["enum"], ["all", "private", "team"])
        description = tool["function"]["description"].lower()
        self.assertIn("ranked", description)
        self.assertIn("snippet", description)

    def test_paper_tool_descriptions_have_no_harness_terms(self):
        for schema in _paper_tools(shell_enabled=True, web_enabled=True):
            encoded = json.dumps(schema).lower()
            for term in HARNESS_TERMS:
                with self.subTest(tool=schema["function"]["name"], term=term):
                    self.assertNotIn(term, encoded)
            self.assertNotIn("quota", encoded)

    def test_non_paper_tool_behavior_is_preserved(self):
        default = _names(tool_schemas())
        self.assertTrue({
            "workspace_read", "workspace_write", "workspace_list", "workspace_remove",
            "mc_execute", "mc_status", "mc_cancel", "mc_pending",
            "memory_read", "memory_write", "memory_list",
            "events_read", "history_read", "artifact_read",
        } <= default)
        # paper_profile defaults to False and keeps the legacy surface unchanged.
        self.assertEqual(default, _names(tool_schemas(paper_profile=False)))
        self.assertNotIn("memory_search", default)

    def test_legacy_paper_flags_still_change_non_paper_surface(self):
        single = _names(tool_schemas(paper_single_program=True))
        for hidden in ("mc_status", "mc_cancel", "mc_pending", "events_read"):
            self.assertNotIn(hidden, single)
        tight = _names(tool_schemas(paper_single_program=True, paper_tight_interface=True))
        for hidden in ("history_read", "artifact_read"):
            self.assertNotIn(hidden, tight)
        shell_cli = _names(tool_schemas(paper_single_program=True, paper_shell_cli=True))
        self.assertNotIn("mc_execute", shell_cli)
        background = _names(tool_schemas(background_enabled=True))
        self.assertTrue({"mc_submit", "mc_wait", "mc_jobs", "mc_job_cancel"} <= background)
        web = _names(tool_schemas(web_enabled=True))
        self.assertTrue({"web_search", "web_fetch"} <= web)
        shell = _names(tool_schemas(shell_enabled=True))
        self.assertIn("shell", shell)


class PaperPlayerPromptTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "profile": "paper_aligned",
            "lifecycle": "paper_aligned",
            "goal": GOAL,
            "peers": ["auratian", "gyldenia", "bulicame"],
            "agent_id": "janesick",
        }
        self.prompt = player_prompt(self.config)
        self.lowered = self.prompt.lower()

    def test_prompt_starts_with_the_objective(self):
        self.assertTrue(self.prompt.startswith(GOAL))
        self.assertIn("defeat the ender dragon", self.lowered)

    def test_prompt_has_paper_general_instructions(self):
        for phrase in (
            "observe the environment before acting",
            "verify the result of each action afterwards",
            "validate a new tool on a short task",
            "build and refine your own tools",
            "record mechanics, strategies and workarounds in memory",
            "work in progress and your next steps in memory",
            "survive context compaction",
            "keep working while background jobs run",
            "honest about your actions and outcomes",
            "rather than exploit flaws of the environment or of its evaluation",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.lowered)

    def test_prompt_has_paper_environment_description(self):
        for phrase in (
            "command-line client",
            "mc exec --file <path>",
            "asynchronous function that receives the bot",
            "pathfinding plugin loaded and no helpers added",
            "built-in modules of node.js",
            "installed mineflayer packages",
            "cannot start processes or write files",
            "global object persist between programs",
            "timeout that you choose (60 seconds by default)",
            "prints what the program logged and returned",
            "chat reaches every bot",
            "appears in the output of each player's next program",
            "server records chat, deaths and advancements",
            "keeps every program with its output",
            "saves the world every five minutes",
            "restarts from the latest save",
            "version control",
            "shell",
            "file editing",
            "web search",
            "private part and a part shared by the team",
            "documentation",
            "/reference",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.lowered)

    def test_prompt_has_a_brief_teammate_note(self):
        for peer in self.config["peers"]:
            self.assertIn(peer, self.prompt)
        self.assertIn("teammates", self.lowered)
        self.assertIn("players decide how to share the work", self.lowered)

    def test_prompt_hides_harness_details_version_and_agent_id(self):
        for term in HARNESS_TERMS + ("quota", "timeout reset", "reconnect"):
            with self.subTest(term=term):
                self.assertNotIn(term, self.lowered)
        self.assertNotIn("prompt version", self.lowered)
        self.assertNotIn(PROMPT_VERSION, self.prompt)
        self.assertNotIn(self.config["agent_id"], self.prompt)

    def test_paper_prompt_is_used_for_paper_profile_only(self):
        self.assertEqual(player_prompt(self.config), paper_player_prompt(self.config))
        strict_config = {
            "agent_id": "legacy", "goal": "Observe", "peers": ["other"],
            "lifecycle": "strict",
        }
        strict = player_prompt(strict_config)
        self.assertNotEqual(strict, self.prompt)
        self.assertIn(f"Prompt version: {STRICT_PROMPT_VERSION}", strict)
        self.assertIn("Agent: legacy", strict)
        self.assertIn("Use mc_execute for synchronous execution", strict)
        self.assertIn("unfinished async work forces a runtime reset", strict)


class PaperObserverPromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = observer_prompt({"profile": "paper_aligned", "agent_id": "observer"})
        self.lowered = self.prompt.lower()

    def test_observer_prompt_has_paper_role_categories(self):
        for phrase in (
            "independent witness",
            "never plays",
            "check the players' claims against the record",
            "specification gaming",
            "fabricated results",
            "leave the game unchanged",
            "intervene only on failures of the environment",
            "broken tool",
            "quota ceiling",
            "never on the players' strategy",
            "summary of progress",
            "log of verified events",
            "findings about the infrastructure",
            "never join the game or run a program",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.lowered)

    def test_observer_prompt_has_general_instructions(self):
        for phrase in (
            "observe the environment before acting",
            "verify the result of each action afterwards",
            "build and refine your own tools",
            "record mechanics, strategies and workarounds in memory",
            "honest about your actions and outcomes",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.lowered)

    def test_observer_prompt_hides_harness_details(self):
        for term in HARNESS_TERMS:
            if term == "evidence":
                # §2.3 says "verified events"; the word "evidence" is harness jargon.
                pass
            with self.subTest(term=term):
                self.assertNotIn(term, self.lowered)
        self.assertNotIn("evidence", self.lowered)
        self.assertNotIn("observer_report", self.lowered)
        self.assertNotIn("infra_issue", self.lowered)
        self.assertNotIn("prompt version", self.lowered)
        self.assertNotIn(PROMPT_VERSION, self.prompt)

    def test_observer_prompt_is_used_for_paper_profile_only(self):
        self.assertEqual(
            observer_prompt({"profile": "paper_aligned"}),
            paper_observer_prompt({"profile": "paper_aligned"}))
        strict = observer_prompt({"agent_id": "observer"})
        self.assertIn(f"Prompt version: {STRICT_PROMPT_VERSION}", strict)
        self.assertIn("Independent experiment observer", strict)
        self.assertIn("specification gaming", strict)
        self.assertIn("experiment records and evidence", strict)


class PromptVersionTests(unittest.TestCase):
    def test_prompt_versions(self):
        self.assertEqual(PROMPT_VERSION, "paper-framework-v4")
        self.assertEqual(STRICT_PROMPT_VERSION, "paper-framework-v3")
        self.assertNotIn(PROMPT_VERSION, player_prompt({
            "profile": "paper_aligned", "goal": GOAL, "peers": [], "agent_id": "a"}))
        self.assertNotIn(PROMPT_VERSION, observer_prompt({"profile": "paper_aligned"}))


if __name__ == "__main__":
    unittest.main()
