"""Frozen runner source snapshot: hash determinism, mounts, PYTHONPATH, tamper fail-closed."""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Keep the module importable from the repository root without an install.
_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from minecraft_harness.recorder import Recorder
from minecraft_harness.runners import (RunnerControlError, RunnerManager, ServiceHub)

TOKENS = {"token-alice": {"agent_id": "alice", "role": "player"}}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _expected_hashes(root):
    """Independent reference implementation of the frozen-tree hash map."""
    root = Path(root)
    hashes = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.relative_to(root).parts or path.suffix == ".pyc":
            continue
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _expected_sha256(hashes):
    canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _build_source_root(root):
    """Fixture repo root: live harness src plus live venv/bot_runtime support paths."""
    _write(root / "src" / "minecraft_harness" / "__init__.py", 'VERSION = "fixture"\n')
    _write(root / "src" / "minecraft_harness" / "runner_rpc.py", "RPC_LAYER = 1\n")
    _write(root / "src" / "minecraft_harness" / "agents" / "loop.py", "AGENT_LOOP = 1\n")
    _write(root / "src" / "minecraft_harness" / "static" / "app.js", "console.log(1)\n")
    _write(root / "src" / "top-level-fixture.txt", "top\n")
    # Bytecode caches must never change the recorded source hash.
    _write(root / "src" / "minecraft_harness" / "__pycache__" / "cached.cpython-311.pyc", "cache")
    _write(root / "src" / "stale.pyc", "stale")
    # Support paths that keep their live meaning for runners.
    (root / ".venv" / "lib" / "python3.11" / "site-packages").mkdir(parents=True, exist_ok=True)
    (root / "bot_runtime" / "src").mkdir(parents=True, exist_ok=True)
    return root


def _manager(source_root, run_dir, *, mode="process", runner_env=None, recorder=None,
             runtime=None):
    runtime_settings = {"mode": "fake", "runner_mode": mode}
    runtime_settings.update(runtime or {})
    config = {"runtime": runtime_settings}
    if mode == "container":
        config["runtime"].setdefault("runner_container_shared_dir",
                                     str(source_root / "shared"))
    specs = {"alice": {
        "schema_version": 1, "run_id": "run-fixture", "agent_id": "alice",
        "role": "player", "state_root": str(run_dir / "agents" / "alice"),
        "shell_enabled": False, "heartbeat_s": 10,
        "agent_config": {"agent_id": "alice"}, "model": {"kind": "fake"}}}
    return RunnerManager(
        run_dir=run_dir, config=config, specs=specs, token_map=TOKENS,
        service_handler=None,
        runner_env=runner_env if runner_env is not None else {"PATH": os.environ.get("PATH", "")},
        recorder=recorder, docker_bin="docker", container_source_root=source_root,
        container_python="/usr/bin/python3.11")


class SourceSnapshotTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = _build_source_root(self.root / "live-source")
        self.run_dir = self.root / "run"
        self.managers = []

    def manager(self, *, run_dir=None, **kwargs):
        manager = _manager(self.source_root, run_dir or self.run_dir, **kwargs)
        self.addCleanup(shutil.rmtree, manager.rpc_dir, True)
        self.managers.append(manager)
        return manager

    def snapshot_src(self, run_dir=None):
        return (run_dir or self.run_dir) / "source-snapshot" / "src"

    def manifest(self, run_dir=None):
        return json.loads(((run_dir or self.run_dir) / "source-snapshot.json").read_text())

    def test_freezes_expected_files_and_deterministic_hash(self):
        manager = self.manager()
        info = manager.freeze_source_snapshot()
        expected_hashes = _expected_hashes(self.source_root / "src")
        expected_sha256 = _expected_sha256(expected_hashes)
        self.assertTrue(info["created"])
        self.assertEqual(info["source_sha256"], expected_sha256)
        self.assertEqual(info["source_files"], len(expected_hashes))
        self.assertEqual(manager.source_sha256, expected_sha256)

        snapshot_src = self.snapshot_src()
        copied = {path.relative_to(snapshot_src).as_posix()
                  for path in snapshot_src.rglob("*") if path.is_file()}
        self.assertEqual(copied, set(expected_hashes))
        self.assertFalse([path for path in snapshot_src.rglob("*") if path.name == "__pycache__"])
        self.assertFalse(list(snapshot_src.rglob("*.pyc")))

        manifest = self.manifest()
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["run_id"], "run-fixture")
        self.assertEqual(manifest["source_sha256"], expected_sha256)
        self.assertEqual(manifest["source_hashes"], expected_hashes)
        self.assertIn("hash_policy", manifest)
        self.assertEqual(manager.snapshot()["source_sha256"], expected_sha256)

    def test_frozen_source_is_readable_by_capdrop_container_runner(self):
        # Container runners use --cap-drop=ALL (no CAP_DAC_OVERRIDE). A 0600
        # source file created by an agent must not make the frozen snapshot
        # unreadable inside the runner container.
        secret = self.source_root / "src" / "minecraft_harness" / "secret.py"
        secret.write_text("VALUE = 1\n", encoding="utf-8")
        secret.chmod(0o600)
        manager = self.manager()
        manager.freeze_source_snapshot()
        frozen = self.snapshot_src() / "minecraft_harness" / "secret.py"
        self.assertTrue(frozen.is_file())
        self.assertTrue(frozen.stat().st_mode & 0o444)
        self.assertEqual(frozen.stat().st_mode & 0o777, 0o644)

    def test_freezes_only_the_package_source_tree(self):
        self.manager().freeze_source_snapshot()
        snapshot_root = self.run_dir / "source-snapshot"
        self.assertEqual({path.name for path in snapshot_root.iterdir()}, {"src"})
        self.assertFalse((snapshot_root / "bot_runtime").exists())
        self.assertFalse((snapshot_root / ".venv").exists())
        self.assertTrue((snapshot_root / "src" / "top-level-fixture.txt").is_file())

    def test_hash_is_deterministic_across_independent_runs(self):
        first = self.manager().freeze_source_snapshot()
        second = self.manager(run_dir=self.root / "run-two").freeze_source_snapshot()
        self.assertEqual(first["source_sha256"], second["source_sha256"])
        # A byte change in the live tree must change the recorded hash.
        _write(self.source_root / "src" / "top-level-fixture.txt", "changed\n")
        third = self.manager(run_dir=self.root / "run-three").freeze_source_snapshot()
        self.assertNotEqual(third["source_sha256"], first["source_sha256"])

    def test_runner_arguments_mount_frozen_source_at_unchanged_destination(self):
        manager = self.manager(mode="container")
        manager.freeze_source_snapshot()
        spec_path = self.run_dir / "runners" / "alice.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("{}")
        state_dir = self.run_dir / "agents" / "alice"
        state_dir.mkdir(parents=True)
        command = " ".join(manager._container_args("alice", spec_path, state_dir))
        frozen = str(manager.source_snapshot_src)
        live = str(manager.container_source_root / "src")
        self.assertIn(f"src={frozen},dst={live},readonly", command)
        self.assertNotIn(f"src={live},dst={live}", command)
        environment = manager._container_environment("alice")
        # dst/PYTHONPATH inside the runner are unchanged even though the host
        # mount source now points at the frozen snapshot.
        self.assertEqual(environment["PYTHONPATH"].split(os.pathsep)[0], live)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")

    def test_runner_arguments_freeze_the_source_on_demand(self):
        # A direct _container_args() caller (unit test/operator tooling) gets the
        # frozen mount without having to call start() first.
        manager = self.manager(mode="container")
        spec_path = self.run_dir / "runners" / "alice.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("{}")
        state_dir = self.run_dir / "agents" / "alice"
        state_dir.mkdir(parents=True)
        command = " ".join(manager._container_args("alice", spec_path, state_dir))
        self.assertTrue((self.run_dir / "source-snapshot.json").is_file())
        self.assertEqual(len(manager.source_sha256), 64)
        self.assertIn(str(manager.source_snapshot_src), command)

    def test_runner_environment_forwards_rpc_timeout_to_runners(self):
        default_manager = self.manager(mode="container")
        default_manager.freeze_source_snapshot()
        self.assertEqual(default_manager._container_environment("alice")
                         ["HARNESS_RUNNER_RPC_TIMEOUT_S"], "120")
        manager = self.manager(mode="container",
                               runtime={"runner_rpc_timeout_s": 7})
        manager.freeze_source_snapshot()
        environment = manager._container_environment("alice")
        self.assertEqual(environment["HARNESS_RUNNER_RPC_TIMEOUT_S"], "7")
        spec_path = self.run_dir / "runners" / "alice.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("{}")
        state_dir = self.run_dir / "agents" / "alice"
        state_dir.mkdir(parents=True)
        command = " ".join(manager._container_args("alice", spec_path, state_dir))
        self.assertIn("HARNESS_RUNNER_RPC_TIMEOUT_S=7", command)

    def test_process_pythonpath_uses_frozen_source_and_keeps_support_paths(self):
        site_packages = str(self.source_root / ".venv" / "lib" / "python3.11" / "site-packages")
        bot_runtime = str(self.source_root / "bot_runtime")
        runner_env = {"PATH": os.environ.get("PATH", ""),
                      "PYTHONPATH": os.pathsep.join([str(self.source_root / "src"),
                                                     site_packages, bot_runtime])}
        manager = self.manager(runner_env=runner_env)
        manager.freeze_source_snapshot()
        environment = manager._process_environment("alice", self.run_dir / "runners" / "alice.json")
        entries = environment["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(entries, [str(manager.source_snapshot_src), site_packages, bot_runtime])
        self.assertEqual(environment["HARNESS_RUNNER_AGENT"], "alice")
        self.assertEqual(environment["HARNESS_RUNNER_RUN_ID"], "run-fixture")

    def test_live_source_edits_after_freeze_do_not_change_the_frozen_snapshot(self):
        manager = self.manager()
        first = manager.freeze_source_snapshot()
        _write(self.source_root / "src" / "minecraft_harness" / "__init__.py", 'VERSION = "edited"\n')
        _write(self.source_root / "src" / "minecraft_harness" / "injected.py", "INJECTED = 1\n")
        second = manager.freeze_source_snapshot()
        self.assertFalse(second["created"])
        self.assertEqual(second["source_sha256"], first["source_sha256"])
        self.assertTrue(second["live_source_drift"])
        self.assertNotEqual(second["live_source_sha256"], first["source_sha256"])
        snapshot_src = self.snapshot_src()
        self.assertNotIn("edited", (snapshot_src / "minecraft_harness" / "__init__.py").read_text())
        self.assertFalse((snapshot_src / "minecraft_harness" / "injected.py").exists())
        self.assertEqual(_expected_sha256(_expected_hashes(snapshot_src)), first["source_sha256"])

    def test_reused_run_verifies_the_existing_snapshot(self):
        first = self.manager().freeze_source_snapshot()
        _write(self.source_root / "src" / "minecraft_harness" / "__init__.py", 'VERSION = "edited"\n')
        resumed = self.manager()  # fresh manager on the same run dir = supervisor resume
        info = resumed.freeze_source_snapshot()
        self.assertFalse(info["created"])
        self.assertEqual(info["source_sha256"], first["source_sha256"])
        self.assertTrue(info["live_source_drift"])
        self.assertNotIn("edited", (self.snapshot_src() / "minecraft_harness" / "__init__.py").read_text())

    def test_tampered_snapshot_fails_closed(self):
        self.manager().freeze_source_snapshot()
        target = self.snapshot_src() / "minecraft_harness" / "runner_rpc.py"
        target.write_text("RPC_LAYER = 666\n")
        fresh = self.manager()
        with self.assertRaises(RunnerControlError) as raised:
            fresh.freeze_source_snapshot()
        self.assertIn("no longer matches", str(raised.exception))
        self.assertIsNone(fresh.source_sha256)

    def test_tampered_manifest_hash_fails_closed(self):
        self.manager().freeze_source_snapshot()
        manifest_path = self.run_dir / "source-snapshot.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source_sha256"] = ("0" if manifest["source_sha256"][0] != "0" else "1") + \
            manifest["source_sha256"][1:]
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(RunnerControlError):
            self.manager().freeze_source_snapshot()

    def test_missing_manifest_fails_closed(self):
        self.manager().freeze_source_snapshot()
        (self.run_dir / "source-snapshot.json").unlink()
        with self.assertRaises(RunnerControlError) as raised:
            self.manager().freeze_source_snapshot()
        self.assertIn("manifest is missing", str(raised.exception))

    def test_missing_snapshot_tree_fails_closed(self):
        self.manager().freeze_source_snapshot()
        shutil.rmtree(self.run_dir / "source-snapshot")
        with self.assertRaises(RunnerControlError) as raised:
            self.manager().freeze_source_snapshot()
        self.assertIn("incomplete", str(raised.exception))

    def test_missing_live_source_fails_closed(self):
        shutil.rmtree(self.source_root / "src")
        with self.assertRaises(RunnerControlError) as raised:
            self.manager().freeze_source_snapshot()
        self.assertIn("cannot freeze", str(raised.exception))

    def test_frozen_event_is_recorded(self):
        recorder = Recorder(self.run_dir)
        self.addCleanup(recorder.close)
        manager = self.manager(recorder=recorder)
        manager.freeze_source_snapshot()
        events = [event for event in recorder.read_events(0, 100)
                  if event["kind"] == "source_snapshot_frozen"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["source_sha256"], manager.source_sha256)
        self.assertTrue(events[0]["payload"]["created"])
        self.assertFalse(events[0]["payload"]["live_source_drift"])

    def test_drift_event_is_recorded_on_reuse(self):
        recorder = Recorder(self.run_dir)
        self.addCleanup(recorder.close)
        self.manager(recorder=recorder).freeze_source_snapshot()
        _write(self.source_root / "src" / "minecraft_harness" / "__init__.py", 'VERSION = "edited"\n')
        resumed = self.manager(recorder=recorder)
        resumed.freeze_source_snapshot()
        events = list(recorder.read_events(0, 100))
        drift = [event for event in events if event["kind"] == "source_snapshot_drift"]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["payload"]["source_sha256"], resumed.source_sha256)
        verified = [event for event in events if event["kind"] == "source_snapshot_frozen"
                    and event["payload"]["created"] is False]
        self.assertEqual(len(verified), 1)


class SourceSnapshotStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = _build_source_root(self.root / "live-source")
        self.run_dir = self.root / "run"

    async def test_start_freezes_source_before_runtime_validation(self):
        manager = _manager(self.source_root, self.run_dir, mode="container")
        self.addCleanup(shutil.rmtree, manager.rpc_dir, True)
        order = []
        original = manager.freeze_source_snapshot

        def freeze():
            order.append("freeze")
            return original()

        async def validate():
            order.append("validate")
            raise RunnerControlError("stop after runtime validation")

        manager.freeze_source_snapshot = freeze
        manager._validate_container_runtime = validate
        with self.assertRaises(RunnerControlError):
            await manager.start()
        self.assertEqual(order, ["freeze", "validate"])
        self.assertEqual(len(manager.source_sha256), 64)
        self.assertTrue((self.run_dir / "source-snapshot" / "src").is_dir())


class RunnerWebBudgetWiringTests(unittest.IsolatedAsyncioTestCase):
    """The control plane must meter runner web calls through the bound identity."""

    async def test_servicehub_meters_web_admission_for_the_bound_agent(self):
        admitted_calls = []

        class FakeBudget:
            def admit_web(self, agent_id, tool="web"):
                admitted_calls.append((agent_id, tool))
                return len(admitted_calls) == 1

        hub = ServiceHub(recorder=None, budget=FakeBudget(), memory=None, gateway=None,
                         workspaces={}, artifacts=None, config={})
        first = await hub.handle("alice", "player", "budget.admit_web",
                                 {"tool": "web_search"})
        self.assertEqual(first, {"admitted": True, "agent_id": "alice",
                                 "tool": "web_search"})
        second = await hub.handle("alice", "player", "budget.admit_web",
                                  {"tool": "web_fetch"})
        self.assertFalse(second["admitted"])
        self.assertEqual(admitted_calls, [("alice", "web_search"), ("alice", "web_fetch")])
        with self.assertRaises(PermissionError):
            await hub.handle("alice", "player", "budget.admit_web",
                             {"tool": "web_search", "agent_id": "bob"})


if __name__ == "__main__":
    unittest.main()
