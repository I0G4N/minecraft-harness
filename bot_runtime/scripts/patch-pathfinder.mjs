#!/usr/bin/env node
/**
 * P0 pathfinder patch for mineflayer-pathfinder 2.4.5.
 *
 * Why: the published 2.4.5 has two known movement defects used by the
 * reference dsh-mc-agent project:
 *   1. lib/physics.js canStraightLine() reports "straight is clear" after the
 *      player is already slightly clipped into a wall, so the executor walks
 *      into the corner instead of jumping.
 *   2. lib/movements.js getMoveDiagonal() may cut a raised corner (a 1-block
 *      step), which the mineflayer physics engine cannot slide around.
 *
 * This script is idempotent. It is invoked by postinstall and explicitly by the
 * bot image build (the image uses npm ci --ignore-scripts). `--check` only
 * reports, and exits 1 when a patch is missing.
 */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const args = process.argv.slice(2);
const checkOnly = args.includes('--check');
const rootArg = args.indexOf('--root') >= 0 ? args[args.indexOf('--root') + 1] : null;
const root = resolve(rootArg ?? process.cwd());
const pfDir = join(root, 'node_modules', 'mineflayer-pathfinder');
const movementsPath = join(pfDir, 'lib', 'movements.js');
const physicsPath = join(pfDir, 'lib', 'physics.js');

for (const p of [movementsPath, physicsPath]) {
  if (!existsSync(p)) {
    console.error(`[patch-pathfinder] missing ${p}; run npm ci first`);
    process.exit(2);
  }
}
const pkg = JSON.parse(readFileSync(join(pfDir, 'package.json'), 'utf8'));
if (pkg.version !== '2.4.5') {
  console.error(`[patch-pathfinder] expected 2.4.5, got ${pkg.version}; review before patching`);
  process.exit(2);
}

let dirty = 0;
let missing = 0;
function replace(file, oldText, newText, label) {
  const text = readFileSync(file, 'utf8');
  if (text.includes(newText)) {
    console.log(`  = ${label} (already patched)`);
    return;
  }
  if (!text.includes(oldText)) {
    console.error(`  x ${label}: anchor not found (pathfinder version changed?)`);
    missing++;
    return;
  }
  if (!checkOnly) writeFileSync(file, text.replace(oldText, newText));
  dirty++;
  console.log(`  + ${label}`);
}

console.log(`[patch-pathfinder] ${pfDir}`);

// ── movements.js: never cut a raised corner ─────────────────────────────────
replace(
  movementsPath,
  `    const blockC = this.getBlock(node, dir.x, 0, dir.z) // Landing block or standing on block when jumping up by 1
    const y = blockC.physical ? 1 : 0
`,
  `    const blockC = this.getBlock(node, dir.x, 0, dir.z) // Landing block or standing on block when jumping up by 1
    const y = blockC.physical ? 1 : 0
    // A diagonal jump can clip the corner of a raised block. Mineflayer physics
    // clips both horizontal axes once the AABB enters a block, so withdraw this
    // edge and let A* use two cardinal moves instead.
    if (y === 1) return
`,
  'getMoveDiagonal: withdraw raised-corner diagonal'
);

replace(
  movementsPath,
  `    const blockB1 = this.getBlock(node, 0, y + 1, dir.z)
    const blockC1 = this.getBlock(node, 0, y, dir.z)
    const blockD1 = this.getBlock(node, 0, y - 1, dir.z)
    cost1 += this.safeOrBreak(blockB1, toBreak1)
`,
  `    const blockB1 = this.getBlock(node, 0, y + 1, dir.z)
    const blockC1 = this.getBlock(node, 0, y, dir.z)
    const blockD1 = this.getBlock(node, 0, y - 1, dir.z)
    if (!blockB1.safe || !blockC1.safe) return
    cost1 += this.safeOrBreak(blockB1, toBreak1)
`,
  'getMoveDiagonal: no diagonal through unsupported side (z)'
);

replace(
  movementsPath,
  `    const blockB2 = this.getBlock(node, dir.x, y + 1, 0)
    const blockC2 = this.getBlock(node, dir.x, y, 0)
    const blockD2 = this.getBlock(node, dir.x, y - 1, 0)
    cost2 += this.safeOrBreak(blockB2, toBreak2)
`,
  `    const blockB2 = this.getBlock(node, dir.x, y + 1, 0)
    const blockC2 = this.getBlock(node, dir.x, y, 0)
    const blockD2 = this.getBlock(node, dir.x, y - 1, 0)
    if (!blockB2.safe || !blockC2.safe) return
    cost2 += this.safeOrBreak(blockB2, toBreak2)
`,
  'getMoveDiagonal: no diagonal through unsupported side (x)'
);

replace(
  movementsPath,
  `    if (cost1 < cost2) {
      cost += cost1
      toBreak.push(...toBreak1)
`,
  `    // The reference patch also refuses a diagonal when either cardinal leg
    // would need a break/place. This is deliberately conservative; a single
    // clipped corner is enough to deadlock the client physics.
    if (cost1 > 0 || cost2 > 0) return
    if (cost1 < cost2) {
      cost += cost1
      toBreak.push(...toBreak1)
`,
  'getMoveDiagonal: no diagonal when either leg needs break/place'
);

// ── physics.js: collision-aware straight-line simulation ────────────────────
replace(
  physicsPath,
  `const { PlayerState } = require('prismarine-physics')
`,
  `const { PlayerState } = require('prismarine-physics')
const { Vec3 } = require('vec3')
`,
  'physics.js: import Vec3'
);

replace(
  physicsPath,
  `  simulateUntil (goal, controller = () => {}, ticks = 1, state = null) {
    if (!state) {
      const simulationControl = {
        forward: this.bot.controlState.forward,
        back: this.bot.controlState.back,
        left: this.bot.controlState.left,
        right: this.bot.controlState.right,
        jump: this.bot.controlState.jump,
        sprint: this.bot.controlState.sprint,
        sneak: this.bot.controlState.sneak
      }
      state = new PlayerState(this.bot, simulationControl)
    }
`,
  `  newPlayerState () {
    const simulationControl = {
      forward: this.bot.controlState.forward,
      back: this.bot.controlState.back,
      left: this.bot.controlState.left,
      right: this.bot.controlState.right,
      jump: this.bot.controlState.jump,
      sprint: this.bot.controlState.sprint,
      sneak: this.bot.controlState.sneak
    }
    return new PlayerState(this.bot, simulationControl)
  }

  simulateUntil (goal, controller = () => {}, ticks = 1, state = null) {
    if (!state) state = this.newPlayerState()
`,
  'physics.js: extract newPlayerState()'
);

replace(
  physicsPath,
  `  canStraightLine (path, sprint = false) {
    const reached = this.getReached(path)
    const state = this.simulateUntil(reached, this.getController(path[0], false, sprint), 200)
    if (reached(state)) return true
`,
  `  collides (pos) {
    // Check the player AABB against block shapes at and around pos. Using
    // block.shapes (rather than boundingBox === 'block') avoids treating
    // slabs/stairs as full walls.
    const width = Number(this.bot.entity.width ?? 0.6)
    const height = Number(this.bot.entity.height ?? 1.8)
    const minX = pos.x - width / 2
    const maxX = pos.x + width / 2
    const minY = pos.y
    const maxY = pos.y + height
    const minZ = pos.z - width / 2
    const maxZ = pos.z + width / 2
    const bx0 = Math.floor(minX)
    const bx1 = Math.floor(maxX - 1e-9)
    const by0 = Math.floor(minY)
    const by1 = Math.floor(maxY - 1e-9)
    const bz0 = Math.floor(minZ)
    const bz1 = Math.floor(maxZ - 1e-9)
    for (let bx = bx0; bx <= bx1; bx++) {
      for (let by = by0; by <= by1; by++) {
        for (let bz = bz0; bz <= bz1; bz++) {
          let block
          try { block = this.world.getBlock(new Vec3(bx, by, bz)) } catch { continue }
          if (!block || !Array.isArray(block.shapes)) continue
          for (const shape of block.shapes) {
            if (!Array.isArray(shape) || shape.length < 6) continue
            const [sx0, sy0, sz0, sx1, sy1, sz1] = shape
            if (minX < bx + sx1 && maxX > bx + sx0 &&
                minY < by + sy1 && maxY > by + sy0 &&
                minZ < bz + sz1 && maxZ > bz + sz0) return true
          }
        }
      }
    }
    return false
  }

  simulateStraight (goal, controller = () => {}, ticks = 200) {
    const state = this.newPlayerState()
    for (let i = 0; i < ticks; i++) {
      controller(state, i)
      this.bot.physics.simulatePlayer(state, this.world)
      if (i > 0 && this.collides(state.pos)) {
        state.blocked = true
        return state
      }
      if (state.isInLava) return state
      if (goal(state)) return state
    }
    return state
  }

  canStraightLine (path, sprint = false) {
    const reached = this.getReached(path)
    const straight = this.simulateStraight(reached, this.getController(path[0], false, sprint), 200)
    // The simulation starts at the bot's current (possibly already clipped)
    // position; only mark blocked after the first simulated tick.
    if (straight.blocked) return false
    if (reached(straight)) return true
`,
  'physics.js: collision-aware canStraightLine'
);

if (missing) {
  console.error(`[patch-pathfinder] ${missing} anchor(s) missing`);
  process.exit(1);
}
if (checkOnly) {
  if (dirty > 0) {
    console.error(`[patch-pathfinder] ${dirty} patch(es) missing`);
    process.exit(1);
  }
  console.log('[patch-pathfinder] check ok');
} else {
  console.log(`[patch-pathfinder] done (${dirty} change${dirty === 1 ? '' : 's'})`);
}
