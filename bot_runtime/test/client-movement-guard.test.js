import test from 'node:test';
import assert from 'node:assert/strict';

import {
  isTrackedEntity,
  assertAttackable,
  installAttackGuard,
  installDeathMovementSuppression,
} from '../src/client-movement-guard.js';

function makeBot() {
  const writes = [];
  const bot = {
    isAlive: true,
    entity: { id: 7, position: { x: 0, y: 64, z: 0 } },
    entities: {
      7: undefined, // filled below
      9: { id: 9, name: 'zombie', position: { x: 2, y: 64, z: 0 } },
      10: { id: 10, name: 'far', position: { x: 100, y: 64, z: 0 } },
    },
    _client: {
      write(name, params) { writes.push({ name, params }); return true; },
    },
    attack(target) { writes.push({ name: 'attack', target }); return true; },
  };
  bot.entities[7] = bot.entity;
  return { bot, writes };
}

test('tracked entity validation rejects stale/self/out-of-range targets', () => {
  const { bot } = makeBot();
  assert.equal(isTrackedEntity(bot, bot.entities[9]), true);
  assert.equal(isTrackedEntity(bot, { id: 9, name: 'stale' }), false);
  assert.throws(() => assertAttackable(bot, null), TypeError);
  assert.throws(() => assertAttackable(bot, { id: '9' }), TypeError);
  assert.throws(() => assertAttackable(bot, { id: 9, name: 'stale' }), /not a live tracked/);
  bot.isAlive = false;
  assert.throws(() => assertAttackable(bot, bot.entities[9]), /cannot attack while dead/);
  bot.isAlive = true;
  assert.throws(() => assertAttackable(bot, bot.entity), /cannot attack self/);
  assert.throws(() => assertAttackable(bot, { id: 7, name: 'self-copy', position: bot.entity.position }), /not a live tracked/);
  assert.throws(() => assertAttackable(bot, bot.entities[10]), /out of reach/);
  assert.equal(assertAttackable(bot, bot.entities[9]), bot.entities[9]);
});

test('installAttackGuard blocks invalid targets before use_entity and uninstalls cleanly', () => {
  const { bot } = makeBot();
  const original = bot.attack;
  const dispose = installAttackGuard(bot);
  assert.throws(() => bot.attack({ id: 9, name: 'stale' }), /not a live tracked/);
  bot.attack(bot.entities[9]);
  assert.equal(bot.attack === original, false);
  dispose();
  assert.equal(bot.attack, original, 'uninstall restores the original attack method');
});

test('dead bots do not send position/position_look/flying packets, but keep teleport_confirm', () => {
  const { bot, writes } = makeBot();
  let suppressed = 0;
  const guard = installDeathMovementSuppression(bot, { onSuppressed: () => { suppressed += 1; } });
  bot.isAlive = false;
  bot._client.write('position', { x: 1 });
  bot._client.write('position_look', { x: 1 });
  bot._client.write('flying', { onGround: false });
  bot._client.write('teleport_confirm', { teleportId: 3 });
  assert.deepEqual(writes.map(item => item.name), ['teleport_confirm']);
  assert.equal(guard.stats().suppressed, 3);
  assert.deepEqual(guard.stats().byPacket, { position: 1, position_look: 1, flying: 1 });
  assert.equal(suppressed, 3);

  bot.isAlive = true;
  bot._client.write('position', { x: 2 });
  assert.deepEqual(writes.map(item => item.name), ['teleport_confirm', 'position']);
  guard.uninstall();
  bot.isAlive = false;
  bot._client.write('position', { x: 3 });
  assert.deepEqual(writes.map(item => item.name), ['teleport_confirm', 'position', 'position']);
});

test('death movement suppression is a no-op without a protocol client', () => {
  const bot = { isAlive: false };
  const guard = installDeathMovementSuppression(bot);
  assert.equal(guard.stats().installed, false);
  guard.uninstall();
});

test('movement suppression only drops position packets while dead', () => {
  const { bot, writes } = makeBot();
  const guard = installDeathMovementSuppression(bot);
  bot.isAlive = true;
  bot._client.write('position', { x: 1 });
  bot._client.write('look', { yaw: 0 });
  bot.isAlive = false;
  bot._client.write('look', { yaw: 1 });
  assert.deepEqual(writes.map(item => item.name), ['position', 'look', 'look']);
  guard.uninstall();
});
