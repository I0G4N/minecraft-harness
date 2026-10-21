import gc
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from minecraft_harness.memory import MemoryConflict, MemoryPermissionError, MemoryStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MemoryStore(Path(self.directory.name))

    def test_permissions_revisions_and_persistence(self):
        first = self.store.write("alice", "team", "plan.md", "first", 0, world_epoch=3)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(self.store.read("bob", "team", "plan.md", author="alice")["content"], "first")
        with self.assertRaises(MemoryPermissionError):
            self.store.write("bob", "team", "plan.md", "overwrite", 1, author="alice")
        self.store.write("alice", "private", "secret", "private", 0)
        with self.assertRaises(MemoryPermissionError):
            self.store.read("bob", "private", "secret", author="alice")
        self.store.write("alice", "team", "plan.md", "second", 1)
        restored = MemoryStore(Path(self.directory.name))
        self.assertEqual(restored.read("alice", "team", "plan.md", revision=1)["world_epoch"], 3)
        self.assertEqual(restored.read("alice", "team", "plan.md")["revision"], 2)
        with self.assertRaises(MemoryConflict):
            restored.write("alice", "team", "plan.md", "stale", 1)

    def test_concurrent_cas_has_one_winner(self):
        def write(value):
            try:
                self.store.write("alice", "team", "race", value, 0)
                return True
            except MemoryConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(write, ["a", "b"]))
        self.assertEqual(sum(result), 1)

    def test_disabled_sharing_and_invalid_paths(self):
        self.store.shared_enabled = False
        with self.assertRaises(MemoryPermissionError):
            self.store.list("alice", "team")
        for path in ("../bob", "/absolute", "a/../b", ""):
            with self.assertRaises(ValueError):
                self.store.write("alice", "private", path, "x", 0)

    def test_bounded_note_pages_and_persistent_author_quotas(self):
        store = MemoryStore(Path(self.directory.name), max_author_bytes=2000, max_author_notes=2)
        store.write("alice", "private", "a", "a" * 100, 0)
        store.write("alice", "private", "b", "b" * 100, 0)
        with self.assertRaisesRegex(ValueError, "note quota"):
            store.write("alice", "private", "c", "c", 0)
        first = store.list("alice", limit=1)
        second = store.list("alice", offset=1, limit=1)
        self.assertEqual([first[0]["path"], second[0]["path"]], ["a", "b"])
        self.assertEqual(store.read("alice", "private", "a", offset=10, limit=20)["next_offset"], 30)
        restored = MemoryStore(Path(self.directory.name), max_author_bytes=2000, max_author_notes=2)
        restored.write("alice", "private", "a", "small", 1)
        with self.assertRaisesRegex(ValueError, "storage quota"):
            restored.write("alice", "private", "a", "small", 2)
        self.assertEqual(restored.read("alice", "private", "a")["revision"], 2)

    def test_concurrent_new_notes_cannot_race_author_quota(self):
        store = MemoryStore(Path(self.directory.name), max_author_notes=1)
        def write(path):
            try:
                store.write("alice", "private", path, "value", 0)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            successes = list(pool.map(write, [str(index) for index in range(8)]))
        self.assertEqual(sum(successes), 1)

    def test_evidence_and_paths_are_bounded_before_storage(self):
        for references in (["x"] * 101, ["x" * 32001], {"invalid": "type"}):
            with self.subTest(references=type(references)):
                with self.assertRaises(ValueError):
                    self.store.write("alice", "private", "a", "value", 0, evidence_refs=references)
        with self.assertRaises(ValueError):
            self.store.write("alice", "private", "a" * 513, "value", 0)

    def test_every_memory_operation_closes_connection_without_garbage_collection(self):
        opened = []
        connect = sqlite3.connect

        def tracked(*args, **kwargs):
            connection = connect(*args, **kwargs)
            opened.append(connection)  # Strong references prevent destructor cleanup.
            return connection

        was_enabled = gc.isenabled()
        gc.disable()
        try:
            with patch("minecraft_harness.memory.store.sqlite3.connect", side_effect=tracked):
                for index in range(10):
                    self.store.write("alice", "private", f"note-{index}", "value", 0)
                    self.store.read("alice", "private", f"note-{index}")
                    self.store.list("alice")
                self.store.close()
            self.assertEqual(len(opened), 31)
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
        finally:
            if was_enabled:
                gc.enable()
