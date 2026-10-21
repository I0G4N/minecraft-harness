// Deterministic loop-deadline instrumentation for generated paper programs.
// Acorn (MIT) is vendored under ./vendor/acorn.mjs; this is a source transform,
// not a security boundary. It lets a CPU loop after `await` throw a catchable
// deadline error on its next iteration instead of wedging the worker.
import * as acorn from './vendor/acorn.mjs';

const CHECK = '__mcWindowCheck';

function instrumentNode(node) {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node)) { for (const child of node) instrumentNode(child); return; }
  if (typeof node.type === 'string') {
    if (['WhileStatement', 'DoWhileStatement', 'ForStatement', 'ForInStatement', 'ForOfStatement'].includes(node.type)) {
      const check = { type: 'ExpressionStatement', expression: { type: 'CallExpression', callee: { type: 'Identifier', name: CHECK }, arguments: [] } };
      if (node.body && node.body.type === 'BlockStatement') {
        node.body.body.unshift(check);
      } else if (node.body) {
        node.body = { type: 'BlockStatement', body: [check, node.body] };
      }
    }
  }
  for (const [key, value] of Object.entries(node)) {
    if (key === 'start' || key === 'end' || key === 'loc' || key === 'range') continue;
    instrumentNode(value);
  }
}

export function instrumentLoopDeadlines(source) {
  if (typeof source !== 'string' || source.length > 524288) return source;
  let ast;
  try {
    ast = acorn.parse('(async function(){\n' + source + '\n})', {
      ecmaVersion: 'latest', sourceType: 'script', allowAwaitOutsideFunction: false,
      allowReturnOutsideFunction: false,
    });
  } catch {
    return source;
  }
  try {
    instrumentNode(ast);
    // Regenerate only the inserted check via text offsets would require a code
    // generator; acorn has none. Use a second parse of a marked wrapper? We
    // instead rely on the AST node ranges: insert checks by source offsets.
    const inserts = [];
    function walk(node) {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node)) { for (const child of node) walk(child); return; }
      if (typeof node.type === 'string') {
        if (['WhileStatement', 'DoWhileStatement', 'ForStatement', 'ForInStatement', 'ForOfStatement'].includes(node.type)) {
          if (node.body && node.body.type === 'BlockStatement') {
            inserts.push(node.body.start + 1);
          } else if (node.body) {
            // Wrap non-block bodies: `{check; original}`.
            inserts.push({ prefix: node.body.start, text: '{' + CHECK + '();' });
            inserts.push({ prefix: node.body.end, text: '}' });
          }
        }
      }
      for (const [key, value] of Object.entries(node)) {
        if (key === 'start' || key === 'end' || key === 'loc' || key === 'range') continue;
        walk(value);
      }
    }
    walk(ast);
    const edits = inserts.map(item => typeof item === 'number' ? { at: item, text: CHECK + '();' } : { at: item.prefix, text: item.text });
    edits.sort((a, b) => b.at - a.at);
    let output = source;
    const offsetBase = '(async function(){\n'.length;
    for (const edit of edits) {
      const at = edit.at - offsetBase;
      if (at >= 0 && at <= output.length) output = output.slice(0, at) + edit.text + output.slice(at);
    }
    return output;
  } catch {
    return source;
  }
}
