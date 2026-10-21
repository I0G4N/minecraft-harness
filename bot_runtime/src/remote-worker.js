import net from 'node:net';
import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';

function parseEndpoint(value) {
  const url = new URL(value);
  if (url.protocol !== 'tcp:' || !url.hostname || !url.port || url.username || url.password || (url.pathname && url.pathname !== '/') || url.search || url.hash) throw new Error('Remote worker endpoint must be tcp://host:port');
  return { host: url.hostname, port: Number(url.port) };
}

export function createRemoteWorker({ endpoint, token, workerConfig, instanceId, agentId, watchdogMs }) {
  const address = parseEndpoint(endpoint);
  if (typeof token !== 'string' || token.length < 16) throw new Error('Remote worker host token too short');
  const child = new EventEmitter();
  child.stderr = new EventEmitter(); child.remote = true; child.pid = null;
  child.exitCode = null; child.signalCode = null; child.connected = false;
  child.connectionId = randomUUID();
  let socket; let pulse; let stopped = false; let disconnected = false; let stopPromise; let confirmPromise;
  const pending = [];
  const write = message => {
    const line = JSON.stringify(message);
    if (Buffer.byteLength(line) > 1048576) throw new Error('Remote worker frame too large');
    if (!socket || socket.destroyed) throw new Error('Remote worker disconnected');
    if (socket.writableLength > 4194304) throw new Error('Remote worker backpressure exceeded');
    return socket.write(line + '\n');
  };
  function exited(code = 0, signal = null) {
    if (stopped) return;
    stopped = true; child.connected = false; child.exitCode = code; child.signalCode = signal;
    clearInterval(pulse); socket?.destroy();
    child.emit('exit', code, signal);
  }
  function open(message, onFrame, onClose) {
    const connection = net.createConnection(address);
    connection.setNoDelay(true); connection.setEncoding('utf8'); let buffer = '';
    connection.once('connect', () => connection.write(JSON.stringify(message) + '\n'));
    connection.on('data', chunk => {
      buffer += chunk.toString('utf8');
      if (Buffer.byteLength(buffer) > 2097152) { connection.destroy(new Error('Remote worker frame limit')); return; }
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
        try { onFrame(JSON.parse(line)); } catch (error) { connection.destroy(error); }
      }
    });
    connection.on('error', error => { if (!stopped) child.emit('error', error); });
    connection.once('close', onClose);
    return connection;
  }
  async function confirmStopped() {
    if (stopped) return;
    if (confirmPromise) return confirmPromise;
    confirmPromise = (async () => {
      const deadline = performance.now() + Math.max(15000, watchdogMs + 5000);
      while (!stopped && performance.now() < deadline) {
        const answer = await new Promise(resolve => {
          let probe;
          const timer = setTimeout(() => { probe.destroy(); resolve(null); }, 1000);
          probe = open({ type: 'status', token, connection_id: child.connectionId, agent_id: agentId }, message => {
            clearTimeout(timer); probe.end(); resolve(message);
          }, () => { clearTimeout(timer); resolve(null); });
        });
        if (answer?.type === 'stopped' && answer.confirmed_stopped === true) { exited(answer.code ?? 0, answer.signal ?? null); return; }
        if (answer?.type === 'active') {
          // Request stop via a separate authenticated channel after reconnect.
          const probe = net.createConnection(address, () => probe.end(JSON.stringify({ type: 'stop', token, connection_id: child.connectionId, agent_id: agentId }) + '\n'));
          probe.on('error', () => {}); probe.setTimeout(1000, () => probe.destroy());
        }
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      if (!stopped) throw new Error('Remote launcher could not confirm sandbox removal; recreation forbidden');
    })();
    return confirmPromise;
  }
  child.send = (message, callback) => {
    try {
      if (!child.connected) {
        if (disconnected || pending.length >= 32) throw new Error('Remote worker unavailable');
        pending.push([message, callback]); return false;
      }
      const accepted = write({ type: 'worker', message }); callback?.(); return accepted;
    } catch (error) { callback?.(error); return false; }
  };
  child.stop = () => {
    if (stopped) return Promise.resolve();
    if (stopPromise) return stopPromise;
    stopPromise = (async () => {
      try { write({ type: 'stop' }); } catch {}
      await confirmStopped();
    })();
    return stopPromise;
  };
  child.disconnect = () => socket?.destroy();
  child.kill = () => { child.stop().catch(error => child.emit('error', error)); return true; };
  socket = open({ type: 'start', token, connection_id: child.connectionId, instance_id: instanceId, agent_id: agentId, watchdog_ms: watchdogMs, worker_config: workerConfig }, message => {
    if (message.type === 'accepted') {
      child.connected = true; child.security = message.security ?? null;
      for (const [queued, callback] of pending.splice(0)) child.send(queued, callback);
      pulse = setInterval(() => { try { write({ type: 'pulse' }); } catch {} }, Math.max(25, Math.min(1000, watchdogMs / 4)));
      pulse.unref();
    } else if (message.type === 'worker') child.emit('message', message.message);
    else if (message.type === 'stderr') child.stderr.emit('data', Buffer.from(String(message.text).slice(0, 8192)));
    else if (message.type === 'stopped' && message.confirmed_stopped === true) exited(message.code ?? 0, message.signal ?? null);
    else if (message.type === 'error') child.emit('error', new Error(String(message.error).slice(0, 4096)));
  }, () => {
    disconnected = true; child.connected = false; clearInterval(pulse);
    if (!stopped) {
      child.emit('error', new Error('Remote control channel closed; sandbox state requires confirmation'));
      confirmStopped().catch(error => child.emit('error', error));
    }
  });
  return child;
}
