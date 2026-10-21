"""Freeze mechanics tested with tiny synthetic files, never an hour/model/server."""
import json
import os
from pathlib import Path
import runpy
import shutil
import tempfile
import unittest


runner=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/frozen_soak.py'),run_name='frozen_soak_test_module')


def writable(root):
    for base,dirs,files in os.walk(root):
        Path(base).chmod(0o755)
        for name in files:
            path=Path(base)/name
            if not path.is_symlink():path.chmod(0o644)


class FrozenSoakTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.source=self.root/'source';self.snapshot=self.root/'snapshot'
        for relative in ['src/minecraft_harness/__init__.py','src/minecraft_harness/supervisor.py',
                         'src/minecraft_harness/execution/__init__.py','src/minecraft_harness/execution/gateway.py',
                         'src/minecraft_harness/observer/__init__.py','src/minecraft_harness/observer/analysis.py',
                         'src/minecraft_harness/evaluation/__init__.py','src/minecraft_harness/evaluation/collector.py',
                         'scripts/longrun_soak.py','bot_runtime/src/server.js','bot_runtime/node_modules/mineflayer/index.js']:
            path=self.source/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('')
        for relative in runner['FILES']:
            path=self.source/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}')
        (self.source/'bot_runtime/package.json').write_text(json.dumps({'type':'module'}))
        (self.source/'bot_runtime/node_modules/mineflayer/package.json').write_text(json.dumps({'main':'index.js'}))
        self.manifest=runner['prepare'](self.source,self.snapshot)
        self.addCleanup(writable,self.root)

    def test_freeze_copies_private_readonly_dependencies_and_detects_live_changes(self):
        self.assertTrue((self.snapshot/'bot_runtime/node_modules').is_symlink())
        target=(self.snapshot/'bot_runtime/node_modules').resolve()
        self.assertTrue(target.is_relative_to(self.snapshot))
        self.assertFalse((target/'mineflayer/index.js').stat().st_mode&0o222)
        self.assertTrue(all(runner['verify'](self.snapshot,self.source,self.manifest).values()))
        (self.source/'src/minecraft_harness/supervisor.py').write_text('changed=1')
        checks=runner['verify'](self.snapshot,self.source,self.manifest)
        self.assertFalse(checks['original_source_unchanged']);self.assertTrue(checks['snapshot_source_unchanged'])
        (self.source/'uv.lock').unlink()
        self.assertFalse(runner['verify'](self.snapshot,self.source,self.manifest)['original_source_unchanged'])

    @unittest.skipUnless(shutil.which('node'),'Node required for frozen resolution proof')
    def test_preflight_imports_only_copied_application_and_dependency_paths(self):
        paths=runner['preflight'](self.snapshot,self.source)
        self.assertTrue(paths['python']['all_application_imports_frozen'])
        self.assertFalse(paths['python']['sys_path_contains_live_source'])
        self.assertEqual(paths['python']['gateway_node_path'],str(self.snapshot/'bot_runtime/src/server.js'))
        self.assertTrue(Path(paths['node']['mineflayer']).is_relative_to(self.snapshot/'.dependencies/node_modules'))
