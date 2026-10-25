// Deterministic loop-deadline instrumentation for generated paper programs.
// Acorn (MIT) is vendored under ./vendor/acorn.mjs; this is a source transform,
// not a security boundary. It lets a CPU loop after `await` throw a catchable
// deadline error on its next iteration instead of wedging the worker.
//
// Implementation notes (2026-10-25 rewrite): the previous version mutated the
// AST (replacing non-block loop bodies with synthetic BlockStatements that had
// no `start`/`end`) and then walked that mutated AST. That produced NaN edit
// offsets, and the NaN entries corrupted the descending sort comparator, so
// valid edits were applied in a shifted order and inserted the check inside
// identifiers/for-init expressions (e.g. `for (let dz = -10;__mcWindowCheck();
// dz <= 10; dz++)`), turning valid agent programs into SyntaxErrors. The
// transform now:
//   * never mutates the AST (offset analysis only),
//   * inserts a check at the start of each loop body (block: after `{`;
//     single-statement/empty body: wrap in `{check; ...}`),
//   * groups edits by source offset with a deterministic prefix/suffix order,
//   * validates every offset and re-parses the result, falling back to the
//     original source if anything is off (the program still runs, with loop
//     protection lost, instead of being rejected).
import * as acorn from './vendor/acorn.mjs';

const CHECK = '__mcWindowCheck';
const WRAP_PREFIX = '(async function(){\n';
const WRAP_SUFFIX = '\n})';
const LOOP_TYPES = new Set([
  'WhileStatement', 'DoWhileStatement', 'ForStatement', 'ForInStatement', 'ForOfStatement',
]);
const MAX_EDITS = 20000;

function parseWrapped(source) {
  return acorn.parse(WRAP_PREFIX + source + WRAP_SUFFIX, {
    ecmaVersion: 'latest', sourceType: 'script',
    allowAwaitOutsideFunction: false, allowReturnOutsideFunction: false,
  });
}

// Collect `{at, text}` edits in source coordinates. Never mutates the AST.
function collectEdits(ast, sourceLength) {
  const edits = [];
  let invalid = null;
  function fail(reason) { if (!invalid) invalid = reason; }
  function walk(node) {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { for (const child of node) walk(child); return; }
    if (typeof node.type !== 'string') return;
    if (LOOP_TYPES.has(node.type)) {
      const body = node.body;
      if (!body || typeof body !== 'object') {
        fail('missing-loop-body');
      } else if (!Number.isInteger(body.start) || !Number.isInteger(body.end)) {
        fail('missing-loop-body-range');
      } else {
        const start = body.start - WRAP_PREFIX.length;
        const end = body.end - WRAP_PREFIX.length;
        if (body.type === 'BlockStatement') {
          // Insert immediately after the opening `{` of the body block.
          edits.push({ at: start + 1, kind: 'check', text: CHECK + '();' });
        } else {
          // Wrap a single-statement or empty body so the check is a statement:
          // `for (...)` + `{CHECK; original-statement}`.
          edits.push({ at: start, kind: 'wrap-open', text: '{' + CHECK + '();' });
          edits.push({ at: end, kind: 'wrap-close', text: '}' });
        }
      }
    }
    for (const key of Object.keys(node)) {
      if (key === 'start' || key === 'end' || key === 'loc' || key === 'range') continue;
      const value = node[key];
      if (value && typeof value === 'object') walk(value);
    }
  }
  walk(ast);
  for (const edit of edits) {
    if (!Number.isInteger(edit.at) || edit.at < 0 || edit.at > sourceLength) {
      fail('invalid-edit-offset');
      break;
    }
  }
  if (edits.length > MAX_EDITS) fail('too-many-edits');
  return { edits, invalid };
}

function applyEdits(source, edits) {
  const grouped = new Map();
  for (const edit of edits) {
    if (!grouped.has(edit.at)) grouped.set(edit.at, []);
    grouped.get(edit.at).push(edit);
  }
  let output = source;
  // Apply right-to-left so earlier offsets stay valid. At the same offset the
  // wrapping prefix `{CHECK;` must be emitted before the closing `}` (relevant
  // for an empty loop body where start === end).
  const positions = [...grouped.keys()].sort((left, right) => right - left);
  for (const at of positions) {
    const group = grouped.get(at);
    group.sort((left, right) => {
      const rank = edit => edit.kind === 'wrap-open' || edit.kind === 'check' ? 0 : 1;
      return rank(left) - rank(right);
    });
    output = output.slice(0, at) + group.map(edit => edit.text).join('') + output.slice(at);
  }
  return output;
}

/**
 * Insert a deadline check at the start of every loop body in `source`.
 *
 * @param {string} source generated program body (the argument is wrapped in
 *   `(async function(){ ... })`, so `await`/`return` are allowed at top level).
 * @param {{onFallback?: (reason: string) => void}} [options] optional diagnostic
 *   callback; called with a short reason when the source is left uninstrumented.
 * @returns {string} instrumented source, or the original source when the
 *   transform cannot be applied safely.
 */
export function instrumentLoopDeadlines(source, options = {}) {
  if (typeof source !== 'string' || source.length > 524288) return source;
  const onFallback = typeof options === 'object' && options && typeof options.onFallback === 'function'
    ? options.onFallback : null;
  const fallback = reason => {
    if (onFallback) { try { onFallback(reason); } catch { /* diagnostics never throw */ } }
    return source;
  };
  let ast;
  try {
    ast = parseWrapped(source);
  } catch {
    // The original program is already invalid; exec() will report the real
    // SyntaxError with its own line/caret to the agent.
    return fallback('original-parse-error');
  }
  try {
    const { edits, invalid } = collectEdits(ast, source.length);
    if (invalid) return fallback(invalid);
    if (!edits.length) return source;
    const output = applyEdits(source, edits);
    try {
      parseWrapped(output);
    } catch {
      return fallback('instrumented-parse-error');
    }
    return output;
  } catch {
    return fallback('instrumentation-exception');
  }
}
