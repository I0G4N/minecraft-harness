"""Provision only agent-owned coding containers; never mount the experiment ledger."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from .subprocesses import bounded_reap


WORKSPACE_MAX_FILE_BYTES = 16 * 1024 ** 2


class WorkspaceDockerError(RuntimeError):
    def __init__(self, message, *, not_found=False):
        super().__init__(message)
        self.not_found = not_found


class WorkspaceProvisioner:
    def __init__(self, config, run_dir, *, run_id):
        self.config, self.run_dir, self.run_id = config, Path(run_dir).resolve(), run_id
        self.owned = []
        self.claims = {}
        if config.get('network', 'none') != 'none':
            raise ValueError('Coding workspaces require network=none; external references use the bounded web adapter')

    def _documentation(self):
        directory = self.run_dir / 'reference-docs'
        directory.mkdir(exist_ok=True)
        packages = Path(__file__).resolve().parents[2] / 'bot_runtime' / 'node_modules'
        references = []
        for package in ('mineflayer', 'mineflayer-pathfinder', 'minecraft-data', 'vec3'):
            source = packages / package
            if not source.is_dir():
                raise RuntimeError('Locked runtime dependencies must be installed before coding workspaces')
            for relative in ('README.md', 'index.d.ts', 'package.json', 'docs/api.md', 'docs/API.md', 'LICENSE'):
                path = source / relative
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= 4 * 1024 ** 2:
                    destination = directory / package / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
                    references.append({'path': f'{package}/{relative}', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        (directory / 'manifest.json').write_text(json.dumps(references, indent=2))
        return directory

    async def _docker(self, *args, timeout=120):
        process = await asyncio.create_subprocess_exec('docker', *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        async def capture(stream, maximum, fail_on_overflow=False):
            data = bytearray()
            while chunk := await stream.read(8192):
                if fail_on_overflow and len(data) + len(chunk) > maximum:
                    raise RuntimeError('Coding workspace Docker response exceeded bounded inspection size')
                data.extend(chunk[:max(0, maximum - len(data))])
            return bytes(data)
        readers = [asyncio.create_task(capture(process.stdout, 1024 ** 2, True)),
                   asyncio.create_task(capture(process.stderr, 16000))]
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
        except BaseException:
            await bounded_reap(process, readers, kill_group=True)
            raise
        if process.returncode:
            message = stderr.decode(errors='replace')
            missing = bool(args and args[0] == 'inspect' and any(
                line.casefold().startswith(('error: no such object:', 'error: no such container:')) for line in message.splitlines()))
            raise WorkspaceDockerError('Coding workspace Docker operation failed: ' + message[-1000:], not_found=missing)
        return stdout.decode()

    async def provision(self, agent_ids):
        if not self.config.get('provision'):
            return {}
        image = self.config['image']
        image_info = json.loads(await self._docker('image', 'inspect', image))[0]
        if not image_info.get('Id'):
            raise RuntimeError('Coding workspace image inspection did not return an immutable image ID')
        # Up to 16 players plus the independent Observer.  Keep this in sync
        # with config.MAX_AGENTS and the dynamic exact-N deployment generator.
        if (not isinstance(agent_ids, list) or not 1 <= len(agent_ids) <= 17 or any(
                not isinstance(agent, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,16}', agent) for agent in agent_ids)
                or len(set(agent_ids)) != len(agent_ids)):
            raise ValueError('Unique safe coding workspace identities are required')
        documentation = self._documentation()
        broker = self.run_dir / 'broker'
        broker.mkdir(exist_ok=True, mode=0o700)
        containers = {}
        for agent in agent_ids:
            workspace = self.run_dir / 'workspaces' / agent
            workspace.mkdir(parents=True, exist_ok=True)
            name = 'mc-workspace-' + hashlib.sha256((self.run_id + ':' + agent).encode()).hexdigest()[:20]
            # Refuse unexpected existing ownership instead of adopting an arbitrary container.
            try:
                raw = await self._docker('inspect', name)
            except WorkspaceDockerError as exc:
                if not exc.not_found:
                    raise
                raw = None
            if raw:
                info = json.loads(raw)[0]
                self._verify_existing(info, image_info, workspace, documentation, broker, agent)
                self.owned.append(name)
                self.claims[name] = workspace
                if info.get('State', {}).get('Running'):
                    # A Supervisor crash can leave old shell descendants alive.
                    # Reuse files, but fence that process namespace before resume.
                    await self._docker('stop', '--time', '1', name, timeout=15)
                    if (await self._docker('inspect', '--format', '{{.State.Running}}', name, timeout=15)).strip() != 'false':
                        raise RuntimeError('Could not confirm old coding workspace processes stopped')
                await self._docker('start', name)
            else:
                # Register before a detached Docker start. Lost client ACK or
                # cancellation must still leave an owned cleanup target.
                self.owned.append(name)
                self.claims[name] = workspace
                await self._docker('run', '-d', '--name', name, '--label', 'minecraft-harness.run=' + self.run_id,
                    '--user', f'{os.getuid()}:{os.getgid()}', '--init', '--read-only', '--cap-drop=ALL',
                    '--security-opt=no-new-privileges', '--network', self.config.get('network', 'none'),
                    '--memory=512m', '--pids-limit=64', '--cpus=1', '--ulimit',
                    f'fsize={WORKSPACE_MAX_FILE_BYTES}:{WORKSPACE_MAX_FILE_BYTES}', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
                    '--mount', f'type=bind,source={workspace},target=/workspace',
                    '--mount', f'type=bind,source={documentation},target=/reference,readonly',
                    *(['--mount', f'type=bind,source={broker},target=/run/mc-broker,readonly'] if agent != 'observer' else []), image)
            await self._docker('exec', '--workdir', '/workspace', name, 'git', 'init', '-q')
            containers[agent] = name
        return containers

    def _verify_existing(self, info, image_info, workspace, documentation, broker, agent):
        config, host = info.get('Config', {}), info.get('HostConfig', {})
        labels = config.get('Labels', {}) or {}
        expected = {(str(workspace), '/workspace', True), (str(documentation), '/reference', False)}
        if agent != 'observer':
            expected.add((str(broker), '/run/mc-broker', False))
        actual = {(mount.get('Source'), mount.get('Destination'), mount.get('RW'))
                  for mount in info.get('Mounts', []) if mount.get('Type') == 'bind'}
        unexpected = [mount for mount in info.get('Mounts', []) if mount.get('Type') != 'bind'
                      and not (mount.get('Type') == 'tmpfs' and mount.get('Destination') == '/tmp')]
        image_config = image_info.get('Config', {})
        security = host.get('SecurityOpt', []) or []
        file_limits = [item for item in host.get('Ulimits', []) or [] if item.get('Name') == 'fsize']
        if (labels.get('minecraft-harness.run') != self.run_id or actual != expected or unexpected
                or info.get('Image') != image_info['Id']
                or config.get('User') != f'{os.getuid()}:{os.getgid()}'
                or config.get('Cmd') != image_config.get('Cmd')
                or config.get('Entrypoint') != image_config.get('Entrypoint')
                or sorted(config.get('Env', [])) != sorted(image_config.get('Env', []))
                or host.get('NetworkMode') != 'none' or host.get('Privileged')
                or not host.get('ReadonlyRootfs') or not host.get('Init')
                or set(host.get('CapDrop') or []) != {'ALL'} or host.get('CapAdd')
                or not any(value in {'no-new-privileges', 'no-new-privileges:true'} for value in security)
                or host.get('PidMode', '') not in {'', 'private'}
                or host.get('IpcMode', '') not in {'', 'private'}
                or not 0 < host.get('Memory', 0) <= 512 * 1024 ** 2
                or not 0 < host.get('PidsLimit', 0) <= 64
                or not 0 < host.get('NanoCpus', 0) <= 1_000_000_000
                or set(host.get('Tmpfs') or {}) != {'/tmp'}
                or len(file_limits) != 1 or file_limits[0].get('Soft') != WORKSPACE_MAX_FILE_BYTES
                or file_limits[0].get('Hard') != WORKSPACE_MAX_FILE_BYTES):
            raise RuntimeError('Workspace container identity or isolation mismatch; refusing to adopt it')

    async def close(self):
        errors = []
        for name in self.owned:
            try:
                try:
                    info = json.loads(await self._docker('inspect', name, timeout=15))[0]
                except WorkspaceDockerError as exc:
                    if exc.not_found:
                        continue
                    raise
                labels = info.get('Config', {}).get('Labels', {}) or {}
                if (labels.get('minecraft-harness.run') != self.run_id or not any(
                        mount.get('Type') == 'bind' and mount.get('Source') == str(self.claims[name])
                        and mount.get('Destination') == '/workspace' for mount in info.get('Mounts', []))):
                    raise RuntimeError('Workspace cleanup ownership changed; refusing to remove container')
                await self._docker('rm', '-f', name, timeout=15)
            except Exception as exc:
                errors.append(exc)
        self.owned.clear()
        self.claims.clear()
        if errors:
            raise errors[0]
