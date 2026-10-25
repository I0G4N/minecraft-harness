/**
 * Best-effort quiesce of native Minecraft actions during a nonfinite-state
 * episode. The bot's async program cannot be cancelled directly, but stopping
 * pathfinder, digging and held controls prevents the corrupted client state
 * from being continuously written while the recovery window runs.
 */
export function quiesceNativeActions(bot, { log = () => {} } = {}) {
  const result = {
    pathfinder_goal_stopped: false,
    pathfinder_stopped: false,
    digging_stopped: false,
    controls_cleared: 0,
    errors: [],
  };
  const attempt = (label, fn) => {
    try { fn(); } catch (error) {
      result.errors.push({ step: label, error: String(error?.message ?? error).slice(0, 200) });
    }
  };
  attempt('pathfinder.setGoal', () => {
    if (bot?.pathfinder && typeof bot.pathfinder.setGoal === 'function') {
      bot.pathfinder.setGoal(null);
      result.pathfinder_goal_stopped = true;
    }
  });
  attempt('pathfinder.stop', () => {
    if (bot?.pathfinder && typeof bot.pathfinder.stop === 'function') {
      bot.pathfinder.stop();
      result.pathfinder_stopped = true;
    }
  });
  attempt('stopDigging', () => {
    if (bot?.targetDigBlock && typeof bot.stopDigging === 'function') {
      bot.stopDigging();
      result.digging_stopped = true;
    }
  });
  attempt('controls', () => {
    if (!bot?.controlState || typeof bot.setControlState !== 'function') return;
    for (const [key, active] of Object.entries(bot.controlState)) {
      if (!active) continue;
      bot.setControlState(key, false);
      result.controls_cleared++;
    }
  });
  log(result);
  return result;
}
