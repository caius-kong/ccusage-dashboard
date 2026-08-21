#!/usr/bin/env node
/**
 * ccusage-dashboard — one-command launcher.
 *
 * Locates the bundled server.py (and its index.html sibling) and runs it with
 * the local python3, forwarding any CLI args. The server itself shells out to
 * ccusage for all numbers, so the dashboard always matches ccusage.
 *
 * Modes:
 *   foreground (default) — the server runs attached to this terminal; Ctrl+C
 *                          stops it. A URL banner is printed when it is up.
 *   --daemon             — start the server detached in the background, print
 *                          the URL, and exit immediately. The server keeps
 *                          running after this terminal closes. If a server is
 *                          already up on the port, it reuses it instead of
 *                          starting a second one.
 *
 * Stop a daemon with:  npx @caius_kong/ccusage-dashboard --stop
 */
'use strict';

const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');

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

// --- arg parsing -----------------------------------------------------------
const rawArgs = process.argv.slice(2);
function argValue(name, fallback) {
  const i = rawArgs.indexOf(name);
  return i >= 0 && rawArgs[i + 1] ? rawArgs[i + 1] : fallback;
}
const host = argValue('--host', '127.0.0.1');
const port = parseInt(argValue('--port', '8799'), 10);
const url = `http://${host}:${port}`;
const wantDaemon = rawArgs.includes('--daemon');
const wantStop = rawArgs.includes('--stop');

// Filter launcher-only flags before forwarding to server.py.
const serverArgs = rawArgs.filter((a) => !['--daemon', '--stop'].includes(a));

// --- daemon pid file (per host:port) ----------------------------------------
function pidFile() {
  const safeHost = host.replace(/[^a-z0-9]/gi, '_');
  return path.join(os.tmpdir(), `ccusage-dashboard-${safeHost}-${port}.pid`);
}

function readDaemonPid() {
  try {
    const pid = parseInt(fs.readFileSync(pidFile(), 'utf8').trim(), 10);
    return Number.isFinite(pid) ? pid : null;
  } catch { return null; }
}

function daemonAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch { return false; }
}

function isServerUp() {
  return new Promise((resolve) => {
    const req = http.get(url + '/api/health', { timeout: 1200 }, (res) => {
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
  });
}

function printBanner() {
  const line = '─'.repeat(Math.max(20, url.length + 6));
  console.log('');
  console.log(line);
  console.log(`  Dashboard is running →  ${url}`);
  console.log(`  Open it manually:      ${url}`);
  console.log(line);
  if (wantDaemon) {
    console.log(`  (background mode — stop with: npx @caius_kong/ccusage-dashboard --stop)`);
    console.log('');
  }
}

async function waitForUp(timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await isServerUp()) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

// --- stop -------------------------------------------------------------------
async function handleStop() {
  const pid = readDaemonPid();
  if (pid && daemonAlive(pid)) {
    try { process.kill(pid, 'SIGTERM'); } catch {}
    console.log(`Stopping dashboard (pid ${pid}) …`);
    const t0 = Date.now();
    while (Date.now() - t0 < 5000 && await isServerUp()) {
      await new Promise((r) => setTimeout(r, 300));
    }
    try { fs.unlinkSync(pidFile()); } catch {}
    console.log('Dashboard stopped.');
    process.exit(0);
  }
  console.log('No running dashboard instance found.');
  process.exit(0);
}

if (wantStop) {
  handleStop();
  return;
}

// --- daemon mode ------------------------------------------------------------
async function handleDaemon() {
  // Reuse an already-running instance on this port.
  if (await isServerUp()) {
    console.log(`A dashboard is already running at ${url} — reusing it.`);
    printBanner();
    process.exit(0);
  }
  // If the pid file points at a dead pid, clear it.
  const oldPid = readDaemonPid();
  if (oldPid && !daemonAlive(oldPid)) {
    try { fs.unlinkSync(pidFile()); } catch {}
  }
  // Start detached; the server outlives this launcher.
  const out = fs.openSync(path.join(os.tmpdir(), `ccusage-dashboard-${port}.log`), 'a');
  const child = spawn(py, [serverPy, ...serverArgs], {
    detached: true,
    stdio: ['ignore', out, out],
    env: { ...process.env },
  });
  child.unref();
  fs.writeFileSync(pidFile(), String(child.pid));
  const up = await waitForUp(15000);
  if (!up) {
    console.error(`[ccusage-dashboard] server did not come up within 15s — check ${path.join(os.tmpdir(), `ccusage-dashboard-${port}.log`)}`);
    try { process.kill(child.pid, 'SIGTERM'); } catch {}
    process.exit(1);
  }
  console.log(`ccusage-dashboard started in background (pid ${child.pid}).`);
  printBanner();
  process.exit(0);
}

if (wantDaemon) {
  handleDaemon();
  return;
}

// --- foreground mode (default) ----------------------------------------------
const child = spawn(py, [serverPy, ...serverArgs], { stdio: 'inherit', env: { ...process.env } });

// Once the server answers /, print a clear URL banner for manual opening.
let bannerPrinted = false;
function printBannerFg() {
  if (bannerPrinted) return;
  bannerPrinted = true;
  printBanner();
}

let checked = false;
function pingAndBanner() {
  if (checked) return;
  checked = true;
  const req = http.get(url + '/api/health', { timeout: 1500 }, (res) => {
    if (res.statusCode === 200) printBannerFg();
    else setTimeout(pingAndBanner, 500);
  });
  req.on('error', () => setTimeout(pingAndBanner, 500));
}

setTimeout(pingAndBanner, 800); // give the python server a beat to bind

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => { if (!child.killed) child.kill(sig); });
}
child.on('exit', (code) => process.exit(code == null ? 0 : code));
