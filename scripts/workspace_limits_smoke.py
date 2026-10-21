"""Real Docker RLIMIT_FSIZE and monitored workspace-total negative probes.

Uses only isolated coding containers. Starts no Minecraft server or model.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import uuid

from minecraft_harness.agents.tools import WorkspaceTools
from minecraft_harness.recorder import atomic_json
from minecraft_harness.workspaces import WorkspaceProvisioner, WORKSPACE_MAX_FILE_BYTES


async def run():
    checks, observations = {}, {}
    with tempfile.TemporaryDirectory(prefix='mc-workspace-limits-') as temporary:
        root = Path(temporary)
        provisioner = WorkspaceProvisioner({'provision': True, 'image': 'minecraft-harness-workspace:local', 'network': 'none'},
                                            root, run_id=uuid.uuid4().hex)
        try:
            names = await provisioner.provision(['probe'])
            tool = WorkspaceTools(root / 'workspaces' / 'probe', True, names['probe'])
            tool.write('fsize_probe.py', '''import errno,json,os,resource,sys
limits=resource.getrlimit(resource.RLIMIT_FSIZE)
try:
    resource.setrlimit(resource.RLIMIT_FSIZE,(limits[0]*2,limits[1]*2))
    raise_blocked=False
except (OSError,ValueError):
    raise_blocked=True
print(json.dumps({"soft_hard_bytes":limits,"hard_limit_raise_blocked":raise_blocked}),flush=True)
try:
    with open("too-large.bin","wb") as stream:
        for _ in range(17): stream.write(b"x" * (1024**2))
except OSError as exc:
    print(json.dumps({"errno":exc.errno,"file_bytes":os.stat("too-large.bin").st_size}),flush=True)
    sys.exit(17)
sys.exit(0)
''')
            result = await tool.shell('python3 fsize_probe.py', timeout_s=10)
            observations['per_file_probe'] = result
            observations['per_file_probe_bytes'] = (tool.root / 'too-large.bin').stat().st_size
            lines = result['output'].splitlines()
            native = json.loads(lines[0])
            limits = native['soft_hard_bytes']
            checks['unprivileged_exec_cannot_raise_hard_file_limit'] = native['hard_limit_raise_blocked'] is True
            checks['docker_exec_inherits_hard_and_soft_16mib_file_limit'] = limits == [WORKSPACE_MAX_FILE_BYTES] * 2
            checks['seventeen_mib_extension_fails_at_or_before_sixteen_mib'] = result['exit_code'] != 0 and observations['per_file_probe_bytes'] <= WORKSPACE_MAX_FILE_BYTES
            checks['per_file_failure_does_not_delete_source_or_stop_container'] = (tool.root / 'fsize_probe.py').exists() and not tool._shell_container_stopped
            tool.remove('too-large.bin')
            constrained = WorkspaceTools(tool.root, True, names['probe'], max_workspace_bytes=128000)
            post = await constrained.shell('head -c 262144 /dev/zero > aggregate.bin', timeout_s=5)
            observations['post_command_aggregate'] = post
            checks['aggregate_postcheck_stops_workspace_and_retains_file'] = post.get('status') == 'workspace_quota_exceeded' and constrained._shell_container_stopped and (tool.root / 'aggregate.bin').stat().st_size == 262144
            try:
                await constrained.shell('touch must-not-exist')
            except ValueError:
                blocked = True
            else:
                blocked = False
            checks['next_shell_blocked_before_reclaim'] = blocked and not (tool.root / 'must-not-exist').exists()
            constrained.remove('aggregate.bin')
            recovered = await constrained.shell('printf recovered')
            checks['reclaim_allows_trusted_restart_of_same_workspace'] = recovered['exit_code'] == 0 and recovered['output'] == 'recovered'
            begin = time.monotonic()
            active = await constrained.shell("head -c 262144 /dev/zero > watched.bin; setsid sh -c 'sleep 30' >/dev/null 2>&1 & wait", timeout_s=30)
            observations['active_aggregate_watch'] = active
            observations['active_watch_elapsed_s'] = round(time.monotonic() - begin, 3)
            info = json.loads(await provisioner._docker('inspect', names['probe']))[0]
            checks['one_second_watcher_aborts_long_shell'] = active.get('status') == 'workspace_quota_exceeded' and observations['active_watch_elapsed_s'] < 8
            checks['whole_container_stop_covers_detached_descendants'] = info['State']['Running'] is False
            checks['aggregate_guard_never_deletes_agent_source'] = (tool.root / 'watched.bin').exists() and (tool.root / 'fsize_probe.py').exists()
        finally:
            await provisioner.close()
        checks['owned_probe_containers_removed'] = not provisioner.owned
    record = {'status': 'passed' if all(checks.values()) else 'failed',
              'created_at': datetime.now(timezone.utc).isoformat(), 'actual_minecraft': False, 'paid_model': False,
              'limit_semantics': {'single_file': 'kernel_hard_limit_16MiB', 'workspace_total': 'monitored_soft_limit_pre_post_and_1s_while_shell_runs'},
              'checks': checks, 'observations': observations}
    atomic_json(Path('validation/2026-10-21-workspace-limits-smoke.json'), record)
    print(json.dumps(record, indent=2))
    if record['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(run())
