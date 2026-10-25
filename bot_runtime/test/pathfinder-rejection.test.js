import test from 'node:test';
import assert from 'node:assert/strict';
import { isPathfinderRejection } from '../src/pathfinder-rejection.js';

test('pathfinder route outcomes are classified as non-fatal', () => {
  for (const name of ['PathStopped', 'NoPath', 'GoalChanged']) {
    assert.equal(isPathfinderRejection(Object.assign(new Error(name), { name })), true);
  }
  const timeout = Object.assign(new Error('Took too long'), { name: 'Timeout' });
  timeout.stack = 'Error: Took too long\n    at goto.js:1:1';
  assert.equal(isPathfinderRejection(timeout), true);
});

test('other errors and generic timeouts stay fatal', () => {
  assert.equal(isPathfinderRejection(new Error('boom')), false);
  assert.equal(isPathfinderRejection(Object.assign(new Error('generic'), { name: 'Timeout' })), false);
  assert.equal(isPathfinderRejection({ name: 'PathStopped', stack: 'unrelated' }), true);
  assert.equal(isPathfinderRejection(null), false);
});
