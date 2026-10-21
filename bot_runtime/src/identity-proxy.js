// Fixed Vanilla 1.20.4 offline identity gate. It forwards original framed packets,
// validates login before any upstream connection, and filters chat at the wire.
import net from 'node:net';
import { inflateSync } from 'node:zlib';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const data = require('minecraft-data')('1.20.4');
const MAX_PACKET = 2 * 1024 ** 2;
export function varint(value) {
  const bytes = []; value >>>= 0;
  do { let byte = value & 127; value >>>= 7; if (value) byte |= 128; bytes.push(byte); } while (value);
  return Buffer.from(bytes);
}
export function readVarint(buffer, offset = 0) {
  let value = 0;
  for (let i = 0; i < 5; i++) {
    if (offset + i >= buffer.length) return null;
    const byte = buffer[offset + i];
    value |= (byte & 127) << (7 * i);
    if (!(byte & 128)) return { value: value >>> 0, end: offset + i + 1 };
  }
  throw new Error('Oversized VarInt');
}
function stringAt(buffer, offset) {
  const length = readVarint(buffer, offset);
  if (!length || length.value > 1024 || length.end + length.value > buffer.length) throw new Error('Malformed login string');
  return { value: buffer.subarray(length.end, length.end + length.value).toString('utf8'), end: length.end + length.value };
}
export class PacketFrames {
  constructor(consume) { this.consume = consume; this.buffer = Buffer.alloc(0); }
  push(chunk) {
    if (this.buffer.length + chunk.length > MAX_PACKET * 2 + 10) throw new Error('Packet buffer limit');
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (this.buffer.length) {
      const size = readVarint(this.buffer);
      if (!size) return;
      if (size.value < 1 || size.value > MAX_PACKET) throw new Error('Packet length limit');
      if (this.buffer.length < size.end + size.value) return;
      const raw = this.buffer.subarray(0, size.end + size.value);
      const payload = raw.subarray(size.end);
      this.buffer = this.buffer.subarray(raw.length);
      this.consume(payload, raw);
    }
  }
}
const mappings = (state, direction) => data.protocol[state]?.[direction]?.types.packet[1][0].type[1].mappings;
function packetName(state, direction, id) {
  return mappings(state, direction)?.['0x' + id.toString(16).padStart(2, '0')];
}
function unpack(payload, compression) {
  if (!compression) return payload;
  const length = readVarint(payload);
  if (!length) throw new Error('Malformed compressed frame');
  if (length.value === 0) return payload.subarray(length.end);
  if (length.value > MAX_PACKET) throw new Error('Inflated packet limit');
  const body = inflateSync(payload.subarray(length.end), { maxOutputLength: MAX_PACKET });
  if (body.length !== length.value) throw new Error('Inflated length mismatch');
  return body;
}

export async function startIdentityProxy({ host = '127.0.0.1', port = 0, targetHost,
  targetPort = 25565, username, gameChat = true, connectTimeoutMs = 5000, idleTimeoutMs = 90000 } = {}) {
  if (!/^[A-Za-z0-9_]{1,16}$/.test(username ?? '') || !targetHost || !Number.isInteger(targetPort) || targetPort < 1 || targetPort > 65535) throw new Error('Fixed target and username required');
  const connections = new Set(); let active = null;
  const stats = { accepted: 0, rejected: 0, filtered: 0 };
  const server = net.createServer(client => {
    client.setNoDelay(true);
    // Even pre-login sockets are bounded; an idle connection cannot allocate
    // unbounded parsers or upstream sockets.
    if (connections.size >= 8) { client.destroy(); stats.rejected++; return; }
    connections.add(client);
    let upstream = null, state = 'handshaking', compression = false, pending = [], queuedBytes = 0;
    let closed = false; let connectTimer;
    const close = () => {
      if (closed) return; closed = true;
      clearTimeout(connectTimer); client.destroy(); upstream?.destroy();
      connections.delete(client); if (active === client) active = null;
    };
    const reject = () => { stats.rejected++; close(); };
    const write = (destination, raw, source) => {
      if (!destination.write(raw)) { source.pause(); destination.once('drain', () => { if (!closed) source.resume(); }); }
    };
    const sendUpstream = raw => {
      if (!upstream || upstream.connecting) {
        queuedBytes += raw.length;
        if (queuedBytes > MAX_PACKET) throw new Error('Pre-connect queue limit');
        pending.push(Buffer.from(raw));
      } else write(upstream, raw, client);
    };
    const incoming = new PacketFrames((payload, raw) => {
      const body = unpack(payload, compression), id = readVarint(body);
      if (!id) throw new Error('Missing packet ID');
      const name = packetName(state, 'toServer', id.value);
      if (!name) throw new Error('Unknown client packet');
      if (state === 'handshaking') {
        if (name !== 'set_protocol') throw new Error('Login handshake required');
        const version = readVarint(body, id.end);
        if (!version || version.value !== 765) throw new Error('Only 1.20.4 supported');
        const address = stringAt(body, version.end);
        const next = readVarint(body, address.end + 2);
        if (!next || next.value !== 2 || next.end !== body.length) throw new Error('Only login permitted');
        state = 'login'; pending.push(Buffer.from(raw)); queuedBytes += raw.length; return;
      }
      if (!upstream) {
        if (name !== 'login_start') throw new Error('Login start required');
        const loginName = stringAt(body, id.end);
        if (loginName.value !== username || active) throw new Error('Player identity or active connection rejected');
        active = client; stats.accepted++;
        upstream = net.createConnection({ host: targetHost, port: targetPort });
        upstream.setNoDelay(true);
        upstream.setTimeout(idleTimeoutMs, close);
        upstream.on('error', close); upstream.on('close', close);
        connectTimer = setTimeout(reject, connectTimeoutMs);
        upstream.once('connect', () => {
          clearTimeout(connectTimer);
          for (const frame of pending) write(upstream, frame, client);
          pending = []; queuedBytes = 0;
        });
        const outgoing = new PacketFrames((encoded, frame) => {
          const decoded = unpack(encoded, compression), packet = readVarint(decoded);
          if (!packet) throw new Error('Missing upstream packet');
          const kind = packetName(state, 'toClient', packet.value);
          if (!kind) throw new Error('Unknown upstream packet');
          if (state === 'login' && kind === 'encryption_begin') throw new Error('Offline research backend required');
          if (state === 'login' && kind === 'compress') compression = true;
          if (!gameChat && state === 'play' && ['player_chat', 'system_chat', 'profileless_chat', 'disguised_chat', 'chat', 'tab_complete', 'declare_commands'].includes(kind)) { stats.filtered++; return; }
          write(client, frame, upstream);
        });
        upstream.on('data', chunk => { try { outgoing.push(chunk); } catch { reject(); } });
      }
      if (!gameChat && state === 'play' && ['chat_message', 'chat_command', 'tab_complete'].includes(name)) { stats.filtered++; return; }
      sendUpstream(raw);
      if (state === 'login' && name === 'login_acknowledged') state = 'configuration';
      else if (state === 'configuration' && name === 'finish_configuration') state = 'play';
      else if (state === 'play' && name === 'configuration_acknowledged') state = 'configuration';
    });
    client.setTimeout(Math.min(idleTimeoutMs, 15000), close);
    client.on('data', chunk => { try { incoming.push(chunk); if (upstream) client.setTimeout(idleTimeoutMs); } catch { reject(); } });
    client.on('error', close); client.on('close', close);
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, host, resolve); });
  return { server, address: server.address(), stats,
    async close() { for (const client of connections) client.destroy(); await new Promise(resolve => server.close(resolve)); } };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const proxy = await startIdentityProxy({ host: process.env.BIND ?? '0.0.0.0', port: Number(process.env.PORT ?? 25565),
    targetHost: process.env.MC_HOST ?? 'server', targetPort: Number(process.env.MC_PORT ?? 25565),
    username: process.env.ALLOWED_USERNAME, gameChat: process.env.GAME_CHAT !== 'false' });
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => proxy.close().then(() => process.exit(0)));
}
