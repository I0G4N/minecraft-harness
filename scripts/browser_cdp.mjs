/** Minimal local Chrome DevTools driver; Node 22 built-ins, no npm install. */
import {spawn} from 'node:child_process';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

class Connection {
  constructor(socket) {
    this.socket = socket;
    this.sequence = 0;
    this.pending = new Map();
    this.events = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const item = this.pending.get(message.id);
        if (!item) return;
        this.pending.delete(message.id);
        clearTimeout(item.timer);
        message.error ? item.reject(new Error(JSON.stringify(message.error))) : item.resolve(message.result);
      } else this.events.push(message);
    });
    socket.addEventListener('close', () => {
      for (const item of this.pending.values()) {
        clearTimeout(item.timer);
        item.reject(new Error('Chrome DevTools socket closed'));
      }
      this.pending.clear();
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Chrome DevTools connect timeout')), 10000);
      socket.addEventListener('open', () => {clearTimeout(timer); resolve();}, {once: true});
      socket.addEventListener('error', event => {clearTimeout(timer); reject(new Error(event.message || 'Chrome DevTools connect failed'));}, {once: true});
    });
    return new Connection(socket);
  }

  send(method, params = {}, timeout = 15000) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {this.pending.delete(id); reject(new Error(`CDP ${method} timeout`));}, timeout);
      this.pending.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({id, method, params}));
    });
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  }

  async waitFor(expression, timeout = 15000) {
    const deadline = Date.now() + timeout;
    let lastError;
    while (Date.now() < deadline) {
      try { if (await this.evaluate(expression)) return; } catch (error) {lastError = error;}
      await delay(100);
    }
    throw new Error(`Browser condition timeout: ${expression}${lastError ? ` (${lastError.message})` : ''}`);
  }

  async click(selector) {
    const box = await this.evaluate(`(async () => { const e=document.querySelector(${JSON.stringify(selector)}); if(!e) throw Error('Missing control'); e.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'}); await Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{}))); const r=e.getBoundingClientRect(); const box={x:r.x+r.width/2,y:r.y+r.height/2}; if(!r.width||!r.height||box.x<0||box.x>innerWidth||box.y<0||box.y>innerHeight) throw Error('Control is not visible: '+${JSON.stringify(selector)}); return box; })()`);
    await this.send('Input.dispatchMouseEvent', {type: 'mousePressed', button: 'left', clickCount: 1, ...box});
    await this.send('Input.dispatchMouseEvent', {type: 'mouseReleased', button: 'left', clickCount: 1, ...box});
  }

  async fill(selector, value) {
    await this.evaluate(`(() => {const e=document.querySelector(${JSON.stringify(selector)}); if(!e) throw Error('Missing input'); e.focus(); e.value=${JSON.stringify(value)}; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  }

  async viewport(width, height = 1000) {
    await this.send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: width < 600});
    await delay(300); // Responsive sidebar transitions must finish before clicking or capturing.
  }

  async screenshot(path) {
    await this.evaluate(`Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{})))`);
    const value = await this.send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    await writeFile(path, Buffer.from(value.data, 'base64'));
  }
}

export async function openBrowser({executable, width = 1440, height = 1000, extraArgs = []} = {}) {
  executable ||= process.env.DASHBOARD_CHROME || '/home/swarmbrain/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell';
  let profile;
  try {profile = await mkdtemp(join(tmpdir(), 'mc-dashboard-browser-'));}
  catch (error) {
    if (!['EACCES', 'ENOENT', 'EROFS'].includes(error.code) || tmpdir() === '/tmp') throw error;
    profile = await mkdtemp('/tmp/mc-dashboard-browser-');
  }
  const child = spawn(executable, [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--disable-background-networking',
    '--disable-component-update', '--disable-sync', '--no-first-run', '--no-default-browser-check',
    '--disable-features=MediaRouter', '--no-proxy-server', '--proxy-bypass-list=*',
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', `--user-data-dir=${profile}`, ...extraArgs, 'about:blank',
  ], {detached: true, stdio: ['ignore', 'ignore', 'pipe'], env: {...process.env, TMPDIR: '/tmp', TMP: '/tmp', TEMP: '/tmp'}});
  let stderr = '', spawnError;
  child.on('error', error => {spawnError = error;});
  child.stderr.on('data', chunk => {stderr = (stderr + chunk).slice(-16000);});
  let client;
  const close = async () => {
    if (client?.socket.readyState === WebSocket.OPEN) {
      try {await client.send('Browser.close', {}, 2000);} catch {}
      client.socket.close();
    }
    for (let attempt = 0; attempt < 20 && child.exitCode === null; attempt++) await delay(50);
    if (child.exitCode === null) {try {process.kill(-child.pid, 'SIGKILL');} catch {}}
    await rm(profile, {recursive: true, force: true});
  };
  try {
    let port;
    for (let attempt = 0; attempt < 100; attempt++) {
      if (spawnError) throw spawnError;
      if (child.exitCode !== null || child.signalCode !== null) throw new Error(`Chrome exited ${child.exitCode ?? child.signalCode}: ${stderr}`);
      try {port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break;} catch {}
      await delay(50);
    }
    if (!port) throw new Error(`Chrome startup timeout: ${stderr}`);
    const address = `http://127.0.0.1:${port}`;
    const version = await (await fetch(`${address}/json/version`)).json();
    const page = await (await fetch(`${address}/json/new?about:blank`, {method: 'PUT'})).json();
    client = await Connection.connect(page.webSocketDebuggerUrl);
    await Promise.all(['Page', 'Runtime', 'Network', 'Log'].map(domain => client.send(`${domain}.enable`)));
    await client.viewport(width, height);
    return {client, close, version, executable, pid: child.pid, stderr: () => stderr};
  } catch (error) {await close(); throw error;}
}
