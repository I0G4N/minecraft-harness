// EULA-free deterministic protocol fixture. No Vanilla game simulation, physics,
// advancement or capability evidence. Serves synthetic chunks and inventory.
import { createRequire } from 'node:module';
import http from 'node:http';
const require = createRequire(import.meta.url);
const protocol = require('minecraft-protocol'), data = require('minecraft-data')('1.20.4');
const nbt = require('prismarine-nbt'), { Vec3 } = require('vec3');
const Chunk = require('prismarine-chunk')('1.20.4');
const chunk = new Chunk({ minY: -64, worldHeight: 384 });
for (let x = 0; x < 16; x++) for (let z = 0; z < 16; z++) chunk.setBlockStateId(new Vec3(x, 63, z), data.blocksByName.grass_block.minStateId);
const stats = { kind: 'synthetic_protocol_fixture_not_vanilla', joined: [], ended: [], chats: [], chat_packets_sent: 0, errors: [] };
const game = protocol.createServer({ host: '0.0.0.0', port: 25565, version: '1.20.4', 'online-mode': false, encryption: false, enforceSecureProfile: false, registryCodec: data.loginPacket.dimensionCodec, hideErrors: true });
game.on('error', error => stats.errors.push(String(error.message).slice(0, 1000)));
game.on('playerJoin', client => {
  stats.joined.push({ username: client.username, id: client.id });
  client.on('error', error => stats.errors.push(String(error.message).slice(0, 1000)));
  client.on('end', reason => stats.ended.push({ username: client.username, id: client.id, reason }));
  client.on('chat_message', packet => stats.chats.push({ username: client.username, message: packet.message }));
  client.write('login', { ...data.loginPacket, entityId: client.id, gameMode: 0, hashedSeed: [0, 42] });
  client.write('difficulty', { difficulty: 1, difficultyLocked: true });
  client.write('map_chunk', { x: 0, z: 0, heightmaps: nbt.comp({}), chunkData: chunk.dump(), blockEntities: [], skyLightMask: [], blockLightMask: [], emptySkyLightMask: [], emptyBlockLightMask: [], skyLight: [], blockLight: [] });
  client.write('position', { x: 8.5, y: 64, z: 8.5, yaw: 0, pitch: 0, flags: 0, teleportId: 1 });
  const items = Array.from({ length: 46 }, () => ({ present: false }));
  items[36] = { present: true, itemId: data.itemsByName.oak_log.id, itemCount: 3 };
  client.write('window_items', { windowId: 0, stateId: 1, items, carriedItem: { present: false } });
  client.write('update_health', { health: 20, food: 20, foodSaturation: 5 });
});
const broadcasts = setInterval(() => {
  for (const client of Object.values(game.clients)) if (client.state === protocol.states.PLAY) {
    client.write('system_chat', { content: nbt.comp({ text: nbt.string('<fixturePeer> wire-ablation-fixture-message') }), isActionBar: false });
    stats.chat_packets_sent++;
  }
}, 100);
const httpServer = http.createServer((request, response) => { response.setHeader('content-type', 'application/json'); response.end(JSON.stringify(stats)); });
httpServer.listen(8080, '0.0.0.0');
async function close() { clearInterval(broadcasts); game.close(); httpServer.close(); }
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => close().then(() => process.exit(0)));
