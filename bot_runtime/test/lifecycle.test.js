import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import { createHash } from 'node:crypto';
import { start } from '../src/server.js';

const admin = 'test-supervisor-token-123456789';
const aliceToken = 'test-alice-token-1234567890';
const bobToken = 'test-bob-token-123456789012';

async function fixture(t, options = {}) {
  const runtime = await start({ port: 0, mode: 'fake', token: admin,
    agentTokens: { alice: aliceToken, bob: bobToken }, startupTimeoutMs: 2000, ...options });
  t.after(() => runtime.close());
  const url = `http://127.0.0.1:${runtime.address.port}`;
  async function request(method, path, body, token = admin) {
    const response = await fetch(`${url}${path}`, { method,
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  }
  async function session(agent = 'alice') {
    const created = await request('POST', '/v1/sessions', { agent_id: agent });
    assert.equal(created.status, 201, JSON.stringify(created));
    assert.equal(created.body.mode, 'fake');
    return `/v1/sessions/${agent}`;
  }
  async function submit(base, source, options = {}) {
    const submitted = await request('POST', `${base}/programs`, { source,
      request_id: options.request_id ?? crypto.randomUUID(), timeout_ms: options.timeout_ms ?? 1000,
      world_epoch: options.world_epoch ?? 1, run_id: options.run_id ?? 'development' });
    assert.equal(submitted.status, 202, JSON.stringify(submitted));
    return submitted.body;
  }
  async function wait(base, id, predicate = record => !['accepted', 'running'].includes(record.status)) {
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const result = await request('GET', `${base}/programs/${id}`);
      assert.equal(result.status, 200);
      if (predicate(result.body)) return result.body;
      await delay(10);
    }
    throw new Error(`Execution never reached expected state: ${id}`);
  }
  return { runtime, request, session, submit, wait };
}

test('persistent globals and errors survive regular execution, requests are idempotent', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  const code = 'globalThis.count = (globalThis.count ?? 0) + 1; globals.note = "kept"; console.log("count", count); return count';
  const first = await submit(base, code, { request_id: 'same-request' });
  assert.equal((await wait(base, first.execution_id)).result, 1);
  const duplicate = await submit(base, code, { request_id: 'same-request' });
  assert.equal(first.execution_id, duplicate.execution_id);
  const conflict = await request('POST', `${base}/programs`, { source: 'return 99', request_id: 'same-request', timeout_ms: 1000, world_epoch: 1, run_id: 'development' });
  assert.equal(conflict.status, 409);
  const failed = await wait(base, (await submit(base, 'throw new Error("deliberate")')).execution_id);
  assert.equal(failed.status, 'failed');
  assert.match(failed.error.message, /deliberate/);
  const second = await wait(base, (await submit(base, 'if (globals.note !== "kept") throw new Error("globals lost"); globalThis.count++; return count')).execution_id);
  assert.equal(second.result, 2);
  assert.equal(second.runtime_epoch, 1);
  const events = (await request('GET', `${base}/events?after_cursor=0&limit=1000`)).body;
  assert.ok(events.events.some(event => event.kind === 'program_log' && event.source === 'agent_claim'));
  const accepted = events.events.find(event => event.kind === 'execution_accepted' && event.execution_id === first.execution_id);
  assert.equal(accepted.source, 'runtime');
  assert.equal(accepted.payload.source, code);
  assert.equal(accepted.payload.timeout_ms, 1000);
  assert.equal(accepted.payload.source_hash, createHash('sha256').update(code).digest('hex'));
  assert.equal(first.source_hash, accepted.payload.source_hash);
  assert.deepEqual(events.events.map(event => event.cursor), Array.from({ length: events.events.length }, (_, index) => index + 1));
});

test('external watchdog kills synchronous infinite loops and resets globals before next program', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  const first = await submit(base, 'globalThis.marker = "old"; while (true) {}', { timeout_ms: 120 });
  const second = await submit(base, 'return typeof marker');
  assert.equal((await request('GET', '/health')).status, 200);
  const timedOut = await wait(base, first.execution_id);
  assert.equal(timedOut.status, 'timed_out');
  assert.equal(timedOut.reset_reason, 'timeout');
  const next = await wait(base, second.execution_id);
  assert.equal(next.result, 'undefined');
  assert.equal(next.runtime_epoch, 2);
  const events = (await request('GET', `${base}/events?limit=1000`)).body.events;
  const reset = events.find(event => event.kind === 'runtime_reset');
  const reconnect = events.find(event => event.kind === 'runtime_ready' && event.runtime_epoch === 2);
  assert.equal(reset.payload.confirmed_old_process_exit, true);
  assert.ok(reset.cursor < reconnect.cursor);
});

test('queued cancellation prevents execution; running cancellation kills and confirms exit', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  const first = await submit(base, 'await bot.fake.wait(5000); return "must not return"', { timeout_ms: 10000 });
  await wait(base, first.execution_id, record => record.status === 'running');
  const queued = await submit(base, 'console.log("must not execute"); return 3');
  const cancelledQueued = await request('POST', `${base}/programs/${queued.execution_id}/cancel`);
  assert.equal(cancelledQueued.body.status, 'cancelled');
  assert.equal(cancelledQueued.body.started_at, null);
  const cancelled = await request('POST', `${base}/programs/${first.execution_id}/cancel`);
  assert.equal(cancelled.body.status, 'cancelled');
  const next = await wait(base, (await submit(base, 'return 9')).execution_id);
  assert.equal(next.result, 9);
  assert.equal(next.runtime_epoch, 2);
  const events = (await request('GET', `${base}/events?limit=1000`)).body.events;
  assert.ok(!events.some(event => event.kind === 'program_log' && event.execution_id === queued.execution_id));
});

test('unawaited native action forces reset, managed timers and listeners are removed without losing safe globals', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  const detached = await wait(base, (await submit(base, 'globalThis.marker=1; bot.fake.wait(5000); return "returned"')).execution_id);
  assert.equal(detached.status, 'succeeded');
  assert.equal(detached.reset_reason, 'unfinished_async_action');
  assert.equal(detached.cleanup.unfinished_actions, 1);
  const installed = await wait(base, (await submit(base, `
    globalThis.counter = 0;
    setInterval(() => counter++, 1);
    bot.on('chat', () => counter++);
    return typeof marker;
  `)).execution_id);
  assert.equal(installed.result, 'undefined');
  assert.equal(installed.cleanup.timers_cleared, 1);
  assert.equal(installed.cleanup.listeners_removed, 1);
  await delay(30);
  const checked = await wait(base, (await submit(base, 'bot.chat("test"); return {counter, listeners: bot.listenerCount("chat")}')).execution_id);
  assert.equal(checked.runtime_epoch, 2);
  assert.deepEqual(checked.result, { counter: 0, listeners: 1 });
  assert.equal((await request('GET', base)).body.mode, 'fake');
});

test('paper async loop after await is interrupted by loop deadline check without reset', async t => {
  const { request, session, submit, wait } = await fixture(t, { leftoverPolicy: 'allow' });
  const base = await session();
  const loop = await wait(base, (await submit(base, 'await bot.fake.wait(20); globals.asyncMarker = "kept"; while (true) {}', { timeout_ms: 500 })).execution_id);
  assert.equal(loop.status, 'timed_out');
  const next = await wait(base, (await submit(base, 'return globals.asyncMarker ?? null')).execution_id);
  assert.equal(next.result, 'kept');
  const events = (await request('GET', `${base}/events?limit=1000`)).body.events;
  assert.ok(!events.some(event => event.kind === 'runtime_reset'));
});

test('paper module surface exposes read-only fs sandbox', async t => {
  const { session, submit, wait } = await fixture(t, { lifecycle: 'paper_aligned' });
  const base = await session();
  const result = await wait(base, (await submit(base, `
    const fs = require('fs');
    let write;
    try { fs.writeFileSync('/tmp/harness-sandbox-write', 'x'); write = 'wrote'; }
    catch (error) { write = error.code || error.name; }
    return {write, read: typeof fs.readFileSync, promises: typeof fs.promises.readFile};
  `)).execution_id);
  assert.equal(result.status, 'succeeded');
  assert.equal(result.result.write, 'EPERM');
  assert.equal(result.result.read, 'function');
  assert.equal(result.result.promises, 'function');
});

test('paper CPU-loop watchdog preserves worker/globals', async t => {
  const { request, session, submit, wait } = await fixture(t, { leftoverPolicy: 'allow' });
  const base = await session();
  const loop = await wait(base, (await submit(base, 'globals.cpuMarker = "kept"; while (true) {}', { timeout_ms: 120 })).execution_id);
  assert.equal(loop.status, 'timed_out');
  const next = await wait(base, (await submit(base, 'return globals.cpuMarker ?? null')).execution_id);
  assert.equal(next.result, 'kept');
  const events = (await request('GET', `${base}/events?limit=1000`)).body.events;
  assert.ok(!events.some(event => event.kind === 'runtime_reset'));
});

test('paper leftover policy retains async actions and globals without reset', async t => {
  const { request, session, submit, wait } = await fixture(t, { leftoverPolicy: 'allow' });
  const base = await session();
  const first = await wait(base, (await submit(base, 'globals.marker = "kept"; bot.fake.wait(5000); return "returned"')).execution_id);
  assert.equal(first.status, 'succeeded');
  assert.equal(first.reset_reason, null);
  assert.equal(first.cleanup.cleanup?.unfinished_actions ?? first.cleanup.unfinished_actions, 1);
  const second = await wait(base, (await submit(base, 'return {marker: globals.marker ?? null, epoch: typeof runtimeEpoch}')).execution_id);
  assert.equal(second.result.marker, 'kept');
  // A timed-out program is detached, not killed/reset; globals and the worker survive.
  const timed = await wait(base, (await submit(base, 'globals.timedMarker = "survived"; await bot.fake.wait(5000); return 1', { timeout_ms: 80 })).execution_id);
  assert.equal(timed.status, 'timed_out');
  assert.equal(timed.reset_reason, 'timeout');
  const after = await wait(base, (await submit(base, 'return globals.timedMarker ?? null')).execution_id);
  assert.equal(after.result, 'survived');
  const events = (await request('GET', `${base}/events?limit=1000`)).body.events;
  assert.ok(!events.some(event => event.kind === 'runtime_reset'));
});

test('agent credentials are bound to own session; administrative lifecycle is privileged', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const alice = await session();
  const bob = await session('bob');
  assert.equal((await request('GET', alice, undefined, aliceToken)).status, 200);
  assert.equal((await request('GET', bob, undefined, aliceToken)).status, 403);
  assert.equal((await request('POST', '/v1/sessions', { agent_id: 'other' }, aliceToken)).status, 403);
  assert.equal((await request('DELETE', alice, undefined, aliceToken)).status, 403);
  assert.equal((await request('POST', `${alice}/programs`, { agent_id: 'bob', request_id: 'cross', source: 'return 1' }, aliceToken)).status, 403);
  const a = await wait(alice, (await submit(alice, 'globalThis.privateValue=6; return privateValue')).execution_id);
  const b = await wait(bob, (await submit(bob, 'return typeof privateValue')).execution_id);
  assert.equal(a.result, 6);
  assert.equal(b.result, 'undefined');
  await wait(alice, (await submit(alice, 'bot.chat("fake-team-message"); return true')).execution_id);
  await delay(20);
  const bobEvents = (await request('GET', `${bob}/events?limit=1000`, undefined, bobToken)).body.events;
  assert.ok(bobEvents.some(event => event.kind === 'bot_chat' && event.payload[0] === 'alice' && event.payload[1] === 'fake-team-message'));
});

test('module allowlist denies host operations and child environment contains no runtime credentials', async t => {
  const { session, submit, wait } = await fixture(t);
  const base = await session();
  const denied = await wait(base, (await submit(base, 'return require("node:fs").readFileSync("/etc/hostname", "utf8")')).execution_id);
  assert.equal(denied.status, 'failed');
  assert.match(denied.error.message, /Module is not enabled/);
  // Deliberately use a known vm escape to verify environment stripping. This proves
  // why vm/allowlists are not a sandbox; deployment must provide OS isolation.
  const env = await wait(base, (await submit(base, 'return bot.constructor.constructor("return process.env")()')).execution_id);
  assert.equal(env.status, 'succeeded');
  assert.equal(env.result.TOKEN, undefined);
  assert.equal(env.result.AGENT_TOKENS_JSON, undefined);
  assert.equal(env.result.OPENAI_API_KEY, undefined);
  assert.ok(env.result.BOT_WORKER_CONFIG);
});

test('deleting a session fences active execution before recreating its game identity', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  const active = await submit(base, 'while(true){}', { timeout_ms: 10000 });
  await wait(base, active.execution_id, record => record.status === 'running');
  const closed = await request('DELETE', base);
  assert.equal(closed.status, 200);
  assert.equal(closed.body.deleted, true);
  assert.equal(closed.body.confirmed_old_process_exit, true);
  assert.ok(closed.body.events.some(event => event.kind === 'execution_accepted' && event.payload.source === 'while(true){}'));
  assert.ok(closed.body.events.some(event => event.kind === 'execution_interrupted' && event.execution_id === active.execution_id));
  assert.equal(closed.body.next_cursor, closed.body.events.at(-1).cursor);
  assert.equal((await request('GET', base)).status, 404);
  await session();
  assert.equal((await wait(base, (await submit(base, 'return "new-runtime"')).execution_id)).result, 'new-runtime');
});

test('single-bot container identity cannot be changed even by the supervisor', async t => {
  const runtime = await start({ mode: 'fake', port: 0, token: admin, allowedAgentId: 'alice' });
  t.after(() => runtime.close());
  async function create(body) {
    const response = await fetch(`http://127.0.0.1:${runtime.address.port}/v1/sessions`, {
      method: 'POST', headers: { authorization: `Bearer ${admin}`, 'content-type': 'application/json' }, body: JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  }
  assert.equal((await create({ agent_id: 'bob' })).status, 403);
  assert.equal((await create({ agent_id: 'alice', username: 'bob' })).status, 403);
  assert.equal((await create({ agent_id: 'alice' })).status, 201);
});

test('program output and mutable bot observations retain untrusted provenance', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  const execution = await wait(base, (await submit(base, `
    bot.health = 999;
    bot.emit('chat', 'claimed_sender', 'fabricated_message');
    console.log('claimed_environment_success');
    return 'claimed_task_success';
  `)).execution_id);
  assert.equal(execution.status, 'succeeded');
  assert.equal(execution.after.health, 999);
  const events = (await request('GET', `${base}/events?limit=1000`)).body.events;
  assert.ok(events.some(event => event.kind === 'bot_chat' && event.source === 'bot_client' && event.payload[1] === 'fabricated_message'));
  assert.ok(events.some(event => event.kind === 'program_log' && event.source === 'agent_claim' && event.payload.message === 'claimed_environment_success'));
  assert.ok(!events.some(event => event.source === 'server_admin' || event.kind === 'task_succeeded'));
});

test('world epoch fences stale submissions and checks zero without truthiness shortcuts', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  assert.equal((await request('POST', '/v1/sessions', { agent_id: 'alice', world_epoch: 0 })).status, 409);
  const body = { request_id: 'epoch-request', source: 'return 3', timeout_ms: 1000, run_id: 'development' };
  for (const epoch of [undefined, null, '1', 1.5, -1]) {
    assert.equal((await request('POST', `${base}/programs`, { ...body, world_epoch: epoch })).status, 400);
  }
  assert.equal((await request('POST', `${base}/programs`, { ...body, world_epoch: 0 })).status, 409);
  const accepted = await submit(base, body.source);
  assert.equal((await wait(base, accepted.execution_id)).result, 3);
  const cursor = (await request('GET', base)).body.event_cursor;
  const final = await request('DELETE', `${base}?after_cursor=${cursor}`);
  assert.equal(final.body.deleted, true);
  assert.ok(final.body.events.every(event => event.cursor > cursor));
  assert.ok(final.body.events.some(event => event.kind === 'runtime_reset'));
  assert.equal((await request('POST', '/v1/sessions', { agent_id: 'alice', world_epoch: 0 })).status, 201);
  assert.equal((await request('POST', `${base}/programs`, { ...body, world_epoch: 1 })).status, 409);
  const zero = await submit(base, 'return 0', { world_epoch: 0 });
  assert.equal((await wait(base, zero.execution_id)).world_epoch, 0);
});

test('run identity fences delayed requests from an earlier run even when epoch is reused', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  assert.equal((await request('POST', '/v1/sessions', { agent_id: 'alice', run_id: 'another-run' })).status, 409);
  const body = { request_id: 'run-request', source: 'return 7', timeout_ms: 1000, world_epoch: 1 };
  assert.equal((await request('POST', `${base}/programs`, body)).status, 400);
  assert.equal((await request('POST', `${base}/programs`, { ...body, run_id: 'another-run' })).status, 409);
  const oldRun = await request('DELETE', base);
  assert.ok(oldRun.body.events.every(event => event.run_id === 'development'));
  assert.equal((await request('POST', '/v1/sessions', { agent_id: 'alice', run_id: 'new-run', world_epoch: 1 })).status, 201);
  assert.equal((await request('GET', base)).body.run_id, 'new-run');
  assert.equal((await request('POST', `${base}/programs`, { ...body, run_id: 'development' })).status, 409);
  const record = await wait(base, (await submit(base, 'return 9', { run_id: 'new-run' })).execution_id);
  assert.equal(record.run_id, 'new-run');
  assert.equal(record.result, 9);
  const newEvents = (await request('GET', `${base}/events?limit=1000`)).body.events;
  assert.ok(newEvents.every(event => event.run_id === 'new-run'));
});

test('administrative pause rejects direct submissions and holds queued work until resume', async t => {
  const { request, session, submit, wait } = await fixture(t);
  const base = await session();
  const first = await submit(base, 'await bot.fake.wait(100); return "first"');
  await wait(base, first.execution_id, record => record.status === 'running');
  const queued = await submit(base, 'return "queued"');
  assert.equal((await request('POST', `${base}/pause`, {}, aliceToken)).status, 403);
  assert.equal((await request('POST', `${base}/pause`, {})).body.paused, true);
  const rejected = await request('POST', `${base}/programs`, {
    source: 'return "must not dispatch"', request_id: 'paused', run_id: 'development', world_epoch: 1,
  }, aliceToken);
  assert.equal(rejected.status, 409);
  assert.equal((await wait(base, first.execution_id)).result, 'first');
  await delay(20);
  assert.equal((await request('GET', `${base}/programs/${queued.execution_id}`)).body.status, 'accepted');
  assert.equal((await request('POST', `${base}/resume`, {})).body.paused, false);
  assert.equal((await wait(base, queued.execution_id)).result, 'queued');
  const createdPaused = await request('POST', '/v1/sessions', { agent_id: 'bob', paused: true });
  assert.equal(createdPaused.body.paused, true);
  assert.equal(createdPaused.body.connected, true);
  assert.equal((await request('POST', '/v1/sessions/bob/programs', {
    source: 'return 1', request_id: 'initial-pause', run_id: 'development', world_epoch: 1,
  })).status, 409);
  await request('POST', '/v1/sessions/bob/resume', {});
  assert.equal((await wait('/v1/sessions/bob', (await submit('/v1/sessions/bob', 'return 1')).execution_id)).result, 1);
});
