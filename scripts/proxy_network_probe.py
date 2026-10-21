"""Probe two disposable Docker peers; optional scoped Mihomo exclusion is reverted.

No Vanilla, credentials, model calls, existing networks, route or global chain
policies are touched. The privileged option adds only newly allocated peer /32s.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def command(*args, timeout=20, check=True):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} operation failed ({result.returncode})')
    return result


def main(args):
    if args.temporary_proxy_exemption and os.geteuid() != 0:
        raise ValueError('Scoped nftables test requires root; omit the option for read-only diagnosis')
    identity = 'mc-proxy-probe-' + uuid.uuid4().hex[:10]
    network, server, client = identity, identity + '-server', identity + '-client'
    containers, added, network_created = [], [], False
    result = {'at': datetime.now(timezone.utc).isoformat(), 'actual_minecraft': False, 'paid_model': False,
              'scope': 'two newly created peer addresses only', 'global_policies_modified': False}
    def probe(address):
        source = f"fetch('http://{address}:3001',{{signal:AbortSignal.timeout(1000)}}).then(r=>{{console.log(r.status);process.exit(r.status===200?0:1)}}).catch(()=>process.exit(1))"
        attempt = command('docker', 'exec', client, 'node', '-e', source, timeout=4, check=False)
        return attempt.returncode == 0 and attempt.stdout.strip() == '200'
    try:
        command('docker', 'network', 'create', '--internal', '--label', 'minecraft-harness.probe=' + identity, network)
        network_created = True
        for name, source in [(server, "require('http').createServer((q,s)=>s.end('probe')).listen(3001,'0.0.0.0')"), (client, 'setInterval(()=>{},10000)')]:
            command('docker', 'run', '-d', '--name', name, '--network', network, '--read-only', '--cap-drop=ALL',
                    '--memory=128m', '--pids-limit=32', '--label', 'minecraft-harness.probe=' + identity,
                    args.image, 'node', '-e', source)
            containers.append(name)
        peers = [json.loads(command('docker', 'inspect', name).stdout)[0] for name in containers]
        addresses = [peer['NetworkSettings']['Networks'][network]['IPAddress'] for peer in peers]
        result.update(network=network, addresses=addresses)
        time.sleep(.3)
        result['before'] = probe(addresses[0])
        if args.temporary_proxy_exemption and not result['before']:
            command('nft', 'list', 'set', 'inet', 'mihomo', 'inet4_local_address_set')
            for address in addresses:
                exists = command('nft', 'get', 'element', 'inet', 'mihomo', 'inet4_local_address_set', '{', address, '}', check=False).returncode == 0
                if not exists:
                    command('nft', 'add', 'element', 'inet', 'mihomo', 'inet4_local_address_set', '{', address, '}')
                    added.append(address)
            result['temporary_elements'] = list(added)
            result['with_temporary_exemption'] = probe(addresses[0])
            for address in added.copy():
                command('nft', 'delete', 'element', 'inet', 'mihomo', 'inet4_local_address_set', '{', address, '}')
                added.remove(address)
            result['after_exemption_removed'] = probe(addresses[0])
            result['status'] = 'cause_verified' if not result['before'] and result['with_temporary_exemption'] and not result['after_exemption_removed'] else 'inconclusive'
        else:
            result['status'] = 'passed' if result['before'] else 'blocked'
    finally:
        cleanup = True
        for address in added:
            try:
                command('nft', 'delete', 'element', 'inet', 'mihomo', 'inet4_local_address_set', '{', address, '}')
            except Exception:
                cleanup = False
        for name in reversed(containers):
            try:
                command('docker', 'rm', '-f', name)
            except Exception:
                cleanup = False
        if network_created:
            try:
                command('docker', 'network', 'rm', network)
            except Exception:
                cleanup = False
        result['temporary_changes_removed'] = cleanup
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temporary-proxy-exemption', action='store_true')
    parser.add_argument('--image', default='minecraft-harness-control:research')
    parser.add_argument('--output', type=Path, default=Path('validation/proxy-network-probe.json'))
    main(parser.parse_args())
