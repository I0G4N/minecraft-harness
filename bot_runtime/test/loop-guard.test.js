import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import * as acorn from '../src/vendor/acorn.mjs';
import { instrumentLoopDeadlines } from '../src/loop-guard.mjs';

const WRAP_PREFIX = '(async function(){\n';
const WRAP_SUFFIX = '\n})';

function parseWrapped(source) {
  return acorn.parse(WRAP_PREFIX + source + WRAP_SUFFIX, {
    ecmaVersion: 'latest', sourceType: 'script',
    allowAwaitOutsideFunction: false, allowReturnOutsideFunction: false,
  });
}

function assertParses(source) {
  assert.doesNotThrow(() => parseWrapped(source), source.slice(0, 200));
}

function countChecks(source) {
  return (source.match(/__mcWindowCheck/g) ?? []).length;
}

async function runInstrumented(source, extraGlobals = {}) {
  const instrumented = instrumentLoopDeadlines(source);
  const context = vm.createContext({ ...extraGlobals, __mcWindowCheck: () => {} });
  const fn = vm.runInContext(WRAP_PREFIX + instrumented + WRAP_SUFFIX, context, { filename: 'loop-guard-test.js' });
  return { instrumented, result: await fn() };
}

// ---------------------------------------------------------------------------
// Regression corpus: real programs from p8-p1-confirm-01 that the previous
// transform corrupted. The bugs were NaN edit offsets from a mutated AST and a
// NaN-corrupted sort that inserted the check into for-init/identifiers.
// ---------------------------------------------------------------------------
test('regression: nested single-statement for-loops from real agent programs stay parseable', async () => {
  const corpus = [
    `for (let dx = -10; dx <= 10; dx++) for (let dy = -6; dy <= 6; dy++) for (let dz = -10; dz <= 10; dz++) {
  const bl = bot.blockAt(p.offset(dx, dy, dz));
}`,
    `for (let r = 1; r <= 3; r++) {
  for (let dx = -r; dx <= r; dx++) for (let dz = -r; dz <= r; dz++) {
    const b = bot.blockAt(p.offset(dx, 0, dz));
  }
}`,
    `for (const k of Object.keys(bot.players)) {
  if (!bot.players[k]) continue;
}`,
    `const ores = [];
for (let dx=-20; dx<=20; dx++) for (let dy=-14; dy<=2; dy++) for (let dz=-20; dz<=20; dz++) {
  const b2 = bot.blockAt(p.offset(dx,dy,dz));
  if (b2 && b2.name.indexOf('ore')>=0) ores.push(b2.name);
}`,
    `for (let dz = -20; dz <= __mcProbe(); dz = dz + 1) { }`,
  ];
  for (const source of corpus) {
    assertParses(source);
    const instrumented = instrumentLoopDeadlines(source);
    assert.notEqual(instrumented, source, 'valid programs must be instrumented');
    assertParses(instrumented);
    if (source.includes('__mcProbe')) continue;
    // The check is a statement inside a block, never glued to an identifier
    // or a for-init expression.
    assert.equal(/[\w$]__mcWindowCheck/.test(instrumented), false, instrumented);
  }
});

test('regression: every loop body gets exactly one check and insertion stays in-body', () => {
  const source = `for (let a = 0; a < 3; a++) for (let b = 0; b < 2; b++) {
  for (const c of [1, 2]) { }
}
let i = 0;
while (i < 2) i++;
do i--; while (i > 0);
for (const key in { x: 1 }) { }`;
  const instrumented = instrumentLoopDeadlines(source);
  // 3 for + 1 while + 1 do-while + 1 for-in = 6 loops
  assert.equal(countChecks(instrumented), 6);
  assert.match(instrumented, /\{__mcWindowCheck\(\);/);
  assert.equal(/[\w$]__mcWindowCheck/.test(instrumented), false);
});

test('instrumentation preserves program semantics for nested loops', async () => {
  const source = `let total = 0;
for (let i = 0; i < 3; i++) for (let j = 0; j < 2; j++) total += i + j;
return total;`;
  const { result } = await runInstrumented(source);
  assert.equal(result, 9);
});

test('empty loop bodies and empty statements are wrapped into valid blocks', async () => {
  const source = `let n = 0;
for (let i = 0; i < 3; i++);
while (n < 3) n++;
for (let i = 0; i < 2; i++) { }
return n;`;
  const { instrumented, result } = await runInstrumented(source);
  assert.equal(result, 3);
  assertParses(instrumented);
  assert.equal(countChecks(instrumented), 3);
});

test('loop-like text inside strings, templates and comments is not instrumented', async () => {
  const source = `const text = 'for (let x=0;x<1;x++) { }';
const tpl = \`while (true) { }\`;
// for (;;) { }
const block = \`\${text}\`;
let n = 0;
while (n < 2) n++;
return { text, tpl, block, n };`;
  const { instrumented, result } = await runInstrumented(source);
  assert.equal(countChecks(instrumented), 1);
  assert.equal(result.text, 'for (let x=0;x<1;x++) { }');
  assert.equal(result.tpl, 'while (true) { }');
  assert.equal(result.n, 2);
});

test('non-ASCII source before loops keeps insertion offsets valid', async () => {
  const source = `const note = '中文 🧱 emoji';
const counts = [];
for (const ch of note) {
  if (ch.length > 0) counts.push(ch);
}
return counts.length;`;
  const { result } = await runInstrumented(source);
  assert.ok(result > 3);
});

test('labeled loops and nested single-statement bodies parse and keep labels working', async () => {
  const source = `let visits = 0;
outer: for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) {
  if (i === 1 && j === 1) continue outer;
  visits++;
}
return visits;`;
  const { result } = await runInstrumented(source);
  assert.equal(result, 7);
});

test('deadline check is reachable inside an await-then-infinite loop', async () => {
  const source = `await Promise.resolve();
while (true) { }
return 'unreachable';`;
  const instrumented = instrumentLoopDeadlines(source);
  assertParses(instrumented);
  let checks = 0;
  const context = vm.createContext({
    __mcWindowCheck: () => {
      checks++;
      if (checks > 5) { const error = new Error('deadline reached'); error.name = 'TimeoutError'; throw error; }
    },
  });
  const fn = vm.runInContext(WRAP_PREFIX + instrumented + WRAP_SUFFIX, context, { filename: 'loop-guard-deadline.js' });
  await assert.rejects(fn(), /deadline reached/);
  assert.ok(checks > 5);
});

test('invalid original source is returned unchanged and the fallback callback fires', () => {
  const bad = 'const = 1;';
  const reasons = [];
  const result = instrumentLoopDeadlines(bad, { onFallback: reason => reasons.push(reason) });
  assert.equal(result, bad);
  assert.deepEqual(reasons, ['original-parse-error']);
});

test('idempotence guard: fallback callback failures never throw', () => {
  const bad = '}';
  const result = instrumentLoopDeadlines(bad, { onFallback: () => { throw new Error('callback bug'); } });
  assert.equal(result, bad);
});

test('a valid source with no loops is returned unchanged', () => {
  const source = 'const x = 1; return x + 1;';
  assert.equal(instrumentLoopDeadlines(source), source);
});
