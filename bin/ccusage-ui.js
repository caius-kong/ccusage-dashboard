#!/usr/bin/env node
/**
 * ccusage-dashboard — one-command launcher.
 *
 * Locates the bundled server.py (and its index.html sibling) and runs it with
 * the local python3, forwarding any CLI args. The server itself shells out to
 * ccusage for all numbers, so the dashboard always matches ccusage.
 */
'use strict';

const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

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

const args = process.argv.slice(2);
if (!args.includes('--open') && !process.env.CCUSAGE_UI_NO_OPEN) {
  args.push('--open'); // default: open browser after start
}

const child = spawn(py, [serverPy, ...args], { stdio: 'inherit', env: { ...process.env } });

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => { if (!child.killed) child.kill(sig); });
}
child.on('exit', (code) => process.exit(code == null ? 0 : code));