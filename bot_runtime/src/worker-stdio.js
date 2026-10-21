// The sandbox has no IPC/control socket. Its only channel is bounded JSON lines
// over stdin/stdout owned by the separate launcher host.
import readline from 'node:readline';
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
