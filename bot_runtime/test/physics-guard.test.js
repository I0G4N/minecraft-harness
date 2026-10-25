import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';

import {
  clientState,
  stateIsFinite,
  nonfiniteFields,
  installPhysicsGuard,
  rateLimitDecision,
  DEFAULT_STALL_MS,
  CONSECUTIVE_BAD_LIMIT,
} from '../src/physics-guard.js';

class FakeVec3 {
  constructor(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z; }
  set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; }
  clone() { return new FakeVec3(this.x, this.y, this.z); }
}

function makeBot({ physics = true, position = [1, 64, 1] } = {}) {
  const bot = new EventEmitter();
  bot.username = 'fake-player';
  bot.version = '1.20.4';
  bot.isAlive = true;
  bot.physicsEnabled = true;
  bot.entity = {
    position: new FakeVec3(...position),
    velocity: new FakeVec3(0, 0, 0),
    yaw: 0.5,
    pitch: 0.25,
    onGround: true,
  };
  if (physics) {
    bot.__nextState = () => ({
      pos: new FakeVec3(bot.entity.position.x, bot.entity.position.y, bot.entity.position.z),
      vel: new FakeVec3(0, 0, 0),
      yaw: bot.entity.yaw,
      pitch: bot.entity.pitch,
      onGround: bot.entity.onGround,
      apply(target) {
        target.entity.position = this.pos;
        target.entity.velocity = this.vel;
        target.entity.onGround = this.onGround;
      },
    });
    bot.physics = {
      simulatePlayer(_input, _world) {
        return bot.__nextState();
      },
    };
    bot.__originalSimulate = bot.physics.simulatePlayer;
  }
  return bot;
}

function badState(bot) {
  return {
    pos: new FakeVec3(Number.NaN, 64, 1),
    vel: new FakeVec3(Number.NaN, Number.NaN, Number.NaN),
    yaw: bot.entity.yaw,
    pitch: bot.entity.pitch,
    onGround: true,
    apply(target) {
      target.entity.position = this.pos;
      target.entity.velocity = this.vel;
    },
  };
}

function finiteTriple(vector) {
  return Number.isFinite(vector.x) && Number.isFinite(vector.y) && Number.isFinite(vector.z);
}

test('clientState/stateIsFinite/nonfiniteFields expose the physics boundary', () => {
  const bot = makeBot();
  const state = clientState(bot.entity);
  assert.deepEqual(state.pos, { x: 1, y: 64, z: 1 });
  assert.ok(stateIsFinite(state));
  assert.deepEqual(nonfiniteFields(state), []);

  const bad = clientState({ position: new FakeVec3(Number.NaN, 1, 2), velocity: new FakeVec3(0, Number.POSITIVE_INFINITY, 0), yaw: Number.NaN, pitch: 0 });
  assert.equal(stateIsFinite(bad), false);
  assert.deepEqual(nonfiniteFields(bad).sort(), ['pos.x', 'vel.y', 'yaw'].sort());
});

test('nonfinite simulatePlayer output is replaced with the last good state before apply', t => {
  const bot = makeBot();
  const prevented = [];
  const repeated = [];
  const guard = installPhysicsGuard(bot, {
    onPrevented: payload => prevented.push(payload),
    onRepeated: stats => repeated.push(stats),
  });
  t.after(() => guard.uninstall());
  assert.equal(guard.installed, true);

  bot.__nextState = () => badState(bot);
  const output = bot.physics.simulatePlayer({}, {});
  assert.ok(finiteTriple(output.pos), 'position must be sanitized before apply');
  assert.ok(finiteTriple(output.vel), 'velocity must be sanitized before apply');
  assert.deepEqual([output.pos.x, output.pos.y, output.pos.z], [1, 64, 1]);
  assert.deepEqual([output.vel.x, output.vel.y, output.vel.z], [0, 0, 0]);

  output.apply(bot);
  assert.ok(finiteTriple(bot.entity.position));
  assert.ok(finiteTriple(bot.entity.velocity));
  assert.equal(bot.entity.position.x, 1);

  assert.equal(prevented.length, 1);
  assert.ok(prevented[0].badFields.includes('pos.x'));
  assert.ok(prevented[0].badFields.includes('vel.y'));
  assert.equal(prevented[0].restored, true);
});

test('nonfinite entity yaw/pitch is restored even when the output position is finite', t => {
  const bot = makeBot();
  const prevented = [];
  const guard = installPhysicsGuard(bot, { onPrevented: payload => prevented.push(payload) });
  t.after(() => guard.uninstall());

  bot.entity.yaw = Number.NaN;
  bot.entity.pitch = Number.POSITIVE_INFINITY;
  const output = bot.physics.simulatePlayer({}, {});
  assert.ok(Number.isFinite(output.yaw));
  assert.ok(Number.isFinite(output.pitch));
  assert.ok(Number.isFinite(bot.entity.yaw));
  assert.ok(Number.isFinite(bot.entity.pitch));
  assert.equal(bot.entity.yaw, 0.5);
  assert.equal(prevented.length, 1);
  assert.ok(prevented[0].badFields.includes('yaw'));
  assert.ok(prevented[0].badFields.includes('pitch'));
});

test('consecutive bad ticks fire onRepeated once and a good physicsTick resets the counter', t => {
  const bot = makeBot();
  const repeated = [];
  const guard = installPhysicsGuard(bot, { onRepeated: stats => repeated.push(stats) });
  t.after(() => guard.uninstall());

  bot.__nextState = () => badState(bot);
  bot.physics.simulatePlayer({}, {});
  bot.physics.simulatePlayer({}, {});
  assert.equal(repeated.length, 0);
  bot.physics.simulatePlayer({}, {});
  assert.equal(repeated.length, 1, `onRepeated after ${CONSECUTIVE_BAD_LIMIT} consecutive bad ticks`);
  bot.physics.simulatePlayer({}, {});
  assert.equal(repeated.length, 1, 'onRepeated is once per bad episode');

  // A good tick (finite entity state) resets the episode.
  bot.entity.position = new FakeVec3(2, 64, 2);
  bot.entity.velocity = new FakeVec3(0, 0, 0);
  bot.entity.yaw = 0.5;
  bot.entity.pitch = 0.25;
  bot.emit('physicsTick');
  bot.__nextState = () => badState(bot);
  bot.physics.simulatePlayer({}, {});
  bot.physics.simulatePlayer({}, {});
  assert.equal(repeated.length, 1, 'good tick reset the consecutive counter');
  bot.physics.simulatePlayer({}, {});
  assert.equal(repeated.length, 2, 'a second bad episode escalates again');
  assert.ok(repeated[1].consecutive >= CONSECUTIVE_BAD_LIMIT);
});

test('physicsTick with no last-good finite state still escalates instead of silently resetting counters', () => {
  // First-tick corruption: there is no finite state to restore. The physicsTick
  // emitted after apply() must not clear the bad-episode counters, otherwise the
  // 3-consecutive/10-in-window escalation can never fire and recovery is left to
  // the stall watchdog.
  const bot = makeBot({ position: [Number.NaN, 64, 1] });
  let repeated = null;
  const guard = installPhysicsGuard(bot, {
    intervalMs: 10000,
    stallMs: 60000,
    onRepeated: stats => { repeated = stats; },
  });
  try {
    assert.equal(guard.installed, true);
    for (let index = 0; index < CONSECUTIVE_BAD_LIMIT; index += 1) bot.emit('physicsTick');
    assert.ok(repeated, 'repeated escalation must fire even without a last-good state');
    assert.ok(repeated.badFields.includes('pos.x'));
    assert.ok(repeated.consecutive >= CONSECUTIVE_BAD_LIMIT);
    assert.ok(Number.isNaN(bot.entity.position.x), 'no finite state exists to restore; value is untouched');
  } finally {
    guard.uninstall();
  }
});

test('stall watchdog fires once per stall episode after a missing physicsTick', async t => {
  const bot = makeBot();
  const stalled = [];
  const guard = installPhysicsGuard(bot, {
    stallMs: 80,
    intervalMs: 20,
    onStalled: payload => stalled.push(payload),
  });
  t.after(() => guard.uninstall());

  await delay(180);
  assert.equal(stalled.length, 1, 'stall fires once while physicsTick is missing');
  assert.ok(stalled[0].msSinceTick >= 60);
  assert.equal(stalled[0].reason, 'physics_stalled');

  await delay(120);
  assert.equal(stalled.length, 1, 'no stall spam while the episode continues');

  bot.emit('physicsTick');
  await delay(180);
  assert.equal(stalled.length, 2, 'a fresh episode after recovery fires again');
});

test('stall watchdog respects isReady/isAlive and never throws without physics', async t => {
  const bot = makeBot({ physics: false });
  const stalled = [];
  const guard = installPhysicsGuard(bot, {
    stallMs: 60,
    intervalMs: 20,
    isReady: () => false,
    onStalled: payload => stalled.push(payload),
  });
  t.after(() => guard.uninstall());
  assert.equal(guard.installed, false);
  await delay(120);
  assert.equal(stalled.length, 0, 'not ready means no stall action');
  assert.doesNotThrow(() => guard.stats());
  assert.doesNotThrow(() => guard.uninstall());
  assert.doesNotThrow(() => guard.uninstall());
});

test('uninstall restores the original simulatePlayer and stops the watchdog', async t => {
  const bot = makeBot();
  const stalled = [];
  const guard = installPhysicsGuard(bot, {
    stallMs: 50,
    intervalMs: 15,
    onStalled: payload => stalled.push(payload),
  });
  const wrapped = bot.physics.simulatePlayer;
  assert.notEqual(wrapped, bot.__originalSimulate);
  guard.uninstall();
  assert.equal(bot.physics.simulatePlayer, bot.__originalSimulate);
  await delay(120);
  assert.equal(stalled.length, 0, 'uninstalled guard has no watchdog');
  t.after(() => bot.removeAllListeners());
});

test('default stall threshold is the documented 15 s', () => {
  assert.equal(DEFAULT_STALL_MS, 15000);
});

test('rateLimitDecision allows 6 resets per 600 s and then refuses', () => {
  let times = [];
  for (let index = 0; index < 6; index++) {
    const decision = rateLimitDecision(times, { now: 1000 + index, limit: 6, windowMs: 600000 });
    assert.equal(decision.allowed, true, `reset ${index + 1} allowed`);
    times = decision.times;
  }
  const denied = rateLimitDecision(times, { now: 2000, limit: 6, windowMs: 600000 });
  assert.equal(denied.allowed, false);
  assert.equal(denied.used, 6);
  // Window rollover frees the old attempts.
  const afterWindow = rateLimitDecision(times, { now: 700000, limit: 6, windowMs: 600000 });
  assert.equal(afterWindow.allowed, true);
  assert.equal(afterWindow.used, 0);
});
