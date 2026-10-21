"""Actual isolated coding workspace -> Unix broker -> durable fake runtime.

This starts no Minecraft server and calls no model. The JavaScript is solely a
protocol fixture, never installed as a player skill.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import uuid

from minecraft_harness.agents.tools import WorkspaceTools
from minecraft_harness.config import validate_config
from minecraft_harness.execution import ExecutionGateway
from minecraft_harness.recorder import Recorder, atomic_json
from minecraft_harness.workspaces import WorkspaceProvisioner


async def run():
    checks = {}
    with tempfile.TemporaryDirectory(prefix='mc-workspace-smoke-') as temporary:
        root = Path(temporary)
        recorder = Recorder(root)
        runtime = validate_config({})['runtime']
        runtime.update(cli_broker_socket=str(root / 'broker' / 'socket'), cli_broker_socket_advertise='/run/mc-broker/socket')
        gateway = ExecutionGateway(runtime, recorder, ['alice'], root)
        provisioner = WorkspaceProvisioner({'provision': True, 'image': 'minecraft-harness-workspace:local', 'network': 'none'}, root, run_id=recorder.run_id)
        try:
            containers = await provisioner.provision(['alice', 'observer'])
            await gateway.start()
            tool = WorkspaceTools(root / 'workspaces' / 'alice', True, containers['alice'], shell_env=gateway.cli_environment('alice'))
            tool.write('probe.js', 'globals.probe=(globals.probe||0)+1;return {username:bot.username,count:globals.probe};')
            command = await tool.shell('mc exec --file probe.js --request-id workspace-fixture')
            if command.get('exit_code') != 0:
                raise RuntimeError('Workspace CLI smoke failed: ' + str(command))
            value = json.loads(command['output'])
            repeated = await tool.shell('mc exec --file probe.js --request-id workspace-fixture')
            second = json.loads(repeated['output'])
            checks['unix_cli_reaches_durable_gateway_without_network'] = value['result'] == {'username': 'alice', 'count': 1}
            checks['cli_request_id_is_not_replayed'] = second['execution_id'] == value['execution_id'] and second['result']['count'] == 1
            checks['cli_submission_is_in_canonical_ledger'] = recorder.submission('alice', 'workspace-fixture')['execution_id'] == value['execution_id']
            development = await tool.shell('git rev-parse --is-inside-work-tree && rg --version | head -1 && python3 --version && test -r /reference/mineflayer/package.json')
            checks['git_search_python_locked_reference_ready'] = development['exit_code'] == 0
            forged = await tool.shell('mc --agent bob exec --file probe.js')
            checks['broker_rejects_foreign_player_identity'] = forged['exit_code'] != 0
            observer = WorkspaceTools(root / 'workspaces' / 'observer', True, containers['observer'])
            boundary = await observer.shell('test ! -e /run/mc-broker/socket && test ! -e /var/run/docker.sock && test ! -e /workspace/events.sqlite3')
            checks['observer_has_no_player_socket_or_ledger_mount'] = boundary['exit_code'] == 0
            for name in containers.values():
                info = json.loads(await provisioner._docker('inspect', name))[0]
                checks[f'{name}_network_none_no_stored_player_credentials'] = info['HostConfig']['NetworkMode'] == 'none' and not any(item.startswith('MC_AGENT_TOKEN=') for item in info['Config']['Env'])
        finally:
            await gateway.close()
            await provisioner.close()
            recorder.close()
        checks['owned_coding_containers_removed'] = not provisioner.owned
    result = {'status': 'passed' if all(checks.values()) else 'failed', 'created_at': datetime.now(timezone.utc).isoformat(),
              'actual_minecraft': False, 'paid_model': False, 'checks': checks}
    atomic_json(Path('validation/2026-10-21-workspace-smoke.json'), result)
    print(json.dumps(result, indent=2))
    if result['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(run())
