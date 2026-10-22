"""Dynamic exact-N-agent deployment generation and runner-count regressions."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from minecraft_harness.config import load_config, validate_config
from minecraft_harness.deployment_generation import (
    MAX_AGENTS, DeploymentGenerationError, agent_ids, bot_port, render_compose,
    render_config, render_env, viewer_port, worker_subnet,
)
from minecraft_harness.recorder import Recorder
from minecraft_harness.supervisor import Supervisor

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_COMPOSE = ROOT / "infra" / "compose.paper.yaml"
TEMPLATE_CONFIG = ROOT / "configs" / "minecraft-paper-runner-20261022.yaml"


def _docker_compose_available():
    try:
        return subprocess.run(["docker", "compose", "version"],
                              capture_output=True, text=True).returncode == 0
    except OSError:
        return False


class AgentCountConfigTests(unittest.TestCase):
    def test_config_accepts_one_through_sixteen_agents(self):
        for count in (1, 2, 3, 4, 5, 6, 8, 12, 16):
            with self.subTest(count=count):
                config = validate_config({"agents": {"count": count}})
                self.assertEqual(config["agents"]["count"], count)
                self.assertEqual(config["agents"]["ids"],
                                 [f"player{i}" for i in range(1, count + 1)])

    def test_invalid_agent_counts_are_rejected_before_run(self):
        for count in (0, 17, -1, 3.0, True, False, "4", None):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    validate_config({"agents": {"count": count}})

    def test_paper_profile_still_requires_exactly_four_agents(self):
        valid = {
            "agents": {"count": 4},
            "experiment": {"profile": "paper_aligned"},
            "runtime": {"lifecycle": "paper_aligned"},
            "server": {"save_interval_s": 300},
            "observer": {"mode": "llm"},
            "workspace": {"provision": True},
        }
        self.assertEqual(validate_config(valid)["agents"]["count"], 4)
        for count in (1, 3, 5, 6):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "four players"):
                    validate_config({**valid, "agents": {"count": count}})


class ComposeRenderingTests(unittest.TestCase):
    def services(self, rendered):
        names = set(rendered["services"])
        expected = {"server"}
        for index in range(1, rendered["_agents"] + 1):
            expected.update({f"identity{index}", f"launcher{index}", f"bot{index}"})
        self.assertEqual(names, expected)

    def test_exact_agent_count_for_one_through_sixteen_agents(self):
        for count in (1, 3, 4, 6, 16):
            with self.subTest(count=count):
                rendered = render_compose(TEMPLATE_COMPOSE, count, f"mc-dynamic-{count}")
                rendered["_agents"] = count
                self.services(rendered)
                self.assertEqual(rendered["name"], f"mc-dynamic-{count}")
                self.assertEqual(
                    rendered["services"]["server"]["environment"]["MAX_PLAYERS"],
                    str(count))
                self.assertEqual(
                    set(name for name in rendered["networks"]
                        if name.startswith("worker")),
                    {f"worker{i}" for i in range(1, count + 1)})
                for index in range(1, count + 1):
                    mode = "paper" if count == 4 else "strict"
                    self.check_agent(rendered, index, count, mode)

    def check_agent(self, rendered, index, count, mode):
        player = f"player{index}"
        viewer = viewer_port(index, count)
        identity = rendered["services"][f"identity{index}"]
        self.assertEqual(identity["environment"]["ALLOWED_USERNAME"], player)
        self.assertEqual(identity["environment"]["VIEWER_PROXY_PORT"], str(viewer))
        self.assertEqual(identity["environment"]["VIEWER_TARGET"],
                         f"{worker_subnet(index).split('/')[0].rsplit('.', 1)[0]}.2")
        self.assertEqual(identity["ports"], [f"127.0.0.1:{viewer}:{viewer}"])
        self.assertEqual(identity["networks"][f"worker{index}"]["ipv4_address"],
                         f"{worker_subnet(index).split('/')[0].rsplit('.', 1)[0]}.1")

        launcher = rendered["services"][f"launcher{index}"]
        self.assertEqual(launcher["environment"]["WORKER_AGENT_ID"], player)
        self.assertEqual(launcher["environment"]["WORKER_HOST_TOKEN"],
                         f"${{WORKER{index}_TOKEN:?Set unique WORKER{index}_TOKEN}}")
        self.assertEqual(launcher["environment"]["WORKER_NETWORK"],
                         f"mc-dynamic-{count}_worker{index}")
        self.assertEqual(launcher["environment"]["WORKER_VIEWER_PORT"], str(viewer))
        self.assertEqual(launcher["depends_on"], [f"identity{index}"])

        bot = rendered["services"][f"bot{index}"]
        self.assertEqual(bot["environment"]["ALLOWED_AGENT_ID"], player)
        self.assertEqual(bot["environment"]["ALLOWED_USERNAME"], player)
        self.assertEqual(bot["environment"]["TOKEN"],
                         f"${{BOT{index}_ADMIN_TOKEN:?Set BOT{index}_ADMIN_TOKEN}}")
        endpoints = json.loads(bot["environment"]["WORKER_ENDPOINTS_JSON"])
        self.assertEqual(endpoints, {player: {
            "endpoint": f"tcp://launcher{index}:3901",
            "token": f"${{WORKER{index}_TOKEN}}"}})
        self.assertEqual(bot["ports"], [f"127.0.0.1:{bot_port(index)}:3001"])
        expected_lifecycle = "paper_aligned" if mode == "paper" else "strict"
        self.assertEqual(bot["environment"]["LIFECYCLE"], expected_lifecycle)
        self.assertEqual(bot["environment"]["PAPER_SINGLE_PROGRAM"],
                         "true" if mode == "paper" else "false")
        self.assertEqual(bot["depends_on"], [f"launcher{index}"])

    def test_no_stale_services_or_cross_agent_references(self):
        rendered = render_compose(TEMPLATE_COMPOSE, 3, "mc-dynamic-3")
        self.assertNotIn("identity4", rendered["services"])
        self.assertNotIn("bot4", rendered["services"])
        self.assertNotIn("worker4", rendered["networks"])
        for index in (1, 2, 3):
            bot = rendered["services"][f"bot{index}"]
            self.assertNotIn(f"player{index + 1}", json.dumps(bot))
            self.assertIn(f"launcher{index}", bot["environment"]["WORKER_ENDPOINTS_JSON"])
            self.assertNotIn(f"launcher{index + 1}",
                             bot["environment"]["WORKER_ENDPOINTS_JSON"])

    def test_worker_networks_are_distinct_and_do_not_overlap(self):
        rendered = render_compose(TEMPLATE_COMPOSE, MAX_AGENTS, "mc-dynamic-16")
        subnets = [ipaddress.ip_network(rendered["networks"][f"worker{i}"]["ipam"]["config"][0]["subnet"])
                   for i in range(1, MAX_AGENTS + 1)]
        self.assertEqual(len(subnets), len(set(subnets)))
        for index, subnet in enumerate(subnets):
            for other in subnets[index + 1:]:
                self.assertFalse(subnet.overlaps(other), (subnet, other))
        for index in range(1, 5):
            self.assertEqual(str(subnets[index - 1]), worker_subnet(index))
        for index in range(5, MAX_AGENTS + 1):
            self.assertTrue(str(subnets[index - 1]).startswith("172.31."))
        # The generated networks also must not collide with the static
        # world/control subnets from the template.
        world = ipaddress.ip_network(
            rendered["networks"]["world"]["ipam"]["config"][0]["subnet"])
        control = ipaddress.ip_network(
            rendered["networks"]["control"]["ipam"]["config"][0]["subnet"])
        for subnet in subnets:
            self.assertFalse(subnet.overlaps(world))
            self.assertFalse(subnet.overlaps(control))

    def test_paper_mode_requires_four_agents_and_strict_can_override(self):
        with self.assertRaises(DeploymentGenerationError):
            render_compose(TEMPLATE_COMPOSE, 3, "mc-dynamic-3", mode="paper")
        rendered = render_compose(TEMPLATE_COMPOSE, 4, "mc-dynamic-4", mode="strict")
        for index in range(1, 5):
            bot = rendered["services"][f"bot{index}"]["environment"]
            self.assertEqual(bot["LIFECYCLE"], "strict")
            self.assertEqual(bot["LEFTOVER_POLICY"], "reset")
            self.assertEqual(bot["CHAT_DELIVERY"], "events")
            self.assertEqual(bot["PAPER_SINGLE_PROGRAM"], "false")
        paper = render_compose(TEMPLATE_COMPOSE, 4, "mc-dynamic-4")
        for index in range(1, 5):
            bot = paper["services"][f"bot{index}"]["environment"]
            self.assertEqual(bot["LIFECYCLE"], "paper_aligned")
            self.assertEqual(bot["LEFTOVER_POLICY"], "allow")
            self.assertEqual(bot["CHAT_DELIVERY"], "program_output")
            self.assertEqual(bot["PAPER_SINGLE_PROGRAM"], "true")


class ConfigAndEnvironmentRenderingTests(unittest.TestCase):
    def test_generated_strict_config_matches_the_rendered_lifecycle(self):
        config = render_config(TEMPLATE_CONFIG, 6, "mc-dynamic-6",
                               server_data_dir=ROOT / "infra" / "server-data",
                               config_path=ROOT / "configs" / "mc-dynamic-6.yaml")
        self.assertEqual(config["agents"]["count"], 6)
        self.assertEqual(config["agents"]["ids"], agent_ids(6))
        self.assertEqual(config["experiment"]["profile"], "strict")
        self.assertEqual(config["runtime"]["lifecycle"], "strict")
        self.assertEqual(config["runtime"]["leftover_policy"], "reset")
        self.assertEqual(config["collaboration"]["chat_delivery"], "events")
        self.assertFalse(config["agents"]["paper_single_program"])
        self.assertLessEqual(config["runtime"]["program_timeout_max_s"], 600)
        self.assertEqual(config["runtime"]["runner_mode"], "container")
        self.assertEqual(config["server"]["managed_container"], "mc-dynamic-6-server")
        self.assertEqual(config["runtime"]["urls"], {
            f"player{i}": f"http://127.0.0.1:{bot_port(i)}" for i in range(1, 7)})
        self.assertEqual(config["runtime"]["token_envs"], {
            f"player{i}": f"BOT{i}_ADMIN_TOKEN" for i in range(1, 7)})
        self.assertEqual(config["runtime"]["viewer_ports"], {
            f"player{i}": viewer_port(i, 6) for i in range(1, 7)})
        self.assertNotIn("api_key", config["model"])

    def test_generated_four_player_config_keeps_paper_aligned(self):
        config = render_config(TEMPLATE_CONFIG, 4, "mc-dynamic-4",
                               server_data_dir=ROOT / "infra" / "server-data",
                               config_path=ROOT / "configs" / "mc-dynamic-4.yaml")
        self.assertEqual(config["experiment"]["profile"], "paper_aligned")
        self.assertEqual(config["runtime"]["lifecycle"], "paper_aligned")
        self.assertEqual(config["runtime"]["leftover_policy"], "allow")
        self.assertEqual(config["collaboration"]["chat_delivery"], "program_output")
        self.assertTrue(config["agents"]["paper_single_program"])
        self.assertEqual(config["runtime"]["viewer_ports"]["player1"], 3105)
        self.assertEqual(config["runtime"]["viewer_ports"]["player4"], 3108)

    def test_generated_config_writes_relative_server_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "configs" / "mc.yaml"
            server_data = Path(directory) / "infra" / "server-data"
            config = render_config(TEMPLATE_CONFIG, 2, "mc-dynamic-2",
                                   server_data_dir=server_data, config_path=target)
            self.assertEqual(config["server"]["world_dir"], "../infra/server-data/world")
            self.assertEqual(config["server"]["log_path"],
                             "../infra/server-data/logs/latest.log")
            target.parent.mkdir(parents=True)
            target.write_text(yaml.safe_dump(config), encoding="utf-8")
            loaded = load_config(target)
            self.assertEqual(loaded["agents"]["count"], 2)
            self.assertEqual(loaded["runtime"]["urls"]["player2"],
                             "http://127.0.0.1:3102")

    def test_env_has_unique_tokens_only_for_the_requested_count(self):
        text = render_env(6, "mc-dynamic-6", server_data_dir="/tmp/mc-server",
                          seed=123, uid=1000, gid=1000)
        lines = dict(line.split("=", 1) for line in text.splitlines()
                     if line and not line.startswith("#"))
        self.assertEqual(lines["HARNESS_PROJECT"], "mc-dynamic-6")
        self.assertEqual(lines["MC_SERVER_CONTAINER"], "mc-dynamic-6-server")
        self.assertEqual(lines["MC_SEED"], "123")
        self.assertNotIn("DEEPSEEK_API_KEY", lines)
        worker_tokens = [lines[f"WORKER{i}_TOKEN"] for i in range(1, 7)]
        bot_tokens = [lines[f"BOT{i}_ADMIN_TOKEN"] for i in range(1, 7)]
        self.assertEqual(len(set(worker_tokens)), 6)
        self.assertEqual(len(set(bot_tokens)), 6)
        self.assertFalse(set(worker_tokens) & set(bot_tokens))
        for index in range(1, 7):
            parsed = json.loads(lines[f"BOT{index}_AGENT_TOKENS_JSON"])
            self.assertEqual(set(parsed), {f"player{index}"})
        self.assertNotIn("WORKER7_TOKEN", lines)
        self.assertNotIn("BOT7_ADMIN_TOKEN", lines)

    def test_env_can_optionally_include_the_model_key(self):
        text = render_env(1, "mc-one", api_key="sk-fixture")
        self.assertIn("DEEPSEEK_API_KEY=sk-fixture", text)


class PrepareDeploymentCliTests(unittest.TestCase):
    def run_cli(self, directory, agents=6, **overrides):
        command = [
            sys.executable, str(ROOT / "scripts" / "prepare_deployment.py"),
            "--agents", str(agents),
            "--project", f"mc-dynamic-{agents}",
            "--compose-output", str(Path(directory) / "compose.yaml"),
            "--config-output", str(Path(directory) / "config.yaml"),
            "--env-output", str(Path(directory) / "env"),
            "--server-data", str(Path(directory) / "server-data"),
        ]
        for key, value in overrides.items():
            if key in {"force", "accept_eula"} and value is True:
                command.append("--" + key.replace("_", "-"))
            else:
                command.extend(["--" + key.replace("_", "-"), str(value)])
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

    def test_cli_writes_compose_config_and_private_env_without_starting_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = self.run_cli(directory)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("agents=6", completed.stdout)
            self.assertIn("runner containers per run: 7", completed.stdout)
            self.assertIn("docker compose", completed.stdout)
            env_path = Path(directory) / "env"
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            compose = yaml.safe_load((Path(directory) / "compose.yaml").read_text())
            self.assertEqual(len([name for name in compose["services"]
                                  if name.startswith("bot")]), 6)
            self.assertNotIn("bot7", compose["services"])
            config = yaml.safe_load((Path(directory) / "config.yaml").read_text())
            self.assertEqual(config["agents"]["count"], 6)
            self.assertEqual(config["runtime"]["runner_mode"], "container")
            self.assertNotIn("sk-", (Path(directory) / "config.yaml").read_text())

    def test_cli_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.run_cli(directory)
            self.assertEqual(first.returncode, 0, first.stderr)
            paths = [Path(directory) / name for name in ("compose.yaml", "config.yaml", "env")]
            before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
            second = self.run_cli(directory)
            self.assertEqual(second.returncode, 2)
            self.assertIn("refusing to overwrite", second.stderr)
            after = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
            self.assertEqual(before, after)
            third = self.run_cli(directory, force=True)
            self.assertEqual(third.returncode, 0, third.stderr)

    def test_cli_rejects_invalid_counts_modes_and_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            for agents in (0, 17):
                completed = self.run_cli(directory, agents=agents)
                self.assertEqual(completed.returncode, 2)
            for extra in ({"mode": "paper"}, {"project": "UPPER"}):
                agents = 3 if extra.get("mode") == "paper" else 4
                # Run against fresh paths so an earlier failure cannot mask it.
                with tempfile.TemporaryDirectory() as other:
                    completed = self.run_cli(other, agents=agents, **extra)
                self.assertEqual(completed.returncode, 2)

    @unittest.skipUnless(_docker_compose_available(),
                         "docker compose plugin not available")
    def test_generated_compose_passes_docker_compose_config(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = self.run_cli(directory)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            check = subprocess.run(
                ["docker", "compose", "-p", "mc-dynamic-6",
                 "-f", str(Path(directory) / "compose.yaml"),
                 "--env-file", str(Path(directory) / "env"),
                 "config", "--quiet"],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)


class DynamicRunnerCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_configured_agents_start_three_independent_runners(self):
        config = load_config(ROOT / "configs" / "offline-smoke.yaml")
        config["runtime"]["runner_mode"] = "process"
        config["agents"]["count"] = 3
        config["agents"]["ids"] = ["player1", "player2", "player3"]
        config["agents"]["max_turns"] = 2
        config["budgets"]["total_model_calls"] = 12
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            result = await Supervisor(config, run_dir).run()
            self.assertNotEqual(result["status"], "infra_failed")
            recorder = Recorder(run_dir)
            try:
                events = list(recorder.all_events())
            finally:
                recorder.close()
            started = {event["payload"]["agent_id"] for event in events
                       if event["kind"] == "runner_process_started"}
            self.assertEqual(started, {"player1", "player2", "player3"})
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["runner_mode"], "process")
            self.assertEqual(set(manifest["agent_interfaces"]),
                             {"player1", "player2", "player3"})
            self.assertEqual(len({event["payload"]["pid"] for event in events
                                  if event["kind"] == "runner_process_started"}), 3)


if __name__ == "__main__":
    unittest.main()
