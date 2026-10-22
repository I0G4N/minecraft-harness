// The sandbox has no IPC/control socket. Its only channel is bounded JSON lines
// over stdin/stdout owned by the separate launcher host.
import readline from 'node:readline';
// The viewer and some libraries write human-readable lines to stdout. Only
// newline-delimited JSON frames are legal on the launcher protocol; redirect
// non-JSON output to stderr so a viewer log cannot corrupt the control channel.
const protocolWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = (chunk, encoding, callback) => {
  const text = Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
  if (text.trimStart().startsWith('{')) return protocolWrite(chunk, encoding, callback);
  return process.stderr.write(chunk, encoding, callback);
};
process.connected = true;
process.send = (message, callback) => {
  const encoded = JSON.stringify(message);
  if (Buffer.byteLength(encoded) > 1048576) { callback?.(new Error('Worker frame too large')); return false; }
  return process.stdout.write(encoded + '\n', error => callback?.(error));
};
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on('line', line => {
  if (Buffer.byteLength(line) > 1048576) process.exit(76);
  try { process.emit('message', JSON.parse(line)); }
  catch { process.exit(77); }
});
lines.on('close', () => { process.connected = false; process.emit('disconnect'); process.exit(0); });
await import('./worker.js');
