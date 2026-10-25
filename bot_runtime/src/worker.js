import vm from 'node:vm';
import { createRequire } from 'node:module';
import { EventEmitter } from 'node:events';
import { createHash, randomUUID } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import { instrumentLoopDeadlines } from './loop-guard.mjs';
import { boundedValue } from './limits.js';
import { modulePolicy } from './capabilities.js';
import { Worker } from 'node:worker_threads';
import { fileURLToPath } from 'node:url';
import { isPathfinderRejection } from './pathfinder-rejection.js';
import { quiesceNativeActions } from './native-action-guard.js';
import { installPhysicsGuard, clientState as physicsClientState, stateIsFinite as physicsStateIsFinite, nonfiniteFields as physicsNonfiniteFields, rateLimitDecision } from './physics-guard.js';

const nativeRequire = createRequire(import.meta.url);
const config = JSON.parse(process.env.BOT_WORKER_CONFIG);
const leftoverPolicy = config.leftoverPolicy === 'allow';
const MUTATING_FS = new Set(['appendFile','appendFileSync','chmod','chmodSync','chown','chownSync','copyFile','copyFileSync','cp','cpSync','createWriteStream','link','linkSync','mkdir','mkdirSync','mkdtemp','mkdtempSync','rename','renameSync','rm','rmSync','rmdir','rmdirSync','symlink','symlinkSync','truncate','truncateSync','unlink','unlinkSync','utimes','utimesSync','write','writeFile','writeFileSync','writeSync','writev','writevSync']);
function readOnlyFs(fs) {
  return new Proxy(fs, { get(target, prop) {
    if (prop === 'promises') return readOnlyFs(target.promises);
    const value = target[prop];
    if (typeof value === 'function') {
      if (MUTATING_FS.has(prop)) return () => { const error = new Error('Filesystem writes are disabled in paper mode'); error.code = 'EPERM'; throw error; };
      return value.bind(target);
    }
    return value;
  }});
}
const leftoverScopes = new Set();
const pendingChat = [];
const scopes = new AsyncLocalStorage();
let pendingSends = 0;
const send = (message) => {
  if (!process.connected) return;
  // process.send returning false is backpressure, not a request to enqueue more.
  if (pendingSends >= 64) { process.exit(72); return; }
  pendingSends++;
  process.send(message, error => { pendingSends--; if (error) process.exit(73); });
};
let bot = null;
// ONE stable agent-facing proxy for every execution; it resolves the *current*
// Mineflayer client through a getter so a program that outlives a soft reset can
// never keep acting on (or reviving) the discarded client.
let agentBot = makeAgentFacingBot(() => bot);
let botGeneration = 0;
let botReady = false;
let resetState = null; // { promise, reason } while a soft reset is in flight
let pendingExecutions = []; // execute messages that arrived during reset/startup
const softResetTimes = [];
let softResetCount = 0;
let physicsGuard = null;
let intentionalBotQuit = false;
let preventedLogs = 0;
let viewer = null;
let viewerSetupDone = false;
let fatalExitScheduled = false;
const PENDING_EXECUTION_LIMIT = 2;
const SOFT_RESET_RATE_LIMIT = 6;
const SOFT_RESET_RATE_WINDOW_MS = 600000;
const SOFT_RESET_RETRY_BACKOFF_MS = 2000;
const SOFT_RESET_ATTEMPTS = 3;
const SOFT_RESET_LOGIN_GRACE_MS = 500;
const SPAWN_TIMEOUT_MS = 25000;
// Replacement clients must fit inside the session's soft-reset gate budget
// (default server timeout 60 s): quit wait 3 s + grace 0.5 s + 3 attempts +
// 2 s backoff => 3 * 10 s spawn cap keeps the worst case below the gate.
const SOFT_RESET_SPAWN_TIMEOUT_MS = 10000;
const recentApi = [];
const recentLogs = [];
function recordApi(name, args) {
  try { recentApi.push({ name, args: args.slice(0, 4).map(summarizeArg), at: Date.now() }); if (recentApi.length > 40) recentApi.shift(); } catch {}
}
function summarizeArg(value) {
  if (value === null || value === undefined) return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : 'nonfinite';
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') return value.slice(0, 80);
  if (Array.isArray(value)) return value.slice(0, 4).map(summarizeArg);
  if (typeof value === 'object') {
    const out = { type: value.constructor?.name || 'object' };
    for (const key of ['x', 'y', 'z', 'name', 'id']) if (key in value) out[key] = summarizeArg(value[key]);
    return out;
  }
  return String(value).slice(0, 40);
}
function recordLog(level, message) {
  recentLogs.push({ level, message: String(message).slice(0, 200), at: Date.now() });
  if (recentLogs.length > 20) recentLogs.shift();
}
let currentScope;
const jobs = new Map();
const controlOwners = new Map();
let goalOwner = null;
const runDeadline = performance.now() + (config.runDeadlineAt ? Math.max(0, config.runDeadlineAt - Date.now()) : config.remainingRunMs ?? 129600000);
const makeScope = (id, jobId = null) => ({ id, jobId, actions: new Set(), timers: new Map(), listeners: [], closed: false, cancelled: false, functionDone: false, logCount: 0, logBytes: 0, logsSuppressed: 0, logsTruncated: 0 });
function cleanup(scope) {
  scope.closed = true;
  let controlsStopped = 0; let goalStopped = false;
  // Scope ownership prevents a foreground return from stopping an explicitly
  // registered task, or leaking its own controls merely because another task
  // exists. Run native cleanup outside the now-closed scope.
  scopes.run(undefined, () => {
    for (const [name, owner] of controlOwners) if (owner === scope) {
      try { bot.setControlState?.(name, false); controlsStopped++; } catch {}
      controlOwners.delete(name);
    }
    if (goalOwner === scope) { try { bot.pathfinder?.setGoal(null); goalStopped = true; } catch {} goalOwner = null; }
  });
  const summary = { logs_suppressed: scope.logsSuppressed, logs_truncated: scope.logsTruncated, log_bytes: scope.logBytes, timers_cleared: scope.timers.size, listeners_removed: scope.listeners.length, unfinished_actions: scope.actions.size, movement_controls_stopped: controlsStopped, pathfinder_goal_stopped: goalStopped };
  for (const [handle, stop] of scope.timers) stop(handle);
  for (const [emitter, event, listener] of scope.listeners) emitter.removeListener(event, listener);
  scope.timers.clear(); scope.listeners.length = 0; scope.actions.clear();
  return summary;
}
const errorValue = error => ({ name: String(error?.name ?? 'Error').slice(0, 128), message: String(error?.message ?? error).slice(0, 16384), stack: String(error?.stack ?? '').slice(0, 32768) });
function scopedLogger() {
  return Object.fromEntries(['log', 'info', 'warn', 'error', 'debug'].map(level => [level, (...values) => {
    const scope = scopes.getStore();
    if (!scope || scope.closed) return;
    if (pendingSends >= 48 || scope.logCount >= (config.maxLogs ?? 128) || scope.logBytes >= (config.maxLogBytes ?? 262144)) { scope.logsSuppressed++; return; }
    const text = values.slice(0, 32).map(value => typeof value === 'string' ? value.slice(0, 16384) : JSON.stringify(serialize(value, 4096))).join(' ');
    const limit = Math.min(16384, Math.max(0, (config.maxLogBytes ?? 262144) - scope.logBytes));
    let message = Buffer.from(text).subarray(0, limit).toString('utf8');
    while (Buffer.byteLength(message) > limit) message = message.slice(0, -1);
    if (Buffer.byteLength(text) > limit || values.length > 32) scope.logsTruncated++;
    scope.logBytes += Buffer.byteLength(message); scope.logCount++;
    if (scope === currentScope) recordLog(level, message);
    send({ type: 'log', execution_id: scope.id, job_id: scope.jobId, level, message });
  }]));
}
function finishJob(scope, status = 'succeeded', error = null) {
  if (scope.closed) return;
  const summary = cleanup(scope);
  clearInterval(scope.monitor);
  scope.status = status;
  send({ type: 'job_finished', job_id: scope.jobId, execution_id: scope.id, status, error, cleanup: summary, result: serialize(scope.result) });
  // Native actions cannot be proved stopped without recreating the client.
  // Escalate to the in-process soft reset instead of a hard worker exit.
  if (summary.unfinished_actions && !resetState) {
    softReset('unfinished_actions', { job_id: scope.jobId, unfinished_actions: summary.unfinished_actions });
  }
}
function checkJob(scope) {
  if (scope.closed || !scope.functionDone) return;
  if (scope.cancelled) {
    if (!scope.actions.size) finishJob(scope, scope.cancelStatus ?? 'cancelled');
  } else if (!scope.actions.size && !scope.timers.size && !scope.listeners.length) finishJob(scope, 'succeeded');
}
const jobAPI = Object.freeze({
  start(fn, options = {}) {
    const parent = scopes.getStore();
    if (config.lifecycle !== 'paper_aligned') throw new Error('Background jobs require paper_aligned lifecycle / protocol v3');
    if (!parent || parent.closed || parent.cancelled) throw new Error('Background registration requires an active execution');
    if (typeof fn !== 'function') throw new TypeError('jobs.start requires a function');
    const ttl = options.ttl_ms;
    if (!Number.isSafeInteger(ttl) || ttl < 1 || ttl > Math.floor(runDeadline - performance.now())) throw new RangeError('ttl_ms must be a positive integer within remaining run budget');
    if ([...jobs.values()].filter(scope => !scope.closed).length >= (config.maxBackgroundJobs ?? 8)) throw new Error('Background job limit reached');
    // Keep the local lookup bounded, independent of the parent durable journal.
    for (const [id, scope] of jobs) { if (jobs.size < 128 + (config.maxBackgroundJobs ?? 8)) break; if (scope.closed) jobs.delete(id); }
    const id = randomUUID();
    const scope = makeScope(parent.id, id); scope.status = 'running'; scope.started_at = new Date().toISOString(); scope.ttl_ms = ttl;
    jobs.set(id, scope);
    send({ type: 'job_started', job_id: id, execution_id: parent.id, ttl_ms: ttl });
    scope.monitor = setInterval(() => checkJob(scope), 25); scope.monitor.unref();
    scopes.run(scope, () => Promise.resolve().then(() => fn({ isCancelled: () => scope.cancelled || scope.closed })).then(result => {
      scope.result = result; scope.functionDone = true; checkJob(scope);
    }, error => { scope.functionDone = true; finishJob(scope, scope.cancelled ? 'cancelled' : 'failed', errorValue(error)); }));
    return id;
  },
  status(id) {
    const scope = jobs.get(id); if (!scope) return null;
    return { job_id: id, execution_id: scope.id, status: scope.status, ttl_ms: scope.ttl_ms, started_at: scope.started_at };
  },
  cancel(id) {
    const scope = jobs.get(id); if (!scope) throw new Error('Background job not found');
    if (!scope.closed) { scope.cancelled = true; scope.status = 'cancelling'; send({ type: 'job_cancel_request', job_id: id }); checkJob(scope); }
    return jobAPI.status(id);
  },
  list() { return [...jobs.keys()].map(id => jobAPI.status(id)); },
});

function serialize(value, maxBytes = config.maxResultBytes ?? 262144) { return boundedValue(value, maxBytes).value; }

function trackPromise(scope, value) {
  if (!scope || !value || typeof value.then !== 'function') return value;
  scope.actions.add(value);
  Promise.resolve(value).then(() => scope.actions.delete(value), () => scope.actions.delete(value));
  return value;
}

const instrumented = new WeakMap();
const containerMethods = ['withdraw', 'deposit', 'close', 'putInput', 'putFuel', 'takeOutput', 'takeInput', 'takeFuel', 'enchant', 'combine', 'trade'];

function instrumentMethod(target, name) {
  const original = target?.[name];
  if (typeof original !== 'function') return;
  const names = instrumented.get(target) ?? new Set();
  if (names.has(name)) return;
  names.add(name); instrumented.set(target, names);
  target[name] = function (...args) {
    const scope = scopes.getStore();
    if (scope?.closed) throw new Error('Execution has ended');
    if (scope && (target === bot || (bot && target === bot.pathfinder))) recordApi(name, args);
    if (target === bot && name === 'setControlState') {
      if (args[1]) controlOwners.set(args[0], scope); else controlOwners.delete(args[0]);
    } else if (target === bot && name === 'clearControlStates') controlOwners.clear();
    else if (target === bot?.pathfinder && name === 'setGoal') goalOwner = args[0] ? scope : null;
    const result = original.apply(this, args);
    if (result && typeof result.then === 'function') {
      Promise.resolve(result).then(value => {
        if (value && typeof value === 'object') for (const method of containerMethods) instrumentMethod(value, method);
      }, () => {});
    }
    return trackPromise(scope, result);
  };
}

function instrumentEmitter(target) {
  const on = target.on;
  for (const method of ['on', 'addListener', 'once', 'prependListener', 'prependOnceListener']) {
    const original = target[method];
    target[method] = function (event, listener) {
      const scope = scopes.getStore();
      if (scope?.closed) throw new Error('Execution has ended');
      if (!scope) return original.call(this, event, listener);
      const wrapped = (...args) => scopes.run(scope, () => {
        if (!scope.closed) return trackPromise(scope, listener.apply(this, args));
      });
      // Register once explicitly to avoid registering wrapper internals twice.
      if (method.includes('Once') || method === 'once') {
        const one = (...args) => { this.removeListener(event, one); scope.listeners = scope.listeners.filter(entry => entry[2] !== one); return wrapped(...args); };
        one.listener = listener;
        scope.listeners.push([this, event, one]);
        return on.call(this, event, one);
      }
      wrapped.listener = listener;
      scope.listeners.push([this, event, wrapped]);
      return original.call(this, event, wrapped);
    };
  }
}

function entitySnapshot(entity) {
  if (!entity || typeof entity !== 'object') return entity;
  const clone = Object.create(Object.getPrototypeOf(entity));
  Object.assign(clone, entity);
  for (const key of ['position', 'velocity']) {
    const vector = entity[key];
    if (vector && typeof vector === 'object') {
      try { clone[key] = new vector.constructor(vector.x, vector.y, vector.z); }
      catch { clone[key] = { x: vector.x, y: vector.y, z: vector.z }; }
    }
  }
  return clone;
}

function snapshotEntities(entities) {
  if (!entities || typeof entities !== 'object') return entities;
  const out = {};
  for (const [key, entity] of Object.entries(entities)) out[key] = entitySnapshot(entity);
  return out;
}

function snapshotPlayers(players) {
  if (!players || typeof players !== 'object') return players;
  const out = {};
  for (const [key, player] of Object.entries(players)) {
    out[key] = { ...player, entity: entitySnapshot(player && player.entity) };
  }
  return out;
}

function finitePoint(point) {
  return point && Number.isFinite(point.x) && Number.isFinite(point.y) && Number.isFinite(point.z);
}

function makeAgentFacingBot(getReal) {
  // `bot._client` exposes the raw protocol object.  Model-generated programs
  // have used it to monkey-patch sock.write/splitter.push and to change the
  // compression threshold, which corrupts the Minecraft stream for the whole
  // server (DecoderException/kicks).  Keep the real bot for the worker and hand
  // the program a proxied view without raw protocol internals.
  //
  // The target is resolved through `getReal()` on every access so the proxy is
  // stable across in-process soft resets: an execution gets the same `bot`
  // object while the underlying Mineflayer client is replaced underneath.
  let pathfinderTarget = null;
  let pathfinderProxy = null;
  const hidden = new Set(['_client', 'client', 'creative', '_events', '_socket', 'physics']);
  const readonly = new Set(['physicsEnabled']);
  const current = () => {
    try { return typeof getReal === 'function' ? getReal() : getReal; } catch { return null; }
  };
  const bound = (target, value) => typeof value === 'function' ? value.bind(target) : value;
  const proxy = new Proxy({}, {
    get(_target, prop) {
      const real = current();
      if (!real) return undefined;
      if (hidden.has(prop)) return undefined;
      if (prop === 'entity') return entitySnapshot(real.entity);
      if (prop === 'entities') return snapshotEntities(real.entities);
      if (prop === 'players') return snapshotPlayers(real.players);
      if (prop === 'look') return (yaw, pitch, force) => {
        if (!Number.isFinite(yaw) || !Number.isFinite(pitch)) throw new TypeError('look requires finite yaw/pitch');
        return real.look(yaw, pitch, force);
      };
      if (prop === 'lookAt') return (point, force) => {
        if (!finitePoint(point)) throw new TypeError('lookAt requires a finite point');
        return real.lookAt(point, force);
      };
      if (prop === 'pathfinder' && real.pathfinder) {
        if (pathfinderTarget !== real.pathfinder) {
          pathfinderTarget = real.pathfinder;
          pathfinderProxy = new Proxy(real.pathfinder, {
            get(pt, p) {
              if (p === 'bot') return proxy;
              if (p === 'setGoal' || p === 'goto') return (...args) => {
                const goal = args[0];
                if (goal && ('x' in goal || 'y' in goal || 'z' in goal) && !finitePoint(goal)) throw new TypeError('pathfinder goal requires finite coordinates');
                return pt[p](...args);
              };
              return bound(pt, Reflect.get(pt, p, pt));
            },
            set(pt, p, value) { if (p === 'bot') return false; pt[p] = value; return true; },
          });
        }
        return pathfinderProxy;
      }
      return bound(real, Reflect.get(real, prop, real));
    },
    set(_target, prop, value) {
      const real = current();
      if (!real) return true;
      if (hidden.has(prop) || readonly.has(prop) || prop === 'entity') return false;
      real[prop] = value;
      return true;
    },
    has(_target, prop) {
      const real = current();
      if (!real) return false;
      return hidden.has(prop) ? false : Reflect.has(real, prop);
    },
    // Keep the agent-visible object shape identical to the previous
    // Proxy(real, ...) implementation even though the target is now a stable
    // placeholder (the real client is resolved per access through the getter).
    ownKeys(_target) {
      const real = current();
      if (!real) return [];
      try { return Reflect.ownKeys(real); } catch { return []; }
    },
    getOwnPropertyDescriptor(_target, prop) {
      const real = current();
      if (!real) return undefined;
      try {
        const descriptor = Reflect.getOwnPropertyDescriptor(real, prop);
        if (!descriptor) return undefined;
        return { ...descriptor, configurable: true };
      } catch { return undefined; }
    },
    getPrototypeOf(_target) {
      const real = current();
      try { return real ? Object.getPrototypeOf(real) : Object.prototype; } catch { return Object.prototype; }
    },
    deleteProperty(_target, prop) {
      const real = current();
      if (!real) return true;
      try { return Reflect.deleteProperty(real, prop); } catch { return false; }
    },
    defineProperty(_target, prop, descriptor) {
      const real = current();
      if (!real) return false;
      try { return Reflect.defineProperty(real, prop, descriptor); } catch { return false; }
    },
  });
  return proxy;
}

function restoreEntityPhysicsState(entity, state) {
  if (!entity || !state) return false;
  try {
    const position = entity.position;
    if (position && typeof position.set === 'function') position.set(state.pos.x, state.pos.y, state.pos.z);
    else if (position) { position.x = state.pos.x; position.y = state.pos.y; position.z = state.pos.z; }
    const velocity = entity.velocity;
    if (velocity && typeof velocity.set === 'function') velocity.set(0, 0, 0);
    else if (velocity) { velocity.x = 0; velocity.y = 0; velocity.z = 0; }
    entity.yaw = state.yaw;
    entity.pitch = state.pitch;
    return true;
  } catch {
    return false;
  }
}

function safeSnapshot() {
  try { return snapshot(); } catch { return null; }
}

function boundedDetail(detail) {
  try { return detail === null || detail === undefined ? null : serialize(detail); } catch { return null; }
}

function stderrEvidence(kind, payload) {
  // Bounded single-line JSON-ish forensics; never throws and never blocks.
  try {
    const text = kind + ' ' + JSON.stringify(payload ?? null);
    process.stderr.write(String(text).slice(0, 1800) + '\n');
  } catch { /* evidence is best effort */ }
}

function sleep(ms) {
  return new Promise(resolve => { setTimeout(resolve, Math.max(0, Number(ms) || 0)); });
}

function fatalExit(message, extra = {}) {
  fatalExitScheduled = true;
  try { stderrEvidence('WORKER_FATAL', { error: String(message ?? 'worker fatal').slice(0, 2000), ...extra }); } catch {}
  try { send({ type: 'fatal', error: String(message ?? 'worker fatal').slice(0, 32768), ...extra }); } catch {}
  // Give process.send a tick to flush before the hard restart.
  setTimeout(() => process.exit(1), 100);
}

function softSnapshot() {
  try {
    return serialize({
      mode: config.mode,
      username: bot?.username ?? config.username ?? null,
      position: bot?.entity?.position ?? null,
      health: bot?.health ?? null,
      food: bot?.food ?? null,
      globals_preserved: true,
    });
  } catch {
    return { mode: config.mode, globals_preserved: true };
  }
}

function waitForBotEnd(client, timeoutMs) {
  return new Promise(resolve => {
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { client?.removeListener?.('end', done); } catch {}
      try { client?._client?.socket?.removeListener?.('close', done); } catch {}
      resolve();
    };
    const timer = setTimeout(done, Math.max(0, Number(timeoutMs) || 0));
    timer.unref?.();
    try { client?.once?.('end', done); } catch {}
    try {
      if (client?._client?.ended) { done(); return; }
      client?._client?.socket?.once?.('close', done);
    } catch { /* socket may be gone already */ }
  });
}

let mineflayerModules = null;
function loadMineflayer() {
  if (!mineflayerModules) {
    mineflayerModules = {
      mineflayer: nativeRequire('mineflayer'),
      pathfinder: nativeRequire('mineflayer-pathfinder').pathfinder,
    };
  }
  return mineflayerModules;
}

function waitForSpawn(client, timeoutMs = SPAWN_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      clearTimeout(timer);
      try {
        client.removeListener('spawn', onSpawn);
        client.removeListener('error', onError);
        client.removeListener('kicked', onKicked);
        client.removeListener('end', onEnd);
      } catch {}
    };
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      callback(value);
    };
    const onSpawn = () => finish(resolve);
    const onError = error => finish(reject, error);
    const onKicked = reason => finish(reject, new Error('Kicked: ' + String(reason)));
    const onEnd = reason => finish(reject, new Error('Connection ended before spawn: ' + String(reason)));
    const timer = setTimeout(
      () => finish(reject, new Error('Bot spawn timed out after ' + timeoutMs + ' ms')),
      timeoutMs);
    client.once('spawn', onSpawn);
    client.once('error', onError);
    client.once('kicked', onKicked);
    client.once('end', onEnd);
  });
}

async function createBotClient({ timeoutMs = SPAWN_TIMEOUT_MS } = {}) {
  if (config.mode === 'fake') return makeFakeBot();
  const { mineflayer, pathfinder } = loadMineflayer();
  let client = null;
  try {
    client = mineflayer.createBot({
      host: config.host, port: config.port, username: config.username,
      version: config.version, auth: 'offline', hideErrors: true,
    });
    client.loadPlugin(pathfinder);
    await waitForSpawn(client, timeoutMs);
    return client;
  } catch (error) {
    // The identity proxy admits one live connection per username. A client that
    // failed to spawn still holds that login, so destroy it here (startBot's
    // catch never sees it because createBotClient never returned it).
    try { client?.quit?.(); } catch {}
    try { client?._client?.socket?.destroy?.(); } catch {}
    throw error;
  }
}

function installBot(client) {
  botGeneration += 1;
  bot = client;
  if (physicsGuard) { try { physicsGuard.uninstall(); } catch {} physicsGuard = null; }
  try {
    const stallConfig = Number(config.physicsStallMs);
    physicsGuard = installPhysicsGuard(client, {
      stallMs: Number.isFinite(stallConfig) && stallConfig > 0 ? stallConfig : undefined,
      isReady: () => botReady,
      recentApi: () => recentApi,
      onPrevented: payload => {
        if (preventedLogs >= 5) return;
        preventedLogs += 1;
        stderrEvidence('PHYSICS_NONFINITE_PREVENTED', {
          reason: 'nonfinite_bot_state',
          execution_id: currentScope?.id ?? null,
          bad_fields: payload.badFields,
          restored: payload.restored,
          recent_api: (payload.recentApi ?? []).slice(-5),
        });
      },
      onRepeated: stats => {
        stderrEvidence('PHYSICS_NONFINITE_REPEATED', {
          reason: 'nonfinite_bot_state',
          execution_id: currentScope?.id ?? null,
          bad_fields: stats.badFields,
          consecutive: stats.consecutive,
          in_window: stats.inWindow,
          recent_api: (stats.recentApi ?? []).slice(-5),
        });
        if (!resetState) softReset('nonfinite_bot_state', {
          source: 'physics_guard',
          bad_fields: stats.badFields,
          recent_api: (stats.recentApi ?? []).slice(-10),
        });
      },
      onStalled: payload => {
        stderrEvidence('PHYSICS_STALLED', {
          reason: 'physics_stalled',
          execution_id: currentScope?.id ?? null,
          ms_since_tick: payload.msSinceTick,
          ms_since_spawn: payload.msSinceSpawn,
          last_physics_tick_at: payload.lastPhysicsTickAt,
        });
        if (!resetState) softReset('physics_stalled', {
          ms_since_tick: payload.msSinceTick,
          ms_since_spawn: payload.msSinceSpawn,
          last_physics_tick_at: payload.lastPhysicsTickAt,
        });
      },
    });
  } catch (error) {
    stderrEvidence('PHYSICS_GUARD_FAILED', { error: String(error?.message ?? error).slice(0, 500) });
  }
  const emitterMethods = new Set(['on', 'once', 'addListener', 'prependListener', 'prependOnceListener', 'off', 'removeListener', 'removeAllListeners', 'emit']);
  try {
    for (const name of Object.keys(client)) if (!emitterMethods.has(name)) instrumentMethod(client, name);
    if (client.pathfinder) {
      instrumentMethod(client.pathfinder, 'goto');
      instrumentMethod(client.pathfinder, 'setGoal');
    }
  } catch (error) {
    stderrEvidence('BOT_INSTRUMENT_FAILED', { error: String(error?.message ?? error).slice(0, 500) });
  }
  for (const event of ['chat', 'whisper', 'death', 'respawn', 'kicked', 'end', 'error']) {
    client.on(event, (...args) => {
      const encoded = boundedValue(args, 32768);
      send({ type: 'event', kind: event, payload: encoded.value, payload_truncated: encoded.truncated });
    });
  }
  client.on('end', () => {
    if (intentionalBotQuit || resetState) return;
    softReset('bot_disconnected', { reason: 'bot_end' });
  });
  client.on('kicked', reason => {
    if (intentionalBotQuit || resetState) return;
    softReset('bot_disconnected', { reason: 'kicked', detail: String(reason ?? '').slice(0, 500) });
  });
  try { instrumentEmitter(client); } catch {}
  if (viewer) { try { viewer.attach(client); } catch (error) { stderrEvidence('VIEWER_ATTACH_FAILED', { error: String(error?.message ?? error).slice(0, 300) }); } }
  if (config.chatDelivery === 'program_output') {
    client.on('chat', (username, message) => {
      pendingChat.push({ username: String(username ?? '').slice(0, 64), message: String(message ?? '').slice(0, 4096), received_at: new Date().toISOString() });
      while (pendingChat.length > 100) pendingChat.shift();
    });
  }
  return client;
}

async function startBot({ initial = false, reason = null } = {}) {
  const startedAt = Date.now();
  let client = null;
  try {
    client = await createBotClient({ timeoutMs: initial ? SPAWN_TIMEOUT_MS : SOFT_RESET_SPAWN_TIMEOUT_MS });
    installBot(client);
    botReady = true;
  if (initial) {
    try { setupViewer(); } catch (error) {
      stderrEvidence('VIEWER_SETUP_FAILED', { error: String(error?.stack ?? error).slice(0, 800) });
    }
    send({
      type: 'ready',
      snapshot: snapshot(),
      mode: config.mode,
      generation: botGeneration,
      module_policy: modulePolicy(config.lifecycle),
      sandbox: {
        native_filter: process.env.HARNESS_NATIVE_SANDBOX === 'seccomp-v1',
        version: process.env.HARNESS_NATIVE_SANDBOX === 'seccomp-v1' ? 1 : null,
      },
    });
    drainPendingExecutions();
    return client;
  }
  softResetCount += 1;
  const durationMs = Date.now() - startedAt;
  stderrEvidence('SOFT_RESET_READY', {
    reason,
    generation: botGeneration,
    duration_ms: durationMs,
    soft_resets: softResetCount,
    snapshot_position: bot?.entity?.position ?? null,
  });
  send({
    type: 'soft_ready',
    reason,
    generation: botGeneration,
    snapshot: softSnapshot(),
    duration_ms: durationMs,
    soft_resets: softResetCount,
  });
  drainPendingExecutions();
  return client;
  } catch (error) {
    // A replacement client that failed after createBot() must be destroyed:
    // the identity proxy admits one live connection per username, so a leaked
    // login would make every subsequent retry fail with socketClosed.
    try { client?.quit?.(); } catch {}
    try { client?._client?.socket?.destroy?.(); } catch {}
    botReady = false;
    throw error;
  }
}

function softReset(reason, detail = null) {
  if (resetState) return resetState.promise;
  const decision = rateLimitDecision(softResetTimes, {
    limit: SOFT_RESET_RATE_LIMIT,
    windowMs: SOFT_RESET_RATE_WINDOW_MS,
  });
  if (!decision.allowed) {
    fatalExit('Soft reset rate limit exceeded (' + SOFT_RESET_RATE_LIMIT + ' in '
      + Math.round(SOFT_RESET_RATE_WINDOW_MS / 1000) + 's): ' + String(reason), { reason });
    const rejected = Promise.resolve(null);
    resetState = { promise: rejected, reason };
    rejected.then(() => { if (resetState?.promise === rejected) resetState = null; });
    return rejected;
  }
  softResetTimes.length = 0;
  softResetTimes.push(...decision.times);
  // The reset may be triggered from a program/AsyncLocalStorage context whose
  // scope is already closed. Run the recovery outside that store: internal
  // Mineflayer timers created from a closed scope would otherwise get an
  // "Execution has ended" fence on every instrumented bot call, and a partially
  // created replacement client would leak its login and block the retries.
  const promise = scopes.run(undefined, () => performSoftReset(reason, detail)).catch(error => {
    stderrEvidence('SOFT_RESET_UNEXPECTED_ERROR', { reason, error: String(error?.stack ?? error).slice(0, 2000) });
    try { send({ type: 'soft_reset_failed', reason, error: String(error?.stack ?? error).slice(0, 4096) }); } catch {}
    fatalExit('Soft reset failed: ' + String(error?.message ?? error), { soft_reset_failed: true, reason });
    return null;
  });
  resetState = { promise, reason };
  const settle = () => {
    if (resetState?.promise === promise) resetState = null;
    // `performSoftReset`/`startBot` send soft_ready and try to drain while
    // resetState is still held, so that drain is a no-op. Drain again now that
    // the single-flight gate is actually clear; otherwise the next execute
    // (especially the server's post-soft_ready dispatch) would stay queued
    // forever. Fatal paths exit and must not start new work.
    if (!fatalExitScheduled) drainPendingExecutions();
  };
  promise.then(settle, settle);
  return promise;
}

async function performSoftReset(reason, detail) {
  const generation = botGeneration + 1;
  const scope = currentScope;
  const executionId = scope?.id ?? null;
  botReady = false;
  stderrEvidence('SOFT_RESET_START', {
    reason,
    generation,
    execution_id: executionId,
    detail: boundedDetail(detail),
  });
  // 4. Complete the in-flight foreground execution exactly once, before the
  //    soft_reset message, so the server can release or fail it deterministically.
  if (scope && !scope.completed) {
    const error = {
      name: 'SoftResetError',
      message: 'Bot client reset: ' + reason,
      stack: new Error('Bot client reset: ' + reason).stack,
    };
    completeScope(scope, null, error, false, { resetReason: 'soft_reset', forceNeedsReset: false });
  }
  // 5. Close every retained leftover scope (paper leftover_policy allow) and
  //    background job so no program callback can touch the old generation after
  //    the bump. Cleanup keeps the exact "Execution has ended" fencing: closed
  //    scopes make instrumented methods throw.
  for (const leftover of [...leftoverScopes]) {
    try { cleanup(leftover); } catch {}
    leftoverScopes.delete(leftover);
  }
  for (const job of jobs.values()) {
    if (job.closed) continue;
    try {
      const summary = cleanup(job);
      clearInterval(job.monitor);
      job.closed = true;
      job.status = 'failed';
      send({
        type: 'job_finished',
        job_id: job.jobId,
        execution_id: job.id,
        status: 'failed',
        error: { name: 'SoftResetError', message: 'Bot client reset: ' + reason, stack: '' },
        cleanup: summary,
        result: null,
      });
    } catch {}
  }
  controlOwners.clear();
  goalOwner = null;
  // 6. Quiesce native actions on the old client. Run outside the (now closed)
  //    AsyncLocalStorage scope so the instrumented pathfinder/bot methods do not
  //    reject with "Execution has ended" just because a program scope was active;
  //    native shutdown must still reach the real client.
  try {
    const quiesce = scopes.run(undefined, () => quiesceNativeActions(bot));
    stderrEvidence('SOFT_RESET_QUIESCE', { reason, quiesce });
  } catch {}
  // 7. Announce the reset (server gates its queue on this message).
  send({
    type: 'soft_reset',
    reason,
    generation,
    execution_id: executionId,
    detail: boundedDetail(detail),
  });
  // 8. Disconnect the old client; the identity proxy needs it active=false
  //    before a new login can be admitted.
  const oldBot = bot;
  intentionalBotQuit = true;
  if (oldBot) {
    try { oldBot.quit?.(); } catch {}
    await waitForBotEnd(oldBot, 3000);
    try { oldBot._client?.socket?.destroy?.(); } catch {}
  }
  try { physicsGuard?.uninstall(); } catch {}
  physicsGuard = null;
  bot = null;
  botReady = false;
  intentionalBotQuit = false;
  // Give the identity proxy's single-active-connection slot time to observe the
  // old socket close before the login is retried; otherwise the replacement
  // login is rejected as a duplicate and looks like an immediate socket close.
  await sleep(SOFT_RESET_LOGIN_GRACE_MS);
  // 9. Create the replacement client with bounded retries/backoff.
  let lastError = null;
  for (let attempt = 1; attempt <= SOFT_RESET_ATTEMPTS; attempt += 1) {
    try {
      await startBot({ initial: false, reason });
      return;
    } catch (error) {
      lastError = error;
      stderrEvidence('SOFT_RESET_ATTEMPT_FAILED', {
        reason,
        attempt,
        error: String(error?.message ?? error).slice(0, 400),
      });
      if (attempt < SOFT_RESET_ATTEMPTS) await sleep(SOFT_RESET_RETRY_BACKOFF_MS);
    }
  }
  // 10. Failure is a hard restart: tell the server, then exit.
  stderrEvidence('SOFT_RESET_FAILED', { reason, error: String(lastError?.stack ?? lastError).slice(0, 2000) });
  try { send({ type: 'soft_reset_failed', reason, error: String(lastError?.stack ?? lastError).slice(0, 4096) }); } catch {}
  fatalExit('Soft reset failed: ' + String(lastError?.message ?? lastError), {
    soft_reset_failed: true,
    reason,
  });
}

function failQueuedExecution(message, reason, name = 'WorkerBusyError') {
  if (!message || !message.execution_id) return;
  try {
    send({
      type: 'completed',
      execution_id: message.execution_id,
      result: null,
      error: { name, message: String(reason).slice(0, 500), stack: '' },
      output_truncated: false,
      before: null,
      after: null,
      timed_out: false,
      needs_reset: false,
      reset_reason: 'worker_reset',
      cleanup: null,
    });
  } catch {}
}

function queueExecution(message) {
  pendingExecutions.push(message);
  while (pendingExecutions.length > PENDING_EXECUTION_LIMIT) {
    failQueuedExecution(pendingExecutions.shift(), 'Worker busy during bot reset');
  }
}

function drainPendingExecutions() {
  if (resetState || !botReady || currentScope) return;
  const next = pendingExecutions.shift();
  if (!next) return;
  execute(next).catch(error => fatalExit('Queued execution failed: ' + String(error?.stack ?? error)));
}

function handleAsyncFatal(error, source) {
  const name = String(error?.name ?? error?.constructor?.name ?? 'Error');
  stderrEvidence(source, {
    name,
    message: String(error?.message ?? error).slice(0, 500),
    execution_id: currentScope?.id ?? null,
  });
  // A reset already owns recovery; swallowing avoids a reset->exception->reset
  // recursion (the in-flight reset will either recover or hard-fail).
  if (resetState) return;
  softReset('uncaught:' + name, {
    source,
    message: String(error?.message ?? error).slice(0, 2000),
    stack: String(error?.stack ?? '').slice(0, 4000),
  });
}

function setupViewer() {
  if (viewerSetupDone || config.mode !== 'minecraft') return;
  const viewerPort = Number(process.env.VIEWER_PORT ?? '');
  if (!(Number.isInteger(viewerPort) && viewerPort > 0 && viewerPort < 65536)) return;
  viewerSetupDone = true;
  const bind = process.env.VIEWER_BIND || '127.0.0.1';
  const viewDistance = Math.max(1, Math.min(8, Number(process.env.VIEWER_VIEW_DISTANCE ?? '2') || 2));
  const firstPerson = (process.env.VIEWER_FIRST_PERSON ?? 'true') !== 'false';
  const viewerScript = fileURLToPath(new URL('./viewer-child.cjs', import.meta.url));
  const viewerChunks = new Map();
  let viewerThread = null;
  let viewerStopping = false;
  let viewerRestarts = 0;
  let viewerLastHeartbeat = Date.now();
  let viewerRestartTimer = null;
  let viewerPositionAt = 0;
  let viewerVersion = null;
  let repaintTimer = null;
  const viewerEntityAt = new Map();

  function viewerSend(message) {
    if (!viewerThread) return;
    try { viewerThread.postMessage(message); } catch { /* restart owns recovery */ }
  }
  function viewerSnapshot() {
    const chunks = [];
    for (const [key, json] of viewerChunks) {
      const [x, z] = key.split(',').map(Number);
      chunks.push({ x, z, json });
    }
    viewerSend({ type: 'chunks', chunks });
    viewerPosition(true);
  }
  function viewerChunk(pos) {
    try {
      const column = bot?.world?.getColumnAt(pos);
      if (!column || typeof column.toJson !== 'function') return;
      const x = Math.floor(pos.x / 16), z = Math.floor(pos.z / 16);
      const json = column.toJson();
      viewerChunks.set(`${x},${z}`, json);
      if (viewerChunks.size > 1024) viewerChunks.delete(viewerChunks.keys().next().value);
      viewerSend({ type: 'chunk', x, z, json });
    } catch (error) { process.stderr.write(`viewer chunk capture failed: ${String(error?.message ?? error).slice(0, 300)}\n`); }
  }
  function viewerAllChunks() {
    try {
      const columns = bot?.world?.columns || {};
      for (const [key, column] of Object.entries(columns)) {
        if (!column || typeof column.toJson !== 'function') continue;
        const [x, z] = String(key).split(',').map(Number);
        if (!Number.isInteger(x) || !Number.isInteger(z)) continue;
        viewerChunks.set(`${x},${z}`, column.toJson());
      }
    } catch (error) { process.stderr.write(`viewer snapshot failed: ${String(error?.message ?? error).slice(0, 300)}\n`); }
  }
  function viewerPosition(force = false) {
    const now = Date.now();
    if (!force && now - viewerPositionAt < 100) return;
    const position = bot?.entity?.position;
    if (!position) return;
    viewerPositionAt = now;
    viewerSend({ type: 'position', pos: { x: position.x, y: position.y, z: position.z }, yaw: bot.entity.yaw, pitch: bot.entity.pitch });
  }
  function viewerEntity(entity, force = false) {
    if (!entity || entity === bot?.entity) return;
    const now = Date.now();
    if (!force && now - (viewerEntityAt.get(entity.id) || 0) < 150) return;
    viewerEntityAt.set(entity.id, now);
    if (viewerEntityAt.size > 256) viewerEntityAt.delete(viewerEntityAt.keys().next().value);
    viewerSend({ type: 'entity', entity: { id: entity.id, name: entity.name, pos: { x: entity.position.x, y: entity.position.y, z: entity.position.z }, width: entity.width, height: entity.height, username: entity.username } });
  }
  function viewerBlockUpdate(oldBlock, newBlock) {
    if (!newBlock || !newBlock.position) return;
    const stateId = Number.isInteger(newBlock.stateId) ? newBlock.stateId : ((newBlock.type << 4) | (newBlock.metadata || 0));
    viewerSend({ type: 'blockUpdate', block: { pos: { x: newBlock.position.x, y: newBlock.position.y, z: newBlock.position.z }, stateId } });
  }
  function viewerStart() {
    if (viewerStopping) return;
    if (viewerRestartTimer) { clearTimeout(viewerRestartTimer); viewerRestartTimer = null; }
    viewerVersion = bot?.version ?? viewerVersion;
    const config = { port: viewerPort, bind, viewDistance, firstPerson, version: viewerVersion };
    let thread;
    try {
      thread = new Worker(viewerScript, {
        workerData: config,
        resourceLimits: { maxOldGenerationSizeMb: 384, maxYoungGenerationSizeMb: 64 },
      });
    } catch (error) {
      process.stderr.write(`viewer thread spawn failed: ${String(error?.message ?? error).slice(0, 500)}\n`);
      viewerScheduleRestart();
      return;
    }
    viewerThread = thread;
    viewerLastHeartbeat = Date.now();
    thread.on('message', (message) => {
      if (!message || typeof message !== 'object') return;
      if (message.type === 'viewer_heartbeat') { viewerLastHeartbeat = Date.now(); return; }
      if (message.type === 'viewer_ready') {
        viewerAllChunks();
        viewerSnapshot();
        send({ type: 'viewer_started', port: viewerPort, bind, first_person: firstPerson, isolated: true, restarts: viewerRestarts });
        return;
      }
      if (message.type === 'viewer_fatal' || message.type === 'viewer_warning') {
        process.stderr.write(`viewer ${message.type}: ${String(message.error).slice(0, 800)}\n`);
      }
    });
    thread.on('error', (error) => {
      process.stderr.write(`viewer thread error: ${String(error?.stack ?? error).slice(0, 1000)}\n`);
    });
    thread.on('exit', (code) => {
      if (viewerThread === thread) viewerThread = null;
      if (viewerStopping) return;
      process.stderr.write(`viewer thread exited code=${code}; restarting\n`);
      viewerScheduleRestart();
    });
  }
  function viewerScheduleRestart() {
    if (viewerStopping || viewerRestartTimer) return;
    viewerRestarts++;
    const delay = Math.min(5000, 500 + viewerRestarts * 250);
    viewerRestartTimer = setTimeout(() => { viewerRestartTimer = null; viewerStart(); }, delay);
  }
  function viewerStop() {
    viewerStopping = true;
    if (viewerRestartTimer) { clearTimeout(viewerRestartTimer); viewerRestartTimer = null; }
    if (repaintTimer) { clearTimeout(repaintTimer); repaintTimer = null; }
    const thread = viewerThread; viewerThread = null;
    if (thread) {
      try { thread.postMessage({ type: 'shutdown' }); } catch {}
      const killTimer = setTimeout(() => { thread.terminate().catch(() => {}); }, 500);
      killTimer.unref?.();
      thread.once('exit', () => clearTimeout(killTimer));
    }
  }
  function scheduleRepaint(delayMs = 50) {
    if (repaintTimer) return;
    repaintTimer = setTimeout(() => {
      repaintTimer = null;
      try { viewerAllChunks(); viewerSnapshot(); } catch {}
    }, delayMs);
    repaintTimer.unref?.();
  }
  function attach(client) {
    // A new bot generation replaces the connection under the same viewer
    // thread: drop the stale chunk cache and re-register all hooks so the
    // existing viewer repaints from the new client. The thread itself is only
    // restarted on exit/heartbeat-timeout, not on a soft reset.
    viewerChunks.clear();
    viewerEntityAt.clear();
    const previousVersion = viewerVersion;
    viewerVersion = client?.version ?? viewerVersion;
    // The pinned MC version never changes across generations, but a version
    // change would need a fresh viewer thread (its workerData is immutable).
    if (previousVersion && viewerVersion && previousVersion !== viewerVersion && viewerThread) {
      try { viewerThread.terminate().catch(() => {}); } catch {}
    }
    client.on('chunkColumnLoad', viewerChunk);
    client.on('move', () => viewerPosition());
    client.on('entitySpawn', (entity) => viewerEntity(entity, true));
    client.on('entityMoved', viewerEntity);
    client.on('entityGone', (entity) => viewerSend({ type: 'entity', entity: { id: entity.id, delete: true } }));
    client.on('blockUpdate', viewerBlockUpdate);
    // The new client is already spawned when the worker installs it; request a
    // fresh repaint shortly after chunks start decoding.
    scheduleRepaint(50);
    client.once('chunkColumnLoad', () => scheduleRepaint(25));
  }
  viewer = { attach, stop: viewerStop };
  // The initial bot was installed before the viewer existed, so attach it here;
  // later generations are attached from installBot(). A generation's listeners
  // live on its own client object, so a soft reset never double-registers them.
  try { attach(bot); } catch (error) {
    stderrEvidence('VIEWER_ATTACH_FAILED', { error: String(error?.message ?? error).slice(0, 300) });
  }
  process.on('exit', viewerStop);
  process.on('SIGTERM', () => { viewerStop(); process.exit(0); });
  const viewerPositionTimer = setInterval(() => viewerPosition(), 250);
  viewerPositionTimer.unref?.();
  const viewerHealthTimer = setInterval(() => {
    if (!viewerThread || viewerStopping) return;
    if (Date.now() - viewerLastHeartbeat > 15000) {
      process.stderr.write('viewer heartbeat timeout; restarting thread\n');
      viewerThread.terminate().catch(() => {});
    }
  }, 5000);
  viewerHealthTimer.unref?.();
  viewerStart();
}

function startBotStateGuard() {
  // One 500 ms guard interval for the whole process (never per generation).
  // It reads the current global bot and resets per-generation recovery state
  // whenever the client generation changes. It tries restoring the last finite
  // state first and only escalates to an in-process soft reset when the state
  // stays nonfinite for 3 checks (~1.5 s).
  if (config.mode !== 'minecraft') return;
  let hadFinite = false;
  let lastGoodState = null;
  let nonfiniteChecks = 0;
  let guardGeneration = botGeneration;
  let evidenceLogged = 0;
  const timer = setInterval(() => {
    try {
      if (guardGeneration !== botGeneration) {
        guardGeneration = botGeneration;
        hadFinite = false;
        lastGoodState = null;
        nonfiniteChecks = 0;
      }
      const entity = bot && bot.entity;
      const position = entity && entity.position;
      if (!position) return;
      const state = physicsClientState(entity);
      if (physicsStateIsFinite(state)) {
        hadFinite = true;
        lastGoodState = state;
        nonfiniteChecks = 0;
        return;
      }
      if (!hadFinite) return;
      nonfiniteChecks += 1;
      if (evidenceLogged < 5) {
        evidenceLogged += 1;
        stderrEvidence('NONFINITE_BOT_STATE', {
          event: 'NONFINITE_BOT_STATE',
          agent: config.username,
          execution_id: currentScope?.id ?? null,
          bad_fields: physicsNonfiniteFields(state),
          position: { x: summarizeArg(position.x), y: summarizeArg(position.y), z: summarizeArg(position.z) },
          yaw_pitch: [summarizeArg(entity.yaw), summarizeArg(entity.pitch)],
          velocity: { x: summarizeArg(entity.velocity?.x), y: summarizeArg(entity.velocity?.y), z: summarizeArg(entity.velocity?.z) },
          last_good: lastGoodState ? { pos: lastGoodState.pos, yaw: lastGoodState.yaw, pitch: lastGoodState.pitch } : null,
          source_guard: 'state_guard',
          check: nonfiniteChecks,
          physics_present: Boolean(bot.physics),
          physics_enabled: bot.physicsEnabled,
          uptime_s: Math.round(process.uptime()),
          rss_bytes: process.memoryUsage().rss,
        });
      }
      if (lastGoodState) restoreEntityPhysicsState(entity, lastGoodState);
      if (nonfiniteChecks >= 3) {
        nonfiniteChecks = 0;
        if (!resetState) {
          softReset('nonfinite_bot_state', {
            source: 'state_guard',
            bad_fields: physicsNonfiniteFields(state),
            recent_api: recentApi.slice(-10),
          });
        }
      }
    } catch { /* the final backstop must never throw */ }
  }, 500);
  timer.unref?.();
}

function makeFakeBot() {
  const fake = new EventEmitter();
  fake.username = config.username;
  fake.entity = { position: { x: 0, y: 64, z: 0 } };
  fake.health = 20;
  fake.food = 20;
  fake.inventory = { items: () => [] };
  fake.controlState = {};
  fake.clearControlStates = () => { fake.controlState = {}; };
  fake.setControlState = (name, value) => { fake.controlState[name] = value; };
  fake.pathfinder = { setGoal: () => {}, isMoving: () => false };
  fake.chat = (message) => send({ type: 'fake_chat_broadcast', username: fake.username, message: String(message).slice(0, 4096) });
  fake.fake = {
    mode: 'fake',
    wait: (ms) => new Promise(resolve => setTimeout(() => resolve('fake_wait_done'), ms)),
    never: () => new Promise(() => {}),
  };
  instrumentMethod(fake.fake, 'wait');
  instrumentMethod(fake.fake, 'never');
  return fake;
}

function snapshot() {
  return serialize({ mode: config.mode, username: bot.username, position: bot.entity?.position,
    health: bot.health, food: bot.food, dimension: bot.game?.dimension,
    inventory: bot.inventory?.items().map(item => ({ name: item.name, count: item.count, slot: item.slot })) ?? [] });
}

const timerAPI = {};
for (const [name, start, stop] of [
  ['setTimeout', setTimeout, clearTimeout], ['setInterval', setInterval, clearInterval],
  ['setImmediate', setImmediate, clearImmediate],
]) {
  timerAPI[name] = (callback, ...args) => {
    const scope = scopes.getStore();
    if (!scope || scope.closed) throw new Error('Timers require an active execution');
    let handle;
    handle = start((...values) => scopes.run(scope, () => {
      if (name !== 'setInterval') scope.timers.delete(handle);
      if (!scope.closed) trackPromise(scope, callback(...values));
    }), ...args);
    scope.timers.set(handle, stop);
    return handle;
  };
}
for (const [name, stop] of [['clearTimeout', clearTimeout], ['clearInterval', clearInterval], ['clearImmediate', clearImmediate]]) {
  timerAPI[name] = (handle) => { scopes.getStore()?.timers.delete(handle); stop(handle); };
}

const context = vm.createContext({ ...timerAPI, globals: Object.create(null) });
context.__mcWindowCheck = () => {
  const scope = currentScope;
  if (!scope || scope.aborted) return;
  if (scope.checkDeadline && performance.now() > scope.checkDeadline) {
    const error = new Error('Program exceeded its deadline inside a loop');
    error.name = 'TimeoutError';
    error.code = 'ERR_EXECUTION_DEADLINE';
    throw error;
  }
};
// vm is used for persistent JavaScript globals, never as a security boundary.
const allowedModules = new Set(modulePolicy(config.lifecycle).allowed);
const requireAllowed = (name) => {
  if (!allowedModules.has(name)) throw new Error(`Module is not enabled: ${name}`);
  const loaded = nativeRequire(name);
  if (name === 'fs' || name === 'node:fs') return readOnlyFs(loaded);
  if (name === 'fs/promises' || name === 'node:fs/promises') return readOnlyFs(loaded);
  return loaded;
};

function retainLeftover(scope) {
  if (!scope.actions.size && !scope.timers.size && !scope.listeners.length) {
    scope.closed = true;
    return;
  }
  leftoverScopes.add(scope);
  if (leftoverScopes.size > (config.maxLeftoverScopes ?? 256)) {
    const oldest = leftoverScopes.values().next().value;
    leftoverScopes.delete(oldest);
    if (oldest) cleanup(oldest);
  }
}

async function execute(message) {
  // The server can race the next execute against soft_reset/soft_ready; queue
  // it instead of throwing "Worker already executing" so no execution is lost
  // to a reset boundary. Queue draining happens after ready/completion.
  if (resetState || !botReady || currentScope) { queueExecution(message); return; }
  const scope = makeScope(message.execution_id);
  currentScope = scope;
  try {
    scope.sourceHash = createHash('sha256').update(String(message.source ?? '')).digest('hex').slice(0, 16);
    scope.sourceHead = String(message.source ?? '').replace(/\s+/g, ' ').slice(0, 300);
  } catch {}
  try { scope.before = safeSnapshot(); } catch { scope.before = null; }
  scope.deadline = performance.now() + (Number(message.timeout_ms) || 60000);
  scope.checkDeadline = scope.deadline - Math.min(150, (Number(message.timeout_ms) || 60000) / 10);
  const logger = scopedLogger();
  let result;
  let error;
  let timedOut = false;
  try {
    const guardedSource = instrumentLoopDeadlines(message.source);
    const fn = vm.runInContext(`(async function(bot, require, console, globalThis, isCancelled, jobs) {\n${guardedSource}\n})`, context,
      { filename: `execution-${scope.id}.js` });
    // Enforce the synchronous portion of the program inside V8's interruptible
    // vm watchdog. This interrupts pure `while(true){}` loops before the HTTP
    // timeout without killing the worker/globals. A CPU loop after an `await`
    // still falls back to the supervisor's abort grace.
    const syncBudget = Math.max(10, Math.min(Number(message.timeout_ms) || 60000,
      (Number(message.timeout_ms) || 60000) - 100));
    context.__paperInvoke = () => fn(agentBot ?? bot, requireAllowed, logger, context,
      () => Boolean(scopes.getStore()?.cancelled || scopes.getStore()?.closed), config.paperSingleProgram === true ? undefined : jobAPI);
    try {
      result = await scopes.run(scope, () => {
        if (config.chatDelivery === 'program_output' && pendingChat.length) {
          for (const item of pendingChat.splice(0, 50)) logger.log('[chat] ' + item.username + ': ' + item.message);
        }
        return vm.runInContext('__paperInvoke()', context, { timeout: syncBudget });
      });
    } catch (caught) {
      if (caught && (caught.code === 'ERR_SCRIPT_EXECUTION_TIMEOUT' || caught.code === 'ERR_EXECUTION_DEADLINE')) {
        timedOut = true;
        error = { name: 'TimeoutError', message: caught.message || 'Program exceeded its deadline' };
      } else {
        error = errorValue(caught);
      }
    } finally {
      try { delete context.__paperInvoke; } catch {}
    }
  } catch (caught) {
    error = errorValue(caught);
  }
  // A timed-out execution was detached by the supervisor. Its leftover work
  // keeps running, but this invocation must not emit a second completion.
  if (scope.aborted) {
    retainLeftover(scope);
    drainPendingExecutions();
    return;
  }
  if (scope.aborted) return;
  completeScope(scope, result, error, timedOut);
  drainPendingExecutions();
}

function completeScope(scope, result, error, timedOut, options = {}) {
  if (scope.completed) return;
  scope.completed = true;
  if (currentScope === scope) currentScope = null;
  const unfinishedActions = scope.actions.size;
  const moving = Object.values(bot.controlState ?? {}).some(Boolean) || Boolean(bot.targetDigBlock) || Boolean(bot.pathfinder?.isMoving?.());
  let summary;
  let needsReset = false;
  let resetReason = timedOut ? 'timeout' : null;
  if (leftoverPolicy) {
    // Paper §4 semantics: return/timeout does not cancel the program's timers,
    // listeners, native actions, movement or globals. Retain the scope and do
    // not clear controls; a later program can observe and continue them.
    summary = { logs_suppressed: scope.logsSuppressed, logs_truncated: scope.logsTruncated, log_bytes: scope.logBytes,
      timers_cleared: 0, listeners_removed: 0, unfinished_actions: unfinishedActions,
      movement_controls_stopped: 0, pathfinder_goal_stopped: false, leftover_retained: true };
    retainLeftover(scope);
  } else {
    summary = cleanup(scope);
    // A registered background task may own movement. Foreground cleanup must not
    // stop it; explicit task cancellation / session fencing owns that lifecycle.
    const backgroundActive = [...jobs.values()].some(job => !job.closed);
    if (!backgroundActive) try { bot.clearControlStates?.(); bot.pathfinder?.setGoal(null); bot.stopDigging?.(); } catch {}
    needsReset = timedOut || unfinishedActions > 0 || (moving && !backgroundActive);
    if (!resetReason) resetReason = unfinishedActions > 0 ? 'unfinished_async_action' : moving && !backgroundActive ? 'unfinished_movement' : null;
  }
  if (options.forceNeedsReset === false) needsReset = false;
  if (options.resetReason) resetReason = options.resetReason;
  const serialized = boundedValue(result, config.maxResultBytes ?? 262144);
  send({ type: 'completed', execution_id: scope.id, result: serialized.value, error, output_truncated: serialized.truncated,
    before: scope.before ?? null, after: safeSnapshot(), timed_out: timedOut, needs_reset: needsReset, reset_reason: resetReason, cleanup: summary });
}

function abortExecution(executionId) {
  if (!currentScope || currentScope.id !== executionId) return false;
  const scope = currentScope;
  scope.aborted = true;
  currentScope = null;
  retainLeftover(scope);
  send({ type: 'abort_ack', execution_id: executionId });
  drainPendingExecutions();
  return true;
}

process.on('message', (message) => {
  if (message.type === 'execute') {
    // Queue during startup/reset: the server may dispatch the next execution
    // immediately after seeing `completed` but before it processes soft_reset.
    if (resetState || !botReady || currentScope) { queueExecution(message); return; }
    execute(message).catch(error => fatalExit('Execution failed: ' + String(error?.stack ?? error)));
    return;
  }
  if (message.type === 'abort_execution') {
    const queuedIndex = pendingExecutions.findIndex(item => item.execution_id === message.execution_id);
    if (queuedIndex >= 0) {
      pendingExecutions.splice(queuedIndex, 1);
      send({ type: 'abort_ack', execution_id: message.execution_id });
      return;
    }
    abortExecution(message.execution_id);
    return;
  }
  if (message.type === 'cancel_execution') {
    const queuedIndex = pendingExecutions.findIndex(item => item.execution_id === message.execution_id);
    if (queuedIndex >= 0) {
      const [queued] = pendingExecutions.splice(queuedIndex, 1);
      failQueuedExecution(queued, 'Program cancelled before it started', 'CancelledError');
      return;
    }
    if (currentScope?.id === message.execution_id) currentScope.cancelled = true;
    return;
  }
  if (message.type === 'cancel_job') {
    const scope = jobs.get(message.job_id);
    if (scope && !scope.closed) {
      scope.cancelled = true; scope.status = 'cancelling'; scope.cancelStatus = message.status;
      // Remove passive registrations now; active function/native actions must
      // acknowledge cancellation before parent confirms it (or fences at 1s).
      for (const [handle, stop] of scope.timers) stop(handle);
      for (const [emitter, event, listener] of scope.listeners) emitter.removeListener(event, listener);
      scope.timers.clear(); scope.listeners.length = 0;
      checkJob(scope);
    }
    return;
  }
  if (message.type === 'fake_chat_deliver' && config.mode === 'fake') {
    try { bot?.emit('chat', message.username, message.message); } catch {}
  }
});
// IPC loss remains a hard exit: the worker must not outlive its session.
process.on('disconnect', () => process.exit(0));

process.on('uncaughtException', error => handleAsyncFatal(error, 'UNCAUGHT_EXCEPTION'));
let ignoredPathfinderRejections = 0;
process.on('unhandledRejection', error => {
  // Pathfinder's normal route outcomes can reject derived/background promises
  // after their caller has moved on. That is not a worker-fatal condition.
  if (isPathfinderRejection(error)) {
    ignoredPathfinderRejections++;
    if (ignoredPathfinderRejections <= 20) {
      process.stderr.write('PATHFINDER_REJECTION_IGNORED ' + JSON.stringify({
        name: String(error?.name ?? error?.constructor?.name ?? ''),
        message: String(error?.message ?? error).slice(0, 240),
        count: ignoredPathfinderRejections,
      }) + '\n');
    }
    return;
  }
  handleAsyncFatal(error, 'UNHANDLED_REJECTION');
});

// One guard interval for the whole process; it follows the current bot across
// in-process soft resets.
startBotStateGuard();
try {
  await startBot({ initial: true });
} catch (error) {
  // Initial startup failure keeps the existing hard-fail behaviour: the server
  // fails the session startup/startup timeout and restarts the worker.
  try { send({ type: 'fatal', error: String(error?.stack ?? error).slice(0, 32768) }); } catch {}
  process.exit(1);
}
