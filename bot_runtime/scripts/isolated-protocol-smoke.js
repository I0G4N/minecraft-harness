// Explicit optional privileged network-fixture acceptance. No Vanilla or models.
// --temporary-nft-exemption adds only addresses inspected on two newly owned
// Docker networks to an existing mihomo local-address set, then removes them.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { writeFileSync } from 'node:fs';
import { startWorkerHost } from '../src/worker-host.js';
import { start } from '../src/server.js';
const exemption = process.argv.includes('--temporary-nft-exemption');
const outputIndex = process.argv.indexOf('--output');
const outputPath = outputIndex >= 0 ? process.argv[outputIndex + 1] : null;
const id = `mc-proto-${process.pid}-${crypto.randomUUID().slice(0, 6)}`;
const front = `${id}-front`, back = `${id}-back`, fixtureName = `${id}-fixture`, proxyName = `${id}-identity`;
const ownershipLabel = `mc-harness.protocol-smoke=${id}`;
const launcherId = `${id}-launch`;
const controlImage = process.env.PROTOCOL_CONTROL_IMAGE ?? 'minecraft-harness-control:research';
const workerImage = process.env.WORKER_SANDBOX_IMAGE ?? 'minecraft-harness-worker:research';
const result = { at: new Date().toISOString(), actual_minecraft: false, paid_model: false, protocol_fixture: true, status: 'blocked', checks: [], networks: [], temporary_elements: [], temporary_changes_removed: false, cleanup_complete: false };
const createdNetworks = [], containers = [], added = new Set();
let runtime, launcher, exemptionTimer, polling = false, exemptionError;
function command(executable, args, options = {}) { return execFileSync(executable, args, { encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'pipe'], ...options }).trim(); }
function docker(...args) { return command('docker', args); }
function inspectNetwork(name) {
  const network = JSON.parse(docker('network', 'inspect', name))[0];
  assert.equal(network.Labels?.['mc-harness.protocol-smoke'], id, 'refusing network without this test ownership label');
  assert.equal(network.Internal, true);
  return network;
}
function refreshExemptions() {
  if (!exemption || polling) return;
  polling = true;
  try {
    for (const name of createdNetworks) {
      const network = inspectNetwork(name);
      for (const member of Object.values(network.Containers ?? {})) {
        const address = member.IPv4Address?.split('/')[0];
        if (!address || added.has(address)) continue;
        assert.match(address, /^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.)/);
        let exists = false;
        try { command('nft', ['get', 'element', 'inet', 'mihomo', 'inet4_local_address_set', '{', address, '}']); exists = true; } catch {}
        if (!exists) {
          command('nft', ['add', 'element', 'inet', 'mihomo', 'inet4_local_address_set', '{', address, '}']);
          added.add(address);
        }
      }
    }
  } catch (error) { exemptionError = error; }
  finally { polling = false; }
}
async function poll(fn, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (exemptionError) throw exemptionError;
    try { const value = await fn(); if (value) return value; } catch (error) { if (Date.now() + 100 >= deadline) throw error; }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error('fixture acceptance deadline');
}
function proxyMetrics() { return JSON.parse(docker('exec', proxyName, 'node', '-e', 'fetch("http://127.0.0.1:8081").then(r=>r.text()).then(t=>process.stdout.write(t))')); }
function metrics() { return JSON.parse(docker('exec', fixtureName, 'node', '-e', 'fetch("http://127.0.0.1:8080").then(r=>r.text()).then(t=>process.stdout.write(t))')); }
try {
  if (exemption && process.getuid?.() !== 0) throw new Error('--temporary-nft-exemption requires root; no changes made');
  result.images = { control: docker('image', 'inspect', controlImage, '--format', '{{.Id}}'), worker: docker('image', 'inspect', workerImage, '--format', '{{.Id}}') };
  for (const name of [front, back]) {
    docker('network', 'create', '--internal', '--label', ownershipLabel, ...(name === front ? ['--opt', 'com.docker.network.bridge.gateway_mode_ipv4=isolated'] : []), name);
    createdNetworks.push(name);
    const info = inspectNetwork(name);
    if (name === front) {
      assert.equal(info.Options?.['com.docker.network.bridge.gateway_mode_ipv4'], 'isolated');
      assert.equal(info.IPAM.Config[0].Gateway, undefined);
      const addresses = JSON.parse(command('ip', ['-j', 'address', 'show', 'dev', 'br-' + info.Id.slice(0, 12)]));
      assert.ok(addresses.every(item => !(item.addr_info ?? []).length), 'isolated frontend must have no host bridge addresses');
    }
    result.networks.push({ name, subnet: info.IPAM.Config[0].Subnet, gateway_mode_ipv4: info.Options?.['com.docker.network.bridge.gateway_mode_ipv4'] ?? 'default', host_gateway: info.IPAM.Config[0].Gateway ?? null });
  }
  if (exemption) command('nft', ['list', 'set', 'inet', 'mihomo', 'inet4_local_address_set']);
  const fixtureScript = fileURLToPath(new URL('../fixtures/protocol-server.js', import.meta.url));
  docker('run', '-d', '--name', fixtureName, '--label', ownershipLabel, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--network', back, '--network-alias', 'fixture', '--mount', `type=bind,src=${fixtureScript},dst=/app/protocol-fixture.js,readonly`, '--entrypoint', 'node', controlImage, '/app/protocol-fixture.js'); containers.push(fixtureName);
  const proxyScript = fileURLToPath(new URL('../fixtures/identity-proxy-server.js', import.meta.url));
  docker('run', '-d', '--name', proxyName, '--label', ownershipLabel, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--network', back, '--env', 'MC_HOST=fixture', '--env', 'MC_PORT=25565', '--env', 'ALLOWED_USERNAME=protoAlice', '--env', 'GAME_CHAT=false', '--mount', `type=bind,src=${proxyScript},dst=/app/proxy-fixture.js,readonly`, '--entrypoint', 'node', controlImage, '/app/proxy-fixture.js'); containers.push(proxyName);
  docker('network', 'connect', '--alias', 'identity-proxy', front, proxyName);
  refreshExemptions();
  if (exemption) exemptionTimer = setInterval(refreshExemptions, 100);
  await poll(() => metrics().kind === 'synthetic_protocol_fixture_not_vanilla');
  launcher = await startWorkerHost({ agentId: 'protoAlice', token: 'isolated-protocol-launcher-fixture-token', id: launcherId, port: 0, image: workerImage, network: front, mcHost: 'identity-proxy', identityProxy: true });
  runtime = await start({ port: 0, mode: 'minecraft', lifecycle: 'paper_aligned', token: 'isolated-protocol-admin-fixture-token', startupTimeoutMs: 12000,
    workerEndpoints: { protoAlice: { endpoint: `tcp://127.0.0.1:${launcher.address.port}`, token: 'isolated-protocol-launcher-fixture-token' } } });
  const url = `http://127.0.0.1:${runtime.address.port}`;
  async function req(method, path, body) {
    const response = await fetch(url + path, { method, headers: { authorization: 'Bearer isolated-protocol-admin-fixture-token', 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(20000) });
    const value = await response.json(); assert.ok(response.ok, `HTTP${response.status}: ${JSON.stringify(value)}`); return value;
  }
  const session = await req('POST', '/v1/sessions', { agent_id: 'protoAlice', remaining_run_ms: 60000 });
  assert.equal(session.connected, true); assert.equal(session.worker_transport, 'remote'); assert.equal(session.native_sandbox.native_filter, true); assert.equal(session.execution_boundary.identity_bound, true);
  result.checks.push('remote_native_worker_spawn_through_fixed_identity_proxy');
  const base = '/v1/sessions/protoAlice';
  async function run(source, timeoutMs = 3000) {
    const initial = await req('POST', base + '/programs', { source, timeout_ms: timeoutMs, request_id: crypto.randomUUID(), run_id: 'development', world_epoch: 1 });
    return poll(async () => { const record = await req('GET', base + '/programs/' + initial.execution_id); return !['accepted', 'running'].includes(record.status) && record; }, timeoutMs + 15000);
  }
  const observation = await run(`return {username:bot.username,health:bot.health,blockBelow:bot.blockAt(bot.entity.position.offset(0,-1,0))?.name, inventory:bot.inventory.items().map(x=>({name:x.name,count:x.count}))}`);
  assert.equal(observation.status, 'succeeded'); assert.deepEqual(observation.result, { username: 'protoAlice', health: 20, blockBelow: 'grass_block', inventory: [{ name: 'oak_log', count: 3 }] }); result.checks.push('real_mineflayer_synthetic_chunk_inventory_decode');
  const ablation = await run(`globals.rawChats=0; const listener=(packet,meta)=>{if(['system_chat','player_chat','profileless_chat'].includes(meta.name))globals.rawChats++};bot._client.on('packet',listener);bot.chat('must-not-reach-fixture');await new Promise(resolve=>setTimeout(resolve,450));bot._client.removeListener('packet',listener);return globals.rawChats`);
  assert.equal(ablation.status, 'succeeded'); assert.equal(ablation.result, 0);
  const afterChat = metrics(); assert.equal(afterChat.chats.length, 0); assert.ok(afterChat.chat_packets_sent > 0); result.checks.push('wire_chat_ablation_blocks_outbound_and_raw_inbound_packets');
  const beforeForge = proxyMetrics().rejected;
  const forged = await run(`const create=bot.constructor.constructor('return process')().getBuiltinModule('module').createRequire('/app/probe.js');const protocol=create('minecraft-protocol');let errorSeen=false; const client=protocol.createClient({host:'identity-proxy',port:25565,username:'forgedPlayer',version:'1.20.4',auth:'offline',hideErrors:true});client.on('error',()=>{errorSeen=true});await new Promise(resolve=>{client.once('end',resolve);setTimeout(resolve,1000)});client.end();return {state:client.state,errorSeen}`);
  assert.equal(forged.status, 'succeeded'); assert.deepEqual(metrics().joined.map(x => x.username), ['protoAlice']); assert.equal(proxyMetrics().rejected, beforeForge + 1); result.checks.push('native_forged_identity_rejected_before_backend_login');
  const native = await run(`const req=bot.constructor.constructor('return process')().getBuiltinModule;let writeCode;try{req('fs').writeFileSync('/tmp/probe','x')}catch(e){writeCode=e.code}return {writeCode,spawnCode:req('child_process').spawnSync('/bin/true').error?.code,secretKeys:Object.keys(bot.constructor.constructor('return process')().env).filter(k=>/TOKEN|SECRET|KEY/.test(k))}`);
  assert.deepEqual(native.result, { writeCode: 'EPERM', spawnCode: 'EPERM', secretKeys: [] }); result.checks.push('native_escape_write_exec_denied_no_credentials');
  const timed = await run('globals.oldMarker=1;while(true){}', 100); assert.equal(timed.status, 'timed_out');
  await poll(() => metrics().ended.length >= 1);
  const again = await run('return {lost:globals.oldMarker===undefined,health:bot.health}'); assert.equal(again.status, 'succeeded'); assert.deepEqual(again.result, { lost: true, health: 20 }); assert.equal(again.runtime_epoch, 2); result.checks.push('deadline_removes_container_and_reconnects_fixed_identity');
  result.proxy = proxyMetrics(); assert.ok(result.proxy.filtered > 0);
  result.fixture = metrics(); assert.deepEqual(result.fixture.errors, []);
  assert.equal((await req('DELETE', base)).confirmed_old_process_exit, true); result.checks.push('confirmed_remote_container_removal');
  result.status = 'passed';
} catch (error) {
  result.error = String(error.message).slice(0, 3000);
  result.status = result.checks.length ? 'failed' : 'blocked';
} finally {
  clearInterval(exemptionTimer);
  try { await runtime?.close(); } catch (error) { result.cleanup_error = String(error.message); }
  try { await launcher?.close(); } catch (error) { result.cleanup_error = String(error.message); }
  for (const container of containers.reverse()) { try { docker('rm', '-f', container); } catch (error) { result.cleanup_error = String(error.message); } }
  for (const address of added) {
    try { command('nft', ['delete', 'element', 'inet', 'mihomo', 'inet4_local_address_set', '{', address, '}']); }
    catch (error) { result.cleanup_error = String(error.message); }
  }
  result.temporary_elements = [...added]; result.temporary_changes_removed = !result.cleanup_error;
  for (const network of createdNetworks.reverse()) { try { docker('network', 'rm', network); } catch (error) { result.cleanup_error = String(error.message); } }
  const left = docker('ps', '-aq', '--filter', `label=mc-harness.worker-host=${launcherId}`);
  result.cleanup_complete = !left && !result.cleanup_error;
  if (!result.cleanup_complete) result.status = 'failed';
}
const encoded = JSON.stringify(result, null, 2) + '\n';
if (outputPath) writeFileSync(outputPath, encoded);
process.stdout.write(encoded);
process.exitCode = result.status === 'passed' ? 0 : result.status === 'blocked' ? 2 : 1;
