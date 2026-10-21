// An independent event loop fences a detached worker group if its HTTP parent
// dies (including SIGKILL) or stops answering. It never receives credentials.
import { readFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';

const workerPid = Number(process.env.GUARDED_WORKER_PID);
const timeoutMs = Number(process.env.PARENT_WATCHDOG_MS ?? 10000);
const maxRssBytes = Number(process.env.MAX_WORKER_RSS_BYTES ?? 536870912);
let lastPulse = performance.now();
function fence(reason = 'runtime_parent_disconnected') {
  if (process.connected) { try { process.send({ type: 'fenced', reason }); } catch {} }
  try { process.kill(process.platform === 'win32' ? workerPid : -workerPid, 'SIGKILL'); }
  catch (error) { if (error.code !== 'ESRCH') process.exitCode = 1; }
  process.exit(process.exitCode ?? 0);
}
process.on('disconnect', () => fence());
process.on('message', message => {
  if (message?.type === 'pulse') lastPulse = performance.now();
  else if (message?.type === 'stop') fence('worker_stopped');
});
process.on('SIGTERM', () => fence('watchdog_shutdown'));
process.on('SIGINT', () => fence('watchdog_shutdown'));
setInterval(() => {
  if (performance.now() - lastPulse > timeoutMs) { fence('runtime_parent_unresponsive'); return; }
  if (process.platform === 'linux') {
    try {
      const status = readFileSync(`/proc/${workerPid}/status`, 'utf8');
      const rss = Number(status.match(/^VmRSS:\s+(\d+)/m)?.[1] ?? 0) * 1024;
      if (rss > maxRssBytes) fence('worker_rss_limit');
    } catch (error) { if (error.code === 'ENOENT') fence('worker_gone'); }
  }
}, Math.max(20, Math.min(500, timeoutMs / 4)));
