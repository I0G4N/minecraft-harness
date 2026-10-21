"""Unit tests for the read-only paper-figure builder."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from minecraft_harness.figures import build_figures


def _uuid(name):
    return str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + name).encode()).digest(), version=3))


class FigureBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name)
        (self.run / "artifacts").mkdir()
        self.start = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def add_artifact(self, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (self.run / "artifacts" / digest).write_bytes(raw)
        return digest

    def stamp(self, seconds=0):
        return datetime.fromtimestamp(self.start.timestamp() + seconds, timezone.utc).isoformat()

    def make_ledger(self, events):
        (self.run / "manifest.json").write_text(json.dumps({"run_id": "run-test", "config": {
            "agents": {"ids": ["player1"]},
            "experiment": {"name": "figure run", "task": "kill_dragon", "profile": "paper_aligned"}}}))
        database = sqlite3.connect(self.run / "events.sqlite3")
        database.executescript("""
            CREATE TABLE events(event_seq INTEGER PRIMARY KEY, kind TEXT, actor_id TEXT, source TEXT,
                received_at TEXT, world_epoch INTEGER, runtime_epoch INTEGER, execution_id TEXT,
                model_call_id TEXT, payload_json TEXT);
            CREATE TABLE state(key TEXT PRIMARY KEY, value_json TEXT);
        """)
        for event in events:
            database.execute("INSERT INTO events(event_seq, kind, actor_id, source, received_at, payload_json) VALUES (?,?,?,?,?,?)",
                             (event["event_seq"], event["kind"], event.get("actor_id"), event.get("source"),
                              event.get("received_at") or self.stamp(), json.dumps(event.get("payload") or {})))
        database.execute("INSERT INTO state VALUES (?,?)", ("status", json.dumps("running")))
        database.commit()
        database.close()

    def test_builds_program_chat_death_stage_and_behavior_series(self):
        player = _uuid("player1")
        stats = self.add_artifact({"stats": {"minecraft:crafted": {"minecraft:bucket": 1}}})
        advancements = self.add_artifact({"minecraft:story/enter_the_nether": {"done": True,
            "criteria": {"minecraft:nether": self.stamp(4)}}})
        snapshot = self.add_artifact({"kind": "world_snapshot", "scope": "explicit_players", "player_ids": [player],
            "files": {"stats/" + player + ".json": stats, "advancements/" + player + ".json": advancements}})
        segment = self.add_artifact({"kind": "server_log_segment", "complete": True, "events": [
            {"kind": "advancement", "actor_id": "player1", "title": "Stone Age", "source_time": self.stamp(2),
             "raw_message": "player1 has made the advancement [Stone Age]", "source": {"line": 10}},
            {"kind": "chat", "actor_id": "player1", "message": "help at 10, 64, 20",
             "classification": {"labels": ["warning"]}, "source_time": self.stamp(5), "source": {"line": 11}},
            {"kind": "death", "actor_id": "player1", "cause": "combat", "raw_message": "player1 was slain by Zombie",
             "source_time": self.stamp(6), "source": {"line": 12}},
        ]})
        self.make_ledger([
            {"event_seq": 1, "kind": "execution_accepted", "actor_id": "player1", "received_at": self.stamp(0),
             "payload": {"record": {"execution_id": "e1", "agent_id": "player1", "accepted_at": self.stamp(0),
                                    "started_at": self.stamp(0)}}},
            {"event_seq": 2, "kind": "execution_finished", "actor_id": "player1", "received_at": self.stamp(3),
             "payload": {"record": {"execution_id": "e1", "agent_id": "player1", "status": "succeeded",
                                    "started_at": self.stamp(0), "finished_at": self.stamp(3)}}},
            {"event_seq": 3, "kind": "server_log_segment", "actor_id": "server_collector", "received_at": self.stamp(6),
             "payload": {"artifact_sha256": segment, "evidence_id": "ev-3"}},
            {"event_seq": 4, "kind": "server_world_snapshot", "actor_id": "server_collector", "received_at": self.stamp(7),
             "payload": {"artifact_sha256": snapshot, "evidence_id": "ev-4"}},
        ])
        figures = build_figures(self.run)
        self.assertEqual(figures["run"]["id"], "run-test")
        self.assertEqual(figures["run"]["status"], "running")
        self.assertTrue(figures["run"]["running"])
        self.assertAlmostEqual(figures["run"]["duration_s"], 7.0, places=2)
        self.assertEqual(len(figures["programs"]), 1)
        self.assertEqual(figures["programs"][0]["agent"], "player1")
        self.assertEqual(len(figures["chats"]), 1)
        self.assertEqual(len(figures["deaths"]), 1)
        self.assertEqual(figures["behavior"]["paper"].get("rescues"), 1)
        self.assertEqual(figures["behavior"]["paper"].get("coordinates"), 1)
        self.assertEqual(figures["death_causes"]["causes"].get("Monsters"), 1)
        stages = {stage["key"]: stage for stage in figures["stages"]}
        self.assertEqual(stages["stone_tools"]["state"], "reached")
        self.assertEqual(stages["stone_tools"]["precision"], "source_time")
        self.assertEqual(stages["nether_portal"]["state"], "reached")
        self.assertEqual(stages["nether_portal"]["agent"], "player1")
        self.assertEqual(stages["bucket"]["state"], "reached")
        self.assertEqual(stages["ten_crystals"]["state"], "unavailable")
        bands = {band["key"] for band in figures["dimension_bands"]}
        self.assertIn("nether", bands)
        self.assertEqual(figures["chats"][0]["evidence"]["event_seq"], 3)
        self.assertTrue(figures["restarts"] == [])

    def test_ten_crystals_uses_player_observation_and_paper_stage_order(self):
        self.make_ledger([
            {"event_seq": 1, "kind": "execution_accepted", "execution_id": "job-crystal",
             "payload": {"status": "accepted"}},
            {"event_seq": 2, "kind": "program_log", "actor_id": "player1",
             "payload": {"level": "log", "message": json.dumps({"crystals": 10})}},
            {"event_seq": 3, "kind": "program_log", "actor_id": "player1",
             "payload": {"level": "log", "message": json.dumps({"crystals": 0})}},
        ])
        figures = build_figures(self.run)
        stages = {stage["key"]: stage for stage in figures["stages"]}
        self.assertEqual(stages["ten_crystals"]["state"], "reached")
        self.assertEqual(stages["ten_crystals"]["precision"], "player_program_observation")
        order = [stage["key"] for stage in figures["stages"]]
        self.assertLess(order.index("ender_pearls"), order.index("blaze_rods"))

    def test_dragon_observation_channel_is_parsed_but_agent_reported(self):
        self.make_ledger([
            {"event_seq": 1, "kind": "execution_accepted", "execution_id": "job-dragon",
             "payload": {"status": "accepted"}},
            {"event_seq": 2, "kind": "program_log", "actor_id": "player1",
             "payload": {"level": "log", "message": json.dumps({"dragon_health": 200, "crystals": 10})}},
            {"event_seq": 3, "kind": "program_log", "actor_id": "player1",
             "payload": {"level": "log", "message": json.dumps({"dragon_health": 120, "crystals": 4})}},
        ])
        figures = build_figures(self.run)
        self.assertTrue(figures["dragon"]["available"])
        self.assertEqual(figures["dragon"]["health"][-1]["hp"], 120)
        self.assertEqual(figures["dragon"]["crystals"][-1]["count"], 4)
        self.assertIn("not server truth", figures["dragon"]["basis"])

    def test_missing_ledger_is_rejected(self):
        with self.assertRaises(ValueError):
            build_figures(self.run)

    def test_series_are_bounded(self):
        player = _uuid("player1")
        segment = self.add_artifact({"kind": "server_log_segment", "complete": True, "events": [
            {"kind": "chat", "actor_id": "player1", "message": "message " + str(index), "source_time": self.stamp(index),
             "classification": {"labels": []}, "source": {"line": index}} for index in range(5)]})
        self.make_ledger([{"event_seq": 1, "kind": "execution_accepted", "actor_id": "player1", "received_at": self.stamp(0),
                           "payload": {"record": {"execution_id": "e1", "agent_id": "player1", "started_at": self.stamp(0)}}},
                          {"event_seq": 2, "kind": "server_log_segment", "actor_id": "server_collector", "received_at": self.stamp(5),
                           "payload": {"artifact_sha256": segment}}])
        figures = build_figures(self.run, max_series=2)
        self.assertEqual(len(figures["chats"]), 2)
        self.assertEqual(figures["omitted"]["chats"], 3)


if __name__ == "__main__":
    unittest.main()
