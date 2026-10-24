import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { deflateSync } from 'node:zlib';
import { inspectChat, inspectMovement, PacketFrames, startIdentityProxy, varint } from '../src/identity-proxy.js';

const text = value => Buffer.concat([varint(Buffer.byteLength(value)), Buffer.from(value)]);
const packet = (id, body = Buffer.alloc(0), compressed = false) => {
  let payload = Buffer.concat([varint(id), body]);
  if (compressed) payload = Buffer.concat([varint(payload.length), deflateSync(payload)]);
  return Buffer.concat([varint(payload.length), payload]);
};
const hello = name => Buffer.concat([
  packet(0, Buffer.concat([varint(765), text('ignored-host'), Buffer.from([0x63, 0xdd]), varint(2)])),
  packet(0, Buffer.concat([text(name), Buffer.alloc(16)])),
]);
async function fixture(t, chat = true) {
  const accepted = []; const received = []; const sockets = [];
  const backend = net.createServer(socket => { sockets.push(socket); accepted.push(socket); socket.on('data', data => received.push(data)); });
  backend.listen(0, '127.0.0.1'); await once(backend, 'listening');
  const proxy = await startIdentityProxy({ targetHost: '127.0.0.1', targetPort: backend.address().port, username: 'player1', gameChat: chat });
  t.after(async () => { for (const socket of sockets) socket.destroy(); await proxy.close(); await new Promise(resolve => backend.close(resolve)); });
  const connect = async name => {
    const client = net.createConnection(proxy.address.port, '127.0.0.1');
    client.setNoDelay(true);
    client.on('error', () => {}); await once(client, 'connect');
    client.write(hello(name)); return client;
  };
  return { proxy, backend, accepted, received, connect };
}

test('identity gate refuses foreign usernames before opening an upstream', async t => {
  const f = await fixture(t); const bad = await f.connect('player2');
  await once(bad, 'close'); assert.equal(f.accepted.length, 0); assert.equal(f.proxy.stats.rejected, 1);
});
test('one identity has one live upstream and can reconnect after confirmed close', async t => {
  const f = await fixture(t); const first = await f.connect('player1');
  for (let i = 0; i < 50 && !f.accepted.length; i++) await delay(5);
  assert.equal(f.accepted.length, 1);
  const second = await f.connect('player1'); await once(second, 'close');
  assert.equal(f.accepted.length, 1); first.destroy(); await delay(30);
  const third = await f.connect('player1');
  for (let i = 0; i < 50 && f.accepted.length < 2; i++) await delay(5);
  assert.equal(f.accepted.length, 2); third.destroy();
});
test('fragmented packets are bounded and forwarded exactly', () => {
  const frames = [], raw = packet(0, Buffer.alloc(512, 5));
  const reader = new PacketFrames((_body, frame) => frames.push(Buffer.from(frame)));
  for (const byte of raw) reader.push(Buffer.from([byte]));
  assert.deepEqual(frames, [raw]);
  assert.throws(() => reader.push(Buffer.from([0xff, 0xff, 0xff, 0xff, 0xff])), /VarInt/);
});
test('wire chat ablation filters compressed packets in both directions', async t => {
  const f = await fixture(t, false); const client = await f.connect('player1'); const downstream = [];
  client.on('data', data => downstream.push(data));
  for (let i = 0; i < 50 && !f.accepted.length; i++) await delay(5);
  const upstream = f.accepted[0];
  upstream.write(packet(3, varint(0))); // Login compression negotiation is itself uncompressed.
  await delay(50);
  client.write(packet(3, Buffer.alloc(0), true)); // login_acknowledged
  await delay(50);
  client.write(packet(2, Buffer.alloc(0), true)); // finish_configuration
  await delay(50);
  const { createRequire } = await import('node:module');
  const mappings = createRequire(import.meta.url)('minecraft-data')('1.20.4').protocol.play.toClient.types.packet[1][0].type[1].mappings;
  const chatId = Number(Object.entries(mappings).find(([, name]) => name === 'system_chat')[0]);
  const before = downstream.length;
  upstream.write(packet(chatId, Buffer.from('hidden-team-message'), true));
  client.write(packet(5, text('hidden-outbound-message'), true));
  await delay(30);
  assert.equal(downstream.length, before); assert.equal(f.proxy.stats.rejected, 0); assert.equal(f.proxy.stats.filtered, 2); client.destroy();
});

test('movement sanitizer rejects non-finite and truncated position packets', () => {
  const finite = Buffer.alloc(34);
  finite[0] = 0x17;
  finite.writeDoubleBE(1, 1); finite.writeDoubleBE(64, 9); finite.writeDoubleBE(2, 17); finite[25] = 1;
  assert.equal(inspectMovement(finite, 1, 'position'), null);

  const nan = Buffer.from(finite); nan.writeDoubleBE(NaN, 9);
  assert.equal(inspectMovement(nan, 1, 'position').reason, 'nonfinite');

  const far = Buffer.from(finite); far.writeDoubleBE(40_000_000, 1);
  assert.equal(inspectMovement(far, 1, 'position').reason, 'out-of-range');

  assert.equal(inspectMovement(Buffer.alloc(5), 1, 'position').reason, 'truncated');
});


test('serverbound chat strings over 256 bytes are rejected before the server', () => {
  const ok = Buffer.concat([varint(256), Buffer.alloc(256)]);
  assert.equal(inspectChat(ok, 0, 'chat_message'), null);
  const long = Buffer.concat([varint(257), Buffer.alloc(257)]);
  assert.deepEqual(inspectChat(long, 0, 'chat_message'), { reason: 'string-too-long', length: 257 });
  const short = Buffer.concat([varint(10), Buffer.alloc(3)]);
  assert.deepEqual(inspectChat(short, 0, 'chat_command'), { reason: 'truncated', length: 10 });
  assert.equal(inspectChat(long, 0, 'position'), null);
});
