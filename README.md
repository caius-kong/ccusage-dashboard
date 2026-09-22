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

Only the view you're looking at is polled: **Today** (the default) refreshes every 60s;
**Week / Month / Custom Range** refresh every 10 minutes. Leaving the dashboard on Today
never re-scans the longer periods in the background.

Because it shells out to `ccusage` for every number, **cost estimates are always identical
to what ccusage itself reports** — the source you already trust.

## One-command install & start

```bash
npx @caius_kong/ccusage-dashboard
```

That's it. npx downloads the package, uses the `ccusage` already on your machine
(or fetches the latest via `npx`), starts a local server on `http://127.0.0.1:8799`,
and prints the dashboard URL for you to open.

> Requirements: **Node.js** (for the launcher) and **Python 3.8+** (for the server).
> On macOS: `brew install python3`. No other installs, no build step, no config.

### CLI options

```bash
npx @caius_kong/ccusage-dashboard --port 9000     # change port
npx @caius_kong/ccusage-dashboard --budget 500    # monthly budget cap (default $300)
npx @caius_kong/ccusage-dashboard --no-warm       # skip background warm-up
npx @caius_kong/ccusage-dashboard --foreground   # run attached to this terminal (Ctrl+C stops it)
npx @caius_kong/ccusage-dashboard --stop          # stop the background instance
npx @caius_kong/ccusage-dashboard --no-update-check  # disable the npm update hint entirely
```

Once started, the launcher prints the dashboard URL — open it in your browser
(no browser is auto-launched).

By default the dashboard runs **in the background**: after printing its URL the
launcher exits and the server keeps running, so you can close the terminal. Use
`--stop` to shut it down; running it again reuses the already-running instance.
`--foreground` keeps it attached to the terminal instead (Ctrl+C to stop).

## What it shows

| | |
|---|---|
| **Today / This Week / This Month / Custom Range** | totals + tokens + cache breakdown + **cache hit rate** |
| **By model** | per-model cost, % of total, in/out/cache-read/cache-write tokens |
| **30-day trend** | daily cost bar chart (hover for values, weekends marked) |
| **Sessions** | per-session rows grouped by workdir name + short session id, sorted by cost — shares the top time-filter tabs (today/week/month/custom) |
| **Budget alert** | monthly cap (default $300) — green <80%, yellow <100%, red ≥100% |

All costs in USD. **Cache hit rate** is the standard input-side metric:
`cacheReadTokens / (cacheReadTokens + non-cached inputTokens)`. Auto-refresh every 60s on the
default Today view (10 min for the other views; budget + trend 10 min).

## Update check (opt-out, purely manual)

This dashboard is otherwise fully offline. The only way it touches the network
is when **you click the "⇪ check update" button** in the header: the server then
makes one quick request to the npm registry and pops up the result — up to date,
update available (with the exact `npx @caius_kong/ccusage-dashboard@latest`
command to run yourself in a terminal), or unreachable. Nothing is checked
automatically, ever. Disable even this with `--no-update-check` or
`CCUSAGE_NO_UPDATE_CHECK=1`.

## How it works

```
Browser (index.html)
   │  fetch /api/...  (polls only the ACTIVE view: today 60s, week/month/range 10min)
   ▼
server.py  (Python stdlib, zero deps)
   │  spawns:  ccusage daily/monthly/weekly/session ... --json --offline
   ▼
ccusage   (your installed version — the real cost engine)
```

- `lib/server.py` — Python stdlib HTTP server. Resolves ccusage the same way you run
  it (PATH → `npx ccusage`), so it always uses the system's version. Warms the reports the
  first page load needs, then serves cached JSON: same-key requests coalesce onto one run
  (single-flight), and a small semaphore keeps different keys from stacking full-history
  scans on top of each other.
- `lib/index.html` — single-file dashboard. No build step, no CDN.
- `bin/ccusage-ui.js` — Node launcher (finds python3, starts server, prints URL).

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
