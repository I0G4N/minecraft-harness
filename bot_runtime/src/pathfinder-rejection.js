// Pathfinder's normal "this route is not usable" outcomes are not fatal worker
// errors. In particular, a derived/background promise can reject after its
// original caller has already moved on; treating that as fatal restarts the
// whole worker and drops the viewer for no experimental benefit.
//
// Only these named outcomes are ignored, and Timeout additionally has to look
// like a pathfinder stack frame so a generic Timeout is still fatal.
export const PATHFINDER_REJECTION_NAMES = Object.freeze(['PathStopped', 'NoPath', 'GoalChanged', 'Timeout']);

export function isPathfinderRejection(error) {
  const name = String(error?.name ?? error?.constructor?.name ?? '');
  if (!PATHFINDER_REJECTION_NAMES.includes(name)) return false;
  if (name !== 'Timeout') return true;
  const stack = String(error?.stack ?? '');
  return /mineflayer-pathfinder|\bgoto\.js\b/i.test(stack);
}
