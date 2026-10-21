import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { once } from 'node:events';
import { start } from '../src/server.js';
import { boundedValue } from '../src/limits.js';

const token = 'longrun-supervisor-token-12345';
const agentToken = 'longrun-agent-token-123456789';
async function fixture(t, options = {}) {
  const runtime = await start({ mode: 'fake', port: 0, token, agentTokens: { alice: agentToken }, ...options });
  t.after(() => runtime.close());
  const url = `http://127.0.0.1:${runtime.address.port}`;
  async function req(method, path, body, credential = token) {
    const response = await fetch(url + path, { method, headers: { authorization: `Bearer ${credential}`, 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  }
  const created = await req('POST', '/v1/sessions', { agent_id: 'alice', run_id: 'longrun', world_epoch: 4 });
  assert.equal(created.status, 201, JSON.stringify(created));
  const base = '/v1/sessions/alice';
  const identity = { session_id: created.body.session_id, run_id: 'longrun', world_epoch: 4 };
  async function submit(source, sequence, extra = {}) {
    return req('POST', base + '/programs', { source, request_id: `request-${sequence}`, request_sequence: sequence,
      timeout_ms: 1000, ...identity, ...extra });
  }
  async function wait(id) {
    for (let i = 0; i < 500; i++) {
      const record = await req('GET', base + '/programs/' + id);
      assert.equal(record.status, 200, JSON.stringify(record));
      if (!['accepted', 'running'].includes(record.body.status)) return record.body;
      await delay(5);
    }
    throw new Error('Execution did not finish');
  }
  async function ack() {
    const feed = await req('GET', base + '/events?after_cursor=' + ((await req('GET', base)).body.acked_cursor) + '&limit=1000');
    assert.equal(feed.status, 200);
    assert.equal(feed.body.has_more, false);
    const response = await req('POST', base + '/events/ack', { ...identity, cursor: feed.body.next_cursor });
    assert.equal(response.status, 200, JSON.stringify(response));
    return response.body;
  }
  return { runtime, req, submit, wait, ack, base, identity, created: created.body };
}

test('bounded serializer avoids getters, handles cycles/BigInt and bounds pathological results', () => {
  let called = false;
  const input = { value: 4n, get danger() { called = true; throw new Error('must not run'); } };
  input.self = input;
  const result = boundedValue(input, 1024);
  assert.equal(called, false);
  assert.equal(result.truncated, true);
  assert.equal(result.value.value, '4');
  assert.equal(result.value.self, '[Circular]');
  assert.ok(Buffer.byteLength(JSON.stringify(boundedValue('x'.repeat(1000000), 2048).value)) < 2048);
});

test('durable ACK trims events and terminal records; retired requests cannot replay', async t => {
  const f = await fixture(t, { maxRetainedExecutions: 3, maxQueue: 2, maxEvents: 32 });
  let lastCursor = 0;
  let first;
  for (let n = 1; n <= 80; n++) {
    const submitted = await f.submit('globals.count=(globals.count??0)+1; return globals.count', n);
    assert.equal(submitted.status, 202, JSON.stringify(submitted));
    if (n === 1) first = submitted.body;
    assert.equal((await f.wait(submitted.body.execution_id)).result, n);
    const state = await f.ack();
    assert.ok(state.event_cursor > lastCursor);
    assert.equal(state.buffers.events, 0);
    assert.equal(state.buffers.event_bytes, 0);
    assert.ok(state.buffers.executions <= 3);
    assert.ok(state.buffers.requests <= 3);
    assert.equal(state.buffers.request_tombstones, 0);
    lastCursor = state.event_cursor;
  }
  assert.equal((await f.submit('globals.count=(globals.count??0)+1; return globals.count', 1)).status, 410);
  assert.equal((await f.submit('return 90', 1, { request_id: 'unknown-old-sequence' })).status, 410);
  assert.equal((await f.req('GET', f.base + '/programs/' + first.execution_id)).status, 404);
  assert.equal((await f.req('GET', f.base + '/events?after_cursor=0')).status, 410);
  const repeatedAck = await f.req('POST', f.base + '/events/ack', { ...f.identity, cursor: lastCursor });
  assert.equal(repeatedAck.status, 200);
  assert.equal(repeatedAck.body.acked_cursor, lastCursor);
  assert.equal((await f.req('POST', f.base + '/events/ack', { ...f.identity, cursor: lastCursor + 1 })).status, 400);
  assert.equal((await f.req('POST', f.base + '/events/ack', { ...f.identity, cursor: lastCursor }, agentToken)).status, 403);
  const health = (await f.req('GET', '/health')).body;
  assert.equal(health.protocol_version, 3);
  assert.equal(health.capabilities.durable_ack, true);
  assert.ok(health.memory.rss > 0);
  assert.equal(health.limits.maxRetainedExecutions, 3);
});

test('unacknowledged event overflow fences worker and is explicitly visible instead of dropping history silently', async t => {
  const f = await fixture(t, { maxEvents: 8 });
  const active = await f.submit('for(let i=0;i<20;i++){ console.log(i); await bot.fake.wait(1); } await bot.fake.never()', 1);
  assert.equal(active.status, 202);
  const result = await f.wait(active.body.execution_id);
  assert.equal(result.status, 'interrupted');
  assert.equal(result.reset_reason, 'event_buffer_overflow');
  const state = (await f.req('GET', f.base)).body;
  assert.equal(state.paused, true);
  assert.equal(state.connected, false);
  assert.ok(state.buffers.events <= 8);
  assert.equal(state.reliability_fault.kind, 'event_buffer_overflow');
  const feed = (await f.req('GET', f.base + '/events?limit=1000')).body;
  assert.equal(feed.reliability_fault.kind, 'event_buffer_overflow');
  assert.ok(feed.dropped_events > 0);
  assert.equal((await f.req('POST', f.base + '/resume', {})).status, 409);
  assert.equal((await f.submit('return 1', 2)).status, 409);
});

test('supervisor lease expiry interrupts running/queued code and cannot be revived by late heartbeat', async t => {
  const f = await fixture(t, { leaseTimeoutMs: 140 });
  assert.equal((await f.req('POST', f.base + '/heartbeat', f.identity, agentToken)).status, 403);
  assert.equal((await f.req('POST', f.base + '/heartbeat', { ...f.identity, session_id: 'stale' })).status, 409);
  const active = await f.submit('while(true){}', 1, { timeout_ms: 10000 });
  const queued = await f.submit('return "must not execute"', 2);
  const result = await f.wait(active.body.execution_id);
  assert.equal(result.status, 'interrupted');
  assert.equal(result.reset_reason, 'supervisor_lease_expired');
  assert.equal((await f.wait(queued.body.execution_id)).status, 'interrupted');
  const state = (await f.req('GET', f.base)).body;
  assert.equal(state.lease_expired, true);
  assert.equal(state.connected, false);
  assert.equal((await f.req('POST', f.base + '/heartbeat', f.identity)).status, 409);
  assert.equal((await f.req('POST', f.base + '/resume', {})).status, 409);
});

test('heartbeat keeps long programs alive; stale session submissions cannot enter a recreated session', async t => {
  const f = await fixture(t, { leaseTimeoutMs: 160 });
  const heartbeat = setInterval(() => f.req('POST', f.base + '/heartbeat', f.identity).catch(() => {}), 35);
  t.after(() => clearInterval(heartbeat));
  const active = await f.submit('await bot.fake.wait(420); return "still alive"', 1);
  assert.equal((await f.wait(active.body.execution_id)).result, 'still alive');
  clearInterval(heartbeat);
  await f.req('DELETE', f.base);
  await f.req('POST', '/v1/sessions', { agent_id: 'alice', run_id: 'longrun', world_epoch: 4 });
  assert.equal((await f.submit('return 1', 2)).status, 409);
});

test('program log spam and very large return values remain bounded and report truncation', async t => {
  const f = await fixture(t, { maxLogs: 5, maxLogBytes: 1000, maxResultBytes: 2048 });
  const submitted = await f.submit('for(let i=0;i<10000;i++)console.log("x".repeat(800)); return "z".repeat(1000000)', 1);
  const record = await f.wait(submitted.body.execution_id);
  assert.equal(record.status, 'succeeded');
  assert.ok(record.logs.length <= 5);
  assert.ok(record.cleanup.log_bytes <= 1000);
  assert.ok(record.cleanup.logs_suppressed > 0);
  assert.equal(record.output_truncated, true);
  assert.ok(Buffer.byteLength(JSON.stringify(record.result)) < 2048);
  const feed = (await f.req('GET', f.base + '/events?limit=1000')).body;
  const terminal = feed.events.find(event => event.kind === 'execution_succeeded');
  assert.equal(terminal.payload.record.output_truncated, true);
  assert.equal(terminal.session_id, f.identity.session_id);
});

test('queue limit rejects excess admission without accepting a hidden action', async t => {
  const f = await fixture(t, { maxQueue: 1 });
  const active = await f.submit('await bot.fake.wait(500); return true', 1);
  await delay(15);
  assert.equal((await f.submit('return 2', 2)).status, 202);
  assert.equal((await f.submit('return 3', 3)).status, 429);
  assert.equal((await f.wait(active.body.execution_id)).status, 'succeeded');
  assert.equal((await f.submit('return 3', 3)).status, 202);
});

async function processAlive(pid) {
  try {
    const stat = await readFile(`/proc/${pid}/stat`, 'utf8');
    return !stat.slice(stat.lastIndexOf(')') + 2).startsWith('Z');
  } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

test('independent guardian kills synchronous worker when HTTP runtime is SIGKILLed', { skip: process.platform !== 'linux', timeout: 7000 }, async t => {
  const parent = spawn(process.execPath, ['src/server.js'], { cwd: new URL('..', import.meta.url), env: { PATH: process.env.PATH, MODE: 'fake', PORT: '0', TOKEN: token, PARENT_WATCHDOG_MS: '400' }, stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '';
  const listening = new Promise((resolve, reject) => {
    parent.stdout.on('data', chunk => { stdout += chunk; if (stdout.includes('\n')) { try { resolve(JSON.parse(stdout.split('\n')[0])); } catch (error) { reject(error); } } });
    parent.once('exit', code => reject(new Error('Parent exited before listen: ' + code)));
  });
  let workerPid;
  t.after(async () => { parent.kill('SIGKILL'); if (workerPid && await processAlive(workerPid)) { try { process.kill(-workerPid, 'SIGKILL'); } catch {} } });
  const address = (await listening).address;
  const url = `http://127.0.0.1:${address.port}`;
  const create = await fetch(url + '/v1/sessions', { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify({ agent_id: 'alice' }) });
  workerPid = (await create.json()).worker_pid;
  assert.ok(await processAlive(workerPid));
  await fetch(url + '/v1/sessions/alice/programs', { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify({ source: 'while(true){}', request_id: 'busy', run_id: 'development', world_epoch: 1, timeout_ms: 10000 }) });
  await delay(30);
  const exited = once(parent, 'exit');
  parent.kill('SIGKILL');
  await exited;
  for (let i = 0; i < 100 && await processAlive(workerPid); i++) await delay(20);
  assert.equal(await processAlive(workerPid), false, 'orphaned infinite-loop worker survived parent death');
});

test('unexpected worker exit fences descendants from its original process group', { skip: process.platform !== 'linux' }, async t => {
  const f = await fixture(t);
  const submitted = await f.submit(`
    const proc=bot.constructor.constructor('return process')();
    const child=proc.getBuiltinModule('node:child_process').spawn(proc.execPath,
      ['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});
    console.log(child.pid);
    await bot.fake.wait(40);
    proc.exit(9);
  `, 1);
  const record = await f.wait(submitted.body.execution_id);
  assert.equal(record.status, 'interrupted');
  const descendant = Number(record.logs[0].message);
  assert.ok(descendant > 0);
  for (let i = 0; i < 100 && await processAlive(descendant); i++) await delay(5);
  assert.equal(await processAlive(descendant), false);
  const recovered = await f.submit('return "recovered"', 2);
  assert.equal((await f.wait(recovered.body.execution_id)).result, 'recovered');
});

test('independent guardian limits worker RSS without relying on its event loop', { skip: process.platform !== 'linux' }, async t => {
  const f = await fixture(t, { maxWorkerRssBytes: 96 * 1024 * 1024, parentWatchdogMs: 200 });
  const submitted = await f.submit(`
    globals.buffer=bot.constructor.constructor('return Buffer')().alloc(128*1024*1024,1);
    while(true){}
  `, 1, { timeout_ms: 5000 });
  const record = await f.wait(submitted.body.execution_id);
  assert.equal(record.status, 'interrupted');
  const events = (await f.req('GET', f.base + '/events?limit=1000')).body.events;
  assert.ok(events.some(event => event.kind === 'runtime_guardian_fenced' && event.payload.reason === 'worker_rss_limit'));
  const recovered = await f.submit('return "recovered"', 2);
  assert.equal((await f.wait(recovered.body.execution_id)).result, 'recovered');
});

test('two-phase close retains final audit tail across lost responses until persisted ACK and deletion', async t => {
  const f = await fixture(t);
  const active = await f.submit('while(true){}', 1, { timeout_ms: 10000 });
  await delay(15);
  const retained = await f.req('DELETE', f.base + '?retain=true&session_id=' + f.identity.session_id);
  assert.equal(retained.status, 200);
  assert.equal(retained.body.closed, true);
  assert.equal(retained.body.deleted, false);
  assert.equal((await f.wait(active.body.execution_id)).status, 'interrupted');
  assert.equal((await f.req('GET', f.base)).status, 200);
  const retried = await f.req('DELETE', f.base + '?retain=true&session_id=' + f.identity.session_id);
  assert.deepEqual(retried.body.events, retained.body.events);
  assert.equal(retried.body.next_cursor, retained.body.next_cursor);
  await f.req('POST', f.base + '/events/ack', { ...f.identity, cursor: retained.body.next_cursor });
  const final = await f.req('DELETE', f.base + '?after_cursor=' + retained.body.next_cursor + '&session_id=' + f.identity.session_id);
  assert.equal(final.body.deleted, true);
  assert.equal((await f.req('GET', f.base)).status, 404);
  await f.req('POST', '/v1/sessions', { agent_id: 'alice', run_id: 'longrun', world_epoch: 4 });
  assert.equal((await f.req('DELETE', f.base + '?session_id=' + f.identity.session_id)).status, 409);
});

test('non-Error throws stay ordinary failures and sparse arrays retain their indexes', async t => {
  const f = await fixture(t);
  const nulled = await f.submit('globals.kept=true; throw null', 1);
  const nullRecord = await f.wait(nulled.body.execution_id);
  assert.equal(nullRecord.status, 'failed');
  assert.equal(nullRecord.error.message, 'null');
  const checked = await f.submit('if(!globals.kept)throw new Error("globals lost"); return [,,3]', 2);
  const result = await f.wait(checked.body.execution_id);
  assert.equal(result.runtime_epoch, 1);
  assert.deepEqual(result.result, [null, null, 3]);
  assert.equal(result.output_truncated, false);
});

test('self-owned local runtime exits after supervisor lease loss and fences its worker before exiting', { skip: process.platform !== 'linux', timeout: 5000 }, async t => {
  const parent = spawn(process.execPath, ['src/server.js'], { cwd: new URL('..', import.meta.url), env: { PATH: process.env.PATH, MODE: 'fake', PORT: '0', TOKEN: token,
    SUPERVISOR_LEASE_MS: '180', EXIT_ON_LEASE_EXPIRY: 'true' }, stdio: ['ignore', 'pipe', 'pipe'] });
  const exited = once(parent, 'exit');
  t.after(() => parent.kill('SIGKILL'));
  let stdout = '';
  const listening = await new Promise((resolve, reject) => {
    parent.stdout.on('data', chunk => { stdout += chunk; if (stdout.includes('\n')) { try { resolve(JSON.parse(stdout.split('\n')[0])); } catch (error) { reject(error); } } });
    parent.once('error', reject);
  });
  const url = `http://127.0.0.1:${listening.address.port}`;
  const response = await fetch(url + '/v1/sessions', { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify({ agent_id: 'alice' }) });
  const identity = await response.json();
  assert.equal(response.status, 201);
  const workerPid = identity.worker_pid;
  const accepted = await fetch(url + '/v1/sessions/alice/programs', { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify({ source: 'while(true){}', request_id: 'abandoned', run_id: 'development', world_epoch: 1, timeout_ms: 10000 }) });
  assert.equal(accepted.status, 202);
  // The owning supervisor has stopped heartbeating. No terminal response is
  // observed: a durable client must reconcile this accepted outcome as unknown.
  const [code, signal] = await exited;
  assert.equal(code, 0);
  assert.equal(signal, null);
  assert.equal(await processAlive(workerPid), false);
  await assert.rejects(fetch(url + '/health'));
});

test('legacy request IDs remain safely tombstoned after bounded record eviction', async t => {
  const f = await fixture(t, { maxRetainedExecutions: 1 });
  for (let sequence = 1; sequence <= 4; sequence++) {
    const accepted = await f.submit('return 1', sequence, { request_sequence: undefined });
    assert.equal(accepted.status, 202);
    assert.equal((await f.wait(accepted.body.execution_id)).status, 'succeeded');
    await f.ack();
  }
  assert.equal((await f.submit('return 1', 1, { request_sequence: undefined })).status, 410);
  const state = (await f.req('GET', f.base)).body;
  assert.equal(state.buffers.request_tombstones, 3);
  assert.equal(state.buffers.tombstone_bytes, 1048576);
});

test('overflow at the dispatch boundary fences an accepted program before any worker action', async t => {
  const f = await fixture(t, { maxEvents: 2 });
  const accepted = await f.submit('bot.chat("must not execute"); return 1', 1);
  assert.equal(accepted.status, 202);
  const record = await f.wait(accepted.body.execution_id);
  assert.equal(record.status, 'interrupted');
  assert.equal(record.started_at, null);
  assert.equal(record.reset_reason, 'event_buffer_overflow');
  assert.equal((await f.req('GET', f.base)).body.connected, false);
});

test('HTTP rejects non-object JSON and oversized bodies without admitting actions', async t => {
  const f = await fixture(t);
  assert.equal((await f.req('POST', f.base + '/programs', null)).status, 400);
  assert.equal((await f.req('POST', f.base + '/programs', [])).status, 400);
  const oversized = await f.req('POST', f.base + '/programs', { source: 'x'.repeat(1100000) });
  assert.equal(oversized.status, 413);
  assert.equal((await f.req('GET', f.base)).body.buffers.executions, 0);
});
