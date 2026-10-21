"""Validate isolated bot + fixed RPC relay images without starting Minecraft."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid

from minecraft_harness.config import validate_config
from minecraft_harness.execution import ExecutionGateway
from minecraft_harness.recorder import Recorder, atomic_json


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE).strip()


async def main():
    suffix = uuid.uuid4().hex[:10]
    network, rpc_network = 'mc-game-' + suffix, 'mc-rpc-' + suffix
    container, relay = 'mc-bot-' + suffix, 'mc-relay-' + suffix
    token = secrets.token_urlsafe(32)
    os.environ['MC_DOCKER_SMOKE_TOKEN'] = token
    created_containers, created_networks = [], []
    result = {'status':'blocked','checked_at_utc':datetime.now(timezone.utc).isoformat(),
              'minecraft_server_started':False,'external_model_called':False}
    try:
        docker('network', 'create', '--internal', network)
        created_networks.append(network)
        docker('network', 'create', rpc_network)
        created_networks.append(rpc_network)
        docker('run', '--detach', '--name', container, '--network', network, '--network-alias', 'bot1',
               '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=512m', '--pids-limit=64',
               '--tmpfs=/tmp:rw,nosuid,nodev,size=64m', '--env=MODE=fake', '--env=BIND=0.0.0.0',
               '--env=MAX_WORKER_RSS_BYTES=268435456', '--env=SUPERVISOR_LEASE_MS=30000', '--env=TOKEN=' + token,
               'minecraft-harness-bot:research')
        created_containers.append(container)
        docker('run', '--detach', '--name', relay, '--network', rpc_network, '--publish', '127.0.0.1::3001',
               '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=128m', '--pids-limit=32',
               '--env=RELAY_ENABLED_AGENTS=bot1', 'minecraft-harness-relay:research')
        created_containers.append(relay)
        docker('network', 'connect', network, relay)
        info, relay_info = json.loads(docker('inspect', container))[0], json.loads(docker('inspect', relay))[0]
        port = relay_info['NetworkSettings']['Ports']['3001/tcp'][0]['HostPort']
        config = validate_config({'agents': {'count': 1, 'ids': ['alice']}, 'runtime': {
            'url': 'http://127.0.0.1:' + port, 'token_env': 'MC_DOCKER_SMOKE_TOKEN', 'request_timeout_s':1}})['runtime']
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory)
            gateway = ExecutionGateway(config, recorder, ['alice'], directory)
            try:
                deadline = time.monotonic()+20
                last_error = None
                while time.monotonic() < deadline:
                    try:
                        health = await gateway._request('GET','/health',timeout=min(1,max(.05,deadline-time.monotonic())))
                        break
                    except Exception as exc:
                        last_error = exc
                        await asyncio.sleep(.1)
                else:
                    raise RuntimeError('Docker RPC health path is unreachable within 20s; check host bridge forwarding/routing') from last_error
                await gateway.start()
                first = await gateway.execute('alice', 'globals.count=(globals.count||0)+1; return globals.count;', request_id='docker-once')
                repeated = await gateway.execute('alice', 'globals.count=(globals.count||0)+1; return globals.count;', request_id='docker-once')
                timeout = await gateway.execute('alice', 'while(true){}', timeout_ms=40)
                after = await gateway.execute('alice', 'return typeof globals.count;')
                await gateway.read_events('alice')
                health = await gateway._request('GET', '/health')
                routes = docker('exec', container, 'node', '-e', "process.stdout.write(require('fs').readFileSync('/proc/net/route','utf8'))")
                checks = {'protocol_v2': health['protocol_version'] == 2,
                          'nonroot_both': info['Config']['User'] == relay_info['Config']['User'] == 'node',
                          'readonly_both': info['HostConfig']['ReadonlyRootfs'] and relay_info['HostConfig']['ReadonlyRootfs'],
                          'bot_internal_network_only': set(info['NetworkSettings']['Networks']) == {network},
                          'bot_has_no_default_route': all(line.split()[1] != '00000000' for line in routes.splitlines()[1:] if line.strip()),
                          'rpc_loopback_via_relay': relay_info['NetworkSettings']['Ports']['3001/tcp'][0]['HostIp'] == '127.0.0.1',
                          'idempotent': first['execution_id'] == repeated['execution_id'] and first['result'] == 1,
                          'deadloop_fenced': timeout['status'] == 'timed_out', 'globals_reset': after['result'] == 'undefined',
                          'journal_acknowledged': all(s['event_cursor'] == s['acked_cursor'] and s['buffers']['events'] == 0 for s in health['session_metrics']),
                          'worker_cap_256m': health['limits']['maxWorkerRssBytes'] == 268435456}
                assert all(checks.values()), checks
                result = {'status': 'passed', 'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'checks': checks,
                          'image_id': info['Image'], 'relay_image_id': relay_info['Image'], 'runtime_mode': health['mode'],
                          'protocol_version': health['protocol_version'], 'minecraft_server_started': False, 'external_model_called': False}
            finally:
                try:
                    await gateway.close(fence=bool(gateway.sessions))
                finally:
                    recorder.close()
        atomic_json(Path(__file__).resolve().parents[1] / 'validation/2026-10-21-docker-fake-smoke.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        result['failure'] = {'type':type(exc).__name__,'message':str(exc)[:1000]}
    finally:
        for name in reversed(created_containers):
            docker('rm', '--force', name)
        for name in reversed(created_networks):
            docker('network', 'rm', name)
        os.environ.pop('MC_DOCKER_SMOKE_TOKEN', None)
        result['owned_containers_and_networks_removed'] = True
        atomic_json(Path(__file__).resolve().parents[1] / 'validation/2026-10-21-docker-fake-smoke.json',result)
    if result['status'] != 'passed':
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise SystemExit(2)


if __name__ == '__main__':
    asyncio.run(main())
