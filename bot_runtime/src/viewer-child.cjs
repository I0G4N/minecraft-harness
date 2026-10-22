'use strict';
// Isolated prismarine-viewer worker thread. It never owns the Minecraft bot or
// the execution channel; a viewer exception is handled by the parent worker,
// which recreates this thread and keeps the experiment running.
const { parentPort, workerData } = require('node:worker_threads');
const express = require('express');
const http = require('http');
const { Vec3 } = require('vec3');
const { WorldView } = require('prismarine-viewer/viewer');
const { setupRoutes } = require('prismarine-viewer/lib/common');

const config = workerData || {};
const port = Number(config.port);
const bind = config.bind || '127.0.0.1';
const viewDistance = Math.max(1, Math.min(8, Number(config.viewDistance) || 2));
const firstPerson = config.firstPerson !== false;
const version = config.version || '1.20.4';

const chunks = new Map();
const sockets = new Set();
const views = new Map();
let latest = { pos: new Vec3(0, 64, 0), yaw: 0, pitch: 0 };
let stopping = false;

function send(message) {
  try { parentPort.postMessage(message); } catch { /* parent is gone */ }
}
function keyFor(x, z) { return `${Math.floor(x / 16)},${Math.floor(z / 16)}`; }

// WorldView only needs getColumnAt(). Returning an object with toJson() keeps
// the viewer protocol identical while avoiding a second world implementation.
const world = {
  async getColumnAt(pos) {
    const json = chunks.get(keyFor(pos.x, pos.z));
    return json ? { toJson: () => json } : null;
  },
  raycast() { return null; }
};

const app = express();
app.use((request, response, next) => {
  response.setHeader('Cross-Origin-Opener-Policy', 'same-origin');
  response.setHeader('Cross-Origin-Embedder-Policy', 'require-corp');
  response.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
  next();
});
setupRoutes(app, '');
const server = http.createServer(app);
const io = require('socket.io')(server, { path: '/socket.io' });

function positionPacket() {
  const packet = { pos: { x: latest.pos.x, y: latest.pos.y, z: latest.pos.z }, yaw: latest.yaw, addMesh: true };
  if (firstPerson) packet.pitch = latest.pitch;
  return packet;
}

io.on('connection', (socket) => {
  sockets.add(socket);
  socket.emit('version', version);
  const view = new WorldView(world, viewDistance, latest.pos, socket);
  views.set(socket, view);
  view.init(latest.pos).catch((error) => send({ type: 'viewer_warning', error: String(error?.message || error).slice(0, 500) }));
  socket.emit('position', positionPacket());
  socket.on('disconnect', () => { sockets.delete(socket); views.delete(socket); });
});

function broadcast(message) {
  for (const socket of sockets) {
    try { socket.emit(message.type, message.payload); } catch { /* one broken socket must not kill the viewer */ }
  }
}

function applyChunk(x, z, json) {
  chunks.set(`${x},${z}`, json);
  for (const view of views.values()) {
    try { view.loadChunk(new Vec3(x * 16, 0, z * 16)); } catch { /* the viewer retries on its own snapshot */ }
  }
}

function handleMessage(message) {
  if (!message || typeof message !== 'object' || stopping) return;
  if (message.type === 'chunks') {
    for (const item of message.chunks || []) applyChunk(item.x, item.z, item.json);
  } else if (message.type === 'chunk') {
    applyChunk(message.x, message.z, message.json);
  } else if (message.type === 'position') {
    latest = { pos: new Vec3(message.pos.x, message.pos.y, message.pos.z), yaw: Number(message.yaw) || 0, pitch: Number(message.pitch) || 0 };
    for (const view of views.values()) view.updatePosition(latest.pos).catch(() => {});
    broadcast({ type: 'position', payload: positionPacket() });
  } else if (message.type === 'entity') {
    broadcast({ type: 'entity', payload: message.entity });
  } else if (message.type === 'blockUpdate') {
    broadcast({ type: 'blockUpdate', payload: message.block });
  } else if (message.type === 'shutdown') {
    close();
  }
}

function close() {
  if (stopping) return;
  stopping = true;
  try { for (const socket of sockets) socket.disconnect(); } catch {}
  try { io.close(); } catch {}
  try { server.close(); } catch {}
  try { parentPort.close(); } catch {}
}

parentPort.on('message', handleMessage);
parentPort.on('close', close);
server.on('error', (error) => send({ type: 'viewer_fatal', error: String(error?.message || error).slice(0, 500) }));
server.listen(port, bind, () => send({ type: 'viewer_ready', port, bind }));
const heartbeat = setInterval(() => send({ type: 'viewer_heartbeat' }), 3000);
heartbeat.unref();

process.on('uncaughtException', (error) => {
  send({ type: 'viewer_fatal', error: String(error?.stack || error).slice(0, 1500) });
  close();
});
process.on('unhandledRejection', (error) => {
  send({ type: 'viewer_fatal', error: String(error?.stack || error).slice(0, 1500) });
  close();
});
