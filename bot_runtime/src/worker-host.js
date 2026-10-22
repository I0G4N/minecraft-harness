// Privileged launcher service. Keep this outside every generated-code namespace.
// Fixed per-agent image/network; never accepts docker options or mounts over RPC.
import net from 'node:net';
import { spawn, fork } from 'node:child_process';
import { timingSafeEqual, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

async function docker(args, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const proc = spawn('docker', args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    const timer = setTimeout(() => { proc.kill('SIGKILL'); reject(new Error('Docker operation timed out')); }, timeoutMs);
    proc.stdout.on('data', chunk => { stdout = (stdout + chunk).slice(-65536); });
    proc.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-8192); });
    proc.once('error', error => { clearTimeout(timer); reject(error); });
    proc.once('exit', code => { clearTimeout(timer); resolve({ code, stdout, stderr }); });
  });
}
const equalToken = (a, b) => typeof a === 'string' && Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b));

export async function startWorkerHost(options = {}) {
  const config = {
    agentId: options.agentId ?? process.env.WORKER_AGENT_ID,
    token: options.token ?? process.env.WORKER_HOST_TOKEN,
    id: options.id ?? process.env.WORKER_HOST_ID,
    image: options.image ?? process.env.WORKER_SANDBOX_IMAGE ?? 'minecraft-harness-worker:research',
    network: options.network ?? process.env.WORKER_NETWORK ?? 'none',
    bind: options.bind ?? process.env.WORKER_BIND ?? '127.0.0.1',
    port: Number(options.port ?? process.env.WORKER_PORT ?? 3901),
    identityProxy: options.identityProxy ?? process.env.WORKER_IDENTITY_PROXY === 'true',
    mcHost: options.mcHost ?? process.env.WORKER_MC_HOST,
    mcPort: Number(options.mcPort ?? process.env.WORKER_MC_PORT ?? 25565),
    maxWatchdogMs: Number(options.maxWatchdogMs ?? 10000),
    maxWorkerMemory: options.maxWorkerMemory ?? process.env.WORKER_MAX_MEMORY ?? '512m',
    sandboxRuntime: options.sandboxRuntime ?? process.env.WORKER_SANDBOX_RUNTIME ?? '',
    viewerPort: Number(options.viewerPort ?? process.env.WORKER_VIEWER_PORT ?? 0),
    workerIp: options.workerIp ?? process.env.WORKER_IP ?? '',
  };
  if (!/^[A-Za-z0-9_]{1,16}$/.test(config.agentId ?? '') || !/^[A-Za-z0-9_-]{1,48}$/.test(config.id ?? '') || typeof config.token !== 'string' || config.token.length < 16) throw new Error('Fixed WORKER_AGENT_ID, stable WORKER_HOST_ID and strong WORKER_HOST_TOKEN required');
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$/.test(config.image) || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(config.network)) throw new Error('Invalid fixed worker image/network');
  if (config.sandboxRuntime && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(config.sandboxRuntime)) throw new Error('Invalid OCI sandbox runtime name');
  if (!(config.viewerPort === 0 || (Number.isInteger(config.viewerPort) && config.viewerPort >= 1024 && config.viewerPort <= 65535))) throw new Error('Invalid WORKER_VIEWER_PORT');
  if (config.workerIp && !/^(?:\d{1,3}\.){3}\d{1,3}$/.test(config.workerIp)) throw new Error('Invalid WORKER_IP');
  if (!Number.isSafeInteger(config.maxWatchdogMs) || config.maxWatchdogMs < 100 || config.maxWatchdogMs > 60000) throw new Error('Invalid worker watchdog');
  const label = `mc-harness.worker-host=${config.id}`;
  // A launcher crash must not strand a sandbox before a replacement connects.
  const prior = await docker(['ps', '-aq', '--filter', `label=${label}`]);
  if (prior.code !== 0) throw new Error('Cannot enumerate old worker containers');
  for (const id of prior.stdout.trim().split(/\s+/).filter(Boolean)) {
    const removed = await docker(['rm', '-f', id]);
    if (removed.code !== 0) throw new Error('Cannot remove old worker container');
  }
  const guardian = fork(fileURLToPath(new URL('./worker-host-guardian.js', import.meta.url)), [], {
    detached: true, env: { PATH: process.env.PATH ?? '/usr/bin:/bin', ...(process.env.DOCKER_HOST ? { DOCKER_HOST: process.env.DOCKER_HOST } : {}), WORKER_HOST_GUARD_ID: config.id, WORKER_HOST_GUARD_MS: String(config.maxWatchdogMs) }, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
  let guardianExpectedExit = false;
  const guardianPulse = setInterval(() => { if (guardian.connected) guardian.send({ type: 'pulse' }, () => {}); }, Math.max(25, Math.min(1000, config.maxWatchdogMs / 4)));
  guardianPulse.unref();
  const active = new Map();
  const sockets = new Set();
  const receipts = new Map();
  let closing = false;
  function send(socket, value) {
    if (!socket || socket.destroyed) return;
    const line = JSON.stringify(value);
    if (Buffer.byteLength(line) > 1048576 || socket.writableLength > 4194304) { socket.destroy(); return; }
    socket.write(line + '\n');
  }
  async function stop(record, reason = 'control_stopped') {
    console.error('WORKER_STOP', reason, record.name);
    if (record.stopping) return record.stopping;
    record.stopping = (async () => {
      clearInterval(record.guard);
      // Finish create before remove: a disconnected control stream must not
      // race an in-flight docker run that starts after an absence check.
      if (record.creating) await record.creating;
      // Do not rely on the Docker attach CLI exiting to prove container exit.
      const removed = await docker(['rm', '-f', record.name]);
      const checked = await docker(['inspect', record.name]);
      if (checked.code === 0 || (removed.code !== 0 && !/No such (container|object)/i.test(removed.stderr + checked.stderr))) throw new Error('Sandbox removal could not be confirmed');
      if (record.proc && record.proc.exitCode === null && record.proc.signalCode === null) record.proc.kill('SIGKILL');
      active.delete(record.connectionId);
      const receipt = { type: 'stopped', confirmed_stopped: true, connection_id: record.connectionId, reason, code: record.proc?.exitCode ?? 0, signal: record.proc?.signalCode ?? null };
      receipts.set(record.connectionId, receipt);
      while (receipts.size > 128) receipts.delete(receipts.keys().next().value);
      send(record.socket, receipt);
      record.socket?.end();
      return receipt;
    })();
    return record.stopping;
  }
  guardian.on('error', () => { closing = true; Promise.all([...active.values()].map(record => stop(record, 'launcher_guardian_error'))).catch(() => {}); });
  guardian.once('exit', () => { clearInterval(guardianPulse); if (!guardianExpectedExit) { closing = true; Promise.all([...active.values()].map(record => stop(record, 'launcher_guardian_lost'))).catch(() => {}); } });
  const server = net.createServer(socket => {
    sockets.add(socket); socket.setNoDelay(true); socket.setEncoding('utf8'); socket.setTimeout(15000);
    let buffer = ''; let authenticated = false; let attached; let handling = Promise.resolve(); let queuedFrames = 0;
    const fail = error => { send(socket, { type: 'error', error: String(error?.message ?? error).slice(0, 1024) }); socket.end(); };
    async function handle(message) {
      if (!authenticated) {
        if (closing || !equalToken(message.token, config.token) || message.agent_id !== config.agentId || typeof message.connection_id !== 'string' || !/^[a-f0-9-]{36}$/.test(message.connection_id)) throw new Error('Forbidden');
        authenticated = true;
        if (message.type === 'status') {
          const record = active.get(message.connection_id);
          send(socket, record ? { type: 'active', connection_id: record.connectionId } : receipts.get(message.connection_id) ?? { type: 'stopped', confirmed_stopped: true, reason: 'no_live_sandbox_after_startup_cleanup', connection_id: message.connection_id });
          socket.end(); return;
        }
        if (message.type === 'stop') {
          const record = active.get(message.connection_id);
          send(socket, record ? await stop(record) : { type: 'stopped', confirmed_stopped: true, connection_id: message.connection_id });
          socket.end(); return;
        }
        if (message.type !== 'start' || receipts.has(message.connection_id) || active.size) throw new Error('Old sandbox must be confirmed stopped before another is created');
        const worker = message.worker_config;
        if (!worker || worker.username !== config.agentId || !['fake', 'minecraft'].includes(worker.mode) || !['strict', 'paper_aligned'].includes(worker.lifecycle)) throw new Error('Worker identity or lifecycle invalid');
        if (worker.mode === 'minecraft' && (config.network === 'none' || !config.mcHost || !config.identityProxy)) throw new Error('Minecraft worker requires fixed identity-proxy network and target');
        const watchdog = Math.min(config.maxWatchdogMs, Math.max(100, Number(message.watchdog_ms) || config.maxWatchdogMs));
        // Copy only declared fields. No incoming environment / container flags.
        const safeWorker = Object.fromEntries(['mode', 'username', 'version', 'lifecycle', 'maxBackgroundJobs', 'remainingRunMs', 'runDeadlineAt', 'maxLogs', 'maxLogBytes', 'maxResultBytes', 'leftoverPolicy', 'chatDelivery', 'maxLeftoverScopes', 'paperSingleProgram'].map(key => [key, worker[key]]));
        safeWorker.host = config.mcHost; safeWorker.port = config.mcPort;
        const name = `mc-worker-${config.id}-${randomUUID().slice(0, 8)}`;
        // gVisor (`runsc`) is an optional OCI sandbox runtime here. It adds a
        // user-space kernel/syscall boundary around the already read-only,
        // network-isolated worker container; the legacy seccomp/LD_PRELOAD guard
        // remains in the image as defense in depth.
        const runtime = config.sandboxRuntime ? ['--runtime', config.sandboxRuntime] : [];
        const resourceLimits = ['--memory', config.maxWorkerMemory, '--cpus', '1'];
        // runsc rejects --pids-limit (unsupported cgroup knob) and would fail to
        // create the sandbox; keep the limit under runc and rely on gVisor's own
        // process isolation otherwise.
        if (!config.sandboxRuntime) resourceLimits.push('--pids-limit', '64');
        const viewerArgs = config.viewerPort ? ['--env', `VIEWER_PORT=${config.viewerPort}`, '--env', 'VIEWER_BIND=0.0.0.0', '--env', 'VIEWER_FIRST_PERSON=true', '--env', 'VIEWER_VIEW_DISTANCE=2'] : [];
        const args = ['create', '--name', name, '--label', label, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '1000:1000', ...resourceLimits, ...viewerArgs, ...(config.workerIp ? ['--ip', config.workerIp] : []), '--network', config.network, ...runtime, '--interactive', '--attach', 'stdin', '--attach', 'stdout', '--attach', 'stderr', '--log-driver', 'none', '--env', `BOT_WORKER_CONFIG=${JSON.stringify(safeWorker)}`, config.image];
        const record = { name, proc: null, connectionId: message.connection_id, socket, lastPulse: performance.now(), watchdog, stopping: null };
        attached = record; active.set(record.connectionId, record);
        record.creating = docker(args);
        const created = await record.creating;
        if (created.code !== 0) { await stop(record, 'sandbox_create_failed'); throw new Error('Could not create worker sandbox'); }
        if (record.stopping || socket.destroyed || closing) { await stop(record, 'control_lost_during_create'); return; }
        const inspected = await docker(['inspect', '--format', '{{.Image}}', name]);
        if (inspected.code !== 0) { await stop(record, 'sandbox_inspection_failed'); throw new Error('Cannot inspect worker image'); }
        const proc = spawn('docker', ['start', '--attach', '--interactive', name], { stdio: ['pipe', 'pipe', 'pipe'] });
        record.proc = proc;
        record.guard = setInterval(() => { if (performance.now() - record.lastPulse >= watchdog) stop(record, 'control_watchdog_expired').catch(fail); }, Math.max(25, Math.min(1000, watchdog / 4)));
        record.guard.unref();
        let output = '';
        proc.stdout.setEncoding('utf8');
        proc.stdout.on('data', chunk => {
          output += chunk.toString('utf8');
          if (Buffer.byteLength(output) > 2097152) { stop(record, 'worker_output_limit').catch(fail); return; }
          let newline;
          while ((newline = output.indexOf('\n')) >= 0) {
            const line = output.slice(0, newline); output = output.slice(newline + 1);
            try { send(socket, { type: 'worker', message: JSON.parse(line) }); }
            catch { stop(record, 'invalid_worker_frame').catch(fail); return; }
          }
        });
        proc.stderr.on('data', chunk => send(socket, { type: 'stderr', text: chunk.toString('utf8').slice(-8192) }));
        proc.on('error', () => stop(record, 'docker_attach_error').catch(fail));
        proc.stdin.on('error', () => stop(record, 'docker_input_error').catch(fail));
        proc.once('exit', () => stop(record, 'sandbox_exit').catch(fail));
        send(socket, { type: 'accepted', connection_id: record.connectionId, security: { separate_container: true, read_only: true, no_host_mounts: true, no_control_credentials: true, native_filter_expected: true, identity_bound: Boolean(config.identityProxy), network: config.network, image_id: inspected.stdout.trim() } });
      } else if (attached && !attached.stopping) {
        if (message.type === 'pulse') attached.lastPulse = performance.now();
        else if (message.type === 'stop') await stop(attached);
        else if (message.type === 'worker') {
          const encoded = JSON.stringify(message.message);
          if (Buffer.byteLength(encoded) > 1048576 || attached.proc.stdin.writableLength > 4194304) throw new Error('Worker input limit');
          attached.proc.stdin.write(encoded + '\n');
        } else throw new Error('Unknown control message');
      }
    }
    socket.on('data', chunk => {
      buffer += chunk.toString('utf8');
      if (Buffer.byteLength(buffer) > 2097152) { socket.destroy(); return; }
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
        if (++queuedFrames > 64) { socket.destroy(); return; }
        handling = handling.then(() => handle(JSON.parse(line))).catch(fail).finally(() => queuedFrames--);
      }
    });
    socket.on('timeout', () => socket.destroy());
    socket.on('error', () => {});
    socket.once('close', () => { sockets.delete(socket); if (attached) stop(attached, 'control_disconnected').catch(() => {}); });
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(config.port, config.bind, resolve); });
  return { address: server.address(), async close() {
    closing = true;
    await Promise.all([...active.values()].map(record => stop(record, 'launcher_stopped')));
    guardianExpectedExit = true; clearInterval(guardianPulse);
    if (guardian.connected) guardian.send({ type: 'stop' }, () => {});
    if (guardian.exitCode === null && guardian.signalCode === null) await new Promise(resolve => guardian.once('exit', resolve));
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  } };
}
if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const host = await startWorkerHost();
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => host.close().then(() => process.exit(0), () => process.exit(1)));
  process.stdout.write(JSON.stringify({ worker_host: 'ready', address: host.address }) + '\n');
}
