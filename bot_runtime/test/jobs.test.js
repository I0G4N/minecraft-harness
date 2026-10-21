import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import { start } from '../src/server.js';

async function fixture(t, options = {}) {
  const runtime = await start({ port: 0, mode: 'fake', token: 'job-test-supervisor-token', lifecycle: 'paper_aligned', startupTimeoutMs: 2000, ...options });
  t.after(() => runtime.close());
  async function req(method, path, body) {
    const response = await fetch(`http://127.0.0.1:${runtime.address.port}${path}`, { method, headers: { authorization: 'Bearer job-test-supervisor-token', 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  }
  const created = await req('POST', '/v1/sessions', { agent_id: 'alice', remaining_run_ms: 10000 });
  assert.equal(created.status, 201);
  const base = '/v1/sessions/alice';
  async function submit(source, timeout_ms = 1000) {
    const result = await req('POST', `${base}/programs`, { source, request_id: crypto.randomUUID(), run_id: 'development', world_epoch: 1, timeout_ms });
    assert.equal(result.status, 202, JSON.stringify(result));
    return result.body.execution_id;
  }
  async function wait(path, terminal = value => !['accepted', 'running', 'cancelling'].includes(value.status)) {
    const deadline = Date.now() + 4000;
    while (Date.now() < deadline) {
      const result = await req('GET', path);
      if (terminal(result.body)) return result.body;
      await delay(10);
    }
    throw new Error('wait timed out');
  }
  const run = async source => wait(`${base}/programs/${await submit(source)}`);
  return { req, base, submit, wait, run };
}

test('registered timers, listeners and logs survive foreground; unregistered resources are cleaned', async t => {
  const f = await fixture(t);
  const first = await f.run(`globals.ticks=0; globals.job=jobs.start(()=>{setInterval(()=>{globals.ticks++;console.log('background tick')},10);bot.on('chat',()=>globals.heard=true)}, {ttl_ms:4000});setInterval(()=>globals.leaked=true,1);return globals.job`);
  assert.equal(first.status, 'succeeded');
  assert.equal(first.cleanup.timers_cleared, 1);
  await delay(60);
  const next = await f.run(`return {ticks:globals.ticks,leaked:globals.leaked??false, jobs:jobs.list()}`);
  assert.ok(next.result.ticks > 0);
  assert.equal(next.result.leaked, false);
  assert.equal(next.runtime_epoch, first.runtime_epoch);
  assert.equal(next.result.jobs[0].status, 'running');
  const listed = await f.req('GET', f.base + '/jobs');
  assert.equal(listed.body.jobs.length, 1);
  assert.ok(listed.body.jobs[0].logs.length > 0);
  const stopped = await f.req('POST', `${f.base}/jobs/${first.result}/cancel`, {});
  assert.equal(stopped.body.status, 'cancelled');
  const ticks = (await f.run('return globals.ticks')).result;
  await delay(50);
  assert.equal((await f.run('return globals.ticks')).result, ticks);
  assert.equal((await f.run('return jobs.status(globals.job).status')).runtime_epoch, 1);
  const events = (await f.req('GET', f.base + '/events?after_cursor=0&limit=1000')).body.events;
  assert.ok(events.some(event => event.kind === 'job_log' && event.payload.job_id === first.result && event.execution_id === first.execution_id));
});

test('cooperative cancellation observes dynamic job isCancelled and preserves globals', async t => {
  const f = await fixture(t);
  const result = await f.run(`globals.marker=42;return jobs.start(async()=>{while(!isCancelled()) await bot.fake.wait(5); globals.cancelled=true}, {ttl_ms:4000})`);
  const cancelled = await f.req('POST', `${f.base}/jobs/${result.result}/cancel`, {});
  assert.equal(cancelled.body.status, 'cancelled');
  const next = await f.run('return {marker:globals.marker,cancelled:globals.cancelled}');
  assert.deepEqual(next.result, { marker: 42, cancelled: true });
  assert.equal(next.runtime_epoch, 1);
});

test('noncooperative task is fenced within cancellation grace, all tasks lose globals and never replay', async t => {
  const f = await fixture(t);
  const result = await f.run(`globals.marker=42;globals.other=jobs.start(()=>{setInterval(()=>{},100)}, {ttl_ms:4000});return jobs.start(async()=>{await bot.fake.wait(20);while(true){}},{ttl_ms:4000})`);
  await delay(50);
  const begin = Date.now();
  const cancelled = await f.req('POST', `${f.base}/jobs/${result.result}/cancel`, {});
  assert.equal(cancelled.body.status, 'cancelled');
  assert.ok(Date.now() - begin < 1700);
  const next = await f.run('return {marker:typeof globals.marker,jobs:jobs.list()}');
  assert.equal(next.result.marker, 'undefined');
  assert.deepEqual(next.result.jobs, []);
  assert.equal(next.runtime_epoch, 2);
  const tasks = (await f.req('GET', f.base + '/jobs')).body.jobs;
  assert.equal(tasks.find(job => job.job_id !== result.result).status, 'interrupted');
});

test('job TTL and session pause fence background work', async t => {
  const f = await fixture(t);
  const result = await f.run(`globals.marker=1;return jobs.start(()=>{setInterval(()=>{},10)},{ttl_ms:80})`);
  const expired = await f.wait(`${f.base}/jobs/${result.result}`);
  assert.equal(expired.status, 'timed_out');
  const second = await f.run(`return jobs.start(()=>{setInterval(()=>{},10)},{ttl_ms:4000})`);
  const paused = await f.req('POST', f.base + '/pause', {});
  assert.equal(paused.body.connected, false);
  assert.equal((await f.req('GET', `${f.base}/jobs/${second.result}`)).body.status, 'interrupted');
  await f.req('POST', f.base + '/resume', {});
  assert.deepEqual((await f.run('return jobs.list()')).result, []);
});

test('job count, TTL and strict lifecycle are validated before registration', async t => {
  const f = await fixture(t, { maxBackgroundJobs: 2 });
  const result = await f.run(`jobs.start(()=>{setInterval(()=>{},100)},{ttl_ms:4000});jobs.start(()=>{setInterval(()=>{},100)},{ttl_ms:4000});jobs.start(()=>{},{ttl_ms:4000})`);
  assert.equal(result.status, 'failed');
  assert.match(result.error.message, /limit/);
  assert.equal((await f.req('GET', f.base + '/jobs')).body.jobs.length, 2);
  assert.match((await f.run('return jobs.start(()=>{},{ttl_ms:100000})')).error.message, /budget/);
  const strict = await fixture(t, { lifecycle: 'strict' });
  const disabled = await strict.run('return jobs.start(()=>{},{ttl_ms:100})');
  assert.equal(disabled.status, 'failed');
  assert.match(disabled.error.message, /paper_aligned/);
});

test('paper foreground cancellation grants cooperative grace while strict remains hard fenced', async t => {
  const f = await fixture(t);
  const id = await f.submit('globals.marker=7;while(!isCancelled()) await bot.fake.wait(5);globals.cancelled=true;return 1', 4000);
  await f.wait(`${f.base}/programs/${id}`, record => record.status === 'running');
  assert.equal((await f.req('POST', `${f.base}/programs/${id}/cancel`, {})).body.status, 'cancelled');
  const next = await f.run('return {marker:globals.marker,cancelled:globals.cancelled}');
  assert.equal(next.runtime_epoch, 1);
  assert.deepEqual(next.result, { marker: 7, cancelled: true });
});

test('foreground controls are cleaned without stopping controls owned by other registered jobs', async t => {
  const f = await fixture(t);
  const created = await f.run(`globals.bg=jobs.start(()=>{bot.setControlState('jump',true);setInterval(()=>{},30)},{ttl_ms:4000});bot.setControlState('forward',true);return globals.bg`);
  assert.equal(created.status, 'succeeded');
  const state = await f.run('return bot.controlState');
  assert.equal(state.result.forward, false);
  assert.equal(state.result.jump, true);
  assert.equal(state.runtime_epoch, 1);
  assert.equal(created.cleanup.movement_controls_stopped, 1);
  await f.req('POST', `${f.base}/jobs/${created.result}/cancel`, {});
  const stopped = await f.run('return bot.controlState');
  assert.ok(!Object.values(stopped.result).some(Boolean));
  assert.equal(stopped.runtime_epoch, 1);
});
