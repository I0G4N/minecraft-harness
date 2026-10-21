"""Offline isolation, preparation and local dashboard boundary regressions."""
import asyncio
from contextlib import closing
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import yaml

from minecraft_harness.config import validate_config
from minecraft_harness.dashboard import artifact_page, create_dashboard, timeline
from minecraft_harness.environment import provision_world
from minecraft_harness.recorder import Recorder
from minecraft_harness.trials import execute_trials, prepare_trials
from minecraft_harness.workspaces import WorkspaceProvisioner, WorkspaceDockerError, WORKSPACE_MAX_FILE_BYTES


class WorkspaceProvisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"provision": True, "image": "fixture:locked", "network": "none"}
        self.provisioner = WorkspaceProvisioner(self.config, self.root, run_id="run-one")
        self.image = {"Id": "sha256:fixture", "Config": {"Env": ["PATH=/usr/bin"], "Cmd": ["sleep", "infinity"], "Entrypoint": None}}
        self.docs = self.root / "reference-docs"
        self.docs.mkdir()

    def existing(self, agent="player1"):
        mounts = [{"Type": "bind", "Source": str(self.root / "workspaces" / agent), "Destination": "/workspace", "RW": True},
                  {"Type": "bind", "Source": str(self.docs), "Destination": "/reference", "RW": False}]
        if agent != "observer":
            mounts.append({"Type": "bind", "Source": str(self.root / "broker"), "Destination": "/run/mc-broker", "RW": False})
        return {"Image": self.image["Id"], "Mounts": mounts,
                "Config": {**copy.deepcopy(self.image["Config"]), "User": f"{os.getuid()}:{os.getgid()}", "Labels": {"minecraft-harness.run": "run-one"}},
                "HostConfig": {"NetworkMode": "none", "Privileged": False, "ReadonlyRootfs": True, "Init": True,
                               "CapDrop": ["ALL"], "CapAdd": [], "SecurityOpt": ["no-new-privileges:true"],
                               "PidMode": "", "IpcMode": "private", "Memory": 512 * 1024 ** 2,
                               "PidsLimit": 64, "NanoCpus": 1000000000,
                               "Ulimits": [{"Name": "fsize", "Soft": WORKSPACE_MAX_FILE_BYTES, "Hard": WORKSPACE_MAX_FILE_BYTES}], "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=64m"}}}

    def verify(self, info, agent="player1"):
        self.provisioner._verify_existing(info, self.image, self.root / "workspaces" / agent,
                                          self.docs, self.root / "broker", agent)

    async def test_new_players_get_only_own_workspace_docs_and_readonly_uds(self):
        commands = []
        async def docker(*args, **kwargs):
            commands.append(args)
            if args[:2] == ("image", "inspect"):
                return json.dumps([self.image])
            if args[0] == "inspect":
                raise WorkspaceDockerError("container not found", not_found=True)
            return "fixture"
        with patch.object(self.provisioner, "_docker", docker), patch.object(self.provisioner, "_documentation", return_value=self.docs):
            names = await self.provisioner.provision(["player1", "observer"])
        self.assertEqual(set(names), {"player1", "observer"})
        self.assertNotEqual(*names.values())
        creates = [args for args in commands if args[0] == "run"]
        self.assertEqual(len(creates), 2)
        for command in creates:
            self.assertIn("--init", command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertEqual(command[command.index("--ulimit") + 1], f"fsize={WORKSPACE_MAX_FILE_BYTES}:{WORKSPACE_MAX_FILE_BYTES}")
            self.assertEqual(command[command.index("--network") + 1], "none")
            joined = " ".join(command)
            self.assertNotIn("docker.sock", joined)
            self.assertNotIn("events.sqlite3", joined)
            self.assertNotIn("agents/", joined)
        player, observer = creates
        self.assertIn("target=/run/mc-broker,readonly", " ".join(player))
        self.assertNotIn("/run/mc-broker", " ".join(observer))
        self.assertNotIn(str(self.root / "workspaces/player1"), " ".join(observer))
        self.assertTrue(any(args[0] == "exec" and args[-3:] == ("git", "init", "-q") for args in commands))

    async def test_valid_existing_container_is_reused_without_changing_boundaries(self):
        info = self.existing()
        commands = []
        async def docker(*args, **kwargs):
            commands.append(args)
            return json.dumps([self.image if args[:2] == ("image", "inspect") else info]) if args[0] in {"image", "inspect"} else ""
        with patch.object(self.provisioner, "_docker", docker), patch.object(self.provisioner, "_documentation", return_value=self.docs):
            await self.provisioner.provision(["player1"])
        self.assertTrue(any(command[0] == "start" for command in commands))
        self.assertFalse(any(command[0] == "run" for command in commands))

    async def test_resume_fences_old_workspace_process_namespace_before_reuse(self):
        info = self.existing()
        info['State'] = {'Running': True}
        commands = []
        async def docker(*args, **kwargs):
            commands.append(args)
            if args[:2] == ('image', 'inspect'):
                return json.dumps([self.image])
            if args[:2] == ('inspect', '--format'):
                return 'false\n'
            if args[0] == 'inspect':
                return json.dumps([info])
            return ''
        with patch.object(self.provisioner, '_docker', docker), patch.object(self.provisioner, '_documentation', return_value=self.docs):
            await self.provisioner.provision(['player1'])
        stop = next(index for index, command in enumerate(commands) if command[0] == 'stop')
        start = next(index for index, command in enumerate(commands) if command[0] == 'start')
        self.assertLess(stop, start)
        self.assertFalse(any(command[0] == 'run' for command in commands))

    async def test_stale_privileged_host_network_or_extra_mount_is_never_adopted(self):
        variants = []
        for key, value in (("Privileged", True), ("NetworkMode", "host"), ("ReadonlyRootfs", False),
                           ("Init", False), ("CapAdd", ["SYS_ADMIN"]), ("PidMode", "host"), ("PidsLimit", -1), ("Ulimits", [])):
            info = self.existing(); info["HostConfig"][key] = value; variants.append(info)
        info = self.existing(); info["Mounts"].append({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True}); variants.append(info)
        info = self.existing(); info["Config"]["Env"] += ["MODEL_API_KEY=secret"]; variants.append(info)
        info = self.existing(); info["Image"] = "sha256:changed"; variants.append(info)
        info = self.existing("observer"); info["Mounts"].append({"Type": "bind", "Source": str(self.root / "broker"), "Destination": "/run/mc-broker", "RW": False})
        with self.assertRaises(RuntimeError):
            self.verify(info, "observer")
        for info in variants:
            with self.subTest(info=info):
                with self.assertRaisesRegex(RuntimeError, "isolation mismatch"):
                    self.verify(info)

    async def test_detached_workspace_created_before_lost_ack_is_still_cleaned(self):
        containers = set()
        async def docker(*args, **kwargs):
            if args[:2] == ("image", "inspect"):
                return json.dumps([self.image])
            if args[0] == "inspect":
                if args[1] in containers:
                    return json.dumps([self.existing()])
                raise WorkspaceDockerError("not found", not_found=True)
            if args[0] == "run":
                containers.add(args[args.index("--name") + 1])
                raise asyncio.CancelledError
            if args[0] == "rm":
                containers.remove(args[-1])
            return ""
        with patch.object(self.provisioner, "_docker", docker), patch.object(self.provisioner, "_documentation", return_value=self.docs):
            with self.assertRaises(asyncio.CancelledError):
                await self.provisioner.provision(["player1"])
            self.assertTrue(containers)
            await self.provisioner.close()
        self.assertEqual(containers, set())

    async def test_unknown_inspection_failure_never_adopts_or_removes_container(self):
        commands = []
        async def docker(*args, **kwargs):
            commands.append(args)
            if args[:2] == ("image", "inspect"):
                return json.dumps([self.image])
            raise RuntimeError("inspection response lost")
        with patch.object(self.provisioner, "_docker", docker), patch.object(self.provisioner, "_documentation", return_value=self.docs):
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                await self.provisioner.provision(["player1"])
            await self.provisioner.close()
        self.assertFalse(self.provisioner.owned)
        self.assertFalse(any(command[0] in {"run", "start", "rm"} for command in commands))

    async def test_network_and_path_escape_identities_are_rejected(self):
        with self.assertRaises(ValueError):
            WorkspaceProvisioner({**self.config, "network": "host"}, self.root, run_id="run")
        with patch.object(self.provisioner, "_docker", AsyncMock(return_value=json.dumps([self.image]))):
            with self.assertRaises(ValueError):
                await self.provisioner.provision(["../outside"])


    async def test_docker_missing_object_error_is_case_insensitive_without_hiding_other_errors(self):
        for message, missing in ((b"error: no such object: fixture", True),
                                 (b"Error: No such container: fixture", True),
                                 (b"permission denied while contacting Docker", False)):
            class Process:
                returncode = 1
                def __init__(self):
                    self.stdout = asyncio.StreamReader(); self.stdout.feed_eof()
                    self.stderr = asyncio.StreamReader(); self.stderr.feed_data(message); self.stderr.feed_eof()
                async def wait(self): return 1
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=Process())):
                with self.assertRaises(WorkspaceDockerError) as caught:
                    await self.provisioner._docker("inspect", "fixture")
                self.assertEqual(caught.exception.not_found, missing)


class TrialPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = validate_config({"experiment": {"profile": "paper_aligned", "seed": None},
            "runtime": {"mode": "minecraft", "lifecycle": "paper_aligned", "url": "http://old-runtime.invalid:9000"},
            "workspace": {"provision": True}, "observer": {"mode": "llm"},
            "server": {"world_dir": "/placeholder", "require_fresh_world": True}})

    async def test_prepare_has_independent_worlds_and_uses_matching_runtime_not_old_url(self):
        manifest = prepare_trials(self.config, self.root / "batch", [None, 42], repetitions=2)
        self.assertFalse(manifest["eula_accepted_by_preparation"])
        self.assertEqual(len(manifest["trials"]), 4)
        self.assertEqual(len({item["server_data"] for item in manifest["trials"]}), 4)
        self.assertEqual(len({item["project"] for item in manifest["trials"]}), 4)
        for item in manifest["trials"]:
            data = Path(item["server_data"])
            config = yaml.safe_load(Path(item["config"]).read_text())
            self.assertFalse((data / "eula.txt").exists())
            self.assertFalse((data / "world").exists())
            self.assertFalse(Path(item["run_dir"]).exists())
            self.assertIsNone(config["runtime"]["url"])
            self.assertEqual(config["runtime"]["urls"]["player1"], "http://127.0.0.1:3101")
            self.assertEqual(config["runtime"]["token_envs"]["player4"], "BOT4_ADMIN_TOKEN")
            self.assertNotIn("old-runtime.invalid", Path(item["config"]).read_text())
        self.assertEqual(self.config["runtime"]["url"], "http://old-runtime.invalid:9000")
        self.assertEqual(manifest["max_batch_model_calls"], self.config["budgets"]["total_model_calls"] * 4)

    async def test_no_eula_never_invokes_docker_or_trial_and_preserves_preparation(self):
        manifest = prepare_trials(self.config, self.root / "batch", [None])
        run_trial = AsyncMock()
        with patch.dict(os.environ, {"ACCEPT_EULA": "false"}), patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaisesRegex(ValueError, "EULA"):
                await execute_trials(manifest, self.root / "batch", run_trial)
        spawn.assert_not_called(); run_trial.assert_not_called()
        self.assertEqual(manifest["trials"][0]["status"], "prepared")

    async def test_invalid_seed_or_custom_identity_has_no_preparation_side_effects(self):
        for seed in (True, 2 ** 63, "random"):
            destination = self.root / str(seed)
            with self.assertRaises(ValueError):
                prepare_trials(self.config, destination, [seed])
            self.assertFalse(destination.exists())
        changed = copy.deepcopy(self.config)
        changed["agents"]["ids"] = ["a", "b", "c", "d"]
        with self.assertRaisesRegex(ValueError, "player1"):
            prepare_trials(changed, self.root / "custom", [42])
        self.assertFalse((self.root / "custom").exists())
        with self.assertRaises(ValueError):
            provision_world(self.root / "invalid-world", seed=True)
        self.assertFalse((self.root / "invalid-world").exists())

    async def test_simulated_batch_uses_uid_stops_each_project_and_refuses_replay(self):
        directory = self.root / "batch"
        manifest = prepare_trials(self.config, directory, [42, 43])
        commands = []
        class Process:
            returncode = 0
            async def wait(self): return 0
        async def spawn(*args, **kwargs):
            commands.append((args, kwargs))
            return Process()
        async def run_trial(config, run_dir):
            self.assertTrue(config["server"]["require_fresh_world"])
            return {"status": "completed"}
        with patch.dict(os.environ, {"ACCEPT_EULA": "true"}), patch("asyncio.create_subprocess_exec", spawn), patch("minecraft_harness.trials.ServerControl.ready", AsyncMock(return_value=True)):
            result = await execute_trials(manifest, directory, run_trial)
            with self.assertRaisesRegex(ValueError, "never be silently replayed"):
                await execute_trials(result, directory, run_trial)
        self.assertEqual([item["status"] for item in result["trials"]], ["completed", "completed"])
        self.assertEqual(sum("down" in args for args, _ in commands), 2)
        self.assertTrue(all(kwargs["env"]["MC_UID"] == str(os.getuid()) and kwargs["env"]["MC_GID"] == str(os.getgid()) for _, kwargs in commands))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recorder = Recorder(self.root); self.addCleanup(self.recorder.close)
        self.controls = []
        self.status = {"status": "running", "supervisor_active": True}
        self.server = create_dashboard(self.root, 0, lambda path: self.status, lambda path, command: self.controls.append(command) or "control-one")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_port
        code, page = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.token = re.search(r'<meta name="operator-token" content="([^"]+)">', page).group(1)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def auth(self, **extra):
        return {"Content-Type": "application/json", "X-Control-Token": self.token, **extra}

    def test_readonly_pages_are_bounded_paginated_and_actor_scoped(self):
        for index in range(40):
            self.recorder.append("chat", {"value": "x" * 20000}, actor_id="player1" if index % 2 else "player2")
        count = self.recorder.latest_event_seq()
        page = timeline(self.root, 0, 100, "player1")
        self.assertTrue(page["events"])
        self.assertLess(len(page["events"]), 20)
        self.assertTrue(all(event["actor_id"] == "player1" for event in page["events"]))
        self.assertLess(len(json.dumps(page).encode()), 260000)
        next_page = timeline(self.root, page["next_cursor"], 100, "player1")
        self.assertTrue(all(event["event_seq"] > page["next_cursor"] for event in next_page["events"]))
        code, body = self.request("GET", "/api/events?limit=100&actor=player1")
        self.assertEqual(code, 200)
        self.assertLess(len(body.encode()), 260000)
        self.assertEqual(self.recorder.latest_event_seq(), count)
        self.assertEqual(self.controls, [])
        for limit in (0, 101, True):
            with self.assertRaises(ValueError): timeline(self.root, 0, limit)

    def test_control_requires_token_same_origin_and_object_body(self):
        payload = json.dumps({"command": "stop"})
        self.assertEqual(self.request("POST", "/api/control", payload, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.request("POST", "/api/control", payload, {"Content-Type": "application/json", "X-Control-Token": "ÿ" * 43})[0], 403)
        self.assertEqual(self.request("POST", "/api/control", payload, self.auth(Origin="https://foreign.example"))[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": "foreign.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/control", "[]", self.auth())[0], 400)
        self.assertEqual(self.request("POST", "/api/control", payload, self.auth(**{"Content-Length": "5000"}))[0], 400)
        self.assertEqual(self.controls, [])
        self.assertEqual(self.request("POST", "/api/control", payload, self.auth(Origin=f"http://127.0.0.1:{self.port}"))[0], 202)
        self.assertEqual(self.controls, ["stop"])
        self.assertEqual(self.request("GET", "/api/status")[0], 200)

    def test_artifact_reads_validate_integrity_limits_and_symlinks(self):
        ref = self.recorder.put_artifact(b"0123456789" * 10000)
        page = artifact_page(self.root, ref["hash"], 3, 17)
        self.assertEqual(page["content"], "34567890123456789")
        self.assertEqual(page["next_offset"], 20)
        for limit in (True, 0, 64001, 1.5):
            with self.assertRaises(ValueError): artifact_page(self.root, ref["hash"], limit=limit)
        target = self.recorder.artifact_dir / ref["hash"]
        target.unlink(); target.symlink_to(self.root / "events.sqlite3")
        with self.assertRaises(ValueError): artifact_page(self.root, ref["hash"])
        self.assertEqual(self.request("GET", "/api/artifact/" + ref["hash"])[0], 400)

    def test_oversized_status_is_rejected_with_small_response(self):
        self.status["unbounded"] = "x" * 1000000
        code, body = self.request("GET", "/api/status")
        self.assertEqual(code, 413)
        self.assertLess(len(body), 200)
