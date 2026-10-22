"""RunnerManager/config/supervisor integration for the optional Pi agent runtime.

These tests never start Docker, Node or a real Pi CLI: they assert the config
enum, the runner spec contract handed to ``minecraft_harness.pi_runner``, the
container mount/environment wiring, process-mode paths, and that the default
Python runtime is byte-for-byte unchanged.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = REPO / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from minecraft_harness.config import load_config, validate_config
from minecraft_harness.recorder import Recorder
from minecraft_harness.pi_model_proxy import ENV_MODEL_FORWARDER_TOKEN
from minecraft_harness.runners import (RunnerControlError, RunnerManager,
                                       resolve_runner_egress)
from minecraft_harness.supervisor import Supervisor

PI_CLI_RELATIVE = (
    "pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js")
MODEL = {"kind": "fake", "model": "fake-model", "base_url": "https://api.example.test",
         "api_key_env": "FAKE_API_KEY", "max_output_tokens": 4096, "timeout_s": 90}
CLI_ENVIRONMENT = {
    "MC_RUNTIME_URL": "http://localhost",
    "MC_AGENT_ID": "alice",
    "MC_AGENT_TOKEN": "broker-token",
    "MC_SUBMISSION_AUTHORITY": "durable_gateway",
    "MC_RUNTIME_SOCKET": "/run/mc-broker/socket",
}


def _fake_source_root(root, *, extensions=True):
    """Repository-shaped fixture: harness src plus a pinned pi_runtime tree."""
    src = root / "src" / "minecraft_harness"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('VERSION = "fixture"\n')
    (src / "runner_rpc.py").write_text("RPC = 1\n")
    cli = root / PI_CLI_RELATIVE
    cli.parent.mkdir(parents=True)
    cli.write_text("// pinned Pi CLI fixture\n")
    if extensions:
        extension_dir = root / "pi_runtime" / "extensions"
        extension_dir.mkdir(parents=True)
        (extension_dir / "minecraft-tools.js").write_text("// player tools\n")
        (extension_dir / "observer-tools.js").write_text("// observer tools\n")
    (root / ".venv" / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
    return root


class PiConfigTests(unittest.TestCase):
    def test_defaults_keep_the_python_runtime(self):
        config = validate_config({})
        runtime = config["runtime"]
        self.assertEqual(runtime["runner_runtime"], "python")
        self.assertEqual(runtime["pi_context_window"], 64000)
        self.assertTrue(Path(runtime["pi_cli"]).is_absolute())
        self.assertTrue(runtime["pi_cli"].endswith(PI_CLI_RELATIVE))
        self.assertEqual(runtime["runner_mode"], "inprocess")

    def test_pi_runtime_values_validate_and_relative_cli_resolves(self):
        config = validate_config({"runtime": {
            "runner_runtime": "pi", "pi_context_window": 32000,
            "pi_cli": "pi_runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"}})
        self.assertEqual(config["runtime"]["runner_runtime"], "pi")
        self.assertEqual(config["runtime"]["pi_context_window"], 32000)
        self.assertEqual(config["runtime"]["pi_cli"], str((REPO / PI_CLI_RELATIVE).resolve()))
        absolute = validate_config({"runtime": {
            "runner_runtime": "pi", "pi_cli": str(REPO / PI_CLI_RELATIVE)}})
        self.assertEqual(absolute["runtime"]["pi_cli"], str((REPO / PI_CLI_RELATIVE).resolve()))

    def test_pi_runtime_rejects_invalid_values(self):
        for invalid in ("node", "inprocess", None, 1, ""):
            with self.subTest(runner_runtime=invalid):
                with self.assertRaises(ValueError):
                    validate_config({"runtime": {"runner_runtime": invalid}})
        for invalid in ("", "cli.js.txt", 7, None):
            with self.subTest(pi_cli=invalid):
                with self.assertRaises(ValueError):
                    validate_config({"runtime": {"runner_runtime": "pi", "pi_cli": invalid}})
        for invalid in (1023, 4_000_001, True, "64000", 1.5):
            with self.subTest(pi_context_window=invalid):
                with self.assertRaises(ValueError):
                    validate_config({"runtime": {"runner_runtime": "pi",
                                                 "pi_context_window": invalid}})
        for valid in (1024, 64000, 4_000_000):
            with self.subTest(pi_context_window=valid):
                self.assertEqual(validate_config({"runtime": {
                    "runner_runtime": "pi", "pi_context_window": valid}})
                    ["runtime"]["pi_context_window"], valid)

    def test_paper_configs_stay_valid_with_python_default(self):
        for name, runner_mode, runner_runtime in (
                ("minecraft-paper-runner-20261022.yaml", "container", "pi"),
                ("minecraft-paper-whole-session.yaml", "process", "python")):
            with self.subTest(config=name):
                config = load_config(REPO / "configs" / name)
                self.assertEqual(config["runtime"]["runner_runtime"], runner_runtime)
                self.assertEqual(config["runtime"]["runner_mode"], runner_mode)
                self.assertEqual(config["runtime"]["pi_context_window"], 64000)


class _ManagerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = _fake_source_root(self.root / "repo")
        self.run_dir = self.root / "run"
        self.managers = []

    def manager(self, *, mode="container", runner_runtime="pi", agents=("alice",),
                runtime=None, runner_env=None, source_root=None,
                egress="bridge", forwarder_socket=None, forwarder_token=None):
        source_root = source_root or self.source_root
        cli = source_root / PI_CLI_RELATIVE
        runtime_settings = {
            "mode": "fake", "runner_mode": mode, "runner_runtime": runner_runtime,
            # Direct-RunnerManager tests choose bridge explicitly; the
            # restricted forwarder mode is exercised separately below.
            "runner_egress": egress,
            "pi_cli": str(cli), "pi_context_window": 32000,
        }
        runtime_settings.update(runtime or {})
        if mode == "container":
            runtime_settings.setdefault("runner_container_shared_dir",
                                        str(self.root / "shared"))
        config = {"runtime": runtime_settings, "model": dict(MODEL),
                  "observer": {"mode": "llm", "interval_s": 30,
                               "max_reviews": 7, "allow_infra_requests": True}}
        tokens, specs = {}, {}
        for agent in agents:
            role = "observer" if agent == "observer" else "player"
            tokens["token-" + agent] = {"agent_id": agent, "role": role}
            specs[agent] = {
                "schema_version": 1, "run_id": "run-fixture", "agent_id": agent,
                "role": role, "state_root": str(self.run_dir / "agents" / agent),
                "shell_enabled": True, "heartbeat_s": 10,
                "agent_config": {"agent_id": agent, "goal": "defeat the dragon",
                                 "peers": [other for other in agents if other != agent],
                                 "profile": "paper_aligned", "context_chars": 224000,
                                 "max_turns": 100000},
                "model": dict(MODEL),
                "pi_model": dict(MODEL),
                "cli_environment": dict(CLI_ENVIRONMENT) if role == "player" else {},
                "observer": ({"interval_s": 30, "max_reviews": 7}
                             if role == "observer" else None),
            }
        manager = RunnerManager(
            run_dir=self.run_dir, config=config, specs=specs, token_map=tokens,
            service_handler=None,
            runner_env=runner_env if runner_env is not None else {
                "PATH": "/usr/local/bin:/usr/bin:/bin", "FAKE_API_KEY": "model-key"},
            docker_bin="docker", container_source_root=source_root,
            container_python="/usr/bin/python3.11",
            model_forwarder_socket=forwarder_socket,
            model_forwarder_token=forwarder_token)
        self.addCleanup(shutil.rmtree, manager.rpc_dir, True)
        self.managers.append(manager)
        return manager


class PiRunnerSpecTests(_ManagerFixture):
    def test_pi_runner_spec_contract(self):
        manager = self.manager()
        spec = manager._runner_spec("alice")
        self.assertEqual(spec["runner_runtime"], "pi")
        self.assertEqual(spec["pi_cli"], str(self.source_root / PI_CLI_RELATIVE))
        self.assertEqual(spec["pi_context_window"], 32000)
        self.assertEqual(spec["workspace_root"], "/workspace")
        self.assertEqual(spec["reference_root"], "/reference")
        self.assertEqual(spec["team_memory_root"], "/memory/team")
        self.assertEqual(spec["broker_socket"], "/run/mc-broker/socket")
        self.assertEqual(spec["cli_environment"],
                         {**CLI_ENVIRONMENT, "MC_RUNTIME_SOCKET": "/run/mc-broker/socket"})
        self.assertEqual(spec["pi_model"], MODEL)
        self.assertNotIn("api_key", spec["pi_model"])
        self.assertEqual(spec["state_root"], "/state")
        self.assertEqual(spec["agent_config"]["profile"], "paper_aligned")
        self.assertEqual(spec["agent_config"]["goal"], "defeat the dragon")

    def test_pi_runner_mounts_and_environment(self):
        manager = self.manager()
        manager.freeze_source_snapshot()
        spec_path = self.run_dir / "runners" / "alice.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("{}")
        state_dir = self.run_dir / "agents" / "alice"
        state_dir.mkdir(parents=True)
        args = manager._container_args("alice", spec_path, state_dir)
        command = " ".join(args)

        snapshot_src = manager.source_snapshot_src
        workspace = manager._pi_host_workspace("alice")
        reference = manager._pi_host_reference()
        team_memory = manager._pi_host_team_memory()
        broker_dir = manager._pi_host_broker_dir()
        pi_tree = self.source_root / "pi_runtime"
        self.assertIn(f"src={snapshot_src},dst={self.source_root / 'src'},readonly", command)
        self.assertIn(f"src={workspace},dst=/workspace", command)
        self.assertIn(f"src={reference},dst=/reference,readonly", command)
        self.assertIn(f"src={team_memory},dst=/memory/team", command)
        self.assertIn(f"src={broker_dir},dst=/run/mc-broker,readonly", command)
        self.assertIn(f"src={pi_tree},dst={pi_tree},readonly", command)
        self.assertIn(f"src={state_dir},dst=/state", command)
        self.assertNotIn(f"src={self.source_root / 'src'},dst={self.source_root / 'src'}", command)
        # Workspace/team memory must stay writable; docs and runtime read-only.
        mounts = [args[index + 1] for index, value in enumerate(args) if value == "--mount"]
        workspace_mount = next(mount for mount in mounts if mount.endswith("dst=/workspace"))
        self.assertNotIn("readonly", workspace_mount)
        team_mount = next(mount for mount in mounts if mount.endswith("dst=/memory/team"))
        self.assertNotIn("readonly", team_mount)
        self.assertTrue(workspace.is_dir())
        self.assertTrue(reference.is_dir())
        self.assertTrue(team_memory.is_dir())
        self.assertTrue(broker_dir.is_dir())
        # F1: the hardened runner is PID 1; no unhardened tini inherits its env.
        self.assertNotIn("--init", command)
        for expected in ("-m minecraft_harness.pi_runner", "HARNESS_PI_CLI=" + str(self.source_root / PI_CLI_RELATIVE),
                         "HARNESS_PI_CONTEXT_WINDOW=32000", "MC_RUNTIME_SOCKET=/run/mc-broker/socket",
                         "MC_AGENT_ID=alice", "MC_AGENT_TOKEN=broker-token",
                         "MC_SUBMISSION_AUTHORITY=durable_gateway", "FAKE_API_KEY=model-key",
                         "PATH=/state/bin:"):
            with self.subTest(expected=expected):
                self.assertIn(expected, command)
        self.assertNotIn("minecraft_harness.runner ", command)

    def test_pi_process_spec_and_environment(self):
        manager = self.manager(mode="process")
        manager.freeze_source_snapshot()
        spec_path = self.run_dir / "runners" / "alice.json"
        spec = manager._runner_spec("alice")
        self.assertEqual(spec["workspace_root"],
                         str(self.run_dir / "workspaces" / "alice"))
        self.assertEqual(spec["reference_root"], str(self.run_dir / "reference-docs"))
        self.assertEqual(spec["team_memory_root"], str(self.run_dir / "memory" / "team"))
        self.assertEqual(spec["broker_socket"], str(self.run_dir / "broker" / "socket"))
        self.assertEqual(spec["cli_environment"]["MC_RUNTIME_SOCKET"],
                         str(self.run_dir / "broker" / "socket"))
        self.assertEqual(spec["state_root"], str(self.run_dir / "agents" / "alice"))
        environment = manager._process_environment("alice", spec_path)
        self.assertEqual(environment["HARNESS_PI_CLI"], str(self.source_root / PI_CLI_RELATIVE))
        self.assertEqual(environment["HARNESS_PI_CONTEXT_WINDOW"], "32000")
        self.assertEqual(environment["MC_RUNTIME_SOCKET"],
                         str(self.run_dir / "broker" / "socket"))
        self.assertEqual(environment["MC_AGENT_ID"], "alice")
        self.assertEqual(environment["PYTHONPATH"].split(os.pathsep)[0],
                         str(manager.source_snapshot_src))
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(manager._process_cwd("alice"),
                         self.run_dir / "workspaces" / "alice")

    def test_pi_spec_without_broker_environment_disables_game_cli(self):
        manager = self.manager()
        manager.specs["alice"]["cli_environment"] = {}
        spec = manager._runner_spec("alice")
        self.assertEqual(spec["cli_environment"], {})
        self.assertIsNone(spec["broker_socket"])

    def test_python_runtime_default_is_unchanged(self):
        manager = self.manager(runner_runtime="python")
        manager.freeze_source_snapshot()
        spec_path = self.run_dir / "runners" / "alice.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("{}")
        state_dir = self.run_dir / "agents" / "alice"
        state_dir.mkdir(parents=True)
        args = manager._container_args("alice", spec_path, state_dir)
        command = " ".join(args)
        self.assertIn("-m minecraft_harness.runner", command)
        self.assertNotIn("minecraft_harness.pi_runner", command)
        self.assertNotIn("HARNESS_PI_CLI", command)
        self.assertNotIn("dst=/workspace", command)
        self.assertNotIn("dst=/reference", command)
        self.assertNotIn("dst=/memory/team", command)
        self.assertNotIn("--init", command)
        spec = manager._runner_spec("alice")
        self.assertEqual(spec["runner_runtime"], "python")
        self.assertNotIn("pi_cli", spec)
        self.assertEqual(manager._process_cwd("alice"), self.run_dir)
        environment = manager._process_environment("alice", spec_path)
        self.assertNotIn("HARNESS_PI_CLI", environment)
        self.assertNotIn("MC_RUNTIME_SOCKET", environment)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["runner_runtime"], "python")
        self.assertEqual(snapshot["alice"]["runtime"], "python")

    def test_observer_uses_pi_observer_runner_and_read_only_spec(self):
        manager = self.manager(agents=("alice", "observer"))
        self.assertEqual(manager._agent_runtime("alice"), "pi")
        self.assertEqual(manager._agent_runtime("observer"), "pi")
        self.assertEqual(manager._agent_module("alice"), "minecraft_harness.pi_runner")
        self.assertEqual(manager._agent_module("observer"),
                         "minecraft_harness.pi_observer_runner")
        spec = manager._runner_spec("observer")
        self.assertEqual(spec["runner_runtime"], "pi")
        self.assertEqual(spec["agent_id"], "observer")
        self.assertEqual(spec["role"], "observer")
        self.assertEqual(spec["state_root"], "/state")
        self.assertEqual(spec["workspace_root"], "/workspace")
        self.assertEqual(spec["reference_root"], "/reference")
        self.assertEqual(spec["pi_cli"], str(self.source_root / PI_CLI_RELATIVE))
        self.assertEqual(spec["pi_context_window"], 32000)
        self.assertEqual(spec["pi_model"], MODEL)
        self.assertNotIn("api_key", spec["pi_model"])
        self.assertEqual(spec["observer"], {"interval_s": 30, "max_reviews": 7})
        self.assertEqual(spec["interval_s"], 30)
        self.assertEqual(spec["max_reviews"], 7)
        self.assertFalse(spec["shell_enabled"])
        self.assertNotIn("team_memory_root", spec)
        self.assertIn(spec.get("broker_socket"), (None, ""))
        self.assertIn(spec.get("cli_environment"), (None, {}))
        # Player specs keep the full Pi player contract.
        player = manager._runner_spec("alice")
        self.assertEqual(player["broker_socket"], "/run/mc-broker/socket")
        self.assertEqual(player["team_memory_root"], "/memory/team")
        self.assertEqual(player["cli_environment"]["MC_AGENT_ID"], "alice")

    def test_observer_config_falls_back_to_supervisor_config(self):
        manager = self.manager(agents=("alice", "observer"))
        manager.specs["observer"].pop("observer", None)
        spec = manager._runner_spec("observer")
        self.assertEqual(spec["observer"], {"mode": "llm", "interval_s": 30,
                                            "max_reviews": 7,
                                            "allow_infra_requests": True})
        self.assertEqual(spec["interval_s"], 30)
        self.assertEqual(spec["max_reviews"], 7)

    def test_observer_pi_container_mounts_and_environment_are_read_only(self):
        manager = self.manager(agents=("alice", "observer"))
        manager.freeze_source_snapshot()
        spec_path = self.run_dir / "runners" / "observer.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("{}")
        state_dir = self.run_dir / "agents" / "observer"
        state_dir.mkdir(parents=True)
        args = manager._container_args("observer", spec_path, state_dir)
        command = " ".join(args)
        workspace = manager._pi_host_workspace("observer")
        reference = manager._pi_host_reference()
        pi_tree = self.source_root / "pi_runtime"
        self.assertIn("-m minecraft_harness.pi_observer_runner", command)
        self.assertNotIn("-m minecraft_harness.pi_runner ", command)
        self.assertIn(f"src={workspace},dst=/workspace", command)
        self.assertIn(f"src={reference},dst=/reference,readonly", command)
        self.assertIn(f"src={pi_tree},dst={pi_tree},readonly", command)
        self.assertIn(f"src={state_dir},dst=/state", command)
        self.assertNotIn("dst=/memory/team", command)
        self.assertNotIn("/run/mc-broker", command)
        self.assertNotIn("/var/run/docker.sock", command)
        self.assertNotIn("MC_AGENT_ID", command)
        self.assertNotIn("MC_AGENT_TOKEN", command)
        self.assertNotIn("MC_RUNTIME_SOCKET", command)
        self.assertIn("HARNESS_PI_CLI=" + str(self.source_root / PI_CLI_RELATIVE), command)
        self.assertIn("HARNESS_PI_CONTEXT_WINDOW=32000", command)
        self.assertNotIn("PATH=/state/bin:", command)
        self.assertNotIn("--init", command)
        # No shared team memory or broker directory is created for the Observer.
        self.assertFalse((self.run_dir / "memory" / "team").exists())
        self.assertFalse((self.run_dir / "broker").exists())

    def test_observer_pi_process_spec_and_environment(self):
        manager = self.manager(mode="process", agents=("alice", "observer"))
        manager.freeze_source_snapshot()
        spec_path = self.run_dir / "runners" / "observer.json"
        spec = manager._runner_spec("observer")
        self.assertEqual(spec["workspace_root"],
                         str(self.run_dir / "workspaces" / "observer"))
        self.assertEqual(spec["reference_root"], str(self.run_dir / "reference-docs"))
        self.assertEqual(spec["state_root"], str(self.run_dir / "agents" / "observer"))
        environment = manager._process_environment("observer", spec_path)
        self.assertEqual(environment["HARNESS_PI_CLI"],
                         str(self.source_root / PI_CLI_RELATIVE))
        self.assertEqual(environment["HARNESS_PI_CONTEXT_WINDOW"], "32000")
        for forbidden in ("MC_AGENT_ID", "MC_AGENT_TOKEN", "MC_RUNTIME_SOCKET",
                          "MC_RUNTIME_URL", "MC_SUBMISSION_AUTHORITY"):
            self.assertNotIn(forbidden, environment)
        self.assertEqual(manager._process_cwd("observer"),
                         self.run_dir / "workspaces" / "observer")

    def test_pi_runtime_hashes_pin_both_extensions(self):
        manager = self.manager(agents=("alice", "observer"))
        hashes = manager._pi_runtime_hashes()
        files = hashes["files"]
        self.assertTrue(any(path.endswith("minecraft-tools.js") for path in files), files)
        self.assertTrue(any(path.endswith("observer-tools.js") for path in files), files)
        self.assertEqual(len(hashes["sha256"]), 64)

    def test_pi_runtime_hash_covers_bundle_chunks_not_just_the_cli_shim(self):
        manager = self.manager(agents=("alice", "observer"))
        chunk = (self.source_root / "pi_runtime" / "node_modules" /
                 "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" /
                 "cli-runtime.js")
        chunk.parent.mkdir(parents=True, exist_ok=True)
        chunk.write_text("// original chunk\n")
        before = manager._pi_runtime_hashes()
        self.assertIn("node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli-runtime.js",
                      before["files"])
        chunk.write_text("// tampered chunk\n")
        after = manager._pi_runtime_hashes()
        self.assertNotEqual(before["sha256"], after["sha256"])

    def test_python_runtime_keeps_observer_on_the_python_runner(self):
        manager = self.manager(runner_runtime="python", agents=("alice", "observer"))
        self.assertEqual(manager._agent_module("alice"), "minecraft_harness.runner")
        self.assertEqual(manager._agent_module("observer"), "minecraft_harness.runner")
        self.assertEqual(manager._runner_spec("observer")["runner_runtime"], "python")
        self.assertEqual(manager.snapshot()["observer"]["runtime"], "python")

    def test_explicit_runner_module_override_still_wins(self):
        # Tests/operators may pin a different module; runtime routing then does
        # not rewrite it (only the default selection is config-driven).
        manager = self.manager()
        manager.runner_module = "stub_pi_runner"
        self.assertEqual(manager._agent_module("alice"), "stub_pi_runner")
        self.assertEqual(manager._agent_module("observer"), "stub_pi_runner")


class PiObserverSpecTests(_ManagerFixture):
    def test_observer_spec_is_accepted_by_pi_observer_runner(self):
        try:
            from minecraft_harness import pi_observer_runner, pi_runner
        except Exception as exc:  # task-18 module must exist in this repo
            self.skipTest(f"pi_observer_runner unavailable: {exc}")
        manager = self.manager(agents=("alice", "observer"))
        spec = manager._runner_spec("observer")
        spec_path = self.run_dir / "runners" / "observer.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text(json.dumps(spec))
        os.chmod(spec_path, 0o600)
        environment = {
            "HARNESS_RUNNER_SOCKET": str(self.root / "control.sock"),
            "HARNESS_RUNNER_TOKEN": "observer-token",
            "HARNESS_RUNNER_AGENT": "observer",
            "HARNESS_RUNNER_ROLE": "observer",
            "HARNESS_RUNNER_RUN_ID": "run-fixture",
            "HARNESS_RUNNER_SPEC": str(spec_path),
        }
        loaded = pi_observer_runner.load_spec(
            pi_runner.PiEnvironment.from_environment(environment))
        self.assertEqual(loaded.run_id, "run-fixture")
        self.assertEqual(loaded.agent_id, "observer")
        self.assertEqual(loaded.role, "observer")
        self.assertEqual(loaded.state_root, Path("/state"))
        self.assertEqual(loaded.workspace_root, Path("/workspace"))
        self.assertEqual(loaded.reference_root, Path("/reference"))
        self.assertEqual(loaded.pi_context_window, 32000)
        self.assertEqual(loaded.interval_s, 30.0)
        self.assertEqual(loaded.max_reviews, 7)
        self.assertEqual(loaded.pi_session_max_bytes, 1_000_000_000)
        self.assertEqual(loaded.pi_artifact_max_bytes, 4_000_000_000)
        self.assertEqual(loaded.agent_config["profile"], "paper_aligned")
        self.assertEqual(loaded.pi_model["model"], "fake-model")


class PiRunnerEgressTests(_ManagerFixture):
    def _forwarder(self):
        directory = self.root / "model-forwarder"
        directory.mkdir(parents=True, exist_ok=True)
        socket_path = directory / "socket"
        if not socket_path.exists():
            socket_path.write_text("unix socket placeholder")
        return socket_path

    def test_egress_resolution_defaults_and_overrides(self):
        pi_container = {"runner_mode": "container", "runner_runtime": "pi"}
        self.assertEqual(resolve_runner_egress({"runtime": pi_container}), "model_only")
        self.assertEqual(resolve_runner_egress({"runtime": {
            "runner_mode": "container", "runner_runtime": "python"}}), "bridge")
        self.assertEqual(resolve_runner_egress({"runtime": {
            "runner_mode": "process", "runner_runtime": "pi"}}), "bridge")
        self.assertEqual(resolve_runner_egress({"runtime": {
            "runner_mode": "inprocess", "runner_runtime": "python"}}), "bridge")
        self.assertEqual(resolve_runner_egress({"runtime": {
            "runner_mode": "container", "runner_runtime": "pi",
            "runner_egress": "bridge"}}), "bridge")
        with self.assertRaises(RunnerControlError):
            resolve_runner_egress({"runtime": {"runner_egress": "tor"}})

    def test_restricted_container_has_no_network_and_uses_forwarder(self):
        socket_path = self._forwarder()
        token = "forwarder-token-" + "z" * 24
        manager = self.manager(agents=("alice", "observer"), egress="model_only",
                               forwarder_socket=str(socket_path),
                               forwarder_token=token)
        manager.freeze_source_snapshot()
        self.assertEqual(manager._container_network_for("alice"), "none")
        self.assertEqual(manager._container_network_for("observer"), "none")
        for agent, module in (("alice", "minecraft_harness.pi_runner"),
                              ("observer", "minecraft_harness.pi_observer_runner")):
            spec_path = self.run_dir / "runners" / f"{agent}.json"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text("{}")
            state_dir = self.run_dir / "agents" / agent
            state_dir.mkdir(parents=True, exist_ok=True)
            args = manager._container_args(agent, spec_path, state_dir)
            command = " ".join(args)
            self.assertIn(f"-m {module}", command)
            self.assertIn("--network none", command)
            self.assertNotIn("--network bridge", command)
            self.assertIn(f"src={socket_path.parent},dst=/run/mc-model,readonly", command)
            self.assertIn(f"{ENV_MODEL_FORWARDER_TOKEN}={token}", command)
            # The provider credential must not be in the runner environment.
            self.assertNotIn("FAKE_API_KEY=model-key", command)
            self.assertNotIn("provider", command)
            spec = manager._runner_spec(agent)
            self.assertEqual(spec["pi_model"]["model_forwarder_socket"],
                             "/run/mc-model/socket")
            self.assertEqual(manager.snapshot()[agent]["egress"], "model_only")
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["runner_egress"], "model_only")
        self.assertEqual(snapshot["model_forwarder"]["container_socket"],
                         "/run/mc-model/socket")

    def test_paper_runner_config_resolves_to_restricted_egress(self):
        config_path = REPO / "configs" / "minecraft-paper-runner-20261022.yaml"
        if not config_path.is_file():
            self.skipTest("paper runner config is not present")
        import yaml
        config = validate_config(yaml.safe_load(config_path.read_text()))
        self.assertEqual(config["runtime"]["runner_runtime"], "pi")
        self.assertEqual(config["runtime"]["runner_mode"], "container")
        # The paper config does not need an extra key: the Pi container runtime
        # defaults to the host model forwarder (python/inprocess keep bridge).
        self.assertEqual(resolve_runner_egress(config), "model_only")

    def test_bridge_mode_keeps_model_key_environment(self):
        manager = self.manager(agents=("alice",))
        environment = manager._container_environment("alice")
        self.assertEqual(environment["FAKE_API_KEY"], "model-key")
        self.assertNotIn(ENV_MODEL_FORWARDER_TOKEN, environment)
        self.assertEqual(manager._container_network_for("alice"), "bridge")
        self.assertNotIn("model_forwarder_socket",
                         manager._runner_spec("alice")["pi_model"])

    def test_restricted_mode_requires_socket_token_and_container(self):
        token = "forwarder-token-" + "z" * 24
        missing = self.root / "missing" / "socket"
        manager = self.manager(egress="model_only", forwarder_socket=str(missing),
                               forwarder_token=token)
        with self.assertRaises(RunnerControlError):
            manager._validate_model_forwarder()
        with self.assertRaises(RunnerControlError):
            self.manager(egress="model_only", forwarder_socket=str(missing))
        with self.assertRaises(RunnerControlError):
            self.manager(mode="process", egress="model_only",
                         forwarder_socket=str(missing), forwarder_token=token)
        with self.assertRaises(RunnerControlError):
            self.manager(runner_runtime="python", egress="model_only",
                         forwarder_socket=str(missing), forwarder_token=token)
        socket_path = self._forwarder()
        manager = self.manager(egress="model_only",
                               forwarder_socket=str(socket_path),
                               forwarder_token=token)
        manager._validate_model_forwarder()  # socket placeholder exists

    def test_restricted_start_fails_closed_when_forwarder_is_absent(self):
        socket_path = self.root / "missing" / "socket"
        token = "forwarder-token-" + "z" * 24
        # Constructor accepts a configured path; validation fails before spawn.
        manager = self.manager(egress="model_only", forwarder_socket=str(socket_path),
                               forwarder_token=token)
        with self.assertRaises(RunnerControlError) as raised:
            asyncio.run(manager.start())
        self.assertIn("model forwarder socket", str(raised.exception))


class PiRuntimeValidationTests(_ManagerFixture):
    def test_missing_pi_cli_fails_closed_before_spawning(self):
        manager = self.manager(mode="process", runtime={
            "pi_cli": str(self.root / "missing-pi-runtime" / "cli.js")})
        with self.assertRaises(RunnerControlError) as raised:
            asyncio.run(manager.start())
        self.assertIn("runtime.pi_cli does not exist", str(raised.exception))
        self.assertTrue(all(state["process"] is None for state in manager.states.values()))
        self.assertFalse(manager._started)

    def test_missing_observer_extension_fails_closed_before_spawning(self):
        source_root = _fake_source_root(self.root / "repo-no-observer-extension")
        (source_root / "pi_runtime" / "extensions" / "observer-tools.js").unlink()
        manager = self.manager(mode="process", agents=("alice", "observer"),
                               source_root=source_root)
        with self.assertRaises(RunnerControlError) as raised:
            asyncio.run(manager.start())
        self.assertIn("observer extension is missing", str(raised.exception))
        self.assertTrue(all(state["process"] is None for state in manager.states.values()))

    def test_pi_model_must_not_include_an_inline_api_key(self):
        manager = self.manager()
        manager.specs["alice"]["pi_model"] = {"model": "x", "api_key": "inline-secret"}
        with self.assertRaises(RunnerControlError) as raised:
            manager._runner_spec("alice")
        self.assertIn("inline api_key", str(raised.exception))

    def test_invalid_config_runtime_rejected_by_manager(self):
        with self.assertRaises(RunnerControlError):
            self.manager(runtime={"runner_runtime": "node"})
        with self.assertRaises(RunnerControlError):
            self.manager(runtime={"pi_context_window": 10})


class SupervisorPiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = Path(self.temporary.name) / "run"

    def test_manifest_exposes_runner_runtime_default_and_pi(self):
        python = Supervisor(validate_config({"runtime": {"mode": "fake"}}), self.run_dir)
        self.assertEqual(python._manifest()["runner_runtime"], "python")
        self.assertEqual(python._manifest()["runner_egress"], "bridge")
        pi = Supervisor(validate_config({"runtime": {"mode": "fake", "runner_runtime": "pi"}}),
                        self.run_dir / "pi-run")
        self.assertEqual(pi._manifest()["runner_runtime"], "pi")
        self.assertEqual(pi._manifest()["runner_egress"], "bridge")
        pi_container = Supervisor(validate_config({
            "runtime": {"mode": "fake", "runner_mode": "container",
                        "runner_runtime": "pi"}}), self.run_dir / "pi-container-run")
        self.assertEqual(pi_container._manifest()["runner_egress"], "model_only")

    def test_runner_cli_environment_degrades_to_empty_without_broker(self):
        supervisor = Supervisor(validate_config({"runtime": {"mode": "fake"}}), self.run_dir)
        supervisor.gateway = types.SimpleNamespace(
            cli_environment=lambda agent: {"MC_AGENT_ID": agent, "MC_RUNTIME_SOCKET": "/run/mc-broker/socket"})
        self.assertEqual(supervisor._runner_cli_environment("player1"),
                         {"MC_AGENT_ID": "player1",
                          "MC_RUNTIME_SOCKET": "/run/mc-broker/socket"})

        def boom(agent):
            raise RuntimeError("player CLI broker is not running")

        supervisor.gateway = types.SimpleNamespace(cli_environment=boom)
        self.assertEqual(supervisor._runner_cli_environment("player1"), {})


class SupervisorPiObserverRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = Path(self.temporary.name) / "run"

    async def test_start_runner_agents_passes_observer_config_and_pi_model(self):
        config = validate_config({
            "runtime": {"mode": "fake", "runner_mode": "process", "runner_runtime": "pi"},
            "observer": {"mode": "llm", "interval_s": 30},
        })
        run_dir = self.run_dir
        supervisor = Supervisor(config, run_dir)
        supervisor.recorder = Recorder(run_dir)
        self.addCleanup(supervisor.recorder.close)
        supervisor.budget = types.SimpleNamespace()
        supervisor.gateway = types.SimpleNamespace(
            cli_environment=lambda agent: {"MC_AGENT_ID": agent,
                                           "MC_RUNTIME_SOCKET": "/run/mc-broker/socket"},
            world_epoch=0)
        captured = {}

        class FakeManager:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def start(self):
                return {agent: {"prompt_sha256": "a" * 64,
                                "tools_sha256": "b" * 64, "pid": 1}
                        for agent in captured["specs"]}

            async def wait(self, agent):
                await asyncio.sleep(3600)

            def snapshot(self):
                return {"runner_runtime": "pi"}

        with mock.patch("minecraft_harness.runners.RunnerManager", FakeManager):
            tasks, observer_task, _ = await supervisor._start_runner_agents(
                ["player1"], {"agent_interfaces": {}},
                run_dir / "manifest.json", {}, None)
        try:
            specs = captured["specs"]
            self.assertEqual(specs["player1"]["role"], "player")
            self.assertEqual(specs["player1"]["pi_model"]["kind"], "fake")
            self.assertIn("cli_environment", specs["player1"])
            self.assertEqual(specs["observer"]["role"], "observer")
            self.assertEqual(specs["observer"]["observer"],
                             {"mode": "llm", "interval_s": 30,
                              "allow_infra_requests": True})
            self.assertEqual(specs["observer"]["agent_config"]["interval_s"], 30)
            self.assertEqual(specs["observer"]["pi_model"]["kind"], "fake")
            self.assertEqual(specs["observer"]["cli_environment"], {})
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["runner_runtime"], "pi")
            self.assertEqual(manifest["runner_processes"]["runner_runtime"], "pi")
        finally:
            for task in [tasks, observer_task]:
                for item in (task if isinstance(task, list) else [task]):
                    if item is not None:
                        item.cancel()
            all_tasks = list(tasks) + ([observer_task] if observer_task else [])
            await asyncio.gather(*all_tasks, return_exceptions=True)


class SupervisorModelForwarderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run"
        self.config = validate_config({
            "agents": {"count": 1, "ids": ["player1"]},
            "runtime": {"mode": "fake", "runner_mode": "container",
                        "runner_runtime": "pi",
                        "runner_container_shared_dir": str(self.root / "shared")},
            "model": {"kind": "chat_completions", "model": "fixture-model",
                      "base_url": "https://api.example.test/v1",
                      "api_key_env": "FAKE_API_KEY"},
        })

    def _supervisor(self):
        supervisor = Supervisor(self.config, self.run_dir)
        supervisor.recorder = Recorder(self.run_dir)
        self.addCleanup(supervisor.recorder.close)
        supervisor.budget = types.SimpleNamespace()
        supervisor.gateway = types.SimpleNamespace(
            cli_environment=lambda agent: {"MC_AGENT_ID": agent,
                                           "MC_RUNTIME_SOCKET": "/run/mc-broker/socket"},
            world_epoch=0)
        return supervisor

    @staticmethod
    def _fake_manager(captured, *, fail=False):
        class FakeManager:
            def __init__(self, **kwargs):
                if fail:
                    raise RuntimeError("runner manager construction failed")
                captured.update(kwargs)

            async def start(self):
                return {agent: {"prompt_sha256": "a" * 64,
                                "tools_sha256": "b" * 64, "pid": 1}
                        for agent in captured["specs"]}

            async def wait(self, agent):
                await asyncio.sleep(3600)

            def snapshot(self):
                return {"runner_egress": "model_only"}

        return FakeManager

    async def _cancel(self, tasks, observer_task):
        all_tasks = list(tasks) + ([observer_task] if observer_task else [])
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)

    async def test_start_runner_agents_starts_host_forwarder_and_manifest(self):
        supervisor = self._supervisor()
        captured = {}
        with mock.patch("minecraft_harness.runners.RunnerManager",
                        self._fake_manager(captured)):
            tasks, observer_task, _ = await supervisor._start_runner_agents(
                ["player1"], {"agent_interfaces": {}},
                self.run_dir / "manifest.json", {}, None)
        try:
            forwarder = supervisor._model_forwarder
            self.assertIsNotNone(forwarder)
            socket_path = Path(captured["model_forwarder_socket"])
            self.assertTrue(socket_path.exists())
            self.assertEqual(socket_path.parent, self.run_dir / "model-forwarder")
            self.assertEqual(len(captured["model_forwarder_token"]), 43)
            manifest = json.loads((self.run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["runner_egress"], "model_only")
            self.assertEqual(manifest["model_forwarder"]["mode"], "model_only")
            self.assertEqual(manifest["model_forwarder"]["socket"], str(socket_path))
            self.assertEqual(manifest["model_forwarder"]["container_socket"],
                             "/run/mc-model/socket")
            self.assertFalse(manifest["model_forwarder"]["provider_credential_in_runner"])
        finally:
            await self._cancel(tasks, observer_task)
            if supervisor._model_forwarder is not None:
                await asyncio.to_thread(supervisor._model_forwarder.stop)
                supervisor._model_forwarder = None
        self.assertFalse(socket_path.exists())

    async def test_start_runner_agents_stops_forwarder_when_manager_fails(self):
        supervisor = self._supervisor()
        captured = {}
        with mock.patch("minecraft_harness.runners.RunnerManager",
                        self._fake_manager(captured, fail=True)):
            with self.assertRaises(RuntimeError):
                await supervisor._start_runner_agents(
                    ["player1"], {"agent_interfaces": {}},
                    self.run_dir / "manifest.json", {}, None)
        self.assertIsNone(supervisor._model_forwarder)
        model_forwarder_dir = self.run_dir / "model-forwarder"
        socket_path = model_forwarder_dir / "socket"
        self.assertFalse(socket_path.exists())

    def test_manifest_exposes_runner_egress(self):
        python = Supervisor(validate_config({"runtime": {"mode": "fake"}}),
                            self.run_dir / "python-run")
        self.assertEqual(python._manifest()["runner_egress"], "bridge")
        restricted = Supervisor(self.config, self.run_dir / "pi-run")
        self.assertEqual(restricted._manifest()["runner_egress"], "model_only")




if __name__ == "__main__":
    unittest.main()
