"""Independent sequential Vanilla trials with explicit, inspectable provisioning."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import time
import uuid

import yaml

from .environment import provision_world
from .recorder import atomic_json
from .server import ServerControl


def prepare_trials(config, directory, seeds, repetitions=1):
    if type(repetitions) is not int or repetitions < 1 or not seeds:
        raise ValueError('Trials require seeds and a positive repetition count')
    if (not isinstance(seeds, (list, tuple)) or any(seed is not None and
            (type(seed) is not int or not -(2 ** 63) <= seed < 2 ** 63) for seed in seeds)):
        raise ValueError('Trial seeds must be null or signed 64-bit integers')
    if config['runtime']['mode'] == 'minecraft' and config['agents']['ids'] != [f'player{i}' for i in range(1, 5)]:
        raise ValueError('Managed four-player deployment requires player1 through player4 identities')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    trials = []
    for seed in seeds:
        for repeat in range(repetitions):
            identity = uuid.uuid4().hex[:12]
            root = directory / f'trial-{len(trials) + 1:03d}-{identity}'
            root.mkdir(mode=0o700)
            world = provision_world(root / 'server-data', seed=seed)
            trial = copy.deepcopy(config)
            trial['experiment']['seed'] = seed  # null means privately recorded, randomly generated seed.
            if trial['runtime']['mode'] == 'minecraft':
                trial['runtime']['url'] = None
                trial['runtime']['urls'] = {f'player{i}': f'http://127.0.0.1:{3100 + i}' for i in range(1, 5)}
                trial['runtime']['token_envs'] = {f'player{i}': f'BOT{i}_ADMIN_TOKEN' for i in range(1, 5)}
            trial['server'].update(world_dir=str(root / 'server-data' / 'world'),
                log_path=str(root / 'server-data' / 'logs' / 'latest.log'),
                properties_path=str(root / 'server-data' / 'server.properties'),
                managed_container=f'mc-trial-{identity}-server', require_fresh_world=True)
            config_path = root / 'config.yaml'
            config_path.write_text(yaml.safe_dump(trial, sort_keys=False))
            trials.append({'trial_id': identity, 'repeat': repeat + 1, 'seed': world['seed'],
                'config': str(config_path), 'run_dir': str(root / 'run'), 'project': f'mc-trial-{identity}',
                'server_container': trial['server']['managed_container'], 'server_data': str(root / 'server-data'),
                'status': 'prepared'})
    manifest = {'schema_version': 1, 'budget_scope': 'per_trial', 'eula_accepted_by_preparation': False,
        'max_batch_cost_usd': config['budgets']['max_cost_usd'] * len(trials) if config['budgets'].get('max_cost_usd') is not None else None,
        'max_batch_model_calls': config['budgets']['total_model_calls'] * len(trials), 'trials': trials}
    atomic_json(directory / 'batch.json', manifest)
    return manifest


async def execute_trials(manifest, directory, run_trial):
    if os.environ.get('ACCEPT_EULA') != 'true':
        raise ValueError('Starting Vanilla requires explicitly accepted EULA (ACCEPT_EULA=true); prepared trials remain inspectable')
    from .config import load_config
    compose = Path(__file__).resolve().parents[2] / 'infra' / 'compose.paper.yaml'
    for trial in manifest['trials']:
        if trial.get('status') != 'prepared':
            raise ValueError('Only prepared trials may execute; completed worlds must never be silently replayed')
        config = load_config(trial['config'])
        if config['experiment']['profile'] != 'paper_aligned':
            raise ValueError('Managed Vanilla batch requires paper_aligned deployment')
        environment = {**os.environ, 'HARNESS_PROJECT': trial['project'], 'MC_SERVER_CONTAINER': trial['server_container'],
                       'SERVER_DATA_DIR': trial['server_data'], 'MC_SEED': str(trial['seed']),
                       'GAME_CHAT': str(config['collaboration']['game_chat']).lower(),
                       'MC_UID': str(os.getuid()), 'MC_GID': str(os.getgid())}
        async def command(*arguments):
            process = await asyncio.create_subprocess_exec('docker', 'compose', '-p', trial['project'], '-f', str(compose),
                *arguments, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=environment)
            try:
                async with asyncio.timeout(180):
                    await process.wait()
            except BaseException:
                process.kill(); await process.wait(); raise
            if process.returncode:
                raise RuntimeError('Trial Compose operation failed; inspect the dedicated project logs')
        try:
            trial['status'] = 'starting'
            atomic_json(Path(directory) / 'batch.json', manifest)
            await command('up', '-d', '--no-build')
            # Probe readiness without creating model/runtime sessions.
            server = ServerControl(config['server'], None)
            deadline = time.monotonic() + config['server']['startup_timeout_s']
            while True:
                try:
                    await server.ready()
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(1)
            trial['result'] = await run_trial(config, Path(trial['run_dir']))
            trial['status'] = trial['result']['status']
        except Exception as exc:
            trial['status'] = 'infra_failed'
            trial['error_type'] = type(exc).__name__
        finally:
            try:
                await command('down', '--remove-orphans', '--timeout', '15')
            except Exception:
                trial['cleanup_unconfirmed'] = True
                trial['status'] = 'infra_failed'
            atomic_json(Path(directory) / 'batch.json', manifest)
        if trial['status'] in {'infra_failed', 'model_failed', 'resource_exhausted', 'reliability_failed'}:
            break
    return manifest
