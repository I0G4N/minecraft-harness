/**
 * Client movement-packet guards (worker-internal, diagnostic + safety).
 *
 * Two independent pre-existing Mineflayer behaviours showed up in the 60-minute
 * validation:
 *
 * 1. `moved too quickly!` warning bursts around death/respawn. Paper rejects a
 *    client movement packet, sends a `position` correction; Mineflayer's
 *    physics plugin applies it and *immediately echoes* a client position
 *    packet, which can produce a network-speed correction loop (hundreds of
 *    warnings in one second). A dead player has no legitimate movement to send:
 *    minflayer's own `updatePosition()` stops after ~20 dead ticks, but the
 *    immediate echo in the `position` handler is not gated by death. Suppress
 *    outgoing position packets while `bot.isAlive === false`; keep
 *    `teleport_confirm` and all non-movement packets untouched. Physics resumes
 *    normally on respawn because the server sends a fresh position packet.
 *
 * 2. `multiplayer.disconnect.invalid_entity_attacked`: `bot.attack(entity)`
 *    forwards `entity.id` without checking that the target is still a tracked,
 *    non-self, in-range entity. After respawn/soft reset, a stale or self
 *    entity object can be submitted and Paper kicks the bot. Validate targets
 *    before sending `use_entity`.
 *
 * Both helpers are duck-typed (no Mineflayer import) and never throw from
 * bookkeeping. They are installed by worker.js on every new client generation.
 */

const MOVEMENT_PACKETS = new Set(['position', 'position_look', 'flying']);
const ATTACK_RANGE = 6.5;

function finitePoint(point) {
  return point && Number.isFinite(point.x) && Number.isFinite(point.y) && Number.isFinite(point.z);
}

export function isTrackedEntity(bot, entity) {
  if (!bot || !entity || typeof entity !== 'object' || !Number.isInteger(entity.id)) return false;
  const tracked = bot.entities?.[entity.id];
  return Boolean(tracked) && tracked === entity;
}

export function assertAttackable(bot, target) {
  if (!target || typeof target !== 'object') throw new TypeError('attack requires an entity object');
  if (!Number.isInteger(target.id)) throw new TypeError('attack requires an entity with a numeric id');
  if (!isTrackedEntity(bot, target)) throw new Error('attack target is not a live tracked entity');
  if (bot.isAlive === false) throw new Error('cannot attack while dead');
  const self = bot.entity;
  if (self && (target === self || target.id === self.id)) throw new Error('cannot attack self');
  const selfPosition = self?.position;
  const targetPosition = target.position;
  if (finitePoint(selfPosition) && finitePoint(targetPosition)) {
    const dx = targetPosition.x - selfPosition.x;
    const dy = targetPosition.y - selfPosition.y;
    const dz = targetPosition.z - selfPosition.z;
    if (Math.sqrt(dx * dx + dy * dy + dz * dz) > ATTACK_RANGE) {
      throw new Error('attack target is out of reach');
    }
  }
  return target;
}

/**
 * Wrap `bot.attack` (and therefore the agent-facing proxy) with target
 * validation. Returns a disposer.
 */
export function installAttackGuard(bot) {
  if (!bot || typeof bot.attack !== 'function') return () => {};
  const original = bot.attack;
  const guardedAttack = function guardedAttack(target) {
    assertAttackable(bot, target);
    return original.call(this, target);
  };
  bot.attack = guardedAttack;
  return () => {
    if (bot.attack === guardedAttack) bot.attack = original;
  };
}

/**
 * Drop outgoing position packets while the bot is dead. Returns
 * `{uninstall, stats}`; `stats.suppressed` counts dropped packets and
 * `stats.byPacket` counts per packet name.
 */
export function installDeathMovementSuppression(bot, options = {}) {
  const onSuppressed = typeof options.onSuppressed === 'function' ? options.onSuppressed : null;
  const client = bot?._client;
  if (!client || typeof client.write !== 'function') {
    return { uninstall: () => {}, stats: () => ({ installed: false, suppressed: 0, byPacket: {} }) };
  }
  const original = client.write.bind(client);
  const stats = { installed: true, suppressed: 0, byPacket: {} };
  const guardedWrite = function guardedWrite(name, params) {
    if (MOVEMENT_PACKETS.has(name) && bot.isAlive === false) {
      stats.suppressed += 1;
      stats.byPacket[name] = (stats.byPacket[name] ?? 0) + 1;
      if (onSuppressed) {
        try { onSuppressed({ name, total: stats.suppressed, byPacket: stats.byPacket }); } catch { /* diagnostics */ }
      }
      return true;
    }
    return original(name, params);
  };
  client.write = guardedWrite;
  return {
    uninstall() {
      if (client.write === guardedWrite) client.write = original;
    },
    stats: () => ({ ...stats, byPacket: { ...stats.byPacket } }),
  };
}
