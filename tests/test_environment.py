import gzip
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from minecraft_harness.config import validate_config
from minecraft_harness.environment import inspect_environment, provision_world, read_level


def text(value):
    raw = value.encode()
    return struct.pack('>H', len(raw)) + raw


def nbt_compound(fields):
    result = b''
    for name, value in fields.items():
        if isinstance(value, dict):
            kind, body = 10, nbt_compound(value)
        elif isinstance(value, str):
            kind, body = 8, text(value)
        elif name in {'Difficulty', 'hardcore', 'allowCommands'}:
            kind, body = 1, struct.pack('>b', value)
        elif name == 'seed':
            kind, body = 4, struct.pack('>q', value)
        else:
            kind, body = 3, struct.pack('>i', value)
        result += bytes([kind]) + text(name) + body
    return result + b'\0'


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        provision_world(self.data, seed=42)
        (self.data / 'world').mkdir()
        self.document = {'Version': {'Name': '1.20.4'}, 'WorldGenSettings': {'seed': 42},
                         'GameType': 0, 'Difficulty': 1, 'hardcore': 0, 'allowCommands': 0}
        self.write_world()
        self.config = validate_config({'experiment': {'profile': 'paper_aligned'},
            'runtime': {'mode': 'minecraft', 'lifecycle': 'paper_aligned', 'url': 'http://127.0.0.1:3001'},
            'server': {'world_dir': str(self.data / 'world'), 'require_fresh_world': True},
            'workspace': {'provision': True}, 'observer': {'mode': 'llm'}})

    def write_world(self):
        raw = bytes([10]) + text('') + nbt_compound({'Data': self.document})
        (self.data / 'world' / 'level.dat').write_bytes(gzip.compress(raw))

    def inspect(self, run_id='run1', resume=False):
        return inspect_environment(self.config, run_id=run_id, resume=resume)

    def test_actual_world_is_bound_and_resume_requires_same_run(self):
        result = self.inspect()
        self.assertTrue(result['verified'])
        self.assertEqual(result['actual']['seed'], 42)
        self.assertEqual(result['deviations'], ['fixed_seed'])
        self.assertEqual(self.inspect(resume=True)['world_id'], result['world_id'])
        with self.assertRaisesRegex(ValueError, 'another experiment'):
            self.inspect('run2')
        with self.assertRaisesRegex(ValueError, 'reuse'):
            self.inspect()

    def test_config_seed_and_actual_rules_cannot_diverge(self):
        self.document['WorldGenSettings']['seed'] = 43
        self.write_world()
        with self.assertRaisesRegex(ValueError, 'seed'):
            self.inspect()
        self.document['WorldGenSettings']['seed'] = 42
        self.document['allowCommands'] = 1
        self.write_world()
        with self.assertRaisesRegex(ValueError, 'cheats'):
            self.inspect()
        self.assertIsNone(json.loads((self.data / '.harness-world.json').read_text())['run_id'])

    def test_existing_player_progress_and_unprovisioned_world_are_rejected(self):
        directory = self.data / 'world' / 'stats'
        directory.mkdir()
        (directory / 'old.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'player progress'):
            self.inspect()
        (self.data / '.harness-world.json').unlink()
        with self.assertRaisesRegex(ValueError, 'origin marker'):
            self.inspect()

    def test_random_provision_does_not_accept_eula_or_start_server(self):
        directory = self.root / 'random'
        first = provision_world(directory)
        self.assertEqual(first['origin'], 'new_random')
        self.assertIs(type(first['seed']), int)
        self.assertFalse((directory / 'eula.txt').exists())
        self.assertFalse((directory / 'world').exists())
        self.assertIn(f"level-seed={first['seed']}", (directory / 'server.properties').read_text())
        with self.assertRaises(FileExistsError):
            provision_world(directory)

    def test_corrupt_nbt_and_symlinks_are_refused(self):
        path = self.data / 'world' / 'level.dat'
        path.write_bytes(gzip.compress(b'\x0a\x00\x00\x0a\x00\x04Data\x07\x00\x01x\x7f\xff\xff\xff'))
        with self.assertRaises(ValueError):
            read_level(path)
        path.unlink()
        path.symlink_to(self.data / 'server.properties')
        with self.assertRaises(ValueError):
            read_level(path)

    def test_profiles_and_legacy_semantics(self):
        self.assertEqual(validate_config({})['experiment']['profile'], 'strict')
        with self.assertRaisesRegex(ValueError, 'four players'):
            validate_config({'experiment': {'profile': 'paper_aligned'}, 'agents': {'count': 1}})
        with self.assertRaisesRegex(ValueError, '300'):
            validate_config({'experiment': {'profile': 'paper_aligned'}, 'runtime': {'lifecycle': 'paper_aligned'}, 'server': {'save_interval_s': 60}})

    def test_malformed_or_symlinked_origin_markers_fail_without_binding(self):
        marker = self.data / '.harness-world.json'
        marker.write_text('[]')
        with self.assertRaisesRegex(ValueError, 'Malformed'):
            self.inspect()
        marker.unlink()
        marker.symlink_to(self.data / 'server.properties')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.inspect()

    def test_empty_symlinked_player_progress_is_never_treated_as_fresh(self):
        outside = self.root / 'outside-progress'
        outside.mkdir()
        (self.data / 'world' / 'stats').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinked player progress'):
            self.inspect()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires POSIX FIFO')
    def test_fifo_environment_inputs_fail_immediately_instead_of_blocking(self):
        from minecraft_harness.environment import read_properties
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            read_level(fifo)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            read_properties(fifo)

    def test_actual_gamerules_and_nonsecret_server_properties_are_declared(self):
        self.document['GameRules'] = {'keepInventory': 'false', 'doDaylightCycle': 'true', 'randomTickSpeed': '3'}
        self.write_world()
        with (self.data / 'server.properties').open('a') as stream:
            stream.write('\npvp=true\nview-distance=10\nrcon.password=private-rcon-key\nresource-pack=https://asset.invalid/?token=secret\n')
        result = self.inspect()
        self.assertEqual(result['actual']['game_rules'], self.document['GameRules'])
        self.assertEqual(result['server_properties']['pvp'], 'true')
        self.assertEqual(result['server_properties']['view-distance'], '10')
        self.assertNotIn('rcon.password', result['server_properties'])
        self.assertNotIn('resource-pack', result['server_properties'])
        self.assertNotIn('private-rcon-key', json.dumps(result))
        self.assertNotIn('token=secret', json.dumps(result))
