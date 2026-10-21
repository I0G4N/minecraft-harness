// Offline endurance/fault-injection exercise. Never starts a Minecraft server.
import { start } from '../src/server.js';
import { setTimeout as delay } from 'node:timers/promises';
import { performance } from 'node:perf_hooks';

const argumentsMap = new Map();
for (let i = 2; i < process.argv.length; i += 2) argumentsMap.set(process.argv[i], Number(process.argv[i + 1]));
const cycles = argumentsMap.get('--cycles') ?? 2000;
const agents = argumentsMap.get('--agents') ?? 4;
const faultEvery = argumentsMap.get('--fault-every') ?? 100;
const seconds = argumentsMap.get('--duration-seconds') ?? 0;
if (![cycles, agents, faultEvery, seconds].every(Number.isSafeInteger) || cycles < 1 || agents < 1 || agents > 16 || faultEvery < 0 || seconds < 0) throw new Error('Invalid soak arguments');
const token = 'offline-endurance-supervisor-token';
const runtime = await start({ mode: 'fake', port: 0, token, maxRetainedExecutions: 16, maxQueue: 8, maxEvents: 256 });
const origin = `http://127.0.0.1:${runtime.address.port}`;
const begun = performance.now();
let peakRss = 0;
let peakWorkerRss = 0;
let peakEvents = 0;
let peakExecutions = 0;
const memorySamples = [];
let lastMemorySample = -1000;
let completed = 0;
let faults = 0;
let failure;
const identities = new Map();
const cursors = new Map();
async function request(method, path, body) {
  const response = await fetch(origin + path, { method, headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(`${method} ${path}: ${response.status} ${JSON.stringify(result)}`);
  return result;
}
async function collect(agent) {
  const base = `/v1/sessions/${agent}`;
  let feed;
  do {
    feed = await request('GET', `${base}/events?after_cursor=${cursors.get(agent) ?? 0}&limit=1000`);
    if (feed.reliability_fault) throw new Error(JSON.stringify(feed.reliability_fault));
    cursors.set(agent, feed.next_cursor);
    // This script itself is ephemeral. Production callers must commit events and
    // the cursor to their durable recorder BEFORE sending this acknowledgement.
    await request('POST', base + '/events/ack', { ...identities.get(agent), cursor: feed.next_cursor });
  } while (feed.has_more);
}
async function sample() {
  const health = await request('GET', '/health');
  peakRss = Math.max(peakRss, health.memory.rss);
  const elapsed = performance.now() - begun;
  if (elapsed - lastMemorySample >= 1000) {
    lastMemorySample = elapsed;
    memorySamples.push({ seconds: Number((elapsed / 1000).toFixed(1)), rss_mib: Number((health.memory.rss / 1048576).toFixed(1)), heap_used_mib: Number((health.memory.heapUsed / 1048576).toFixed(1)) });
    if (memorySamples.length > 120) memorySamples.shift();
  }
  for (const state of health.session_metrics) {
    if (state.reliability_fault || state.lease_expired) throw new Error('Runtime lost reliability: ' + JSON.stringify(state));
    peakWorkerRss = Math.max(peakWorkerRss, state.worker_rss_bytes ?? 0);
    peakEvents = Math.max(peakEvents, state.buffers.events);
    peakExecutions = Math.max(peakExecutions, state.buffers.executions);
    if (state.buffers.events > 256 || state.buffers.executions > 25) throw new Error('Configured cache limit exceeded');
  }
  return health;
}
let heartbeat;
try {
  for (let i = 0; i < agents; i++) {
    const agent = `soak${i}`;
    const state = await request('POST', '/v1/sessions', { agent_id: agent, run_id: 'offline-soak', world_epoch: 1 });
    identities.set(agent, { session_id: state.session_id, run_id: state.run_id, world_epoch: state.world_epoch });
  }
  heartbeat = setInterval(() => {
    Promise.all([...identities].map(([agent, identity]) => request('POST', `/v1/sessions/${agent}/heartbeat`, identity))).catch(error => { failure = error; });
  }, 1000);
  const initial = await sample();
  console.log(JSON.stringify({ event: 'soak_started', agents, cycles, fault_every: faultEvery, duration_seconds: seconds, rss_bytes: initial.memory.rss }));
  await Promise.all([...identities].map(async ([agent, identity]) => {
    for (let cycle = 1; cycle <= cycles || (seconds > 0 && performance.now() - begun < seconds * 1000); cycle++) {
      if (failure) throw failure;
      const isFault = faultEvery > 0 && cycle % faultEvery === 0;
      const source = isFault ? 'while(true){}' : `globals.count=(globals.count??0)+1; console.log("soak",${cycle}); bot.chat("soak-${cycle}"); return {cycle:${cycle}}`;
      const record = await request('POST', `/v1/sessions/${agent}/programs`, { ...identity, source,
        request_id: `${agent}-${cycle}`, request_sequence: cycle, timeout_ms: isFault ? 30 : 1000 });
      let terminal;
      const deadline = performance.now() + 5000;
      do {
        terminal = await request('GET', `/v1/sessions/${agent}/programs/${record.execution_id}`);
        if (!['accepted', 'running'].includes(terminal.status)) break;
        if (performance.now() > deadline) throw new Error('Terminal state deadline exceeded');
        await delay(2);
      } while (true);
      if (isFault) {
        if (terminal.status !== 'timed_out') throw new Error('Injected fault did not time out');
        faults++;
      } else if (terminal.status !== 'succeeded' || terminal.result.cycle !== cycle) throw new Error('Wrong program result: ' + JSON.stringify(terminal));
      completed++;
      await collect(agent);
      if (cycle % 100 === 0) await sample();
    }
  }));
  for (const agent of identities.keys()) await collect(agent);
  const final = await sample();
  console.log(JSON.stringify({ event: 'soak_passed', mode: 'fake', completed, injected_timeouts: faults,
    elapsed_seconds: Number(((performance.now() - begun) / 1000).toFixed(3)), initial_rss_bytes: initial.memory.rss,
    final_rss_bytes: final.memory.rss, peak_rss_bytes: peakRss, peak_worker_rss_bytes: peakWorkerRss,
    memory_samples: memorySamples, peak_sampled_buffered_events: peakEvents, peak_sampled_execution_records: peakExecutions,
    final_sessions: final.session_metrics.map(state => ({ agent_id: state.agent_id, ...state.buffers })) }));
} finally {
  clearInterval(heartbeat);
  await runtime.close();
}
