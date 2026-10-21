import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
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
