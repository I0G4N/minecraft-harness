"""Bounded operator-only deployment provenance; never inspect credentials/Env."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re

from .subprocesses import bounded_reap


_CONTAINER = ('{{$chat := ""}}{{$host := ""}}{{$port := ""}}{{$username := ""}}'
              '{{range .Config.Env}}{{$key := index (split . "=") 0}}'
              '{{if eq $key "GAME_CHAT"}}{{$chat = .}}{{else if eq $key "MC_HOST"}}{{$host = .}}'
              '{{else if eq $key "MC_PORT"}}{{$port = .}}{{else if eq $key "ALLOWED_USERNAME"}}{{$username = .}}{{end}}{{end}}'
              '{"Id":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},'
              '"ConfigImage":{{json .Config.Image}},"User":{{json .Config.User}},'
              '"RunLabel":{{json (index .Config.Labels "minecraft-harness.run")}},'
              '"ComposeProject":{{json (index .Config.Labels "com.docker.compose.project")}},'
              '"ComposeService":{{json (index .Config.Labels "com.docker.compose.service")}},'
              '"GameChat":{{json $chat}},"MinecraftHost":{{json $host}},"MinecraftPort":{{json $port}},"AllowedUsername":{{json $username}},'
              '"ReadonlyRootfs":{{json .HostConfig.ReadonlyRootfs}},"Privileged":{{json .HostConfig.Privileged}},'
              '"CapDrop":{{json .HostConfig.CapDrop}},"SecurityOpt":{{json .HostConfig.SecurityOpt}},'
              '"Memory":{{json .HostConfig.Memory}},"PidsLimit":{{json .HostConfig.PidsLimit}},"NanoCpus":{{json .HostConfig.NanoCpus}},'
              '"NetworkMode":{{json .HostConfig.NetworkMode}},"Networks":{{json .NetworkSettings.Networks}},"PublishedPorts":{{json .NetworkSettings.Ports}},"PidMode":{{json .HostConfig.PidMode}},'
              '"IpcMode":{{json .HostConfig.IpcMode}},"Mounts":{{json .Mounts}}}')
_IMAGE = ('{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}},'
          '"Os":{{json .Os}},"Architecture":{{json .Architecture}}}')
_NETWORK = ('{"Id":{{json .Id}},"Name":{{json .Name}},"Internal":{{json .Internal}},"EnableIPv6":{{json .EnableIPv6}},"Options":{{json .Options}},"IPAM":{{json .IPAM.Config}},"Containers":{{json .Containers}}}')
_IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')


class DeploymentInspectionError(RuntimeError):
    pass


async def _docker_projection(kind, identity, *, timeout_s=10):
    if kind == 'container':
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', identity):
            raise ValueError('Managed container identity is invalid')
        arguments = ['container', 'inspect', '--format', _CONTAINER, identity]
    elif kind == 'network':
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', identity):
            raise ValueError('Declared worker network identity is invalid')
        arguments = ['network', 'inspect', '--format', _NETWORK, identity]
    elif kind == 'project':
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', identity):
            raise ValueError('Owned Compose project identity is invalid')
        arguments = ['container', 'ls', '--all', '--quiet', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + identity]
    elif kind == 'image':
        if not isinstance(identity, str) or not _IMAGE_ID.fullmatch(identity):
            raise ValueError('Only actual immutable image IDs may be inspected')
        arguments = ['image', 'inspect', '--format', _IMAGE, identity]
    else:
        raise ValueError('Unsupported deployment evidence kind')
    process = await asyncio.create_subprocess_exec('docker', *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    async def capture(stream, maximum):
        output = bytearray()
        while chunk := await stream.read(8192):
            if len(output) + len(chunk) > maximum:
                raise DeploymentInspectionError('Docker provenance response exceeds its byte bound')
            output.extend(chunk)
        return bytes(output)
    readers = [asyncio.create_task(capture(process.stdout, 256 * 1024)), asyncio.create_task(capture(process.stderr, 8192))]
    try:
        async with asyncio.timeout(timeout_s):
            raw, _ = await asyncio.gather(*readers)
            await process.wait()
        if process.returncode:
            # Docker stderr can echo arguments; do not persist arbitrary output.
            raise DeploymentInspectionError(f'Cannot inspect declared {kind} provenance')
        if kind == 'project':
            identities = raw.decode('ascii').split()
            if len(identities) > 20 or len(identities) != len(set(identities)) or any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in identities):
                raise DeploymentInspectionError('Owned Compose project inventory exceeds 20 safe identities')
            return {'container_ids': identities}
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise DeploymentInspectionError('Docker provenance must be a JSON object')
        return document
    except (ValueError, UnicodeError) as exc:
        raise DeploymentInspectionError('Malformed Docker provenance') from exc
    finally:
        await bounded_reap(process, readers, kill_group=True)



def _safe_text(value, maximum=1024):
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode()) > maximum:
        raise DeploymentInspectionError('Malformed deployment text metadata')
    return value


def _image_record(info, actual_id):
    if info.get('Id') != actual_id or not _IMAGE_ID.fullmatch(actual_id):
        raise DeploymentInspectionError('Image provenance identity mismatch')
    digests = info.get('RepoDigests') or []
    if not isinstance(digests, list) or len(digests) > 64 or any(not isinstance(item, str) or len(item) > 1024 or '@sha256:' not in item for item in digests):
        raise DeploymentInspectionError('Malformed immutable repository digests')
    return {'image_id': actual_id, 'repo_digests': digests, 'os': _safe_text(info.get('Os'), 32), 'architecture': _safe_text(info.get('Architecture'), 32)}


def _public_session(session):
    if not isinstance(session, dict):
        raise DeploymentInspectionError('Missing public runtime session metadata')
    result = {key: session[key] for key in ('agent_id', 'username', 'session_id', 'run_id', 'world_epoch', 'runtime_epoch', 'mode', 'lifecycle', 'protocol_version', 'worker_transport', 'execution_security') if key in session}
    boundary = session.get('execution_boundary') or {}
    native = session.get('native_sandbox') or {}
    modules = session.get('module_policy') or {}
    result['execution_boundary'] = {key: boundary[key] for key in ('separate_container', 'read_only', 'no_host_mounts', 'no_control_credentials', 'native_filter_expected', 'identity_bound', 'network', 'image_id') if key in boundary}
    result['native_sandbox'] = {key: native[key] for key in ('native_filter', 'version') if key in native}
    result['module_policy'] = {key: modules[key] for key in ('schema_version', 'policy_kind', 'allowed', 'network', 'filesystem_write', 'process_creation', 'vm_is_security_boundary') if key in modules}
    for key in ('dependencies', 'versions'):
        value = session.get(key)
        if isinstance(value, dict):
            result[key] = {str(name): _safe_text(version, 128) for name, version in value.items() if isinstance(name, str) and len(name) <= 128 and isinstance(version, str)}
    if len(json.dumps(result, allow_nan=False).encode()) > 64000:
        raise DeploymentInspectionError('Public runtime deployment metadata exceeds its bound')
    return result


async def capture_deployment(config, sessions, coding_containers, *, run_id):
    """Capture only declared server/owned workspaces and public worker images.

    The caller must persist this operator-only result before constructing any
    model. Fake diagnostics perform no Docker calls. Missing formal server
    provenance fails admission instead of inventing a server image.
    """
    if config['runtime']['mode'] == 'fake':
        return {'schema_version': 1, 'diagnostic_only': True, 'verified': False, 'reason': 'synthetic_game_no_deployment_inspection', 'operator_only': True}
    paper = config['experiment']['profile'] == 'paper_aligned'
    server_name = config['server'].get('managed_container')
    if paper and not server_name:
        raise DeploymentInspectionError('Formal paper deployment requires an inspectable managed server')
    if (not isinstance(coding_containers, dict) or len(coding_containers) > 17
            or not isinstance(sessions, dict) or len(sessions) > 17):
        raise DeploymentInspectionError('Deployment identity list exceeds bounds')
    captured = {'schema_version': 1, 'captured_at': datetime.now(timezone.utc).isoformat(), 'operator_only': True,
                'verified': True, 'server': None, 'workspaces': {}, 'runtime_sessions': {}, 'images': {}, 'worker_networks': {}, 'worker_containers': {}, 'owned_project': None, 'project_containers': {}, 'deviations': []}
    cache = captured['images']
    async def image(actual_id, *, required=True):
        if not isinstance(actual_id, str) or not _IMAGE_ID.fullmatch(actual_id):
            raise DeploymentInspectionError('Actual immutable image ID missing')
        if actual_id not in cache:
            try:
                cache[actual_id] = _image_record(await _docker_projection('image', actual_id), actual_id)
            except DeploymentInspectionError:
                if required:
                    raise
                cache[actual_id] = {'image_id': actual_id, 'repo_digests': None, 'inspection': 'not_available_on_operator_docker_daemon'}
        return cache[actual_id]
    container_cache = {}
    async def container(name, *, actor=None):
        info = await _docker_projection('container', name)
        if not isinstance(info.get('Id'), str) or not re.fullmatch(r'[0-9a-f]{64}', info['Id']):
            raise DeploymentInspectionError('Actual container ID missing')
        actual_name = _safe_text(info.get('Name'), 256)
        if not actual_name:
            raise DeploymentInspectionError('Actual container name missing')
        actual_name = actual_name.lstrip('/')
        if actual_name != name and info['Id'] != name:
            raise DeploymentInspectionError('Container provenance identity mismatch')
        if actor is not None and info.get('RunLabel') != run_id:
            raise DeploymentInspectionError('Coding workspace ownership changed before model admission')
        actual_image = await image(info.get('Image'))
        mounts = info.get('Mounts') or []
        if not isinstance(mounts, list) or len(mounts) > 64:
            raise DeploymentInspectionError('Container mount evidence exceeds bounds')
        mount_records = [{key: mount[key] for key in ('Type', 'Source', 'Destination', 'RW', 'Name') if key in mount} for mount in mounts if isinstance(mount, dict)]
        selected_environment = {}
        for key, field in [('GAME_CHAT', 'GameChat'), ('MC_HOST', 'MinecraftHost'), ('MC_PORT', 'MinecraftPort'), ('ALLOWED_USERNAME', 'AllowedUsername')]:
            value = _safe_text(info.get(field), 4096)
            if value:
                if not value.startswith(key + '='):
                    raise DeploymentInspectionError('Malformed selected public deployment environment')
                selected_environment[key] = value[len(key) + 1:]
        networks = info.get('Networks') or {}
        if not isinstance(networks, dict) or len(networks) > 16:
            raise DeploymentInspectionError('Container network declaration exceeds bounds')
        network_records = {str(name): {key: value[key] for key in ('NetworkID', 'IPAddress', 'IPPrefixLen', 'GlobalIPv6Address', 'GlobalIPv6PrefixLen', 'Gateway', 'IPv6Gateway', 'Aliases') if key in value}
                           for name, value in networks.items() if isinstance(value, dict)}
        record = {'container_id': info['Id'], 'container_name': actual_name, 'image': actual_image,
                'configured_image': _safe_text(info.get('ConfigImage')), 'user': _safe_text(info.get('User'), 128),
                'run_label': _safe_text(info.get('RunLabel'), 128), 'compose_project': _safe_text(info.get('ComposeProject'), 128), 'compose_service': _safe_text(info.get('ComposeService'), 128),
                'selected_public_environment': selected_environment, 'networks': network_records, 'published_ports': info.get('PublishedPorts') or {},
                'resource_limits': {key: info.get(key) for key in ('Memory', 'PidsLimit', 'NanoCpus')}, 'readonly_rootfs': info.get('ReadonlyRootfs'),
                'privileged': info.get('Privileged'), 'cap_drop': info.get('CapDrop'), 'security_options': info.get('SecurityOpt'),
                'network_mode': _safe_text(info.get('NetworkMode'), 256), 'pid_mode': _safe_text(info.get('PidMode'), 64),
                'ipc_mode': _safe_text(info.get('IpcMode'), 64), 'mounts': mount_records}
        container_cache[info['Id']] = record
        return record
    if server_name:
        captured['server'] = await container(server_name)
        project = captured['server'].get('compose_project')
        if project:
            captured['owned_project'] = project
            inventory = await _docker_projection('project', project)
            identities = inventory.get('container_ids')
            if not isinstance(identities, list) or len(identities) > 20 or captured['server']['container_id'] not in identities:
                raise DeploymentInspectionError('Owned project inventory is missing the managed server')
            for identity in identities:
                member = container_cache.get(identity) or await container(identity)
                if member.get('compose_project') != project:
                    raise DeploymentInspectionError('Compose project ownership changed during capture')
                captured['project_containers'][member['container_name']] = member
        else:
            captured['verified'] = False
            captured['deviations'].append('compose_project_unknown_control_launcher_proxy_images_not_inspected')
    else:
        captured['verified'] = False
        captured['deviations'].append('unmanaged_server_image_unknown')
    for actor, name in sorted(coding_containers.items()):
        captured['workspaces'][actor] = await container(name, actor=actor)
    used_networks = set()
    for actor, session in sorted(sessions.items()):
        public = _public_session(session)
        if public.get('agent_id') != actor or public.get('run_id') != run_id:
            raise DeploymentInspectionError('Runtime deployment session identity mismatch')
        boundary = public['execution_boundary']
        if paper and (public.get('worker_transport') != 'remote' or public['native_sandbox'].get('native_filter') is not True or boundary.get('identity_bound') is not True):
            raise DeploymentInspectionError('Formal runtime deployment boundary not confirmed')
        if boundary.get('image_id'):
            await image(boundary['image_id'], required=False)
        elif paper:
            raise DeploymentInspectionError('Actual generated-worker image provenance missing')
        network_name = boundary.get('network')
        if paper and not network_name:
            raise DeploymentInspectionError('Actual worker network declaration missing')
        if paper and network_name in used_networks:
            raise DeploymentInspectionError('Formal players require separate worker networks')
        used_networks.add(network_name)
        if network_name and network_name not in captured['worker_networks']:
            network = await _docker_projection('network', network_name)
            ipam = network.get('IPAM') or []
            options = network.get('Options') or {}
            peers = network.get('Containers') or {}
            if (network.get('Name') != network_name or not isinstance(ipam, list) or not isinstance(options, dict)
                    or not isinstance(peers, dict) or len(peers) > 16):
                raise DeploymentInspectionError('Malformed actual worker network evidence')
            if paper and (network.get('Internal') is not True or network.get('EnableIPv6') is not False
                          or options.get('com.docker.network.bridge.gateway_mode_ipv4') != 'isolated'
                          or not ipam or any(not isinstance(entry, dict) or entry.get('Gateway') for entry in ipam)):
                raise DeploymentInspectionError('Formal worker network must be internal/isolated, without gateway or IPv6')
            peer_records = {}
            for identity, declaration in peers.items():
                if not isinstance(declaration, dict):
                    raise DeploymentInspectionError('Malformed worker network peer declaration')
                member = container_cache.get(identity) or await container(identity)
                peer_records[identity] = {'name': member['container_name'], 'image_id': member['image']['image_id'],
                    'compose_project': member.get('compose_project'), 'compose_service': member.get('compose_service'),
                    'ipv4_address': _safe_text(declaration.get('IPv4Address'), 128), 'ipv6_address': _safe_text(declaration.get('IPv6Address'), 128)}
            if paper:
                workers = [value for value in peer_records.values() if value['image_id'] == boundary.get('image_id')]
                proxies = [value for value in peer_records.values() if value.get('compose_project') == captured['owned_project']
                           and isinstance(value.get('compose_service'), str) and value['compose_service'].startswith('identity')]
                if len(peer_records) != 2 or len(workers) != 1 or len(proxies) != 1:
                    raise DeploymentInspectionError('Formal worker network must contain only its worker and owned identity proxy')
                worker_id = next(identity for identity, value in peer_records.items() if value is workers[0])
                proxy_id = next(identity for identity, value in peer_records.items() if value is proxies[0])
                worker = container_cache[worker_id]
                proxy = container_cache[proxy_id]
                if (set(worker['networks']) != {network_name} or worker['network_mode'] not in {network_name, network.get('Id')}
                        or worker.get('privileged') or not worker.get('readonly_rootfs') or worker.get('mounts')
                        or worker.get('pid_mode') not in {'', 'private'} or worker.get('ipc_mode') not in {'', 'private'}
                        or set(worker.get('cap_drop') or []) != {'ALL'}
                        or not any(option in {'no-new-privileges', 'no-new-privileges:true'} for option in worker.get('security_options') or [])):
                    raise DeploymentInspectionError('Actual worker has an extra network, mount or unsafe host namespace')
                allowed = proxy['selected_public_environment']
                expected_chat = 'true' if config.get('collaboration', {}).get('game_chat', True) else 'false'
                if (allowed.get('ALLOWED_USERNAME') != actor or allowed.get('GAME_CHAT') != expected_chat
                        or allowed.get('MC_HOST') != 'server' or allowed.get('MC_PORT') != '25565'):
                    raise DeploymentInspectionError('Actual identity proxy username/chat/backend does not match formal experiment')
                captured['worker_containers'][actor] = worker
            captured['worker_networks'][network_name] = {'network_id': network.get('Id'), 'name': network_name,
                'internal': network.get('Internal'), 'enable_ipv6': network.get('EnableIPv6'),
                'gateway_mode_ipv4': options.get('com.docker.network.bridge.gateway_mode_ipv4'), 'ipam': ipam, 'peers': peer_records}
        captured['runtime_sessions'][actor] = public
    return captured
