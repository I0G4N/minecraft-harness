import vm from 'node:vm';
import { createRequire } from 'node:module';
import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import { instrumentLoopDeadlines } from './loop-guard.mjs';
import { boundedValue } from './limits.js';
import { modulePolicy } from './capabilities.js';
import { Worker } from 'node:worker_threads';
import { fileURLToPath } from 'node:url';

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
let bot;
let agentBot;
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
    send({ type: 'log', execution_id: scope.id, job_id: scope.jobId, level, message });
  }]));
}
function finishJob(scope, status = 'succeeded', error = null) {
  if (scope.closed) return;
  const summary = cleanup(scope);
  clearInterval(scope.monitor);
  scope.status = status;
  send({ type: 'job_finished', job_id: scope.jobId, execution_id: scope.id, status, error, cleanup: summary, result: serialize(scope.result) });
  // Native actions cannot be proved stopped without terminating this process.
  if (summary.unfinished_actions) { send({ type: 'fatal', error: 'Background task ended with unfinished native actions' }); process.exit(74); }
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

function makeAgentFacingBot(real) {
  // `bot._client` exposes the raw protocol object.  Model-generated programs
  // have used it to monkey-patch sock.write/splitter.push and to change the
  // compression threshold, which corrupts the Minecraft stream for the whole
  // server (DecoderException/kicks).  Keep the real bot for the worker and hand
  // the program a proxied view without raw protocol internals.
  let pathfinderProxy;
  const hidden = new Set(['_client', 'client', '_events', '_socket']);
  const bound = (target, value) => typeof value === 'function' ? value.bind(target) : value;
  const proxy = new Proxy(real, {
    get(target, prop) {
      if (hidden.has(prop)) return undefined;
      if (prop === 'entity') return entitySnapshot(target.entity);
      if (prop === 'pathfinder' && target.pathfinder) {
        if (!pathfinderProxy) pathfinderProxy = new Proxy(target.pathfinder, {
          get(pt, p) { if (p === 'bot') return proxy; return bound(pt, Reflect.get(pt, p, pt)); },
          set(pt, p, value) { if (p === 'bot') return false; pt[p] = value; return true; },
        });
        return pathfinderProxy;
      }
      return bound(target, Reflect.get(target, prop, target));
    },
    set(target, prop, value) { if (hidden.has(prop) || prop === 'entity') return false; target[prop] = value; return true; },
    has(target, prop) { return hidden.has(prop) ? false : Reflect.has(target, prop); },
  });
  return proxy;
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
  if (currentScope) throw new Error('Worker already executing');
  const scope = makeScope(message.execution_id);
  currentScope = scope;
  scope.before = snapshot();
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
    return;
  }
  if (scope.aborted) return;
  completeScope(scope, result, error, timedOut);
}

function completeScope(scope, result, error, timedOut) {
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
  const serialized = boundedValue(result, config.maxResultBytes ?? 262144);
  send({ type: 'completed', execution_id: scope.id, result: serialized.value, error, output_truncated: serialized.truncated,
    before: scope.before ?? null, after: snapshot(), timed_out: timedOut, needs_reset: needsReset, reset_reason: resetReason, cleanup: summary });
}

function abortExecution(executionId) {
  if (!currentScope || currentScope.id !== executionId) return false;
  const scope = currentScope;
  scope.aborted = true;
  currentScope = null;
  retainLeftover(scope);
  send({ type: 'abort_ack', execution_id: executionId });
  return true;
}

process.on('message', (message) => {
  if (message.type === 'execute') execute(message).catch(error => {
    send({ type: 'fatal', error: String(error.stack ?? error).slice(0, 32768) }); process.exit(1);
  });
  else if (message.type === 'abort_execution') abortExecution(message.execution_id);
  else if (message.type === 'cancel_execution' && currentScope?.id === message.execution_id) currentScope.cancelled = true;
  else if (message.type === 'cancel_job') {
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
  }
  else if (message.type === 'fake_chat_deliver' && config.mode === 'fake') bot.emit('chat', message.username, message.message);
});
process.on('disconnect', () => process.exit(0));
process.on('uncaughtException', error => { send({ type: 'fatal', error: String(error.stack ?? error).slice(0, 32768) }); process.exit(1); });
process.on('unhandledRejection', error => { send({ type: 'fatal', error: String(error?.stack ?? error).slice(0, 32768) }); process.exit(1); });

if (config.mode === 'fake') {
  bot = makeFakeBot();
} else {
  const mineflayer = nativeRequire('mineflayer');
  const { pathfinder } = nativeRequire('mineflayer-pathfinder');
  bot = mineflayer.createBot({ host: config.host, port: config.port, username: config.username, version: config.version,
    auth: 'offline', hideErrors: true });
  bot.loadPlugin(pathfinder);
  await new Promise((resolve, reject) => {
    bot.once('spawn', resolve);
    bot.once('error', reject);
    bot.once('kicked', reason => reject(new Error(`Kicked: ${String(reason)}`)));
    bot.once('end', reason => reject(new Error(`Connection ended before spawn: ${reason}`)));
  });
  // Optional live first-person viewer for human observation. The HTTP/socket
  // renderer runs in an isolated worker thread inside the same read-only
  // sandbox. A viewer exception is caught by this parent thread, which restarts
  // the viewer; the agent bot, execution channel and experiment are untouched.
  const viewerPort = Number(process.env.VIEWER_PORT ?? '');
  if (Number.isInteger(viewerPort) && viewerPort > 0 && viewerPort < 65536) {
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
        const column = bot.world?.getColumnAt(pos);
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
        const columns = bot.world?.columns || {};
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
      const position = bot.entity?.position;
      if (!position) return;
      viewerPositionAt = now;
      viewerSend({ type: 'position', pos: { x: position.x, y: position.y, z: position.z }, yaw: bot.entity.yaw, pitch: bot.entity.pitch });
    }
    const viewerEntityAt = new Map();
    function viewerEntity(entity, force = false) {
      if (!entity || entity === bot.entity) return;
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
      const config = { port: viewerPort, bind, viewDistance, firstPerson, version: bot.version };
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
      const thread = viewerThread; viewerThread = null;
      if (thread) {
        try { thread.postMessage({ type: 'shutdown' }); } catch {}
        const killTimer = setTimeout(() => { thread.terminate().catch(() => {}); }, 500);
        killTimer.unref?.();
        thread.once('exit', () => clearTimeout(killTimer));
      }
    }
    process.on('exit', viewerStop);
    process.on('SIGTERM', () => { viewerStop(); process.exit(0); });
    bot.on('chunkColumnLoad', viewerChunk);
    bot.on('move', () => viewerPosition());
    bot.on('entitySpawn', (entity) => viewerEntity(entity, true));
    bot.on('entityMoved', viewerEntity);
    bot.on('entityGone', (entity) => viewerSend({ type: 'entity', entity: { id: entity.id, delete: true } }));
    bot.on('blockUpdate', viewerBlockUpdate);
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

}
const emitterMethods = new Set(['on', 'once', 'addListener', 'prependListener', 'prependOnceListener', 'off', 'removeListener', 'removeAllListeners', 'emit']);
for (const name of Object.keys(bot)) if (!emitterMethods.has(name)) instrumentMethod(bot, name);
instrumentMethod(bot.pathfinder, 'goto');
instrumentMethod(bot.pathfinder, 'setGoal');
for (const event of ['chat', 'whisper', 'death', 'respawn', 'kicked', 'end', 'error']) {
  bot.on(event, (...args) => {
    const encoded = boundedValue(args, 32768);
    send({ type: 'event', kind: event, payload: encoded.value, payload_truncated: encoded.truncated });
  });
}
bot.on('end', () => process.exit(1));
instrumentEmitter(bot);
agentBot = makeAgentFacingBot(bot);
if (config.chatDelivery === 'program_output') {
  bot.on('chat', (username, message) => {
    pendingChat.push({ username: String(username ?? '').slice(0, 64), message: String(message ?? '').slice(0, 4096), received_at: new Date().toISOString() });
    while (pendingChat.length > 100) pendingChat.shift();
  });
}
send({ type: 'ready', snapshot: snapshot(), mode: config.mode, module_policy: modulePolicy(config.lifecycle), sandbox: { native_filter: process.env.HARNESS_NATIVE_SANDBOX === 'seccomp-v1', version: process.env.HARNESS_NATIVE_SANDBOX === 'seccomp-v1' ? 1 : null } });
