"""Actual Docker-Go-template provenance validation using only owned sleep fixtures."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import subprocess

from minecraft_harness.deployment import capture_deployment

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE, timeout=20).strip()


async def main():
    identity = 'mc-deployment-probe-' + secrets.token_hex(5)
    run_id = 'fixture-run-' + secrets.token_hex(5)
    names = [identity + '-server', identity + '-workspace']
    created = []
    result = {'at': datetime.now(timezone.utc).isoformat(), 'actual_minecraft': False, 'paid_model': False,
              'global_policies_modified': False, 'status': 'failed', 'cleanup_complete': False}
    synthetic_secret = 'synthetic-' + secrets.token_hex(20)
    try:
        image = 'minecraft-harness-control:research'
        actual_image = docker('image', 'inspect', '--format', '{{.Id}}', image)
        for name, role in zip(names, ['server', 'workspace']):
            docker('run', '-d', '--name', name, '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--memory', '64m', '--pids-limit', '16',
                   '--label', 'com.docker.compose.project=' + identity,
                   '--label', 'com.docker.compose.service=' + role,
                   '--label', 'minecraft-harness.run=' + run_id,
                   '--env', 'GAME_CHAT=false', '--env', 'MC_HOST=owned-fixture', '--env', 'MC_PORT=25565', '--env', 'ALLOWED_USERNAME=alice',
                   '--env', 'SYNTHETIC_MODEL_SECRET=' + synthetic_secret,
                   '--entrypoint', 'sleep', image, 'infinity')
            created.append(name)
        config = {'runtime': {'mode': 'minecraft'}, 'experiment': {'profile': 'strict'}, 'server': {'managed_container': names[0]}}
        captured = await capture_deployment(config, {}, {'alice': names[1]}, run_id=run_id)
        encoded = json.dumps(captured)
        assert synthetic_secret not in encoded and 'SYNTHETIC_MODEL_SECRET' not in encoded
        assert captured['server']['image']['image_id'] == actual_image
        assert captured['workspaces']['alice']['image']['image_id'] == actual_image
        assert captured['owned_project'] == identity
        assert set(captured['project_containers']) == set(names)
        assert captured['server']['selected_public_environment'] == {'GAME_CHAT': 'false', 'MC_HOST': 'owned-fixture', 'MC_PORT': '25565', 'ALLOWED_USERNAME': 'alice'}
        assert captured['server']['readonly_rootfs'] is True
        result.update(status='passed', checks=['docker_go_template_split_and_projection', 'actual_immutable_container_image_id',
                      'bounded_own_compose_project_inventory', 'only_four_nonsecret_environment_values', 'synthetic_secret_absent',
                      'workspace_actual_image_and_readonly_identity'], captured=captured)
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)[:1000]
    finally:
        errors = []
        for name in reversed(created):
            try: docker('rm', '-f', name)
            except Exception as exc: errors.append(type(exc).__name__)
        result['cleanup_complete'] = not errors
        if errors: result['status'] = 'failed'; result['cleanup_errors'] = errors
        (ROOT / 'validation/2026-10-21-deployment-projection.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({key: value for key, value in result.items() if key != 'captured'}, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
