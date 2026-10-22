"""End-to-end tests for same-host independent runner processes."""
import asyncio
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from minecraft_harness.budget import Budget
from minecraft_harness.config import load_config, validate_config
from minecraft_harness.recorder import Recorder
from minecraft_harness.runner_rpc import RpcServer
from minecraft_harness.runners import ArtifactVault, RunnerControlError, RunnerManager, ServiceHub
from minecraft_harness.supervisor import Supervisor, request_control

REPO = Path(__file__).resolve().parents[1]


STUB_RUNNER = '''
import asyncio
import os
import pathlib

from minecraft_harness.runner_rpc import RpcClient


async def main():
    agent = os.environ["HARNESS_RUNNER_AGENT"]
    role = os.environ["HARNESS_RUNNER_ROLE"]
    pathlib.Path(os.environ["STUB_PID_DIR"], agent + ".pid").write_text(str(os.getpid()))
    client = RpcClient(os.environ["HARNESS_RUNNER_SOCKET"],
                       os.environ["HARNESS_RUNNER_TOKEN"], agent, role)
    await client.call("runner.ready", {
        "agent_id": agent, "role": role, "pid": os.getpid(),
        "prompt_sha256": "0" * 64, "tools_sha256": "0" * 64})
    while True:
        await asyncio.sleep(0.5)
        await client.call("runner.heartbeat", {"turns": 0})


asyncio.run(main())
'''


class RunnerSupervisorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_mode_runs_agents_in_independent_os_processes(self):
        config = load_config(REPO / "configs" / "offline-smoke.yaml")
        config["runtime"]["runner_mode"] = "process"
        config["agents"]["count"] = 2
        config["agents"]["ids"] = ["player1", "player2"]
        config["agents"]["max_turns"] = 2
        config["budgets"]["total_model_calls"] = 8
        with tempfile.TemporaryDirectory() as directory:
            # Deep run directories exceed the AF_UNIX 108-byte path limit; the
            # runner control socket must not live under run_dir.
            run_dir = Path(directory) / ("deep-run-directory-" + "x" * 70)
            run_dir.mkdir()
            result = await Supervisor(config, run_dir).run()
            # Fake-mode runs finish with a run status such as "completed"; the
            # connectivity diagnostic lives in the evaluation record.
            self.assertEqual((result.get("evaluation") or {}).get("status"),
                             "diagnostic_pass")
            recorder = Recorder(run_dir)
            events = list(recorder.all_events())
            recorder.close()
            started = [event for event in events
                       if event["kind"] == "runner_process_started"]
            self.assertEqual({event["payload"]["agent_id"] for event in started},
                             {"player1", "player2"})
            pids = {event["payload"]["pid"] for event in started}
            self.assertEqual(len(pids), 2)
            self.assertNotIn(os.getpid(), pids)
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["runner_mode"], "process")
            # Python remains the default agent runtime; the Pi runtime is opt-in.
            self.assertEqual(manifest["runner_runtime"], "python")
            self.assertEqual(manifest["runner_processes"]["runner_runtime"], "python")
            self.assertEqual(manifest["runner_processes"]["player1"]["runtime"], "python")
            snapshot_manifest = json.loads((run_dir / "source-snapshot.json").read_text())
            self.assertEqual(manifest["runner_processes"]["source_sha256"],
                             snapshot_manifest["source_sha256"])
            self.assertTrue((run_dir / "source-snapshot" / "src"
                             / "minecraft_harness" / "runners.py").is_file())
            frozen_events = [event for event in events
                             if event["kind"] == "source_snapshot_frozen"]
            self.assertEqual(len(frozen_events), 1)
            self.assertEqual(frozen_events[0]["payload"]["source_sha256"],
                             snapshot_manifest["source_sha256"])
            self.assertEqual(set(manifest["agent_interfaces"]), {"player1", "player2"})
            for agent in ("player1", "player2"):
                self.assertEqual(len(manifest["agent_interfaces"][agent]["prompt_sha256"]), 64)
                self.assertTrue((run_dir / "agents" / agent / "runner.log").is_file())
                self.assertTrue((run_dir / "agents" / agent / "checkpoint.json").is_file())
            completed = {event["actor_id"] for event in events
                         if event["kind"] == "execution_finished"}
            self.assertEqual(completed, {"player1", "player2"})


    async def test_process_mode_runs_the_observer_as_a_fifth_runner(self):
        config = load_config(REPO / "configs" / "offline-smoke.yaml")
        config["runtime"]["runner_mode"] = "process"
        config["agents"]["count"] = 1
        config["agents"]["ids"] = ["player1"]
        config["agents"]["max_turns"] = 2
        config["budgets"]["total_model_calls"] = 10
        config["observer"] = {"mode": "llm", "interval_s": 60,
                              "max_turns": 2, "max_calls_per_review": 1}
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            result = await Supervisor(config, run_dir).run()
            self.assertNotEqual(result["status"], "infra_failed")
            recorder = Recorder(run_dir)
            events = list(recorder.all_events())
            recorder.close()
            started = {event["payload"]["agent_id"] for event in events
                       if event["kind"] == "runner_process_started"}
            self.assertEqual(started, {"player1", "observer"})
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertIn("observer", manifest["agent_interfaces"])
            self.assertEqual(manifest["runner_mode"], "process")


    async def test_process_mode_pause_gate_freezes_runner_model_calls(self):
        config = load_config(REPO / "configs" / "offline-smoke.yaml")
        config["runtime"]["runner_mode"] = "process"
        config["agents"]["count"] = 1
        config["agents"]["ids"] = ["player1"]
        config["agents"]["max_turns"] = 100000
        config["agents"]["max_no_progress_turns"] = 100000
        config["budgets"] = {"wall_time_minutes": 2, "total_model_calls": 100000}
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)

            def snapshot():
                recorder = Recorder(run_dir)
                try:
                    return (recorder.get_state("status"),
                            (recorder.get_state("budget") or {}).get("calls"),
                            recorder.get_state("runner_processes", {}))
                finally:
                    recorder.close()

            supervisor = Supervisor(config, run_dir)
            task = asyncio.create_task(supervisor.run())
            for _ in range(400):
                status, calls, runners = await asyncio.to_thread(snapshot)
                if status == "running":
                    break
                await asyncio.sleep(.05)
            else:
                self.fail(f"supervisor did not start: {status}")
            await asyncio.to_thread(request_control, run_dir, "pause")
            for _ in range(400):
                status, calls, runners = await asyncio.to_thread(snapshot)
                if status == "reasoning_paused_world_running":
                    break
                await asyncio.sleep(.05)
            else:
                self.fail(f"supervisor did not pause: {status}")
            paused_pid = runners["player1"]["pid"]
            paused_calls = calls
            await asyncio.sleep(.8)
            status, later_calls, later_runners = await asyncio.to_thread(snapshot)
            self.assertEqual(status, "reasoning_paused_world_running")
            self.assertLessEqual((later_calls or 0) - (paused_calls or 0), 1)
            self.assertEqual(later_runners["player1"]["pid"], paused_pid)
            await asyncio.to_thread(request_control, run_dir, "resume")
            for _ in range(400):
                status, calls, runners = await asyncio.to_thread(snapshot)
                if status == "running":
                    break
                await asyncio.sleep(.05)
            else:
                self.fail(f"supervisor did not resume: {status}")
            await asyncio.sleep(.3)
            await asyncio.to_thread(request_control, run_dir, "stop")
            result = await asyncio.wait_for(task, 120)
            self.assertEqual(result["status"], "stopped")


class ArtifactVaultMigrationTests(unittest.TestCase):
    def test_legacy_per_agent_acl_is_imported_into_central_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            recorder = Recorder(run_dir)
            digest = "a" * 64
            agent_dir = run_dir / "agents" / "player1"
            agent_dir.mkdir(parents=True)
            legacy = sqlite3.connect(agent_dir / "artifact-access.sqlite3")
            legacy.execute("CREATE TABLE owned_artifacts(hash TEXT PRIMARY KEY)")
            legacy.execute("INSERT INTO owned_artifacts VALUES(?)", (digest,))
            legacy.commit()
            legacy.close()
            vault = ArtifactVault(recorder, run_dir)
            self.assertTrue(vault.owned("player1", digest))
            self.assertFalse(vault.owned("player2", digest))
            recorder.close()

    def test_legacy_acl_migration_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            recorder = Recorder(run_dir)
            digest = "b" * 64
            agent_dir = run_dir / "agents" / "player1"
            agent_dir.mkdir(parents=True)
            legacy = sqlite3.connect(agent_dir / "artifact-access.sqlite3")
            legacy.execute("CREATE TABLE owned_artifacts(hash TEXT PRIMARY KEY)")
            legacy.execute("INSERT INTO owned_artifacts VALUES(?)", (digest,))
            legacy.commit()
            legacy.close()
            ArtifactVault(recorder, run_dir)
            recorder.close()
            recorder = Recorder(run_dir)
            vault = ArtifactVault(recorder, run_dir)
            self.assertTrue(vault.owned("player1", digest))
            recorder.close()


STUB_FALLBACK_RUNNER = """
import asyncio
import hashlib
import json
import os
import sys

from minecraft_harness.runner_rpc import RpcClient


async def main():
    agent = os.environ["HARNESS_RUNNER_AGENT"]
    role = os.environ["HARNESS_RUNNER_ROLE"]
    client = RpcClient(os.environ["HARNESS_RUNNER_SOCKET"],
                       os.environ["HARNESS_RUNNER_TOKEN"], agent, role)
    await client.call("runner.ready", {
        "agent_id": agent, "role": role, "pid": os.getpid(),
        "prompt_sha256": "0" * 64, "tools_sha256": "0" * 64})
    # Emulate a lost runner.finished response: only the documented stdout
    # fallback carries the result before a clean exit.
    sys.stdout.write(json.dumps({"__runner_result__":
        {"agent_id": agent, "status": "completed", "turns": 3}}) + "\\n")
    sys.stdout.flush()


asyncio.run(main())
"""


class RunnerFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_consumes_the_stdout_result_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            stub_dir = path / "stub"
            stub_dir.mkdir()
            (stub_dir / "stub_fallback_runner.py").write_text(STUB_FALLBACK_RUNNER)
            recorder = Recorder(path)
            config = validate_config({
                "agents": {"count": 1, "ids": ["alice"]},
                "runtime": {"mode": "fake"},
                "budgets": {"total_model_calls": 10}})
            budget = Budget(config["budgets"], recorder)
            tokens = {"token-alice": {"agent_id": "alice", "role": "player"}}
            specs = {"alice": {
                "schema_version": 1, "run_id": recorder.run_id, "agent_id": "alice",
                "role": "player", "state_root": str(path / "agents" / "alice"),
                "shell_enabled": False, "heartbeat_s": 60,
                "agent_config": {"agent_id": "alice"}, "model": {"kind": "fake"}}}

            async def handler(agent_id, role, method, params):
                return {}

            manager = RunnerManager(
                run_dir=path, config=config, specs=specs, token_map=tokens,
                service_handler=handler,
                runner_env={"PATH": os.environ.get("PATH", ""),
                            "PYTHONPATH": str(REPO / "src") + os.pathsep + str(stub_dir)},
                recorder=recorder, budget=budget,
                runner_module="stub_fallback_runner")
            await manager.start()
            outcome = await asyncio.wait_for(manager.wait("alice"), 30)
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(outcome["turns"], 3)
            events = await asyncio.to_thread(recorder.read_events, 0, 5000)
            self.assertTrue(any(event["kind"] == "runner_result_recovered_from_stdout"
                                for event in events))
            await manager.stop(grace=5)
            recorder.close()


class ServiceHubBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_cannot_settle_another_agents_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(Path(directory))
            config = validate_config({"agents": {"count": 1, "ids": ["player1"]}})
            budget = Budget(config["budgets"], recorder)
            hub = ServiceHub(recorder=recorder, budget=budget, memory=None,
                             gateway=None, workspaces={}, artifacts=None,
                             config=config)
            reservation = await budget.reserve("player1", purpose="agent_decision",
                                               role="player")
            with self.assertRaises(PermissionError):
                await hub.handle("observer", "observer", "budget.settle",
                                 {"reservation_id": reservation, "usage": {}})
            self.assertIn(reservation, budget.state["pending"])
            await hub.handle("player1", "player", "budget.settle",
                             {"reservation_id": reservation,
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                        "total_tokens": 2}})
            self.assertNotIn(reservation, budget.state["pending"])
            recorder.close()


class ObserverShutdownClassificationTests(unittest.TestCase):
    def test_expected_shutdown_failure_is_degraded_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            config = validate_config({"agents": {"count": 1, "ids": ["player1"]}})
            supervisor = Supervisor(config, directory)
            supervisor.recorder = Recorder(Path(directory))
            supervisor.config["experiment"]["profile"] = "paper_aligned"
            changed = supervisor._consume_observer_outcome(
                {"status": "infra_failed", "error": "FixtureFailure"},
                expected_shutdown=True)
            self.assertFalse(changed)
            self.assertNotEqual(supervisor.status, "infra_failed")
            events = list(supervisor.recorder.all_events())
            self.assertTrue(any(event["kind"] == "observer_degraded"
                                for event in events))
            supervisor.recorder.close()

    def test_runner_environment_uses_a_private_home(self):
        with tempfile.TemporaryDirectory() as directory:
            config = validate_config({"agents": {"count": 1, "ids": ["player1"]}})
            supervisor = Supervisor(config, directory)
            environment = supervisor._runner_environment()
            expected = Path(directory).resolve() / "runners" / "home"
            self.assertEqual(Path(environment["HOME"]), expected)
            self.assertTrue(expected.is_dir())
            self.assertIn(str(REPO / "src"), environment["PYTHONPATH"])


class RpcServerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_a_non_socket_path_is_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            path.write_text("not a socket")
            tokens = {"token": {"agent_id": "player1", "role": "player"}}

            async def handler(agent_id, role, method, params):
                return {}

            server = RpcServer(path, tokens, handler)
            with self.assertRaises(ValueError):
                await server.start()
            path.unlink()
            await server.start()
            await server.close()


class ContainerConfigAndCommandTests(unittest.TestCase):
    def test_container_config_validation(self):
        config = validate_config({"runtime": {
            "runner_mode": "container", "runner_container_memory_mb": 512,
            "runner_container_cpus": 2, "runner_container_read_only": True,
            "runner_container_network": "bridge"}})
        self.assertEqual(config["runtime"]["runner_mode"], "container")
        with self.assertRaises(ValueError):
            validate_config({"runtime": {"runner_mode": "pod"}})
        with self.assertRaises(ValueError):
            validate_config({"runtime": {"runner_container_network": "bad network"}})
        with self.assertRaises(ValueError):
            validate_config({"runtime": {"runner_container_network": "host"}})
        with self.assertRaises(ValueError):
            validate_config({"runtime": {"runner_container_cpus": 0}})
        with self.assertRaises(ValueError):
            validate_config({"runtime": {"runner_container_memory_mb": 1}})
        with self.assertRaises(ValueError):
            validate_config({"runtime": {"runner_container_shared_dir": "relative"}})

    def test_container_shared_dir_uses_the_same_socket_path_limit_as_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            # Candidate directories whose final control.sock path would exceed
            # the creation limit must be rejected during selection, not accepted
            # and then fail after the wrong shared dir was created.
            deep = Path(directory) / ("x" * 60)
            manager = object.__new__(RunnerManager)
            manager.config = {"runtime": {"runner_container_shared_dir": str(deep)}}
            manager.run_dir = deep
            with self.assertRaises(RunnerControlError):
                manager._container_shared_dir()
            shallow = Path(directory) / "short"
            manager.config = {"runtime": {"runner_container_shared_dir": str(shallow)}}
            self.assertEqual(manager._container_shared_dir(), shallow.resolve())

    def test_container_command_isolates_mounts_and_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "source"
            (source / "src").mkdir(parents=True)
            (source / ".venv" / "bin").mkdir(parents=True)
            python = source / ".venv" / "bin" / "python"
            python.write_text("fixture")
            config = validate_config({
                "agents": {"count": 1, "ids": ["alice"]},
                "runtime": {"mode": "fake", "runner_mode": "container",
                            "runner_container_shared_dir": str(path)},
                "model": {"kind": "chat_completions", "model": "fixture",
                          "base_url": "https://api.deepseek.com",
                          "api_key_env": "DEEPSEEK_API_KEY"}})
            specs = {"alice": {
                "schema_version": 1, "run_id": "run-fixture", "agent_id": "alice",
                "role": "player", "state_root": str(path / "agents" / "alice"),
                "shell_enabled": False, "heartbeat_s": 10,
                "agent_config": {"agent_id": "alice"}, "model": {"kind": "fake"}}}
            manager = RunnerManager(
                run_dir=path, config=config, specs=specs,
                token_map={"token": {"agent_id": "alice", "role": "player"}},
                service_handler=None,
                runner_env={"PATH": os.environ.get("PATH", ""),
                            "DEEPSEEK_API_KEY": "model-key",
                            "BOT1_ADMIN_TOKEN": "must-not-leak"},
                docker_bin="docker", container_source_root=source,
                container_python=str(python))
            snapshot = manager.freeze_source_snapshot()
            frozen_source = path / "source-snapshot" / "src"
            self.assertTrue(frozen_source.is_dir())
            self.assertEqual(snapshot["source_sha256"], manager.source_sha256)
            spec_path = path / "runners" / "alice.json"
            spec_path.parent.mkdir(parents=True)
            spec_path.write_text("{}")
            state_dir = path / "agents" / "alice"
            state_dir.mkdir(parents=True)
            arguments = manager._container_args("alice", spec_path, state_dir)
            command = " ".join(arguments)
            self.assertNotIn("/var/run/docker.sock", command)
            self.assertNotIn("BOT1_ADMIN_TOKEN", command)
            self.assertNotIn("must-not-leak", command)
            self.assertIn("DEEPSEEK_API_KEY=model-key", command)
            self.assertIn("dst=/state", command)
            self.assertIn("dst=/run/mc-runner-rpc", command)
            self.assertIn("dst=/run/mc-runner/spec.json", command)
            # The frozen snapshot is the mount source; dst/PYTHONPATH stay the
            # same container-side paths, so the live tree is never loaded.
            self.assertIn(f"src={frozen_source},dst={source / 'src'},readonly", command)
            self.assertNotIn(f"src={source / 'src'},dst=", command)
            self.assertIn(f"PYTHONPATH={source / 'src'}", command)
            # Container runners receive the configured per-call RPC bound too.
            self.assertIn("HARNESS_RUNNER_RPC_TIMEOUT_S=120", command)
            # F1: no unhardened tini PID 1 with the runner env secrets; the
            # hardened runner process is PID 1 under `sh -c 'exec "$@"'`.
            self.assertNotIn("--init", arguments)
            self.assertIn("readonly", command)
            self.assertNotIn(f"src={source},dst={source}", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertIn("--security-opt=no-new-privileges", command)
            self.assertTrue(str(manager.socket_path).startswith(str(path.resolve())))


CONTAINER_TESTS = os.environ.get("HARNESS_CONTAINER_TESTS") == "1"


@unittest.skipUnless(CONTAINER_TESTS, "set HARNESS_CONTAINER_TESTS=1 with Docker socket and host paths mounted")
class ContainerRunnerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_container_runners_complete_a_fake_run_and_are_removed(self):
        config = load_config(REPO / "configs" / "offline-smoke.yaml")
        config["runtime"]["runner_mode"] = "container"
        config["agents"]["count"] = 2
        config["agents"]["ids"] = ["player1", "player2"]
        config["agents"]["max_turns"] = 2
        config["budgets"]["total_model_calls"] = 8
        shared = os.environ.get("HARNESS_CONTAINER_SHARED_DIR")
        if shared:
            config["runtime"]["runner_container_shared_dir"] = shared
        root = os.environ.get("HARNESS_CONTAINER_RUN_ROOT")
        if not root:
            self.skipTest("HARNESS_CONTAINER_RUN_ROOT host-shared directory is required")
        run_dir = Path(root) / ("container-run-" + uuid.uuid4().hex[:10])
        run_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, run_dir, True)
        result = await Supervisor(config, run_dir).run()
        self.assertEqual((result.get("evaluation") or {}).get("status"), "diagnostic_pass")
        recorder = Recorder(run_dir)
        events = list(recorder.all_events())
        recorder.close()
        started = [event for event in events
                   if event["kind"] == "runner_process_started"]
        self.assertEqual({event["payload"]["agent_id"] for event in started},
                         {"player1", "player2"})
        for event in started:
            self.assertTrue(str(event["payload"].get("container", "")).startswith("mc-runner-"))
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["runner_mode"], "container")
        for agent in ("player1", "player2"):
            self.assertTrue((run_dir / "agents" / agent / "checkpoint.json").is_file())
        leftovers = await asyncio.to_thread(
            lambda: subprocess.run(
                ["docker", "ps", "-a", "--filter",
                 "label=minecraft-harness.runner=1", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=30))
        names = set(leftovers.stdout.split())
        self.assertFalse({name for name in names if name.endswith("-player1") or name.endswith("-player2")})


@unittest.skipUnless(CONTAINER_TESTS, "set HARNESS_CONTAINER_TESTS=1 with Docker socket and host paths mounted")
class ContainerObserverIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_and_player_run_as_separate_containers(self):
        config = load_config(REPO / "configs" / "offline-smoke.yaml")
        config["runtime"]["runner_mode"] = "container"
        config["agents"]["count"] = 1
        config["agents"]["ids"] = ["player1"]
        config["agents"]["max_turns"] = 2
        config["budgets"]["total_model_calls"] = 10
        config["observer"] = {"mode": "llm", "interval_s": 60,
                              "max_turns": 2, "max_calls_per_review": 1}
        shared = os.environ.get("HARNESS_CONTAINER_SHARED_DIR")
        if shared:
            config["runtime"]["runner_container_shared_dir"] = shared
        root = os.environ.get("HARNESS_CONTAINER_RUN_ROOT")
        if not root:
            self.skipTest("HARNESS_CONTAINER_RUN_ROOT host-shared directory is required")
        run_dir = Path(root) / ("container-observer-" + uuid.uuid4().hex[:10])
        run_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, run_dir, True)
        result = await Supervisor(config, run_dir).run()
        self.assertNotEqual(result["status"], "infra_failed")
        recorder = Recorder(run_dir)
        events = list(recorder.all_events())
        run_id = recorder.run_id
        recorder.close()
        started = [event for event in events if event["kind"] == "runner_process_started"]
        self.assertEqual({event["payload"]["agent_id"] for event in started},
                         {"player1", "observer"})
        self.assertTrue(all(str(event["payload"].get("container", "")).startswith("mc-runner-")
                            for event in started))
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertIn("observer", manifest["agent_interfaces"])
        leftovers = await asyncio.to_thread(
            lambda: subprocess.run(
                ["docker", "ps", "-a", "--filter",
                 f"label=minecraft-harness.run={run_id}", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=30).stdout.strip())
        self.assertEqual(leftovers, "")


@unittest.skipUnless(CONTAINER_TESTS, "set HARNESS_CONTAINER_TESTS=1 with Docker socket and host paths mounted")
class ContainerRunnerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_container_mounts_are_isolated_and_one_kill_restarts_only_victim(self):
        config = load_config(REPO / "configs" / "offline-smoke.yaml")
        config["runtime"]["runner_mode"] = "container"
        config["agents"]["count"] = 2
        config["agents"]["ids"] = ["player1", "player2"]
        config["agents"]["max_turns"] = 100000
        config["agents"]["max_no_progress_turns"] = 100000
        config["budgets"] = {"wall_time_minutes": 2, "total_model_calls": 1000000}
        shared = os.environ.get("HARNESS_CONTAINER_SHARED_DIR")
        if shared:
            config["runtime"]["runner_container_shared_dir"] = shared
        root = os.environ.get("HARNESS_CONTAINER_RUN_ROOT")
        if not root:
            self.skipTest("HARNESS_CONTAINER_RUN_ROOT host-shared directory is required")
        run_dir = Path(root) / ("container-isolation-" + uuid.uuid4().hex[:10])
        run_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, run_dir, True)
        supervisor = Supervisor(config, run_dir)
        task = asyncio.create_task(supervisor.run())

        async def wait_status(expected, timeout=120):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                recorder = Recorder(run_dir)
                try:
                    status = recorder.get_state("status")
                    run_id = recorder.run_id
                finally:
                    recorder.close()
                if status == expected:
                    return run_id
                await asyncio.sleep(.1)
            self.fail(f"status {expected} not reached")

        run_id = await wait_status("running")
        slug = hashlib.sha256(run_id.encode()).hexdigest()[:10]
        names = {agent: f"mc-runner-{slug}-{agent}" for agent in ("player1", "player2")}

        def docker(*arguments):
            result = subprocess.run(["docker", *arguments], capture_output=True,
                                    text=True, timeout=90)
            return result.returncode, result.stdout.strip(), result.stderr.strip()

        for _ in range(100):
            inspect = docker("inspect", "--format", "{{.Id}}", names["player1"])
            if inspect[0] == 0:
                break
            await asyncio.sleep(.1)
        self.assertEqual(inspect[0], 0, inspect[2])
        code, document, error = docker("inspect", names["player1"])
        self.assertEqual(code, 0, error)
        info = json.loads(document)[0]
        host = info["HostConfig"]
        mounts = info["Mounts"]
        environment = info["Config"]["Env"]
        self.assertTrue(host["ReadonlyRootfs"])
        # F1: the runner process itself is PID 1 so its secrets are only ever in
        # a PR_SET_DUMPABLE=0 process env, never in an unhardened init.
        self.assertFalse(host.get("Init", False))
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertFalse(host["Privileged"])
        self.assertEqual(host["Memory"], 1024 * 1024 * 1024)
        self.assertEqual(host["PidsLimit"], 256)
        sources = {mount["Source"] for mount in mounts if mount["Type"] == "bind"}
        destinations = {mount["Destination"] for mount in mounts if mount["Type"] == "bind"}
        snapshot_manifest = json.loads((run_dir / "source-snapshot.json").read_text())
        self.assertIn(snapshot_manifest["snapshot_src"], sources)
        self.assertIn(str(REPO / "src"), destinations)
        self.assertNotIn(str(REPO / "src"), sources)
        self.assertNotIn(str(REPO / "infra"), sources)
        self.assertIn(str(run_dir / "agents" / "player1"), sources)
        self.assertIn(str(run_dir / "runners" / "player1.json"), sources)
        self.assertFalse(any("/var/run/docker.sock" in source for source in sources))
        keys = {item.split("=", 1)[0] for item in environment}
        self.assertFalse({key for key in keys if "BOT" in key or "MC_AGENT" in key})
        self.assertIn("HARNESS_RUNNER_TOKEN", keys)
        no_socket = docker("exec", names["player1"], "sh", "-c",
                           "test ! -S /var/run/docker.sock")
        self.assertEqual(no_socket[0], 0)
        ids_before = {agent: docker("inspect", "--format", "{{.Id}}", name)[1]
                      for agent, name in names.items()}
        docker("kill", names["player2"])
        deadline = time.monotonic() + 60
        restarted = False
        while time.monotonic() < deadline:
            recorder = Recorder(run_dir)
            try:
                crashed = [event for event in recorder.all_events()
                           if event["kind"] == "runner_crashed" and
                           event["payload"].get("agent_id") == "player2"]
            finally:
                recorder.close()
            if crashed:
                code, new_id, _ = docker("inspect", "--format", "{{.Id}}", names["player2"])
                if code == 0 and new_id and new_id != ids_before["player2"]:
                    restarted = True
                    break
            await asyncio.sleep(.2)
        self.assertTrue(restarted)
        code, player1_id, _ = docker("inspect", "--format", "{{.Id}}", names["player1"])
        self.assertEqual(code, 0)
        self.assertEqual(player1_id, ids_before["player1"])
        await asyncio.to_thread(request_control, run_dir, "stop")
        result = await asyncio.wait_for(task, 180)
        self.assertEqual(result["status"], "stopped")
        leftovers = docker("ps", "-a", "--filter",
                           f"label=minecraft-harness.run={run_id}",
                           "--format", "{{.Names}}")[1]
        self.assertEqual(leftovers, "")


class RunnerManagerRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_killed_runner_is_restarted_without_restarting_peers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            stub_dir = path / "stub"
            stub_dir.mkdir()
            (stub_dir / "stub_runner.py").write_text(STUB_RUNNER)
            recorder = Recorder(path)
            config = validate_config({
                "agents": {"count": 2, "ids": ["alice", "bob"]},
                "runtime": {"mode": "fake"},
                "budgets": {"total_model_calls": 10}})
            budget = Budget(config["budgets"], recorder)
            tokens, specs = {}, {}
            for agent in ("alice", "bob"):
                tokens["token-" + agent] = {"agent_id": agent, "role": "player"}
                specs[agent] = {
                    "schema_version": 1, "run_id": recorder.run_id,
                    "agent_id": agent, "role": "player",
                    "state_root": str(path / "agents" / agent),
                    "shell_enabled": False, "heartbeat_s": 1,
                    "agent_config": {"agent_id": agent}, "model": {"kind": "fake"},
                }

            async def handler(agent_id, role, method, params):
                return {}

            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(REPO / "src") + os.pathsep + str(stub_dir),
                "STUB_PID_DIR": str(stub_dir),
            }
            manager = RunnerManager(
                run_dir=path, config=config, specs=specs, token_map=tokens,
                service_handler=handler, runner_env=environment,
                recorder=recorder, budget=budget, runner_module="stub_runner")
            await manager.start()
            self.assertEqual({agent: state["ready_info"] is not None
                              for agent, state in manager.states.items()},
                             {"alice": True, "bob": True})
            snapshot_src = manager.source_snapshot_src
            manifest = json.loads((path / "source-snapshot.json").read_text())
            self.assertEqual(len(manifest["source_sha256"]), 64)
            self.assertEqual(manifest["run_id"], recorder.run_id)
            self.assertEqual(manager.snapshot()["source_sha256"], manifest["source_sha256"])
            self.assertTrue((snapshot_src / "minecraft_harness" / "runners.py").is_file())
            self.assertFalse([item for item in snapshot_src.rglob("*")
                              if item.name == "__pycache__" or item.suffix == ".pyc"])
            ready_events = await asyncio.to_thread(recorder.read_events, 0, 5000)
            frozen_events = [event for event in ready_events
                             if event["kind"] == "source_snapshot_frozen"]
            self.assertEqual(len(frozen_events), 1)
            self.assertEqual(frozen_events[0]["payload"]["source_sha256"],
                             manifest["source_sha256"])

            def pythonpath_of(pid):
                entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                prefix = b"PYTHONPATH="
                value = next((entry[len(prefix):] for entry in entries
                              if entry.startswith(prefix)), b"")
                return value.decode("utf-8").split(os.pathsep)

            original = {agent: manager.states[agent]["process"].pid
                        for agent in ("alice", "bob")}
            self.assertEqual(len(set(original.values())), 2)
            for agent, pid in original.items():
                entries = pythonpath_of(pid)
                self.assertEqual(entries[0], str(snapshot_src),
                                 f"{agent} must import the frozen runner source first")
                self.assertNotIn(str(REPO / "src"), entries)
                self.assertIn(str(stub_dir), entries)
            os.kill(original["alice"], signal.SIGKILL)
            deadline = time.monotonic() + 40
            restarted = False
            while time.monotonic() < deadline:
                events = await asyncio.to_thread(recorder.read_events, 0, 5000)
                if any(event["kind"] == "runner_restarted" and
                       event["payload"].get("agent_id") == "alice" for event in events):
                    restarted = True
                    break
                await asyncio.sleep(.1)
            self.assertTrue(restarted, "killed runner was not restarted")
            self.assertEqual(manager.states["alice"]["attempts"], 0)
            self.assertEqual(manager.states["bob"]["process"].pid, original["bob"])
            self.assertNotEqual(manager.states["alice"]["process"].pid, original["alice"])
            # The restarted runner still loads the frozen snapshot, not live src.
            self.assertEqual(pythonpath_of(manager.states["alice"]["process"].pid)[0],
                             str(snapshot_src))
            self.assertEqual(json.loads((path / "source-snapshot.json").read_text())["source_sha256"],
                             manifest["source_sha256"])
            outcomes = await manager.stop(grace=5)
            self.assertEqual(outcomes["alice"]["status"], "stopped")
            self.assertEqual(outcomes["bob"]["status"], "stopped")
            recorder.close()


if __name__ == "__main__":
    unittest.main()
