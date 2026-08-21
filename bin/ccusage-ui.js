#!/usr/bin/env node
/**
 * ccusage-dashboard — one-command launcher.
 *
 * Locates the bundled server.py (and its index.html sibling) and runs it with
 * the local python3, forwarding any CLI args. The server itself shells out to
 * ccusage for all numbers, so the dashboard always matches ccusage.
 *
 * Startup UX: once the server is up, a clear URL banner is printed so the
 * user can click/copy it to open the dashboard. No background magic.
 */
'use strict';

const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');

// Data files live in package/lib (installed layout) or the repo root (dev layout).
function resolveLibFile(name) {
  for (const dir of [path.join(__dirname, '..', 'lib'), path.join(__dirname, '..')]) {
    const p = path.join(dir, name);
    if (fs.existsSync(p)) return p;
  }
  console.error(`[ccusage-dashboard] missing bundled file ${name} — broken install?`);
  process.exit(1);
}

const serverPy = resolveLibFile('server.py');

function findPython() {
  for (const bin of ['python3', 'python']) {
    try {
      const r = spawnSync(bin, ['--version'], { stdio: 'ignore', timeout: 5000 });
      if (r.status === 0) return bin;
    } catch { /* try next */ }
  }
  return null;
}

const py = findPython();
if (!py) {
  console.error('[ccusage-dashboard] python3 is required but was not found on PATH.');
  console.error('Install it with:  brew install python3   (macOS)');
  process.exit(1);
}

// Parse the user's --port / --host so the banner points at the real URL.
const rawArgs = process.argv.slice(2);
function argValue(name, fallback) {
  const i = rawArgs.indexOf(name);
  return i >= 0 && rawArgs[i + 1] ? rawArgs[i + 1] : fallback;
}
const host = argValue('--host', '127.0.0.1');
const port = parseInt(argValue('--port', '8799'), 10);
const url = `http://${host}:${port}`;

const args = rawArgs.slice();

const child = spawn(py, [serverPy, ...args], { stdio: 'inherit', env: { ...process.env } });

// Once the server answers /, print a clear URL banner for manual opening.
let bannerPrinted = false;
function printBanner() {
  if (bannerPrinted) return;
  bannerPrinted = true;
  const line = '─'.repeat(Math.max(20, url.length + 6));
  console.log('');
  console.log(line);
  console.log(`  Dashboard is running →  ${url}`);
  console.log(`  Open it manually:      ${url}`);
  console.log(line);
  console.log('');
}

let checked = false;
function pingAndBanner() {
  if (checked) return;
  checked = true;
  const req = http.get(url + '/api/health', { timeout: 1500 }, (res) => {
    if (res.statusCode === 200) printBanner();
    else setTimeout(pingAndBanner, 500);
  });
  req.on('error', () => setTimeout(pingAndBanner, 500));
}

setTimeout(pingAndBanner, 800); // give the python server a beat to bind

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => { if (!child.killed) child.kill(sig); });
}
child.on('exit', (code) => process.exit(code == null ? 0 : code));