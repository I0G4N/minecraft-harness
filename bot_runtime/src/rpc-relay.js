// Fixed Minecraft-harness RPC routes. No HTTP proxy, target parameters, tokens,
// model access, or world access. Only the relay joins the ordinary RPC network.
import net from 'node:net';
import { fileURLToPath } from 'node:url';

const ROUTES = Object.freeze([1, 2, 3, 4].map(index => Object.freeze({ agent: `bot${index}`, listen_port: 3000 + index, host: `bot${index}`, port: 3001 })));
export const fixedRoutes = () => ROUTES.map(route => ({ ...route }));

function positive(value, fallback, name) {
  const result = Number(value ?? fallback);
  if (!Number.isSafeInteger(result) || result < 1) throw new Error(`${name} must be a positive integer`);
  return result;
}

export async function startRelay(options = {}) {
  const enabled = options.enabledAgents ?? (process.env.RELAY_ENABLED_AGENTS ?? 'bot1,bot2,bot3,bot4').split(',');
  if (!Array.isArray(enabled) || !enabled.length || new Set(enabled).size !== enabled.length || enabled.some(agent => !ROUTES.some(route => route.agent === agent))) throw new Error('Only distinct fixed agents bot1,bot2,bot3,bot4 may be enabled');
  const maxConnections = positive(options.maxConnections ?? process.env.RELAY_MAX_CONNECTIONS, 128, 'RELAY_MAX_CONNECTIONS');
  const maxPerRoute = positive(options.maxPerRoute ?? process.env.RELAY_MAX_PER_ROUTE, 32, 'RELAY_MAX_PER_ROUTE');
  const idleTimeoutMs = positive(options.idleTimeoutMs ?? process.env.RELAY_IDLE_TIMEOUT_MS, 90000, 'RELAY_IDLE_TIMEOUT_MS');
  const connectTimeoutMs = positive(options.connectTimeoutMs ?? process.env.RELAY_CONNECT_TIMEOUT_MS, 5000, 'RELAY_CONNECT_TIMEOUT_MS');
  const bind = options.bind ?? process.env.RELAY_BIND ?? '0.0.0.0';
  // Dependency injection is local test code only; no request or environment can
  // choose an upstream host/port. Production always uses the routes above.
  const connect = options.connect ?? (parameters => net.createConnection(parameters));
  const pairs = new Set();
  const servers = new Map();
  const counts = new Map(enabled.map(agent => [agent, 0]));
  let closing = false;
  let closePromise;
  let rejected = 0;
  let upstreamErrors = 0;
  async function close() {
    if (closePromise) return closePromise;
    closing = true;
    closePromise = (async () => {
      const closed = [...servers.values()].map(server => new Promise(resolve => server.close(resolve)));
      for (const pair of [...pairs]) pair.destroy();
      await Promise.all(closed);
    })();
    return closePromise;
  }
  try {
    for (const route of ROUTES.filter(route => enabled.includes(route.agent))) {
      const server = net.createServer({ allowHalfOpen: true, highWaterMark: 65536 }, client => {
        if (closing || pairs.size >= maxConnections || counts.get(route.agent) >= maxPerRoute) { rejected++; client.destroy(); return; }
        client.pause();
        client.setNoDelay(true);
        const pair = { client, upstream: null, timer: null, released: false, destroy() {
          clearTimeout(this.timer); this.client.destroy(); this.upstream?.destroy(); release();
        } };
        pairs.add(pair); counts.set(route.agent, counts.get(route.agent) + 1);
        function release() {
          if (pair.released) return;
          if (!client.destroyed || (pair.upstream && !pair.upstream.destroyed)) return;
          pair.released = true;
          clearTimeout(pair.timer);
          pairs.delete(pair); counts.set(route.agent, counts.get(route.agent) - 1);
        }
        client.on('error', () => pair.destroy());
        client.on('close', () => { pair.upstream?.destroy(); release(); });
        client.setTimeout(idleTimeoutMs, () => pair.destroy());
        try {
          pair.upstream = connect({ host: route.host, port: route.port, allowHalfOpen: true, highWaterMark: 65536 });
          const upstream = pair.upstream;
          upstream.setNoDelay(true);
          upstream.setTimeout(idleTimeoutMs, () => pair.destroy());
          pair.timer = setTimeout(() => pair.destroy(), connectTimeoutMs);
          upstream.once('connect', () => {
            clearTimeout(pair.timer);
            if (closing || client.destroyed) { pair.destroy(); return; }
            // pipe() pauses the readable side when the peer's writable buffer
            // fills; neither direction builds an application-level data queue.
            client.pipe(upstream); upstream.pipe(client); client.resume();
          });
          upstream.on('error', () => { upstreamErrors++; pair.destroy(); });
          upstream.on('close', hadError => {
            clearTimeout(pair.timer);
            if (hadError) pair.destroy();
            else if (!client.destroyed) client.end();
            release();
          });
        } catch {
          upstreamErrors++;
          pair.destroy(); release();
        }
      });
      servers.set(route.agent, server);
      // Tests use ephemeral listener ports, while the CLI has fixed 3001..3004.
      await new Promise((resolve, reject) => { server.once('error', reject); server.listen(options.listenPorts?.[route.agent] ?? route.listen_port, bind, resolve); });
      server.on('error', () => { close().catch(() => {}); });
    }
  } catch (error) { await close(); throw error; }
  return { close,
    addresses: Object.fromEntries([...servers].map(([agent, server]) => [agent, server.address()])),
    describe() { return { active_connections: pairs.size, rejected_connections: rejected, upstream_errors: upstreamErrors,
      routes: ROUTES.filter(route => enabled.includes(route.agent)).map(route => ({ ...route, active_connections: counts.get(route.agent) })),
      max_connections: maxConnections, max_per_route: maxPerRoute, idle_timeout_ms: idleTimeoutMs, connect_timeout_ms: connectTimeoutMs }; } };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fileURLToPath(new URL(`file://${process.argv[1]}`))) {
  const relay = await startRelay();
  console.log(JSON.stringify({ event: 'rpc_relay_listening', addresses: relay.addresses, ...relay.describe() }));
  let closing = false;
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, async () => {
    if (closing) return;
    closing = true;
    await relay.close();
  });
}
