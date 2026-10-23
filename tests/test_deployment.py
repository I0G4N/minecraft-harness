import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from minecraft_harness.deployment import capture_deployment, DeploymentInspectionError, _docker_projection, _CONTAINER
from minecraft_harness.deployment_generation import (DeploymentGenerationError, render_compose, render_config, resolve_mode)

ROOT = Path(__file__).resolve().parents[1]

SERVER_IMAGE = 'sha256:' + '1' * 64
WORKSPACE_IMAGE = 'sha256:' + '2' * 64
WORKER_IMAGE = 'sha256:' + '3' * 64
CONTROL_IMAGE = 'sha256:' + '4' * 64
LAUNCHER_IMAGE = 'sha256:' + '5' * 64


def container_id(name):
    return hashlib.sha256(name.encode()).hexdigest()


def container(name, image, run='run1', service=None):
    return {'Id': container_id(name), 'Name': '/' + name, 'Image': image, 'ConfigImage': name + ':mutable-tag',
            'User': '1000:1000', 'RunLabel': run, 'ReadonlyRootfs': True, 'Privileged': False,
            'ComposeProject': 'own-project' if service else None, 'ComposeService': service,
            'NetworkMode': 'none', 'Networks': {}, 'PublishedPorts': {}, 'PidMode': '', 'IpcMode': 'private', 'CapDrop': ['ALL'],
            'SecurityOpt': ['no-new-privileges:true'], 'Memory': 536870912, 'PidsLimit': 64, 'NanoCpus': 1000000000,
            'Mounts': [{'Type': 'bind', 'Source': '/private/world', 'Destination': '/data', 'RW': True}],
            'GameChat': 'GAME_CHAT=false', 'MinecraftHost': 'MC_HOST=server', 'MinecraftPort': 'MC_PORT=25565', 'AllowedUsername': 'ALLOWED_USERNAME=alice',
            'Config': {'Env': ['MODEL_API_KEY=must-never-be-recorded']}, 'Password': 'must-never-be-recorded'}


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        config = {'runtime': {'mode': 'minecraft'}, 'experiment': {'profile': 'paper_aligned'},
                  'server': {'managed_container': 'own-server'}, 'collaboration': {'game_chat': False}}
        session = {'agent_id': 'alice', 'username': 'alice', 'run_id': 'run1', 'session_id': 's1',
                   'world_epoch': 1, 'runtime_epoch': 1, 'worker_transport': 'remote',
                   'native_sandbox': {'native_filter': True, 'version': 1, 'token': 'native-secret'},
                   'execution_boundary': {'image_id': WORKER_IMAGE, 'network': 'own-worker-front', 'identity_bound': True, 'separate_container': True, 'token': 'launcher-secret'},
                   'module_policy': {'allowed': ['vec3'], 'token': 'policy-secret'},
                   'dependencies': {'mineflayer': '4.35.0'}, 'versions': {'node': '22.19.0'}, 'token': 'runtime-secret'}
        members = {'own-server': (SERVER_IMAGE, 'server'), 'own-workspace': (WORKSPACE_IMAGE, None),
                   'own-worker': (WORKER_IMAGE, None), 'own-control': (CONTROL_IMAGE, 'bot1'),
                   'own-launcher': (LAUNCHER_IMAGE, 'launcher1'), 'own-identity': (CONTROL_IMAGE, 'identity1')}
        aliases = {container_id(name): name for name in members}
        async def inspect(kind, identity):
            if kind == 'container':
                name = aliases.get(identity, identity)
                image, service = members[name]
                result = container(name, image, service=service)
                if name == 'own-worker':
                    result.update(NetworkMode='own-worker-front', Networks={'own-worker-front': {'IPAddress': '172.31.0.2'}}, Mounts=[])
                return result
            if kind == 'project':
                self.assertEqual(identity, 'own-project')
                return {'container_ids': [container_id(name) for name, (_, service) in members.items() if service]}
            if kind == 'network':
                return {'Id': 'b' * 64, 'Name': 'own-worker-front', 'Internal': True, 'EnableIPv6': False,
                        'Options': {'com.docker.network.bridge.gateway_mode_ipv4': 'isolated'}, 'IPAM': [{'Subnet': '172.31.0.0/16'}],
                        'Containers': {container_id(name): {'IPv4Address': address, 'IPv6Address': ''} for name, address in [('own-worker', '172.31.0.2/16'), ('own-identity', '172.31.0.3/16')]}}
            return {'Id': identity, 'RepoDigests': [] if identity == WORKER_IMAGE else ['registry/research@' + identity], 'Os': 'linux', 'Architecture': 'amd64'}
        return config, session, AsyncMock(side_effect=inspect)

    async def test_only_declared_project_containers_images_networks_and_public_boundary_are_persisted(self):
        config, session, inspect = self.fixture()
        with patch('minecraft_harness.deployment._docker_projection', inspect):
            result = await capture_deployment(config, {'alice': session}, {'alice': 'own-workspace'}, run_id='run1')
        self.assertTrue(result['verified']); self.assertTrue(result['operator_only'])
        self.assertEqual(result['server']['image']['image_id'], SERVER_IMAGE)
        self.assertEqual(result['server']['configured_image'], 'own-server:mutable-tag')
        self.assertEqual(result['server']['mounts'][0]['RW'], True)
        self.assertEqual(result['workspaces']['alice']['image']['image_id'], WORKSPACE_IMAGE)
        self.assertEqual(result['runtime_sessions']['alice']['execution_boundary']['image_id'], WORKER_IMAGE)
        self.assertEqual(result['runtime_sessions']['alice']['dependencies'], {'mineflayer': '4.35.0'})
        self.assertEqual(set(result['images']), {SERVER_IMAGE, WORKSPACE_IMAGE, WORKER_IMAGE, CONTROL_IMAGE, LAUNCHER_IMAGE})
        self.assertEqual(set(result['project_containers']), {'own-server', 'own-control', 'own-launcher', 'own-identity'})
        self.assertEqual(result['project_containers']['own-identity']['selected_public_environment'], {'GAME_CHAT': 'false', 'MC_HOST': 'server', 'MC_PORT': '25565', 'ALLOWED_USERNAME': 'alice'})
        self.assertNotIn('secret', json.dumps(result)); self.assertNotIn('must-never-be-recorded', json.dumps(result))
        self.assertEqual([call.args for call in inspect.await_args_list if call.args[0] == 'project'], [('project', 'own-project')])
        self.assertNotIn('{{json .Config.Env}}', _CONTAINER); self.assertNotIn('.Cmd', _CONTAINER)
        self.assertEqual(result['worker_networks']['own-worker-front']['gateway_mode_ipv4'], 'isolated')

    async def test_fake_inspection_skips_docker_entirely(self):
        config, session, inspect = self.fixture(); config['runtime']['mode'] = 'fake'
        with patch('minecraft_harness.deployment._docker_projection', inspect):
            result = await capture_deployment(config, {'alice': session}, {'alice': 'own-workspace'}, run_id='run1')
        self.assertFalse(result['verified']); self.assertTrue(result['diagnostic_only']); inspect.assert_not_awaited()

    async def test_formal_missing_server_or_worker_image_fails_before_any_model(self):
        config, session, inspect = self.fixture(); config['server']['managed_container'] = None
        with self.assertRaisesRegex(DeploymentInspectionError, 'managed server'):
            await capture_deployment(config, {'alice': session}, {}, run_id='run1')
        config['server']['managed_container'] = 'own-server'; del session['execution_boundary']['image_id']
        with patch('minecraft_harness.deployment._docker_projection', inspect), self.assertRaisesRegex(DeploymentInspectionError, 'worker image'):
            await capture_deployment(config, {'alice': session}, {}, run_id='run1')

    async def test_workspace_owner_must_match_this_run(self):
        config, session, original = self.fixture()
        async def foreign(kind, identity):
            info = await original(kind, identity)
            if kind == 'container' and identity == 'own-workspace': info['RunLabel'] = 'another-run'
            return info
        with patch('minecraft_harness.deployment._docker_projection', AsyncMock(side_effect=foreign)), self.assertRaisesRegex(DeploymentInspectionError, 'ownership'):
            await capture_deployment(config, {'alice': session}, {'alice': 'own-workspace'}, run_id='run1')

    async def test_formal_ordinary_gateway_ipv6_or_extra_peers_are_rejected(self):
        for alteration in ['ordinary', 'gateway', 'ipv6', 'extra_peer', 'extra_network', 'host_network', 'chat_bypass', 'wrong_username', 'wrong_backend']:
            with self.subTest(alteration=alteration):
                config, session, original = self.fixture()
                async def changed(kind, identity):
                    info = await original(kind, identity)
                    if kind == 'network':
                        if alteration == 'ordinary': info['Options'] = {}
                        if alteration == 'gateway': info['IPAM'][0]['Gateway'] = '172.31.0.1'
                        if alteration == 'ipv6': info['EnableIPv6'] = True
                        if alteration == 'extra_peer': info['Containers'][container_id('own-control')] = {'IPv4Address': '172.31.0.4/16', 'IPv6Address': ''}
                    if kind == 'container' and identity == container_id('own-worker'):
                        if alteration == 'extra_network': info['Networks']['routed-network'] = {'IPAddress': '192.0.2.1'}
                        if alteration == 'host_network': info['NetworkMode'] = 'host'
                    if kind == 'container' and identity == container_id('own-identity'):
                        if alteration == 'chat_bypass': info['GameChat'] = 'GAME_CHAT=true'
                        if alteration == 'wrong_username': info['AllowedUsername'] = 'ALLOWED_USERNAME=another'
                        if alteration == 'wrong_backend': info['MinecraftHost'] = 'MC_HOST=unrelated-service'
                    return info
                with patch('minecraft_harness.deployment._docker_projection', AsyncMock(side_effect=changed)), self.assertRaises(DeploymentInspectionError):
                    await capture_deployment(config, {'alice': session}, {}, run_id='run1')

    async def test_inspection_identity_validation_precedes_subprocess(self):
        with patch('asyncio.create_subprocess_exec', AsyncMock()) as spawn:
            for kind, identity in [('container', '--all'), ('container', 'server;other'), ('image', 'mutable:tag'), ('project', '--all'), ('network', 'peer;other')]:
                with self.assertRaises(ValueError): await _docker_projection(kind, identity)
            spawn.assert_not_awaited()

    async def test_provenance_failure_prevents_model_construction(self):
        import tempfile
        from pathlib import Path
        from minecraft_harness.config import validate_config
        from minecraft_harness.supervisor import Supervisor
        config = validate_config({'agents': {'count': 1, 'max_turns': 1}})
        with tempfile.TemporaryDirectory() as directory:
            with patch('minecraft_harness.supervisor.capture_deployment', AsyncMock(side_effect=DeploymentInspectionError('fixture metadata unavailable'))), patch('minecraft_harness.supervisor.build_model') as model:
                result = await Supervisor(config, Path(directory) / 'run').run()
            self.assertEqual(result['status'], 'infra_failed')
            model.assert_not_called()

    async def test_deployment_is_in_durable_manifest_before_constructing_a_model(self):
        import tempfile
        from pathlib import Path
        from minecraft_harness.config import validate_config
        from minecraft_harness.supervisor import Supervisor
        from minecraft_harness.models import build_model as real_build
        config = validate_config({'agents': {'count': 1, 'max_turns': 1}})
        declaration = {'schema_version': 1, 'operator_only': True, 'verified': True, 'server': {'image_id': SERVER_IMAGE}}
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / 'run'
            seen = []
            def construct(model_config):
                manifest = json.loads((run_dir / 'manifest.json').read_text())
                self.assertEqual(manifest['effective_environment']['deployment'], declaration)
                seen.append(True)
                return real_build(model_config)
            with patch('minecraft_harness.supervisor.capture_deployment', AsyncMock(return_value=declaration)), patch('minecraft_harness.supervisor.build_model', side_effect=construct):
                await Supervisor(config, run_dir).run()
            self.assertTrue(seen)

    async def test_shared_worker_network_is_rejected_for_the_second_actor(self):
        config, session, inspect = self.fixture()
        other = dict(session, agent_id='bob', username='bob', session_id='s2')
        with patch('minecraft_harness.deployment._docker_projection', inspect), self.assertRaisesRegex(DeploymentInspectionError, 'separate worker networks'):
            await capture_deployment(config, {'alice': session, 'bob': other}, {}, run_id='run1')

    async def test_projection_overflow_timeout_and_cancellation_reap_real_cli_pipes(self):
        import asyncio
        import sys
        import time
        original = asyncio.create_subprocess_exec
        for outcome in ['overflow', 'timeout', 'cancel']:
            with self.subTest(outcome=outcome):
                children = []
                source = ('import os,time;os.write(1,b"x"*4_000_000);time.sleep(30)' if outcome == 'overflow'
                          else 'import time;time.sleep(30)')
                async def launch(*args, **kwargs):
                    child = await original(sys.executable, '-c', source, **kwargs)
                    children.append(child)
                    return child
                start = time.monotonic()
                with patch('minecraft_harness.deployment.asyncio.create_subprocess_exec', side_effect=launch):
                    task = asyncio.create_task(_docker_projection('container', 'own-fixture', timeout_s=.1 if outcome == 'timeout' else 10))
                    if outcome == 'cancel':
                        await asyncio.sleep(.05); task.cancel()
                    expected = DeploymentInspectionError if outcome == 'overflow' else TimeoutError if outcome == 'timeout' else asyncio.CancelledError
                    with self.assertRaises(expected):
                        await asyncio.wait_for(task, 2)
                self.assertLess(time.monotonic() - start, 2)
                self.assertTrue(children)
                self.assertIsNotNone(children[0].returncode)


class GeneratedComposeRestartPolicyTests(unittest.TestCase):
    def test_generated_compose_restarts_player_services_but_not_server(self):
        rendered = render_compose(ROOT / "infra" / "compose.paper.yaml", 4, "mc-deploy-test")
        services = rendered["services"]
        self.assertEqual(services["server"]["restart"], "no")
        for name, service in services.items():
            if name.startswith(("identity", "launcher", "bot")):
                self.assertEqual(service["restart"], "unless-stopped", name)

    def test_checked_in_paper_template_restart_policies(self):
        document = yaml.safe_load((ROOT / "infra" / "compose.paper.yaml").read_text(encoding="utf-8"))
        services = document["services"]
        self.assertEqual(services["server"]["restart"], "no")
        self.assertTrue(any(
            service["restart"] == "unless-stopped" for name, service in services.items()
            if name.startswith(("identity", "launcher", "bot"))))


class PaperScaleOverrideTests(unittest.TestCase):
    def test_paper_scale_override_allows_eight_agents_explicitly(self):
        with self.assertRaises(DeploymentGenerationError):
            resolve_mode(8, "paper")
        self.assertEqual(resolve_mode(8, "paper", allow_paper_scale=True), "paper")
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = render_config(
                ROOT / "configs" / "minecraft-paper-runner-20261022.yaml",
                8, "paper8-test", mode="paper", allow_paper_scale=True,
                server_data_dir=root / "data",
                config_path=root / "config.yaml",
                wall_time_minutes=5, total_model_calls=200)
            self.assertEqual(config["experiment"]["profile"], "paper_aligned")
            self.assertTrue(config["experiment"]["paper_scale_override"])
            self.assertEqual(config["agents"]["count"], 8)
            self.assertEqual(config["runtime"]["lifecycle"], "paper_aligned")
            self.assertTrue(config["agents"]["paper_single_program"])
