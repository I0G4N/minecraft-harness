import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { randomUUID } from 'node:crypto';
import { start } from '../src/server.js';

const admin = 'test-supervisor-token-123456789';
const remoteToken = 'x'.repeat(32);

// A fake remote worker: EventEmitter child with the exact surface server.js
// uses (stdout/stderr emitters, send/stop/kill, exitCode/signalCode) that
// speaks the worker<->server protocol. Instances stay in one process so a soft
// reset must reuse the same fake worker object.
function fakeWorkerFactory() {
  const instances = [];
  const options = { autoComplete: true, beforeReady: [] };
  function factory(workerOptions) {
    const worker = new EventEmitter();
    worker.stderr = new EventEmitter();
    worker.stdout = new EventEmitter();
    worker.connected = true;
    worker.remote = false; // server.js marks remote transports itself.
    worker.pid = 9000 + instances.length;
    worker.exitCode = null;
    worker.signalCode = null;
    worker.sent = [];
    worker.options = workerOptions;
    worker.stopped = false;
    worker.killed = false;
    worker.stop = async () => {
      worker.stopped = true;
      if (worker.exitCode === null && worker.signalCode === null) {
        worker.connected = false;
        worker.exitCode = 0;
        queueMicrotask(() => worker.emit('exit', 0, null));
      }
    };
    worker.kill = signal => {
      worker.killed = true;
      worker.connected = false;
      if (worker.exitCode === null && worker.signalCode === null) {
        worker.signalCode = signal ?? 'SIGKILL';
        queueMicrotask(() => worker.emit('exit', null, worker.signalCode));
      }
    };
    worker.send = (message, callback) => {
      worker.sent.push(message);
      if (callback) queueMicrotask(() => callback(null));
      if (message.type === 'execute' && options.autoComplete) {
        queueMicrotask(() => worker.emit('message', {
          type: 'completed', execution_id: message.execution_id,
          result: { source: message.source }, timed_out: false,
        }));
      }
    };
    instances.push(worker);
    queueMicrotask(() => {
      // Optional startup-race messages (e.g. a soft reset arriving before ready).
      for (const message of options.beforeReady) worker.emit('message', message);
      if (!worker.stopped) {
        worker.emit('message', {
          type: 'ready', snapshot: { mode: 'fake', username: workerOptions.agentId },
          generation: 1, module_policy: null, sandbox: null,
        });
      }
    });
    return worker;
  }
  factory.instances = instances;
  factory.options = options;
  return factory;
}

async function fixture(t, options = {}) {
  const factory = fakeWorkerFactory();
  if (options.factorySetup) await options.factorySetup(factory);
  const runtime = await start({
    port: 0, mode: 'fake', token: admin, startupTimeoutMs: 2000,
    softResetTimeoutMs: options.softResetTimeoutMs ?? 5000,
    physicsStallMs: options.physicsStallMs ?? 15000,
    workerEndpoints: { alice: { endpoint: 'tcp://127.0.0.1:1', token: remoteToken } },
    workerFactory: factory,
    ...(options.extra ?? {}),
  });
  t.after(() => runtime.close());
  const url = `http://127.0.0.1:${runtime.address.port}`;

  async function request(method, path, body) {
    const response = await fetch(`${url}${path}`, {
      method,
      headers: { authorization: `Bearer ${admin}`, 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, body: await response.json() };
  }
  async function submit(source, extra = {}) {
    const submitted = await request('POST', '/v1/sessions/alice/programs', {
      source, request_id: extra.request_id ?? randomUUID(),
      timeout_ms: extra.timeout_ms ?? 1000, world_epoch: 1, run_id: 'development',
    });
    assert.equal(submitted.status, 202, JSON.stringify(submitted));
    return submitted.body;
  }
  async function read(executionId) {
    const result = await request('GET', `/v1/sessions/alice/programs/${executionId}`);
    assert.equal(result.status, 200, JSON.stringify(result));
    return result.body;
  }
  async function wait(executionId, predicate = record => !['accepted', 'running'].includes(record.status), timeout = 5000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const record = await read(executionId);
      if (predicate(record)) return record;
      await delay(10);
    }
    throw new Error(`execution ${executionId} never reached expected state`);
  }
  async function waitUntil(predicate, timeout = 3000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await predicate()) return;
      await delay(10);
    }
    throw new Error('condition never became true');
  }
  async function events(after = 0) {
    const page = await request('GET', `/v1/sessions/alice/events?after_cursor=${after}&limit=1000`);
    assert.equal(page.status, 200, JSON.stringify(page));
    return page.body.events;
  }
  async function waitEvent(kind, timeout = 5000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const match = (await events()).find(event => event.kind === kind);
      if (match) return match;
      await delay(20);
    }
    throw new Error(`event ${kind} never arrived`);
  }
  async function describe() {
    const result = await request('GET', '/v1/sessions/alice');
    assert.equal(result.status, 200, JSON.stringify(result));
    return result.body;
  }
  const created = await request('POST', '/v1/sessions', { agent_id: 'alice' });
  assert.equal(created.status, 201, JSON.stringify(created));
  return { runtime, factory, request, submit, read, wait, waitUntil, events, waitEvent, describe, created };
}

test('soft reset gates dispatch and preserves the runtime epoch, globals and ready worker', async t => {
  const f = await fixture(t);
  const first = await f.submit('globalThis.marker = 7; return marker');
  const firstDone = await f.wait(first.execution_id);
  assert.equal(firstDone.status, 'succeeded');
  assert.equal(firstDone.runtime_epoch, 1);

  const worker = f.factory.instances[0];
  const executeCount = () => worker.sent.filter(message => message.type === 'execute').length;
  assert.equal(executeCount(), 1);
  assert.equal((await f.describe()).worker_generation, 1, 'initial ready generation is adopted');
  worker.emit('message', {
    type: 'soft_reset', reason: 'nonfinite_bot_state', execution_id: null,
    generation: 2, detail: { bad_fields: ['pos.x'], recent_api: [] },
  });

  const queued = await f.submit('return globalThis.marker');
  await delay(200);
  assert.equal(executeCount(), 1, 'a queued program must not be dispatched during a soft reset');
  assert.equal((await f.read(queued.execution_id)).status, 'accepted');

  const during = await f.describe();
  assert.equal(during.soft_resetting, true);
  assert.equal(during.soft_resets, 0);
  assert.equal(during.runtime_epoch, 1);
  assert.equal(during.connected, true);
  assert.equal(f.factory.instances.length, 1, 'a soft reset must not spawn a new worker process');

  worker.emit('message', {
    type: 'soft_ready', reason: 'nonfinite_bot_state', generation: 2,
    snapshot: { mode: 'fake', username: 'alice', globals_preserved: true },
    duration_ms: 1234, soft_resets: 1,
  });
  const continued = await f.wait(queued.execution_id);
  assert.equal(continued.status, 'succeeded');
  assert.equal(continued.runtime_epoch, 1);
  assert.deepEqual(continued.result, { source: 'return globalThis.marker' });

  const after = await f.describe();
  assert.equal(after.soft_resetting, false);
  assert.equal(after.soft_resets, 1);
  assert.equal(after.worker_generation, 2);
  assert.equal(after.runtime_epoch, 1);
  assert.equal(after.connected, true);
  assert.equal(worker.stopped, false);

  const recorded = await f.events();
  const soft = recorded.find(event => event.kind === 'runtime_soft_reset');
  assert.deepEqual(soft.payload, {
    reason: 'nonfinite_bot_state', execution_id: null, generation: 2,
    detail: { bad_fields: ['pos.x'], recent_api: [] }, globals_lost: false,
  });
  const ready = recorded.find(event => event.kind === 'runtime_soft_reset_ready');
  assert.deepEqual(ready.payload, {
    generation: 2, duration_ms: 1234, soft_resets: 1,
    snapshot: { mode: 'fake', username: 'alice', globals_preserved: true },
    globals_lost: false,
  });
  assert.ok(!recorded.some(event => event.kind === 'runtime_reset'), 'soft reset must not record a hard reset');
  const hardReady = recorded.filter(event => event.kind === 'runtime_ready');
  assert.equal(hardReady.length, 1);
  assert.equal(hardReady[0].payload.globals_lost, false);
  assert.ok(soft.cursor < ready.cursor);
  assert.equal(soft.runtime_epoch, 1);
  assert.equal(ready.runtime_epoch, 1);
});

test('soft reset fails the in-flight program as interrupted without a hard restart', async t => {
  const f = await fixture(t);
  f.factory.options.autoComplete = false;
  const running = await f.submit('return "long"', { timeout_ms: 10000 });
  await f.wait(running.execution_id, record => record.status === 'running');

  const worker = f.factory.instances[0];
  worker.emit('message', {
    type: 'soft_reset', reason: 'uncaught:TypeError', execution_id: running.execution_id,
    generation: 2, detail: { kind: 'TypeError' },
  });
  const finished = await f.wait(running.execution_id);
  assert.equal(finished.status, 'interrupted');
  assert.equal(finished.reset_reason, 'soft_reset');
  assert.equal(finished.error.name, 'SoftResetError');
  assert.equal(finished.runtime_epoch, 1);
  assert.equal(worker.stopped, false);

  f.factory.options.autoComplete = true;
  worker.emit('message', {
    type: 'soft_ready', reason: 'uncaught:TypeError', generation: 2,
    snapshot: { mode: 'fake', username: 'alice' }, duration_ms: 50, soft_resets: 1,
  });
  const next = await f.submit('return 1');
  const nextDone = await f.wait(next.execution_id);
  assert.equal(nextDone.status, 'succeeded');
  assert.equal(nextDone.runtime_epoch, 1);
  assert.equal(f.factory.instances.length, 1);
  const recorded = await f.events();
  const interrupted = recorded.find(event => event.kind === 'execution_interrupted' && event.execution_id === running.execution_id);
  const softEvent = recorded.find(event => event.kind === 'runtime_soft_reset');
  assert.ok(interrupted && softEvent);
  // Ordering guarantee: the running program's interruption is recorded before
  // runtime_soft_reset.
  assert.ok(interrupted.cursor < softEvent.cursor);
  assert.ok(!recorded.some(event => event.kind === 'runtime_reset'));
});

test('soft reset timeout emits an event and kills the worker so queued work cannot hang', async t => {
  const f = await fixture(t, { softResetTimeoutMs: 5000 });
  const worker = f.factory.instances[0];
  worker.emit('message', {
    type: 'soft_reset', reason: 'physics_stalled', execution_id: null,
    generation: 2, detail: { ms_since_tick: 15000 },
  });
  const timedOut = await f.waitEvent('runtime_soft_reset_timeout', 8000);
  assert.equal(timedOut.payload.timeout_ms, 5000);
  assert.equal(timedOut.payload.reason, 'physics_stalled');
  await f.waitUntil(() => f.factory.instances[0].stopped === true);
  await f.waitUntil(async () => (await f.describe()).connected === false);
  assert.equal((await f.describe()).soft_resetting, false);

  // Hard-restart fallback: the next program starts on a replacement worker.
  const next = await f.submit('return 5');
  const done = await f.wait(next.execution_id, undefined, 8000);
  assert.equal(done.status, 'succeeded');
  assert.equal(f.factory.instances.length, 2);
  assert.equal(done.runtime_epoch, 2);
  // A soft reset timeout is the last-resort HARD restart: runtime_reset must be
  // recorded and the replacement worker's ready event must say globals_lost.
  const recorded = await f.events();
  assert.ok(!recorded.some(event => event.kind === 'runtime_soft_reset_ready'));
  const hardReset = recorded.find(event => event.kind === 'runtime_reset');
  assert.ok(hardReset, 'timeout must record the hard restart');
  assert.equal(hardReset.payload.globals_lost, true);
  assert.equal(hardReset.payload.reason, 'soft_reset_timeout');
  const readyEvents = recorded.filter(event => event.kind === 'runtime_ready');
  assert.equal(readyEvents.length, 2);
  assert.equal(readyEvents.at(-1).payload.globals_lost, true);
  assert.equal(readyEvents.at(-1).runtime_epoch, 2);
});

test('a normal hard worker fatal still records runtime_reset and globals-lost ready', async t => {
  const f = await fixture(t);
  f.factory.options.autoComplete = false;
  const running = await f.submit('return "long"', { timeout_ms: 10000 });
  await f.wait(running.execution_id, record => record.status === 'running');
  const worker = f.factory.instances[0];
  worker.emit('message', { type: 'fatal', error: 'worker exploded' });
  const finished = await f.wait(running.execution_id);
  assert.equal(finished.status, 'interrupted');
  assert.equal(finished.reset_reason, 'worker_fatal');
  await f.waitUntil(() => f.factory.instances[0].stopped === true);
  f.factory.options.autoComplete = true;
  // The next submission is what provisions the replacement (hard-restarted)
  // worker; the session never silently spawns one on its own.
  const next = await f.submit('return 1');
  await f.waitUntil(() => f.factory.instances.length === 2);
  await f.waitUntil(() => f.factory.instances[1].options !== undefined);
  const done = await f.wait(next.execution_id, undefined, 8000);
  assert.equal(done.status, 'succeeded');
  assert.equal(done.runtime_epoch, 2);
  const recorded = await f.events();
  const hardReset = recorded.find(event => event.kind === 'runtime_reset');
  assert.ok(hardReset);
  assert.equal(hardReset.payload.globals_lost, true);
  const readyEvents = recorded.filter(event => event.kind === 'runtime_ready');
  assert.equal(readyEvents.length, 2);
  assert.equal(readyEvents.at(-1).payload.globals_lost, true);
  assert.equal(readyEvents.at(-1).runtime_epoch, 2);
});

test('soft_reset_failed reports the failure and hard-restarts the worker', async t => {
  const f = await fixture(t);
  const worker = f.factory.instances[0];
  worker.emit('message', {
    type: 'soft_reset', reason: 'bot_disconnected', execution_id: null,
    generation: 2, detail: null,
  });
  worker.emit('message', {
    type: 'soft_reset_failed', reason: 'bot_disconnected', error: 'login rejected',
  });
  const failed = await f.waitEvent('runtime_soft_reset_failed', 5000);
  assert.equal(failed.payload.reason, 'bot_disconnected');
  assert.match(failed.payload.error, /login rejected/);
  await f.waitUntil(() => f.factory.instances[0].stopped === true);
  assert.equal((await f.describe()).soft_resetting, false);

  const next = await f.submit('return 3');
  const done = await f.wait(next.execution_id, undefined, 8000);
  assert.equal(done.status, 'succeeded');
  assert.equal(f.factory.instances.length, 2);
  assert.equal(done.runtime_epoch, 2);
});

test('back-to-back soft_reset messages keep one gate and one timer', async t => {
  const f = await fixture(t);
  const worker = f.factory.instances[0];
  worker.emit('message', { type: 'soft_reset', reason: 'nonfinite_bot_state', execution_id: null, generation: 2, detail: null });
  // A duplicate notification must not crash, overwrite the gate or reverse it.
  worker.emit('message', { type: 'soft_reset', reason: 'physics_stalled', execution_id: null, generation: 3, detail: null });
  const queued = await f.submit('return 1');
  await delay(100);
  assert.equal(worker.sent.filter(message => message.type === 'execute').length, 0);
  assert.equal((await f.describe()).soft_resetting, true);
  const softEvents = (await f.events()).filter(event => event.kind === 'runtime_soft_reset');
  assert.equal(softEvents.length, 1);
  assert.equal(softEvents[0].payload.reason, 'nonfinite_bot_state');
  worker.emit('message', {
    type: 'soft_ready', reason: 'nonfinite_bot_state', generation: 2,
    snapshot: { mode: 'fake', username: 'alice' }, duration_ms: 10, soft_resets: 1,
  });
  assert.equal((await f.wait(queued.execution_id)).status, 'succeeded');
  await delay(200);
  const kinds = (await f.events()).map(event => event.kind);
  assert.equal(kinds.filter(kind => kind === 'runtime_soft_reset_ready').length, 1);
  assert.ok(!kinds.includes('runtime_soft_reset_timeout'));
  assert.equal((await f.describe()).soft_resets, 1);
});

test('older soft_ready generations are ignored without resolving a gate twice', async t => {
  const f = await fixture(t);
  const worker = f.factory.instances[0];
  worker.emit('message', { type: 'soft_reset', reason: 'bot_disconnected', execution_id: null, generation: 2, detail: null });
  worker.emit('message', {
    type: 'soft_ready', reason: 'bot_disconnected', generation: 2,
    snapshot: {}, duration_ms: 12, soft_resets: 1,
  });
  const readyCount = () => f.events().then(events =>
    events.filter(event => event.kind === 'runtime_soft_reset_ready').length);
  assert.equal(await readyCount(), 1);
  // Stale/unknown ready after the gate already settled: ignored, no count bump.
  worker.emit('message', {
    type: 'soft_ready', reason: 'bot_disconnected', generation: 1,
    snapshot: {}, duration_ms: 99, soft_resets: 7,
  });
  await delay(100);
  assert.equal(await readyCount(), 1);
  const described = await f.describe();
  assert.equal(described.soft_resets, 1);
  assert.equal(described.worker_generation, 2);
  // A stale duplicate of the already-resolved soft_reset must not reopen the
  // gate (which would otherwise time out and hard-restart a healthy worker).
  worker.emit('message', { type: 'soft_reset', reason: 'bot_disconnected', execution_id: null, generation: 1, detail: null });
  await delay(100);
  assert.equal((await f.describe()).soft_resetting, false);
  assert.equal((await f.events()).filter(event => event.kind === 'runtime_soft_reset').length, 1);
  // A hard exit with a stale ready message must resolve via kill/exit, not hang.
  worker.exitCode = 1;
  worker.emit('exit', 1, null);
  await f.waitUntil(async () => (await f.describe()).connected === false);
  assert.equal((await f.describe()).soft_resetting, false);
  const next = await f.submit('return 4');
  const done = await f.wait(next.execution_id, undefined, 8000);
  assert.equal(done.status, 'succeeded');
});

test('soft_reset arriving while the worker startup is in flight opens a drained gate', async t => {
  const f = await fixture(t, {
    factorySetup: factory => {
      factory.options.beforeReady.push({
        type: 'soft_reset', reason: 'nonfinite_bot_state', execution_id: null,
        generation: 1, detail: { during: 'startup' },
      });
    },
  });
  // Session creation awaited the ready message; the gate is already open.
  const createdState = await f.describe();
  assert.equal(createdState.soft_resetting, true);
  assert.equal(createdState.connected, true);
  const queued = await f.submit('return 2');
  await delay(150);
  assert.equal(f.factory.instances[0].sent.filter(message => message.type === 'execute').length, 0);
  f.factory.instances[0].emit('message', {
    type: 'soft_ready', reason: 'nonfinite_bot_state', generation: 1,
    snapshot: { mode: 'fake', username: 'alice' }, duration_ms: 5, soft_resets: 1,
  });
  const done = await f.wait(queued.execution_id);
  assert.equal(done.status, 'succeeded');
  assert.equal((await f.describe()).soft_resets, 1);
});

test('session close during a soft reset settles the gate without hanging or leaking its timer', async t => {
  const f = await fixture(t);
  const worker = f.factory.instances[0];
  worker.emit('message', { type: 'soft_reset', reason: 'bot_disconnected', execution_id: null, generation: 2, detail: null });
  assert.equal((await f.describe()).soft_resetting, true);
  const startedAt = Date.now();
  // retain=true keeps the closed session queryable so its ledger can be read.
  const deleted = await f.request('DELETE', '/v1/sessions/alice?retain=true');
  const elapsed = Date.now() - startedAt;
  assert.equal(deleted.status, 200, JSON.stringify(deleted));
  assert.equal(deleted.body.closed, true);
  assert.ok(elapsed < 2000, `session close took ${elapsed} ms`);
  await f.waitUntil(() => f.factory.instances[0].stopped === true);
  // The 5 s soft-reset timer must not fire after close (would be a leaked timer).
  await delay(250);
  const timedOut = (await f.events()).some(event => event.kind === 'runtime_soft_reset_timeout');
  assert.equal(timedOut, false);
});

test('soft reset and physics stall configuration is validated and reaches the worker', async t => {
  for (const [key, value] of [
    ['softResetTimeoutMs', 4999], ['softResetTimeoutMs', 120001],
    ['softResetTimeoutMs', 5000.5], ['physicsStallMs', 2999],
    ['physicsStallMs', 60001], ['physicsStallMs', '15000'],
  ]) {
    await assert.rejects(
      () => start({ port: 0, mode: 'fake', token: admin, workerEndpoints: {}, [key]: value }),
      new RegExp(key),
      `${key}=${value} must be rejected`,
    );
  }
  const defaults = await fixture(t);
  assert.equal(defaults.factory.instances[0].options.workerConfig.physicsStallMs, 15000);
  assert.equal((await defaults.describe()).soft_resetting, false);
  assert.equal((await defaults.describe()).soft_resets, 0);
  assert.equal((await defaults.describe()).worker_generation, 1);

  const custom = await fixture(t, { physicsStallMs: 30000 });
  assert.equal(custom.factory.instances[0].options.workerConfig.physicsStallMs, 30000);
});

test('a soft-reset completion opens the dispatch gate before drain can dispatch the next program', async t => {
  // Regression for the completed -> drain microtask race: the worker sends
  // `completed` (reset_reason soft_reset) and then `soft_reset`. The server must
  // open the gate from the completion message; otherwise drain dispatches the
  // next queued program in the gap, the worker queues and later executes it, and
  // the session records that program as interrupted even though it ran.
  const f = await fixture(t, { softResetTimeoutMs: 5000 });
  f.factory.options.autoComplete = false;
  const running = await f.submit('return "long"', { timeout_ms: 10000 });
  await f.wait(running.execution_id, record => record.status === 'running');
  const queued = await f.submit('return "queued"');
  const worker = f.factory.instances[0];
  const executeCount = () => worker.sent.filter(message => message.type === 'execute').length;
  assert.equal(executeCount(), 1);

  worker.emit('message', {
    type: 'completed', execution_id: running.execution_id, result: null,
    error: { name: 'SoftResetError', message: 'Bot client reset: uncaught:Error', stack: '' },
    timed_out: false, needs_reset: false, reset_reason: 'soft_reset', cleanup: null,
  });
  const finished = await f.wait(running.execution_id);
  assert.equal(finished.status, 'failed');
  await delay(60);
  assert.equal(executeCount(), 1, 'the queued program must not be dispatched in the completion->soft_reset gap');
  assert.equal((await f.describe()).soft_resetting, true);

  worker.emit('message', {
    type: 'soft_reset', reason: 'uncaught:Error', execution_id: running.execution_id,
    generation: 2, detail: { source: 'UNCAUGHT_EXCEPTION' },
  });
  const recorded = await f.events();
  const softEvents = recorded.filter(event => event.kind === 'runtime_soft_reset');
  assert.equal(softEvents.length, 1, 'runtime_soft_reset must be recorded exactly once');
  assert.equal(softEvents[0].payload.reason, 'uncaught:Error');

  f.factory.options.autoComplete = true;
  worker.emit('message', {
    type: 'soft_ready', reason: 'uncaught:Error', generation: 2,
    snapshot: { mode: 'fake', username: 'alice', globals_preserved: true },
    duration_ms: 50, soft_resets: 1,
  });
  const done = await f.wait(queued.execution_id);
  assert.equal(done.status, 'succeeded');
  assert.equal(executeCount(), 2);
  assert.equal(done.runtime_epoch, 1);
  assert.equal(f.factory.instances.length, 1);
});
