import http from 'node:http';
import { readFileSync, readdirSync } from 'node:fs';
import { performance } from 'node:perf_hooks';
import { fork } from 'node:child_process';
import { randomUUID, createHash, timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { modulePolicy } from './capabilities.js';

const watchdogPath = fileURLToPath(new URL('./watchdog.js', import.meta.url));
const workerPath = fileURLToPath(new URL('./worker.js', import.meta.url));
const TERMINAL = new Set(['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted']);
const now = () => new Date().toISOString();
function workerRss(pid) {
  if (!pid || process.platform !== 'linux') return null;
  try { return Number(readFileSync(`/proc/${pid}/status`, 'utf8').match(/^VmRSS:\s+(\d+)/m)?.[1] ?? 0) * 1024; }
  catch { return null; }
}

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function tokenEqual(a, b) {
  if (!a || !b) return false;
  const left = Buffer.from(a); const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

// Fixed-size conservative tombstones: false positives reject (410), never replay.
class Tombstones {
  constructor() { this.bits = new Uint8Array(1024 * 1024); this.count = 0; }
  positions(id) { const hash = createHash('sha256').update(id).digest(); return [0, 4, 8, 12].map(offset => hash.readUInt32LE(offset) % (this.bits.length * 8)); }
  has(id) { return this.positions(id).every(bit => this.bits[bit >> 3] & (1 << (bit & 7))); }
  add(id) { for (const bit of this.positions(id)) this.bits[bit >> 3] |= 1 << (bit & 7); this.count++; }
}

class Session {
  constructor(config, identity) {
    this.config = config;
    this.sessionId = randomUUID();
    this.agentId = identity.agent_id;
    this.username = identity.username ?? identity.agent_id;
    this.worldEpoch = identity.world_epoch ?? 1;
    this.runId = identity.run_id ?? 'development';
    this.paused = identity.paused ?? false;
    this.lifecycle = identity.lifecycle ?? config.lifecycle;
    this.maxBackgroundJobs = identity.max_background_jobs ?? config.maxBackgroundJobs;
    this.runDeadline = performance.now() + (identity.remaining_run_ms ?? config.maxBackgroundTtlMs);
    this.jobs = new Map();
    this.runtimeEpoch = 0;
    this.reservedRuntimeEpoch = false;
    this.worker = null;
    this.starting = null;
    this.current = null;
    this.aborting = new Map();
    this.executions = new Map();
    this.requests = new Map();
    this.queue = [];
    this.events = [];
    this.eventCursor = 0;
    this.ackedCursor = 0;
    this.eventBytes = 0;
    this.reliabilityFault = null;
    this.eventDrops = 0;
    this.tombstones = new Tombstones();
    this.sequenceHighWater = 0;
    this.renewLease(config.leaseTimeoutMs);
    this.leaseExpired = false;
    this.fencing = null;
    this.draining = false;
    this.closed = false;
    // Soft reset (same worker process, new Mineflayer client) coordination. A
    // soft reset never changes runtimeEpoch and never clears child.ready.
    this.softResetting = false;
    this.softResetCount = 0;
    this.workerGeneration = 0;
    this.softResetDone = null;
    this.softResetResolve = null;
    this.softResetTimer = null;
    this.softResetReason = null;
  }

  renewLease(duration = this.config.leaseTimeoutMs) {
    this.leaseExpiresAt = Date.now() + duration;
    this.leaseDeadline = performance.now() + duration;
  }

  event(kind, payload = {}, executionId = null, source = 'runtime', attributes = {}, rejectOnOverflow = false) {
    const event = { session_id: this.sessionId, cursor: this.eventCursor + 1, kind, agent_id: this.agentId, run_id: this.runId, received_at: now(),
      world_epoch: this.worldEpoch, runtime_epoch: this.runtimeEpoch, mode: this.config.mode,
      source, execution_id: executionId, payload, ...attributes };
    const bytes = Buffer.byteLength(JSON.stringify(event));
    if (this.events.length >= this.config.maxEvents || this.eventBytes + bytes > this.config.maxEventBytes) {
      if (rejectOnOverflow) throw new HttpError(429, 'Event admission limit reached; collect and acknowledge events before submitting');
      this.eventDrops++;
      if (!this.reliabilityFault) {
        this.reliabilityFault = { kind: 'event_buffer_overflow', first_lost_kind: kind, after_cursor: this.eventCursor, at: now() };
        this.paused = true;
        queueMicrotask(() => this.fence('event_buffer_overflow').catch(() => {}));
      }
      return null;
    }
    this.eventCursor++;
    Object.defineProperty(event, '_bytes', { value: bytes });
    this.events.push(event);
    this.eventBytes += bytes;
    return event;
  }

  checkIdentity(body) {
    if (body.session_id !== undefined && body.session_id !== this.sessionId) throw new HttpError(409, 'session_id mismatch');
    if (body.run_id !== this.runId || body.world_epoch !== this.worldEpoch) throw new HttpError(409, 'Session run_id/world_epoch mismatch');
  }

  heartbeat(body) {
    this.checkIdentity(body);
    if (this.closed || this.leaseExpired || performance.now() >= this.leaseDeadline) throw new HttpError(409, 'Supervisor lease expired; recreate session');
    this.renewLease();
    return this.describe();
  }

  readEvents(after, limit = 1000) {
    if (after < this.ackedCursor) throw new HttpError(410, `Event cursor already acknowledged/pruned; oldest cursor is ${this.ackedCursor}`);
    if (after > this.eventCursor) throw new HttpError(409, 'Event cursor exceeds session high water mark');
    const events = [];
    let bytes = 0;
    for (const event of this.events) {
      if (event.cursor <= after) continue;
      if (events.length >= limit || (events.length && bytes + event._bytes > this.config.maxResponseBytes / 2)) break;
      events.push(event); bytes += event._bytes;
    }
    const next = events.at(-1)?.cursor ?? after;
    return { session_id: this.sessionId, events, next_cursor: next, has_more: next < this.eventCursor,
      acked_cursor: this.ackedCursor, reliability_fault: this.reliabilityFault, dropped_events: this.eventDrops };
  }

  acknowledge(body) {
    this.checkIdentity(body);
    const cursor = body.cursor;
    if (!Number.isSafeInteger(cursor) || cursor < 0 || cursor > this.eventCursor) throw new HttpError(400, 'Invalid acknowledgement cursor');
    if (cursor > this.ackedCursor) {
      let count = 0;
      while (count < this.events.length && this.events[count].cursor <= cursor) this.eventBytes -= this.events[count++]._bytes;
      this.events.splice(0, count);
      this.ackedCursor = cursor;
    }
    this.pruneExecutions();
    return this.describe();
  }

  pruneExecutions() {
    let terminal = [...this.executions.values()].filter(record => TERMINAL.has(record.status)).length;
    for (const [id, record] of this.executions) {
      if (terminal <= this.config.maxRetainedExecutions) break;
      if (!TERMINAL.has(record.status) || record.event_cursor > this.ackedCursor) continue;
      this.executions.delete(id); this.requests.delete(record.request_id);
      if (record.request_sequence === null) this.tombstones.add(record.request_id);
      terminal--;
    }
  }

  async fence(reason) {
    this.paused = true;
    if (this.fencing) return this.fencing;
    this.fencing = (async () => {
      for (const record of this.executions.values()) if (record.status === 'accepted') {
        record.status = 'interrupted'; record.error = { name: 'FencedError', message: reason }; record.reset_reason = reason; record.finished_at = now();
        record.event_cursor = this.eventCursor + 1;
        this.event('execution_interrupted', { error: record.error, reset_reason: reason, record: { ...record } }, record.execution_id);
        record.event_cursor = this.eventCursor;
      }
      this.queue = [];
      if (this.current) await this.finish(this.current, 'interrupted', { error: { name: 'FencedError', message: reason }, reset_reason: reason }, true);
      await this.killWorker(reason);
    })().finally(() => { this.fencing = null; });
    return this.fencing;
  }

  securityStatus() {
    if (!this.worker) return 'no_live_generated_worker';
    if (this.worker.remote && this.worker.sandbox?.native_filter && this.worker.security?.identity_bound) return 'remote_native_sandbox';
    if (this.worker.remote && this.worker.sandbox?.native_filter) return 'remote_native_sandbox_identity_unconfirmed';
    return 'unsafe_untrusted_code_requires_external_container_isolation';
  }

  describe() {
    return { worker_transport: this.config.workerEndpoints[this.agentId] ? 'remote' : 'local', native_sandbox: this.worker?.sandbox ?? null, execution_boundary: this.worker?.security ?? null, module_policy: this.worker?.modulePolicy ?? modulePolicy(this.lifecycle), lifecycle: this.lifecycle, protocol_version: 3, background_jobs: [...this.jobs.values()].filter(job => !TERMINAL.has(job.status)).length, remaining_run_ms: Math.max(0, Math.floor(this.runDeadline - performance.now())), session_id: this.sessionId, closed: this.closed, agent_id: this.agentId, username: this.username, run_id: this.runId, paused: this.paused, mode: this.config.mode,
      world_epoch: this.worldEpoch, runtime_epoch: this.runtimeEpoch, connected: Boolean(this.worker?.ready && !this.worker?.expectedExit && !this.reliabilityFault),
      active_execution_id: this.current?.record.execution_id ?? null, queued: this.queue.length,
      event_cursor: this.eventCursor, acked_cursor: this.ackedCursor, lease_expired: this.leaseExpired,
      lease_expires_at: new Date(this.leaseExpiresAt).toISOString(), lease_timeout_ms: this.config.leaseTimeoutMs,
      reliability_fault: this.reliabilityFault, dropped_events: this.eventDrops,
      buffers: { events: this.events.length, event_bytes: this.eventBytes, executions: this.executions.size,
        requests: this.requests.size, request_tombstones: this.tombstones.count, tombstone_bytes: this.tombstones.bits.byteLength },
      request_sequence_high_water: this.sequenceHighWater,
      soft_resetting: this.softResetting, soft_resets: this.softResetCount,
      worker_generation: this.workerGeneration,
      worker_pid: this.worker?.pid ?? null, worker_rss_bytes: workerRss(this.worker?.pid),
      execution_security: this.securityStatus() };
  }

  async ensureWorker() {
    if (this.closed) throw new Error('Session closed');
    // A soft reset recreates the client in the SAME worker process; the worker is
    // still ready, so never fork a replacement just because the gate is set.
    if (this.softResetting && this.worker?.ready && !this.worker.expectedExit && !this.worker.killPromise) return this.worker;
    if (this.worker?.expectedExit && this.worker.killPromise) await this.worker.killPromise;
    if (this.worker?.ready && !this.worker.expectedExit) return this.worker;
    if (this.starting) return this.starting;
    if (!this.reservedRuntimeEpoch) this.runtimeEpoch += 1;
    this.reservedRuntimeEpoch = false;
    const workerConfig = { mode: this.config.mode, username: this.username, host: this.config.mcHost,
      port: this.config.mcPort, version: this.config.mcVersion, lifecycle: this.lifecycle, paperSingleProgram: this.config.paperSingleProgram, maxBackgroundJobs: this.maxBackgroundJobs, remainingRunMs: Math.max(0, Math.floor(this.runDeadline - performance.now())), runDeadlineAt: Date.now() + Math.max(0, this.runDeadline - performance.now()), maxLogs: this.config.maxLogs, maxLogBytes: this.config.maxLogBytes, maxResultBytes: this.config.maxResultBytes, leftoverPolicy: this.config.leftoverPolicy, chatDelivery: this.config.chatDelivery, maxLeftoverScopes: Number(process.env.MAX_LEFTOVER_SCOPES ?? 256), physicsStallMs: this.config.physicsStallMs };
    // Never pass gateway/model secrets to code execution children.
    const remote = this.config.workerEndpoints[this.agentId];
    let child;
    let pulse;
    let watchdog;
    if (remote) {
      const factory = this.config.workerFactory ?? (await import('./remote-worker.js')).createRemoteWorker;
      child = factory({ endpoint: remote.endpoint, token: remote.token, workerConfig, instanceId: this.config.instanceId, agentId: this.agentId, watchdogMs: this.config.parentWatchdogMs });
      child.remote = true;
    } else {
      if (this.lifecycle === 'paper_aligned' && this.config.mode === 'minecraft') throw new Error('paper_aligned Minecraft requires a separate remote execution container');
      child = fork(workerPath, [], { detached: process.platform !== 'win32',
        env: { PATH: process.env.PATH ?? '/usr/bin:/bin', NODE_ENV: 'production', RUNTIME_INSTANCE_ID: this.config.instanceId, BOT_WORKER_CONFIG: JSON.stringify(workerConfig), VIEWER_PORT: process.env.VIEWER_PORT ?? '', VIEWER_FIRST_PERSON: process.env.VIEWER_FIRST_PERSON ?? 'true', VIEWER_BIND: process.env.VIEWER_BIND ?? '127.0.0.1', VIEWER_VIEW_DISTANCE: process.env.VIEWER_VIEW_DISTANCE ?? '2' },
        stdio: ['ignore', 'ignore', 'pipe', 'ipc'] });
      watchdog = fork(watchdogPath, [], { detached: process.platform !== 'win32',
        env: { PATH: process.env.PATH ?? '/usr/bin:/bin', RUNTIME_INSTANCE_ID: this.config.instanceId, GUARDED_WORKER_PID: String(child.pid), PARENT_WATCHDOG_MS: String(this.config.parentWatchdogMs), MAX_WORKER_RSS_BYTES: String(this.config.maxWorkerRssBytes) },
        stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
      child.watchdog = watchdog;
      watchdog.on('message', message => {
        if (message?.type === 'fenced' && message.reason !== 'worker_stopped' && !child.expectedExit) {
          child.fenceReason = message.reason;
          this.event('runtime_guardian_fenced', { reason: message.reason });
        }
      });
      pulse = setInterval(() => {
        if (watchdog?.connected) watchdog.send({ type: 'pulse' }, () => {});
      }, Math.max(10, Math.min(1000, this.config.parentWatchdogMs / 4)));
      pulse.unref();
      watchdog.on('error', () => { child.kill('SIGKILL'); });
      watchdog.once('exit', () => { clearInterval(pulse); if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
    }
    this.worker = child;
    let stderr = '';
    child.stderr.on('data', chunk => { stderr = (stderr + chunk.toString()).slice(-8192); console.error('WORKER_STDERR', chunk.toString().slice(-2000)); });
    let readyResolve; let readyReject;
    const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
    const startupTimer = setTimeout(() => {
      readyReject(new Error('Bot connection startup timed out'));
      this.killWorker('startup_timeout').catch(() => {});
    }, this.config.startupTimeoutMs);
    child.on('message', message => {
      if (this.worker !== child || child.expectedExit) return;
      if (message.type === 'ready') {
        if (child.ready) return; // startup metadata is immutable after admitting generated programs.
        child.modulePolicy = message.module_policy ?? null;
        child.sandbox = message.sandbox ?? null;
        if (this.lifecycle === 'paper_aligned' && this.config.mode === 'minecraft' && (!child.remote || !child.sandbox?.native_filter || !child.security?.identity_bound)) {
          readyReject(new Error('Paper-aligned worker isolation/native filter/identity boundary not confirmed'));
          this.killWorker('unconfirmed_execution_boundary').catch(() => {}); return;
        }
        child.ready = true;
        clearTimeout(startupTimer);
        // A hard (re)start is a fresh worker process, so a generation-less ready
        // means the client numbering restarted at the first client.
        this.workerGeneration = Number.isSafeInteger(message.generation) ? message.generation : 1;
        this.event('runtime_ready', { snapshot: message.snapshot, globals_lost: this.runtimeEpoch > 1 });
        readyResolve(child);
      } else if (message.type === 'job_started') {
        if (this.lifecycle !== 'paper_aligned' || this.jobs.has(message.job_id) || [...this.jobs.values()].filter(job => !TERMINAL.has(job.status)).length >= this.maxBackgroundJobs || !Number.isSafeInteger(message.ttl_ms) || message.ttl_ms < 1 || message.ttl_ms > Math.max(0, Math.floor(this.runDeadline - performance.now())) + 100) {
          this.killWorker('invalid_background_job_admission').catch(() => {}); return;
        }
        const job = { job_id: message.job_id, execution_id: message.execution_id, agent_id: this.agentId, session_id: this.sessionId, world_epoch: this.worldEpoch, runtime_epoch: this.runtimeEpoch, status: 'running', started_at: now(), finished_at: null, ttl_ms: message.ttl_ms, logs: [] };
        this.jobs.set(job.job_id, job);
        Object.defineProperty(job, 'timer', { configurable: true, value: setTimeout(() => this.cancelJob(job.job_id, 'timed_out').catch(() => {}), message.ttl_ms) });
        this.event('job_started', { job: { ...job } }, job.execution_id);
      } else if (message.type === 'job_finished') {
        this.finishJob(message.job_id, message.status, message);
      } else if (message.type === 'job_cancel_request') {
        this.cancelJob(message.job_id).catch(() => {});
      } else if (message.type === 'event') {
        this.event(`bot_${message.kind}`, message.payload, this.current?.record.execution_id ?? null, 'bot_client', { payload_truncated: Boolean(message.payload_truncated) });
      } else if (message.type === 'fake_chat_broadcast' && this.config.mode === 'fake') {
        this.config.broadcastFakeChat(message.username, message.message);
      } else if (message.type === 'log' && message.job_id) {
        const job = this.jobs.get(message.job_id);
        if (job && !TERMINAL.has(job.status)) {
          if (job.logs.length < this.config.maxLogs) job.logs.push({ level: message.level, message: message.message });
          this.event('job_log', { job_id: job.job_id, level: message.level, message: message.message }, job.execution_id, 'agent_claim');
        }
      } else if (message.type === 'log' && this.current?.record.execution_id === message.execution_id) {
        const record = this.current.record;
        if (record.logs.length < this.config.maxLogs) record.logs.push({ level: message.level, message: message.message });
        this.event('program_log', { level: message.level, message: message.message }, record.execution_id, 'agent_claim');
      } else if (message.type === 'abort_ack') {
        const entry = this.aborting.get(message.execution_id);
        if (entry) { clearTimeout(entry.timer); this.aborting.delete(message.execution_id); }
      } else if (message.type === 'completed' && this.current?.record.execution_id === message.execution_id) {
        const status = this.current.cancelRequested ? 'cancelled' : message.timed_out ? 'timed_out' : message.error ? 'failed' : 'succeeded';
        this.finish(this.current, status, message, message.needs_reset).catch(() => {});
      } else if (message.type === 'soft_reset') {
        this._handleSoftResetMessage(message);
      } else if (message.type === 'soft_ready') {
        this._handleSoftReadyMessage(message);
      } else if (message.type === 'soft_reset_failed') {
        this._handleSoftResetFailedMessage(message);
      } else if (message.type === 'fatal') {
        this.event('runtime_error', { error: message.error });
        if (this.current) this.finish(this.current, 'interrupted', { error: { message: message.error }, reset_reason: 'worker_fatal' }, true).catch(() => {});
        else { readyReject(new Error(message.error)); this.killWorker('worker_fatal').catch(() => {}); }
      }
    });
    child.on('error', error => {
      if (!child.ready) { readyReject(error); return; }
      // A remote control-channel loss is an execution fault, not a reason to
      // crash the entire bot process. Let the supervisor recover the session.
      this.event('runtime_error', { error: String(error?.message ?? error).slice(0, 1000) });
      this.killWorker('remote_worker_error').catch(() => {});
    });
    child.once('exit', (code, signal) => {
      // An exited worker can never send soft_ready: settle the gate so queued
      // dispatch (and session close) cannot hang on a dead soft reset.
      this._settleSoftReset();
      for (const [executionId, entry] of this.aborting) {
        if (entry.active?.child === child) { clearTimeout(entry.timer); this.aborting.delete(executionId); }
      }
      clearTimeout(startupTimer);
      clearInterval(pulse);
      // The group may outlive its leader; always fence descendants on exit.
      try { if (!child.remote && process.platform !== 'win32') process.kill(-child.pid, 'SIGKILL'); } catch {}
      if (watchdog?.connected) watchdog.send({ type: 'stop' }, () => {});
      readyReject(new Error(`Bot worker exited (${code ?? signal}): ${stderr}`));
      if (this.worker === child) this.worker = null;
      if (this.lifecycle === 'paper_aligned') { this.runtimeEpoch += 1; this.reservedRuntimeEpoch = true; }
      for (const job of this.jobs.values()) if (!TERMINAL.has(job.status)) this.finishJob(job.job_id, 'interrupted', { reset_reason: child.fenceReason ?? 'worker_exit' });
      console.error('WORKER_EXIT', code, signal, Boolean(child.expectedExit), stderr.slice(-2000));
      this.event('runtime_exit', { code, signal, expected: Boolean(child.expectedExit), stderr_tail: stderr.slice(-2000) });
      if (!child.expectedExit && this.current?.child === child) {
        this.finish(this.current, 'interrupted', { error: { message: `Worker exited (${code ?? signal})` }, reset_reason: child.fenceReason ?? 'worker_exit' }, false).catch(() => {});
      }
    });
    this.starting = ready.finally(() => { this.starting = null; });
    return this.starting;
  }

  finishJob(id, status, data = {}) {
    const job = this.jobs.get(id);
    if (!job || TERMINAL.has(job.status)) return job;
    clearTimeout(job.timer); clearTimeout(job.cancelTimer);
    const requested = job.cancel_status;
    Object.assign(job, { status: requested ?? status, finished_at: now(), error: data.error ?? null, result: data.result ?? null, cleanup: data.cleanup ?? null, evidence_incomplete: Boolean(data.cleanup?.logs_suppressed || data.cleanup?.logs_truncated), reset_reason: data.reset_reason ?? null });
    this.event(`job_${job.status}`, { job: { ...job } }, job.execution_id);
    job.resolveCancel?.(job);
    // Retain at most 128 terminal records; their full records remain in the ACK journal.
    for (const [key, value] of this.jobs) {
      if (this.jobs.size <= this.maxBackgroundJobs + 128) break;
      if (TERMINAL.has(value.status)) this.jobs.delete(key);
    }
    return job;
  }

  async cancelJob(id, status = 'cancelled') {
    const job = this.jobs.get(id);
    if (!job) throw new HttpError(404, 'Background job not found');
    if (TERMINAL.has(job.status)) return job;
    if (job.cancelPromise) return job.cancelPromise;
    job.status = 'cancelling'; job.cancel_status = status;
    this.event('job_cancelling', { job_id: id, status }, job.execution_id);
    const promise = new Promise(resolve => {
      Object.defineProperty(job, 'resolveCancel', { configurable: true, value: resolve });
      Object.defineProperty(job, 'cancelTimer', { configurable: true, value: setTimeout(async () => {
        try { await this.killWorker(`background_${status}_unconfirmed`); }
        finally { resolve(this.jobs.get(id)); }
      }, 1000) });
    });
    Object.defineProperty(job, 'cancelPromise', { configurable: true, value: promise });
    if (this.worker?.connected) this.worker.send({ type: 'cancel_job', job_id: id, status }, error => { if (error) this.killWorker('background_cancel_dispatch_error').catch(() => {}); });
    else this.finishJob(id, 'interrupted', { reset_reason: 'worker_absent' });
    return promise;
  }

  async killWorker(reason) {
    this._settleSoftReset();
    for (const [executionId, entry] of this.aborting) {
      clearTimeout(entry.timer); this.aborting.delete(executionId);
    }
    const child = this.worker;
    if (!child) return;
    if (child.killPromise) return child.killPromise;
    child.expectedExit = true;
    child.killPromise = (async () => {
      const exited = child.exitCode === null && child.signalCode === null ? new Promise(resolve => child.once('exit', resolve)) : Promise.resolve();
      try {
        if (!child.remote && process.platform !== 'win32') process.kill(-child.pid, 'SIGKILL');
        else if (child.remote && child.stop) await child.stop();
        else child.kill('SIGKILL');
      } catch (error) { if (error.code !== 'ESRCH') throw error; }
      await exited;
      if (this.worker === child) this.worker = null;
      for (const job of this.jobs.values()) if (!TERMINAL.has(job.status)) this.finishJob(job.job_id, 'interrupted', { reset_reason: reason });
      this.event('runtime_reset', { reason, globals_lost: true, confirmed_old_process_exit: true });
    })();
    return child.killPromise;
  }

  submit(body) {
    if (this.closed) throw new HttpError(409, 'Session closed');
    if (body.session_id !== undefined && body.session_id !== this.sessionId) throw new HttpError(409, 'session_id mismatch');
    if (typeof body.run_id !== 'string' || !body.run_id || body.run_id.length > 128) throw new HttpError(400, 'run_id must be an explicit nonempty string, at most 128 characters');
    if (body.run_id !== this.runId) throw new HttpError(409, 'run_id is stale; observe the current experiment before submitting');
    if (!Number.isInteger(body.world_epoch) || body.world_epoch < 0) throw new HttpError(400, 'world_epoch must be an explicit nonnegative integer');
    if (body.world_epoch !== this.worldEpoch) throw new HttpError(409, 'world_epoch is stale; observe the new session before submitting');
    if (typeof body.request_id !== 'string' || !body.request_id || body.request_id.length > 256) throw new HttpError(400, 'request_id must be a nonempty string, at most 256 characters');
    if (typeof body.source !== 'string' || Buffer.byteLength(body.source) > Math.min(524288, this.config.maxResponseBytes / 4)) throw new HttpError(400, 'source must be a string, at most 512 KiB');
    const timeout = body.timeout_ms ?? 60000;
    if (!Number.isInteger(timeout) || timeout < 10 || timeout > this.config.maxTimeoutMs) throw new HttpError(400, `timeout_ms must be between 10 and ${this.config.maxTimeoutMs}`);
    const hash = createHash('sha256').update(JSON.stringify([body.source, timeout])).digest('hex');
    const sourceHash = createHash('sha256').update(body.source).digest('hex');
    if (body.request_sequence !== undefined && (!Number.isSafeInteger(body.request_sequence) || body.request_sequence < 1)) throw new HttpError(400, 'request_sequence must be a positive safe integer');
    const existing = this.requests.get(body.request_id);
    if (existing) {
      if (existing.sequence !== body.request_sequence) throw new HttpError(409, 'request_id sequence mismatch');
      if (existing.hash !== hash) throw new HttpError(409, 'request_id already used for different program or timeout');
      return existing.record;
    }
    if (performance.now() >= this.leaseDeadline || this.leaseExpired) throw new HttpError(409, 'Supervisor lease expired; recreate session');
    if (this.paused || this.reliabilityFault) throw new HttpError(409, 'Session is paused or fenced');
    if ((body.request_sequence === undefined && this.tombstones.has(body.request_id)) || (body.request_sequence !== undefined && body.request_sequence <= this.sequenceHighWater)) throw new HttpError(410, 'Request was retired; reconcile durable ledger, do not replay');
    this.pruneExecutions();
    if (this.queue.length >= this.config.maxQueue || this.executions.size >= this.config.maxRetainedExecutions + this.config.maxQueue + 1) throw new HttpError(429, 'Execution retention/queue limit reached; collect and acknowledge events before submitting');
    const record = { session_id: this.sessionId, execution_id: randomUUID(), request_id: body.request_id, request_sequence: body.request_sequence ?? null, agent_id: this.agentId, run_id: this.runId,
      status: 'accepted', accepted_at: now(), started_at: null, finished_at: null, timeout_ms: timeout,
      runtime_epoch: null, world_epoch: this.worldEpoch, mode: this.config.mode, source_hash: sourceHash,
      result: null, error: null, logs: [], before: null, after: null, reset_reason: null, event_cursor: 0 };
    // Commit the exact source event before admission. A full buffer rejects the
    // submission, so a program never starts without its recoverable source.
    this.event('execution_accepted', { request_id: body.request_id, request_sequence: body.request_sequence ?? null, source_hash: sourceHash,
      source: body.source, timeout_ms: timeout }, record.execution_id, 'runtime', {}, true);
    this.executions.set(record.execution_id, record);
    this.requests.set(body.request_id, { hash, record, sequence: body.request_sequence });
    if (body.request_sequence !== undefined) this.sequenceHighWater = body.request_sequence;
    this.queue.push({ record, source: body.source });
    record.event_cursor = this.eventCursor;
    this.drain().catch(error => this.event('queue_error', { error: String(error.message) }));
    return record;
  }

  async drain() {
    if (this.draining || this.closed || this.paused) return;
    this.draining = true;
    try {
      while (!this.closed && !this.paused && this.queue.length) {
        if (this.softResetting) {
          // Do not dispatch into a worker whose Mineflayer client is being
          // recreated; the gate settles on soft_ready or on kill/exit.
          const gate = this.softResetDone;
          if (gate) await gate;
          if (this.closed || this.paused) break;
          continue;
        }
        const item = this.queue.shift();
        if (item.record.status !== 'accepted') continue;
        try {
          const child = await this.ensureWorker();
          if (this.closed || item.record.status !== 'accepted') continue;
          if (this.softResetting) {
            // A soft reset can start while ensureWorker() was resolving; requeue
            // and wait for the new client to be ready before dispatching. This is
            // checked before the paused re-queue so a gate is never left pending.
            this.queue.unshift(item);
            const gate = this.softResetDone;
            if (gate) await gate;
            if (this.closed || this.paused) break;
            continue;
          }
          if (this.paused) { this.queue.unshift(item); break; }
          const record = item.record;
          if (!this.event('execution_running', {}, record.execution_id)) { this.queue.unshift(item); break; }
          record.status = 'running'; record.started_at = now(); record.runtime_epoch = this.runtimeEpoch;
          await new Promise(resolve => {
            const active = { record, child, resolve, finishing: false, timer: null };
            this.current = active;
            active.timer = setTimeout(() => {
              if (this.config.leftoverPolicy === 'allow') {
                try { active.child.send({ type: 'abort_execution', execution_id: record.execution_id }); } catch {}
                this.finish(active, 'timed_out', {
                  error: { name: 'TimeoutError', message: `Program exceeded ${record.timeout_ms} ms` }, reset_reason: 'timeout' }, false).catch(() => {});
              } else {
                this.finish(active, 'timed_out', {
                  error: { name: 'TimeoutError', message: `Program exceeded ${record.timeout_ms} ms` }, reset_reason: 'timeout' }, true).catch(() => {});
              }
            }, record.timeout_ms);
            child.send({ type: 'execute', execution_id: record.execution_id, source: item.source, timeout_ms: record.timeout_ms }, error => {
              if (error) this.finish(active, 'interrupted', { error: { message: String(error.message) }, reset_reason: 'dispatch_error' }, true).catch(() => {});
            });
          });
        } catch (error) {
          if (!TERMINAL.has(item.record.status)) {
            item.record.status = 'interrupted'; item.record.error = { message: String(error.message) }; item.record.finished_at = now();
            item.record.event_cursor = this.eventCursor + 1;
            this.event('execution_interrupted', { error: item.record.error, record: { ...item.record } }, item.record.execution_id);
            item.record.event_cursor = this.eventCursor;
          }
          await this.killWorker('connection_failed');
        }
      }
    } finally { this.draining = false; }
  }

  // ---- soft reset (same worker process, new Mineflayer client) ---------
  _settleSoftReset() {
    if (this.softResetTimer) { clearTimeout(this.softResetTimer); this.softResetTimer = null; }
    this.softResetting = false;
    const resolve = this.softResetResolve;
    this.softResetResolve = null;
    this.softResetDone = null;
    try { resolve?.(); } catch {}
  }

  _handleSoftResetMessage(message) {
    if (this.softResetting) return; // single flight: one outstanding gate
    const reason = typeof message.reason === 'string' && message.reason ? message.reason.slice(0, 200) : 'unknown';
    const generation = Number.isSafeInteger(message.generation) ? message.generation : null;
    // A duplicated/stale announcement from an already-resolved client
    // generation must not open a second gate (which would later time out and
    // hard-restart a healthy worker).
    if (this.softResetCount > 0 && generation !== null && generation <= this.workerGeneration) return;
    const executionId = typeof message.execution_id === 'string' ? message.execution_id : null;
    let detail = message.detail ?? null;
    if (detail !== null) {
      try { if (Buffer.byteLength(JSON.stringify(detail)) > 8192) detail = { truncated: true }; }
      catch { detail = { truncated: true }; }
    }
    // Open the gate synchronously: from here on drain() must not dispatch.
    this.softResetting = true;
    this.softResetReason = reason;
    this.softResetDone = new Promise(resolve => { this.softResetResolve = resolve; });
    if (this.softResetTimer) clearTimeout(this.softResetTimer);
    this.softResetTimer = setTimeout(() => {
      if (!this.softResetting) return;
      this.softResetTimer = null;
      // Emit before killing so the timeout is durable in the ledger before the
      // hard-restart events (runtime_exit/runtime_reset) that killWorker emits.
      this.event('runtime_soft_reset_timeout', { reason: this.softResetReason, timeout_ms: this.config.softResetTimeoutMs, generation: this.workerGeneration, globals_lost: false });
      this.killWorker('soft_reset_timeout').catch(() => {});
    }, this.config.softResetTimeoutMs);
    // The soft reset is authoritative session state, not a globals-lost restart:
    // runtimeEpoch and child.ready are deliberately untouched.
    const payload = { reason, execution_id: executionId, generation, detail, globals_lost: false };
    const emit = () => this.event('runtime_soft_reset', payload, executionId);
    if (this.current && !TERMINAL.has(this.current.record.status)) {
      // Defensive: the worker normally completes/fails the running program before
      // sending soft_reset (ordering guarantee). If it did not, it cannot survive
      // the client recreation, so finish it exactly once as an execution fault
      // BEFORE recording runtime_soft_reset (reset=false: keep the worker alive).
      let finished;
      try {
        finished = this.finish(this.current, 'interrupted', { error: { name: 'SoftResetError', message: `Bot client reset: ${reason}` }, reset_reason: 'soft_reset' }, false);
      } catch { emit(); return; }
      finished.then(emit, emit);
    } else {
      emit();
    }
  }

  _handleSoftReadyMessage(message) {
    const generation = Number.isSafeInteger(message.generation) ? message.generation : this.workerGeneration;
    const durationMs = Number.isSafeInteger(message.duration_ms) ? message.duration_ms : null;
    const hadGate = this.softResetting;
    // Duplicate/late ready for a generation we already adopted: the gate was
    // already resolved (never resolve twice) and the count must not inflate.
    if (!hadGate && this.softResetCount > 0 && generation <= this.workerGeneration) return;
    if (hadGate) this.softResetCount += 1;
    else if (Number.isSafeInteger(message.soft_resets)) this.softResetCount = Math.max(this.softResetCount, message.soft_resets);
    if (hadGate || generation > this.workerGeneration && Number.isSafeInteger(message.generation)) this.workerGeneration = generation;
    // Resolve the gate first, then kick dispatch: an already-running drain
    // re-checks the gate itself, and a fresh drain starts after it is cleared.
    this._settleSoftReset();
    this.event('runtime_soft_reset_ready', { generation, duration_ms: durationMs, soft_resets: this.softResetCount, snapshot: message.snapshot ?? null, globals_lost: false });
    this.drain().catch(error => this.event('queue_error', { error: String(error.message) }));
  }

  _handleSoftResetFailedMessage(message) {
    const reason = typeof message.reason === 'string' && message.reason ? message.reason.slice(0, 200) : (this.softResetReason ?? 'unknown');
    const error = typeof message.error === 'string' ? message.error.slice(0, 2000) : String(message.error ?? 'unknown');
    this._settleSoftReset();
    this.event('runtime_soft_reset_failed', { reason, error, globals_lost: false });
    // A failed soft reset is the last-resort hard restart path.
    this.killWorker('soft_reset_failed').catch(() => {});
  }

  async finish(active, status, data, reset) {
    if (active.finishing) return active.done;
    active.finishing = true;
    clearTimeout(active.timer); clearTimeout(active.cancelTimer);
    active.done = (async () => {
      if (reset) await this.killWorker(data.reset_reason ?? status);
      Object.assign(active.record, { status, result: data.result ?? null, error: data.error ?? null,
        before: data.before ?? null, after: data.after ?? null, reset_reason: data.reset_reason ?? null,
        cleanup: data.cleanup ?? null, output_truncated: data.output_truncated ?? false, evidence_incomplete: Boolean(data.output_truncated || data.cleanup?.logs_suppressed || data.cleanup?.logs_truncated), finished_at: now() });
      active.record.event_cursor = this.eventCursor + 1;
      this.event(`execution_${status}`, { error: active.record.error, reset_reason: active.record.reset_reason, record: { ...active.record } }, active.record.execution_id);
      active.record.event_cursor = this.eventCursor;
      if (this.current === active) this.current = null;
      active.resolve(); active.cancelResolve?.(active.record);
      this.pruneExecutions();
      return active.record;
    })();
    return active.done;
  }

  async cancel(id) {
    const record = this.executions.get(id);
    if (!record) throw new HttpError(404, 'Execution not found');
    if (TERMINAL.has(record.status)) return record;
    if (this.current?.record === record) {
      if (this.lifecycle === 'paper_aligned') {
        const active = this.current;
        if (active.cancelPromise) return active.cancelPromise;
        active.cancelRequested = true;
        active.cancelPromise = new Promise(resolve => { active.cancelResolve = resolve; });
        active.cancelTimer = setTimeout(() => this.finish(active, 'cancelled', { error: { name: 'CancelledError', message: 'Cancellation not acknowledged within 1000 ms' }, reset_reason: 'cancelled_unconfirmed' }, true).catch(() => {}), 1000);
        if (active.child.connected) active.child.send({ type: 'cancel_execution', execution_id: id }, error => { if (error) this.finish(active, 'cancelled', { reset_reason: 'cancel_dispatch_error' }, true).catch(() => {}); });
        return active.cancelPromise;
      }
      await this.finish(this.current, 'cancelled', { error: { name: 'CancelledError', message: 'Cancelled by caller' }, reset_reason: 'cancelled' }, true);
    } else {
      record.status = 'cancelled'; record.finished_at = now();
      this.queue = this.queue.filter(item => item.record !== record);
      record.event_cursor = this.eventCursor + 1;
      this.event('execution_cancelled', { queued: true, record: { ...record } }, id);
      record.event_cursor = this.eventCursor;
    }
    return record;
  }

  async close() {
    if (this.closePromise) return this.closePromise;
    this.closed = true;
    this.closePromise = (async () => {
    if (this.fencing) await this.fencing;
    // A request can have left the queue while awaiting worker startup. Fence all
    // accepted records, including that request, before returning final events.
    for (const record of this.executions.values()) if (record.status === 'accepted') await this.cancel(record.execution_id);
    this.queue = [];
    if (this.current) await this.finish(this.current, 'interrupted', { error: { message: 'Session closed' }, reset_reason: 'session_closed' }, true);
    await this.killWorker('session_closed');
    })();
    return this.closePromise;
  }

  async pause() {
    if (this.closed) throw new HttpError(409, 'Session closed');
    this.paused = true;
    if (this.lifecycle === 'paper_aligned') await this.fence('session_paused');
    this.event('session_paused', { current_program_may_continue: this.lifecycle === 'strict' });
    return this.describe();
  }

  resume() {
    if (this.closed) throw new HttpError(409, 'Session closed');
    if (this.reliabilityFault) throw new HttpError(409, 'Session has lost audit events; recreate after recording the reliability fault');
    if (this.leaseExpired || performance.now() >= this.leaseDeadline || this.fencing) throw new HttpError(409, 'Renew supervisor lease and wait for fencing before resume');
    this.leaseExpired = false;
    this.paused = false;
    this.event('session_resumed');
    this.drain().catch(error => this.event('queue_error', { error: String(error.message) }));
    return this.describe();
  }
}

async function readBody(request) {
  let size = 0; const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 1048576) throw new HttpError(413, 'JSON body exceeds 1 MiB');
    chunks.push(chunk);
  }
  try {
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {};
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('Expected JSON object');
    return body;
  }
  catch { throw new HttpError(400, 'Invalid JSON body'); }
}

export async function start(options = {}) {
  const sourceHash = createHash('sha256');
  for (const name of readdirSync(new URL('.', import.meta.url)).filter(name => name.endsWith('.js')).sort()) sourceHash.update(name).update(readFileSync(new URL(name, import.meta.url)));
  const sourceDigest = sourceHash.digest('hex');
  const config = { instanceId: options.instanceId ?? process.env.RUNTIME_INSTANCE_ID ?? randomUUID(), mode: options.mode ?? process.env.MODE ?? 'minecraft',
    bind: options.bind ?? process.env.BIND ?? '127.0.0.1', port: Number(options.port ?? process.env.PORT ?? 3001),
    token: options.token ?? process.env.TOKEN,
    agentTokens: options.agentTokens ?? JSON.parse(process.env.AGENT_TOKENS_JSON ?? '{}'),
    mcHost: options.mcHost ?? process.env.MC_HOST ?? '127.0.0.1',
    mcPort: Number(options.mcPort ?? process.env.MC_PORT ?? 25565),
    mcVersion: options.mcVersion ?? process.env.MC_VERSION ?? '1.20.4',
    startupTimeoutMs: options.startupTimeoutMs ?? Number(process.env.STARTUP_TIMEOUT_MS ?? 45000),
    lifecycle: options.lifecycle ?? process.env.LIFECYCLE ?? 'strict',
    paperSingleProgram: options.paperSingleProgram ?? ['1', 'true'].includes(String(process.env.PAPER_SINGLE_PROGRAM ?? 'false').toLowerCase()),
    leftoverPolicy: options.leftoverPolicy ?? process.env.LEFTOVER_POLICY ?? 'reset',
    chatDelivery: options.chatDelivery ?? process.env.CHAT_DELIVERY ?? 'events',
    abortGraceMs: options.abortGraceMs ?? Number(process.env.ABORT_GRACE_MS ?? 5000),
    maxBackgroundJobs: options.maxBackgroundJobs ?? Number(process.env.MAX_BACKGROUND_JOBS ?? 8),
    maxBackgroundTtlMs: options.maxBackgroundTtlMs ?? Number(process.env.MAX_BACKGROUND_TTL_MS ?? 129600000),
    maxTimeoutMs: options.maxTimeoutMs ?? Number(process.env.MAX_TIMEOUT_MS ?? 180000),
    softResetTimeoutMs: options.softResetTimeoutMs ?? Number(process.env.SOFT_RESET_TIMEOUT_MS ?? 45000),
    physicsStallMs: options.physicsStallMs ?? Number(process.env.PHYSICS_STALL_MS ?? 15000),
    exitOnLeaseExpiry: options.exitOnLeaseExpiry ?? ['1', 'true'].includes(process.env.EXIT_ON_LEASE_EXPIRY ?? 'false'),
    leaseTimeoutMs: options.leaseTimeoutMs ?? Number(process.env.SUPERVISOR_LEASE_MS ?? 30000),
    parentWatchdogMs: options.parentWatchdogMs ?? Number(process.env.PARENT_WATCHDOG_MS ?? 10000),
    maxWorkerRssBytes: options.maxWorkerRssBytes ?? Number(process.env.MAX_WORKER_RSS_BYTES ?? 536870912),
    maxEvents: options.maxEvents ?? Number(process.env.MAX_BUFFERED_EVENTS ?? 4096),
    maxEventBytes: options.maxEventBytes ?? Number(process.env.MAX_EVENT_BYTES ?? 8388608),
    maxRetainedExecutions: options.maxRetainedExecutions ?? Number(process.env.MAX_RETAINED_EXECUTIONS ?? 128),
    maxQueue: options.maxQueue ?? Number(process.env.MAX_QUEUE ?? 32),
    maxLogs: options.maxLogs ?? Number(process.env.MAX_PROGRAM_LOGS ?? 128),
    maxLogBytes: options.maxLogBytes ?? Number(process.env.MAX_PROGRAM_LOG_BYTES ?? 262144),
    maxResultBytes: options.maxResultBytes ?? Number(process.env.MAX_RESULT_BYTES ?? 262144),
    maxResponseBytes: options.maxResponseBytes ?? Number(process.env.MAX_RESPONSE_BYTES ?? 4194304),
    maxSessions: options.maxSessions ?? Number(process.env.MAX_SESSIONS ?? 16),
    workerFactory: options.workerFactory, workerEndpoints: options.workerEndpoints ?? JSON.parse(process.env.WORKER_ENDPOINTS_JSON ?? '{}'),
    allowedAgentId: options.allowedAgentId ?? process.env.ALLOWED_AGENT_ID ?? null };
  if (!config.workerEndpoints || typeof config.workerEndpoints !== 'object' || Array.isArray(config.workerEndpoints)) throw new Error('WORKER_ENDPOINTS_JSON must be an object');
  if (!['strict', 'paper_aligned'].includes(config.lifecycle)) throw new Error('LIFECYCLE must be strict or paper_aligned');
  if (!Number.isSafeInteger(config.maxBackgroundJobs) || config.maxBackgroundJobs < 1 || config.maxBackgroundJobs > 128 || !Number.isSafeInteger(config.maxBackgroundTtlMs) || config.maxBackgroundTtlMs < 1) throw new Error('Invalid background job limits');
  if (!['fake', 'minecraft'].includes(config.mode)) throw new Error('MODE must be fake or minecraft');
  if (typeof config.token !== 'string' || config.token.length < 16) throw new Error('TOKEN must contain at least 16 characters');
  if (typeof config.agentTokens !== 'object' || config.agentTokens === null || Array.isArray(config.agentTokens)) throw new Error('AGENT_TOKENS_JSON must be an object');
  if (config.allowedAgentId !== null && !/^[A-Za-z0-9_]{1,16}$/.test(config.allowedAgentId)) throw new Error('ALLOWED_AGENT_ID must also be a valid Minecraft username');
  const tokens = new Set([config.token]);
  for (const [agent, token] of Object.entries(config.agentTokens)) {
    if (!/^[A-Za-z0-9_-]{1,16}$/.test(agent) || typeof token !== 'string' || token.length < 16 || tokens.has(token)) throw new Error('Agent names/tokens invalid or tokens are not distinct');
    if (config.allowedAgentId !== null && agent !== config.allowedAgentId) throw new Error('Agent tokens must match ALLOWED_AGENT_ID');
    tokens.add(token);
  }
  if (typeof config.exitOnLeaseExpiry !== 'boolean') throw new Error('exitOnLeaseExpiry must be a boolean');
  for (const key of ['leaseTimeoutMs', 'parentWatchdogMs', 'maxWorkerRssBytes', 'maxEvents', 'maxEventBytes', 'maxRetainedExecutions', 'maxQueue', 'maxLogs', 'maxLogBytes', 'maxResultBytes', 'maxResponseBytes', 'maxSessions', 'startupTimeoutMs', 'maxTimeoutMs']) {
    if (!Number.isSafeInteger(config[key]) || config[key] < 1) throw new Error(`${key} must be a positive safe integer`);
  }
  if (config.maxResponseBytes < config.maxResultBytes * 4 || config.maxEventBytes < 1024) throw new Error('Response/event byte limits are too small');
  if (!Number.isSafeInteger(config.softResetTimeoutMs) || config.softResetTimeoutMs < 5000 || config.softResetTimeoutMs > 120000) throw new Error('softResetTimeoutMs must be an integer between 5000 and 120000');
  if (!Number.isSafeInteger(config.physicsStallMs) || config.physicsStallMs < 3000 || config.physicsStallMs > 60000) throw new Error('physicsStallMs must be an integer between 3000 and 60000');
  const sessions = new Map();
  let closing = false;
  let closePromise;
  let orphanExiting = false;
  let orphanEmptyDeadline = performance.now() + config.startupTimeoutMs + config.leaseTimeoutMs;
  const leaseTimer = setInterval(() => {
    for (const session of sessions.values()) if (!session.closed && !session.leaseExpired && performance.now() >= session.leaseDeadline) {
      session.leaseExpired = true;
      session.event('supervisor_lease_expired', { expires_at: new Date(session.leaseExpiresAt).toISOString() });
      session.fence('supervisor_lease_expired').catch(() => {});
    }
    if (config.exitOnLeaseExpiry && !orphanExiting && (sessions.size ? [...sessions.values()].every(session => session.leaseExpired || (session.closed && performance.now() >= session.leaseDeadline)) : performance.now() >= orphanEmptyDeadline)) {
      orphanExiting = true;
      closeRuntime().catch(() => {});
    }
  }, Math.max(10, Math.min(1000, config.leaseTimeoutMs / 4)));
  leaseTimer.unref();
  config.broadcastFakeChat = (username, message) => {
    for (const session of sessions.values()) {
      if (session.worker?.connected && session.worker.ready && !session.worker.expectedExit) {
        session.worker.send({ type: 'fake_chat_deliver', username, message }, () => {});
      }
    }
  };
  function authorize(request, agent, adminOnly = false) {
    const token = (request.headers.authorization ?? '').replace(/^Bearer /, '');
    if (tokenEqual(token, config.token)) return;
    if (!adminOnly && agent && tokenEqual(token, config.agentTokens[agent])) return;
    throw new HttpError(403, 'Forbidden');
  }
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://runtime.local');
      const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
      let result; let code = 200;
      if (request.method === 'GET' && url.pathname === '/health') {
        result = { source_hash: sourceDigest, worker_transport: Object.keys(config.workerEndpoints).length ? 'remote' : 'local', worker_transports: Object.fromEntries(Object.keys(config.workerEndpoints).map(agent => [agent, 'remote'])), native_sandbox: { expected_for_remote: true, version: 1 }, status: closing ? 'closing' : 'ok', runtime_instance_id: config.instanceId, protocol_version: 3, capabilities: { durable_ack: true, leases: true, asynchronous_submission: true, background_jobs: true, lifecycles: ['strict', 'paper_aligned'], leftover_policies: ['reset', 'allow'], module_policies: { strict: modulePolicy('strict'), paper_aligned: modulePolicy('paper_aligned') } }, mode: config.mode, sessions: sessions.size,
          memory: process.memoryUsage(), session_metrics: [...sessions.values()].map(session => session.describe()),
          limits: { exitOnLeaseExpiry: config.exitOnLeaseExpiry, ...Object.fromEntries(['leaseTimeoutMs', 'parentWatchdogMs', 'maxWorkerRssBytes', 'maxEvents', 'maxEventBytes', 'maxRetainedExecutions', 'maxQueue', 'maxLogs', 'maxLogBytes', 'maxResultBytes', 'maxResponseBytes', 'maxSessions'].map(key => [key, config[key]])) },
          execution_security: sessions.size && [...sessions.values()].every(session => session.securityStatus() === 'remote_native_sandbox') ? 'remote_native_sandbox' : 'execution_boundary_unverified_or_mixed' };
      } else if (parts[0] === 'v1' && parts[1] === 'sessions' && parts.length === 2) {
        authorize(request, null, true);
        if (request.method === 'GET') result = { sessions: [...sessions.values()].map(session => session.describe()) };
        else if (request.method === 'POST') {
          const body = await readBody(request);
          if (typeof body.agent_id !== 'string' || !/^[A-Za-z0-9_-]{1,16}$/.test(body.agent_id)) throw new HttpError(400, 'agent_id must be 1–16 ASCII letters, digits, underscore or hyphen');
          if (body.username !== undefined && (typeof body.username !== 'string' || !/^[A-Za-z0-9_]{1,16}$/.test(body.username))) throw new HttpError(400, 'username must be a valid Minecraft username');
          if (body.world_epoch !== undefined && (!Number.isInteger(body.world_epoch) || body.world_epoch < 0)) throw new HttpError(400, 'world_epoch must be a nonnegative integer');
          if (body.run_id !== undefined && (typeof body.run_id !== 'string' || !body.run_id || body.run_id.length > 128)) throw new HttpError(400, 'run_id must be a nonempty string, at most 128 characters');
          if (body.lifecycle !== undefined && !['strict', 'paper_aligned'].includes(body.lifecycle)) throw new HttpError(400, 'Invalid lifecycle');
          if (body.max_background_jobs !== undefined && (!Number.isSafeInteger(body.max_background_jobs) || body.max_background_jobs < 1 || body.max_background_jobs > config.maxBackgroundJobs)) throw new HttpError(400, 'Invalid background job limit');
          if (body.remaining_run_ms !== undefined && (!Number.isSafeInteger(body.remaining_run_ms) || body.remaining_run_ms < 1 || body.remaining_run_ms > config.maxBackgroundTtlMs)) throw new HttpError(400, 'Invalid remaining run deadline');
          if (body.paused !== undefined && typeof body.paused !== 'boolean') throw new HttpError(400, 'paused must be a boolean');
          if (config.allowedAgentId !== null && (body.agent_id !== config.allowedAgentId || (body.username !== undefined && body.username !== config.allowedAgentId))) throw new HttpError(403, 'Container game identity is fixed by ALLOWED_AGENT_ID');
          if (sessions.has(body.agent_id)) {
            const existing = sessions.get(body.agent_id);
            if ((body.lifecycle !== undefined && body.lifecycle !== existing.lifecycle) || (body.username !== undefined && body.username !== existing.username) || (body.world_epoch !== undefined && body.world_epoch !== existing.worldEpoch) || (body.run_id ?? 'development') !== existing.runId) throw new HttpError(409, 'Session identity/run/epoch is immutable; delete before recreation');
            result = existing.describe();
          } else {
            if (closing) throw new HttpError(503, 'Runtime closing');
            if (sessions.size >= config.maxSessions) throw new HttpError(429, 'Session limit reached');
            const session = new Session(config, body);
            session.renewLease(config.startupTimeoutMs + config.leaseTimeoutMs);
            if ([...sessions.values()].some(existing => existing.username === session.username)) throw new HttpError(409, 'Minecraft username already belongs to another session');
            sessions.set(body.agent_id, session);
            try { await session.ensureWorker(); if (session.closed) throw new Error('Session closed during startup'); session.renewLease(); result = session.describe(); code = 201; }
            catch (error) { if (sessions.get(body.agent_id) === session) { sessions.delete(body.agent_id); if (!sessions.size) orphanEmptyDeadline = performance.now() + config.leaseTimeoutMs; } await session.close(); throw new HttpError(503, String(error.message)); }
          }
        } else throw new HttpError(405, 'Method not allowed');
      } else if (parts[0] === 'v1' && parts[1] === 'sessions' && parts.length >= 3) {
        const agent = parts[2];
        authorize(request, agent, request.method === 'DELETE' || (parts.length === 4 && ['pause', 'resume', 'heartbeat'].includes(parts[3])) || (parts[3] === 'events' && parts[4] === 'ack'));
        const session = sessions.get(agent);
        if (!session) throw new HttpError(404, 'Session not found');
        if (url.searchParams.has('session_id') && url.searchParams.get('session_id') !== session.sessionId) throw new HttpError(409, 'session_id mismatch');
        if (parts.length === 3 && request.method === 'GET') result = session.describe();
        else if (parts.length === 4 && parts[3] === 'heartbeat' && request.method === 'POST') result = session.heartbeat(await readBody(request));
        else if (parts.length === 5 && parts[3] === 'events' && parts[4] === 'ack' && request.method === 'POST') result = session.acknowledge(await readBody(request));
        else if (parts.length === 4 && parts[3] === 'pause' && request.method === 'POST') result = await session.pause();
        else if (parts.length === 4 && parts[3] === 'resume' && request.method === 'POST') result = session.resume();
        else if (parts.length === 3 && request.method === 'DELETE') {
          const after = Number(url.searchParams.get('after_cursor') ?? 0);
          if (!Number.isInteger(after) || after < 0) throw new HttpError(400, 'Invalid event cursor');
          if (url.searchParams.get('retain') === 'true') session.retainingClose = true;
          // Close first, then drain all pages before acknowledging deletion. A
          // closed session remains queryable until the tail fits one response.
          session.readEvents(after);
          await session.close();
          const tail = session.readEvents(after);
          const retained = url.searchParams.get('retain') === 'true';
          const deleted = !retained && !tail.has_more && (!session.retainingClose || session.ackedCursor >= session.eventCursor);
          if (deleted && sessions.get(agent) === session) { sessions.delete(agent); if (!sessions.size) orphanEmptyDeadline = performance.now() + config.leaseTimeoutMs; }
          result = { deleted, awaiting_ack: Boolean(session.retainingClose && session.ackedCursor < session.eventCursor), closed: true, agent_id: agent, confirmed_old_process_exit: true, ...tail };
        } else if (parts[3] === 'events' && parts.length === 4 && request.method === 'GET') {
          const after = Number(url.searchParams.get('after_cursor') ?? 0);
          const limit = Number(url.searchParams.get('limit') ?? 100);
          if (!Number.isInteger(after) || after < 0 || !Number.isInteger(limit) || limit < 1 || limit > 1000) throw new HttpError(400, 'Invalid event cursor or limit');
          result = session.readEvents(after, limit);
        } else if (parts[3] === 'jobs' && parts.length === 4 && request.method === 'GET') {
          result = { session_id: session.sessionId, world_epoch: session.worldEpoch, runtime_epoch: session.runtimeEpoch, jobs: [...session.jobs.values()] };
        } else if (parts[3] === 'jobs' && parts.length === 5 && request.method === 'GET') {
          result = session.jobs.get(parts[4]); if (!result) throw new HttpError(404, 'Background job not found');
        } else if (parts[3] === 'jobs' && parts.length === 6 && parts[5] === 'cancel' && request.method === 'POST') {
          result = await session.cancelJob(parts[4]);
        } else if (parts[3] === 'programs' && parts.length === 4 && request.method === 'POST') {
          const body = await readBody(request);
          if (body.agent_id !== undefined && body.agent_id !== agent) throw new HttpError(403, 'agent_id does not match authenticated path');
          result = session.submit(body); code = 202;
        } else if (parts[3] === 'programs' && parts.length === 5 && request.method === 'GET') {
          result = session.executions.get(parts[4]);
          if (!result) throw new HttpError(404, 'Execution not found');
        } else if (parts[3] === 'programs' && parts[5] === 'cancel' && parts.length === 6 && request.method === 'POST') result = await session.cancel(parts[4]);
        else throw new HttpError(404, 'Route not found');
      } else throw new HttpError(404, 'Route not found');
      const encoded = JSON.stringify(result);
      if (Buffer.byteLength(encoded) > config.maxResponseBytes) throw new HttpError(413, 'Response exceeds configured limit; collect event pages first');
      response.writeHead(code, { 'content-type': 'application/json; charset=utf-8' });
      response.end(encoded);
    } catch (error) {
      response.writeHead(error.status ?? 500, { 'content-type': 'application/json; charset=utf-8' });
      response.end(JSON.stringify({ error: String(error.message) }));
    }
  });
  server.requestTimeout = 15000; server.headersTimeout = 10000; server.keepAliveTimeout = 2000; server.timeout = 15000; server.maxRequestsPerSocket = 1000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(config.port, config.bind, resolve); });
  async function closeRuntime() {
    if (closePromise) return closePromise;
    closing = true; clearInterval(leaseTimer);
    closePromise = (async () => {
      await Promise.all([...sessions.values()].map(session => session.close()));
      // Self-owned local fixtures must not survive a dead Python supervisor.
      // Once workers are fenced, closing connections leaves outcomes unknown.
      if (orphanExiting) server.closeAllConnections();
      await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    })();
    return closePromise;
  }
  return { server, address: server.address(), mode: config.mode, close: closeRuntime };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const runtime = await start();
  console.log(JSON.stringify({ event: 'runtime_listening', address: runtime.address, mode: runtime.mode,
    execution_security: 'execution_boundary_unverified_until_sessions_spawn' }));
  let closing = false;
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {
    if (closing) return; closing = true;
    await runtime.close(); process.exit(0);
  });
}
