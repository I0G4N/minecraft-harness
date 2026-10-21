import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minecraft_harness.world_checkpoints import create_checkpoint, restore_checkpoint


class WorldCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = self.root / "server-data"
        self.data.mkdir()
        (self.data / "world" / "region").mkdir(parents=True)
        (self.data / "world" / "empty-dimension").mkdir()
        (self.data / "world" / "level.dat").write_bytes(b"initial-world")
        (self.data / "world" / "region" / "r.0.0.mca").write_bytes(b"initial-region")
        (self.data / "server.properties").write_text("difficulty=easy\n")
        self.checkpoints = self.root / "checkpoints"

    def checkpoint(self, **metadata):
        return create_checkpoint(self.data, self.checkpoints, metadata)

    def contents(self):
        return {path.relative_to(self.data).as_posix(): path.read_bytes()
                for path in self.data.rglob("*") if path.is_file()}

    def test_restore_preserves_root_inode_empty_directories_and_epoch_metadata(self):
        saved = self.checkpoint(world_epoch=3, runtime_epochs={"alice": 8},
                                evidence_refs=["event:42"], restored_checkpoint_id="older")
        root_inode = self.data.stat().st_ino
        (self.data / "world" / "level.dat").write_bytes(b"later-world")
        (self.data / "later-file").write_text("later")
        result = restore_checkpoint(saved["checkpoint_dir"], self.data, self.root / "backup-current")
        self.assertEqual(self.data.stat().st_ino, root_inode)
        self.assertEqual((self.data / "world" / "level.dat").read_bytes(), b"initial-world")
        self.assertFalse((self.data / "later-file").exists())
        self.assertTrue((self.data / "world" / "empty-dimension").is_dir())
        self.assertEqual(result["metadata"], saved["metadata"])
        self.assertEqual(result["metadata"]["world_epoch"], 3)
        self.assertIn("world/region/r.0.0.mca", result["files"])

    def test_pre_restore_backup_can_restore_the_previous_world(self):
        saved = self.checkpoint(world_epoch=1)
        (self.data / "world" / "level.dat").write_bytes(b"world-before-rollback")
        (self.data / "new-player-state").write_bytes(b"inventory")
        before = self.contents()
        backup = self.root / "backup-before-first-restore"
        restored = restore_checkpoint(saved["checkpoint_dir"], self.data, backup)
        from minecraft_harness.world_checkpoints import acknowledge_restore_receipt
        acknowledge_restore_receipt(self.data, restored["restore_id"])
        self.assertNotEqual(self.contents(), before)
        rollback = restore_checkpoint(backup, self.data, self.root / "backup-before-undo")
        self.assertEqual(self.contents(), before)
        self.assertEqual(rollback["metadata"]["kind"], "pre_restore_backup")

    def test_tampered_snapshot_is_rejected_without_touching_data(self):
        saved = self.checkpoint(world_epoch=1)
        (self.data / "world" / "level.dat").write_bytes(b"keep-current")
        before = self.contents()
        (Path(saved["checkpoint_dir"]) / "data" / "world" / "level.dat").write_bytes(b"tampered")
        backup = self.root / "should-not-be-created"
        with self.assertRaises(ValueError):
            restore_checkpoint(saved["checkpoint_dir"], self.data, backup)
        self.assertEqual(self.contents(), before)
        self.assertFalse(backup.exists())

    def test_manifest_paths_cannot_escape_the_checkpoint(self):
        saved = self.checkpoint()
        checkpoint = Path(saved["checkpoint_dir"])
        manifest_path = checkpoint / "manifest.json"
        original = json.loads(manifest_path.read_text())
        outside = self.root / "outside-file"
        outside.write_text("must-remain")
        before = self.contents()
        for relative in ("../outside-file", "/tmp/absolute", "world/../../outside-file", "world//level.dat"):
            with self.subTest(path=relative):
                changed = dict(original)
                changed["files"] = {relative: {"sha256": "0" * 64, "size": 1}}
                manifest_path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    restore_checkpoint(checkpoint, self.data, self.root / "unused-backup")
                self.assertEqual(self.contents(), before)
                self.assertEqual(outside.read_text(), "must-remain")

    def test_symlinks_in_source_checkpoint_and_destination_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("must-remain")
        (self.data / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.checkpoint()
        (self.data / "escape").unlink()
        saved = self.checkpoint()
        checkpoint = Path(saved["checkpoint_dir"])
        (checkpoint / "data" / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            restore_checkpoint(checkpoint, self.data, self.root / "unused-backup-1")
        (checkpoint / "data" / "escape").unlink()
        (self.data / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            restore_checkpoint(checkpoint, self.data, self.root / "unused-backup-2")
        self.assertEqual((outside / "secret").read_text(), "must-remain")

    def test_symlink_manifest_and_parent_directory_are_rejected(self):
        saved = self.checkpoint()
        checkpoint = Path(saved["checkpoint_dir"])
        manifest = checkpoint / "manifest.json"
        external_manifest = self.root / "external-manifest.json"
        manifest.rename(external_manifest)
        manifest.symlink_to(external_manifest)
        with self.assertRaises(ValueError):
            restore_checkpoint(checkpoint, self.data, self.root / "backup")
        (self.root / "linked-checkpoints").symlink_to(self.checkpoints, target_is_directory=True)
        with self.assertRaises(ValueError):
            create_checkpoint(self.data, self.root / "linked-checkpoints" / "nested", {})

    def test_fifo_is_rejected_before_snapshot_or_restore(self):
        os.mkfifo(self.data / "blocking-pipe")
        with self.assertRaises(ValueError):
            self.checkpoint()
        (self.data / "blocking-pipe").unlink()
        saved = self.checkpoint()
        os.mkfifo(Path(saved["checkpoint_dir"]) / "data" / "blocking-pipe")
        with self.assertRaises(ValueError):
            restore_checkpoint(saved["checkpoint_dir"], self.data, self.root / "backup")

    def test_directory_nesting_and_existing_backup_are_rejected(self):
        with self.assertRaises(ValueError):
            create_checkpoint(self.data, self.data / "snapshots", {})
        with self.assertRaises(ValueError):
            create_checkpoint(self.data, self.root, {})
        saved = self.checkpoint()
        for backup in (self.data / "backup", self.root, Path(saved["checkpoint_dir"]) / "backup"):
            with self.subTest(backup=backup):
                with self.assertRaises(ValueError):
                    restore_checkpoint(saved["checkpoint_dir"], self.data, backup)
        existing = self.root / "existing-backup"
        existing.mkdir()
        (existing / "valuable").write_text("do-not-delete")
        with self.assertRaises(FileExistsError):
            restore_checkpoint(saved["checkpoint_dir"], self.data, existing)
        self.assertEqual((existing / "valuable").read_text(), "do-not-delete")

    def test_unmarked_directories_are_not_restore_sources(self):
        fake = self.root / "unmarked"
        fake.mkdir()
        (fake / "data").mkdir()
        (fake / "manifest.json").write_text(json.dumps({"files": {}, "directories": [], "schema_version": 1}))
        before = self.contents()
        with self.assertRaises(ValueError):
            restore_checkpoint(fake, self.data, self.root / "backup")
        self.assertEqual(self.contents(), before)

    def test_copy_failure_automatically_restores_complete_pre_restore_backup(self):
        saved = self.checkpoint()
        (self.data / "world" / "level.dat").write_bytes(b"current-world")
        (self.data / "current-only").write_text("keep-this-too")
        before = self.contents()
        backup = self.root / "recovery-backup"
        import minecraft_harness.world_checkpoints as module
        original_copy = module.shutil.copy2
        injected = False

        def fail_once(source, destination, **kwargs):
            nonlocal injected
            if Path(source).is_relative_to(Path(saved["checkpoint_dir"]) / "data") and not injected:
                injected = True
                raise OSError("injected restore copy failure")
            return original_copy(source, destination, **kwargs)

        with patch("minecraft_harness.world_checkpoints.shutil.copy2", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "original data was restored"):
                restore_checkpoint(saved["checkpoint_dir"], self.data, backup)
        self.assertTrue(injected)
        self.assertEqual(self.contents(), before)
        self.assertTrue((backup / "manifest.json").is_file())


class WorldCrashRecoveryTests(unittest.TestCase):
    setUp = WorldCheckpointTests.setUp
    checkpoint = WorldCheckpointTests.checkpoint
    contents = WorldCheckpointTests.contents

    def test_atomic_publication_does_not_expose_a_partial_checkpoint(self):
        import minecraft_harness.world_checkpoints as module
        original = module._copy_tree
        def inspect_during_copy(source, destination, files, directories):
            self.assertTrue(Path(destination).parent.name.startswith('.pending-'))
            self.assertEqual(list(self.checkpoints.glob('checkpoint-*')), [])
            return original(source, destination, files, directories)
        with patch.object(module, '_copy_tree', side_effect=inspect_during_copy):
            saved = self.checkpoint()
        self.assertTrue((Path(saved['checkpoint_dir']) / 'manifest.json').is_file())
        self.assertEqual(list(self.checkpoints.glob('.pending-*')), [])

    def test_low_disk_fails_before_publishing_snapshot_or_touching_world(self):
        from collections import namedtuple
        usage = namedtuple('usage', 'total used free')(1000, 995, 5)
        before = self.contents()
        with patch('minecraft_harness.world_checkpoints.shutil.disk_usage', return_value=usage):
            with self.assertRaisesRegex(OSError, 'Insufficient free disk'):
                self.checkpoint()
        self.assertEqual(before, self.contents())
        self.assertFalse(self.checkpoints.exists())

    def test_process_death_mid_restore_recovers_verified_original_world(self):
        import subprocess
        import sys
        from minecraft_harness.world_checkpoints import has_interrupted_restore, recover_pending_restore
        saved = self.checkpoint()
        (self.data / 'world' / 'level.dat').write_bytes(b'valuable-current-world')
        (self.data / 'current-only').write_text('must survive recovery')
        before = self.contents()
        backup = self.root / 'crash-backup'
        program = '''
import os,sys
from pathlib import Path
import minecraft_harness.world_checkpoints as module
original=module._restore_contents
def die_during_copy(checkpoint,data,manifest):
    module._empty_data_root(data)
    (data/'partial').write_text('interrupted')
    os._exit(42)
module._restore_contents=die_during_copy
module.restore_checkpoint(*sys.argv[1:])
'''
        child = subprocess.run([sys.executable, '-c', program, saved['checkpoint_dir'], str(self.data), str(backup)], capture_output=True)
        self.assertEqual(child.returncode, 42, child.stderr.decode())
        self.assertTrue(has_interrupted_restore(self.data))
        self.assertNotEqual(self.contents(), before)
        recovered = recover_pending_restore(self.data)
        self.assertEqual(recovered['backup_dir'], str(backup))
        self.assertEqual(self.contents(), before)
        self.assertFalse(has_interrupted_restore(self.data))
        self.assertIsNone(recover_pending_restore(self.data))

    def test_invalid_recovery_backup_keeps_journal_and_partial_world_for_operator(self):
        import minecraft_harness.world_checkpoints as module
        saved = self.checkpoint()
        backup = self.root / 'saved-backup'
        # Simulate a power loss after a durable backup/journal, before commit.
        module._build(self.data, backup, {'kind': 'pre_restore_backup'}, 'backup-test')
        journal = module._journal_path(self.data)
        module._write_journal(journal, {'marker': module.MARKER, 'schema_version': 1,
            'data_dir': str(self.data), 'checkpoint_dir': saved['checkpoint_dir'], 'backup_dir': str(backup)})
        (backup / 'data/world/level.dat').write_bytes(b'corrupt')
        before = self.contents()
        with self.assertRaises(ValueError):
            module.recover_pending_restore(self.data)
        self.assertTrue(journal.exists())
        self.assertEqual(before, self.contents())


class RestoreReceiptTests(unittest.TestCase):
    setUp = WorldCheckpointTests.setUp
    checkpoint = WorldCheckpointTests.checkpoint
    contents = WorldCheckpointTests.contents

    def test_foreign_or_missing_lineage_checkpoint_is_rejected_before_world_changes(self):
        for index, metadata in enumerate(({}, {'run_id':'another-run','world_epoch':0,'restore_point_event_seq':1},
            {'run_id':'expected-run','world_epoch':0},
            {'run_id':'expected-run','world_epoch':False,'restore_point_event_seq':1},
            {'run_id':'expected-run','world_epoch':0,'restore_point_event_seq':-1})):
            with self.subTest(metadata=metadata):
                saved=self.checkpoint(**metadata)
                (self.data/'world/level.dat').write_bytes(b'valuable-current-world')
                before=self.contents()
                backup=self.root/f'forbidden-backup-{index}'
                with self.assertRaises(ValueError):
                    restore_checkpoint(saved['checkpoint_dir'],self.data,backup,expected_run_id='expected-run')
                self.assertEqual(before,self.contents())
                self.assertFalse(backup.exists())

    def test_completion_receipt_requires_matching_ledger_ack_before_next_restore(self):
        from minecraft_harness.world_checkpoints import read_pending_restore_receipt, acknowledge_restore_receipt
        saved = self.checkpoint(world_epoch=3,restore_point_event_seq=42,run_id='test-run')
        (self.data/'world/level.dat').write_bytes(b'later world')
        restored = restore_checkpoint(saved['checkpoint_dir'],self.data,self.root/'before-restore')
        receipt = read_pending_restore_receipt(self.data)
        self.assertEqual(receipt['restore_id'],restored['restore_id'])
        self.assertEqual(receipt['metadata']['restore_point_event_seq'],42)
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            acknowledge_restore_receipt(self.data,'0'*32)
        with self.assertRaisesRegex(RuntimeError,'previous world restore receipt'):
            restore_checkpoint(saved['checkpoint_dir'],self.data,self.root/'should-not-exist')
        with self.assertRaisesRegex(RuntimeError,'Pending restore receipt'):
            self.checkpoint()
        acknowledge_restore_receipt(self.data,restored['restore_id'])
        self.assertIsNone(read_pending_restore_receipt(self.data))

    def _die_at_journal_clear(self, after):
        import subprocess,sys
        saved = self.checkpoint(world_epoch=0,restore_point_event_seq=17,run_id='run-fixture')
        (self.data/'world/level.dat').write_bytes(b'world before attempted rollback')
        before = self.contents()
        program = '''
import os,sys
from pathlib import Path
import minecraft_harness.world_checkpoints as module
original=module._clear_journal
def crash(path):
    if path == module._journal_path(Path(sys.argv[2])):
        if sys.argv[4] == 'after': original(path)
        os._exit(42)
    return original(path)
module._clear_journal=crash
module.restore_checkpoint(*sys.argv[1:4])
'''
        child=subprocess.run([sys.executable,'-c',program,saved['checkpoint_dir'],str(self.data),str(self.root/'crash-backup'),'after' if after else 'before'],capture_output=True)
        self.assertEqual(child.returncode,42,child.stderr.decode())
        return before

    def test_crash_after_receipt_before_journal_clear_aborts_restore_on_recovery(self):
        from minecraft_harness.world_checkpoints import has_interrupted_restore,recover_pending_restore,read_pending_restore_receipt
        before=self._die_at_journal_clear(False)
        self.assertTrue(has_interrupted_restore(self.data))
        with self.assertRaisesRegex(RuntimeError,'journal must be recovered'):
            read_pending_restore_receipt(self.data)
        recover_pending_restore(self.data)
        self.assertEqual(before,self.contents())
        self.assertIsNone(read_pending_restore_receipt(self.data))

    def test_crash_after_journal_clear_retains_committed_receipt_for_lineage_repair(self):
        from minecraft_harness.world_checkpoints import has_interrupted_restore,recover_pending_restore,read_pending_restore_receipt,acknowledge_restore_receipt
        before=self._die_at_journal_clear(True)
        self.assertFalse(has_interrupted_restore(self.data))
        self.assertIsNone(recover_pending_restore(self.data))
        self.assertNotEqual(before,self.contents())
        receipt=read_pending_restore_receipt(self.data)
        self.assertEqual(receipt['metadata']['restore_point_event_seq'],17)
        self.assertEqual((self.data/'world/level.dat').read_bytes(),b'initial-world')
        acknowledge_restore_receipt(self.data,receipt['restore_id'])
        self.assertIsNone(read_pending_restore_receipt(self.data))
