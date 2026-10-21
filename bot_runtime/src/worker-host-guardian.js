// Separate trusted launcher process: a dead / wedged launcher cannot leave a
// running generated-code container. No worker namespace receives this channel.
import { spawn } from 'node:child_process';
const id = process.env.WORKER_HOST_GUARD_ID;
const timeoutMs = Number(process.env.WORKER_HOST_GUARD_MS ?? 10000);
if (!/^[A-Za-z0-9_-]{1,48}$/.test(id ?? '') || !Number.isSafeInteger(timeoutMs) || timeoutMs < 100) process.exit(79);
let last = performance.now(); let stopping = false;
function command(args) {
  return new Promise((resolve, reject) => {
    const child = spawn('docker', args, { stdio: ['ignore', 'pipe', 'ignore'] });
    let output = ''; const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('Docker guardian deadline')); }, 15000);
    child.stdout.on('data', chunk => { output = (output + chunk).slice(-65536); });
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('exit', code => { clearTimeout(timer); code === 0 ? resolve(output) : reject(new Error('Docker guardian operation failed')); });
  });
}
async function fence() {
  if (stopping) return; stopping = true;
  // Retry transient Docker daemon errors. Main control transport refuses to
  // acknowledge stop until the daemon confirms removal independently.
  for (let attempt = 0; attempt < 10; attempt++) {
    try {
      const containers = await command(['ps', '-aq', '--filter', `label=mc-harness.worker-host=${id}`]);
      for (const container of containers.trim().split(/\s+/).filter(Boolean)) await command(['rm', '-f', container]);
      // Let an in-flight Docker create finish before the final sweep. Containers
      // are created stopped; only the live launcher may start them.
      await new Promise(resolve => setTimeout(resolve, 1000));
      const final = await command(['ps', '-aq', '--filter', `label=mc-harness.worker-host=${id}`]);
      for (const container of final.trim().split(/\s+/).filter(Boolean)) await command(['rm', '-f', container]);
      process.exit(0);
    } catch { await new Promise(resolve => setTimeout(resolve, Math.min(2000, 100 * 2 ** attempt))); }
  }
  process.exit(80);
}
process.on('message', message => {
  if (message.type === 'pulse') last = performance.now();
  else if (message.type === 'stop') { stopping = true; process.exit(0); }
});
process.on('disconnect', fence);
setInterval(() => { if (performance.now() - last > timeoutMs) fence(); }, Math.max(25, Math.min(1000, timeoutMs / 4)));
