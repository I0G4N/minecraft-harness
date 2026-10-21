import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { fixedRoutes, startRelay } from '../src/rpc-relay.js';

async function echoFixture(t, options = {}) {
  const sockets = new Set();
  const server = net.createServer({ allowHalfOpen: true }, socket => {
    sockets.add(socket); socket.on('close', () => sockets.delete(socket)); socket.on('error', () => {});
    if (options.readDelay) { socket.pause(); setTimeout(() => socket.resume(), options.readDelay); }
    socket.pipe(socket);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { for (const socket of sockets) socket.destroy(); await new Promise(resolve => server.close(resolve)); });
  return server.address().port;
}
async function client(t, port) {
  const socket = net.createConnection({ host: '127.0.0.1', port, allowHalfOpen: false });
  socket.on('error', () => {});
  t.after(() => socket.destroy());
  await once(socket, 'connect');
  return socket;
}
async function relayFixture(t, options = {}) {
  const targetPort = await echoFixture(t, { readDelay: options.readDelay });
  const routed = [];
  const relay = await startRelay({ enabledAgents: ['bot1'], bind: '127.0.0.1', listenPorts: { bot1: 0 },
    connect(parameters) { routed.push(parameters); return net.createConnection({ ...parameters, host: '127.0.0.1', port: targetPort }); }, ...options });
  t.after(() => relay.close());
  return { relay, port: relay.addresses.bot1.port, routed };
}

async function waitForEmpty(relay) {
  // A peer's close event does not await the relay's other socket close event.
  // Assert eventual release with a deadline, including parallel full-suite load.
  const deadline = Date.now() + 1000;
  while (relay.describe().active_connections && Date.now() < deadline) await delay(5);
  assert.equal(relay.describe().active_connections, 0);
}

test('relay routes are fixed and reject arbitrary targets or agent names', async () => {
  assert.deepEqual(fixedRoutes().map(route => [route.listen_port, route.host, route.port]), [
    [3001, 'bot1', 3001], [3002, 'bot2', 3001], [3003, 'bot3', 3001], [3004, 'bot4', 3001]]);
  for (const enabledAgents of [['evil.example'], ['bot1', 'bot1'], [], ['bot1:1234']]) {
    await assert.rejects(startRelay({ enabledAgents }), /Only distinct fixed agents/);
  }
});

test('relay preserves TCP bytes and half-close with backpressure', { timeout: 5000 }, async t => {
  const { relay, port, routed } = await relayFixture(t, { readDelay: 30 });
  const socket = await client(t, port);
  const payload = Buffer.alloc(2 * 1024 * 1024, 0x63);
  const chunks = [];
  socket.on('data', chunk => chunks.push(chunk));
  const ended = once(socket, 'end');
  socket.end(payload);
  await ended;
  assert.deepEqual(Buffer.concat(chunks), payload);
  assert.equal(routed[0].host, 'bot1');
  assert.equal(routed[0].port, 3001);
  await delay(10);
  await waitForEmpty(relay);
});

test('relay enforces connection limits and recovers capacity after disconnect', async t => {
  const { relay, port } = await relayFixture(t, { maxConnections: 1, maxPerRoute: 1 });
  const first = await client(t, port);
  const excess = await client(t, port);
  if (!excess.destroyed) await once(excess, 'close');
  assert.equal(relay.describe().active_connections, 1);
  assert.equal(relay.describe().rejected_connections, 1);
  first.destroy(); await delay(15);
  const next = await client(t, port);
  assert.equal(relay.describe().active_connections, 1);
  next.destroy();
});

test('relay times out idle clients and closes upstream failures', async t => {
  const { relay, port } = await relayFixture(t, { idleTimeoutMs: 40 });
  const idle = await client(t, port);
  if (!idle.destroyed) await once(idle, 'close');
  await waitForEmpty(relay);
  const rejected = await startRelay({ enabledAgents: ['bot1'], bind: '127.0.0.1', listenPorts: { bot1: 0 },
    connect: parameters => net.createConnection({ ...parameters, host: '127.0.0.1', port: 1 }), connectTimeoutMs: 40 });
  t.after(() => rejected.close());
  const failed = await client(t, rejected.addresses.bot1.port);
  if (!failed.destroyed) await once(failed, 'close');
  await waitForEmpty(rejected);
  assert.equal(rejected.describe().upstream_errors, 1);
});

test('relay connect deadline and shutdown close sockets and listeners', async t => {
  const stalled = new net.Socket();
  const relay = await startRelay({ enabledAgents: ['bot1'], bind: '127.0.0.1', listenPorts: { bot1: 0 },
    connect: () => stalled, connectTimeoutMs: 35 });
  t.after(() => relay.close());
  const socket = await client(t, relay.addresses.bot1.port);
  if (!socket.destroyed) await once(socket, 'close');
  await waitForEmpty(relay);
  const active = await client(t, relay.addresses.bot1.port);
  await relay.close();
  if (!active.destroyed) await once(active, 'close');
  await waitForEmpty(relay);
  await assert.rejects(new Promise((resolve, reject) => {
    const denied = net.createConnection({ host: '127.0.0.1', port: relay.addresses.bot1.port });
    denied.once('connect', () => { denied.destroy(); resolve(); }); denied.once('error', reject);
  }));
});
