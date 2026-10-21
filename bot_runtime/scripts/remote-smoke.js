// Explicit Docker integration test; starts no Vanilla server and no paid model.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
import { createRemoteWorker } from '../src/remote-worker.js';
import { startWorkerHost } from '../src/worker-host.js';
import { start } from '../src/server.js';
const launcherId = `acceptance-${process.pid}`;
const launcher = await startWorkerHost({ agentId: 'alice', token: 'offline-launcher-fixture-token', id: launcherId, port: 0, maxWatchdogMs: 500 });
let runtime;
const checks = [];
try {
  runtime = await start({ port: 0, mode: 'fake', token: 'offline-control-fixture-token', lifecycle: 'paper_aligned', startupTimeoutMs: 10000, parentWatchdogMs: 500,
    workerEndpoints: { alice: { endpoint: `tcp://127.0.0.1:${launcher.address.port}`, token: 'offline-launcher-fixture-token' } } });
  const url = `http://127.0.0.1:${runtime.address.port}`;
  async function req(method, path, body) {
    const response = await fetch(url + path, { method, headers: { authorization: 'Bearer offline-control-fixture-token', 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const result = await response.json(); assert.ok(response.ok, JSON.stringify(result)); return result;
  }
  const created = await req('POST', '/v1/sessions', { agent_id: 'alice', remaining_run_ms: 20000 });
  assert.equal(created.connected, true); assert.equal(created.worker_pid, null); checks.push('separate_docker_worker_ready');
  const base = '/v1/sessions/alice';
  async function run(source, timeout_ms = 1000) {
    let record = await req('POST', base + '/programs', { source, timeout_ms, run_id: 'development', world_epoch: 1, request_id: crypto.randomUUID() });
    const deadline = Date.now() + timeout_ms + 3000;
    while (['running', 'accepted'].includes(record.status) && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 20)); record = await req('GET', base + '/programs/' + record.execution_id);
    }
    return record;
  }
  const first = await run('globals.ticks=0;return jobs.start(()=>{setInterval(()=>globals.ticks++,10)},{ttl_ms:5000})');
  assert.equal(first.status, 'succeeded'); assert.equal((await req('GET', base + '/jobs')).jobs.length, 1);
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.ok((await run('return globals.ticks')).result > 0); checks.push('background_survives_foreground');
  assert.equal((await req('POST', `${base}/jobs/${first.result}/cancel`, {})).status, 'cancelled'); checks.push('cooperative_job_cancel');
  // vm escape probes deliberately bypass the JS module allowlist, proving the
  // independent native boundary rather than merely a require() rejection.
  const probe = await run(`const req=bot.constructor.constructor('return process')().getBuiltinModule;
    const fs=req('fs'),cp=req('child_process');
    let writeCode,spawnCode;try{fs.writeFileSync('/tmp/probe','x')}catch(e){writeCode=e.code}
    spawnCode=cp.spawnSync('/bin/true').error?.code;
    return {writeCode,spawnCode,env:Object.keys(bot.constructor.constructor('return process')().env).filter(k=>/TOKEN|SECRET|KEY/.test(k))}`);
  assert.deepEqual(probe.result, { writeCode: 'EPERM', spawnCode: 'EPERM', env: [] }); checks.push('native_write_exec_denied_without_admin_credentials');
  const timed = await run('while(true){}', 100); assert.equal(timed.status, 'timed_out'); checks.push('control_survives_worker_infinite_loop');
  assert.equal((await run('return typeof globals.ticks')).result, 'undefined'); checks.push('hard_reset_discards_globals_no_replay');
  const closed = await req('DELETE', base); assert.equal(closed.confirmed_old_process_exit, true); checks.push('confirmed_sandbox_removal');
  await runtime.close(); runtime = null;
  const remote = createRemoteWorker({ endpoint: `tcp://127.0.0.1:${launcher.address.port}`, token: 'offline-launcher-fixture-token', workerConfig: { mode: 'fake', username: 'alice', lifecycle: 'paper_aligned', maxBackgroundJobs: 8, remainingRunMs: 20000 }, instanceId: 'disconnect-fixture', agentId: 'alice', watchdogMs: 500 });
  remote.on('error', () => {});
  await new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error('remote ready deadline')), 5000); remote.on('message', message => { if (message.type === 'ready') { clearTimeout(timer); resolve(); } }); });
  remote.send({ type: 'execute', execution_id: crypto.randomUUID(), source: 'while(true){}' });
  await new Promise(resolve => setTimeout(resolve, 30));
  const disconnectedExit = new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error('remote disconnect did not fence')), 5000); remote.once('exit', () => { clearTimeout(timer); resolve(); }); });
  remote.disconnect(); await disconnectedExit; checks.push('control_disconnect_fences_cpu_loop_and_reconciles');
  const crashId = `crash-${process.pid}`;
  const crashHost = spawn(process.execPath, [fileURLToPath(new URL('../src/worker-host.js', import.meta.url))], { env: { PATH: process.env.PATH, WORKER_AGENT_ID: 'alice', WORKER_HOST_TOKEN: 'crash-launcher-fixture-token', WORKER_HOST_ID: crashId, WORKER_PORT: '0' }, stdio: ['ignore', 'pipe', 'pipe'] });
  let crashSocket;
  try {
    const address = await new Promise((resolve, reject) => {
      let data = ''; const timer = setTimeout(() => reject(new Error('crash host ready deadline')), 5000);
      crashHost.stdout.on('data', chunk => { data += chunk; if (data.includes('\n')) { clearTimeout(timer); resolve(JSON.parse(data.split('\n')[0]).address); } });
      crashHost.once('exit', () => { clearTimeout(timer); reject(new Error('crash host exited before ready')); });
    });
    const ready = new Promise((resolve, reject) => {
      let data = ''; const timer = setTimeout(() => reject(new Error('crash worker ready deadline')), 5000);
      crashSocket = net.createConnection({ host: '127.0.0.1', port: address.port }, () => crashSocket.write(JSON.stringify({ type: 'start', token: 'crash-launcher-fixture-token', connection_id: crypto.randomUUID(), agent_id: 'alice', instance_id: 'crash-fixture', watchdog_ms: 10000, worker_config: { mode: 'fake', username: 'alice', lifecycle: 'paper_aligned', remainingRunMs: 20000 } }) + '\n'));
      crashSocket.on('error', () => {});
      crashSocket.on('data', chunk => {
        data += chunk;
        let newline; while ((newline = data.indexOf('\n')) >= 0) { const frame = JSON.parse(data.slice(0, newline)); data = data.slice(newline + 1); if (frame.type === 'worker' && frame.message.type === 'ready') { clearTimeout(timer); resolve(); } }
      });
    });
    await ready;
    crashSocket.write(JSON.stringify({ type: 'worker', message: { type: 'execute', execution_id: crypto.randomUUID(), source: 'while(true){}' } }) + '\n');
    crashHost.kill('SIGKILL'); await new Promise(resolve => crashHost.once('exit', resolve));
    const deadline = Date.now() + 5000;
    let containers;
    do { containers = execFileSync('docker', ['ps', '-aq', '--filter', `label=mc-harness.worker-host=${crashId}`], { encoding: 'utf8' }).trim(); if (containers) await new Promise(resolve => setTimeout(resolve, 50)); } while (containers && Date.now() < deadline);
    assert.equal(containers, ''); checks.push('launcher_sigkill_independent_guardian_removes_cpu_loop');
  } finally {
    crashSocket?.destroy(); if (crashHost.exitCode === null && crashHost.signalCode === null) crashHost.kill('SIGKILL');
    for (const id of execFileSync('docker', ['ps', '-aq', '--filter', `label=mc-harness.worker-host=${crashId}`], { encoding: 'utf8' }).trim().split(/\s+/).filter(Boolean)) execFileSync('docker', ['rm', '-f', id]);
  }
} finally {
  await runtime?.close(); await launcher.close();
  const leftovers = execFileSync('docker', ['ps', '-aq', '--filter', `label=mc-harness.worker-host=${launcherId}`], { encoding: 'utf8' }).trim();
  assert.equal(leftovers, ''); checks.push('no_orphan_containers');
}
console.log(JSON.stringify({ status: 'passed', at: new Date().toISOString(), image: execFileSync('docker', ['image', 'inspect', 'minecraft-harness-worker:research', '--format', '{{.Id}}'], { encoding: 'utf8' }).trim(), checks }, null, 2));
