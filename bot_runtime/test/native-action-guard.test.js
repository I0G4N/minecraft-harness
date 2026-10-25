import test from 'node:test';
import assert from 'node:assert/strict';
import { quiesceNativeActions } from '../src/native-action-guard.js';

test('quiesce stops pathfinder, digging and held controls', () => {
  const calls = [];
  const bot = {
    pathfinder: {
      setGoal(goal) { calls.push(['setGoal', goal]); },
      stop() { calls.push(['stop']); },
    },
    targetDigBlock: { name: 'stone' },
    stopDigging() { calls.push(['stopDigging']); },
    controlState: { forward: true, jump: false, sprint: true },
    setControlState(key, value) { calls.push(['control', key, value]); },
  };
  const result = quiesceNativeActions(bot);
  assert.equal(result.pathfinder_goal_stopped, true);
  assert.equal(result.pathfinder_stopped, true);
  assert.equal(result.digging_stopped, true);
  assert.equal(result.controls_cleared, 2);
  assert.deepEqual(calls, [['setGoal', null], ['stop'], ['stopDigging'], ['control', 'forward', false], ['control', 'sprint', false]]);
});

test('quiesce is best-effort when individual native operations throw', () => {
  const bot = {
    pathfinder: { setGoal() { throw new Error('bad goal'); }, stop() { throw new Error('bad stop'); } },
    targetDigBlock: null,
    controlState: { forward: true },
    setControlState() { throw new Error('bad control'); },
  };
  const result = quiesceNativeActions(bot);
  assert.equal(result.pathfinder_goal_stopped, false);
  assert.equal(result.controls_cleared, 0);
  assert.equal(result.errors.length, 3);
});
