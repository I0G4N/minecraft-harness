import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const PF_DIR = require.resolve('mineflayer-pathfinder/package.json').replace(/\/package\.json$/, '');
const Physics = require(`${PF_DIR}/lib/physics.js`);
const Movements = require(`${PF_DIR}/lib/movements.js`);
const mcData = require('minecraft-data')('1.20.4');
const Block = require('prismarine-block')(mcData);
const { Vec3 } = require('vec3');

const FULL = [[0, 0, 0, 1, 1, 1]];
const V = (x, y, z) => new Vec3(x, y, z);

function fakeBot(pos, grid) {
  return {
    entity: { position: V(pos.x, pos.y, pos.z), width: 0.6, height: 1.8 },
    controlState: { forward: false, back: false, left: false, right: false, jump: false, sprint: false, sneak: false },
    blockAt: p => grid.get(`${Math.floor(p.x)},${Math.floor(p.y)},${Math.floor(p.z)}`) ?? null,
    physics: {
      simulatePlayer(state, world) {
        const dx = state._tx - state.pos.x;
        const dz = state._tz - state.pos.z;
        if (Math.abs(dx) > 1e-6) state.pos.x += Math.sign(dx) * Math.min(0.25, Math.abs(dx));
        if (Math.abs(dz) > 1e-6) state.pos.z += Math.sign(dz) * Math.min(0.25, Math.abs(dz));
        state.onGround = true;
        state.isInWater = false;
        state.isInLava = false;
      },
    },
  };
}

function makePhysics(bot) {
  const phys = new Physics(bot);
  phys.newPlayerState = () => ({
    pos: bot.entity.position.clone(),
    control: {},
    onGround: true,
    isInWater: false,
    isInLava: false,
  });
  const realGetController = phys.getController.bind(phys);
  phys.getController = (target, jump, sprint, jumpAfter = 0) => {
    const ctrl = realGetController(target, jump, sprint, jumpAfter);
    return (state, tick) => { ctrl(state, tick); state._tx = target.x; state._tz = target.z; };
  };
  return phys;
}

function gridWithStep() {
  const grid = new Map();
  const put = (x, y, z, shapes, name = 'grass_block') => grid.set(`${x},${y},${z}`, { name, shapes, boundingBox: 'block' });
  for (let x = -6; x <= 3; x++) for (let z = 2; z <= 8; z++) put(x, 65, z, FULL);
  put(-2, 66, 5, FULL);
  put(2, 65, 5, [[0, 0, 0, 1, 0.5, 1]], 'stone_slab');
  put(-1, 66, 5, FULL);
  put(1, 66, 5, FULL);
  return grid;
}

test('pathfinder source contains the P0 patches', () => {
  const fs = require('node:fs');
  const physics = fs.readFileSync(`${PF_DIR}/lib/physics.js`, 'utf8');
  const movements = fs.readFileSync(`${PF_DIR}/lib/movements.js`, 'utf8');
  assert.match(physics, /collides\s*\(pos\)/);
  assert.match(physics, /simulateStraight\s*\(/);
  assert.match(physics, /straight\.blocked/);
  assert.match(movements, /if \(y === 1\) return/);
  assert.match(movements, /if \(cost1 > 0 \|\| cost2 > 0\) return/);
});

test('collides detects a clipped wall but not the exact edge', () => {
  const grid = gridWithStep();
  const bot = fakeBot({ x: -2.3, y: 66, z: 5.5 }, grid);
  const phys = makePhysics(bot);
  assert.equal(phys.collides(V(-2.06, 66, 5.5)), true);
  assert.equal(phys.collides(V(-2.30, 66, 5.5)), false);
  assert.equal(phys.collides(V(-3.00, 66, 5.5)), false);
  assert.equal(phys.collides(V(0.8, 66, 5.5)), true);
});

test('canStraightLine no longer reports a clipped corner as clear', () => {
  const grid = gridWithStep();
  const bot = fakeBot({ x: -2.3, y: 66, z: 5.5 }, grid);
  const phys = makePhysics(bot);
  const path = [V(-1.5, 66, 4.5)];
  const straight = phys.simulateStraight(phys.getReached(path), phys.getController(path[0], false, true), 200);
  assert.equal(straight.blocked, true);
  assert.equal(phys.canStraightLine(path, true), false);

  const clearBot = fakeBot({ x: -4.5, y: 66, z: 5.5 }, grid);
  const clearPhys = makePhysics(clearBot);
  const clearPath = [V(-3.0, 66, 5.5)];
  assert.equal(clearPhys.canStraightLine(clearPath, true), true);
});

test('getMoveDiagonal refuses a raised corner but permits an open diagonal', () => {
  const grid = new Map();
  const key = (x, y, z) => `${x},${y},${z}`;
  const setBlock = (x, y, z, name) => {
    const b = Block.fromStateId(mcData.blocksByName[name].defaultState, 0);
    b.position = new Vec3(x, y, z);
    grid.set(key(x, y, z), b);
  };
  for (let x = -7; x <= 3; x++) for (let y = 64; y <= 68; y++) for (let z = 1; z <= 9; z++) setBlock(x, y, z, 'air');
  for (let x = -7; x <= 3; x++) for (let z = 1; z <= 9; z++) setBlock(x, 65, z, 'grass_block');

  const bot = {
    registry: mcData,
    username: 'test',
    entity: { position: V(-2.5, 66, 5.5), username: 'test', effects: [] },
    entities: {},
    pathfinder: { bestHarvestTool: () => null },
    blockAt: p => grid.get(key(Math.floor(p.x), Math.floor(p.y), Math.floor(p.z))) ?? null,
  };
  const mv = new Movements(bot);
  const node = { x: -3, y: 66, z: 5, remainingBlocks: 0 };
  const ne = { x: 1, z: -1 };

  setBlock(-2, 66, 5, 'grass_block');
  const blocked = [];
  mv.getMoveDiagonal(node, ne, blocked);
  assert.equal(blocked.length, 0);

  setBlock(-2, 66, 5, 'air');
  const open = [];
  mv.getMoveDiagonal(node, ne, open);
  assert.equal(open.length, 1);
});
