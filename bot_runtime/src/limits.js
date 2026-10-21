// Keep serialization inside the disposable worker bounded. Do not invoke agent
// getters/toJSON or inspect custom hooks while collecting an execution result.
export function boundedValue(input, maxBytes = 262144) {
  let remaining = maxBytes;
  let entries = 0;
  let truncated = false;
  const seen = new WeakSet();
  function visit(value, depth) {
    if (remaining <= 0 || entries++ >= 8192 || depth > 24) { truncated = true; return '[Truncated]'; }
    if (value === null || value === undefined) { remaining -= 4; return null; }
    if (typeof value === 'boolean' || typeof value === 'number') { remaining -= 24; return Number.isFinite(value) || typeof value === 'boolean' ? value : null; }
    if (typeof value !== 'object') {
      const source = typeof value === 'string' ? value : typeof value === 'bigint' ? String(value) : `[${typeof value}]`;
      // Account conservatively for JSON escaping and UTF-8 encoding.
      const length = Math.max(0, Math.min(source.length, Math.floor(remaining / 6) - 2));
      const out = source.slice(0, length);
      remaining -= 6 * out.length + 12;
      if (length < source.length) truncated = true;
      return out;
    }
    if (seen.has(value)) { truncated = true; return '[Circular]'; }
    seen.add(value);
    const out = Array.isArray(value) ? [] : {};
    remaining -= 2;
    try {
      if (Array.isArray(out)) {
        for (let index = 0; index < value.length; index++) {
          if (remaining < 64 || entries >= 8192) { truncated = true; break; }
          const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
          if (!descriptor) { out.push(null); remaining -= 5; entries++; continue; }
          if ('value' in descriptor) out.push(visit(descriptor.value, depth + 1));
          else { out.push('[Accessor omitted]'); truncated = true; remaining -= 24; entries++; }
        }
      } else for (const key in value) {
        if (!Object.hasOwn(value, key)) continue;
        if (remaining < 64 || entries >= 8192) { truncated = true; break; }
        const descriptor = Object.getOwnPropertyDescriptor(value, key);
        const name = String(key).slice(0, 256);
        if (name !== key) truncated = true;
        remaining -= name.length * 6 + 4;
        const child = descriptor && 'value' in descriptor ? visit(descriptor.value, depth + 1) : '[Accessor omitted]';
        if (!descriptor || !('value' in descriptor)) truncated = true;
        Object.defineProperty(out, name, { value: child, enumerable: true, writable: true, configurable: true });
      }
    } catch { truncated = true; }
    seen.delete(value);
    return out;
  }
  return { value: visit(input, 0), truncated };
}
