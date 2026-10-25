import test from 'node:test';
import assert from 'node:assert/strict';
import {
  copySafeWorkerConfig,
  PHYSICS_STALL_MS_DEFAULT,
  PHYSICS_STALL_MS_MAX,
  PHYSICS_STALL_MS_MIN,
  SAFE_WORKER_FIELDS,
} from '../src/worker-host.js';

function workerConfig(overrides = {}) {
  return {
    mode: 'fake', username: 'alice', version: '1.20.4', lifecycle: 'strict',
    maxBackgroundJobs: 8, remainingRunMs: 1000, runDeadlineAt: 123456,
    maxLogs: 128, maxLogBytes: 262144, maxResultBytes: 262144,
    leftoverPolicy: 'reset', chatDelivery: 'events', maxLeftoverScopes: 256,
    paperSingleProgram: false, physicsStallMs: 30000,
    // Fields the launcher must never forward from an untrusted worker_config.
    host: 'evil.example.test', port: 31337, env: { SECRET: 'leak' },
    image: 'custom:latest', network: 'host', mounts: ['/:/host'],
    ...overrides,
  };
}

const fixed = { host: '127.0.0.1', port: 25565 };

test('safe worker config copies only declared fields and fixes the game target', () => {
  const safe = copySafeWorkerConfig(workerConfig(), fixed);
  assert.deepEqual(Object.keys(safe).sort(), [...SAFE_WORKER_FIELDS, 'physicsStallMs', 'host', 'port'].sort());
  assert.equal(safe.host, '127.0.0.1');
  assert.equal(safe.port, 25565);
  assert.equal(safe.env, undefined);
  assert.equal(safe.image, undefined);
  assert.equal(safe.network, undefined);
  assert.equal(safe.mounts, undefined);
  assert.equal(safe.username, 'alice');
  assert.equal(safe.mode, 'fake');
});

test('physicsStallMs is copied as a validated integer', () => {
  for (const value of [PHYSICS_STALL_MS_MIN, 15000, PHYSICS_STALL_MS_MAX]) {
    assert.equal(copySafeWorkerConfig(workerConfig({ physicsStallMs: value }), fixed).physicsStallMs, value);
  }
  // Missing (e.g. an older supervisor) defaults to the documented interval.
  assert.equal(copySafeWorkerConfig(workerConfig({ physicsStallMs: undefined }), fixed).physicsStallMs,
    PHYSICS_STALL_MS_DEFAULT);
});

test('physicsStallMs outside the supported range is rejected', () => {
  for (const value of [PHYSICS_STALL_MS_MIN - 1, PHYSICS_STALL_MS_MAX + 1, 0, -1,
    15000.5, '15000', true, null, NaN, Infinity]) {
    assert.throws(() => copySafeWorkerConfig(workerConfig({ physicsStallMs: value }), fixed),
      /physicsStallMs/, `physicsStallMs=${String(value)} must be rejected`);
  }
});

test('a non-object worker configuration is rejected', () => {
  for (const value of [null, undefined, 'worker', 42, []]) {
    assert.throws(() => copySafeWorkerConfig(value, fixed), /object/i);
  }
});
