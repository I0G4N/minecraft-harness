import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fork } from 'node:child_process';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { start } from '../src/server.js';

const require = createRequire(import.meta.url);
const protocol = require('minecraft-protocol');
const minecraftData = require('minecraft-data');
const nbt = require('prismarine-nbt');
const { Vec3 } = require('vec3');

// This is a minimal protocol fixture, NOT a Vanilla Minecraft server/world.
// It tests real Mineflayer protocol decoding and runtime lifecycle integration,
// and supplies no evidence about Minecraft task success, AI ability or physics.
async function protocolFixture() {
  const version = '1.20.4';
  const data = minecraftData(version);
  const Chunk = require('prismarine-chunk')(version);
  const chunk = new Chunk({ minY: -64, worldHeight: 384 });
  for (let x = 0; x < 16; x++) for (let z = 0; z < 16; z++) {
    chunk.setBlockStateId(new Vec3(x, 63, z), data.blocksByName.grass_block.minStateId);
  }
  const server = protocol.createServer({ host: '127.0.0.1', port: 0, version,
    'online-mode': false, encryption: false, enforceSecureProfile: false,
    registryCodec: data.loginPacket.dimensionCodec, hideErrors: true });
  const joined = [];
  const ended = [];
  const chats = [];
  const errors = [];
  server.on('error', error => errors.push(String(error.stack ?? error)));
  server.on('playerJoin', client => {
    joined.push({ username: client.username, id: client.id, at: Date.now() });
    client.on('error', error => errors.push(String(error.stack ?? error)));
    client.on('end', reason => ended.push({ username: client.username, id: client.id, reason, at: Date.now() }));
    client.on('chat_message', packet => {
      chats.push({ username: client.username, message: packet.message });
      const content = nbt.comp({ text: nbt.string(`<${client.username}> ${packet.message}`) });
      server.writeToClients(Object.values(server.clients).filter(peer => peer.state === protocol.states.PLAY),
        'system_chat', { content, isActionBar: false });
    });
    client.write('login', { ...data.loginPacket, entityId: client.id, gameMode: 0, hashedSeed: [0, 42] });
    client.write('difficulty', { difficulty: 1, difficultyLocked: true });
    client.write('map_chunk', {
      x: 0, z: 0, heightmaps: nbt.comp({}), chunkData: chunk.dump(), blockEntities: [],
      skyLightMask: [], blockLightMask: [], emptySkyLightMask: [], emptyBlockLightMask: [],
      skyLight: [], blockLight: [],
    });
    client.write('position', { x: 8.5, y: 64, z: 8.5, yaw: 0, pitch: 0, flags: 0, teleportId: 1 });
    const items = Array.from({ length: 46 }, () => ({ present: false }));
    items[36] = { present: true, itemId: data.itemsByName.oak_log.id, itemCount: 3 };
    client.write('window_items', { windowId: 0, stateId: 1, items, carriedItem: { present: false } });
    client.write('update_health', { health: 20, food: 20, foodSaturation: 5 });
  });
  await once(server, 'listening');
  return { kind: 'protocol_fixture_not_vanilla', server, port: server.socketServer.address().port,
    joined, ended, chats, errors,
    async close() {
      const closed = once(server, 'close');
      server.close();
      await closed;
    } };
}

test('protocol fixture: real Mineflayer 1.20.4 spawns, observes, chats across bots and reconnects after watchdog kill', { timeout: 20000 }, async t => {
  const fixture = await protocolFixture();
  assert.equal(fixture.kind, 'protocol_fixture_not_vanilla');
  const token = 'protocol-fixture-supervisor-token';
  const runtime = await start({ mode: 'minecraft', port: 0, token,
    mcHost: '127.0.0.1', mcPort: fixture.port, mcVersion: '1.20.4', startupTimeoutMs: 6000 });
  t.after(async () => { await runtime.close(); await fixture.close(); });
  const url = `http://127.0.0.1:${runtime.address.port}`;
  async function request(method, path, body) {
    const response = await fetch(`${url}${path}`, { method,
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const result = await response.json();
    assert.ok(response.ok, `HTTP ${response.status}: ${JSON.stringify(result)}`);
    return result;
  }
  async function submit(agent, source, timeoutMs = 3000) {
    return request('POST', `/v1/sessions/${agent}/programs`, { request_id: crypto.randomUUID(), source, timeout_ms: timeoutMs, world_epoch: 1, run_id: 'development' });
  }
  async function wait(agent, executionId) {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      const record = await request('GET', `/v1/sessions/${agent}/programs/${executionId}`);
      if (!['accepted', 'running'].includes(record.status)) return record;
      await delay(15);
    }
    throw new Error('Protocol-fixture execution did not reach terminal status');
  }
  for (const agent of ['protoAlice', 'protoBob']) {
    const session = await request('POST', '/v1/sessions', { agent_id: agent });
    assert.equal(session.connected, true);
    assert.equal(session.mode, 'minecraft');
  }
  assert.deepEqual(fixture.joined.map(item => item.username), ['protoAlice', 'protoBob']);
  const observation = await wait('protoAlice', (await submit('protoAlice', `
    globals.observed = true;
    return {username: bot.username, position: bot.entity.position,
      health: bot.health, dimension: bot.game.dimension,
      blockBelow: bot.blockAt(bot.entity.position.offset(0, -1, 0))?.name,
      inventory: bot.inventory.items().map(item => ({name:item.name,count:item.count})),
      pathfinderLoaded: typeof bot.pathfinder.goto === 'function'};
  `)).execution_id);
  assert.equal(observation.status, 'succeeded', JSON.stringify(observation.error));
  assert.equal(observation.result.username, 'protoAlice');
  assert.equal(observation.result.health, 20);
  assert.equal(observation.result.dimension, 'overworld');
  assert.equal(observation.result.blockBelow, 'grass_block');
  assert.deepEqual(observation.result.inventory, [{ name: 'oak_log', count: 3 }]);
  assert.equal(observation.result.pathfinderLoaded, true);
  assert.equal(observation.result.position.x, 8.5);
  assert.equal(observation.result.position.y, 64);
  const said = await wait('protoAlice', (await submit('protoAlice', 'bot.chat("protocol-fixture-team-chat"); return true')).execution_id);
  assert.equal(said.status, 'succeeded');
  let bobEvents;
  for (let attempt = 0; attempt < 40; attempt++) {
    bobEvents = await request('GET', '/v1/sessions/protoBob/events?limit=1000');
    if (bobEvents.events.some(event => event.kind === 'bot_chat')) break;
    await delay(15);
  }
  assert.deepEqual(fixture.chats, [{ username: 'protoAlice', message: 'protocol-fixture-team-chat' }]);
  assert.ok(bobEvents.events.some(event => event.kind === 'bot_chat' && event.payload[0] === 'protoAlice' && event.payload[1] === 'protocol-fixture-team-chat'));
  const loop = await submit('protoAlice', 'while (true) {}', 100);
  assert.equal((await request('GET', '/health')).status, 'ok');
  const timedOut = await wait('protoAlice', loop.execution_id);
  assert.equal(timedOut.status, 'timed_out');
  const reconnected = await wait('protoAlice', (await submit('protoAlice', 'return {lost: globals.observed === undefined, health: bot.health}')).execution_id);
  assert.equal(reconnected.status, 'succeeded', JSON.stringify(reconnected.error));
  assert.deepEqual(reconnected.result, { lost: true, health: 20 });
  assert.equal(reconnected.runtime_epoch, 2);
  assert.equal(fixture.joined.filter(item => item.username === 'protoAlice').length, 2);
  assert.ok(fixture.ended.some(item => item.username === 'protoAlice'));
  const events = (await request('GET', '/v1/sessions/protoAlice/events?limit=1000')).events;
  const reset = events.find(event => event.kind === 'runtime_reset' && event.payload.reason === 'timeout');
  const readyAgain = events.find(event => event.kind === 'runtime_ready' && event.runtime_epoch === 2);
  assert.equal(reset.payload.confirmed_old_process_exit, true);
  assert.ok(reset.cursor < readyAgain.cursor);
  assert.deepEqual(fixture.errors, []);
});

// ---------------------------------------------------------------------------
// Soft reset: the worker recreates the Mineflayer client in the SAME Node
// process after an uncaught async program error. The VM context/globals, the
// session and its runtime epoch must survive; only the bot connection changes.
// ---------------------------------------------------------------------------
test('protocol fixture: in-process soft reset preserves globals, session and runtime epoch', { timeout: 30000 }, async t => {
  const fixture = await protocolFixture();
  const token = 'protocol-fixture-soft-reset-token';
  const runtime = await start({ mode: 'minecraft', port: 0, token,
    mcHost: '127.0.0.1', mcPort: fixture.port, mcVersion: '1.20.4', startupTimeoutMs: 6000 });
  t.after(async () => { await runtime.close(); await fixture.close(); });
  const url = `http://127.0.0.1:${runtime.address.port}`;
  async function request(method, path, body) {
    const response = await fetch(`${url}${path}`, { method,
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const result = await response.json();
    assert.ok(response.ok, `HTTP ${response.status}: ${JSON.stringify(result)}`);
    return result;
  }
  async function submit(agent, source, timeoutMs = 3000) {
    return request('POST', `/v1/sessions/${agent}/programs`, {
      request_id: crypto.randomUUID(), source, timeout_ms: timeoutMs,
      world_epoch: 1, run_id: 'development' });
  }
  async function wait(agent, executionId, timeoutMs = 10000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const record = await request('GET', `/v1/sessions/${agent}/programs/${executionId}`);
      if (!['accepted', 'running'].includes(record.status)) return record;
      await delay(15);
    }
    throw new Error('Soft-reset execution did not reach terminal status');
  }
  async function events(agent) {
    return (await request('GET', `/v1/sessions/${agent}/events?limit=1000`)).events;
  }
  async function waitForEvents(agent, kinds, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    let current = [];
    while (Date.now() < deadline) {
      current = await events(agent);
      if (kinds.every(kind => current.some(event => event.kind === kind))) return current;
      await delay(25);
    }
    throw new Error(`missing session events ${JSON.stringify(kinds)}; saw ${JSON.stringify(current.map(event => event.kind))}`);
  }

  const session = await request('POST', '/v1/sessions', { agent_id: 'protoReset' });
  assert.equal(session.connected, true);
  assert.equal(session.mode, 'minecraft');

  // The program writes the globals marker, then throws asynchronously while it
  // is still the foreground execution: the worker fails it with SoftResetError
  // and recreates the client in-process instead of exiting.
  const first = await submit('protoReset', `
    globals.marker = 'soft-reset-preserved';
    globals.counter = (globals.counter ?? 0) + 1;
    await new Promise(resolve => setTimeout(() => {
      throw new Error('resync');
    }, 50));
  `);
  const firstRecord = await wait('protoReset', first.execution_id);
  assert.equal(firstRecord.status, 'failed', JSON.stringify(firstRecord));
  assert.equal(firstRecord.error?.name, 'SoftResetError');
  assert.match(firstRecord.error?.message ?? '', /Bot client reset: uncaught:Error/);
  assert.equal(firstRecord.needs_reset ?? false, false);

  const sessionEvents = await waitForEvents('protoReset', ['runtime_soft_reset', 'runtime_soft_reset_ready']);
  const softReset = sessionEvents.find(event => event.kind === 'runtime_soft_reset');
  const softReady = sessionEvents.find(event => event.kind === 'runtime_soft_reset_ready');
  assert.equal(softReset.payload.reason, 'uncaught:Error');
  assert.equal(softReset.payload.globals_lost, false);
  assert.equal(softReset.payload.execution_id, first.execution_id);
  assert.ok(Number.isSafeInteger(softReset.payload.generation));
  assert.ok(softReset.payload.generation >= 2);
  assert.ok(softReset.cursor < softReady.cursor, 'soft_reset must precede soft_ready');
  assert.equal(softReady.payload.globals_lost, false);
  assert.equal(softReady.payload.generation, softReset.payload.generation);
  assert.ok(Number.isSafeInteger(softReady.payload.duration_ms));
  assert.ok(softReady.payload.soft_resets >= 1);
  assert.equal(softReady.payload.snapshot.globals_preserved, true);
  assert.equal(softReady.payload.snapshot.username, 'protoReset');

  // No hard-restart / globals-lost evidence may be emitted for a soft reset.
  assert.equal(sessionEvents.some(event => event.kind === 'runtime_reset'), false);
  assert.equal(sessionEvents.filter(event => event.kind === 'runtime_ready').length, 1);
  assert.equal(sessionEvents.find(event => event.kind === 'runtime_ready').payload.globals_lost, false);
  const failedEvent = sessionEvents.find(event => event.kind === 'execution_failed');
  assert.ok(failedEvent.cursor < softReset.cursor, 'completed/failure must precede soft_reset');

  // The next program runs in the SAME session/VM context and can still see the
  // globals written before the reset. Submitting it right after the failure also
  // exercises the server's soft-reset dispatch gate.
  const second = await submit('protoReset', `
    return {
      marker: globals.marker,
      counter: globals.counter,
      username: bot.username,
      health: bot.health,
      dimension: bot.game.dimension,
      position: { x: bot.entity.position.x, y: bot.entity.position.y, z: bot.entity.position.z },
    };
  `);
  const secondRecord = await wait('protoReset', second.execution_id);
  assert.equal(secondRecord.status, 'succeeded', JSON.stringify(secondRecord.error));
  assert.equal(secondRecord.result.marker, 'soft-reset-preserved');
  assert.equal(secondRecord.result.counter, 1);
  assert.equal(secondRecord.result.username, 'protoReset');
  assert.equal(secondRecord.result.health, 20);
  assert.equal(secondRecord.result.dimension, 'overworld');
  assert.ok(Number.isFinite(secondRecord.result.position.x));
  assert.equal(secondRecord.runtime_epoch, 1, 'soft reset must not bump runtime_epoch');
  assert.equal(firstRecord.runtime_epoch, 1);

  // The Mineflayer connection itself was replaced (new join) while the session
  // stayed connected with one soft reset and generation 2.
  const describe = await request('GET', '/v1/sessions/protoReset');
  assert.equal(describe.connected, true);
  assert.equal(describe.soft_resetting, false);
  assert.equal(describe.runtime_epoch, 1);
  assert.ok(describe.soft_resets >= 1);
  assert.ok(describe.worker_generation >= 2);
  assert.equal(fixture.joined.filter(item => item.username === 'protoReset').length, 2);
  assert.ok(fixture.ended.some(item => item.username === 'protoReset'));
  assert.deepEqual(fixture.errors, []);
});


// ---------------------------------------------------------------------------
// Worker-only regression: an execution that arrives while the soft-reset gate
// is closed must be queued and drained once soft_ready is sent. This is the
// exact race where session.drain() sees soft_reset and the worker still holds
// `resetState`; without the post-reset drain the execution stalls forever.
// Fake mode is used so the test is deterministic and does not need Mineflayer.
// ---------------------------------------------------------------------------
test('worker soft reset drains an execution queued during the reset gate', { timeout: 15000 }, async t => {
  const workerPath = fileURLToPath(new URL('../src/worker.js', import.meta.url));
  const workerCwd = fileURLToPath(new URL('..', import.meta.url));
  const config = {
    mode: 'fake', username: 'fake-worker', lifecycle: 'strict',
    maxBackgroundJobs: 8, remainingRunMs: 600000,
    runDeadlineAt: Date.now() + 600000, maxLogs: 128, maxLogBytes: 262144,
    maxResultBytes: 262144, leftoverPolicy: 'reset', chatDelivery: 'none',
    maxLeftoverScopes: 256, paperSingleProgram: false, physicsStallMs: 15000,
    host: '127.0.0.1', port: 0, version: '1.20.4',
  };
  const child = fork(workerPath, [], {
    cwd: workerCwd,
    env: { ...process.env, BOT_WORKER_CONFIG: JSON.stringify(config) },
    stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
  });
  t.after(() => { try { child.kill('SIGKILL'); } catch {} });
  const messages = [];
  const stderr = [];
  child.stderr.on('data', chunk => stderr.push(String(chunk)));
  child.on('message', message => messages.push(message));
  const waitFor = (predicate, timeoutMs = 8000) => new Promise((resolve, reject) => {
    const check = () => {
      const found = messages.find(predicate);
      if (found) { clearTimeout(timer); resolve(found); }
    };
    const timer = setTimeout(() => reject(new Error(
      `timeout; messages=${JSON.stringify(messages.map(message => message.type))}; `
      + `stderr=${stderr.join('').slice(-1200)}`)), timeoutMs);
    check();
    const onMessage = () => check();
    child.on('message', onMessage);
    t.after(() => child.removeListener('message', onMessage));
  });

  const ready = await waitFor(message => message.type === 'ready');
  assert.equal(ready.mode, 'fake');

  const firstId = crypto.randomUUID();
  const secondId = crypto.randomUUID();
  // First execution: writes a globals marker, then throws asynchronously while
  // still the foreground execution -> SoftResetError + in-process soft reset.
  child.send({
    type: 'execute', execution_id: firstId, timeout_ms: 5000,
    source: "globals.marker = 'queued-across-reset'; "
      + "setTimeout(() => { throw new Error('resync'); }, 15); "
      + "await new Promise(() => {});",
  });
  // Second execution is sent immediately, i.e. while the first is still running
  // and before the worker has announced the soft reset. It must be queued and
  // then run after soft_ready, in the same VM context.
  child.send({
    type: 'execute', execution_id: secondId, timeout_ms: 5000,
    source: 'return { marker: globals.marker, health: bot.health };',
  });

  const failed = await waitFor(message => message.type === 'completed'
    && message.execution_id === firstId);
  assert.equal(failed.error?.name, 'SoftResetError');
  assert.equal(failed.needs_reset, false);
  const softReset = await waitFor(message => message.type === 'soft_reset');
  assert.equal(softReset.reason, 'uncaught:Error');
  assert.equal(softReset.detail.source, 'UNCAUGHT_EXCEPTION');
  assert.equal(softReset.execution_id, firstId);
  assert.ok(softReset.generation >= 2);
  const softReady = await waitFor(message => message.type === 'soft_ready');
  assert.equal(softReady.snapshot.globals_preserved, true);
  assert.equal(softReady.reason, 'uncaught:Error');
  assert.ok(softReady.soft_resets >= 1);
  assert.ok(messages.indexOf(failed) < messages.indexOf(softReset),
    'completion must be sent before soft_reset');
  assert.ok(messages.indexOf(softReset) < messages.indexOf(softReady),
    'soft_reset must be sent before soft_ready');

  const queued = await waitFor(message => message.type === 'completed'
    && message.execution_id === secondId);
  assert.equal(queued.error ?? null, null, JSON.stringify(queued.error));
  assert.deepEqual(queued.result, { marker: 'queued-across-reset', health: 20 });
  assert.equal(queued.runtime_epoch, undefined);

  // The worker is still healthy and keeps the same VM globals afterwards.
  const thirdId = crypto.randomUUID();
  child.send({
    type: 'execute', execution_id: thirdId, timeout_ms: 5000,
    source: 'globals.counter = (globals.counter ?? 0) + 1; '
      + 'return { marker: globals.marker, counter: globals.counter };',
  });
  const third = await waitFor(message => message.type === 'completed'
    && message.execution_id === thirdId);
  assert.equal(third.error ?? null, null, JSON.stringify(third.error));
  assert.deepEqual(third.result, { marker: 'queued-across-reset', counter: 1 });
  assert.equal(child.exitCode, null, 'worker must not exit on a soft reset');
  assert.equal(stderr.join('').includes('SOFT_RESET_FAILED'), false);
});
