import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { createRemoteWorker } from '../src/remote-worker.js';

async function fixture(t) {
  const sockets = new Set(); const messages = []; const ids = new Set();
  const server = net.createServer(socket => {
    sockets.add(socket); socket.on('error', () => {}); socket.once('close', () => sockets.delete(socket));
    let buffer = ''; let id;
    socket.on('data', chunk => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const message = JSON.parse(buffer.slice(0, newline)); buffer = buffer.slice(newline + 1); messages.push(message);
        if (message.type === 'start') {
          id = message.connection_id; ids.add(id);
          socket.write(JSON.stringify({ type: 'accepted' }) + '\n');
          socket.write(JSON.stringify({ type: 'worker', message: { type: 'ready' } }) + '\n');
        } else if (message.type === 'worker') socket.write(JSON.stringify({ type: 'worker', message: { type: 'completed', source: message.message.source } }) + '\n');
        else if (message.type === 'stop') {
          ids.delete(message.connection_id ?? id);
          socket.end(JSON.stringify({ type: 'stopped', confirmed_stopped: true }) + '\n');
        } else if (message.type === 'status') socket.end(JSON.stringify({ type: ids.has(message.connection_id) ? 'active' : 'stopped', confirmed_stopped: !ids.has(message.connection_id) }) + '\n');
      }
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { for (const socket of sockets) socket.destroy(); await new Promise(resolve => server.close(resolve)); });
  const worker = createRemoteWorker({ endpoint: `tcp://127.0.0.1:${server.address().port}`, token: 'remote-worker-test-secret', workerConfig: { mode: 'fake' }, instanceId: 'test', agentId: 'alice', watchdogMs: 1000 });
  const errors = []; worker.on('error', error => errors.push(error));
  return { worker, messages, errors, sockets };
}

test('remote transport queues pre-handshake sends, leases host and confirms stop', async t => {
  const f = await fixture(t);
  const completed = new Promise(resolve => f.worker.on('message', message => { if (message.type === 'completed') resolve(message); }));
  f.worker.send({ type: 'execute', source: 'return 你好' });
  assert.equal((await completed).source, 'return 你好');
  await delay(300);
  assert.ok(f.messages.some(message => message.type === 'pulse'));
  await f.worker.stop();
  assert.equal(f.worker.exitCode, 0);
  assert.equal(f.worker.connected, false);
  assert.deepEqual(f.errors, []);
});

test('lost control connection reconciles host state and confirms old sandbox stopped before exit', async t => {
  const f = await fixture(t);
  await new Promise(resolve => f.worker.once('message', resolve));
  const exited = new Promise(resolve => f.worker.once('exit', resolve));
  for (const socket of f.sockets) socket.destroy();
  await exited;
  assert.ok(f.errors.length > 0);
  assert.ok(f.messages.some(message => message.type === 'status'));
  assert.ok(f.messages.some(message => message.type === 'stop'));
  assert.equal(f.worker.exitCode, 0);
});
