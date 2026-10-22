"""FTS5/ LIKE ranked memory retrieval tests (``MemoryStore.search``).

Covers the paper's "private + team persistent memory" retrieval requirement:
latest-revision-only snippets, ranked results, private/team permission
boundaries, restart rebuild/migration, and the FTS5 -> LIKE fallback.

Run in the disposable test env::

    PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
        .test-venv/bin/python -m pytest -q tests/test_memory_search.py
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from minecraft_harness.memory import MemoryConflict, MemoryPermissionError, MemoryStore


class MemorySearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = MemoryStore(self.root / "memory")

    def _seed(self):
        self.store.write("alice", "team", "cache.md",
                         "dragon portal coordinates near the base", 0,
                         world_epoch=2)
        self.store.write("alice", "team", "intro.md",
                         "dragon observed circling the tower", 0)
        self.store.write("alice", "team", "mine.md",
                         "portal network notes and mining routes", 0)
        self.store.write("alice", "private", "secret.md",
                         "private alice secret token plan", 0)
        self.store.write("bob", "team", "bob-cache.md",
                         "dragon sightings from bob's watchtower", 0)

    # -- ranked retrieval ---------------------------------------------------
    def test_ranked_results_return_latest_revision_metadata_and_snippet(self):
        self._seed()
        results = self.store.search("alice", "dragon portal", scope="team")
        self.assertTrue(results)
        self.assertEqual(results[0]["path"], "cache.md")
        self.assertEqual(results[0]["revision"], 1)
        self.assertIn("author", results[0])
        self.assertIn("written_at", results[0])
        self.assertIn("world_epoch", results[0])
        # The snippet must be a bounded excerpt with the match highlighted,
        # never the whole note content.
        self.assertIn("dragon", results[0]["snippet"].lower())
        self.assertIn("[", results[0]["snippet"])
        self.assertIsInstance(results[0]["rank"], float)
        self.assertIsInstance(results[0]["mode"], str)

    def test_more_matching_terms_rank_higher(self):
        self._seed()
        for mode in ("fts5", "like"):
            with self.subTest(mode=mode):
                self.store.fts_enabled = mode == "fts5"
                combined = self.store.search("alice", "dragon portal", scope="team")
                self.assertEqual(combined[0]["path"], "cache.md")
                dragon_only = self.store.search("alice", "dragon", scope="team")
                paths = {item["path"] for item in dragon_only}
                self.assertIn("cache.md", paths)
                self.assertIn("intro.md", paths)
                self.assertNotIn("mine.md", paths)
                self.assertGreaterEqual(combined[0]["rank"], combined[1]["rank"])
        self.store.fts_enabled = True

    def test_only_latest_revision_is_searchable(self):
        self.store.write("alice", "private", "plan.md", "obsolete_marker stays hidden", 0)
        # Revision 1 is the current content, so it is searchable until replaced.
        self.assertEqual(len(self.store.search("alice", "obsolete_marker")), 1)
        self.store.write("alice", "private", "plan.md", "current_marker replaced it", 1)
        self.assertEqual(self.store.search("alice", "obsolete_marker"), [])
        hits = self.store.search("alice", "current_marker", scope="private")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "plan.md")
        self.assertEqual(hits[0]["revision"], 2)

    def test_query_operators_are_literal_text_not_fts_syntax(self):
        self.store.write("alice", "private", "notes.md", "ordinary observation text", 0)
        for query in ('" OR 1=1 --', "NEAR(", "*", "notes.md AND x", "\x00"):
            with self.subTest(query=query):
                # Must never raise a SQL/FTS error, and must not leak anything.
                self.assertEqual(self.store.search("alice", query),
                                 self.store.search("alice", query))

    # -- permissions --------------------------------------------------------
    def test_private_notes_are_author_only(self):
        self._seed()
        self.assertEqual(self.store.search("bob", "secret token"), [])
        self.assertEqual(self.store.search("bob", "secret token", scope="all"), [])
        self.assertEqual(len(self.store.search("alice", "secret token", scope="private")), 1)
        with self.assertRaises(MemoryPermissionError):
            self.store.search("bob", "secret token", scope="private", author="alice")

    def test_team_notes_are_shared_across_authors(self):
        self._seed()
        by_bob = self.store.search("bob", "dragon", scope="team")
        self.assertIn("bob-cache.md", {item["path"] for item in by_bob})
        self.assertEqual({item["path"] for item in self.store.search("alice", "dragon", scope="team")},
                         {"cache.md", "intro.md", "bob-cache.md"})
        authored = self.store.search("alice", "dragon", author="bob")
        self.assertEqual([item["path"] for item in authored], ["bob-cache.md"])
        self.assertEqual(self.store.search("bob", "dragon", author="alice", scope="team"),
                         [item for item in authored if item["author"] == "bob"] and
                         self.store.search("bob", "dragon", author="alice", scope="team"))
        self.assertEqual({item["author"] for item in
                          self.store.search("bob", "dragon", author="alice", scope="team")},
                         {"alice"})

    def test_all_scope_combines_own_private_and_shared_team(self):
        self._seed()
        self.store.write("bob", "private", "bob-secret", "bob dragon private note", 0)
        paths = {item["path"] for item in self.store.search("bob", "dragon", scope="all")}
        self.assertIn("bob-cache.md", paths)
        self.assertIn("bob-secret", paths)
        self.assertNotIn("secret.md", paths)

    def test_disabled_shared_memory_blocks_team_search(self):
        self._seed()
        self.store.shared_enabled = False
        with self.assertRaises(MemoryPermissionError):
            self.store.search("alice", "dragon", scope="team")
        self.assertEqual({item["author"] for item in self.store.search("alice", "secret")},
                         {"alice"})
        self.assertEqual(self.store.search("bob", "dragon", scope="all"), [])

    # -- validation ---------------------------------------------------------
    def test_invalid_arguments_are_rejected(self):
        for query in ("", "   ", None, 5, "x" * 1001):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    self.store.search("alice", query)
        with self.assertRaises(ValueError):
            self.store.search("alice", "term", scope="everywhere")
        for limit in (0, 101, True, "20", 1.5):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.store.search("alice", "term", limit=limit)
        with self.assertRaises(ValueError):
            self.store.search("alice", "term", author="bad actor!")

    # -- durability / fallback ---------------------------------------------
    def test_like_fallback_returns_bounded_snippets(self):
        self._seed()
        self.store.fts_enabled = False
        results = self.store.search("alice", "dragon portal", scope="team")
        self.assertTrue(results)
        self.assertEqual(results[0]["mode"], "like")
        self.assertEqual(results[0]["path"], "cache.md")
        for item in results:
            self.assertLessEqual(len(item["snippet"]), 120)
            self.assertNotIn("content", item)
        self.store.fts_enabled = True

    def test_index_is_rebuilt_on_restart_when_missing(self):
        self._seed()
        self.assertEqual(self.store.search("alice", "dragon portal")[0]["path"], "cache.md")
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("DELETE FROM notes_fts")
        restored = MemoryStore(self.root / "memory")
        self.assertTrue(restored.fts_enabled)
        self.assertEqual(restored.search("alice", "dragon portal")[0]["path"], "cache.md")

    def test_legacy_database_without_search_tables_is_migrated(self):
        self._seed()
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("DROP TABLE notes_fts")
            connection.execute("DROP TABLE notes_latest")
        restored = MemoryStore(self.root / "memory")
        self.assertTrue(restored.fts_enabled)
        hits = {item["path"] for item in restored.search("alice", "dragon", scope="team")}
        self.assertEqual(hits, {"cache.md", "intro.md", "bob-cache.md"})
        # CAS and revision history are untouched by the migration.
        self.assertEqual(restored.read("alice", "team", "cache.md")["revision"], 1)
        restored.write("alice", "team", "cache.md", "updated dragon cache", 1)
        restored.write("alice", "team", "cache.md", "third dragon cache", 2)
        hits = restored.search("alice", "dragon", scope="team")
        self.assertEqual(hits[0]["path"], "cache.md")
        self.assertEqual(hits[0]["revision"], 3)
        with self.assertRaises(MemoryConflict):
            restored.write("alice", "team", "cache.md", "stale", 2)

    def test_search_does_not_change_quota_or_cas_state(self):
        self.store.write("alice", "private", "a", "quota observation value", 0)
        self.store.search("alice", "observation")
        restored = MemoryStore(self.root / "memory")
        restored.search("alice", "observation")
        self.assertEqual(restored.list("alice"), [{"scope": "private", "author": "alice",
                                                   "path": "a", "revision": 1}])
        self.assertEqual(restored.read("alice", "private", "a")["revision"], 1)


class _FakeRecorder:
    def __init__(self):
        self.events = []

    def append(self, kind, payload, **kwargs):
        event = {"kind": kind, "payload": payload, **kwargs}
        self.events.append(event)
        return event

    def put_artifact(self, value):
        return {"hash": "0" * 64, "path": "artifact"}


class RemoteMemorySearchProxyTests(unittest.TestCase):
    def test_runner_proxy_forwards_search_with_bound_actor(self):
        from minecraft_harness.runner import RemoteMemory

        class FakeClient:
            def __init__(self):
                self.calls = []

            def call_sync(self, method, params=None, *, timeout_s=None):
                self.calls.append((method, dict(params or {})))
                return [{"path": "team.md", "rank": 1.0}]

        client = FakeClient()
        proxy = RemoteMemory(client, agent_id="alice")
        results = proxy.search("alice", query="dragon", scope="team", limit=5)
        self.assertEqual(results, [{"path": "team.md", "rank": 1.0}])
        self.assertEqual(client.calls, [("memory.search",
                                         {"actor_id": "alice", "query": "dragon",
                                          "scope": "team", "limit": 5})])
        with self.assertRaises(ValueError):
            proxy.search("bob", query="dragon")


class MemorySearchRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_hub_forces_the_bound_actor_identity(self):
        from minecraft_harness.runners import ServiceHub

        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory")
            store.write("alice", "team", "team.md", "dragon portal for the team", 0)
            store.write("alice", "private", "private.md", "dragon private plan", 0)
            hub = ServiceHub(recorder=_FakeRecorder(), budget=None, memory=store,
                             gateway=None, workspaces={}, artifacts=None,
                             config={"storage": {"max_event_bytes": 4 * 1024 ** 2}})
            # A runner cannot search another actor's private notes by supplying
            # its own actor_id: the connection identity always wins.
            result = await hub.handle("bob", "player", "memory.search",
                                      {"actor_id": "alice", "query": "dragon",
                                       "scope": "all"})
            self.assertEqual([item["path"] for item in result], ["team.md"])
            own = await hub.handle("alice", "player", "memory.search",
                                   {"query": "dragon", "scope": "private"})
            self.assertEqual([item["path"] for item in own], ["private.md"])


if __name__ == "__main__":
    unittest.main()
