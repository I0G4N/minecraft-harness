import json
from pathlib import Path
import tempfile
import unittest

from minecraft_harness.evaluation import ServerEvidenceCollector, evaluate
from minecraft_harness.recorder import Recorder


PLAYER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.world = self.root / "world"
        self.world.mkdir()
        self.recorder = Recorder(self.run)
        self.addCleanup(self.recorder.close)
        self.collector = ServerEvidenceCollector(self.run, self.recorder)

    def save(self, category, document, player=PLAYER):
        directory = self.world / category
        directory.mkdir(exist_ok=True)
        (directory / f"{player}.json").write_text(json.dumps(document))

    def score(self, task):
        return evaluate(self.run, task, list(self.recorder.all_events()))

    def test_unregistered_server_source_and_chat_cannot_claim_success(self):
        self.recorder.append("server_advancement", {"name": "minecraft:end/kill_dragon", "done": True}, source="server")
        self.recorder.append("program_succeeded", {"inventory": {"oak_log": 64}}, source="agent_claim")
        log = self.root / "latest.log"
        log.write_text("[12:00:00] [Server thread/INFO]: <player1> player1 has made the advancement [Free the End]\n")
        self.collector.collect_log(log)
        self.assertEqual(self.score("kill_dragon")["status"], "insufficient_evidence")
        self.assertIsNone(self.score("observe_only")["capability_success"])
        self.assertNotEqual(self.score("observe_only")["status"], "diagnostic_pass")

    def test_log_pickup_delta_is_success_and_reproducible(self):
        self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": 3}}})
        self.collector.collect_world(self.world, baseline=True)
        self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": 5}}})
        self.collector.collect_world(self.world)
        result = self.score({"name": "obtain_logs", "min_count": 2})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["criteria"][0]["observed_delta"], 2)
        self.assertEqual(len(result["evidence_refs"]), 2)
        self.assertEqual(result, self.score({"name": "obtain_logs", "min_count": 2}))
        self.assertEqual(result, evaluate(self.run, {"name": "obtain_logs", "min_count": 2}))

    def test_recipe_unlock_and_mining_do_not_prove_crafting_or_pickup(self):
        self.collector.collect_world(self.world, baseline=True)
        self.save("advancements", {"minecraft:recipes/decorations/crafting_table": {"done": True}})
        self.save("stats", {"stats": {"minecraft:mined": {"minecraft:oak_log": 10}}})
        self.collector.collect_world(self.world)
        self.assertEqual(self.score("craft_table")["status"], "not_succeeded")
        self.assertEqual(self.score("obtain_logs")["status"], "not_succeeded")
        self.save("stats", {"stats": {"minecraft:crafted": {"minecraft:crafting_table": 1}}})
        self.collector.collect_world(self.world)
        self.assertEqual(self.score("craft_table")["status"], "succeeded")

    def test_old_advancement_is_not_new_progress(self):
        self.save("advancements", {"minecraft:end/kill_dragon": {"done": True}})
        self.collector.collect_world(self.world, baseline=True)
        self.collector.collect_world(self.world)
        self.assertEqual(self.score("kill_dragon")["status"], "not_succeeded")

    def test_new_iron_and_dragon_advancements_are_verified(self):
        self.collector.collect_world(self.world, baseline=True)
        self.save("advancements", {"minecraft:end/kill_dragon": {"done": True}, "minecraft:story/smelt_iron": {"done": True}})
        self.collector.collect_world(self.world)
        log = self.root / "latest.log"
        log.write_text("[12:00:00] [Server thread/INFO]: player1 has made the advancement [Free the End]\n")
        self.collector.collect_log(log)
        self.assertEqual(self.score("kill_dragon")["status"], "succeeded")
        self.assertEqual(self.score("obtain_iron_ingot")["status"], "succeeded")
        self.assertEqual(self.score({"name": "obtain_iron_ingot", "min_count": 2})["status"], "not_succeeded")

    def test_tampered_artifact_is_rejected(self):
        self.collector.collect_world(self.world, baseline=True)
        self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": 1}}})
        event = self.collector.collect_world(self.world)
        digest = event["payload"]["artifact_sha256"]
        (self.run / "artifacts" / digest).write_text('{}')
        result = self.score("obtain_logs")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertTrue(result["warnings"])

    def test_epoch_change_invalidates_rolled_back_success(self):
        self.collector.collect_world(self.world, baseline=True)
        self.save("stats", {"stats": {"minecraft:crafted": {"minecraft:crafting_table": 1}}})
        self.collector.collect_world(self.world)
        self.recorder.append("world_restored", {}, world_epoch=1)
        result = self.score("craft_table")
        self.assertEqual(result["world_epoch"], 1)
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_other_players_cannot_gain_zero_baseline_credit(self):
        self.save("stats", {"stats": {"minecraft:crafted": {"minecraft:crafting_table": 20}}}, OTHER)
        self.collector.collect_world(self.world, baseline=True, player_ids=[PLAYER])
        self.collector.collect_world(self.world)
        result = self.score({"name": "craft_table", "player_ids": [OTHER]})
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_unsupported_criteria_are_explicitly_rejected(self):
        for task in ("build_house", {"name": "craft_table", "inventory_count": 10}, None):
            self.assertEqual(self.score(task)["status"], "unsupported")

    def test_no_baseline_and_malformed_save_are_not_certified(self):
        self.save("stats", {"stats": {"minecraft:crafted": {"minecraft:crafting_table": 20}}})
        self.collector.collect_world(self.world)
        self.assertEqual(self.score("craft_table")["status"], "insufficient_evidence")
        (self.world / "stats" / f"{PLAYER}.json").write_text('{')
        with self.assertRaises(ValueError):
            self.collector.collect_world(self.world)



class LongRunEvaluationTests(unittest.TestCase):
    setUp = EvaluationTests.setUp
    save = EvaluationTests.save
    score = EvaluationTests.score

    def test_restart_baseline_preserves_prior_progress_and_accumulation(self):
        from minecraft_harness.evaluation import IncrementalEvaluator
        self.collector.collect_world(self.world, baseline=True)
        scorer = IncrementalEvaluator(self.run, {"name": "obtain_logs", "min_count": 3})
        for epoch, amount in ((0, 1), (1, 2), (2, 3)):
            if epoch:
                self.recorder.append("world_restored", {"rollback": False, "source_world_epoch": epoch - 1}, world_epoch=epoch)
                self.collector.collect_world(self.world, world_epoch=epoch, baseline=True)
            self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": amount}}})
            self.collector.collect_world(self.world, world_epoch=epoch)
            actual = scorer.evaluate()
            self.assertEqual(actual, self.score({"name": "obtain_logs", "min_count": 3}))
        self.assertEqual(actual["status"], "succeeded")
        self.assertEqual(actual["criteria"][0]["observed_delta"], 3)
        self.assertLessEqual(len(scorer.rows), 4)
        self.assertLessEqual(len(scorer.cache), 8)

    def test_rollback_truncates_abandoned_success_but_preserves_checkpoint_progress(self):
        from minecraft_harness.evaluation import IncrementalEvaluator
        self.collector.collect_world(self.world, baseline=True)
        self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": 2}}})
        checkpoint = self.collector.collect_world(self.world)
        scorer = IncrementalEvaluator(self.run, {"name": "obtain_logs", "min_count": 3})
        self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": 3}}})
        self.collector.collect_world(self.world)
        self.assertEqual(scorer.evaluate()["status"], "succeeded")
        self.recorder.append("world_restored", {"rollback": True, "source_world_epoch": 0,
                             "restore_point_event_seq": checkpoint["event_seq"]}, world_epoch=1)
        self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": 2}}})
        self.collector.collect_world(self.world, world_epoch=1, baseline=True)
        result = scorer.evaluate()
        self.assertEqual(result, self.score({"name": "obtain_logs", "min_count": 3}))
        self.assertEqual(result["status"], "not_succeeded")
        self.assertEqual(result["criteria"][0]["observed_delta"], 2)
        self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": 3}}})
        restored = self.collector.collect_world(self.world, world_epoch=1)
        result = scorer.evaluate()
        self.assertEqual(result["first_success_event_seq"], restored["event_seq"])
        self.assertEqual(result, self.score({"name": "obtain_logs", "min_count": 3}))

    def test_same_epoch_baseline_does_not_clear_success(self):
        self.collector.collect_world(self.world, baseline=True)
        self.save("stats", {"stats": {"minecraft:crafted": {"minecraft:crafting_table": 1}}})
        completed = self.collector.collect_world(self.world)
        self.collector.collect_world(self.world, baseline=True)
        result = self.score("craft_table")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["first_success_event_seq"], completed["event_seq"])

    def test_live_scoring_does_not_reread_old_artifacts(self):
        from unittest.mock import patch
        from minecraft_harness.evaluation import IncrementalEvaluator
        from minecraft_harness.evaluation import scoring
        self.collector.collect_world(self.world, baseline=True)
        scorer = IncrementalEvaluator(self.run, {"name": "obtain_logs", "min_count": 1000})
        for amount in range(1, 60):
            self.save("stats", {"stats": {"minecraft:picked_up": {"minecraft:oak_log": amount}}})
            self.collector.collect_world(self.world)
            with patch.object(scoring, "_load_snapshot", wraps=scoring._load_snapshot) as loader:
                scorer.evaluate()
                self.assertLessEqual(loader.call_count, 2)
            self.assertLessEqual(len(scorer.rows), 4)
        with patch.object(scoring, "_load_snapshot", side_effect=AssertionError("unchanged evidence was loaded")):
            self.recorder.append("model_usage", {"usage": {"input_tokens": 10}})
            self.assertEqual(scorer.evaluate()["criteria"][0]["observed_delta"], 59)

    def test_collector_skips_unchanged_world_and_log(self):
        self.collector.collect_world(self.world, baseline=True)
        self.assertIsNotNone(self.collector.collect_world(self.world))
        self.assertIsNone(self.collector.collect_world(self.world))
        log = self.root / "latest.log"
        log.write_text('[12:00:00] [Server thread/INFO]: <player1> hi\n')
        self.assertIsNotNone(self.collector.collect_log(log))
        self.assertIsNone(self.collector.collect_log(log))

    def test_log_replay_after_crash_does_not_duplicate_a_segment(self):
        from unittest.mock import patch
        from minecraft_harness.observer import build_report
        log = self.root / "latest.log"
        log.write_text('[12:00:00] [Server thread/INFO]: <player1> first\n')
        original = self.collector._record
        def crash_after_publish(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash before cursor commit")
        with patch.object(self.collector, "_record", side_effect=crash_after_publish):
            with self.assertRaisesRegex(RuntimeError, "cursor commit"):
                self.collector.collect_log(log)
        with log.open("a") as stream:
            stream.write('[12:00:01] [Server thread/INFO]: <player1> second\n')
        restarted = ServerEvidenceCollector(self.run, self.recorder)
        restarted.collect_log(log)
        report = build_report(self.run)
        self.assertEqual(report["metrics"]["chat_message_count"], 2)
        self.assertEqual(report["events_by_kind"]["server_log_segment"], 2)

    def test_rotation_and_partial_log_lines_are_counted_once(self):
        from minecraft_harness.observer import build_report
        log = self.root / "latest.log"
        log.write_text('[12:00:00] [Server thread/INFO]: <player1> first\npartial')
        self.collector.collect_log(log)
        self.assertIsNone(self.collector.collect_log(log))
        log.rename(self.root / "old.log")
        log.write_text('[12:00:01] [Server thread/INFO]: <player1> second\n')
        self.collector.collect_log(log, world_epoch=1)
        log.write_text('[12:00:02] [Server thread/INFO]: <player1> third\n')
        self.collector.collect_log(log, world_epoch=1)
        self.assertEqual(build_report(self.run)["metrics"]["chat_message_count"], 3)

    def test_restart_reconciles_world_evidence_committed_before_registry_link(self):
        import sqlite3
        self.collector.collect_world(self.world, baseline=True)
        self.save("stats", {"stats": {"minecraft:crafted": {"minecraft:crafting_table": 1}}})
        event = self.collector.collect_world(self.world)
        with sqlite3.connect(self.collector.registry) as connection:
            connection.execute("UPDATE evidence SET event_seq=NULL WHERE evidence_id=?", (event["payload"]["evidence_id"],))
        self.assertNotEqual(self.score("craft_table")["status"], "succeeded")
        ServerEvidenceCollector(self.run, self.recorder)
        self.assertEqual(self.score("craft_table")["status"], "succeeded")




class SQLiteConnectionLifetimeTests(unittest.TestCase):
    def test_collect_scoring_and_reporting_close_connections_with_gc_disabled(self):
        import gc
        import sqlite3
        from unittest.mock import patch
        from minecraft_harness.evaluation import IncrementalEvaluator
        from minecraft_harness.observer import build_report
        from minecraft_harness.cli import run_status
        opened=[]
        class TrackedConnection(sqlite3.Connection):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                self.explicitly_closed=False
            def close(self):
                super().close()
                self.explicitly_closed=True
        connect=sqlite3.connect
        def tracked(*args,**kwargs):
            kwargs['factory']=TrackedConnection
            connection=connect(*args,**kwargs)
            opened.append(connection)  # Strong refs ensure GC cannot hide leaks.
            return connection
        enabled=gc.isenabled()
        gc.disable()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                world=root/'world'
                world.mkdir()
                recorder=Recorder(root/'run')
                try:
                    with patch('sqlite3.connect',side_effect=tracked):
                        collector=ServerEvidenceCollector(root/'run',recorder)
                        collector.collect_world(world,baseline=True)
                        scorer=IncrementalEvaluator(root/'run','craft_table')
                        for _ in range(30):
                            collector.collect_world(world)
                            scorer.evaluate()
                            build_report(root/'run')
                            run_status(root/'run')
                            self.assertTrue(all(connection.explicitly_closed for connection in opened))
                finally:
                    recorder.close()
        finally:
            if enabled:
                gc.enable()
        self.assertGreater(len(opened),200)


if __name__ == "__main__":
    unittest.main()
