<div align="center">

# ccusage-dashboard

**A tiny local dashboard built specifically for [ccusage](https://github.com/ccusage/ccusage).**

> ⚠️ **This is not a re-implementation.** Every number you see comes straight from
> `ccusage ... --json`. No own pricing tables, no re-parsing of session logs for costs —
> the dashboard is a thin view over ccusage's own accurate cost engine. If `ccusage`
> says it, this dashboard shows it.
>
> The only exception is the **workdir label** in the Sessions tab: ccusage's session
> report doesn't expose a real directory name for every agent, so the dashboard reads
> each agent's own local session file (pi/openclaw/claude/codex stores) to display the
> actual working directory. This is purely a display label — every cost/token number
> still comes 100% from ccusage.

</div>

---

## What is this?

[ccusage](https://github.com/ccusage/ccusage) is a powerful CLI that analyzes coding-agent
token usage & cost from local data — accurate, but terminal-only and hard to *watch*.

**ccusage-dashboard puts a live web UI on top of it**: today / this week / this month /
custom-range cost grouped by model, a 30-day cost trend, and a monthly budget alert.
It auto-refreshes while you work, so you can *see* spend happen instead of running reports.

Because it shells out to `ccusage` for every number, **cost estimates are always identical
to what ccusage itself reports** — the source you already trust.

## One-command install & start

```bash
npx @caius_kong/ccusage-dashboard
```

That's it. npx downloads the package (including its own `ccusage` dependency),
starts a local server on `http://127.0.0.1:8799`, and opens your browser.

> Requirements: **Node.js** (for the launcher) and **Python 3.8+** (for the server).
> On macOS: `brew install python3`. No other installs, no build step, no config.

### CLI options

```bash
npx @caius_kong/ccusage-dashboard --port 9000     # change port
npx @caius_kong/ccusage-dashboard --budget 500    # monthly budget cap (default $300)
npx @caius_kong/ccusage-dashboard --no-warm       # skip 3s startup warm-up
CCUSAGE_UI_NO_OPEN=1 npx @caius_kong/ccusage-dashboard   # don't auto-open browser
```

## What it shows

| | |
|---|---|
| **Today / This Week / This Month / Custom Range** | totals + tokens + cache breakdown |
| **By model** | per-model cost, % of total, in/out/cache-read/cache-write tokens |
| **30-day trend** | daily cost bar chart (hover for values, weekends marked) |
| **Sessions** | per-session rows grouped by workdir name + short session id, filterable 7d/30d/90d/all, sorted by cost |
| **Budget alert** | monthly cap (default $300) — green <80%, yellow <100%, red ≥100% |

All costs in USD. Auto-refresh every 15s.

## How it works

```
Browser (index.html)
   │  fetch /api/...  (auto-refresh)
   ▼
server.py  (Python stdlib, zero deps)
   │  spawns:  ccusage daily/monthly/weekly ... --json --offline
   ▼
ccusage   (bundled dependency — the real cost engine)
```

- `lib/server.py` — Python stdlib HTTP server. Resolves a local ccusage
  (bundled dep → PATH → npx cache), warms caches on boot (~3s), then serves instant responses.
- `lib/index.html` — single-file dashboard. No build step, no CDN.
- `bin/ccusage-ui.js` — Node launcher (finds python3, starts server, opens browser).

## Local development

```bash
python3 lib/server.py --budget 300    # run server directly from the repo
# or
node bin/ccusage-ui.js                # same as the npx experience
```

## Releasing a new version

Releases are done entirely from the GitHub Actions tab (no local commands, full audit log):

1. Push your code changes to `main` as usual.
2. Go to **Actions → Publish to npm → Run workflow**.
3. Leave **Version** empty to auto-bump a patch, or type a semver (e.g. `1.2.3`).
4. The workflow bumps `package.json`, tags `v*`, pushes back to `main`, and publishes to npm.

You can also trigger it with the CLI:
```bash
gh workflow run "Publish to npm"   # bumps patch
```

## License

MIT © Caius Kong
