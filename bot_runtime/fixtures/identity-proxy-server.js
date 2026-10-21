// Test-only metrics wrapper around the production identity proxy implementation.
import http from 'node:http';
import { startIdentityProxy } from './src/identity-proxy.js';
const proxy = await startIdentityProxy({ host: '0.0.0.0', port: 25565, targetHost: process.env.MC_HOST ?? 'fixture', targetPort: 25565, username: 'protoAlice', gameChat: false });
const metrics = http.createServer((request, response) => { response.setHeader('content-type', 'application/json'); response.end(JSON.stringify(proxy.stats)); });
metrics.listen(8081, '127.0.0.1');
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => proxy.close().then(() => metrics.close(() => process.exit(0))));
