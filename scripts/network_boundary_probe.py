"""Compare only owned host/peer fixtures on ordinary vs isolated Docker bridges.

No global firewall or proxy state is changed. A pre-existing proxy may already
block ordinary traffic; that is reported as inconclusive rather than as proof of
the ordinary network's isolation. The isolated case also verifies no host bridge
address through a trusted operator-side interface inspection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def command(*arguments, check=True):
    return subprocess.run(arguments, capture_output=True, text=True, timeout=20, check=check)


def probe(image, network, targets):
    source = r'''
const net=require('net');const targets=JSON.parse(process.argv[1]);
(async()=>{const result={};for(const [name,target]of Object.entries(targets)){
result[name]=await new Promise(resolve=>{let output='';let finished=false;
const client=net.createConnection({host:target.host,port:target.port},()=>{client.end('GET / HTTP/1.0\r\n\r\n')});
function done(value){if(finished)return;finished=true;client.destroy();resolve(value)}
client.on('data',chunk=>output+=chunk);client.on('end',()=>done({connected:true,response:output.slice(0,300)}));
client.on('error',error=>done({connected:false,error:error.code}));client.setTimeout(1200,()=>done({connected:false,error:'timeout'}));});}
console.log(JSON.stringify(result));})().catch(()=>process.exit(1));'''
    result = command('docker', 'run', '--rm', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--network', network, '--dns', '127.0.0.1', '--entrypoint', 'node', image, '-e', source, json.dumps(targets))
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'validation/2026-10-21-network-boundary.json')
    args = parser.parse_args()
    identity = 'mc-net-boundary-' + secrets.token_hex(5)
    networks, cleanup_errors = [], []
    listener = socket.socket(); listener.bind(('0.0.0.0', 0)); listener.listen(); listener.settimeout(.2)
    own_port = listener.getsockname()[1]
    stop = threading.Event()
    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(.5)
                try:
                    connection.recv(4096)
                    body = identity.encode()
                    connection.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
                except OSError:
                    pass
    thread = threading.Thread(target=serve, daemon=True); thread.start()
    result = {'at': datetime.now(timezone.utc).isoformat(), 'actual_minecraft': False, 'paid_model': False,
              'global_policies_modified': False, 'host_service': 'this_test_own_http_fixture_only', 'cases': {}, 'cleanup_complete': False}
    try:
        image = 'minecraft-harness-control:research'
        for mode in ['ordinary', 'isolated']:
            name = identity + '-' + mode
            options = ['--opt', 'com.docker.network.bridge.gateway_mode_ipv4=isolated'] if mode == 'isolated' else []
            command('docker', 'network', 'create', '--internal', '--label', 'mc-harness.network-probe=' + identity, *options, name)
            networks.append(name)
            info = json.loads(command('docker', 'network', 'inspect', name).stdout)[0]
            if info.get('Labels', {}).get('mc-harness.network-probe') != identity:
                raise RuntimeError('Network ownership mismatch')
            gateway = info['IPAM']['Config'][0].get('Gateway')
            subnet = info['IPAM']['Config'][0]['Subnet']
            # Docker reports a logical gateway even in isolated mode; the host
            # bridge has no assigned address. Also test the usual first address.
            import ipaddress
            subnet_gateway = str(next(ipaddress.ip_network(subnet).hosts()))
            bridge = 'br-' + info['Id'][:12]
            addresses = json.loads(command('ip', '-j', 'address', 'show', 'dev', bridge).stdout)
            host_addresses = [entry['local'] for item in addresses for entry in item.get('addr_info', [])]
            targets = {'own_host_service': {'host': gateway or subnet_gateway, 'port': own_port},
                       'own_host_service_at_subnet_first_address': {'host': subnet_gateway, 'port': own_port},
                       'public_network': {'host': '1.1.1.1', 'port': 443},
                       'host_loopback_control_namespace': {'host': '127.0.0.1', 'port': own_port}}
            attempts = probe(image, name, targets)
            result['cases'][mode] = {'network': name, 'subnet': subnet, 'reported_gateway': gateway,
                                     'host_bridge_addresses': host_addresses, 'probes': attempts}
        ordinary = result['cases']['ordinary']; isolated = result['cases']['isolated']
        isolated_blocked = not isolated['host_bridge_addresses'] and all(not item['connected'] for item in isolated['probes'].values())
        ordinary_reachable = any(item['connected'] and identity in item.get('response', '') for key, item in ordinary['probes'].items() if key.startswith('own_host_service'))
        result['ordinary_host_gateway_reachable'] = ordinary_reachable
        result['isolated_boundary_verified'] = isolated_blocked
        result['status'] = 'passed' if isolated_blocked else 'failed'
        if not ordinary_reachable:
            result['ordinary_comparison'] = 'inconclusive_existing_host_policy_may_block_control_case'
    except Exception as exc:
        result['status'] = 'blocked'; result['error'] = type(exc).__name__ + ': ' + str(exc)[:1000]
    finally:
        stop.set(); listener.close(); thread.join(timeout=1)
        for network in reversed(networks):
            try:
                command('docker', 'network', 'rm', network)
            except Exception as exc:
                cleanup_errors.append(str(exc)[:500])
        result['cleanup_complete'] = not cleanup_errors
        if cleanup_errors:
            result['cleanup_errors'] = cleanup_errors; result['status'] = 'failed'
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
