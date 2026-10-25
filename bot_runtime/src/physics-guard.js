/**
 * Physics boundary guard for the Mineflayer worker.
 *
 * Mineflayer calls `physics.simulatePlayer(new PlayerState(bot, control), world).apply(bot)`.
 * When chunk/world state is stale or numerically corrupted (NaN/Infinity), the
 * returned PlayerState can contain nonfinite position/velocity. Applying it
 * publishes a poisoned client state that the identity proxy rejects (or that
 * trips the server's invalid-movement check), which used to end in a hard worker
 * exit. This guard wraps `simulatePlayer` and overwrites the returned state with
 * the last known-good finite state (zeroing velocity) *before* `.apply(bot)` runs,
 * so a bad tick can never publish NaN. Repeated bad ticks/stalls are escalated to
 * the worker's in-process soft reset instead of a process exit.
 *
 * The module is deliberately duck-typed (no Mineflayer import) so it is cheap to
 * unit test with fake bot/entity/physics objects, and it never throws from its
 * own bookkeeping: all callbacks are best-effort and guarded.
 */

export const DEFAULT_STALL_MS = 15000;
export const DEFAULT_CHECK_INTERVAL_MS = 500;
export const CONSECUTIVE_BAD_LIMIT = 3;
export const BAD_WINDOW_MS = 60000;
export const BAD_WINDOW_LIMIT = 10;
const RECENT_API_LIMIT = 20;

const finiteNumber = value => typeof value === 'number' && Number.isFinite(value);
const finiteVector = vector => Boolean(vector)
  && finiteNumber(vector.x) && finiteNumber(vector.y) && finiteNumber(vector.z);

/** Snapshot the client-visible physics state of a Mineflayer entity. */
export function clientState(entity) {
  const position = entity?.position;
  const velocity = entity?.velocity;
  return {
    pos: { x: position?.x, y: position?.y, z: position?.z },
    vel: { x: velocity?.x, y: velocity?.y, z: velocity?.z },
    yaw: entity?.yaw,
    pitch: entity?.pitch,
    onGround: entity?.onGround,
  };
}

/** True when a clientState() snapshot has finite position/velocity/look values. */
export function stateIsFinite(state) {
  if (!state || typeof state !== 'object') return false;
  return finiteVector(state.pos) && finiteVector(state.vel)
    && finiteNumber(state.yaw) && finiteNumber(state.pitch);
}

/** Names of the fields in one clientState() snapshot that are not finite. */
export function nonfiniteFields(state) {
  const fields = [];
  for (const axis of ['x', 'y', 'z']) {
    if (!finiteNumber(state?.pos?.[axis])) fields.push(`pos.${axis}`);
    if (!finiteNumber(state?.vel?.[axis])) fields.push(`vel.${axis}`);
  }
  if (!finiteNumber(state?.yaw)) fields.push('yaw');
  if (!finiteNumber(state?.pitch)) fields.push('pitch');
  return fields;
}

function setVectorFields(target, source) {
  if (!target || typeof target !== 'object') return false;
  try {
    target.x = source.x;
    target.y = source.y;
    target.z = source.z;
    return true;
  } catch {
    return false;
  }
}

/**
 * Overwrite a returned PlayerState (or PlayerState-like object) with the last
 * known-good client state and zero velocity. Mutates in place where possible so
 * the real Vec3 instances/classes `apply()` expects are preserved.
 */
function restorePlayerState(output, good, { zeroVelocity = true } = {}) {
  if (!output || typeof output !== 'object' || !good) return false;
  try {
    if (!setVectorFields(output.pos, good.pos)) return false;
    if (!setVectorFields(output.vel, zeroVelocity
      ? { x: 0, y: 0, z: 0 }
      : good.vel)) {
      // Some PlayerState variants expose velocity under `velocity`.
      if (!setVectorFields(output.velocity, zeroVelocity
        ? { x: 0, y: 0, z: 0 }
        : good.vel)) return false;
    }
    output.yaw = good.yaw;
    output.pitch = good.pitch;
    output.onGround = good.onGround;
    return true;
  } catch {
    return false;
  }
}

/** Restore the live entity's look values (apply() does not copy yaw/pitch). */
function restoreEntityLook(entity, good) {
  if (!entity || !good) return false;
  try {
    entity.yaw = good.yaw;
    entity.pitch = good.pitch;
    return true;
  } catch {
    return false;
  }
}

function safeDateNow(now) {
  try {
    const value = Number(now());
    return Number.isFinite(value) ? value : Date.now();
  } catch {
    return Date.now();
  }
}

/**
 * Install the physics guard on a bot.
 *
 * @param {object} bot duck-typed Mineflayer bot (needs physics.simulatePlayer,
 *   entity, on()).
 * @param {object} [options]
 * @param {number} [options.stallMs] watchdog threshold for missing physicsTick.
 * @param {Function} [options.onPrevented] called with
 *   `{input, output, badFields, recentApi, state}` on every prevented bad tick.
 * @param {Function} [options.onRepeated] called once per bad episode with
 *   `{badFields, consecutive, inWindow, recentApi}`.
 * @param {Function} [options.onStalled] called once per stall episode with
 *   `{msSinceTick, msSinceSpawn, spawnedAt, lastPhysicsTickAt}`.
 * @param {Function} [options.onTick] called on every physicsTick.
 * @param {Function} [options.isReady] optional worker readiness predicate.
 * @param {Function} [options.recentApi] optional provider for worker API ring.
 * @returns {{uninstall: Function, stats: Function, installed: boolean}}
 */
export function installPhysicsGuard(bot, options = {}) {
  const stallMs = Number.isFinite(options.stallMs) && options.stallMs > 0
    ? options.stallMs
    : DEFAULT_STALL_MS;
  const intervalMs = Number.isFinite(options.intervalMs) && options.intervalMs > 0
    ? options.intervalMs
    : DEFAULT_CHECK_INTERVAL_MS;
  const onPrevented = typeof options.onPrevented === 'function' ? options.onPrevented : null;
  const onRepeated = typeof options.onRepeated === 'function' ? options.onRepeated : null;
  const onStalled = typeof options.onStalled === 'function' ? options.onStalled : null;
  const onTick = typeof options.onTick === 'function' ? options.onTick : null;
  const isReady = typeof options.isReady === 'function' ? options.isReady : () => true;
  const recentApiProvider = typeof options.recentApi === 'function'
    ? options.recentApi : () => [];

  let installed = false;
  let uninstalled = false;
  let timer = null;
  let originalSimulate = null;
  let physicsTarget = null;

  let lastGoodState = null;
  let spawnedAt = null;
  let lastPhysicsTickAt = safeDateNow(options.now ?? Date.now);
  let stalled = false;
  let badTickTimes = [];
  let consecutiveBad = 0;
  let repeatedFired = false;
  let preventedCount = 0;
  let lastPreventedAt = null;
  let lastBadFields = [];
  let stallCount = 0;

  const nowFn = typeof options.now === 'function' ? options.now : Date.now;

  const safeCall = (callback, payload) => {
    if (typeof callback !== 'function') return;
    try { callback(payload); } catch { /* guard bookkeeping never throws */ }
  };

  const safeRecentApi = () => {
    try {
      const entries = recentApiProvider();
      return Array.isArray(entries) ? entries.slice(-RECENT_API_LIMIT) : [];
    } catch {
      return [];
    }
  };

  const currentState = () => {
    try { return clientState(bot?.entity); } catch { return null; }
  };

  const rememberGoodState = () => {
    const state = currentState();
    if (stateIsFinite(state)) {
      lastGoodState = state;
      return state;
    }
    return null;
  };

  const recordGoodTick = () => {
    lastPhysicsTickAt = safeDateNow(nowFn);
    stalled = false;
    const state = currentState();
    if (stateIsFinite(state)) {
      badTickTimes = [];
      if (consecutiveBad > 0) {
        consecutiveBad = 0;
        repeatedFired = false;
        lastBadFields = [];
      }
      rememberGoodState();
      return;
    }
    // A physicsTick fired but the applied client state is still nonfinite (e.g.
    // first-tick corruption with no last-good state to restore). Do not clear
    // the bad-episode counters here, otherwise the 3-consecutive/10-in-60 s
    // escalation can never fire and recovery falls back to the 15 s stall
    // watchdog. Count it as another bad tick so escalation stays possible.
    recordBadTick(nonfiniteFields(state ?? {}));
  };

  const recordBadTick = (badFields) => {
    const at = safeDateNow(nowFn);
    consecutiveBad += 1;
    preventedCount += 1;
    lastPreventedAt = at;
    lastBadFields = badFields.slice();
    badTickTimes.push(at);
    while (badTickTimes.length && at - badTickTimes[0] > BAD_WINDOW_MS) badTickTimes.shift();
    if (!repeatedFired
        && (consecutiveBad >= CONSECUTIVE_BAD_LIMIT || badTickTimes.length >= BAD_WINDOW_LIMIT)) {
      repeatedFired = true;
      safeCall(onRepeated, {
        reason: 'nonfinite_bot_state',
        badFields: badFields.slice(),
        consecutive: consecutiveBad,
        inWindow: badTickTimes.length,
        prevented: preventedCount,
        recentApi: safeRecentApi(),
      });
    }
  };

  const guardSimulate = function guardedSimulatePlayer(input, world) {
    if (uninstalled) return originalSimulate.apply(this, [input, world]);
    let before = null;
    try { before = currentState(); } catch { before = null; }
    const output = originalSimulate.apply(this, [input, world]);
    try {
      const outputState = output && typeof output === 'object'
        ? {
          pos: output.pos,
          vel: output.vel ?? output.velocity,
          yaw: output.yaw ?? bot?.entity?.yaw,
          pitch: output.pitch ?? bot?.entity?.pitch,
        }
        : null;
      const entityState = currentState();
      const fields = [];
      const outputBad = nonfiniteFields(outputState);
      const entityLookBad = entityState
        ? nonfiniteFields({ pos: entityState.pos, vel: { x: 0, y: 0, z: 0 },
          yaw: entityState.yaw, pitch: entityState.pitch }).filter(field => field === 'yaw' || field === 'pitch')
        : [];
      const entityPositionBad = entityState && nonfiniteFields(entityState)
        .filter(field => field.startsWith('pos.'));
      fields.push(...outputBad.map(field => field.startsWith('vel') ? field : field));
      for (const field of entityLookBad) if (!fields.includes(field)) fields.push(field);
      if (!fields.length && entityPositionBad?.length) fields.push(...entityPositionBad);
      if (!fields.length) return output;
      const good = (lastGoodState && stateIsFinite(lastGoodState)) ? lastGoodState
        : (before && stateIsFinite(before) ? before : null);
      let restored = false;
      if (good && output && typeof output === 'object') {
        restored = restorePlayerState(output, good);
        // apply() does not copy yaw/pitch; repair the live entity so no look
        // packet can publish NaN. Position/velocity are fixed via output.
        if (entityLookBad.length || entityPositionBad?.length) {
          restoreEntityLook(bot?.entity, good);
        }
      }
      const payload = {
        input,
        output,
        badFields: fields,
        restored,
        recentApi: safeRecentApi(),
        state: good,
      };
      recordBadTick(fields);
      if (!good && !repeatedFired) {
        // No finite state exists to restore, so this bad PlayerState will be
        // applied. Escalate immediately: for position NaN Mineflayer's
        // tickPhysics early-returns on the blockAt(NaN) gate and no further
        // physicsTick/simulate calls arrive, so the 3-consecutive threshold
        // would otherwise never fire and only the 15 s stall watchdog recovers.
        repeatedFired = true;
        safeCall(onRepeated, {
          reason: 'nonfinite_bot_state',
          badFields: fields.slice(),
          consecutive: Math.max(consecutiveBad, CONSECUTIVE_BAD_LIMIT),
          inWindow: badTickTimes.length,
          prevented: preventedCount,
          recentApi: safeRecentApi(),
          noLastGood: true,
        });
      }
      safeCall(onPrevented, payload);
      return output;
    } catch {
      // Guard bookkeeping must never break the physics tick.
      return output;
    }
  };

  const onPhysicsTick = () => {
    try {
      recordGoodTick();
      safeCall(onTick, { at: lastPhysicsTickAt, state: lastGoodState });
    } catch { /* never throw from a bot listener */ }
  };

  const onSpawn = () => {
    try {
      spawnedAt = safeDateNow(nowFn);
      lastPhysicsTickAt = spawnedAt;
      rememberGoodState();
    } catch { /* best effort */ }
  };

  const checkStall = () => {
    try {
      if (uninstalled) return;
      const now = safeDateNow(nowFn);
      if (spawnedAt === null) {
        if (bot?.entity) spawnedAt = now;
        return;
      }
      let ready = true;
      try { ready = Boolean(isReady()); } catch { ready = true; }
      if (!ready) return;
      if (bot?.isAlive === false) return;
      if (now - spawnedAt <= stallMs) return;
      if (now - lastPhysicsTickAt <= stallMs) return;
      if (stalled) return;
      stalled = true;
      stallCount += 1;
      safeCall(onStalled, {
        reason: 'physics_stalled',
        msSinceTick: now - lastPhysicsTickAt,
        msSinceSpawn: now - spawnedAt,
        spawnedAt,
        lastPhysicsTickAt,
        stallMs,
      });
    } catch { /* watchdog must never throw */ }
  };

  // Wrap bot.physics.simulatePlayer when available; a bot without physics (or
  // without the plugin) still gets the watchdog and is otherwise a no-op.
  try {
    const physics = bot?.physics;
    if (physics && typeof physics.simulatePlayer === 'function') {
      physicsTarget = physics;
      originalSimulate = physics.simulatePlayer;
      physics.simulatePlayer = guardSimulate;
      installed = true;
    }
  } catch { installed = false; }

  try {
    if (bot && typeof bot.on === 'function') {
      bot.on('physicsTick', onPhysicsTick);
      bot.on('spawn', onSpawn);
    }
  } catch { /* listeners are best effort */ }

  try {
    spawnedAt = safeDateNow(nowFn);
    rememberGoodState();
    timer = setInterval(checkStall, intervalMs);
    timer.unref?.();
  } catch { timer = null; }

  return {
    installed,
    uninstall() {
      if (uninstalled) return;
      uninstalled = true;
      try {
        if (timer !== null) clearInterval(timer);
        timer = null;
      } catch { /* ignore */ }
      try {
        if (bot && typeof bot.removeListener === 'function') {
          bot.removeListener('physicsTick', onPhysicsTick);
          bot.removeListener('spawn', onSpawn);
        }
      } catch { /* ignore */ }
      try {
        if (physicsTarget && originalSimulate
            && physicsTarget.simulatePlayer === guardSimulate) {
          physicsTarget.simulatePlayer = originalSimulate;
        }
      } catch { /* ignore */ }
    },
    stats() {
      return {
        installed,
        prevented: preventedCount,
        consecutive: consecutiveBad,
        inWindow: badTickTimes.length,
        repeatedFired,
        badFields: lastBadFields.slice(),
        lastPreventedAt,
        lastGoodState,
        spawnedAt,
        lastPhysicsTickAt,
        stalled,
        stalls: stallCount,
      };
    },
  };
}

/**
 * Pure soft-reset rate-limit decision (6 per 600 s by default). Kept here so
 * the worker's reset policy is unit-testable without importing worker.js (which
 * starts Mineflayer) and without any Mineflayer dependency.
 *
 * @param {number[]} timestamps previous reset attempt timestamps (ms).
 * @param {{now?: number, limit?: number, windowMs?: number}} [options]
 * @returns {{allowed: boolean, times: number[], used: number, limit: number, windowMs: number}}
 */
export function rateLimitDecision(timestamps = [], options = {}) {
  const now = Number.isFinite(options.now) ? options.now : Date.now();
  const limit = Number.isFinite(options.limit) && options.limit > 0
    ? Math.floor(options.limit) : 6;
  const windowMs = Number.isFinite(options.windowMs) && options.windowMs > 0
    ? Math.floor(options.windowMs) : 600000;
  const times = (Array.isArray(timestamps) ? timestamps : [])
    .filter(value => Number.isFinite(value) && now - value <= windowMs && value <= now)
    .sort((left, right) => left - right);
  const allowed = times.length < limit;
  return {
    allowed,
    times: allowed ? [...times, now] : times,
    used: times.length,
    limit,
    windowMs,
  };
}
